# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

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
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

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
    """本机的任务执行器。

    单独用（测试里、没接集群时）：自己扫到点的任务自己跑，和以前一样。
    接了集群（cluster 不为 None）：**不自己挑任务**，只跑主机派给本机的
    （src_opinion_crawl_task.node_id = 本机、status = queued），派任务的事归 cluster/service.py。
    """

    def __init__(self, config: Config, task_repo: TaskRepository, runner: TaskRunner,
                 browser_manager=None, cluster=None):
        self.config = config
        self.tasks = task_repo
        self.runner = runner
        self.browser_manager = browser_manager
        self.cluster = cluster
        self._fixed_max_running = int(config.get("scheduler.max_running_tasks", 3))

        self._scheduler: Optional[AsyncIOScheduler] = None
        self._running: Dict[str, asyncio.Task] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        #: 任务启动时刻（事件循环单调时钟），看门狗据此判断"跑了太久"
        self._started_at: Dict[str, float] = {}
        #: 运行中任务的名称 / 目标 / 平台 / 开始时间，顶栏悬停提示「现在在跑哪些任务」用
        self._running_meta: Dict[str, Dict[str, Any]] = {}
        self._poll_lock = asyncio.Lock()

    @property
    def max_running(self) -> int:
        return self.cluster.max_running if self.cluster is not None else self._fixed_max_running

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        # 上次进程被杀时可能留下 running 状态的任务，先清理掉。
        # 集群里只清本机的；主机再顺带清升级前没有机器归属的老记录
        if self.cluster is not None:
            me = self.cluster.me
            reset = await self.tasks.reset_stuck_running(
                [me.node_id, ""] if me.is_master_role else [me.node_id])
        else:
            reset = await self.tasks.reset_stuck_running()
        if reset:
            logger.warning("发现 %d 个上次未正常结束的任务，已标记为失败", reset)
        # 运行记录同理：本机还挂着「运行中」的，进程已经没了，记成「中断」
        runs = getattr(self.runner, "runs", None)
        if runs is not None:
            try:
                n = await runs.mark_interrupted(self._node_id())
                if n:
                    logger.warning("%d 条运行记录停在「运行中」（上次没正常结束），已记为中断", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理运行记录失败（不影响调度）：%s", exc)

        if not self.config.get("scheduler.enabled", True):
            if self.cluster is None:
                logger.warning("调度器在配置里被关闭，任务只能手动触发")
                return
            # 集群里本机照样要轮询：「立即执行」派过来的任务得有人跑。只是主机不再自动派到点的任务
            logger.warning("调度器在配置里被关闭，到点的任务不会自动执行，只能手动触发")

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
    def poke(self) -> None:
        """主机刚把任务派给本机：马上轮询一次，不等下一个 5 秒。"""
        try:
            asyncio.get_running_loop().create_task(self._poll())
        except RuntimeError:
            pass

    async def _poll_assigned(self) -> None:
        """集群：读别的机器发来的取消请求，再把派给本机的任务跑起来。"""
        for task_id in await self.tasks.should_stop(sorted(self._running)):
            event = self._cancel_events.get(task_id)
            if event is not None and not event.is_set():
                logger.info("任务 %s 收到取消请求（来自别的机器的页面）", task_id)
                event.set()
        node_id = self.cluster.me.node_id
        # 不按 max_running 截断：派过来的都是主机按本机空位算过的
        for task in await self.tasks.assigned_to(node_id):
            if task["task_id"] in self._running:
                continue
            if not await self.tasks.start_assigned(task["task_id"], node_id):
                continue        # 刚被取消了
            # 主机派给自己的，派发时记下了触发原因（手动执行 / 定时触发 / 排队恢复 / 立即执行）
            pop = getattr(self.cluster, "pop_trigger", None)
            await self._launch(task, claimed=True, trigger=pop(task["task_id"]) if pop else "")

    async def _poll(self) -> None:
        async with self._poll_lock:
            self._reap_finished()
            await self._watchdog()
            if self.cluster is not None:
                await self._poll_assigned()
                return
            slots = self.max_running - len(self._running)
            if slots <= 0:
                return

            # 谁先排队谁先跑：排队等账号的（有号能用了就不再干等到预定时间）、
            # 到点的、新建的，统一按开始等的时间排（scheduler/queue.py）
            from .queue import ready_tasks
            accounts = getattr(getattr(self.runner, "browser_manager", None), "accounts", None)
            candidates = await ready_tasks(self.tasks, accounts, limit=max(slots, 20))

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
                self._running_meta.pop(task_id, None)
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

    def _node_id(self) -> str:
        """本机编号：集群里用节点 ID，单机用主机名。运行记录里记「在哪台机器上跑的」"""
        me = getattr(self.cluster, "me", None) if self.cluster is not None else None
        if me is not None and getattr(me, "node_id", ""):
            return me.node_id
        import socket
        return socket.gethostname()

    @staticmethod
    def _trigger_of(task: Dict[str, Any], claimed: bool) -> str:
        """这次是怎么触发的（运行记录的 trigger_type）。看的是启动前任务在库里的状态。

        集群派过来的（claimed）看不到派发前的状态：主机派给自己时会留下原因（cluster.pop_trigger），
        派给别的机器的拿不到，记「集群派发」。"""
        if claimed:
            return "dispatch"
        from ..repositories.run_repo import trigger_of_task
        return trigger_of_task(task)

    async def _launch(self, task: Dict[str, Any], claimed: bool = False, trigger: str = "") -> None:
        task_id = task["task_id"]
        # 运行记录要的两样：怎么触发的、在哪台机器上（runner.run 读这两个键）
        task = {**task, "_trigger": trigger or self._trigger_of(task, claimed), "_node_id": self._node_id()}
        if not claimed:
            await self.tasks.update(task_id, {"status": TaskStatus.QUEUED.value})
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event
        self._started_at[task_id] = asyncio.get_event_loop().time()
        self._running_meta[task_id] = {
            "task_id": task_id,
            "task_name": task.get("task_name") or task_id,
            "scenic_name": task.get("scenic_name") or task.get("scenic_id") or "",
            "channels": list(task.get("channels") or []) if isinstance(task.get("channels"), (list, tuple)) else [],
            "started_at": time.time(),
        }
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
        """立即执行一次，不等轮询。集群里由本机当场挑一台机器派出去（可能是别的机器）。"""
        self._reap_finished()
        if task_id in self._running:
            return False
        if self.cluster is not None:
            return await self.cluster.dispatch_now(task_id) is not None
        task = await self.tasks.get(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        if len(self._running) >= self.max_running:
            raise RuntimeError(
                f"当前已有 {len(self._running)} 个任务在跑，达到上限 {self.max_running}，请稍后再试"
            )
        await self._launch(task, trigger="manual")
        return True

    async def cancel(self, task_id: str) -> bool:
        event = self._cancel_events.get(task_id)
        if event is None and self.cluster is not None:
            # 在别的机器上排队 / 在跑：排队的直接取消，在跑的打标记，那台机器几秒内读到后自己停
            outcome = await self.tasks.request_cancel(task_id)
            if outcome == "flagged":
                return True
            if outcome == "canceled":
                task = await self.tasks.get(task_id)
                if task:
                    await self.tasks.reschedule(task)
                await self._cancel_waiting_run(task_id)
                return False
        if event is None:
            # 没在跑，但可能是排队中的周期任务，直接停用
            task = await self.tasks.get(task_id)
            await self.tasks.update(task_id, {"status": TaskStatus.CANCELED.value, "queued_at": None})
            if task and task.get("status") == TaskStatus.WAITING.value:
                # 取消"排队等账号"：next_run_time 还是那个恢复时刻，不改的话
                # 到点照样会被捞起来跑。按任务自己的调度规则重新算（一次性的就停用）。
                await self.tasks.reschedule(task)
                await self._cancel_waiting_run(task_id)
            return False
        event.set()
        return True

    async def _cancel_waiting_run(self, task_id: str) -> None:
        """排队被取消：那条「排队等账号」的运行记录记成「已取消」。写不上不影响取消本身。"""
        runs = getattr(getattr(self, "runner", None), "runs", None)
        if runs is None:
            return
        try:
            await runs.cancel_waiting(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取消排队后更新运行记录失败（%s）：%s", task_id, exc)

    def running_task_ids(self) -> Set[str]:
        return set(self._running)

    def _running_brief(self) -> List[Dict[str, Any]]:
        now = time.time()
        out = []
        for task_id in self._running:
            meta = self._running_meta.get(task_id) or {"task_id": task_id, "task_name": task_id,
                                                       "scenic_name": "", "channels": [], "started_at": now}
            out.append({**{k: v for k, v in meta.items() if k != "started_at"},
                        "running_seconds": int(now - meta["started_at"])})
        return sorted(out, key=lambda x: -x["running_seconds"])

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self._scheduler is not None,
            "running": sorted(self._running),
            "running_count": len(self._running),
            # 在跑的是哪些任务（按开始时间排）：顶栏「运行中任务」悬停时列出来
            "running_tasks": self._running_brief(),
            "max_running": self.max_running,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        }
