"""景区档案采集作业：按区域分批跑，进度可见，可取消。

为什么要做成**后台作业**而不是一个接口同步跑完：一个省几百到两千条，
携程还要逐条拉详情——十几分钟起步。同步跑的话浏览器早超时了，
而采集还在后台继续，用户看到的是"失败"，实际上数据在进。

为什么进度要能**刷新页面后还看得见**：这是上一版补标功能踩过的坑。
进度只存在前端内存里的话，用户切个页面回来就以为任务没了，
于是再点一次——两个作业同时打同一个站点，风控直接拉黑。
所以进度存在服务端，页面只是去取。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.logging import get_logger
from .archive import ArchiveRow, Region, get_source
from .archive_store import ArchiveStore

logger = get_logger(__name__)

#: 详情一批拉多少条。并发再高也没用——出口 IP 就那么几个，
#: 堆并发只会让同一个 IP 的 QPS 变高，更容易被风控。
DETAIL_BATCH = 20


@dataclass
class ArchiveJob:
    job_id: str
    channel: str
    region_ids: List[str] = field(default_factory=list)
    region_names: List[str] = field(default_factory=list)
    with_detail: bool = False
    #: 已经处理完的区域数
    regions_done: int = 0
    collected: int = 0          # 采到的条数（去重后）
    created: int = 0            # 其中新增
    updated: int = 0            # 其中更新（已存在的）
    detail_done: int = 0
    detail_failed: int = 0
    current: str = ""           # 当前在采哪个区域，给页面显示
    message: str = ""
    status: str = "running"     # running / finished / failed / canceled
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False

    def touch(self, message: str = "") -> None:
        if message:
            self.message = message
        self.updated_at = time.time()

    @property
    def percent(self) -> float:
        total = len(self.region_ids) or 1
        return round(self.regions_done * 100.0 / total, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id, "channel": self.channel,
            "region_ids": self.region_ids, "region_names": self.region_names,
            "with_detail": self.with_detail,
            "regions_total": len(self.region_ids),
            "regions_done": self.regions_done,
            "collected": self.collected, "created": self.created,
            "updated": self.updated, "detail_done": self.detail_done,
            "detail_failed": self.detail_failed, "current": self.current,
            "message": self.message, "status": self.status, "error": self.error,
            "percent": self.percent,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "elapsed_seconds": round(
                (self.finished_at or time.time()) - self.started_at, 1),
        }


class ArchiveManager:
    """全局单例（挂在 AppState 上）。同一个渠道同时只允许一个作业。"""

    def __init__(self, config, proxy_manager, db):
        self.config = config
        self.proxy_manager = proxy_manager
        self.db = db
        self.store = ArchiveStore(db)
        self._jobs: Dict[str, ArchiveJob] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def source(self, channel: str):
        return get_source(channel, self.config, self.proxy_manager, self.db)

    # ---------------- 区域 ----------------
    async def refresh_regions(self, channel: str) -> Dict[str, Any]:
        regions = await self.source(channel).regions()
        saved = await self.store.save_regions(
            channel, [{"region_id": r.region_id, "region_name": r.region_name,
                       "level": r.level} for r in regions])
        return {"total": len(regions), "saved": saved}

    async def add_region(self, channel: str, region_id: str,
                         region_name: str, level: str = "province") -> None:
        """手工加一条区域。

        留这个口子是因为三个平台的区域清单都是从页面上扒的，
        平台一改版解析就可能失灵——那时候整个渠道的档案就采不了了。
        能手工填一条 pid/districtId，功能就不会被一次改版彻底堵死。
        """
        await self.store.save_regions(channel, [{
            "region_id": region_id, "region_name": region_name, "level": level}])

    # ---------------- 作业 ----------------
    def job(self, job_id: str) -> Optional[ArchiveJob]:
        return self._jobs.get(job_id)

    def jobs(self) -> List[Dict[str, Any]]:
        return [j.to_dict() for j in
                sorted(self._jobs.values(), key=lambda x: x.started_at, reverse=True)]

    def running_job(self, channel: str) -> Optional[ArchiveJob]:
        for job in self._jobs.values():
            if job.channel == channel and job.status == "running":
                return job
        return None

    def start(self, channel: str, regions: List[Dict[str, str]], *,
              with_detail: bool = False, limit: int = 0) -> ArchiveJob:
        running = self.running_job(channel)
        if running:
            raise RuntimeError(
                f"{channel} 已经有一个采集在跑了（{running.current or '准备中'}）。"
                f"两个作业同时打同一个站点，最可能的结果是两个都被风控。")
        job = ArchiveJob(
            job_id=uuid.uuid4().hex[:12], channel=channel,
            region_ids=[r["region_id"] for r in regions],
            region_names=[r.get("region_name") or r["region_id"] for r in regions],
            with_detail=with_detail,
        )
        self._jobs[job.job_id] = job
        self._tasks[job.job_id] = asyncio.create_task(
            self._run(job, regions, limit))
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "running":
            return False
        job.cancel_requested = True
        job.touch("已请求停止，正在收尾……")
        return True

    async def _run(self, job: ArchiveJob, regions: List[Dict[str, str]],
                   limit: int) -> None:
        source = self.source(job.channel)
        try:
            for entry in regions:
                if job.cancel_requested:
                    job.status = "canceled"
                    job.touch("已停止")
                    break
                region = Region(region_id=entry["region_id"],
                                region_name=entry.get("region_name") or entry["region_id"],
                                level=entry.get("level") or "province")
                job.current = region.region_name
                job.touch(f"正在采 {region.region_name}")

                def on_progress(count: int, text: str, _job=job) -> None:
                    _job.touch(text)

                rows: List[ArchiveRow] = await source.collect_region(
                    region, progress=on_progress, limit=limit)
                payload = [r.to_dict() for r in rows]
                result = await self.store.upsert_many(
                    job.channel, payload, supports_detail=source.supports_detail)
                job.collected += result["total"]
                job.created += result["created"]
                job.updated += result["updated"]
                await self.store.mark_region_done(
                    job.channel, region.region_id, result["total"])
                job.regions_done += 1
                job.touch(f"{region.region_name} 采到 {result['total']} 条"
                          f"（新增 {result['created']}，更新 {result['updated']}）")

                if job.with_detail and source.supports_detail:
                    await self._collect_details(job, source, region.region_id)

            if job.status == "running":
                job.status = "finished"
                job.touch("采集完成")
        except asyncio.CancelledError:
            job.status = "canceled"
            job.touch("已取消")
            raise
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)[:500]
            job.touch(f"失败：{job.error}")
            logger.exception("[档案采集] %s 作业失败", job.channel)
        finally:
            job.finished_at = time.time()
            job.current = ""
            self._tasks.pop(job.job_id, None)

    async def _collect_details(self, job: ArchiveJob, source,
                               region_id: str) -> None:
        while True:
            if job.cancel_requested:
                return
            poi_ids = await self.store.pending_detail_ids(
                job.channel, region_id, limit=DETAIL_BATCH)
            if not poi_ids:
                return
            for poi_id in poi_ids:
                if job.cancel_requested:
                    return
                try:
                    detail = await source.fetch_detail(poi_id)
                    await self.store.save_detail(job.channel, poi_id, detail)
                    job.detail_done += 1
                except Exception as exc:  # noqa: BLE001
                    # 单条详情失败**不能**让整个作业停——一个景区的详情页
                    # 挂了，不该连累后面几百个。状态写 error，之后可以单独重采。
                    await self.store.mark_detail_error(job.channel, poi_id, str(exc))
                    job.detail_failed += 1
                job.touch(f"详情 {job.detail_done} 成功 / {job.detail_failed} 失败")

    # ---------------- 单条重采 ----------------
    async def refresh_one(self, channel: str, poi_id: str) -> Dict[str, Any]:
        source = self.source(channel)
        if not source.supports_detail:
            raise RuntimeError(f"{source.label}没有景区详情页，这条采不到更多字段")
        try:
            detail = await source.fetch_detail(poi_id)
        except Exception as exc:  # noqa: BLE001
            await self.store.mark_detail_error(channel, poi_id, str(exc))
            raise
        await self.store.save_detail(channel, poi_id, detail)
        return await self.store.get(channel, poi_id) or {}


# ---------------------------------------------------------------- 单例
#: 作业进度**必须**活在一个全局单例里：页面刷新后还要能查到同一个作业，
#: 每个请求各建一个 manager 的话，刷新一下进度就"消失"了，
#: 用户会以为任务没跑，于是再点一次。
_manager: Optional[ArchiveManager] = None


def get_manager(config=None, proxy_manager=None, db=None) -> ArchiveManager:
    global _manager
    if _manager is None:
        if config is None or db is None:
            raise RuntimeError("档案管理器还没初始化")
        _manager = ArchiveManager(config, proxy_manager, db)
    return _manager


def reset_manager() -> None:
    """只给测试用：每个用例换一个干净的实例。"""
    global _manager
    _manager = None
