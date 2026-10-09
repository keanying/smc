"""采集实时画面：把**正在跑采集的那个页面**推到网页上看。

为什么要有它：
    无头模式下服务端不弹窗口，采集过程只能靠日志推断。日志能告诉你
    "点了筛选"，但告诉不了你"点完之后页面上是什么"——出现验证码、
    被风控挡了、卡在一个空列表，这些只有看一眼才知道。

和 live_session.py 的区别（两个都在推流，别搞混）：
    live_session   为**登录**服务：自己开浏览器，画面双向。
    live_view（本文件）  为**观察**服务：不开浏览器，只把采集器已经在用的
                   那个页面借来推流。**默认只读**，但可以显式接管。

默认只读的理由（没变，仍然成立）：
    采集器正在这个页面上按自己的节奏走流程（切多列 → 点筛选 → 逐条点开
    作品 → 翻评论）。观众随手点一下，采集器的下一步就落在一个它没预料到的
    页面上——轻则这一条作品采空，重则整轮流程错位。

那为什么还是开了接管：
    采集跑到一半弹出**拖动验证**（滑块）时，只读就把人堵死了：
    页面在等人拖，采集器在等页面，谁也不动，最后耗到看门狗超时把任务杀掉。
    这时候唯一能救的就是有人在画面上把滑块拖过去。
    "先停任务、再用登录窗口"那条路救不了——任务一停，这一轮采集就废了，
    而且登录窗口是另开的浏览器，验证要重新触发一次。

风险是这么控住的，三条缺一不可：
    1. **默认关闭**，必须显式点「接管」才通输入。手滑点一下画面不会有任何事。
    2. 接管期间画面上有明显的状态条，关掉浏览器/断开连接自动退出接管。
    3. **每一个注入的事件都写日志**（带 task_id）。原来那句"事后完全无法
       归因"是拒绝双向的主要理由，现在归因链是全的：日志里能看到
       几点几分谁往哪个坐标点了什么。

开销：
    只有**有人在看**的时候才 startScreencast；最后一个观众断开就
    stopScreencast。没人看的时候这个模块对采集的影响是零。

事件循环：
    页面属于 SUBPROCESS_LOOP（浏览器循环），WebSocket 属于服务器循环。
    帧在浏览器循环里产生，经 SUBPROCESS_LOOP.to_caller_loop 包装后投递回去。
    所有 Playwright / CDP 调用一律 SUBPROCESS_LOOP.run。
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..core.logging import get_logger
from ..core.subprocess_loop import SUBPROCESS_LOOP

logger = get_logger(__name__)

#: 推流参数。质量和分辨率都比登录窗口低一档：登录要看清二维码，
#: 这里只要能看出"页面现在长什么样、卡在哪一步"。
SCREENCAST_OPTIONS = {
    "format": "jpeg",
    "quality": 55,
    "maxWidth": 1280,
    "maxHeight": 800,
    "everyNthFrame": 2,
}

#: 单帧截图的最小间隔。前端轮询兜底时不至于把浏览器循环占满。
SNAPSHOT_MIN_INTERVAL = 0.7

FrameCallback = Callable[[str, int, int], Awaitable[None]]


def make_view_id(task_id: str, channel: str, account_name: str) -> str:
    """可读且稳定的 id：前端知道 task_id 就能直接拼出来，不用先查列表。"""
    return f"{task_id or 'adhoc'}::{channel}::{account_name or 'default'}"


class LiveView:
    """一个可观察的采集页面。"""

    def __init__(self, *, view_id: str, page, context, task_id: str,
                 task_name: str, channel: str, account_name: str,
                 scenic_name: str, engine: str = "human"):
        self.view_id = view_id
        self.task_id = task_id
        self.task_name = task_name
        self.channel = channel
        self.account_name = account_name
        self.scenic_name = scenic_name
        self.engine = engine
        self.started_at = time.time()

        self._page = page
        self._context = context
        self._cdp = None
        self._closed = False
        self._viewers: List[FrameCallback] = []
        self._lock = asyncio.Lock()
        #: 最近一帧。新观众连上时先补这一帧，不然要等页面下一次变化才有画面，
        #: 而采集经常停在一个静止的列表页上——那会看起来像"连上了但黑屏"。
        self._last_frame: Optional[Dict[str, Any]] = None
        self._last_snapshot_at = 0.0
        self._last_snapshot: bytes = b""
        #: 当前步骤，由采集器调用 note() 写进来。日志里那句话在画面旁边同步显示，
        #: 才知道这一帧对应的是哪一步。
        self.step: str = ""
        self.step_at: float = 0.0
        #: 人工接管开关。默认 False = 只读，前端发来的输入一律丢弃。
        self.takeover: bool = False
        self.takeover_at: float = 0.0
        #: 接管期间注入了多少个事件。写在 info() 里给前端显示，
        #: 也方便事后对日志——"这一轮到底有没有人动过页面"一眼可见。
        self.injected: int = 0

    # ---------------- 元信息 ----------------
    def note(self, step: str) -> None:
        self.step = step
        self.step_at = time.time()

    async def info(self) -> Dict[str, Any]:
        url, title = "", ""
        try:
            url = await SUBPROCESS_LOOP.run(self._page_url())
            title = await SUBPROCESS_LOOP.run(self._page.title())
        except Exception:  # noqa: BLE001  页面正在导航时取不到，不值得报错
            pass
        return {
            "view_id": self.view_id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "channel": self.channel,
            "account_name": self.account_name,
            "scenic_name": self.scenic_name,
            "engine": self.engine,
            "url": url,
            "title": title,
            "step": self.step,
            "running_seconds": round(time.time() - self.started_at, 1),
            "viewers": len(self._viewers),
            "streaming": self._cdp is not None,
            "takeover": self.takeover,
            "takeover_seconds": (round(time.time() - self.takeover_at, 1)
                                 if self.takeover else 0),
            "injected": self.injected,
        }

    async def _page_url(self) -> str:
        return self._page.url

    # ---------------- 人工接管 ----------------
    def set_takeover(self, on: bool) -> None:
        """开/关人工接管。关闭时**不**清 injected——那是这一轮的审计数据。"""
        on = bool(on)
        if on == self.takeover:
            return
        self.takeover = on
        self.takeover_at = time.time() if on else 0.0
        logger.warning(
            "[实时画面] 任务 %s / %s 的采集页面 %s 人工接管（账号 %s）%s",
            self.task_id or "-", self.channel, "进入" if on else "退出",
            self.account_name or "-",
            "" if on else f"，本轮共注入 {self.injected} 个事件",
        )

    async def handle_input(self, event: Dict[str, Any]) -> None:
        """把前端的鼠标/键盘事件打到采集页面上。

        ⚠️ 没开接管就直接丢弃——这是"手滑点一下不会毁掉一轮采集"的那道闸。
        """
        if self._closed or not self.takeover:
            return
        await SUBPROCESS_LOOP.run(self._handle_input(event))

    async def _handle_input(self, event: Dict[str, Any]) -> None:
        """跑在浏览器循环上。坐标按**真实视口**换算，不是直接用帧坐标。

        ⚠️ 为什么不能像登录窗口那样直接用帧坐标：
        推流有上限（SCREENCAST_OPTIONS 的 maxWidth/maxHeight），视口比它大的话
        CDP 会把画面**缩小**再发，帧坐标和视口坐标就不是一回事了。
        现在采集用的视口正好是 1280x800、和上限一样，所以不换算也碰巧是对的——
        但哪天有人把视口调大，点击就会整体偏移，而现象是"点了没反应"或者
        "点到了旁边的东西"，极难往"坐标没换算"上想。这里按比例算掉，
        以后改视口也不会出事。
        """
        page = self._page
        if page is None:
            return
        kind = event.get("type")

        def _xy() -> tuple:
            x, y = float(event.get("x", 0)), float(event.get("y", 0))
            frame = self._last_frame or {}
            fw, fh = int(frame.get("width") or 0), int(frame.get("height") or 0)
            size = None
            try:
                size = page.viewport_size
            except Exception:      # noqa: BLE001
                size = None
            if size and fw > 0 and fh > 0:
                x *= float(size["width"]) / fw
                y *= float(size["height"]) / fh
            return x, y

        try:
            if kind == "click":
                x, y = _xy()
                await page.mouse.click(x, y, button=event.get("button", "left"),
                                       click_count=int(event.get("clickCount", 1)))
            elif kind == "mousemove":
                x, y = _xy()
                await page.mouse.move(x, y)
            elif kind == "mousedown":
                x, y = _xy()
                await page.mouse.move(x, y)
                await page.mouse.down(button=event.get("button", "left"))
            elif kind == "mouseup":
                x, y = _xy()
                await page.mouse.move(x, y)
                await page.mouse.up(button=event.get("button", "left"))
            elif kind == "wheel":
                await page.mouse.wheel(float(event.get("deltaX", 0)),
                                       float(event.get("deltaY", 0)))
            elif kind == "key":
                await page.keyboard.press(str(event.get("key") or ""))
            elif kind == "type":
                await page.keyboard.type(str(event.get("text") or ""), delay=30)
            else:
                return
        except Exception as exc:      # noqa: BLE001
            # 注入失败不能把推流带崩：页面可能正在导航，或者采集器刚好把它关了
            logger.warning("[实时画面] 注入 %s 失败：%s", kind, exc)
            return

        self.injected += 1
        # ⚠️ 每个事件都记。原来拒绝双向的理由就是"事后无法归因"，
        #    有了这行，采集出问题时能直接对上"是不是人动过页面"。
        #    mousemove 拖一次滑块能有上百个点，降到 debug，别把日志淹了。
        level = logger.debug if kind == "mousemove" else logger.info
        level("[实时画面] 任务 %s 注入 %s %s", self.task_id or "-", kind,
              f"({event.get('x')},{event.get('y')})" if "x" in event else "")

    # ---------------- 单帧（轮询兜底 / 列表缩略图） ----------------
    async def snapshot(self) -> bytes:
        """来一张当前画面的 JPEG。

        节流是必须的：这个调用会占用浏览器循环，而采集器正等在同一条循环上
        做它自己的事。多个观众一起轮询时，没有节流就是在跟采集抢时间片。
        """
        now = time.monotonic()
        if self._last_snapshot and now - self._last_snapshot_at < SNAPSHOT_MIN_INTERVAL:
            return self._last_snapshot
        # 有推流在跑的话，直接用最近一帧，不必再截一次
        if self._last_frame:
            try:
                data = base64.b64decode(self._last_frame["data"])
                self._last_snapshot, self._last_snapshot_at = data, now
                return data
            except Exception:  # noqa: BLE001
                pass
        data = await SUBPROCESS_LOOP.run(
            self._page.screenshot(type="jpeg", quality=55, timeout=8000)
        )
        self._last_snapshot, self._last_snapshot_at = data, now
        return data

    # ---------------- 推流 ----------------
    async def attach(self, on_frame: FrameCallback) -> FrameCallback:
        """加一个观众。第一个观众到场时才真正开始推流。"""
        wrapped = SUBPROCESS_LOOP.to_caller_loop(on_frame)
        async with self._lock:
            self._viewers.append(wrapped)
            first = len(self._viewers) == 1
        if self._last_frame:
            # 先把最近一帧补给他，别让他对着黑屏等页面变化
            try:
                await on_frame(self._last_frame["data"],
                               self._last_frame["width"], self._last_frame["height"])
            except Exception:  # noqa: BLE001
                pass
        if first:
            await self._start_screencast()
        return wrapped

    async def detach(self, token: FrameCallback) -> None:
        """撤一个观众。最后一个走了就停止推流，把开销还给采集。"""
        async with self._lock:
            if token in self._viewers:
                self._viewers.remove(token)
            empty = not self._viewers
        if empty:
            await self._stop_screencast()

    async def _start_screencast(self) -> None:
        if self._closed or self._cdp is not None:
            return
        try:
            self._cdp = await SUBPROCESS_LOOP.run(
                self._context.new_cdp_session(self._page))
        except Exception as exc:  # noqa: BLE001
            # 推流开不起来不是致命的：前端会退回轮询单帧接口
            logger.warning("[实时画面] 建立 CDP 会话失败，退回单帧模式：%s", exc)
            self._cdp = None
            return

        async def _on_frame_event(params: Dict[str, Any]) -> None:
            if self._closed:
                return
            try:
                await self._cdp.send("Page.screencastFrameAck",
                                     {"sessionId": params["sessionId"]})
            except Exception:  # noqa: BLE001  导航期间 ack 会失败，忽略
                pass
            meta = params.get("metadata") or {}
            width = int(meta.get("deviceWidth") or 1280)
            height = int(meta.get("deviceHeight") or 800)
            self._last_frame = {"data": params["data"], "width": width, "height": height}
            for viewer in list(self._viewers):
                try:
                    await viewer(params["data"], width, height)
                except Exception:  # noqa: BLE001  某个观众断了不该影响别人
                    pass

        def _sync_handler(params: Dict[str, Any]) -> None:
            # 这个回调在浏览器循环里触发，create_task 也落在浏览器循环上
            asyncio.create_task(_on_frame_event(params))

        try:
            self._cdp.on("Page.screencastFrame", _sync_handler)
            await SUBPROCESS_LOOP.run(
                self._cdp.send("Page.startScreencast", SCREENCAST_OPTIONS))
            logger.info("[实时画面] 开始推流：%s", self.view_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[实时画面] 启动推流失败，退回单帧模式：%s", exc)
            self._cdp = None

    async def _stop_screencast(self) -> None:
        if self._cdp is None:
            return
        cdp, self._cdp = self._cdp, None
        try:
            await SUBPROCESS_LOOP.run(cdp.send("Page.stopScreencast"))
        except Exception:  # noqa: BLE001
            pass
        try:
            await SUBPROCESS_LOOP.run(cdp.detach())
        except Exception:  # noqa: BLE001
            pass
        logger.info("[实时画面] 观众已全部离开，停止推流：%s", self.view_id)

    async def close(self) -> None:
        self._closed = True
        await self._stop_screencast()
        self._viewers.clear()


# ---------------- 给采集器用的两个快捷方法 ----------------
def attach_session(session, ctx, channel: str, engine: str = "human"):
    """把一个 PageSession 的页面挂成可观察的实时画面。

    哪些采集路径**有**常驻页面可看：
      · 拟人模式（browser_capture 的子类）——全程都在页面上操作
      · 快手接口模式——每次请求的签名都要调页面里的 __ks_realm，浏览器必须常驻
    哪些没有：
      · 抖音/小红书/微博的接口模式——浏览器只在刷 Cookie 时开一下就关，
        没有"当前页面"这个东西可看

    注册几乎零成本（不开推流），出任何问题都只记日志不抛——
    看画面是便利功能，绝不能拖累采集本身。
    """
    try:
        view = register(LiveView(
            view_id=make_view_id(ctx.task_id, channel, session.account_name),
            page=session._page,
            context=session._context,
            task_id=ctx.task_id,
            task_name=str(ctx.params.get("task_name") or ""),
            channel=channel,
            account_name=session.account_name,
            scenic_name=ctx.scenic_name,
            engine=engine,
        ))
        ctx.log(f"[{channel}] 实时画面已就绪，任务详情页的「实时画面」"
                f"可以直接看到浏览器现在在做什么")
        return view
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] 实时画面注册失败（不影响采集）：%s", channel, exc)
        return None


async def detach_session(view) -> None:
    """采集结束时把画面撤掉。同样是出错不抛。"""
    if view is None:
        return
    try:
        await unregister(view.view_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("实时画面注销失败（可忽略）：%s", exc)


# ---------------- 注册表 ----------------
LIVE_VIEWS: Dict[str, LiveView] = {}


def register(view: LiveView) -> LiveView:
    old = LIVE_VIEWS.get(view.view_id)
    if old is not None and old is not view:
        # 同一条任务重跑时会撞上。旧的那个页面早就关了，直接顶掉。
        logger.debug("[实时画面] 顶替同 id 的旧会话：%s", view.view_id)
    LIVE_VIEWS[view.view_id] = view
    return view


def get(view_id: str) -> Optional[LiveView]:
    return LIVE_VIEWS.get(view_id)


def for_task(task_id: str) -> List[LiveView]:
    return [v for v in LIVE_VIEWS.values() if v.task_id == task_id]


def all_views() -> List[LiveView]:
    return sorted(LIVE_VIEWS.values(), key=lambda v: v.started_at, reverse=True)


async def unregister(view_id: str) -> None:
    view = LIVE_VIEWS.pop(view_id, None)
    if view is not None:
        await view.close()


async def close_all() -> None:
    for view_id in list(LIVE_VIEWS):
        await unregister(view_id)
