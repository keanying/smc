"""任务调度器。

四种触发方式统一到一个轮询循环里，不为每个任务单独注册 APScheduler job——
理由是任务可以在页面上随时增删改，逐个注册/注销 job 容易和 DB 状态不一致；
而"每隔几秒扫一次 next_run_time <= now"这个模型简单、可重启恢复、
多副本部署时也只需给取任务加一把锁就能扩展。

    once     创建后立即执行一次
    at       到指定时刻执行一次，执行完停用
    interval 固定间隔重复
    cron     cron 表达式（5 段标准 / 6 段 Quartz 秒在前）
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..core.config import Config
from ..core.constants import TaskStatus
from ..core.logging import get_logger
from ..repositories.task_repo import TaskRepository
from .runner import TaskRunner

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 5


class TaskScheduler:
    def __init__(self, config: Config, task_repo: TaskRepository, runner: TaskRunner,
                 browser_manager=None):
        self.config = config
        self.tasks = task_repo
        self.runner = runner
        self.browser_manager = browser_manager
        self.max_running = int(config.get("scheduler.max_running_tasks", 3))

        self._scheduler: Optional[AsyncIOScheduler] = None
        self._running: Dict[str, asyncio.Task] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        #: 任务启动时刻（事件循环单调时钟），看门狗据此判断"跑了太久"
        self._started_at: Dict[str, float] = {}
        self._poll_lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        # 上次进程被杀时可能留下 running 状态的任务，先清理掉
        reset = await self.tasks.reset_stuck_running()
        if reset:
            logger.warning("发现 %d 个上次未正常结束的任务，已标记为失败", reset)

        if not self.config.get("scheduler.enabled", True):
            logger.warning("调度器在配置里被关闭，任务只能手动触发")
            return

        self._scheduler = AsyncIOScheduler(timezone=self.config.get("scheduler.timezone", "Asia/Shanghai"))
        self._scheduler.add_job(
            self._poll,
            trigger=IntervalTrigger(seconds=POLL_INTERVAL_SECONDS),
            id="smc_poll",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=int(self.config.get("scheduler.misfire_grace_seconds", 300)),
        )
        # 定时采集各账号的 Cookie：登录态过期不用等到任务跑起来才发现
        refresh_minutes = int(self.config.get("browser.auto_refresh_cookie_minutes", 0) or 0)
        if refresh_minutes > 0 and self.browser_manager is not None:
            self._scheduler.add_job(
                self._refresh_cookies,
                trigger=IntervalTrigger(minutes=refresh_minutes),
                id="smc_cookie_refresh",
                max_instances=1,
                coalesce=True,
            )
            logger.info("已开启 Cookie 定时采集，每 %d 分钟一次", refresh_minutes)

        self._scheduler.start()
        logger.info(
            "调度器已启动（每 %d 秒扫描一次，最多并行 %d 个任务）",
            POLL_INTERVAL_SECONDS, self.max_running,
        )

    async def _refresh_cookies(self) -> None:
        """后台把每个启用账号的 Cookie 重抓一遍。

        走的就是账号页「采集 Cookie」那条链路（静默打开 profile 取 Cookie），
        失效的账号会被自动标成 expired，页面上能直接看到。

        ⚠️ skip_busy=True 不能去掉：正在跑的采集任务持有那个 profile，
        这时候再开一个 Chromium 会把采集那边的页面卡死（见 check_all 的说明）。
        """
        try:
            results = await self.browser_manager.check_all(skip_busy=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时采集 Cookie 失败：%s", exc)
            return
        if not results:
            return
        failed = [r for r in results if not r.get("ok")]
        skipped = [r for r in results if r.get("skipped")]
        logger.info(
            "定时采集 Cookie 完成：%d 个账号，%d 个失效%s",
            len(results), len(failed),
            f"，%d 个因正在采集跳过" % len(skipped) if skipped else "",
        )
        for row in failed:
            logger.warning(
                "账号 [%s/%s] 登录态异常：%s",
                row.get("channel"), row.get("account_name"), row.get("message"),
            )

    async def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        for task_id in list(self._cancel_events):
            self._cancel_events[task_id].set()
        if self._running:
            logger.info("等待 %d 个运行中的任务收尾…", len(self._running))
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    # ---------------- 轮询 ----------------
    async def _poll(self) -> None:
        async with self._poll_lock:
            self._reap_finished()
            await self._watchdog()
            slots = self.max_running - len(self._running)
            if slots <= 0:
                return

            candidates = await self.tasks.pending_once_tasks(limit=slots)
            if len(candidates) < slots:
                candidates += await self.tasks.due_tasks(limit=slots - len(candidates))

            for task in candidates[:slots]:
                if task["task_id"] in self._running:
                    continue
                await self._launch(task)

    async def _watchdog(self) -> None:
        """看门狗：任务跑得太久就取消掉，别让它永远卡着。

        ⚠️ 这是"抖音采到一半就不动了"这类问题的最后兜底：
        平台风控把连接挂住（不回包也不断开）时，请求超时、重试、
        取消信号全都可能失效——任务看起来在跑，实际一条数据也出不来，
        占着并发槽位，后面的任务永远排不上。
        超过 crawl.task_timeout_minutes（默认 6 小时）还没结束的任务，
        先置取消信号让它自己收尾，再过 5 分钟还没死就直接 cancel。
        """
        timeout_minutes = float(self.config.get("crawl.task_timeout_minutes", 360) or 360)
        if timeout_minutes <= 0:
            return
        now = asyncio.get_event_loop().time()
        for task_id, handle in list(self._running.items()):
            started = self._started_at.get(task_id)
            if started is None:
                self._started_at[task_id] = now
                continue
            elapsed_minutes = (now - started) / 60
            if elapsed_minutes < timeout_minutes:
                continue
            event = self._cancel_events.get(task_id)
            if event is not None and not event.is_set():
                logger.error(
                    "任务 %s 已运行 %.0f 分钟，超过看门狗上限 %.0f 分钟，置取消信号",
                    task_id, elapsed_minutes, timeout_minutes,
                )
                event.set()
                continue
            # 取消信号已经给过了，还没死：强杀
            logger.error("任务 %s 取消后仍未退出，强制 cancel", task_id)
            handle.cancel()

    def _reap_finished(self) -> None:
        for task_id, handle in list(self._running.items()):
            if handle.done():
                self._running.pop(task_id, None)
                self._cancel_events.pop(task_id, None)
                self._started_at.pop(task_id, None)
                cancelled = handle.cancelled()
                exception = handle.exception() if not cancelled else None
                if exception:
                    logger.error("任务 %s 异常结束：%s", task_id, exception)
                if cancelled or exception:
                    # ⚠️ 兜底：协程被强杀 / 抛了非预期异常时，
                    # runner 里的 mark_finished 可能没来得及跑。
                    # 不补这一下，任务会在库里永远停在"运行中"，
                    # 既占着并发名额，页面上也一直转圈。
                    asyncio.create_task(self._force_mark_stopped(task_id, cancelled))

    async def _force_mark_stopped(self, task_id: str, cancelled: bool) -> None:
        """把还挂在"运行中"的任务收尾。已经是终态的不动它。"""
        try:
            task = await self.tasks.get(task_id)
            if not task:
                return
            if task.get("status") not in (TaskStatus.RUNNING.value,
                                          TaskStatus.QUEUED.value):
                return      # runner 自己已经收过尾了
            status = TaskStatus.CANCELED.value if cancelled else TaskStatus.FAILED.value
            reason = "被强制取消（看门狗超时）" if cancelled else "异常结束"
            logger.warning("任务 %s 结束时状态还挂在运行中，补标为 %s", task_id, status)
            await self.tasks.mark_finished(task_id, status, error=reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("补标任务 %s 状态失败：%s", task_id, exc)

    async def _launch(self, task: Dict[str, Any]) -> None:
        task_id = task["task_id"]
        await self.tasks.update(task_id, {"status": TaskStatus.QUEUED.value})
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event
        self._started_at[task_id] = asyncio.get_event_loop().time()
        self._running[task_id] = asyncio.create_task(self._run_and_reschedule(task, cancel_event))
        logger.info("任务已启动：%s（%s）", task["task_name"], task_id)

    async def _run_and_reschedule(self, task: Dict[str, Any], cancel_event: asyncio.Event) -> None:
        task_id = task["task_id"]
        try:
            await self.runner.run(task, cancel_event)
        finally:
            try:
                next_run = await self.tasks.reschedule(task)
                if next_run:
                    logger.info("任务 %s 下次运行时间：%s", task_id, next_run)
            except Exception as exc:  # noqa: BLE001
                logger.error("计算任务 %s 下次运行时间失败：%s", task_id, exc)
            # 自己摘干净。不能只靠 _poll 回收：调度器被关掉时 _poll 不跑，
            # 任务会永远留在 _running 里，导致"已经跑完了却无法再次触发"。
            self._running.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self._started_at.pop(task_id, None)

    # ---------------- 手动控制 ----------------
    async def trigger_now(self, task_id: str) -> bool:
        """立即执行一次，不等轮询。"""
        self._reap_finished()
        if task_id in self._running:
            return False
        task = await self.tasks.get(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        if len(self._running) >= self.max_running:
            raise RuntimeError(
                f"当前已有 {len(self._running)} 个任务在跑，达到上限 {self.max_running}，请稍后再试"
            )
        await self._launch(task)
        return True

    async def cancel(self, task_id: str) -> bool:
        event = self._cancel_events.get(task_id)
        if event is None:
            # 没在跑，但可能是排队中的周期任务，直接停用
            await self.tasks.update(task_id, {"status": TaskStatus.CANCELED.value})
            return False
        event.set()
        return True

    def running_task_ids(self) -> Set[str]:
        return set(self._running)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self._scheduler is not None,
            "running": sorted(self._running),
            "running_count": len(self._running),
            "max_running": self.max_running,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        }
