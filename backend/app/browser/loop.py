"""给 Playwright 单独跑一个事件循环。

要解决的问题
------------------------------------------------------------------
Playwright 启动浏览器时必须先 fork 一个 node 驱动进程。而 Windows 上
只有 ProactorEventLoop 支持创建子进程，SelectorEventLoop 会直接抛
NotImplementedError：

    File "asyncio\\base_events.py", line 503, in _make_subprocess_transport
        raise NotImplementedError

uvicorn 恰好会在某些情况下把策略切成 Selector：

    # uvicorn/config.py
    use_subprocess = bool(self.reload or self.workers > 1)
    # uvicorn/loops/asyncio.py
    if sys.platform == "win32" and use_subprocess:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

也就是说，只要在 Windows 上带 --reload 或 --workers>1 启动，
服务器主循环就变成不能开子进程的那种，账号登录和抖音/快手采集全部起不来。

做法
------------------------------------------------------------------
不去和 uvicorn 抢事件循环策略（那是它自己的选择，而且改了也未必生效——
策略要在循环创建之前设，而应用模块是循环创建之后才被导入的）。
改成：**所有 Playwright 调用都提交到本模块维护的一个后台线程循环上执行**。
这个循环由我们自己创建，Windows 上固定用 ProactorEventLoop，
和服务器怎么启动完全无关。

注意事项
------------------------------------------------------------------
1. Playwright 的连接对象绑定在创建它的循环上，所以从建浏览器到关浏览器
   的每一次调用都必须在同一个循环里，不能只把 launch 挪过去。
2. 数据库操作（aiomysql 连接池）绑定在服务器循环上，**不能**丢到这个循环里跑。
   所以调用方要把「操作浏览器」和「读写数据库」拆开：
   浏览器部分用 run() 提交，拿到结果后回到服务器循环再落库。
3. 浏览器循环里产生的回调（比如推流帧）要送回服务器循环，
   用 to_caller_loop() 包一层，不要直接 await 服务器循环上的对象。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any, Awaitable, Callable, Coroutine, Optional, TypeVar

from ..core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

START_TIMEOUT_SECONDS = 15


class BrowserLoop:
    """维护一个专供 Playwright 使用的后台事件循环。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    # ---------------- 生命周期 ----------------
    @staticmethod
    def _new_loop() -> asyncio.AbstractEventLoop:
        if sys.platform == "win32":
            # Windows 上只有 Proactor 能开子进程；显式指定，不看全局策略脸色
            return asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
        return asyncio.new_event_loop()

    def start(self) -> None:
        """幂等启动。第一次用到浏览器时自动调用，也可以在服务启动时预热。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._ready.clear()

            def _runner() -> None:
                loop = self._new_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.close()
                    except Exception:  # noqa: BLE001
                        pass

            self._thread = threading.Thread(
                target=_runner, name="playwright-loop", daemon=True
            )
            self._thread.start()

        if not self._ready.wait(START_TIMEOUT_SECONDS):
            raise RuntimeError("浏览器事件循环线程启动超时")

        logger.info(
            "浏览器事件循环已就绪（%s，线程 %s）",
            type(self._loop).__name__, self._thread.name if self._thread else "-",
        )

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._loop = None
        self._thread = None
        logger.info("浏览器事件循环已停止")

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        assert self._loop is not None
        return self._loop

    def is_running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    # ---------------- 提交任务 ----------------
    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """在浏览器循环上执行协程，并把结果交回调用方所在的循环。

        调用方被取消时，会一并取消浏览器循环上的那个任务。
        """
        target = self.loop
        future = asyncio.run_coroutine_threadsafe(coro, target)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    def to_caller_loop(
        self, callback: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[None]]:
        """把「运行在服务器循环上的异步回调」包装成可以从浏览器循环里安全调用的形式。

        推流帧、状态变更这些是在浏览器循环里产生的，而 WebSocket 属于服务器循环，
        直接 await 会跨循环出问题。这里记下调用方循环，之后用 threadsafe 方式投递。
        """
        caller_loop = asyncio.get_running_loop()

        async def _forward(*args: Any, **kwargs: Any) -> None:
            future = asyncio.run_coroutine_threadsafe(
                callback(*args, **kwargs), caller_loop
            )
            try:
                # 不阻塞浏览器循环太久：推流帧丢一两个无所谓，卡住才是问题
                await asyncio.wrap_future(future)
            except Exception as exc:  # noqa: BLE001
                logger.debug("跨循环回调失败（可忽略）：%s", exc)

        return _forward


#: 全局单例。整个进程只有这一个浏览器循环。
BROWSER_LOOP = BrowserLoop()


def current_loop_supports_subprocess() -> bool:
    """当前运行的循环能不能开子进程。用于自检和错误提示。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if sys.platform != "win32":
        return True
    # Windows 上只有 Proactor 实现了 _make_subprocess_transport
    return type(loop).__name__ == "ProactorEventLoop"
