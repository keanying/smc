# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""补标：持续投喂 + 真实进度 + 可取消。

这三件事以前都不对：
  · 捞一批 limit 条就收工，库里剩几万条也只标这一批，用户得反复点
  · 进度是"提交条数"，0 → 全部一次跳完，中间十几分钟看不出在动
  · 点错了没法停，只能重启服务
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.labeling.manager import BackfillJob, LabelingManager   # noqa: E402


# ---------------------------------------------------------------- 进度模型
def test_percent_uses_written_rows_not_submitted():
    """百分比按**回库数**算。

    喂进队列 ≠ 标完了：中间可能失败、可能被规则判成无效。
    拿提交数当进度，会出现"100% 了但库里还有一半没标"。
    """
    job = BackfillJob(job_id="j", total=200, submitted=200, done=50)
    assert job.percent == 25.0


def test_percent_never_exceeds_100():
    """有人手动标了几条，回库数可能超过开跑时的总数——别显示 130%。"""
    job = BackfillJob(job_id="j", total=100, submitted=100, done=130)
    assert job.percent == 100.0


def test_eta_absent_until_there_is_data():
    """没跑出东西之前不猜剩余时间。乱跳的预估比不显示更让人不信。"""
    job = BackfillJob(job_id="j", total=100, done=0)
    assert job.to_dict()["eta_seconds"] is None


def test_eta_appears_once_progress_is_real():
    job = BackfillJob(job_id="j", total=100, done=25)
    job.started_at = time.time() - 10          # 10 秒标了 25 条
    eta = job.to_dict()["eta_seconds"]
    assert eta is not None and 20 <= eta <= 40  # 剩 75 条，约 30 秒


def test_to_dict_carries_everything_the_page_needs():
    """页面刷新后要靠这份 dict 把进度整个还原出来，不能缺字段。"""
    d = BackfillJob(job_id="j").to_dict()
    for key in ("job_id", "total", "submitted", "done", "batches", "queued",
                "percent", "status", "elapsed_seconds", "eta_seconds",
                "updated_at", "cancel_requested"):
        assert key in d, f"缺字段 {key}，页面刷新后会显示不全"


# ---------------------------------------------------------------- 取消
def _mgr_with_job(status="running"):
    mgr = LabelingManager.__new__(LabelingManager)
    mgr._jobs = {}
    job = BackfillJob(job_id="j1", status=status)
    mgr._jobs["j1"] = job
    return mgr, job


def test_cancel_marks_the_flag():
    mgr, job = _mgr_with_job()
    assert mgr.cancel_backfill("j1") is True
    assert job.cancel_requested is True


def test_cancel_is_noop_on_finished_job():
    mgr, _ = _mgr_with_job(status="finished")
    assert mgr.cancel_backfill("j1") is False


def test_cancel_unknown_job():
    mgr, _ = _mgr_with_job()
    assert mgr.cancel_backfill("nope") is False


# ---------------------------------------------------------------- where 条件
def test_count_and_fetch_share_the_same_filter():
    """⚠️ 统计口径必须和取数口径一致。

    分母用一套条件、分子用另一套，进度会卡在 97% 不动，看着像卡死。
    """
    mgr = LabelingManager.__new__(LabelingManager)
    job = BackfillJob(job_id="j", scenic_id="S1", channel="douyin")
    where = mgr._where(job)
    assert "S1" in where and "douyin" in where
    assert where.count(" AND ") == 1


def test_where_is_empty_without_filters():
    mgr = LabelingManager.__new__(LabelingManager)
    assert mgr._where(BackfillJob(job_id="j")) == ""


def test_scenic_id_is_quoted():
    """景区 id 是拼进 SQL 的（引擎的 extra_where 接口就是字符串），
    必须转义——否则一个带引号的 id 就能把 SQL 拼坏。"""
    mgr = LabelingManager.__new__(LabelingManager)
    where = mgr._where(BackfillJob(job_id="j", scenic_id="a'b"))
    assert "a'b" not in where or "\\'" in where or "''" in where


# ---------------------------------------------------------------- 并发保护
@pytest.mark.asyncio
async def test_second_backfill_is_rejected_while_one_runs():
    """两个补标同时跑会抢同一批未标注的行 → 重复调模型 → 重复花钱。"""
    mgr = LabelingManager.__new__(LabelingManager)
    mgr._jobs = {"j1": BackfillJob(job_id="j1", status="running")}
    mgr._job_seq = 0

    async def _started():
        return True
    mgr.ensure_started = _started

    with pytest.raises(RuntimeError, match="已经有一个补标任务"):
        await mgr.start_backfill()


@pytest.mark.asyncio
async def test_backfill_allowed_after_previous_finished():
    mgr = LabelingManager.__new__(LabelingManager)
    mgr._jobs = {"j1": BackfillJob(job_id="j1", status="finished")}
    mgr._job_seq = 0
    mgr._engine = SimpleNamespace()

    async def _started():
        return True
    mgr.ensure_started = _started

    import asyncio
    created = []

    async def _fake_run(job, limit):
        created.append((job, limit))
    mgr._run_backfill = _fake_run

    job = await mgr.start_backfill(scenic_id="S9")
    await asyncio.sleep(0)
    assert job.scenic_id == "S9"
    assert job.status == "running"


def test_batch_size_is_sane():
    """批太小捞库太频繁，批太大取消不及时、进度条跳得粗。"""
    assert 50 <= LabelingManager.BACKFILL_BATCH <= 500


# ---------------------------------------------------------------- 流水线投喂
# 线上：4 并发补 3179 条，15 分钟才处理 195 条。以前每批 200 条要全部排干才捞下一批，
# 批尾几条慢的（推理模型单条十几秒、截断重试更久）拖着，其余线程空等。

class _SimEngine:
    """内存里的"表 + 队列 + worker"：每次 wait_idle 处理掉队首 rate 条。"""

    def __init__(self, n_rows, failing=(), rate=3, concurrency=2):
        self.unlabeled = set(range(1, n_rows + 1))
        self.failing = set(failing)
        self.queue = []
        self.rate = rate
        self.cfg = SimpleNamespace(worker=SimpleNamespace(concurrency=concurrency))

    def submit_many(self, rows):
        self.queue.extend(r["comment_id"] for r in rows)
        return len(rows)

    def wait_idle(self, timeout, poll=0.5):
        for cid in self.queue[:self.rate]:
            if cid not in self.failing:
                self.unlabeled.discard(cid)
        del self.queue[:self.rate]
        return not self.queue


def _sim_manager(tmp_path, engine):
    cfg = {"labeling": {"enabled": False},
           "labeling.runtime_config_path": str(tmp_path / "engine.runtime.yaml")}
    mgr = LabelingManager(SimpleNamespace(get=lambda key, default=None: cfg.get(key, default),
                                          source_path=None))
    mgr._engine = engine
    mgr.BACKFILL_BATCH = 10
    fetch_backlogs = []

    def _count(job, after_id=None, upto_id=None):
        ids = [i for i in engine.unlabeled
               if after_id is None or after_id < i <= upto_id]
        if after_id is None:
            job.max_id = max(engine.unlabeled, default=0)
        return len(ids)

    def _fetch(job, limit):
        fetch_backlogs.append(len(engine.queue))
        ids = sorted(i for i in engine.unlabeled if job.last_id < i <= job.max_id)[:limit]
        return [{"id": i, "comment_id": i} for i in ids]

    mgr._count_unlabeled = _count
    mgr._fetch_unlabeled = _fetch
    mgr._queue_backlog = lambda: len(engine.queue)
    return mgr, fetch_backlogs


def test_backfill_tops_up_queue_before_it_runs_dry(tmp_path):
    """⚠️ 下一批要在队列排空**之前**续上，线程才不会在批尾空等。"""
    import asyncio
    engine = _SimEngine(60)
    mgr, fetch_backlogs = _sim_manager(tmp_path, engine)
    job = BackfillJob(job_id="bf", status="running")
    asyncio.run(mgr._run_backfill(job, 0))

    assert job.status == "finished"
    assert job.total == 60 and job.done == 60 and job.failed == 0
    refills = [b for b in fetch_backlogs[1:-1]]          # 去掉第一次和最后一次（捞空）
    assert refills and all(b > 0 for b in refills), \
        f"续批时队列已经排空了，线程会空等：{fetch_backlogs}"
    assert max(fetch_backlogs) < mgr._low_water(), "队列压得太多，取消不及时"


def test_backfill_counts_failures_exactly_at_the_end(tmp_path):
    """标不上的记失败、不重送；整轮结束按区间精确重算，不多不少。"""
    import asyncio
    engine = _SimEngine(45, failing={3, 17, 44})
    mgr, _ = _sim_manager(tmp_path, engine)
    job = BackfillJob(job_id="bf", status="running")
    asyncio.run(mgr._run_backfill(job, 0))
    assert job.status == "finished"
    assert job.done == 45 and job.failed == 3 and job.percent == 100.0
    assert not job.open_batches


def test_backfill_respects_limit_exactly(tmp_path):
    """⚠️ 额度刚好用完时剩余额度是 0——不能把 0 当成"不限"继续往下捞。"""
    import asyncio
    engine = _SimEngine(100)
    mgr, _ = _sim_manager(tmp_path, engine)
    job = BackfillJob(job_id="bf", status="running")
    asyncio.run(mgr._run_backfill(job, 25))
    assert job.submitted == 25, f"上限 25 条，实际送了 {job.submitted} 条"
    assert len(engine.unlabeled) == 75


def test_low_water_scales_with_concurrency(tmp_path):
    mgr, _ = _sim_manager(tmp_path, _SimEngine(1, concurrency=16))
    assert mgr._low_water() == 32
    mgr._engine = SimpleNamespace()                         # 读不到并发数：按 4 兜底
    assert mgr._low_water() == 8
