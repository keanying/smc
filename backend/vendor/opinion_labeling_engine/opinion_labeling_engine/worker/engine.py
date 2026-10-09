"""引擎门面：对外唯一入口。

调用方（采集侧）只需要三步：

    engine = LabelingEngine.from_config()      # 读 config.yaml，装配所有组件
    engine.start()                             # 拉起消费线程池
    engine.submit(row)                         # 一边采集一边推，随采随标

``row`` 就是需求里给的那一行原始数据（不含五个标注字段），
dict / CommentRecord 都可以，缺字段不影响。

进程退出前调用 ``engine.stop()``（或用 with 语句），会把在途任务处理完再退出。
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from ..config import AppConfig, load_config
from ..domain.models import CommentRecord, LabelResult
from ..domain.taxonomy import Taxonomy
from ..labeling.service import LabelingFailed, LabelingService
from ..logging_conf import get_logger, setup_logging
from ..queues import BaseQueue, create_queue
from ..storage.failure_store import (BaseFailureStore, FailureRecord,
                                     create_failure_store)
from ..storage.mysql import MySQLPool
from ..storage.repository import LabelRepository
from .consumer import WorkerPool

__all__ = ["LabelingEngine"]

logger = get_logger(__name__)

RecordLike = Union[CommentRecord, Dict[str, Any]]


class LabelingEngine:
    """舆情标注引擎。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        queue: Optional[BaseQueue] = None,
        service: Optional[LabelingService] = None,
        repository: Optional[LabelRepository] = None,
        failure_store: Optional[BaseFailureStore] = None,
        enable_db: bool = True,
    ) -> None:
        self.cfg = cfg
        self._pool: Optional[MySQLPool] = None

        self.taxonomy: Taxonomy = (service.taxonomy if service
                                   else Taxonomy.load(cfg.taxonomy_path))
        self.service = service or LabelingService(cfg, taxonomy=self.taxonomy)
        self.queue = queue or create_queue(cfg.queue)

        if repository is not None:
            self.repository: Optional[LabelRepository] = repository
        elif enable_db:
            self._pool = MySQLPool(cfg.mysql)
            self.repository = LabelRepository(self._pool, cfg.storage)
        else:
            self.repository = None

        # 失败池独立于队列后端：队列可以是 memory，失败记录也必须跨重启存活
        self.failure_store: BaseFailureStore = (
            failure_store if failure_store is not None
            else create_failure_store(cfg.failure_store)
        )
        self._pool_runner = WorkerPool(cfg, self.queue, self.service,
                                       self.repository, self.failure_store)
        self._started = False
        self._lock = threading.Lock()
        self.submitted = 0
        self.dropped = 0

    # ================================================================== 构造
    @classmethod
    def from_config(
        cls,
        path: Optional[str] = None,
        *,
        enable_db: bool = True,
        setup_log: bool = True,
    ) -> "LabelingEngine":
        """从 config.yaml 装配引擎。

        Args:
            path: 配置文件路径，默认 ``config/config.yaml``。
            enable_db: False 时不连库（干跑模式，结果只打日志）。
            setup_log: 是否初始化日志（作为库被嵌入时可置 False）。
        """
        cfg = load_config(path, require_db=enable_db)
        if setup_log:
            setup_logging(cfg.logging)
        return cls(cfg, enable_db=enable_db)

    # ================================================================== 生命周期
    def start(self, *, check_schema: bool = True) -> "LabelingEngine":
        """启动消费线程池，并做启动期自检。

        Raises:
            RuntimeError: 库连不上或表结构不匹配——这类问题必须在启动期暴露。
        """
        with self._lock:
            if self._started:
                return self
            if self.repository is not None:
                assert self._pool is not None or True
                if self._pool is not None and not self._pool.ping():
                    raise RuntimeError("MySQL 连接失败，引擎拒绝启动")
                if check_schema:
                    self.repository.ensure_schema()
            self._pool_runner.start()
            self._started = True
        pending_failures = self.failure_store.count()
        logger.info(
            "%s 启动完成 | env=%s | 队列=%s | 失败池=%s(%d 条待重标) | 并发=%d | "
            "模型=%s | 维度=%d 条",
            self.cfg.name, self.cfg.env, self.cfg.queue.backend,
            self.cfg.failure_store.backend, pending_failures,
            self.cfg.worker.concurrency, self.cfg.llm.model, len(self.taxonomy.nodes),
        )
        if pending_failures:
            logger.warning("失败池里还有 %d 条没标成的评论，跑 retry-failed 可以重标",
                           pending_failures)
        return self

    def stop(self, timeout: Optional[float] = None) -> None:
        """停机：等在途任务处理完，再关闭连接。"""
        with self._lock:
            if not self._started:
                return
            self._pool_runner.stop(timeout=timeout)
            self._started = False
        self.service.close()
        self.queue.close()
        self.failure_store.close()
        if self._pool is not None:
            self._pool.close()
        logger.info("引擎已停止 | %s", self.stats())

    def __enter__(self) -> "LabelingEngine":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ================================================================== 入队
    def submit(self, row: RecordLike) -> bool:
        """提交一条评论进入标注队列（非阻塞，立即返回）。

        Args:
            row: 原始评论行，dict 或 :class:`CommentRecord`。

        Returns:
            是否成功入队。队列满且策略为 drop 时返回 False。

        Raises:
            ValueError: 缺少 ``comment_id``——没有主键就无法写回，属于调用方错误。
        """
        record = self._to_record(row)
        ok = self.queue.submit(record)
        if ok:
            self.submitted += 1
        else:
            self.dropped += 1
        return ok

    # ------------------------------------------------------------------ 入参校验
    def _to_record(self, row: RecordLike) -> CommentRecord:
        """归一成 :class:`CommentRecord` 并按入参契约校验。

        两档处理，区别在于「缺了这个字段还能不能正确写回」：

        - **写回主键**（``storage.key_columns``：channel + scenic_id + comment_id）
          任一为空就直接拒绝。这类问题如果放过，表现出来只是写库时
          「UPDATE 影响 0 行」的一条 WARNING，排查起来要翻半天日志；
          在入口拒绝能直接告诉调用方缺了哪个字段。
        - **其余约定字段**（work_id / scenic_name / content）缺失只告警。
          scenic_name 缺了会拉低"是否与景区相关"的判定准确率，
          work_id 缺了只影响后续下钻定位，content 为空会被清洗判为无效内容走中性——
          三者都不妨碍写回，所以不该拦。

        Raises:
            ValueError: 缺少 ``comment_id``，或任一写回主键为空。
        """
        record = row if isinstance(row, CommentRecord) else CommentRecord.from_dict(row)

        def _blank(field: str) -> bool:
            return not str(getattr(record, field, "") or "").strip()

        keys = self.cfg.storage.key_columns
        missing_keys = [col for col in keys if _blank(col)]
        if missing_keys:
            raise ValueError(
                f"评论 {record.comment_id!r} 缺少写回主键字段 {missing_keys}，"
                f"无法定位 {self.cfg.storage.table} 中的目标行"
            )

        soft = [f for f in self.cfg.storage.required_input_fields
                if f not in keys and _blank(f)]
        if soft:
            logger.warning("评论 %s 未提供约定字段 %s（不影响写回，但会影响准确率或可追溯性）",
                           record.comment_id, soft)
        return record

    def submit_many(self, rows: Iterable[RecordLike]) -> int:
        """批量提交，返回成功入队条数。

        单条格式错误只跳过该条并记日志，不中断整批——采集侧不该因为
        一条脏数据就整批失败。
        """
        accepted = 0
        for row in rows:
            try:
                if self.submit(row):
                    accepted += 1
            except ValueError as exc:
                self.dropped += 1
                logger.error("跳过一条非法输入：%s", exc)
        return accepted

    # ================================================================== 同步标注
    def label_sync(self, row: RecordLike, *, write: bool = False) -> LabelResult:
        """不走队列，直接同步标注一条。

        用于接口联调、单条重标、以及「先看看效果再上量」的场景。

        Args:
            row: 原始评论行。
            write: 是否立即写回数据库。
        """
        # write=False 时只看标注效果，不需要主键完整；要写库就必须校验
        record = (self._to_record(row) if write
                  else (row if isinstance(row, CommentRecord)
                        else CommentRecord.from_dict(row)))
        result = self.service.label(record)     # 失败会抛 LabelingFailed，不会写库
        if write and self.repository is not None:
            self.repository.update_one(record, result)
        return result

    # ================================================================== 重标
    def retry_failed(
        self,
        *,
        limit: int = 0,
        min_age_seconds: Optional[float] = None,
        batch_size: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, int]:
        """把失败池里的评论重新入队标注。

        取出即从池中移除；如果这次又失败，worker 会把它重新记回去
        （``attempts`` 继续累加），所以反复跑这个方法是安全的，
        也不会因为并发跑两个重标进程而把同一条领两次。

        Args:
            limit: 本次最多重标多少条，0 = 把池子清空为止。
            min_age_seconds: 只取失败超过这么久的记录，默认取配置值。
                刚失败的先晾一会儿——模型正在抽风时立刻重试只是白烧 token。
            batch_size: 每批取多少条。
            timeout: 每批等待处理完成的超时秒数。

        Returns:
            ``{"taken": 取出条数, "submitted": 入队条数, "remaining": 池中剩余}``
        """
        if not self._started:
            raise RuntimeError("引擎尚未 start()，无法重标")

        cfg = self.cfg.failure_store
        age = cfg.retry_min_age_seconds if min_age_seconds is None else min_age_seconds
        size = batch_size or cfg.retry_batch_size
        taken = submitted = 0
        run_started = time.time()

        while True:
            want = size if not limit else min(size, limit - taken)
            if want <= 0:
                break

            # 只取"本轮开始之前就已经在池子里"的记录。
            # 这一轮重标又失败的会被 worker 重新写回池子，如果不设这条线，
            # 它们立刻又满足取数条件，同一轮里被反复捞出来重标 —— 死循环。
            # 让门槛随耗时一起走，deadline 就永远钉在 run_started 上。
            effective_age = max(age, time.time() - run_started)
            batch: List[FailureRecord] = self.failure_store.take(
                want, min_age_seconds=effective_age)
            if not batch:
                break

            taken += len(batch)
            # 把已有的累计失败次数带上，这轮再失败时才能继续往上累加
            rows = []
            for item in batch:
                row = dict(item.record)
                row["retry_count"] = item.attempts
                rows.append(row)
            submitted += self.submit_many(rows)
            logger.info("重标取出 %d 条（累计 %d），等待处理……", len(batch), taken)
            if not self.wait_idle(timeout=timeout or max(120.0, len(batch) * 5.0)):
                logger.warning("等待重标批次完成超时，停止继续取数")
                break

        remaining = self.failure_store.count()
        logger.info("重标结束：取出 %d 条，入队 %d 条，池中剩余 %d 条",
                    taken, submitted, remaining)
        return {"taken": taken, "submitted": submitted, "remaining": remaining}

    # ================================================================== 等待
    def wait_idle(self, timeout: Optional[float] = None, *, poll: float = 0.5) -> bool:
        """阻塞直到队列排空（用于跑批场景 / 单测）。

        Returns:
            True 表示已排空；False 表示超时仍有积压。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._pool_runner.is_idle():
                return True
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(poll)

    def run_forever(self) -> None:
        """常驻模式：装 SIGINT/SIGTERM 优雅停机，然后阻塞。"""
        stop_event = threading.Event()

        def _handler(signum, _frame):
            logger.info("收到信号 %s，准备停机", signum)
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):    # 非主线程时装不了，忽略
                pass

        last_report = time.monotonic()
        while not stop_event.wait(1.0):
            if time.monotonic() - last_report >= 60:
                logger.info("运行状态 | %s", self.stats())
                last_report = time.monotonic()
        self.stop()

    # ================================================================== 观测
    def stats(self) -> Dict[str, Any]:
        """汇总运行指标，供日志与监控使用。"""
        data: Dict[str, Any] = {
            "submitted": self.submitted,
            "dropped_on_submit": self.dropped,
            "processed": self._pool_runner.processed(),
            "buffered": self._pool_runner.buffered(),
        }
        data.update(self.queue.stats())
        data.update(self.service.stats.snapshot())
        data["failure_pool"] = self.failure_store.count()
        return data
