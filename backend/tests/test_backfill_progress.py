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
    """批太小 drain 开销占比高，批太大取消不及时、进度条跳得粗。"""
    assert 50 <= LabelingManager.BACKFILL_BATCH <= 500
