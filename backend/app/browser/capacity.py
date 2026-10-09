"""浏览器并发闸门：限制**同时活着的 Chromium 数量**。

为什么必须有这个东西（实测数据，不是估的）
------------------------------------------------------------------
在本项目的启动参数下，量过一个 headless Chromium 的真实开销：

    about:blank（纯浏览器自身开销）   10 个进程   ~700 MB
    带 2000 个 div 的页面             10 个进程   ~745 MB

也就是说**700 MB 是固定成本**，页面内容只占几十兆。还试过几套
"省内存"的启动参数（--renderer-process-limit / 关后台节流 / 小视口），
三套下来内存差异全在噪声范围内——**调参数没用**，唯一的变量是
同时开几个。

于是：

    并发 6 个浏览器 = 60 个进程 + 4.5 GB
    并发 8 个浏览器 = 80 个进程 + 6.0 GB

而 `scheduler.max_running_tasks` 默认就是 8。一台 8G 的机器上，
8 条浏览器型任务同时跑必然把内存吃光 → 开始换页 → CPU 全耗在
kswapd 上 → 整机没有响应，只能重启。这不是"泄漏"（关掉之后内存
是干净回落的），是**根本没有上限**。

所以闸门不看任务数，只看浏览器数——这是真正的稀缺资源。
携程 / 同程 / 去哪儿这些纯 HTTP 的渠道不开浏览器，也就不受它限制：
8 条任务照样能同时跑，只要它们不是浏览器型的。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from ..core.logging import get_logger

logger = get_logger(__name__)

#: 一个 Chromium 的实测内存开销（GB）。用来从机器内存反推能开几个。
#: 宁可估大：估小了的后果是把机器跑死，估大了只是慢一点。
BROWSER_FOOTPRINT_GB = 0.9

#: 给系统和本进程留的余量（GB）。MySQL、Redis、Python 自己都要吃内存。
RESERVED_GB = 1.5

#: 自动推算时的上下限。上限压在 4：再多的话单机带宽和风控都先扛不住了。
MIN_AUTO = 1
MAX_AUTO = 4

#: 低于这个可用内存（GB）就不再开新浏览器，先等着。
#: 这是防"死亡螺旋"的那道闸：内存一旦见底，系统会开始换页，
#: 此时再开浏览器只会让所有已经在跑的任务一起变慢直至全部超时。
DEFAULT_MIN_FREE_GB = 1.0

#: 等内存的轮询间隔和上限
_POLL_SECONDS = 5.0
DEFAULT_WAIT_TIMEOUT = 600.0


def available_memory_gb() -> Optional[float]:
    """当前可用内存（GB）。拿不到就返回 None——**不猜**。

    拿不到时的策略是"放行"：宁可不限制，也不能因为读不到内存
    就把所有采集卡死。并发上限那一层还在。
    """
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:  # noqa: BLE001
        pass
    # psutil 没装时退回 /proc（Linux）。Windows 上直接放弃。
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    return None


def total_memory_gb() -> Optional[float]:
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    return None


def auto_limit() -> int:
    """按机器内存推算能同时开几个浏览器。读不到内存就保守给 2。"""
    total = total_memory_gb()
    if not total:
        return 2
    usable = max(0.0, total - RESERVED_GB)
    return max(MIN_AUTO, min(MAX_AUTO, int(usable // BROWSER_FOOTPRINT_GB)))


class BrowserCapacity:
    """全局单例。**每一处启动 Chromium 的代码都要先过这里。**

    ⚠️ 用 threading 信号量而不是 asyncio 的，是因为本项目有**两个事件循环**：
    服务器循环（uvicorn）和浏览器循环（SUBPROCESS_LOOP，跑在另一个线程里）。
    asyncio.Semaphore 绑定在创建它的循环上，跨循环用会静默失效——
    两个循环各拿到一个"满额"的闸门，合起来还是没有上限，而且完全看不出来。
    threading 的版本对两边都成立。

    代价是拿不到名额时要轮询（而不是被唤醒）。开一个浏览器本来就要好几秒，
    这点延迟无所谓。
    """

    def __init__(self) -> None:
        self._sem: Optional[threading.Semaphore] = None
        self._limit = 0
        self._min_free_gb = DEFAULT_MIN_FREE_GB
        self._wait_timeout = DEFAULT_WAIT_TIMEOUT
        self._active = 0
        self._waiting = 0
        self._peak = 0
        self._state_lock = threading.Lock()

    # ---------------- 配置 ----------------
    def configure(self, config) -> None:
        """从配置里取上限。`browser.max_concurrent` 留空或 0 = 按内存自动推算。"""
        raw = config.get("browser.max_concurrent", 0)
        try:
            limit = int(raw or 0)
        except (TypeError, ValueError):
            limit = 0
        self._limit = limit if limit > 0 else auto_limit()
        try:
            self._min_free_gb = float(
                config.get("browser.min_free_memory_gb", DEFAULT_MIN_FREE_GB))
        except (TypeError, ValueError):
            self._min_free_gb = DEFAULT_MIN_FREE_GB
        try:
            self._wait_timeout = float(
                config.get("browser.capacity_wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT))
        except (TypeError, ValueError):
            self._wait_timeout = DEFAULT_WAIT_TIMEOUT
        with self._state_lock:
            self._sem = threading.Semaphore(self._limit)
            self._active = 0
        total = total_memory_gb()
        logger.info(
            "浏览器并发上限：%d 个（%s；机器内存 %s，一个 Chromium 实测约 %.1fGB/10 进程），"
            "可用内存低于 %.1fGB 时暂停开新浏览器",
            self._limit, "手工配置" if limit > 0 else "按内存自动推算",
            f"{total:.1f}GB" if total else "读不到", BROWSER_FOOTPRINT_GB,
            self._min_free_gb,
        )

    @property
    def limit(self) -> int:
        if self._limit <= 0:
            self._limit = auto_limit()
        return self._limit

    def _semaphore(self) -> threading.Semaphore:
        with self._state_lock:
            if self._sem is None:
                self._sem = threading.Semaphore(self.limit)
            return self._sem

    # ---------------- 闸门 ----------------
    async def acquire(self, who: str = "") -> None:
        sem = self._semaphore()
        started = time.monotonic()
        with self._state_lock:
            self._waiting += 1
        try:
            got = sem.acquire(blocking=False)
            if not got:
                logger.info("[浏览器闸门] %s 排队中：已开 %d/%d 个",
                            who or "任务", self._active, self.limit)
            while not got:
                # 轮询而不是阻塞等待：阻塞会把整个事件循环卡住，
                # 连接口和心跳一起停——那比排队本身糟糕得多。
                await asyncio.sleep(_POLL_SECONDS)
                got = sem.acquire(blocking=False)
        finally:
            with self._state_lock:
                self._waiting -= 1

        try:
            await self._wait_for_memory(who, started)
        except BaseException:
            # 等内存时被取消/超时，必须把名额还回去，否则这个位置永久丢失
            sem.release()
            raise

        with self._state_lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
            active = self._active
        waited = time.monotonic() - started
        logger.info("[浏览器闸门] %s 拿到名额（%d/%d 在用%s）",
                    who or "任务", active, self.limit,
                    f"，等了 {waited:.0f}s" if waited >= 1 else "")

    async def _wait_for_memory(self, who: str, started: float) -> None:
        """可用内存不够就等。等不到就放行并**大声说出来**。

        为什么超时之后是放行而不是报错：内存长期紧张时，报错会让所有
        任务连着失败，用户看到一片红也不知道是内存的事。放行 + 一条
        明确的 ERROR 日志，至少任务还能跑，而原因是查得到的。
        """
        if self._min_free_gb <= 0:
            return
        warned = False
        while True:
            free = available_memory_gb()
            if free is None or free >= self._min_free_gb:
                if warned and free is not None:
                    logger.info("[浏览器闸门] 内存已回到 %.1fGB，%s 继续", free, who)
                return
            if time.monotonic() - started > self._wait_timeout:
                logger.error(
                    "[浏览器闸门] 等了 %.0f 秒内存仍只有 %.1fGB（阈值 %.1fGB），"
                    "放行 %s——但这台机器多半撑不住，把 browser.max_concurrent 调小",
                    self._wait_timeout, free, self._min_free_gb, who)
                return
            if not warned:
                logger.warning(
                    "[浏览器闸门] 可用内存只剩 %.1fGB（低于 %.1fGB），"
                    "%s 先等着——这时候再开浏览器会把整机拖进换页",
                    free, self._min_free_gb, who)
                warned = True
            await asyncio.sleep(_POLL_SECONDS)

    def release(self, who: str = "") -> None:
        """还名额。**同步方法**：所有调用点都在 finally / close 里，
        改成 async 很容易漏掉一处，而漏一处就是永久少一个名额。"""
        sem = self._sem
        if sem is None:
            return
        with self._state_lock:
            if self._active <= 0:
                # 重复释放会把上限越放越大，等于闸门失效。宁可吞掉。
                logger.debug("[浏览器闸门] %s 重复释放，已忽略", who or "任务")
                return
            self._active -= 1
            active = self._active
        sem.release()
        logger.debug("[浏览器闸门] %s 已释放（%d/%d 在用）",
                     who or "任务", active, self.limit)

    # ---------------- 状态 ----------------
    def stats(self) -> dict:
        free = available_memory_gb()
        total = total_memory_gb()
        return {
            "active": self._active,
            "limit": self.limit,
            "waiting": self._waiting,
            "peak": self._peak,
            "min_free_gb": self._min_free_gb,
            "memory_available_gb": round(free, 2) if free is not None else None,
            "memory_total_gb": round(total, 2) if total is not None else None,
            "footprint_gb": BROWSER_FOOTPRINT_GB,
        }


#: 全局单例。整个进程共用一个闸门。
BROWSER_CAPACITY = BrowserCapacity()
