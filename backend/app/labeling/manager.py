"""标注引擎的接入管理器。

职责边界（很重要）
------------------
引擎自己已经把「标注」这件事做完了：清洗、调模型、后处理、写回、重试、死信。
这一层**不重复实现任何一样**，只负责：

1. 用 smc 的配置把它装配起来（config_bridge）
2. 把它的**线程模型**桥到 asyncio —— 会阻塞的调用一律走 to_thread
3. 开关与自检：没开就什么都不做；开了但配置不对，**明确拒绝**而不是偷偷不标
4. 把「边采边标」「补历史」「单条重标」暴露给上层

⚠️ 线程/事件循环那条线是这里最容易出事的地方。
   `LabelingEngine.start()` 会连库、建连接池、起 worker 线程，
   `label_sync()` 会同步等模型返回（几秒到几十秒），
   `repository.fetch_unlabeled()` 是同步 PyMySQL 查询。
   这些**全部**必须 `asyncio.to_thread`，否则整个 FastAPI 卡死——
   现象是"页面全部转圈"，而日志一切正常，极难查。
   只有 `submit()` 是纯入队、非阻塞，可以直接调。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.config import Config
from ..core.logging import get_logger
from . import config_bridge

logger = get_logger(__name__)


@dataclass
class CheckItem:
    """自检的一项。ok=False 时 detail 要说清楚**怎么修**，不是只报个错。"""
    name: str
    ok: bool
    detail: str = ""


@dataclass
class BackfillJob:
    """一次补标历史的进度。

    ⚠️ 这些数字要经得起"页面刷新之后还对得上"：
    进度不是把提交条数当完成数，而是**回库数**——
    用「开跑时还没标的总数」减去「现在还没标的数」。
    这样即使中间有失败、有人手动标了几条，看到的也是真实进度。
    """
    job_id: str
    scenic_id: str = ""
    channel: str = ""
    total: int = 0                 # 开跑时符合条件的未标注总数
    submitted: int = 0             # 累计喂进队列的条数
    done: int = 0                  # 已经标完写回库的条数（回库数，不是提交数）
    batches: int = 0               # 已经喂了几批
    queued: int = 0                # 当前队列里还压着多少
    status: str = "running"        # running / finished / failed / canceled
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    updated_at: float = field(default_factory=time.time)
    #: 取消标志。一键补标动辄十几分钟、每条都花钱，
    #: 没有"停"这个动作的话，点错了只能重启服务。
    cancel_requested: bool = False
    #: 被服务关闭 / 配置变更打断（不是人点的取消）：断点保留，重启后自动接着补
    interrupted: bool = False

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 100.0 if self.status != "running" else 0.0
        return min(100.0, round(self.done * 100.0 / self.total, 1))

    def to_dict(self) -> Dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        # 剩余时间按已完成的平均速度估。刚开始没数据就不猜——
        # 一个乱跳的"预计剩余 3 秒"比不显示更让人不信任。
        eta = None
        if self.status == "running" and self.done > 0 and elapsed > 0:
            speed = self.done / elapsed
            if speed > 0:
                eta = int(max(0, (self.total - self.done)) / speed)
        return {
            "job_id": self.job_id, "scenic_id": self.scenic_id,
            "channel": self.channel, "total": self.total,
            "submitted": self.submitted, "done": self.done,
            "batches": self.batches, "queued": self.queued,
            "percent": self.percent,
            "status": self.status, "error": self.error,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": eta,
            "updated_at": self.updated_at,
            "cancel_requested": self.cancel_requested,
        }


class LabelingManager:
    """标注引擎在 smc 里的门面。"""

    def __init__(self, config: Config):
        self.config = config
        self._engine = None                 # LabelingEngine，懒启动
        self._lock = asyncio.Lock()
        self._start_error = ""
        self._jobs: Dict[str, BackfillJob] = {}
        self._job_seq = 0
        #: 边采边标在运行期被判定为"不可用"之后就不再反复尝试。
        #: 否则每条评论都去连一次连不上的库，日志刷屏、采集还被拖慢。
        self._degraded = False

    # ------------------------------------------------------------------ 开关
    @property
    def enabled(self) -> bool:
        """总开关：系统设置里的「边采边标」。"""
        return bool((self.config.get("labeling") or {}).get("enabled", False))

    @property
    def available(self) -> bool:
        """引擎当前能不能用（已启动且没降级）。"""
        return self._engine is not None and not self._degraded

    # ------------------------------------------------------------------ 自检
    async def check(self) -> List[CheckItem]:
        """逐项自检：配置 / 标签体系 / 模型配置 / Redis / MySQL / 表结构。

        ⚠️ **开启边采边标之前必须过这一关**。
        用户的要求原话："开启了要检测模型配置正确，数据库联调这些，
        存在异常不标注"。所以这不是个可选的诊断按钮，
        而是 `ensure_started()` 的前置条件。
        """
        return await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> List[CheckItem]:
        items: List[CheckItem] = []

        if not config_bridge.engine_importable():
            items.append(CheckItem(
                "标注引擎", False,
                f"没找到引擎：{config_bridge.ENGINE_ROOT}。"
                f"确认 backend/vendor/opinion_labeling_engine 完整"))
            return items
        items.append(CheckItem("标注引擎", True, str(config_bridge.ENGINE_ROOT)))

        config_bridge.ensure_on_path()
        try:
            path = config_bridge.write_runtime_config(self.config)
            items.append(CheckItem("运行时配置", True, str(path)))
        except Exception as exc:            # noqa: BLE001
            items.append(CheckItem("运行时配置", False, f"生成失败：{exc}"))
            return items

        # ---- 模型配置：Key 和模型名是最常见的两个坑，单独各报一条 ----
        ark = (self.config.get("labeling.ark") or {})
        key = str(ark.get("api_key") or "").strip()
        model = str(ark.get("model") or "").strip()
        if not key:
            items.append(CheckItem("模型 API Key", False,
                                   "没配。在系统设置的「标注」里填，"
                                   "或设环境变量 SMC_LABELING__ARK__API_KEY"))
        elif len(key) < 20:
            items.append(CheckItem("模型 API Key", False,
                                   f"看着像被截断了（只有 {len(key)} 位）。"
                                   f"方舟 Key 通常 36 位"))
        else:
            items.append(CheckItem("模型 API Key", True, f"已配置（{key[:6]}…{key[-4:]}）"))
        items.append(CheckItem("模型名称", bool(model), model or "没配 labeling.ark.model"))

        try:
            from opinion_labeling_engine.config import load_config
            from opinion_labeling_engine.domain.taxonomy import Taxonomy

            cfg = load_config(str(path))
            items.append(CheckItem("配置校验", True,
                                   f"模型 {cfg.llm.model} @ {cfg.llm.url}"))
        except Exception as exc:            # noqa: BLE001
            items.append(CheckItem("配置校验", False, str(exc)))
            return items

        # ---- 模型连通：真打一次。Key/模型名"填了"不等于"能用" ----
        ok, detail = probe_model(cfg.llm)
        items.append(CheckItem("模型连通", ok, detail))

        try:
            tax = Taxonomy.load(cfg.taxonomy_path)
            items.append(CheckItem(
                "标签体系", True,
                f"v{tax.version}，维度路径 {len(tax.nodes)} 条，"
                f"实体类型 {len(tax.entity_types)} 个"))
        except Exception as exc:            # noqa: BLE001
            items.append(CheckItem("标签体系", False, str(exc)))

        if cfg.queue.backend == "redis":
            try:
                from opinion_labeling_engine.queues.redis_queue import RedisQueue

                q = RedisQueue(cfg.queue)
                if q.ping():
                    items.append(CheckItem(
                        "队列 Redis", True,
                        f"{cfg.queue.redis.host}:{cfg.queue.redis.port}"
                        f"（积压 {q.size()}，在途 {q.inflight()}，死信 {q.dead_size()}）"))
                else:
                    items.append(CheckItem("队列 Redis", False, "连接失败"))
            except Exception as exc:        # noqa: BLE001
                items.append(CheckItem("队列 Redis", False, str(exc)))
        else:
            items.append(CheckItem(
                "队列", True,
                "memory（进程内。重启会丢掉还没标完的任务；"
                "量大或多进程部署请在设置里切 redis）"))

        # ---- 失败池：start() 真正会去连的东西，自检必须覆盖 ----
        # ⚠️ 这一项是补上来的：之前自检 9 项全绿，ensure_started() 却直接
        #    抛「失败池连不上 Redis」。因为失败池和任务队列是**两个**组件，
        #    队列切成 memory 不代表失败池也是 memory——引擎在
        #    create_failure_store 里对 redis 后端会**启动期就炸**
        #    （storage/failure_store.py：跑到一半才发现失败记录全丢了）。
        #    自检绿灯 → 启动报错，是最难看的一种：用户会以为是偶发。
        try:
            from opinion_labeling_engine.storage.failure_store import create_failure_store

            fs_cfg = cfg.failure_store
            if not fs_cfg.enabled or fs_cfg.backend == "none":
                items.append(CheckItem("失败池", True, "已关闭（标注失败只写 label_review_flag=5）"))
            elif fs_cfg.backend == "redis":
                store = create_failure_store(fs_cfg)
                try:
                    # count()，不是 size()——失败池的接口是 record/take/peek/count。
                    # 顺带一提：条数只是给人看的，取不到不该让整项自检变红。
                    pending = store.count()
                    detail = f"{fs_cfg.redis.host}:{fs_cfg.redis.port}（待重标 {pending} 条）"
                except Exception:  # noqa: BLE001
                    detail = f"{fs_cfg.redis.host}:{fs_cfg.redis.port}（连通）"
                finally:
                    try:
                        store.close()
                    except Exception:  # noqa: BLE001
                        pass
                items.append(CheckItem("失败池 Redis", True, detail))
            else:
                items.append(CheckItem("失败池", True, f"{fs_cfg.backend}（进程内，重启会丢）"))
        except Exception as exc:            # noqa: BLE001
            items.append(CheckItem("失败池", False, str(exc)))

        # ---- 数据库联调：连得上 + 表结构对得上，两件事分开报 ----
        try:
            from opinion_labeling_engine.storage.mysql import MySQLPool
            from opinion_labeling_engine.storage.repository import LabelRepository

            pool = MySQLPool(cfg.mysql)
            try:
                if not pool.ping():
                    items.append(CheckItem("数据库连接", False,
                                           f"{cfg.mysql.host}:{cfg.mysql.port} 连不上"))
                else:
                    items.append(CheckItem(
                        "数据库连接", True,
                        f"{cfg.mysql.host}:{cfg.mysql.port}/{cfg.mysql.database}"))
                    repo = LabelRepository(pool, cfg.storage)
                    repo.ensure_schema()
                    items.append(CheckItem("写回表结构", True, cfg.storage.table))
            finally:
                pool.close()
        except Exception as exc:            # noqa: BLE001
            items.append(CheckItem("数据库/表结构", False, str(exc)))

        return items

    # ------------------------------------------------------------------ 生命周期
    async def ensure_started(self) -> bool:
        """需要用引擎时才启动它，**自检不过就不启动**。

        返回 True 表示可用。失败原因留在 `self._start_error`，
        由 API 原样透出去——用户要能看到"为什么没标"。
        """
        if self._engine is not None:
            return True
        async with self._lock:
            if self._engine is not None:
                return True
            checks = await self.check()
            bad = [c for c in checks if not c.ok]
            if bad:
                self._start_error = "；".join(f"{c.name}：{c.detail}" for c in bad)
                logger.warning("[标注] 自检没过，不启动引擎：%s", self._start_error)
                return False
            try:
                self._engine = await asyncio.to_thread(self._start_sync)
                self._start_error = ""
                self._degraded = False
                logger.info("[标注] 引擎已启动")
                return True
            except Exception as exc:        # noqa: BLE001
                self._start_error = str(exc)
                logger.error("[标注] 引擎启动失败：%s", exc)
                return False

    def _start_sync(self):
        config_bridge.ensure_on_path()
        from opinion_labeling_engine import LabelingEngine

        path = config_bridge.write_runtime_config(self.config)
        # setup_log=False：引擎作为**库**嵌进来，日志配置由 smc 统一管，
        # 让它自己 setup 会把 smc 的 root logger handler 顶掉
        engine = LabelingEngine.from_config(str(path), enable_db=True, setup_log=False)
        engine.start(check_schema=True)
        return engine

    async def stop(self) -> None:
        # ⚠️ 先叫停正在跑的补标，再抽走引擎。以前顺序反了：服务关闭时
        #    补标任务撞上 self._engine=None，日志里一句
        #    「补标任务 xxx 失败：'NoneType' object has no attribute 'wait_idle'」。
        #    这不是失败，是被打断——断点留着，重启后 resume_pending 会接着补。
        self._interrupt_backfills("服务关闭或配置变更，补标中止；重启/恢复后会自动接着补")
        engine, self._engine = self._engine, None
        if engine is None:
            return
        try:
            await asyncio.to_thread(engine.stop, 15.0)
            logger.info("[标注] 引擎已停止")
        except Exception as exc:            # noqa: BLE001
            logger.warning("[标注] 引擎停止时出错（忽略）：%s", exc)

    async def reset(self) -> None:
        """系统设置里改了「AI 标注」之后调用：停掉旧引擎、清掉降级标记。

        ⚠️ 不调这个，页面上改模型名称 / API Key 是**不生效**的：
        引擎启动时就把 Key 和模型名烤进了 HTTP 客户端，之后只认那一份。
        真实发生过：方舟上旧模型被关了（InvalidEndpoint.ClosedEndpoint），
        用户在页面上换了模型、提示"已保存并立即生效"，引擎却还在打旧模型，
        每条评论都进失败池，直到重启服务。

        下一次 submit_comments / 补标会按新配置重新自检、重新启动。
        """
        async with self._lock:
            await self.stop()
            self._degraded = False
            self._start_error = ""
        logger.info("[标注] 配置已变更，引擎将按新配置重启")
        # 队列里压着的不能等"下一次采集"才动：按新配置立刻拉起来接着标
        if self.enabled:
            asyncio.create_task(self._resume_quietly("配置变更"))

    # ------------------------------------------------------------------ 重启续标
    #: 内存队列重启即丢，按库里补回"最近这么久采到、从没标过"的评论。
    #: 更早的算历史数据，交给「一键补标」——别让一次重启悄悄花掉一大笔模型费
    RESUME_WINDOW_HOURS = 24
    RESUME_MAX_ROWS = 5000

    async def resume_pending(self) -> str:
        """服务启动 / 改完 AI 标注配置后调用：队列里还有没标完的，立刻接着标。

        ⚠️ 以前引擎是"懒启动"的——只有新的采集推数据进来才会拉起来。
        于是重启之后 Redis 队列里剩下的评论**一直躺着**，直到下一次有采集任务；
        没有定时任务的话就永远不标。

          - redis 队列：内容和在途租约都在 Redis 里（在途的超时会被引擎回收），
            把引擎拉起来，worker 自己就会接着消费，**不用再补投**（补了会重复标、重复花钱）
          - memory 队列：重启就没了。按库里「最近 RESUME_WINDOW_HOURS 小时采到、
            标注状态还是 0（从没处理过）」的评论补投回去
        失败过的（5）不在这里重试：它们在失败池里，走「重标」那条路。
        """
        if not self.enabled:
            return ""
        if not await self.ensure_started():
            msg = f"边采边标已开启，但引擎没能启动，队列暂不处理：{self._start_error}"
            logger.warning("[标注] %s", msg)
            return msg
        resumed_backfill = await self._resume_backfill()
        backend = str(getattr(self._engine.cfg.queue, "backend", "memory"))
        if backend == "redis":
            backlog = self._queue_backlog()
            msg = (f"Redis 队列里还有 {backlog} 条待标注，已接着处理" if backlog
                   else "队列为空，引擎已就绪")
            msg += resumed_backfill
            logger.info("[标注] %s", msg)
            return msg
        if resumed_backfill:
            # 补标捞的是全部未标注，最近没标过的也在里面，不用再按窗口补投一遍
            logger.info("[标注] %s", resumed_backfill.strip("；"))
            return resumed_backfill.strip("；")
        rows = await asyncio.to_thread(self._fetch_recent_pending)
        if not rows:
            return "队列为空，引擎已就绪"
        submitted = await asyncio.to_thread(self._engine.submit_many, rows)
        msg = (f"内存队列重启后已清空，按库补回最近 {self.RESUME_WINDOW_HOURS} 小时"
               f"没标过的 {submitted} 条重新入队")
        logger.info("[标注] %s", msg)
        return msg

    async def _resume_backfill(self) -> str:
        """上次没补完的一键补标（服务关闭/配置变更打断的），接着补。"""
        marker = self._load_backfill_marker()
        if not marker:
            return ""
        if any(j.status == "running" for j in self._jobs.values()):
            return ""
        job = await self.start_backfill(
            scenic_id=str(marker.get("scenic_id") or ""),
            channel=str(marker.get("channel") or ""),
            limit=int(marker.get("limit") or 0))
        scope = "、".join(x for x in (marker.get("scenic_id"), marker.get("channel")) if x) or "全部"
        return f"；上次没补完的一键补标（{scope}）已接着补（{job.job_id}）"

    async def _resume_quietly(self, why: str) -> None:
        try:
            await self.resume_pending()
        except Exception as exc:            # noqa: BLE001
            # 续标是锦上添花，出错不能把启动/保存设置带崩
            logger.warning("[标注] %s后续标失败（不影响其它功能）：%s", why, exc)

    def _fetch_recent_pending(self) -> List[Dict[str, Any]]:
        return self._engine.repository.fetch_unlabeled(
            limit=self.RESUME_MAX_ROWS,
            extra_where=self._recent_pending_where(self._engine.cfg.storage))

    def _recent_pending_where(self, storage: Any) -> str:
        """「最近采到、从没处理过」的条件，拼给引擎 fetch_unlabeled 的 extra_where。"""
        since = datetime.now() - timedelta(hours=self.RESUME_WINDOW_HOURS)
        conds = [f"`crawl_time` >= {_quote(since.strftime('%Y-%m-%d %H:%M:%S'))}"]
        review_col = str(getattr(storage, "review_column", "") or "")
        if review_col:
            pending = [int(f) for f in (getattr(storage, "pending_flags", None) or [0])]
            flags = ", ".join(str(f) for f in pending)
            null_ok = f"`{review_col}` IS NULL OR " if 0 in pending else ""
            conds.append(f"({null_ok}`{review_col}` IN ({flags}))")
        return " AND ".join(conds)

    # ------------------------------------------------------------------ 边采边标
    async def submit_comments(self, rows: List[Dict[str, Any]]) -> int:
        """采集侧推数据。**绝不能把采集带崩**。

        任何异常都吞掉并降级：标注失败最多是这批评论没标签，
        而让采集因为标注挂掉，丢的是数据本身。
        """
        if not rows or not self.enabled or self._degraded:
            return 0
        if not await self.ensure_started():
            self._degraded = True
            logger.warning("[标注] 边采边标已开启但引擎不可用，本次运行不再尝试：%s",
                           self._start_error)
            return 0
        try:
            # submit_many 只入队，不阻塞；引擎自己保证非法行只跳过不中断
            return await asyncio.to_thread(self._engine.submit_many, rows)
        except Exception as exc:            # noqa: BLE001
            self._degraded = True
            logger.warning("[标注] 入队失败，本次运行不再尝试：%s", exc)
            return 0

    # ------------------------------------------------------------------ 单条重标
    async def relabel_one(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """页面上点「AI 再标注」：同步跑一条并写库，把结果返回给前端。

        用 label_sync 而不是 submit：用户点了要立刻看到结果，
        丢进队列就变成"点了没反应"。
        """
        if not await self.ensure_started():
            raise RuntimeError(self._start_error or "标注引擎不可用")
        return await asyncio.to_thread(self._relabel_sync, row)

    def _relabel_sync(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """同步跑一条并写回，**连 label_review_flag 一起写**。

        ⚠️ 这里不能直接用 `label_sync(row, write=True)`。
        它内部调的是 `repository.update_one(record, result)`——
        review_flag 用的是默认值 **0**，也就是「未标注」。
        结果就是：页面上点了「AI 再标注」，五个标注字段都更新了，
        复核标记却退回 0，这条评论在审核页上又变成"没标过"，
        点多少次都一样。实跑验出来的就是这个（flag 期望 4，实得 0）。

        队列那条路没这个问题——consumer 会先算好 flag 再写
        （worker/consumer.py 的 `_review_flag`）。所以这里
        **复用引擎自己的那份规则**，而不是在这边重写一遍阈值判断：
        规则以后在引擎里改了，这里跟着变，不会两边漂。
        """
        record = self._engine._to_record(row)
        result = self._engine.service.label(record)
        flag = self._review_flag_of(result)
        repo = getattr(self._engine, "repository", None)
        if repo is not None:
            repo.update_one(record, result, review_flag=flag)
        payload = _result_to_dict(result)
        payload["label_review_flag"] = flag
        return payload

    def _review_flag_of(self, result: Any) -> int:
        """这条标注结果该写哪一档复核标记（4 成功 / 6 置信度低）。

        直接借引擎 worker 的 `_review_flag`：它只用到 `self._cfg`，
        拿个最小壳对象绑上去调就行，不用真的起一个消费线程。
        这样阈值规则以后在引擎里改了，这边跟着变，不会两边漂。

        ⚠️ 借别人的私有方法是有代价的：类名或方法名一改，这里就静悄悄
        退回兜底值。**兜底必须喊出来**——第一版把类名写成了
        `LabelConsumer`（真名是 `LabelingWorker`），ImportError 被
        except 吞掉，低置信度的评论全被写成 4，接口一切正常。
        是单测里那条低置信度用例抓出来的，不是跑出来的。
        """
        fallback = 4
        try:
            fallback = int(self._engine.cfg.storage.review_flags.get("ai_success", 4))
        except Exception:  # noqa: BLE001
            pass
        try:
            from opinion_labeling_engine.worker.consumer import LabelingWorker

            shell = LabelingWorker.__new__(LabelingWorker)
            shell._cfg = self._engine.cfg
            return int(LabelingWorker._review_flag(shell, result))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[标注] 借不到引擎的复核标记规则（%s: %s），本条按 %d 写。"
                "引擎的 worker/consumer.py 改过名字的话，"
                "app/labeling/manager.py 的 _review_flag_of 要跟着改",
                type(exc).__name__, exc, fallback)
            return fallback

    # ------------------------------------------------------------------ 补历史
    def list_jobs(self) -> List[Dict[str, Any]]:
        return [j.to_dict() for j in
                sorted(self._jobs.values(), key=lambda x: -x.started_at)]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self._jobs.get(job_id)
        return job.to_dict() if job else None

    async def start_backfill(self, *, scenic_id: str = "", channel: str = "",
                             limit: int = 0) -> BackfillJob:
        """一键补标：把历史上没标过的评论**一直**捞出来跑，直到捞完。

        limit=0 表示不设上限（默认）。以前是"捞一批 limit 条就收工"，
        库里剩几万条也只标这一批，用户得反复点——所以改成持续投喂。

        在后台任务里跑，接口立刻返回 job_id，前端轮询进度。
        """
        if not await self.ensure_started():
            raise RuntimeError(self._start_error or "标注引擎不可用")

        running = [j for j in self._jobs.values() if j.status == "running"]
        if running:
            # 两个补标任务同时跑会互相抢同一批未标注的行（都从 LIMIT 头部捞），
            # 结果是重复调用模型、重复花钱。直接拦住，并告诉用户是哪个在跑。
            raise RuntimeError(f"已经有一个补标任务在跑了（{running[0].job_id}），"
                               f"等它跑完或先取消")

        self._job_seq += 1
        job = BackfillJob(job_id=f"bf{int(time.time())}-{self._job_seq}",
                          scenic_id=scenic_id, channel=channel)
        self._jobs[job.job_id] = job
        # 断点落盘：服务中途重启的话，resume_pending 会照这个接着补
        self._save_backfill_marker(job, limit)
        asyncio.create_task(self._run_backfill(job, limit))
        return job

    def _interrupt_backfills(self, reason: str) -> None:
        for job in self._jobs.values():
            if job.status == "running":
                job.cancel_requested = True
                job.interrupted = True
                job.error = reason
                job.touch()

    # ---- 补标断点：服务重启后接着补 ----
    def _backfill_marker(self) -> Path:
        return config_bridge.runtime_config_path(self.config).parent / "backfill.json"

    def _save_backfill_marker(self, job: BackfillJob, limit: int) -> None:
        try:
            path = self._backfill_marker()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "scenic_id": job.scenic_id, "channel": job.channel, "limit": int(limit or 0),
                "job_id": job.job_id, "started_at": job.started_at,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:            # noqa: BLE001
            logger.warning("[标注] 记录补标断点失败（不影响本次补标，只是重启后不会自动续）：%s", exc)

    def _clear_backfill_marker(self) -> None:
        try:
            self._backfill_marker().unlink(missing_ok=True)
        except Exception as exc:            # noqa: BLE001
            logger.warning("[标注] 清除补标断点失败：%s", exc)

    def _load_backfill_marker(self) -> Optional[Dict[str, Any]]:
        try:
            path = self._backfill_marker()
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:            # noqa: BLE001
            logger.warning("[标注] 补标断点读不了，忽略：%s", exc)
            return None

    def cancel_backfill(self, job_id: str) -> bool:
        """请求停止补标。已经喂进队列的那批还会跑完（不能半路撕掉）。"""
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        job.cancel_requested = True
        job.touch()
        logger.info("[标注] 补标任务 %s 收到取消请求", job_id)
        return True

    #: 每批喂多少条。批小了 drain 的开销占比高，批大了取消不及时、
    #: 进度条也跳得粗。200 条在 4 并发下大约一两分钟一批。
    BACKFILL_BATCH = 200

    async def _run_backfill(self, job: BackfillJob, limit: int) -> None:
        """分批捞、分批喂，直到库里没有未标注的为止。

        ⚠️ 每批之间必须**等队列排干**再捞下一批，这是正确性要求不是优化：
        `fetch_unlabeled` 的条件是「sentiment_label 为空」+ LIMIT，
        而"已提交但还没标完"的行**仍然满足这个条件**。不等排干就接着捞，
        捞回来的还是刚才那批——同一条评论会被反复提交、反复调用模型、
        反复花钱，而进度看着还在涨。
        引擎的 is_idle() 覆盖了 pending / inflight / buffered 三样，
        返回 True 时这批已经落库，条件自然不再命中，下一批就是新的。
        """
        try:
            job.total = await asyncio.to_thread(self._count_unlabeled, job)
            job.touch()
            if job.total <= 0:
                job.status = "finished"
                logger.info("[标注] 补标任务 %s：没有未标注的评论", job.job_id)
                return

            logger.info("[标注] 补标任务 %s 开始，共 %d 条待标注（每批 %d）",
                        job.job_id, job.total, self.BACKFILL_BATCH)

            # ⚠️ 先等队列里**已有**的标完再捞。fetch_unlabeled 只看"有没有标签"，
            #    还排在队列里的（边采边标推进来的、或重启前没跑完的）也满足条件——
            #    不等的话同一条评论会被再提交一次，标两遍、花两遍钱。
            #    等不完也不算失败（比如被杀掉的进程留下的在途租约要等引擎回收），
            #    超时就照常往下走，最多是少数几条重复。
            backlog = self._queue_backlog()
            if backlog:
                logger.info("[标注] 补标任务 %s：队列里还有 %d 条，先等它们标完再捞，免得重复提交",
                            job.job_id, backlog)
                if not await self._drain(job, backlog):
                    logger.warning("[标注] 补标任务 %s：等队列排空超时，照常继续", job.job_id)

            remaining_budget = int(limit) if limit and limit > 0 else 0
            while True:
                if job.cancel_requested:
                    job.status = "canceled"
                    break

                size = self.BACKFILL_BATCH
                if remaining_budget:
                    size = min(size, remaining_budget)
                    if size <= 0:
                        break

                rows = await asyncio.to_thread(self._fetch_unlabeled, job, size)
                if not rows:
                    break                       # 捞完了，正常收工

                submitted = await asyncio.to_thread(self._engine.submit_many, rows)
                job.submitted += submitted
                job.batches += 1
                if remaining_budget:
                    remaining_budget -= submitted
                job.touch()
                logger.info("[标注] 补标任务 %s 第 %d 批入队 %d 条（累计 %d/%d）",
                            job.job_id, job.batches, submitted,
                            job.submitted, job.total)

                # 等这批跑完再捞下一批，中途持续刷新进度给前端看
                ok = await self._drain(job, submitted)
                await self._refresh_progress(job)
                if not ok:
                    job.status = "failed"
                    job.error = (f"第 {job.batches} 批等待超时，队列里还压着 "
                                 f"{job.queued} 条。可能是模型接口卡住了，"
                                 f"看一眼 data/logs/labeling.log")
                    logger.error("[标注] 补标任务 %s %s", job.job_id, job.error)
                    return

            await self._refresh_progress(job)
            if job.status == "running":
                job.status = "finished"
            logger.info("[标注] 补标任务 %s %s：喂了 %d 条，完成 %d/%d",
                        job.job_id,
                        "已取消" if job.status == "canceled" else "结束",
                        job.submitted, job.done, job.total)
        except Exception as exc:            # noqa: BLE001
            if job.interrupted or (job.cancel_requested and self._engine is None):
                # stop()/reset() 抽走了引擎：预期内的中止，别报成失败
                job.status = "canceled"
                logger.info("[标注] 补标任务 %s 被打断（%s）", job.job_id, job.error)
                return
            job.status = "failed"
            job.error = str(exc)
            logger.error("[标注] 补标任务 %s 失败：%s", job.job_id, exc)
        finally:
            job.finished_at = time.time()
            job.touch()
            # 被打断的留着断点等重启续补；跑完、人点取消、真失败的都清掉。
            # 有上限的话断点里记**剩下**的额度：已喂进队列的那些（redis 队列重启不丢）
            # 不能再算一遍，否则续补一次就把上限多用了一截
            if not job.interrupted:
                self._clear_backfill_marker()
            elif limit and limit > 0:
                left = int(limit) - int(job.submitted)
                if left > 0:
                    self._save_backfill_marker(job, left)
                else:
                    self._clear_backfill_marker()

    async def _drain(self, job: BackfillJob, batch_size: int) -> bool:
        """等当前这批标完并落库，期间每秒刷新一次进度。

        超时按"每条 30 秒"给，最少 5 分钟——模型慢的时候一条十几秒是常事，
        给紧了会把正常跑着的任务判成失败。
        """
        deadline = time.time() + max(300.0, batch_size * 30.0)
        while time.time() < deadline:
            idle = await asyncio.to_thread(
                lambda: self._engine.wait_idle(1.0, poll=0.5))
            await self._refresh_progress(job)
            if idle:
                return True
            if job.cancel_requested:
                # 取消时不再等剩下的，但已入队的那批仍会被 worker 跑完
                return True
        return False

    async def _refresh_progress(self, job: BackfillJob) -> None:
        """进度以**库里还剩多少未标注**为准，不是以提交条数为准。

        提交数只能说明"喂进去了"，喂进去之后失败的、被规则判成无效的，
        都不该算完成。用回库数才经得起用户拿 SQL 对。
        """
        try:
            left = await asyncio.to_thread(self._count_unlabeled, job)
            job.done = max(0, job.total - left)
            job.queued = await asyncio.to_thread(self._queue_backlog)
        except Exception as exc:            # noqa: BLE001
            logger.debug("[标注] 刷新补标进度失败（忽略）：%s", exc)
        job.touch()

    def _queue_backlog(self) -> int:
        try:
            queue = self._engine.queue
            return int(queue.size()) + int(queue.inflight())
        except Exception:                   # noqa: BLE001
            return 0

    def _where(self, job: BackfillJob) -> str:
        conds = []
        if job.scenic_id:
            conds.append(f"scenic_id = {_quote(job.scenic_id)}")
        if job.channel:
            conds.append(f"channel = {_quote(job.channel)}")
        return " AND ".join(conds)

    def _count_unlabeled(self, job: BackfillJob) -> int:
        """还有多少条没标。

        ⚠️ 条件必须和引擎的 fetch_unlabeled **完全一致**
        （storage/repository.py：sentiment_label IS NULL OR = ''），
        否则进度条会和实际捞到的行对不上——分母是一套口径、
        分子是另一套，跑到最后卡在 97% 不动，看着像卡死了。
        """
        cfg = self._engine.cfg.storage
        label_col = cfg.columns["sentiment_label"]
        where = f"(`{label_col}` IS NULL OR `{label_col}` = '')"
        extra = self._where(job)
        if extra:
            where += f" AND ({extra})"
        sql = f"SELECT COUNT(*) AS c FROM `{cfg.table}` WHERE {where}"
        with self._engine._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
        if isinstance(row, dict):
            return int(next(iter(row.values())) or 0)
        return int((row or [0])[0] or 0)

    def _fetch_unlabeled(self, job: BackfillJob, limit: int) -> List[Dict[str, Any]]:
        """捞未标注的行。引擎的 fetch_unlabeled 支持 extra_where，
        我们用它按景区/平台收口——补标通常是"先把这个景区补齐"。"""
        return self._engine.repository.fetch_unlabeled(
            limit=limit, extra_where=self._where(job))

    # ------------------------------------------------------------------ 状态
    async def status(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "enabled": self.enabled,
            "running": self._engine is not None,
            "degraded": self._degraded,
            "error": self._start_error,
            "engine_present": config_bridge.engine_importable(),
        }
        if self._engine is not None:
            try:
                data["stats"] = await asyncio.to_thread(self._engine.stats)
            except Exception as exc:        # noqa: BLE001
                data["stats_error"] = str(exc)
        return data


def _quote(value: str) -> str:
    """给 extra_where 用的字面量转义。

    ⚠️ 这里是**拼 SQL**（引擎的 extra_where 接口就是字符串），所以必须转义。
    值来自页面上的下拉框（景区 id / 平台名），但"来源可信"不是不转义的理由——
    下一个改这段代码的人不会知道这个前提。
    """
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _result_to_dict(result: Any) -> Dict[str, Any]:
    """把引擎的 LabelResult 转成前端要的形状。

    用 getattr 逐个取而不是 asdict：引擎是 vendor 的，
    它加字段不该让我们这边报错。
    """
    def _g(name, default=None):
        return getattr(result, name, default)

    def _tags(name):
        """DimensionTag / EntityTag 是 dataclass，得调它自己的 to_dict 才是落库那个形状。"""
        out = []
        for tag in (_g(name, []) or []):
            to_dict = getattr(tag, "to_dict", None)
            out.append(to_dict() if callable(to_dict) else tag)
        return out

    source = _g("source", "")
    return {
        "sentiment_label": _g("sentiment_label", ""),
        "sentiment_score": _g("sentiment_score", 0),
        "dimension_tags": _tags("dimension_tags"),
        "entity_tags": _tags("entity_tags"),
        "keyword_tags": list(_g("keyword_tags", []) or []),
        "confidence": _g("confidence", None),
        "relevant": _g("relevant", True),
        "reason": _g("reason", ""),
        "model": _g("model", ""),
        "latency_ms": _g("latency_ms", 0),
        # source 是枚举（LabelSource），取 .value 才是字符串
        "source": getattr(source, "value", source) or "",
        # dropped 是 dict（按类别分组的被丢弃项），不是 list
        "dropped": _g("dropped", {}) or {},
    }


#: 方舟常见的"配置类"错误码 → 该去哪儿改。这些重试一万次也不会好。
_ARK_HINTS = {
    "InvalidEndpoint.ClosedEndpoint":
        "模型服务在方舟上已关闭/未开通（或这个模型版本已下线）。"
        "到方舟控制台「开通管理」开通，或把「模型名称」换成已开通的模型",
    "InvalidEndpointOrModel.NotFound":
        "模型名称不存在或这个 Key 没有权限，检查「模型名称」拼写",
    "ModelNotOpen": "这个模型还没在方舟控制台开通",
    "AuthenticationError": "API Key 无效，检查「模型 API Key」",
    "AccessDenied": "这个 Key 没有调用该模型的权限",
}


def probe_model(llm: Any, *, transport: Any = None) -> "tuple[bool, str]":
    """用最小的请求真打一次模型接口，确认 Key + 模型名 + 地址三者配得上。

    ⚠️ 为什么自检要有这一项：Key 和模型名只校验"填没填"的话，
    模型在方舟上被关掉（InvalidEndpoint.ClosedEndpoint）时自检照样全绿，
    开了边采边标之后**每一条评论**都重试 3 次再进失败池，日志刷屏。
    在这里一次就能拦下来，并且告诉用户去哪儿改。

    只看 HTTP 状态：2xx 就算通，不关心输出被不被截断——
    这里只验"连得上、认不认这个模型"，标注效果不是自检的事。
    """
    import httpx

    payload: Dict[str, Any] = {
        "model": llm.model,
        "stream": False,
        "input": [{"role": "user",
                   "content": [{"type": "input_text", "text": "ping"}]}],
        "max_output_tokens": 16,
    }
    # 和正式调用保持一致（比如关思考链），免得自检过了、正式请求被拒
    if getattr(llm, "extra_body", None):
        payload.update(llm.extra_body)
    try:
        with httpx.Client(transport=transport, timeout=httpx.Timeout(
                30.0, connect=float(getattr(llm, "connect_timeout", 5.0)))) as client:
            resp = client.post(llm.url, json=payload, headers={
                "Authorization": f"Bearer {llm.api_key}",
                "Content-Type": "application/json",
            })
    except httpx.HTTPError as exc:
        return False, f"连不上 {llm.url}：{exc}"

    if resp.status_code < 400:
        return True, f"{llm.model} 调用正常"

    code, message = "", resp.text[:300]
    try:
        err = (resp.json() or {}).get("error") or {}
        code = str(err.get("code") or "")
        message = str(err.get("message") or message)[:300]
    except ValueError:
        pass
    hint = next((h for k, h in _ARK_HINTS.items() if code.startswith(k)), "")
    detail = f"模型 {llm.model} 调用失败：HTTP {resp.status_code} {code} {message}".strip()
    return False, f"{detail.rstrip('.。')}。{hint}" if hint else detail


_manager: Optional[LabelingManager] = None


def get_manager(config: Config = None) -> LabelingManager:
    """全局单例。引擎自带线程池和连接池，一个进程只该有一个。

    采集侧（runner）调用时不传 config——那条路径上没有它，
    而且如果连管理器都还没建过，说明服务还没起来，本来也不该标注。
    所以这时候返回一个"永远关着"的空壳，而不是抛异常把采集带崩。
    """
    global _manager
    if _manager is None:
        if config is None:
            return _DISABLED
        _manager = LabelingManager(config)
    return _manager


class _DisabledManager:
    """占位：还没初始化时用它，所有操作都是 no-op。"""
    enabled = False
    available = False

    async def submit_comments(self, rows) -> int:
        return 0

    async def reset(self) -> None:
        return None

    async def resume_pending(self) -> str:
        return ""

    async def status(self):
        return {"enabled": False, "running": False,
                "error": "标注管理器尚未初始化（服务还没完成启动）"}


_DISABLED = _DisabledManager()
