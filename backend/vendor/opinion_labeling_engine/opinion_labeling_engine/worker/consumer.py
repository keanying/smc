"""消费者：从队列取任务 → 标注 → 攒批写库 → ack。

关键设计：
    1. **先写库，后 ack。** 顺序反过来的话，写库失败的任务就丢了。
       现在的顺序下最坏情况是重复标注一次，而写回是幂等 UPDATE，无害。
    2. **每个 worker 线程持有自己的攒批缓冲。** 不用共享缓冲 + 锁，
       线程之间零竞争；代价是 flush 粒度变成「每线程 batch」，可以接受。
    3. **缓冲里的任务在 flush 之前都还挂着租约。** 进程被 kill 时它们不会 ack，
       可见性超时后由 reclaim 捞回重投，所以攒批不会造成数据丢失。
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

from ..config import AppConfig
from ..domain.enums import LabelSource
from ..domain.models import CommentRecord, LabelResult
from ..labeling.service import LabelingFailed, LabelingService
from ..logging_conf import get_logger
from ..queues.base import BaseQueue, Lease
from ..storage.failure_store import BaseFailureStore, NullFailureStore
from ..storage.repository import LabelRepository

__all__ = ["LabelingWorker", "WorkerPool"]

logger = get_logger(__name__)

# 缓冲里的一项：租约 + 原始评论 + 标注结果 + 复核标记
BufferItem = Tuple[Lease, CommentRecord, LabelResult, int]


class LabelingWorker(threading.Thread):
    """单个消费线程。"""

    def __init__(
        self,
        name: str,
        cfg: AppConfig,
        queue: BaseQueue,
        service: LabelingService,
        repository: Optional[LabelRepository],
        stop_event: threading.Event,
        failure_store: Optional[BaseFailureStore] = None,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._cfg = cfg
        self._queue = queue
        self._service = service
        self._repo = repository
        self._failures = failure_store or NullFailureStore()
        # 注意：不能叫 self._stop —— Thread 内部用 _stop 做私有方法，覆盖它会让 join() 崩掉
        self._stop_event = stop_event
        self._buffer: List[BufferItem] = []
        self._last_flush = time.monotonic()
        self.processed = 0
        self.write_failed = 0
        self.label_failed = 0

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:  # noqa: D102 - Thread.run
        logger.info("worker %s 启动", self.name)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:                       # noqa: BLE001
                # 消费循环绝不允许因为单条数据异常而退出，否则并发度会悄悄掉下来
                logger.exception("worker %s 循环异常，继续运行", self.name)
                time.sleep(0.5)
        self._flush(force=True)
        logger.info("worker %s 退出，累计处理 %d 条", self.name, self.processed)

    def _tick(self) -> None:
        lease = self._queue.get()
        if lease is None:
            self._flush_if_due()
            return

        record = lease.task.record
        try:
            result = self._service.label(record)
        except LabelingFailed as exc:
            # 模型没标出来 —— 绝不写库。先交给队列重试；队列放弃后进失败池，
            # 等 retry-failed 再捞出来标。写个假中性会把行占掉，
            # 以后既认不出它是失败的，下游指标还会当成一条真实的中性评价。
            self.label_failed += 1
            self._handle_failure(lease, record, str(exc))
            return

        review_flag = self._review_flag(result)

        if self._repo is None:
            # 无库模式（联调 / 干跑）：直接确认，结果只进日志
            self._queue.ack(lease)
            self.processed += 1
            logger.info("[dry-run] %s", result.summary())
            return

        self._buffer.append((lease, record, result, review_flag))
        self._flush_if_due()

    def _handle_failure(self, lease: Lease, record: CommentRecord, error: str) -> None:
        """标注失败的收尾：队列重试，重试用尽后落失败池。"""
        gave_up = self._queue.nack(lease, error=error)
        if not gave_up:
            return

        # 只把标记置成 ai_failed，五个标注字段一个都不动
        if self._repo is not None and self._cfg.storage.write_flag_on_failure:
            try:
                self._repo.mark_failed([record])
            except Exception:                       # noqa: BLE001
                logger.exception("标记标注失败状态时出错，评论 %s", record.comment_id)

        try:
            self._failures.record(record, error)
            self._service.stats.incr("failed_stored")
            logger.warning("评论 %s 已放弃重试，记入失败池等待重标", record.comment_id)
        except Exception:                           # noqa: BLE001
            # 失败池写不进去也不能让 worker 挂掉，但必须把原始数据打进日志，
            # 否则这条评论就真的无迹可寻了
            logger.exception("失败池写入异常，评论 %s 的失败只留在日志里：%s",
                             record.comment_id, error)

    # ------------------------------------------------------------------ 攒批
    def _flush_if_due(self) -> None:
        if not self._buffer:
            self._last_flush = time.monotonic()
            return
        due_by_size = len(self._buffer) >= self._cfg.worker.flush_batch_size
        due_by_time = (time.monotonic() - self._last_flush) >= self._cfg.worker.flush_interval
        if due_by_size or due_by_time:
            self._flush()

    def _flush(self, *, force: bool = False) -> None:
        """把缓冲写库，成功后统一 ack；失败则整批 nack 重投。"""
        if not self._buffer or self._repo is None:
            self._buffer.clear()
            self._last_flush = time.monotonic()
            return

        batch = self._buffer
        self._buffer = []
        pairs = [(rec, res) for _, rec, res, _ in batch]
        flags = [flag for _, _, _, flag in batch]

        try:
            self._repo.update_many(pairs, review_flags=flags)
        except Exception as exc:                    # noqa: BLE001
            self.write_failed += len(batch)
            logger.error("worker %s 批量写库失败（%d 条），整批重投：%s",
                         self.name, len(batch), exc)
            for lease, _, _, _ in batch:
                self._queue.nack(lease, error=f"write_failed:{exc}")
            self._last_flush = time.monotonic()
            return

        for lease, _, _, _ in batch:
            self._queue.ack(lease)
        self.processed += len(batch)
        self._last_flush = time.monotonic()

    # ------------------------------------------------------------------ 复核
    def _review_flag(self, result: LabelResult) -> int:
        """按标注结果决定 ``label_review_flag`` 的取值。

        引擎只写 4/5/6 三档，1/2/3 留给人工复核：

        - **4 AI标注成功**：正常标出结果，置信度达标
        - **6 未人工复核**：标出来了但置信度低于阈值，需要人工看一眼
        - **5 AI标注错误**：不走这里——失败的评论根本不进写库缓冲，
          由 :meth:`_handle_failure` 单独打标（见 repository.mark_failed）

        规则命中的中性（纯表情、景区无关）算标注成功：它们是确定的结论，
        不是"没标出来"，所以给 4 而不是 6。
        """
        flags = self._cfg.storage.review_flags
        threshold = self._cfg.labeling.low_confidence_threshold
        if result.source is LabelSource.LLM and 0 < result.confidence < threshold:
            return int(flags.get("ai_low_confidence", 6))
        return int(flags.get("ai_success", 4))

    def pending_in_buffer(self) -> int:
        return len(self._buffer)


class WorkerPool:
    """消费线程池，负责启动、优雅停机与队列回收。"""

    def __init__(
        self,
        cfg: AppConfig,
        queue: BaseQueue,
        service: LabelingService,
        repository: Optional[LabelRepository],
        failure_store: Optional[BaseFailureStore] = None,
    ) -> None:
        self._cfg = cfg
        self._queue = queue
        self._service = service
        self._repo = repository
        self._failures = failure_store or NullFailureStore()
        self._stop = threading.Event()
        self._workers: List[LabelingWorker] = []
        self._reclaimer: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._workers:
            raise RuntimeError("WorkerPool 已经启动")
        for idx in range(self._cfg.worker.concurrency):
            worker = LabelingWorker(
                name=f"labeler-{idx + 1}",
                cfg=self._cfg,
                queue=self._queue,
                service=self._service,
                repository=self._repo,
                stop_event=self._stop,
                failure_store=self._failures,
            )
            worker.start()
            self._workers.append(worker)

        self._reclaimer = threading.Thread(
            target=self._reclaim_loop, name="reclaimer", daemon=True
        )
        self._reclaimer.start()
        logger.info("消费线程池启动，并发 %d", len(self._workers))

    def stop(self, timeout: Optional[float] = None) -> None:
        """优雅停机：置停止位，等 worker 把缓冲 flush 完。"""
        if not self._workers:
            return
        logger.info("消费线程池停机中……")
        self._stop.set()
        wait = timeout if timeout is not None else self._cfg.worker.shutdown_timeout
        deadline = time.monotonic() + wait
        for worker in self._workers:
            remaining = max(0.1, deadline - time.monotonic())
            worker.join(timeout=remaining)
        alive = [w.name for w in self._workers if w.is_alive()]
        if alive:
            logger.warning("以下 worker 未在 %.0fs 内退出：%s（其缓冲中的任务会被"
                           "可见性超时回收重投，不会丢失）", wait, alive)
        self._workers.clear()

    def _reclaim_loop(self) -> None:
        """周期性回收超时未 ack 的任务。"""
        interval = max(5, self._cfg.queue.redis.reclaim_interval)
        while not self._stop.wait(interval):
            try:
                recovered = self._queue.reclaim()
                if recovered:
                    logger.warning("回收超时任务 %d 条", recovered)
                self._failures.prune()
            except Exception:                       # noqa: BLE001
                logger.exception("回收任务时异常")

    # ------------------------------------------------------------------ 观测
    def processed(self) -> int:
        return sum(w.processed for w in self._workers)

    def label_failed(self) -> int:
        return sum(w.label_failed for w in self._workers)

    def buffered(self) -> int:
        return sum(w.pending_in_buffer() for w in self._workers)

    def is_idle(self) -> bool:
        """队列空、无 in-flight、无缓冲，即认为空闲。"""
        stats = self._queue.stats()
        return stats["pending"] == 0 and stats["inflight"] == 0 and self.buffered() == 0
