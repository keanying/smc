"""采集期间保持一个「活的浏览器页面」。

为什么需要：
  抖音的 msToken 来自页面 localStorage 的 xmst；
  快手的 __NS_hxfalcon 签名只能调用页面里已加载的签名环境（window.__ks_realm.$encode）。
  这两个平台光有 Cookie 串是采不动的，必须在采集全程持有一个页面。

小红书用纯 Python 算法（xhshow），微博不需要签名，所以这两个平台不走这里，
只用 BrowserSessionManager.get_cookie_header() 拿 Cookie 即可——省掉一个浏览器进程。

事件循环：
  浏览器上下文和页面全部创建并运行在 SUBPROCESS_LOOP 上（见 loop.py），
  所以本类**不对外暴露 page 对象**——外部拿到 page 直接 await 会跨循环。
  需要什么能力就在这里加一个方法，内部用 SUBPROCESS_LOOP.run 提交。

用法：
    async with PageSession(browser_manager, "douyin", account_name) as session:
        await session.local_storage("xmst")
        await session.evaluate("() => navigator.userAgent")
        session.cookie_header
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from ..collectors.base import LoginRequired
from ..core.logging import get_logger
from ..repositories.account_repo import AccountRepository
from ..core.subprocess_loop import SUBPROCESS_LOOP
from .capacity import BROWSER_CAPACITY
from . import cookie_import
from .specs import LoginSpec, get_spec, missing_login_cookies, needs_login

logger = get_logger(__name__)

NAV_TIMEOUT_MS = 60_000


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


class PageSession:
    def __init__(self, manager, channel: str, account_name: str = "",
                 *, init_scripts: Optional[list[str]] = None, group: str = "",
                 slot_token: str = ""):
        self.manager = manager
        self.channel = channel
        self.account_name = account_name
        self.group = group
        self.init_scripts = init_scripts or []
        #: 调度层给这条任务发的浏览器租约标识。
        #: 拿着它说明"这个账号本来就是我占着的"——不该被自己的占用挡住，
        #: 也不该在 close() 时把租约还掉（租约由调度层在 finally 里统一释放）。
        self.slot_token = slot_token

        self.spec: Optional[LoginSpec] = get_spec(channel) if needs_login(channel) else None
        self.cookie_header: str = ""
        self.account: Dict[str, Any] = {}

        # 以下三个只在浏览器循环里访问
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        #: 是否登记过 profile 占用（close 时要还回去）
        self._holds_profile = False
        #: 这次占用是不是自己登记的。False = 调度层的租约占着，close 时不还
        self._owns_slot = True

    # ---------------- 生命周期 ----------------
    async def __aenter__(self) -> "PageSession":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def start(self) -> None:
        # 1) 先在服务器循环上确认账号可用，避免白开一个浏览器
        if self.spec is not None:
            self.account = await self.manager.accounts.pick_active(
                self.channel, preferred=self.account_name, group=self.group
            ) or {}
            if not self.account:
                raise LoginRequired(
                    f"平台 {self.channel} 没有可用账号，请先在账号管理里添加并登录一个账号"
                )
            self.account_name = self.account["account_name"]

        directory = self.manager.profile_dir(self.channel, self.account_name or "default")

        # 登录窗口正开着的话，profile 目录被那个 Chromium 独占。
        # 再开一个通常拿不到里面的登录态（甚至会退化成一个全新的空 profile），
        # 结果就是"明明登录了，采集却说没登录"。宁可直接说清楚。
        # 调度层已经替这条任务占住这个账号了（拟人模式/快手），那就直接用，
        # 别被自己的占用拦下来，也别重复登记。
        self._owns_slot = not self.manager.slots.held_by(
            self.channel, self.account_name, self.slot_token
        )
        if self._owns_slot:
            self.manager.raise_if_profile_busy(self.channel, self.account_name)
        # 反过来也要登记：采集期间这个 profile 归我。
        # ⚠️ 不登记的话，定时「采集 Cookie」任务会在采集跑到一半时
        # 对同一个 profile 再开一个 Chromium —— 两个实例抢同一个
        # user-data-dir，采集这边的页面会卡住不动（用户看到的是
        # "抖音采到一半就不动了"，日志里安静得像什么都没发生）。
        if self._owns_slot:
            self.manager.mark_profile_busy(
                self.channel, self.account_name, "采集任务正在使用"
            )
            self._holds_profile = True

        # 2) 过浏览器闸门——**开 Chromium 之前**，不是之后。
        #    一个 Chromium 实测 ~700MB / 10 个进程，8 条任务同时开就是 6GB，
        #    机器换页之后所有任务一起完蛋。名额在 close() 里还。
        await BROWSER_CAPACITY.acquire(f"{self.channel}/{self.account_name}")
        self._holds_capacity = True

        # 3) 浏览器部分全部在浏览器循环里完成。
        #    把库里存的 Cookie 一起带进去当种子：手动导入的 Cookie 就是这样
        #    进到页面环境的，profile 自己丢了登录态时也能靠它救回来。
        seed = AccountRepository.cookies_of(self.account) if self.account else []
        logged_in, cookies = await SUBPROCESS_LOOP.run(
            self._open_browser(directory, seed)
        )

        # 4) 回到服务器循环做数据库相关的事
        if self.spec is not None and not logged_in:
            await self.manager.accounts.mark_expired(
                self.channel, self.account_name, "采集开始前检查未检测到登录态"
            )
            await self.close()
            raise LoginRequired(
                f"账号 [{self.channel}/{self.account_name}] 登录态已失效，"
                f"请在账号管理里重新登录"
            )

        await self._store_cookies(cookies, directory)
        logger.info(
            "[%s/%s] 采集页面已就绪，%d 个 Cookie",
            self.channel, self.account_name, len(cookies),
        )

    async def _open_browser(
        self, directory: Path, seed_cookies: Optional[List[Dict]] = None
    ) -> tuple[bool, List[Dict]]:
        """跑在浏览器循环上：开 context、种 Cookie、注入脚本、导航、判定登录态、取 Cookie。"""
        self._playwright = await async_playwright().start()
        # 清掉上次非正常退出留下的单例锁（见 BrowserSessionManager.release_profile_locks）
        self.manager.release_profile_locks(directory)
        # 有头/无头跟系统设置走：调试风控问题时开成有头能直接看到平台弹了什么
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(directory), **self.manager._launch_kwargs()
        )

        # 先把库里的 Cookie 种进去，再导航——顺序反了页面就是用旧状态加载的。
        # 手动导入的 Cookie 靠这一步进入页面环境：抖音的 msToken 和快手的签名
        # 都要在**已登录的页面**里才拿得到，光有 Cookie 串不够。
        if seed_cookies:
            try:
                await self._context.add_cookies(_playwright_cookies(seed_cookies))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s/%s] 注入已保存的 Cookie 失败：%s",
                    self.channel, self.account_name, exc,
                )

        # stealth 与平台自定义脚本必须在导航前注入，否则页面已经跑完初始化了
        stealth = self.manager.config.get("browser.stealth_js")
        if stealth and Path(stealth).exists():
            try:
                await self._context.add_init_script(path=stealth)
            except Exception as exc:  # noqa: BLE001
                logger.debug("注入 stealth.js 失败（可忽略）：%s", exc)
        for script in self.init_scripts:
            await self._context.add_init_script(script=script)

        self._page = (
            self._context.pages[0] if self._context.pages
            else await self._context.new_page()
        )

        logged_in = True
        if self.spec is not None and self.spec.home_url:
            await self._page.goto(
                self.spec.home_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            await self._page.wait_for_timeout(2500)
            logged_in = await self.manager._is_logged_in(self._context, self.spec)

        cookies = await self._context.cookies(
            self.spec.cookie_scope if self.spec is not None else None
        )
        return logged_in, cookies

    async def close(self) -> None:
        try:
            await SUBPROCESS_LOOP.run(self._close_browser())
        finally:
            # 无论关得干不干净都要还回去，否则这个账号从此跑不了任务
            if getattr(self, "_holds_profile", False):
                self.manager.release_profile(self.channel, self.account_name)
                self._holds_profile = False
            # 闸门名额同理：漏还一次就永久少一个名额，跑一天之后
            # 所有浏览器任务都会卡在排队上，而且完全看不出原因
            if getattr(self, "_holds_capacity", False):
                BROWSER_CAPACITY.release(f"{self.channel}/{self.account_name}")
                self._holds_capacity = False

    async def _close_browser(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._context = None
            self._page = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None

    # ---------------- Cookie ----------------
    async def _store_cookies(self, cookies: List[Dict], directory: Path) -> None:
        """把 Cookie 拼成请求头并写回账号表（数据库操作，留在服务器循环）。"""
        # 走公共实现：同名 Cookie 要去重，见 cookie_import.to_header
        self.cookie_header = cookie_import.to_header(cookies)
        if self.spec is not None and self.account_name:
            await self.manager.accounts.save_session(
                self.channel, self.account_name,
                cookies=cookies, profile_dir=str(directory),
            )

    async def refresh_cookies(self) -> str:
        """重新从页面同步 Cookie。长任务中途登录态被刷新时用得上。"""
        cookies = await SUBPROCESS_LOOP.run(self._read_cookies())
        directory = self.manager.profile_dir(self.channel, self.account_name or "default")
        await self._store_cookies(cookies, directory)
        return self.cookie_header

    async def _read_cookies(self) -> List[Dict]:
        if self._context is None:
            return []
        return await self._context.cookies(
            self.spec.cookie_scope if self.spec is not None else None
        )

    def ensure_logged_in(self) -> None:
        """采集器 prepare 完之后调一次：Cookie 里没有登录凭据就直接抛，别去撞接口。"""
        missing = missing_login_cookies(self.channel, self.cookie_header)
        if not missing:
            return
        count = len([p for p in self.cookie_header.split(";") if p.strip()])
        raise LoginRequired(
            f"账号 [{self.channel}/{self.account_name}] 看起来没有真正登录："
            f"浏览器 profile 里只有 {count} 个 Cookie，且不含任何登录凭据"
            f"（需要其中之一：{'、'.join(missing)}）。"
            f"请在账号管理里点「打开浏览器」重新登录，"
            f"或用「导入 Cookie」把浏览器里导出的 Cookie 贴进来。"
        )

    def cookie_value(self, name: str) -> str:
        for pair in self.cookie_header.split("; "):
            key, _, value = pair.partition("=")
            if key == name:
                return value
        return ""

    # ---------------- 页面能力（都经浏览器循环转发） ----------------
    async def local_storage(self, key: str) -> Optional[str]:
        """读页面 localStorage。抖音的 msToken 就存在这里（键名 xmst）。"""
        return await SUBPROCESS_LOOP.run(self._local_storage(key))

    async def _local_storage(self, key: str) -> Optional[str]:
        if self._page is None:
            return None
        try:
            return await self._page.evaluate(
                "k => { try { return localStorage.getItem(k); } catch (e) { return null; } }",
                key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 localStorage[%s] 失败：%s", key, exc)
            return None

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        return await SUBPROCESS_LOOP.run(self._evaluate(expression, arg))

    async def _evaluate(self, expression: str, arg: Any) -> Any:
        if self._page is None:
            raise RuntimeError("页面尚未就绪")
        return await self._page.evaluate(expression, arg)

    async def user_agent(self) -> str:
        return await SUBPROCESS_LOOP.run(self._user_agent())

    async def _user_agent(self) -> str:
        if self._page is None:
            return ""
        return await self._page.evaluate("() => navigator.userAgent")

    async def wait_for_function(self, expression: str, timeout_ms: int = 15_000) -> None:
        """等页面里某个条件成立，比如快手的签名环境挂载完成。"""
        await SUBPROCESS_LOOP.run(self._wait_for_function(expression, timeout_ms))

    async def _wait_for_function(self, expression: str, timeout_ms: int) -> None:
        if self._page is None:
            raise RuntimeError("页面尚未就绪")
        await self._page.wait_for_function(expression, timeout=timeout_ms)

    async def reload(self) -> None:
        await SUBPROCESS_LOOP.run(self._reload())

    async def _reload(self) -> None:
        if self._page is None:
            raise RuntimeError("页面尚未就绪")
        await self._page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    async def goto(self, url: str) -> None:
        await SUBPROCESS_LOOP.run(self._goto(url))

    async def _goto(self, url: str) -> None:
        if self._page is None:
            raise RuntimeError("页面尚未就绪")
        await self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
