"""账号都在冷却时，定时任务排队、恢复后自动执行（而不是这一轮跳过）。

客户原话："因为账号有轮换机制，定时任务要是在冷却期启动，要投入队列，恢复后执行"。
以前撞上冷却：pick_active 挑不到号 / 配额闸门判不过 → 日志一句「这一轮跳过」，
这一轮的定时采集就白白丢了。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.core.constants import TaskStatus
from app.scheduler.runner import AccountsCoolingDown, TaskRunner

TEST_DB = "scenic_media_cooling_queue"


# ---------------------------------------------------------------------------
# 真实 MySQL：SQL 层的几处改动（排队、取任务、改期、冷却汇总）
# ---------------------------------------------------------------------------

@pytest.fixture()
async def repos():
    old = os.environ.get("SMC_MYSQL_DATABASE")
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"

    from app.core.config import load_config, reset_cache
    from app.core.db import Database
    from app.repositories.account_repo import AccountRepository
    from app.repositories.task_repo import TaskRepository

    reset_cache()
    db = Database(load_config(use_cache=False))
    await db.connect()
    try:
        yield SimpleNamespace(db=db, tasks=TaskRepository(db), accounts=AccountRepository(db))
    finally:
        await db.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        await db.close()
        if old is None:
            os.environ.pop("SMC_MYSQL_DATABASE", None)
        else:
            os.environ["SMC_MYSQL_DATABASE"] = old
        reset_cache()


async def _cron_task(tasks, name="武夷山-小红书") -> str:
    return await tasks.create({
        "task_name": name, "scenic_id": "S1", "channels": ["xiaohongshu"],
        "collect_type": "keyword", "keywords": ["武夷山"],
        "schedule_type": "cron", "cron_expression": "0 2 * * *",
    })


@pytest.mark.asyncio
async def test_waiting_task_is_picked_up_when_cooldown_ends(repos):
    task_id = await _cron_task(repos.tasks)
    resume = datetime.now().replace(microsecond=0) + timedelta(minutes=30)
    await repos.tasks.mark_waiting(task_id, resume, "排队等账号")

    task = await repos.tasks.get(task_id)
    assert task["status"] == TaskStatus.WAITING.value
    assert task["next_run_time"] == resume, "恢复时间要落库，重启后才捞得回来"

    assert task_id not in [t["task_id"] for t in await repos.tasks.due_tasks()], "没到点不该跑"
    due = await repos.tasks.due_tasks(now=resume + timedelta(seconds=1))
    assert task_id in [t["task_id"] for t in due], "到点要被调度器捞起来"


@pytest.mark.asyncio
async def test_waiting_once_task_still_resumes(repos):
    """一次性任务跑过一次后 schedule_enabled=0，排队时也得能被捞起来。"""
    task_id = await repos.tasks.create({
        "task_name": "一次性", "scenic_id": "S1", "channels": ["douyin"],
        "schedule_type": "once",
    })
    await repos.db.execute(
        "UPDATE `src_opinion_crawl_task` SET schedule_enabled = 0 WHERE task_id = %s", [task_id])
    resume = datetime.now().replace(microsecond=0) - timedelta(seconds=5)
    await repos.tasks.mark_waiting(task_id, resume, "排队等账号")
    assert task_id in [t["task_id"] for t in await repos.tasks.due_tasks()]


@pytest.mark.asyncio
async def test_reschedule_keeps_the_resume_time(repos):
    """跑完的收尾会 reschedule；按 cron 重算会把恢复时刻覆盖成明天凌晨 2 点。"""
    task_id = await _cron_task(repos.tasks)
    resume = datetime.now().replace(microsecond=0) + timedelta(minutes=30)
    await repos.tasks.mark_waiting(task_id, resume, "排队等账号")
    await repos.tasks.reschedule(await repos.tasks.get(task_id))
    assert (await repos.tasks.get(task_id))["next_run_time"] == resume


@pytest.mark.asyncio
async def test_cancelling_a_waiting_task_drops_the_resume(repos):
    from app.scheduler.scheduler import TaskScheduler

    task_id = await _cron_task(repos.tasks)
    resume = datetime.now().replace(microsecond=0) + timedelta(minutes=30)
    await repos.tasks.mark_waiting(task_id, resume, "排队等账号")

    sched = TaskScheduler.__new__(TaskScheduler)
    sched.tasks, sched._cancel_events = repos.tasks, {}
    await sched.cancel(task_id)

    task = await repos.tasks.get(task_id)
    assert task["status"] == TaskStatus.CANCELED.value
    assert task["next_run_time"] != resume, "取消了还按恢复时刻跑，就等于没取消"
    assert task["next_run_time"].hour == 2, "周期任务回到自己的 cron 节奏"


@pytest.mark.asyncio
async def test_cooling_summary(repos):
    acc = repos.accounts
    for name in ("a", "b"):
        await acc.create({"channel": "xiaohongshu", "account_name": name,
                          "status": "active", "account_group": "g1"})
    soon = datetime.now().replace(microsecond=0) + timedelta(minutes=20)
    later = soon + timedelta(minutes=40)
    set_cd = ("UPDATE `src_opinion_social_account` SET cooldown_until = %s "
              "WHERE channel = 'xiaohongshu' AND account_name = %s")

    await repos.db.execute(set_cd, [later, "a"])
    s = await acc.cooling_summary("xiaohongshu")
    assert s["available"] == 1, "b 还能用，不该排队"

    await repos.db.execute(set_cd, [soon, "b"])
    s = await acc.cooling_summary("xiaohongshu")
    assert s == {"available": 0, "resume_at": soon}, "取最早恢复的那个"

    s = await acc.cooling_summary("xiaohongshu", preferred="a")
    assert s["resume_at"] == later, "任务点名了账号，就等那个号"
    s = await acc.cooling_summary("xiaohongshu", group="other")
    assert s == {"available": 0, "resume_at": None}, "该组没号 → 不是冷却，是要人处理"

    # 过期的冷却不算冷却
    await repos.db.execute(set_cd, [datetime.now() - timedelta(minutes=1), "b"])
    assert (await acc.cooling_summary("xiaohongshu"))["available"] == 1


# ---------------------------------------------------------------------------
# runner：什么时候排队、排到几点、任务状态落成什么
# ---------------------------------------------------------------------------

class _Accounts:
    def __init__(self, available: int, resume_at):
        self.summary = {"available": available, "resume_at": resume_at}
        self.calls: List[Dict[str, Any]] = []

    async def cooling_summary(self, channel, *, preferred="", group=""):
        self.calls.append({"channel": channel, "preferred": preferred, "group": group})
        return dict(self.summary)


def _runner(accounts) -> TaskRunner:
    r = TaskRunner.__new__(TaskRunner)
    r.browser_manager = SimpleNamespace(accounts=accounts)
    return r


def _ctx(account="", group=""):
    return SimpleNamespace(account_name=account, params={"account_group": group})


@pytest.mark.asyncio
async def test_all_cooling_raises_with_earliest_resume():
    resume = datetime.now() + timedelta(minutes=45)
    runner = _runner(_Accounts(0, resume))
    with pytest.raises(AccountsCoolingDown) as info:
        await runner._raise_if_all_cooling("xiaohongshu", _ctx(group="g1"))
    assert info.value.resume_at >= resume
    assert info.value.resume_at - resume < timedelta(minutes=1)
    assert "冷却" in info.value.reason
    assert runner.browser_manager.accounts.calls[0]["group"] == "g1"


@pytest.mark.asyncio
async def test_some_account_free_means_no_wait():
    runner = _runner(_Accounts(1, datetime.now() + timedelta(hours=1)))
    await runner._raise_if_all_cooling("xiaohongshu", _ctx())     # 不抛


@pytest.mark.asyncio
async def test_no_accounts_at_all_is_not_cooling():
    """一个登录过的号都没有：那是 LoginRequired（要人处理），不能排队干等。"""
    runner = _runner(_Accounts(0, None))
    await runner._raise_if_all_cooling("xiaohongshu", _ctx())     # 不抛


@pytest.mark.asyncio
async def test_resume_is_never_in_the_past():
    """冷却刚到期（或时钟有偏差）也至少隔一分钟，别让调度器同一秒反复捞。"""
    runner = _runner(_Accounts(0, datetime.now() - timedelta(minutes=5)))
    with pytest.raises(AccountsCoolingDown) as info:
        await runner._raise_if_all_cooling("xiaohongshu", _ctx())
    assert info.value.resume_at > datetime.now() + timedelta(seconds=50)


@pytest.mark.asyncio
async def test_quota_gate_retries_soon_when_another_account_is_free():
    """当前这个号刚被配额闸门判进冷却，但同组还有别的号 → 一分钟后再跑（会挑到别的号）。"""
    runner = _runner(_Accounts(1, datetime.now() + timedelta(hours=3)))
    ctx = _ctx()
    ctx.params["preferred_account"] = ""
    at = await runner._cooling_resume_at("xiaohongshu", ctx)
    assert at < datetime.now() + timedelta(minutes=2)


class _TaskRepo:
    def __init__(self):
        self.calls: List[tuple] = []

    async def mark_running(self, task_id):
        self.calls.append(("running", task_id))

    async def mark_waiting(self, task_id, resume_at, reason, stats=None):
        self.calls.append(("waiting", task_id, resume_at, reason))

    async def mark_finished(self, task_id, status, **kw):
        self.calls.append(("finished", task_id, status))

    async def update_progress(self, *a, **kw):
        pass

    async def add_logs(self, rows):
        pass


@pytest.mark.asyncio
async def test_run_marks_task_waiting_instead_of_skipping():
    resume = datetime.now() + timedelta(minutes=30)
    runner = TaskRunner.__new__(TaskRunner)
    runner.tasks = _TaskRepo()

    async def _cooling(*a, **kw):
        raise AccountsCoolingDown(resume, "小红书的账号都在冷却中")

    runner._run_channel = _cooling
    await runner.run({"task_id": "T1", "task_name": "武夷山", "scenic_id": "",
                      "channels": ["xiaohongshu"]})

    kinds = [c[0] for c in runner.tasks.calls]
    assert "waiting" in kinds, "要排队"
    assert "finished" not in kinds, "不能落成完成/失败——那样调度器不会再捞它"
    waiting = next(c for c in runner.tasks.calls if c[0] == "waiting")
    assert waiting[2] == resume and "冷却" in waiting[3]
