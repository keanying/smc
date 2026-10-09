"""按账号维护持久化浏览器 profile，并静默提取 Cookie。

要解决的问题（需求 3）：
    以前每次建任务都新开浏览器、重走一遍登录页，用户被反复打断。

做法：
    1. 每个账号一个持久化目录 data/browser_profiles/{channel}/{account}/，
       Playwright 的 launch_persistent_context 会把 Cookie、localStorage、
       IndexedDB 和浏览器指纹相关状态都留在这个目录里。
    2. 用户只在"账号管理"里显式登录一次。
    3. 之后任务运行时走 get_cookie_header()：
         a. DB 里的 Cookie 还新鲜   -> 直接用，完全不启动浏览器（最快）
         b. Cookie 过期但 profile 在 -> 无头静默打开 profile 取新 Cookie，
                                        因为 profile 里有登录态，不会弹登录页
         c. profile 也失效           -> 标记账号 expired 并抛 LoginRequired，
                                        由前端提示用户去重新登录那一个账号
    整个 a/b 路径对用户是无感知的。

关于事件循环（重要）：
    Playwright 的调用一律通过 SUBPROCESS_LOOP 提交到专用后台循环执行，
    数据库读写留在服务器循环上——两者绑定的循环不同，不能混。
    原因见 app/core/subprocess_loop.py 的说明。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import BrowserContext, async_playwright

from ..collectors.base import LoginRequired
from ..core.config import Config
from ..core.logging import get_logger
from .capacity import BROWSER_CAPACITY
from ..repositories.account_repo import AccountRepository
from ..core.subprocess_loop import SUBPROCESS_LOOP
from . import cookie_import
from .slots import BrowserSlots
from .specs import (
    LoginProbe, LoginSpec, cookie_names, get_spec,
    missing_login_cookies, needs_login,
)

logger = get_logger(__name__)

#: 平台自检最小间隔（秒）。登录窗口每 2 秒轮询一次，
#: 不节流就等于每 2 秒打一次平台接口——正好在用户扫码那几十秒里。
VERIFY_MIN_INTERVAL_SECONDS = 8.0

# 无头静默取 Cookie 的超时；超过就认为 profile 不可用
SILENT_TIMEOUT_SECONDS = 45


def _playwright_cookies(cookies: List[Dict]) -> List[Dict]:
    """把库里存的 Cookie 修成 add_cookies 能收的形状。

    存进去的本来就是 Playwright 结构，但历史数据里可能缺 domain，
    或者带了 add_cookies 不认识的键（Cookie-Editor 的 storeId、hostOnly 之类），
    多余的键会让整批被拒，所以这里只挑认识的字段。
    """
    allowed = ("name", "value", "domain", "path", "expires",
               "httpOnly", "secure", "sameSite")
    result = []
    for cookie in cookies:
        if not cookie.get("name") or not cookie.get("domain"):
            continue
        item = {k: cookie[k] for k in allowed if k in cookie and cookie[k] is not None}
        item.setdefault("path", "/")
        result.append(item)
    return result


class BrowserSessionManager:
    def __init__(self, config: Config, account_repo: AccountRepository):
        self.config = config
        self.accounts = account_repo
        self._locks: Dict[str, asyncio.Lock] = {}
        # 浏览器占用登记表。Chromium 不允许两个实例共用一个 user-data-dir，
        # 硬开的话拿到的往往是个全新的空 profile —— 表现就是
        # "刚扫码登录完，任务却说只有 4 个 Cookie"。
        # 登录窗口、静默取 Cookie、拟人采集都往这里登记；
        # 拟人采集还会**排队等**（见 slots.BrowserSlots.acquire）。
        self.slots = BrowserSlots()
        # Obscura 进程管理：engine=obscura 时，每个账号一个 obscura serve 进程
        self._obscura_processes: Dict[str, asyncio.subprocess.Process] = {}

    # ---------------- 路径与启动参数 ----------------
    def profile_dir(self, channel: str, account_name: str) -> Path:
        """返回账号的浏览器 profile 目录，不存在则自动重建。

        用户可能手动删除 data/browser_profiles/ 下的目录来"重置"账号，
        这里每次调用都确保目录存在，避免后续 launch_persistent_context 报错。
        """
        root = Path(self.config.get("browser.profiles_dir"))
        # 账号名可能含特殊字符，做一层安全化
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in account_name)
        path = root / channel / (safe or "default")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _use_obscura(self) -> bool:
        """是否使用 Obscura 浏览器引擎。"""
        return self.config.get("browser.engine", "playwright") == "obscura"

    def _obscura_port(self) -> int:
        """Obscura CDP 端口。"""
        return int(self.config.get("browser.obscura.port", 9223))

    def _obscura_stealth(self) -> bool:
        """Obscura 是否启用 stealth 模式。"""
        return bool(self.config.get("browser.obscura.stealth", True))

    def _obscura_executable(self) -> str:
        """Obscura 二进制路径。

        配置里写的是相对路径（如 ./obscura/obscura），
        但 subprocess 的 cwd 不一定是项目根目录，所以解析成绝对路径。
        """
        raw = self.config.get("browser.obscura.executable", "") or "obscura"
        if raw == "obscura":
            # 从 PATH 找
            return raw
        # 相对路径转绝对路径：基于项目根目录
        from ..core.config import PROJECT_ROOT
        resolved = (PROJECT_ROOT / raw).resolve()
        if resolved.exists():
            return str(resolved)
        # 找不到就按原样返回，让 subprocess 报 FileNotFoundError
        return raw

    async def _ensure_obscura(self, channel: str, account_name: str) -> str:
        """确保 Obscura 进程在运行，返回 CDP WebSocket 地址。

        每个账号一个独立的 obscura serve 进程，用 --storage-dir 隔离 Cookie。
        """
        key = f"{channel}:{account_name}"
        if key in self._obscura_processes:
            proc = self._obscura_processes[key]
            if proc.returncode is None:
                # 进程还活着
                return f"ws://127.0.0.1:{self._obscura_port()}"
            # 进程死了，清理掉
            del self._obscura_processes[key]

        # 启动新进程
        storage_dir = self.profile_dir(channel, account_name) / "obscura_storage"
        storage_dir.mkdir(parents=True, exist_ok=True)

        args = [
            self._obscura_executable(),
            "serve",
            "--port", str(self._obscura_port()),
            "--storage-dir", str(storage_dir),
        ]
        if self._obscura_stealth():
            args.append("--stealth")

        logger.info("启动 Obscura: %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._obscura_processes[key] = proc

        # 等 CDP 就绪
        import httpx
        for _ in range(30):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{self._obscura_port()}/json/version", timeout=1)
                    if resp.status_code == 200:
                        logger.info("Obscura CDP 就绪: ws://127.0.0.1:%s", self._obscura_port())
                        return f"ws://127.0.0.1:{self._obscura_port()}"
            except Exception:
                pass
            await asyncio.sleep(0.5)

        # 启动失败
        del self._obscura_processes[key]
        raise RuntimeError(f"Obscura 启动超时（30 秒）")

    async def _stop_obscura(self, channel: str, account_name: str) -> None:
        """停止指定账号的 Obscura 进程。"""
        key = f"{channel}:{account_name}"
        proc = self._obscura_processes.pop(key, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            logger.info("Obscura 已停止: %s", key)

    def headless_for_collect(self) -> bool:
        """采集 / 采集 Cookie 开不开可见窗口，由 browser.headless 决定。"""
        return bool(self.config.get("browser.headless", True))

    def headless_for_login(self) -> bool:
        """交互式登录窗口开不开可见窗口。

        单独一个开关（browser.headless_login），默认**有头**：
        无头模式下不少平台的风控会直接拦掉登录，二维码也可能渲染不出来。
        但服务跑在自己电脑上时，这个窗口会实实在在弹在桌面上打扰人，
        而画面本来就通过推流看得到，所以允许关掉。
        """
        return bool(self.config.get("browser.headless_login", False))

    @staticmethod
    def release_profile_locks(directory: Path) -> List[str]:
        """清掉 Chromium 留在 profile 里的单例锁。

        ⚠️ 这是持久化 profile 方案**迟早会踩到**的一个坑：
        Chromium 用 SingletonLock / SingletonSocket / SingletonCookie
        标记"这个目录正被我用着"。进程被强杀、机器蓝屏、服务被 kill -9
        的时候这几个文件留在原地，下次启动 Chromium 认为目录被占用，
        于是**默默开一个全新的空 profile** —— 登录态看起来凭空消失了，
        而且不报任何错。用户的感受是"昨天还好好的，今天又要重新登录"。

        只在启动前清理，且只清这几个已知文件名，不碰 profile 里别的东西。
        返回清掉了哪些，供日志和测试用。
        """
        removed: List[str] = []
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"):
            candidate = directory / name
            try:
                if candidate.exists() or candidate.is_symlink():
                    # Linux/Mac 上这几个是符号链接，exists() 对断链返回 False，
                    # 所以要连 is_symlink 一起判，否则清不掉
                    candidate.unlink()
                    removed.append(name)
            except OSError as exc:
                logger.debug("清理 %s 失败：%s", candidate, exc)
        if removed:
            logger.warning(
                "profile %s 里有上次残留的浏览器锁（%s），已清理——"
                "不清的话 Chromium 会另开一个空 profile，登录态就像凭空没了",
                directory.name, "、".join(removed),
            )
        return removed

    def _launch_kwargs(self, headless: Optional[bool] = None) -> Dict[str, Any]:
        """headless 传 None 表示"按配置来"，传布尔值则强制。"""
        if headless is None:
            headless = self.headless_for_collect()
        # 把实际生效的值打出来：用户反馈过"设置了无头还是弹窗口"，
        # 有这行日志就能一眼确认到底是配置没生效还是走了别的路径
        logger.debug("启动浏览器：headless=%s", headless)
        kwargs: Dict[str, Any] = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                # 禁用站点隔离，让跨域 iframe 能正常交互（抖音滑块验证需要）
                "--disable-features=IsolateOrigins,site-per-process",
                # ⚠️ 这里曾经有 --disable-gpu / --disable-software-rasterizer /
                # --disable-webgl / --disable-webrtc 四个参数，已全部删除。
                # 实测（同一 Chromium 二进制，两套参数各跑一次）：
                #   带这四个   -> WebGL 不可用
                #   不带       -> WebGL 可用（ANGLE/SwiftShader 软件渲染）
                # 两者的编解码器能力完全相同，所以 WebGL 是唯一差异。
                # 抖音播放器在渲染能力探测失败时报"浏览器版本过低"——
                # --disable-software-rasterizer 原来的注释写着"避免版本过低提示"，
                # 实际效果正好相反：它断掉了 SwiftShader 这条软件回退路径。
                # --disable-webgl 更是最强的机器人特征之一，真人浏览器都有 WebGL。
                # （--disable-webrtc 并非有效的 Chromium 开关，加了也没用。）
                # 参照 opinion-hub-all 的 browser_engine.py：它一个都没加，页面正常。
                # 禁用音频，避免音频指纹
                "--mute-audio",
                # 禁用通知，避免权限弹窗
                "--disable-notifications",
                # 禁用弹出窗口拦截，避免登录弹窗被拦
                "--disable-popup-blocking",
                # 禁用翻译，避免翻译弹窗
                "--disable-translate",
                # 禁用默认应用检查，避免"设为默认浏览器"提示
                "--no-default-browser-check",
                # 禁用首次运行向导，避免欢迎页
                "--no-first-run",
                # 禁用密码保存提示
                "--password-store=basic",
                # 禁用同步，避免登录 Google 账号提示
                "--disable-sync",
                # 禁用后台网络，避免遥测
                "--disable-background-networking",
                # 禁用组件更新，避免版本检查
                "--disable-component-update",
                # 禁用域可靠性，避免遥测
                "--disable-domain-reliability",
                # 禁用客户端钓鱼检测，避免安全警告
                "--disable-client-side-phishing-detection",
                # 禁用安全浏览，避免安全警告
                "--safebrowsing-disable-auto-update",
                # 禁用下载保护，避免下载警告
                "--disable-download-protection",
            ],
            # ⚠️ 对齐参考项目：去掉 --enable-automation 标记，
            # 让 navigator.webdriver 为 undefined，更像真实浏览器
            "ignore_default_args": ["--enable-automation"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
            "timezone_id": self.config.get("scheduler.timezone", "Asia/Shanghai"),
            # ⚠️ 对齐参考项目：固定桌面 Chrome UA，
            # 不固定的话 Playwright 会带 HeadlessChrome 字样，被风控识别
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        executable = self.config.get("browser.executable_path") or ""
        if executable:
            kwargs["executable_path"] = executable
        slow_mo = int(self.config.get("browser.slow_mo_ms", 0) or 0)
        if slow_mo:
            kwargs["slow_mo"] = slow_mo
        return kwargs

    def _lock_for(self, channel: str, account_name: str) -> asyncio.Lock:
        """同一账号的 profile 目录不能被两个浏览器同时打开。"""
        key = f"{channel}:{account_name}"
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ---------------- profile 占用 ----------------
    # 这几个方法是 slots 的薄封装，保留原名字是因为 PageSession / LiveBrowserSession
    # 都在用；真正的占用表在 self.slots 里，采集任务走的是能排队的 acquire_browser。
    def mark_profile_busy(self, channel: str, account_name: str, reason: str) -> None:
        self.slots.mark(channel, account_name, reason)

    def release_profile(self, channel: str, account_name: str) -> None:
        self.slots.release(channel, account_name)

    def profile_busy_reason(self, channel: str, account_name: str) -> str:
        return self.slots.reason(channel, account_name)

    def raise_if_profile_busy(self, channel: str, account_name: str) -> None:
        reason = self.profile_busy_reason(channel, account_name)
        if reason:
            raise LoginRequired(
                f"账号 [{channel}/{account_name}] 的浏览器正被占用（{reason}）。"
                f"同一个 profile 不能被两个浏览器同时打开。"
                f"如果是登录窗口开着，关掉它再跑；如果是另一个采集任务在用，"
                f"等它跑完——调度器会自动排队。"
            )

    async def acquire_browser(
        self,
        channel: str,
        accounts,
        *,
        reason: str,
        timeout=None,
        on_wait=None,
        should_cancel=None,
    ):
        """占一个能开浏览器的账号；都被占着就排队等，不跳过。

        采集任务只应该走这个入口——mark_profile_busy 是"我已经开了"的登记，
        不排队，两者不要混用。
        """
        return await self.slots.acquire(
            channel, accounts, reason=reason, timeout=timeout,
            on_wait=on_wait, should_cancel=should_cancel,
        )

    def busy_snapshot(self):
        """当前哪些账号的浏览器被占着。排障和前端展示用。"""
        return self.slots.snapshot()

    # ---------------- 登录态判定 ----------------
    @staticmethod
    async def _is_logged_in(
        context: BrowserContext,
        spec: LoginSpec,
        *,
        verify_state: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """verify_state：调用方给一个可变 dict 就能把平台自检的结果缓存住。

        登录窗口每 2 秒轮询一次，不缓存的话就是每 2 秒打一次平台接口——
        在"用户正在登录"这个最敏感的时刻高频请求，很容易把自己撞进风控。
        一次性调用（静默刷新、手动采集 Cookie）不传，每次都问最新的。
        """
        # ⚠️ 只看**采集域**能收到的 Cookie（cookie_urls）。
        # 跨注册域时拿全量 Cookie 判定会"登录成功"，但采集时那份根本用不上，
        # 用户看到的就是"登录完又让我登录"。
        cookies = await context.cookies(spec.cookie_scope)
        by_name = {c["name"]: c.get("value") for c in cookies if c.get("value")}

        # 平台自己有"我登录了吗"的接口时，以它为准。
        # Cookie key 判定只能看出字段在不在，看不出值还有没有效——
        # profile 里留着上次过期的 Cookie，key 一个不少，照样判成已登录。
        # 微博用的是 weibo.com/ajax/profile/info（失效时会被重定向到登录页）。
        if spec.probes:
            verdict = await BrowserSessionManager._ask_platform(
                context, spec, state=verify_state
            )
            if verdict is not None:
                return verdict
            # 问不到（断网、接口改了）就退回 key 判定，不能因此把用户锁在门外

        for name, expected in spec.cookie_equals.items():
            if by_name.get(name) == expected:
                return True
        for name in spec.session_cookies:
            if by_name.get(name):
                return True

        if spec.local_storage_flags:
            pages = context.pages
            if pages:
                for key, expected in spec.local_storage_flags.items():
                    try:
                        value = await pages[0].evaluate(
                            "k => { try { return localStorage.getItem(k); } catch (e) { return null; } }",
                            key,
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    if value is not None and (expected is None or value == expected):
                        return True
        return False

    @staticmethod
    async def _ask_platform(
        context: BrowserContext,
        spec: LoginSpec,
        *,
        state: Optional[Dict[str, Any]] = None,
    ) -> Optional[bool]:
        """请求 spec.verify_url，让平台自己回答登没登录。

        走 context.request —— 它和这个浏览器上下文**共用同一份 Cookie**，
        所以问到的就是这个 profile 的真实状态。
        返回 None 表示"没问出结果"（网络不通、返回不是 JSON、路径对不上），
        由调用方退回 Cookie key 判定。
        """
        now = time.monotonic()
        if state is not None:
            asked_at = state.get("at")
            # 上一轮问过而且还没到间隔，直接复用。
            # 只复用"未登录"的结论：一旦确认已登录，本来也就不再轮询了。
            if asked_at is not None and now - asked_at < VERIFY_MIN_INTERVAL_SECONDS:
                return state.get("value")

        # 逐个探测，**任意一个说"登录着"就算登录**。
        # 微博的登录态在 weibo.com，采集用的 m.weibo.cn 是 SSO 换过去的；
        # 只认后者的话，换过去之前会被误判成"登录已失效"。
        unknown = 0
        for probe in spec.probes:
            outcome = await BrowserSessionManager._run_probe(context, probe)
            if outcome is True:
                if state is not None:
                    state["at"] = now
                    state["value"] = True
                return True
            if outcome is None:
                unknown += 1

        if unknown == len(spec.probes):
            # 一个都没问出结果（断网、接口改了）→ 交回给 Cookie key 判定
            return None
        if state is not None:
            state["at"] = now
            state["value"] = False
        return False

    @staticmethod
    async def _run_probe(context: BrowserContext, probe: LoginProbe) -> Optional[bool]:
        """跑一次探测。True=登录着，False=没登录，None=问不出来。"""
        try:
            response = await context.request.get(probe.url, timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            logger.debug("登录态自检 %s 请求失败：%s", probe.url, exc)
            return None

        final_url = (response.url or "").lower()
        if any(bad in final_url for bad in probe.reject_url_contains):
            # 被踢回登录页 —— 这是"没登录"的确证，不是"问不出来"
            logger.debug("登录态自检 %s 被重定向到 %s", probe.url, final_url)
            return False
        if not response.ok:
            logger.debug("登录态自检 %s 返回 HTTP %s", probe.url, response.status)
            return None if response.status >= 500 else False

        if not probe.truthy_path:
            # 没给 JSON 路径：能 200 且没被踢回登录页就算登录着
            return True

        try:
            payload = await response.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("登录态自检 %s 返回不是 JSON：%s", probe.url, exc)
            return None

        node: Any = payload
        for key in probe.truthy_path:
            if not isinstance(node, dict) or key not in node:
                logger.debug(
                    "登录态自检返回里没有 %s：%s", probe.truthy_path, str(payload)[:200]
                )
                return None
            node = node[key]
        return bool(node)

    # ---------------- 静默刷新 ----------------
    async def _grab_cookies(
        self, spec: LoginSpec, directory: Path, seed_cookies: Optional[List[Dict]] = None
    ) -> Optional[List[Dict]]:
        """纯浏览器动作，跑在浏览器循环上。未检测到登录态返回 None。

        这里一行数据库操作都不能有——aiomysql 连接池绑在服务器循环上，
        在别的循环里用会直接坏掉。

        ⚠️ seed_cookies：把数据库里已保存的 Cookie 一起带进去当种子。
        这样即使 profile 里的 Cookie 被微博刷新/清理了一部分，
        也能从数据库里补回来，不会越采越少。
        """
        if self._use_obscura():
            return await self._grab_cookies_obscura(spec, directory, seed_cookies)
        return await self._grab_cookies_playwright(spec, directory, seed_cookies)

    async def _grab_cookies_playwright(
        self, spec: LoginSpec, directory: Path, seed_cookies: Optional[List[Dict]] = None
    ) -> Optional[List[Dict]]:
        """用 Playwright + Chromium 静默刷新 Cookie。

        定时「采集 Cookie」也要过浏览器闸门：它和采集任务抢的是同一份内存。
        不过闸门的话，一轮 Cookie 刷新能在采集正忙时再拉起十几个浏览器。
        """
        await BROWSER_CAPACITY.acquire(f"cookie/{spec.channel}")
        try:
            return await self._grab_cookies_playwright_inner(
                spec, directory, seed_cookies)
        finally:
            BROWSER_CAPACITY.release(f"cookie/{spec.channel}")

    async def _grab_cookies_playwright_inner(
        self, spec: LoginSpec, directory: Path, seed_cookies: Optional[List[Dict]] = None
    ) -> Optional[List[Dict]]:
        self.release_profile_locks(directory)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(directory), **self._launch_kwargs()
            )
            try:
                # 先把数据库里的 Cookie 种进去，再导航——顺序反了页面就是用旧状态加载的
                if seed_cookies:
                    try:
                        await context.add_cookies(_playwright_cookies(seed_cookies))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("注入已保存的 Cookie 失败：%s", exc)

                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    spec.home_url,
                    wait_until=spec.wait_until,
                    timeout=SILENT_TIMEOUT_SECONDS * 1000,
                )
                # 给页面一点时间落 Cookie
                await page.wait_for_timeout(2000)
                await self._run_handoff(page, spec)

                if not await self._is_logged_in(context, spec):
                    return None

                # 合并 profile 里的 Cookie 和数据库里的种子 Cookie：
                # profile 里的可能被微博刷新/清理了一部分，数据库里的更全。
                # 同名 Cookie 以 profile 里的为准（更新鲜），数据库里的补充缺失的。
                profile_cookies = await context.cookies(spec.cookie_scope)
                if seed_cookies:
                    profile_names = {c["name"] for c in profile_cookies}
                    for seed in seed_cookies:
                        if seed.get("name") and seed["name"] not in profile_names:
                            profile_cookies.append(seed)
                return profile_cookies
            finally:
                await context.close()

    async def _grab_cookies_obscura(
        self, spec: LoginSpec, directory: Path, seed_cookies: Optional[List[Dict]] = None
    ) -> Optional[List[Dict]]:
        """用 Obscura 静默刷新 Cookie。

        Obscura 的 --storage-dir 已经持久化了 Cookie，所以不需要像 Playwright 那样
        每次重新打开 profile。直接连 CDP 取 Cookie 即可。
        """
        # 从 directory 提取 channel 和 account_name
        # directory 格式：profiles_dir/{channel}/{account_name}
        parts = directory.parts
        if len(parts) >= 2:
            channel = parts[-2]
            account_name = parts[-1]
        else:
            channel = "unknown"
            account_name = "unknown"

        ws_url = await self._ensure_obscura(channel, account_name)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_url)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()

                # 先把数据库里的 Cookie 种进去
                if seed_cookies:
                    try:
                        await context.add_cookies(_playwright_cookies(seed_cookies))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("注入已保存的 Cookie 失败：%s", exc)

                await page.goto(
                    spec.home_url,
                    wait_until=spec.wait_until,
                    timeout=SILENT_TIMEOUT_SECONDS * 1000,
                )
                await page.wait_for_timeout(2000)
                await self._run_handoff(page, spec)

                if not await self._is_logged_in(context, spec):
                    return None

                profile_cookies = await context.cookies(spec.cookie_scope)
                if seed_cookies:
                    profile_names = {c["name"] for c in profile_cookies}
                    for seed in seed_cookies:
                        if seed.get("name") and seed["name"] not in profile_names:
                            profile_cookies.append(seed)
                return profile_cookies
            finally:
                await browser.close()

    @staticmethod
    async def _run_handoff(page, spec: LoginSpec) -> None:
        """访问采集域，让平台把会话从登录域 SSO 过来。

        ⚠️ 这一步**每次静默刷新都要做**，不只是刚登录完那一次。
        适用于"登录域和采集域不是同一个注册域"的平台：采集域那套会话
        有自己的有效期，过期后登录域通常还好好的，访问一次采集域，
        平台会自动走 SSO 把会话重新种上。少了这一步就会出现
        "明明还登录着，系统却说登录态失效，反复要求重新登录"。
        （微博曾经属于这一类，现在全走 weibo.com 了；机制留着给将来用。）

        SSO 是一串重定向，不一定一次就落地，所以给两次机会。

        ⚠️ 成功的判据是**采集域那个探测点**（probes[0]），不是"随便哪个域说登录着"。
        用后者的话，PC 站一直是登录的，第一次访问完就直接返回了，
        采集域其实还没换过来——重试等于没写。
        """
        collect_probe = spec.probes[0] if spec.probes else None
        for url in spec.handoff_urls:
            for attempt in (1, 2):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2500)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("会话交接访问 %s 失败（第 %d 次）：%s", url, attempt, exc)
                    continue
                if collect_probe is None:
                    break
                if await BrowserSessionManager._run_probe(
                    page.context, collect_probe
                ) is True:
                    return      # 采集域的会话已经种上了

    async def silent_refresh(
        self, channel: str, account_name: str, *, slot_token: str = ""
    ) -> List[Dict]:
        """无头打开账号 profile，确认登录态并取出最新 Cookie。用户无感知。

        profile 里已有登录态，所以这一步不会弹出任何登录界面；
        如果真的失效了，抛 LoginRequired，由上层提示用户去重新登录。

        ⚠️ 会把数据库里已保存的 Cookie 一起带进去当种子，
        防止 profile 里的 Cookie 被微博刷新/清理后越采越少。
        """
        spec = get_spec(channel)
        directory = self.profile_dir(channel, account_name)
        # 拿着租约的调用方（正在跑的采集任务）本来就占着这个账号，
        # 别被自己的占用挡住——那会让"API 模式中途刷 Cookie"直接失败。
        if not self.slots.held_by(channel, account_name, slot_token):
            self.raise_if_profile_busy(channel, account_name)

        # 先取数据库里已保存的 Cookie 当种子
        account = await self.accounts.get(channel, account_name)
        seed = AccountRepository.cookies_of(account) if account else []

        async with self._lock_for(channel, account_name):
            # 浏览器部分丢到专用循环；DB 部分留在当前（服务器）循环
            cookies = await SUBPROCESS_LOOP.run(
                self._grab_cookies(spec, directory, seed_cookies=seed)
            )

        if cookies is None:
            await self.accounts.mark_expired(
                channel, account_name, "静默检查时未检测到登录态"
            )
            raise LoginRequired(
                f"账号 [{channel}/{account_name}] 登录态已失效，请在账号管理里重新登录"
            )

        await self.accounts.save_session(
            channel, account_name, cookies=cookies, profile_dir=str(directory),
        )
        logger.info(
            "[%s/%s] 静默刷新成功，取到 %d 个 Cookie",
            channel, account_name, len(cookies),
        )
        return cookies

    # ---------------- 对采集器的主入口 ----------------
    async def get_cookie_header(
        self, channel: str, account_name: str = "", *,
        force_refresh: bool = False, for_host: str = "", group: str = "",
        slot_token: str = "",
    ) -> Tuple[Dict[str, Any], str]:
        """返回 (账号记录, Cookie 请求头字符串)。

        这是采集器唯一需要调用的方法——不关心账号从哪来、Cookie 怎么拿到的。

        `for_host` 给了就只返回该站点收得到的 Cookie。**微博必须给**：
        `.weibo.com` 和 `.weibo.cn` 各有一个叫 SUB 的 Cookie，值完全不同，
        混在一起发出去平台会判未登录。
        `group` 给了就只在该账号分组里挑账号（多账号轮换按组隔离）。
        """
        if not needs_login(channel):
            return {}, ""

        account = await self.accounts.pick_active(
            channel, preferred=account_name, group=group
        )
        if not account:
            raise LoginRequired(
                f"平台 {channel} 没有可用账号，请先在账号管理里添加并登录一个账号"
            )

        max_age = int(self.config.get("browser.session_check_interval_seconds", 600))
        cookie_time = account.get("cookie_updated_at")
        fresh = (
            not force_refresh
            and account.get("status") == "active"
            and cookie_time is not None
            and datetime.now() - cookie_time < timedelta(seconds=max_age)
            and AccountRepository.cookies_of(account)
        )

        if fresh:
            # 最快路径：完全不启动浏览器
            return account, AccountRepository.cookie_header(account, host=for_host)

        cookies = await self.silent_refresh(
            channel, account["account_name"], slot_token=slot_token
        )
        account = await self.accounts.get(channel, account["account_name"]) or account
        header = (
            cookie_import.header_for_host(cookies, for_host) if for_host
            else cookie_import.to_header(cookies)
        )
        return account, header

    # ---------------- 手动导入 Cookie ----------------
    async def import_cookies(
        self, channel: str, account_name: str, raw: str
    ) -> Dict[str, Any]:
        """把用户粘贴的 Cookie 存进账号，并**写进该账号的浏览器 profile**。

        写进 profile 这一步不能省：抖音和快手的签名依赖页面环境
        （msToken 在 localStorage、__NS_hxfalcon 要调页面里的函数），
        采集时会用这个 profile 开一个真实页面。
        只把 Cookie 存进数据库的话，页面还是未登录状态，
        抖音照样回「请先登录再继续搜索吧」。
        """
        spec = get_spec(channel) if needs_login(channel) else None
        fallback = cookie_import.default_domain(spec.home_url) if spec else ""

        cookies, source_format = cookie_import.parse(raw, fallback_domain=fallback)
        # 补过期时间：没有 expires 的 Cookie 是会话 Cookie，
        # Chromium 不会把它们写进 profile，导入就白做了
        cookies = cookie_import.ensure_persistent(cookies)
        header = cookie_import.to_header(cookies)

        missing = missing_login_cookies(channel, header)
        if missing:
            # 把**实际收到的 key 名**一起报出来（只有名字，没有值）。
            # 平台改 Cookie 名是常事——快手 2026-08 就从 web_st 换成了
            # webday7_st，只说"需要 web_st"的话，用户对着一屏 Cookie
            # 根本不知道差在哪，只能来回问。列出来一眼就看得出。
            found = cookie_names(header)
            raise ValueError(
                f"这份 Cookie 里没有登录凭据（需要其中之一：{'、'.join(missing)}）。"
                f"这份 Cookie 实际带的 key 有 {len(found)} 个："
                f"{'、'.join(found[:40])}{' …' if len(found) > 40 else ''}。"
                f"如果你确认浏览器里是登录状态，多半是平台改了 Cookie 名字，"
                f"把上面这行发给开发者即可。"
                f"另外请确认导出的是 {spec.home_url if spec else ''} 这个站点的 Cookie。"
            )

        directory = self.profile_dir(channel, account_name)
        injected = 0
        inject_error = ""
        async with self._lock_for(channel, account_name):
            try:
                injected = await SUBPROCESS_LOOP.run(
                    self._inject_cookies(cookies, directory)
                )
            except Exception as exc:  # noqa: BLE001
                # 写 profile 失败不该让整个导入失败：数据库里的 Cookie 对
                # 小红书、微博这类不需要页面环境的平台已经够用了
                inject_error = str(exc)
                logger.warning("[%s/%s] 写入浏览器 profile 失败：%s",
                               channel, account_name, exc)

        await self.accounts.save_session(
            channel, account_name, cookies=cookies, profile_dir=str(directory),
        )
        logger.info(
            "[%s/%s] 手动导入 %d 个 Cookie（%s 格式），写入 profile %d 个",
            channel, account_name, len(cookies), source_format, injected,
        )

        result = cookie_import.summarize(cookies)
        result.update({
            "format": source_format,
            "injected_to_profile": injected,
            "profile_dir": str(directory),
        })
        if inject_error:
            result["profile_warning"] = (
                f"Cookie 已保存，但写入浏览器 profile 失败：{inject_error}。"
                f"小红书/微博不受影响；抖音、快手需要页面登录态，建议重试一次。"
            )
        return result

    async def _inject_cookies(self, cookies: List[Dict], directory: Path) -> int:
        """跑在子进程循环上：把 Cookie 灌进持久化 profile 再关掉，让它落盘。"""
        await BROWSER_CAPACITY.acquire("cookie-inject")
        try:
            return await self._inject_cookies_inner(cookies, directory)
        finally:
            BROWSER_CAPACITY.release("cookie-inject")

    async def _inject_cookies_inner(self, cookies: List[Dict], directory: Path) -> int:
        self.release_profile_locks(directory)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                # 只是写一下 Cookie 就关，没必要弹窗，这里固定无头
                str(directory), **self._launch_kwargs(headless=True)
            )
            try:
                await context.add_cookies(cookies)
                stored = await context.cookies()
                return len(stored)
            finally:
                # 必须正常 close，Chromium 才会把 Cookie 刷到 profile 的 SQLite 里
                await context.close()

    # ---------------- 账号维护 ----------------
    async def check_account(self, channel: str, account_name: str) -> Dict[str, Any]:
        """账号管理页的"检测"按钮：跑一次静默刷新并返回结果。"""
        try:
            cookies = await self.silent_refresh(channel, account_name)
            return {"ok": True, "cookie_count": len(cookies), "message": "登录态正常"}
        except LoginRequired as exc:
            return {"ok": False, "cookie_count": 0, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            await self.accounts.mark_expired(channel, account_name, str(exc))
            return {"ok": False, "cookie_count": 0, "message": f"检测失败：{exc}"}

    async def check_all(
        self, channel: str = "", *, skip_busy: bool = False
    ) -> List[Dict[str, Any]]:
        """批量体检，调度器可以定期跑，提前发现失效账号。

        ⚠️ `skip_busy=True` 是给**定时任务**用的，非常重要：
        正在跑的采集任务持有那个账号的 profile，这时候再对同一个
        user-data-dir 开一个 Chromium，两个实例会互相抢，
        采集那边的页面直接卡住不动 —— 日志里安静得像什么都没发生，
        用户看到的就是"抖音采到一半就不采了"。
        定时体检本来就是"顺手看一眼"，撞上了跳过就行，下一轮再看。
        """
        listing = await self.accounts.list(channel=channel, page_size=200)
        results = []
        for account in listing["items"]:
            if not account["enabled"] or account["status"] == "disabled":
                continue
            busy = self.profile_busy_reason(account["channel"], account["account_name"])
            if skip_busy and busy:
                logger.info(
                    "[%s/%s] profile 正被占用（%s），本轮定时体检跳过",
                    account["channel"], account["account_name"], busy,
                )
                results.append({
                    "channel": account["channel"],
                    "account_name": account["account_name"],
                    "ok": True, "skipped": True, "cookie_count": 0,
                    "message": f"跳过（{busy}）",
                })
                continue
            result = await self.check_account(account["channel"], account["account_name"])
            results.append({
                "channel": account["channel"],
                "account_name": account["account_name"],
                **result,
            })
        return results

    async def clear_profile(self, channel: str, account_name: str) -> None:
        """退出登录：删掉 profile 目录和 DB 里的 Cookie。"""
        import shutil
        directory = self.profile_dir(channel, account_name)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        await self.accounts.update(channel, account_name, {
            "cookies": None, "cookie_updated_at": None,
            "status": "never_login", "last_error": None,
        })
