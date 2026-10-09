"""实时流交互浏览器：把服务端的浏览器画面推到网页上，并把网页上的鼠标键盘操作打回去。

用途：账号登录。服务部署在服务器上，用户看不到服务端弹出的浏览器窗口，
所以用 CDP 的 Page.startScreencast 把画面以 JPEG 帧推给前端，
前端的点击/输入/滚动通过 WebSocket 回传，用 page.mouse / page.keyboard 重放。

相比"截图轮询"，screencast 只在画面变化时推帧，扫码登录这种场景带宽和延迟都好很多。

事件循环划分（三条线，别搞混）：
    浏览器循环    浏览器、页面、CDP 会话，以及推流帧的产生
    服务器循环    WebSocket 收发、数据库读写、登录态轮询的节拍
    跨循环投递    帧和状态经 SUBPROCESS_LOOP.to_caller_loop 包装后送回服务器循环

生命周期：
    session = LiveBrowserSession(manager, "douyin", "账号A")
    await session.start(on_frame=..., on_status=...)   # 打开浏览器并开始推流
    await session.handle_input({...})                  # 前端事件回放
    await session.stop()                               # 保存 Cookie 并关闭
会话在内存里用 LIVE_SESSIONS 注册，WebSocket 端点按 session_id 找到它。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from ..core.logging import get_logger
from ..core.subprocess_loop import SUBPROCESS_LOOP
from .capacity import BROWSER_CAPACITY
from . import cookie_import
from .specs import (
    LoginSpec, cookie_names, get_spec, missing_login_cookies,
)

logger = get_logger(__name__)

FrameCallback = Callable[[str, int, int], Awaitable[None]]
StatusCallback = Callable[[str, str], Awaitable[None]]

# 推流参数：质量 60、宽度上限 1280，够看清二维码又不至于太吃带宽
SCREENCAST_OPTIONS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1280,
    "maxHeight": 800,
    "everyNthFrame": 1,
}

POLL_INTERVAL_SECONDS = 2


class LiveBrowserSession:
    def __init__(self, manager, channel: str, account_name: str):
        self.session_id = uuid.uuid4().hex
        self.manager = manager
        self.channel = channel
        self.account_name = account_name
        self.spec: LoginSpec = get_spec(channel)

        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cdp = None
        self._on_frame: Optional[FrameCallback] = None
        self._on_status: Optional[StatusCallback] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._closed = False
        self.logged_in = False
        #: 打开窗口的那一刻就已经是登录状态（profile 里的登录态还在）
        self._opened_logged_in = False
        #: 登录弹窗（微博 PC 端的登录是 window.open 出来的新窗口）
        self._popup = None
        #: 推流和输入当前作用在哪个页面上——有弹窗时是弹窗
        self._active_page = None
        #: 平台登录态自检的节流状态，见 BrowserSessionManager._is_logged_in
        self._verify_state: Dict[str, Any] = {}
        self.started_at = time.time()

    # ---------------- 启动 ----------------
    async def start(self, on_frame: FrameCallback, on_status: StatusCallback) -> None:
        # 帧和状态是在浏览器循环里产生的，但 WebSocket 属于服务器循环，
        # 这里先把回调包成可以跨循环投递的形式
        self._on_frame = SUBPROCESS_LOOP.to_caller_loop(on_frame)
        self._on_status = SUBPROCESS_LOOP.to_caller_loop(on_status)

        await on_status("starting", "正在启动浏览器…")
        directory = self.manager.profile_dir(self.channel, self.account_name)

        await on_status("navigating", f"正在打开 {self.spec.login_entry}")
        # 登记占用：这个窗口开着的时候，采集任务不能再去开同一个 profile
        self.manager.mark_profile_busy(self.channel, self.account_name, "登录窗口已打开")
        # 登录窗口是**有头**的，比采集用的无头还重一点，同样要过闸门。
        # 不过的话，采集把内存占满时再点一次登录，整机就被推过临界点了。
        await BROWSER_CAPACITY.acquire(f"login/{self.channel}/{self.account_name}")
        self._holds_capacity = True
        try:
            await SUBPROCESS_LOOP.run(self._open_browser(directory))
        except Exception:
            # 没开起来就别一直占着，否则这个账号从此跑不了任务
            self.manager.release_profile(self.channel, self.account_name)
            BROWSER_CAPACITY.release(f"login/{self.channel}/{self.account_name}")
            self._holds_capacity = False
            raise

        # 轮询的节拍放在服务器循环上，因为登录成功后要写数据库
        self._watch_task = asyncio.create_task(self._watch_login())
        # 打开时就已经登录着的话直接说清楚，别让用户以为还得再扫一次——
        # 这正是"每次打开都要登录"那个错觉的来源
        if self._opened_logged_in:
            await on_status(
                "streaming", "这个账号还登录着，不用重新扫码。要换号就先退出再登录"
            )
        else:
            await on_status(
                "streaming", "扫码或输入账号密码完成登录，然后点「我已登录，保存」"
            )

    async def _open_browser(self, directory: Path) -> None:
        """跑在浏览器循环上：开有头浏览器、导航、启动推流。"""
        self._playwright = await async_playwright().start()
        if self.manager._use_obscura():
            await self._open_browser_obscura(directory)
        else:
            await self._open_browser_playwright(directory)

    async def _open_browser_playwright(self, directory: Path) -> None:
        """用 Playwright + Chromium 打开登录浏览器。"""
        # 交互式登录必须用有头模式：无头会被大部分平台的风控直接拦掉，
        # 而且二维码常常渲染不出来。有头窗口跑在服务端，用户通过推流看到画面。
        # 登录窗口默认有头（风控 + 二维码），但服务跑在自己机器上时窗口会打扰人，
        # 所以给了 browser.headless_login 这个开关。画面本来就通过推流看得到。
        headless = self.manager.headless_for_login()
        # 清掉上次非正常退出留下的单例锁，否则 Chromium 会另开一个空 profile，
        # 表现就是"登录过的账号又变回没登录"（见 release_profile_locks）
        self.manager.release_profile_locks(directory)
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(directory), **self.manager._launch_kwargs(headless=headless)
        )
        logger.info(
            "[%s/%s] 登录浏览器已启动（%s）",
            self.channel, self.account_name, "无头" if headless else "有头窗口",
        )
        # stealth 与平台自定义脚本必须在导航前注入，否则页面已经跑完初始化了
        stealth = self.manager.config.get("browser.stealth_js")
        if stealth and Path(stealth).exists():
            try:
                await self._context.add_init_script(path=stealth)
            except Exception as exc:  # noqa: BLE001
                logger.debug("注入 stealth.js 失败（可忽略）：%s", exc)
        self._page = (
            self._context.pages[0] if self._context.pages
            else await self._context.new_page()
        )
        # ⚠️ 先开**首页**，不是登录页。
        # 微博的 weibo.com/login.php 无论登没登录都渲染登录界面——
        # 直接开它，用户每次点「打开浏览器」看到的都是"请登录"，
        # 就会以为登录态没保住（其实 profile 里好好的）。
        # 开首页的话，还登录着就直接看到时间线，一眼就知道不用再扫码了。
        await self._page.goto(
            self.spec.home_url, wait_until="domcontentloaded", timeout=60_000
        )
        await self._page.wait_for_timeout(1500)

        already = False
        try:
            already = await self.manager._is_logged_in(self._context, self.spec)
        except Exception:  # noqa: BLE001
            pass
        if not already and self.spec.login_url and self.spec.login_url != self.spec.home_url:
            try:
                await self._page.goto(
                    self.spec.login_url, wait_until="domcontentloaded", timeout=60_000
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("跳转登录页失败（%s），留在首页让用户自己点登录", exc)
        self._opened_logged_in = already

        # 登录弹窗要跟上，见 _on_popup
        self._context.on("page", self._sync_on_popup)
        await self._start_screencast()

    async def _open_browser_obscura(self, directory: Path) -> None:
        """用 Obscura 打开登录浏览器。

        Obscura 是无头浏览器，但画面通过 CDP screencast 推流到前端，
        用户看到的和本地浏览器一样。stealth 模式内置反检测。
        """
        ws_url = await self.manager._ensure_obscura(self.channel, self.account_name)
        browser = await self._playwright.chromium.connect_over_cdp(ws_url)
        self._context = browser.contexts[0] if browser.contexts else await browser.new_context()
        logger.info(
            "[%s/%s] 登录浏览器已启动（Obscura stealth=%s）",
            self.channel, self.account_name, self.manager._obscura_stealth(),
        )
        self._page = (
            self._context.pages[0] if self._context.pages
            else await self._context.new_page()
        )
        # ⚠️ 先开**首页**，不是登录页。
        # 微博的 weibo.com/login.php 无论登没登录都渲染登录界面——
        # 直接开它，用户每次点「打开浏览器」看到的都是"请登录"，
        # 就会以为登录态没保住（其实 profile 里好好的）。
        # 开首页的话，还登录着就直接看到时间线，一眼就知道不用再扫码了。
        await self._page.goto(
            self.spec.home_url, wait_until="domcontentloaded", timeout=60_000
        )
        await self._page.wait_for_timeout(1500)

        already = False
        try:
            already = await self.manager._is_logged_in(self._context, self.spec)
        except Exception:  # noqa: BLE001
            pass
        if not already and self.spec.login_url and self.spec.login_url != self.spec.home_url:
            try:
                await self._page.goto(
                    self.spec.login_url, wait_until="domcontentloaded", timeout=60_000
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("跳转登录页失败（%s），留在首页让用户自己点登录", exc)
        self._opened_logged_in = already

        # 登录弹窗要跟上，见 _on_popup
        self._context.on("page", self._sync_on_popup)
        await self._start_screencast()

    # ---------------- 登录弹窗 ----------------
    def _sync_on_popup(self, page) -> None:
        """新窗口回调跑在浏览器循环上，转成任务处理。"""
        asyncio.create_task(self._on_popup(page))

    async def _on_popup(self, page) -> None:
        """把推流和输入切到登录弹窗上。

        ⚠️ 微博 PC 端的登录是**弹窗**：
        `passport.weibo.com/sso/signin?...&disp=popup` 会 window.open 一个新窗口。
        推流绑在主页面上的话，用户在界面里看到的是 weibo.com 首页，
        二维码在另一个看不见的窗口里——扫都没法扫。
        """
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:  # noqa: BLE001
            pass
        logger.info("[%s/%s] 登录弹窗已打开：%s",
                    self.channel, self.account_name, (page.url or "")[:120])
        self._popup = page
        page.on("close", self._sync_on_popup_closed)
        await self._retarget_screencast(page)
        if self._on_status:
            await self._on_status("popup", "登录弹窗已打开，请在画面里完成登录")

    def _sync_on_popup_closed(self, page) -> None:
        asyncio.create_task(self._on_popup_closed())

    async def _on_popup_closed(self) -> None:
        """弹窗关了：切回主页面并刷新一次，让它显示登录后的状态。"""
        self._popup = None
        if self._page is None or self._closed:
            return
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=30_000)
        except Exception:  # noqa: BLE001
            pass
        await self._retarget_screencast(self._page)
        if self._on_status:
            await self._on_status("popup_closed", "登录弹窗已关闭")

    async def _retarget_screencast(self, page) -> None:
        """把 CDP 推流切到另一个页面上。"""
        try:
            if self._cdp is not None:
                try:
                    await self._cdp.send("Page.stopScreencast")
                except Exception:  # noqa: BLE001
                    pass
            self._active_page = page
            await self._start_screencast()
        except Exception as exc:  # noqa: BLE001
            logger.warning("切换推流目标失败：%s", exc)

    async def _start_screencast(self) -> None:
        target = self._active_page or self._page
        self._cdp = await self._context.new_cdp_session(target)

        async def _on_frame_event(params: Dict[str, Any]) -> None:
            if self._closed:
                return
            try:
                await self._cdp.send(
                    "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
                )
            except Exception:  # noqa: BLE001 - 页面导航时 ack 可能失败，忽略即可
                pass
            metadata = params.get("metadata") or {}
            if self._on_frame:
                await self._on_frame(
                    params["data"],
                    int(metadata.get("deviceWidth") or 1280),
                    int(metadata.get("deviceHeight") or 800),
                )

        def _sync_handler(params: Dict[str, Any]) -> None:
            # 这个回调在浏览器循环里被触发，create_task 也落在浏览器循环上
            asyncio.create_task(_on_frame_event(params))

        self._cdp.on("Page.screencastFrame", _sync_handler)
        await self._cdp.send("Page.startScreencast", SCREENCAST_OPTIONS)

    # ---------------- 输入回放 ----------------
    async def handle_input(self, event: Dict[str, Any]) -> None:
        """把前端回传的操作重放到服务端页面上。

        坐标以帧的像素坐标为准，前端负责按显示尺寸换算好再发过来。
        """
        if self._closed:
            return
        await SUBPROCESS_LOOP.run(self._handle_input(event))

    async def _handle_input(self, event: Dict[str, Any]) -> None:
        # 输入要打到**当前推流的那个页面**上。有登录弹窗时就是弹窗——
        # 否则用户在画面里点二维码旁边的"账号登录"，点击落在了后面的主页面上，
        # 看起来就是"点了没反应"。
        page = self._active_page or self._page
        if page is None:
            return
        kind = event.get("type")
        try:
            if kind == "click":
                await page.mouse.click(
                    float(event["x"]), float(event["y"]),
                    button=event.get("button", "left"),
                    click_count=int(event.get("clickCount", 1)),
                )
            elif kind == "mousemove":
                await page.mouse.move(float(event["x"]), float(event["y"]))
            elif kind == "mousedown":
                await page.mouse.move(float(event["x"]), float(event["y"]))
                await page.mouse.down(button=event.get("button", "left"))
            elif kind == "mouseup":
                await page.mouse.up(button=event.get("button", "left"))
            elif kind == "wheel":
                await page.mouse.wheel(
                    float(event.get("deltaX", 0)), float(event.get("deltaY", 0))
                )
            elif kind == "key":
                await page.keyboard.press(event["key"])
            elif kind == "type":
                await page.keyboard.type(event.get("text", ""), delay=30)
            elif kind == "goto":
                await page.goto(event["url"], wait_until="domcontentloaded")
            elif kind == "reload":
                await page.reload(wait_until="domcontentloaded")
            elif kind == "back":
                await page.go_back()
        except Exception as exc:  # noqa: BLE001
            logger.warning("回放输入事件失败（%s）：%s", kind, exc)

    # ---------------- 登录态轮询 ----------------
    async def _check_logged_in(self) -> bool:
        """跑在浏览器循环上的一次性判定。"""
        if self._context is None:
            return False
        return await self.manager._is_logged_in(
            self._context, self.spec, verify_state=self._verify_state
        )

    async def _read_cookies(self) -> List[Dict]:
        if self._context is None:
            return []
        return await self._context.cookies(self.spec.cookie_scope)

    async def _settle_login(self) -> None:
        """登录成功后先跳一次采集域，让站点把 Cookie 种到那边。

        微博是典型：在 passport.weibo.com 登录，Cookie 落在 .weibo.com；
        采集域和登录域可能属于**不同的注册域**（微博曾经就是这样）。
        不跳这一下，采集域一个登录 Cookie 都拿不到，
        表现就是"刚登录完，任务又说登录态失效"。
        """
        if self._page is None or not self.spec.settle_url:
            return
        # 已经在采集域上就不用跳了——goto 到同一个地址会整页重载，
        # 白白多花几秒，还会把用户刚在页面上做的操作冲掉
        current = self._page.url or ""
        if _same_origin(current, self.spec.settle_url):
            return
        try:
            await self._page.goto(
                self.spec.settle_url, wait_until="domcontentloaded", timeout=30_000
            )
            await self._page.wait_for_timeout(2500)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[%s/%s] 登录后跳转 %s 失败：%s",
                self.channel, self.account_name, self.spec.settle_url, exc,
            )

    async def _watch_login(self) -> None:
        """跑在服务器循环上：定期问一次浏览器循环，成功后落库。"""
        timeout = int(self.manager.config.get("browser.login_timeout_seconds", 300))
        deadline = time.time() + timeout
        while not self._closed and time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                if await SUBPROCESS_LOOP.run(self._check_logged_in()):
                    # 先保存再置标志：logged_in 的含义是"登录态已经落库了"。
                    # 反过来的话，外面看到 logged_in=True 时保存可能还在路上——
                    # 加了登录后跳转采集域这一步之后，这个窗口被拉长到秒级，
                    # 于是出现"显示已登录、但库里还没有 Cookie"。
                    await self._save_session()
                    self.logged_in = True
                    if self._on_status:
                        await self._on_status(
                            "logged_in", "登录成功，登录态已保存，可以关闭窗口"
                        )
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("轮询登录态出错：%s", exc)
        if not self._closed and not self.logged_in and self._on_status:
            await self._on_status("timeout", f"{timeout} 秒内未完成登录，已放弃")

    async def save_now(self) -> Dict[str, Any]:
        """**用户说"我登录好了"** —— 直接把当前浏览器里的 Cookie 存下来。

        ⚠️ 为什么要有这么个手动入口：自动判定再准也有失手的时候
        （平台改了接口、网络抖了一下、弹窗流程和预期不一样），
        而"我到底登没登上"这件事，**人看一眼画面就知道**，比任何探测都可靠。
        没有这个入口的话，一旦判定失手，用户明明登录成功了也只能干瞪眼，
        下次打开还得重扫——这正是用户反馈的情形。

        唯一的把关是：Cookie 里得真有登录凭据。否则存进去的是游客态，
        采集时才发现，那种错更难查。
        """
        cookies = await SUBPROCESS_LOOP.run(self._read_cookies())
        header = cookie_import.to_header(cookies)
        missing = missing_login_cookies(self.channel, header)
        if missing:
            names = cookie_names(header)
            return {
                "saved": False,
                "message": (
                    f"这个浏览器里还没有登录凭据（需要其中之一：{'、'.join(missing)}）。"
                    f"当前有 {len(names)} 个 Cookie：{'、'.join(names[:30])}"
                    f"{' …' if len(names) > 30 else ''}。"
                    f"请确认画面里确实已经登录成功再点保存。"
                ),
            }
        await self._persist(cookies)
        self.logged_in = True
        if self._on_status:
            await self._on_status("logged_in", "登录态已保存")
        return {"saved": True, "cookie_count": len(cookies),
                "message": f"已保存 {len(cookies)} 个 Cookie"}

    async def _persist(self, cookies: List[Dict]) -> None:
        await self.manager.accounts.save_session(
            self.channel, self.account_name,
            cookies=cookies,
            profile_dir=str(self.manager.profile_dir(self.channel, self.account_name)),
        )
        logger.info(
            "[%s/%s] 登录态已保存，%d 个 Cookie",
            self.channel, self.account_name, len(cookies),
        )

    async def _save_session(self) -> None:
        """数据库写入，必须在服务器循环上跑。"""
        # 先把登录态"落地"到采集域，再取 Cookie —— 顺序反了拿到的还是旧的
        await SUBPROCESS_LOOP.run(self._settle_login())
        cookies = await SUBPROCESS_LOOP.run(self._read_cookies())
        await self.manager.accounts.save_session(
            self.channel, self.account_name,
            cookies=cookies,
            profile_dir=str(self.manager.profile_dir(self.channel, self.account_name)),
        )
        logger.info(
            "[%s/%s] 交互式登录成功，保存 %d 个 Cookie",
            self.channel, self.account_name, len(cookies),
        )

    # ---------------- 收尾 ----------------
    async def stop(self) -> Dict[str, Any]:
        if self._closed:
            return {"logged_in": self.logged_in}
        self._closed = True

        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # 关闭前再抢救一次登录态：用户可能刚登上就关了窗口
        if not self.logged_in:
            try:
                if await SUBPROCESS_LOOP.run(self._check_logged_in()):
                    await self._save_session()
                    self.logged_in = True
            except Exception:  # noqa: BLE001
                pass

        # ⚠️ 探测说没登录，但浏览器里**确实有登录凭据**时也要存下来。
        # 探测会失手（平台改接口、网络抖动、弹窗流程和预期不一样），
        # 而用户明明已经扫码成功了。这时候丢掉 Cookie 的代价是
        # "下次打开还要重扫"，收下的代价只是可能存了一份没用的——
        # 后者由采集前的登录态自检兜底，代价小得多。
        if not self.logged_in:
            try:
                cookies = await SUBPROCESS_LOOP.run(self._read_cookies())
                header = cookie_import.to_header(cookies)
                if cookies and not missing_login_cookies(self.channel, header):
                    await self._persist(cookies)
                    self.logged_in = True
                    logger.warning(
                        "[%s/%s] 自动判定没通过，但 Cookie 里有登录凭据，仍然保存了",
                        self.channel, self.account_name,
                    )
                else:
                    logger.warning(
                        "[%s/%s] 关窗时没有登录凭据，本次不保存。当前 Cookie：%s",
                        self.channel, self.account_name,
                        "、".join(cookie_names(header)[:30]) or "（一个都没有）",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("关窗兜底保存失败：%s", exc)

        await SUBPROCESS_LOOP.run(self._close_browser())
        self.manager.release_profile(self.channel, self.account_name)
        if getattr(self, "_holds_capacity", False):
            BROWSER_CAPACITY.release(f"login/{self.channel}/{self.account_name}")
            self._holds_capacity = False
        LIVE_SESSIONS.pop(self.session_id, None)
        return {"logged_in": self.logged_in}

    async def _close_browser(self) -> None:
        try:
            if self._cdp is not None:
                await self._cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._context = None
            self._page = None
            self._cdp = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None
            # Obscura 进程由 manager 统一管理，这里不单独停
            # 下次 _ensure_obscura 会复用已运行的进程


def _same_origin(a: str, b: str) -> bool:
    from urllib.parse import urlparse

    left, right = urlparse(a), urlparse(b)
    return bool(left.hostname) and (left.scheme, left.hostname, left.port) == (
        right.scheme, right.hostname, right.port
    )


#: session_id -> LiveBrowserSession
LIVE_SESSIONS: Dict[str, LiveBrowserSession] = {}


def register(session: LiveBrowserSession) -> str:
    LIVE_SESSIONS[session.session_id] = session
    return session.session_id


def get_session(session_id: str) -> Optional[LiveBrowserSession]:
    return LIVE_SESSIONS.get(session_id)


async def close_all() -> None:
    for session in list(LIVE_SESSIONS.values()):
        try:
            await session.stop()
        except Exception:  # noqa: BLE001
            pass
    LIVE_SESSIONS.clear()
