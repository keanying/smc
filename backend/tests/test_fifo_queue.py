# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""谁先排队谁先执行，后来的排在后面——三处排队都要守这条：

  1. 调度 / 派发：排队等账号的、到点的、新建的，按开始等的时间排；
     排队的任务有号能用了就开始，不再干等到预定恢复时间
  2. 账号浏览器（browser/slots.py）：空出来的号先给排在前面、用得上它的人
  3. 全局浏览器名额（browser/capacity.py）：按取号先后拿
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.browser.capacity import BrowserCapacity
from app.browser.slots import BrowserSlots
from app.scheduler.queue import fifo, ready_tasks


# ---------------------------------------------------------------- 账号浏览器
async def test_freed_account_goes_to_the_earliest_waiter():
    slots = BrowserSlots()
    holder = slots.try_acquire("weibo", ["a"], reason="在跑")
    order = []

    async def wait(name):
        slot = await slots.acquire("weibo", ["a"], reason=name)
        order.append(name)
        await asyncio.sleep(0.01)
        slot.release()

    first = asyncio.create_task(wait("先来的"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(wait("后来的"))
    await asyncio.sleep(0.05)
    assert slots.queue_length("weibo") == 2
    # 有人在排队等 a：新来的不能插队直接拿走
    holder.release()
    assert slots.try_acquire("weibo", ["a"], reason="插队的") is None
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert order == ["先来的", "后来的"]


async def test_newcomer_may_use_an_account_nobody_waiting_wants():
    """前面的人只等 a，空着的 b 他用不上：后来的照样能用 b，不白等。"""
    slots = BrowserSlots()
    slots.try_acquire("weibo", ["a"], reason="在跑")
    waiter = asyncio.create_task(slots.acquire("weibo", ["a"], reason="只要 a"))
    await asyncio.sleep(0.05)
    got = slots.try_acquire("weibo", ["a", "b"], reason="新来的")
    assert got is not None and got.account_name == "b"
    waiter.cancel()


async def test_cancelled_waiter_leaves_the_queue():
    slots = BrowserSlots()
    slots.try_acquire("weibo", ["a"], reason="在跑")
    waiter = asyncio.create_task(slots.acquire("weibo", ["a"], reason="等"))
    await asyncio.sleep(0.05)
    waiter.cancel()
    await asyncio.sleep(0.05)
    assert slots.queue_length("weibo") == 0


# ---------------------------------------------------------------- 全局浏览器名额
async def test_capacity_hands_out_in_ticket_order(monkeypatch):
    from app.browser import capacity as cap_module

    monkeypatch.setattr(cap_module, "_TAKE_POLL_SECONDS", 0.02)
    cap = BrowserCapacity()
    cap._limit = 1
    cap._min_free_gb = 0
    await cap.acquire("在用的")
    order = []

    async def wait(name):
        await cap.acquire(name)
        order.append(name)
        cap.release(name)

    first = asyncio.create_task(wait("先来的"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(wait("后来的"))
    await asyncio.sleep(0.05)
    cap.release("在用的")
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert order == ["先来的", "后来的"]


# ---------------------------------------------------------------- 调度顺序
def _task(task_id, *, queued=None, next_run=None, created=None, channel="weibo",
          status="waiting", **params):
    return {"task_id": task_id, "id": int(task_id[-1]), "status": status, "channels": [channel],
            "queued_at": queued, "next_run_time": next_run, "create_time": created,
            "params": params}


def test_earliest_waiter_first():
    now = datetime(2026, 11, 11, 17, 10)
    tasks = [
        _task("t1", next_run=now, status="pending"),                         # 17:10 到点的定时任务
        _task("t2", queued=now - timedelta(minutes=10), next_run=now + timedelta(minutes=20)),  # 17:00 就在排队
        _task("t3", created=now + timedelta(minutes=1), status="pending"),   # 刚新建的
    ]
    assert [t["task_id"] for t in fifo(tasks)] == ["t2", "t1", "t3"]


class _Tasks:
    def __init__(self, due=(), waiting=(), once=()):
        self._due, self._waiting, self._once = list(due), list(waiting), list(once)

    async def pending_once_tasks(self, limit=50):
        return self._once

    async def due_tasks(self, limit=50):
        return self._due

    async def waiting_not_due(self, limit=50):
        return self._waiting


class _Accounts:
    def __init__(self, free):
        self.free = free
        self.calls = []

    async def cooling_summary(self, channel, *, preferred="", group="", local=True):
        self.calls.append((channel, preferred, group, local))
        key = preferred or channel
        return {"available": 1 if key in self.free else 0, "resume_at": None}


async def test_waiting_task_starts_as_soon_as_an_account_is_free():
    now = datetime.now()
    waiting = _task("t2", queued=now - timedelta(minutes=10), next_run=now + timedelta(minutes=20))
    due = _task("t1", next_run=now, status="pending")
    picked = await ready_tasks(_Tasks(due=[due], waiting=[waiting]), _Accounts({"weibo"}), limit=10)
    assert [t["task_id"] for t in picked] == ["t2", "t1"]     # 排队的先上，不用等到 17:30


async def test_waiting_task_keeps_waiting_when_its_account_is_still_cooling():
    now = datetime.now()
    # 点名要账号 x，x 还在冷却：平台上别的号空着也不行
    waiting = _task("t2", queued=now, next_run=now + timedelta(minutes=20), account_name="x")
    accounts = _Accounts({"weibo"})
    picked = await ready_tasks(_Tasks(waiting=[waiting]), accounts, limit=10)
    assert picked == []
    assert accounts.calls == [("weibo", "x", "", False)]      # 看的是所有机器上的号


# ---------------------------------------------------------------- 使用率：等浏览器的不占运行名额
class _Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Names:
    def __init__(self, names):
        self.names = names

    async def active_names(self, channel, *, preferred="", group=""):
        return [preferred] if preferred else list(self.names.get(channel, []))


class _Cap:
    def __init__(self, active, limit):
        self.active, self.limit = active, limit

    def stats(self):
        return {"active": self.active, "limit": self.limit}


async def test_browser_task_is_blocked_only_when_all_its_browsers_are_busy():
    from app.scheduler.queue import browser_blocked

    slots = BrowserSlots()
    accounts = _Names({"kuaishou": ["k1", "k2"], "weibo": ["w1"]})
    ks = {"task_id": "ks", "channels": ["kuaishou"], "params": {}}
    kw = dict(config=_Cfg(), accounts=accounts, slots=slots, capacity=_Cap(1, 4))
    assert not await browser_blocked(ks, **kw)
    slots.try_acquire("kuaishou", ["k1"], reason="在跑")
    assert not await browser_blocked(ks, **kw)                 # k2 还空着
    slots.try_acquire("kuaishou", ["k2"], reason="在跑")
    assert await browser_blocked(ks, **kw)                     # 两个号的浏览器都被占着

    # 微博接口模式不常驻浏览器：浏览器满不满都不影响它启动
    slots.try_acquire("weibo", ["w1"], reason="在跑")
    wb_api = {"task_id": "wb", "channels": ["weibo"], "params": {"collect_engine": "api"}}
    assert not await browser_blocked(wb_api, **kw)
    wb_human = {"task_id": "wb2", "channels": ["weibo"], "params": {"collect_engine": "human"}}
    assert await browser_blocked(wb_human, **kw)


async def test_browser_task_is_blocked_when_global_browser_limit_is_full():
    from app.scheduler.queue import browser_blocked

    ks = {"task_id": "ks", "channels": ["kuaishou"], "params": {}}
    assert await browser_blocked(ks, config=_Cfg(), accounts=_Names({"kuaishou": ["k1"]}),
                                 slots=BrowserSlots(), capacity=_Cap(3, 3))


async def test_poll_gives_the_slot_to_a_task_that_can_run_now(monkeypatch):
    """排在前面的快手任务等浏览器：这一轮先启动后面能跑的微博任务，快手留着位置。"""
    from app.scheduler import queue as queue_module
    from app.scheduler.scheduler import TaskScheduler

    sched = TaskScheduler.__new__(TaskScheduler)
    sched.cluster = None
    sched._fixed_max_running = 1
    sched._running, sched._cancel_events, sched._started_at, sched._running_meta = {}, {}, {}, {}
    sched._poll_lock = asyncio.Lock()
    sched.runner = None
    launched = []

    async def fake_ready(tasks_repo, accounts, *, limit):
        return [{"task_id": "ks"}, {"task_id": "wb"}]

    async def blocked(task):
        return task["task_id"] == "ks"

    async def launch(task, claimed=False, trigger=""):
        launched.append(task["task_id"])

    async def nothing():
        return None

    monkeypatch.setattr(queue_module, "ready_tasks", fake_ready)
    sched._browser_blocked = blocked
    sched._launch = launch
    sched._watchdog = nothing
    sched.tasks = None
    await sched._poll()
    assert launched == ["wb"]
