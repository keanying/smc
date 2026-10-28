# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""集群：派任务的规则、租约选主、任务认领、掉线收尾、设置同步、请求转发。

纯逻辑（dispatcher.plan、转发判断）不连库；其余在真实 MySQL 上跑，
用两个 Cluster 对象模拟两台机器共用一个库。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cluster.dispatcher import AccountPool, NodeSlot, plan
from app.cluster.identity import NodeIdentity
from app.core.constants import TaskStatus

TEST_DB = "scenic_media_cluster_test"


def _task(channel="xiaohongshu", **params):
    return {"task_id": "t1", "task_name": "测试", "channels": [channel], "params": params}


def _slot(node_id, max_running=2, load=0):
    return NodeSlot(node_id=node_id, node_name=node_id, max_running=max_running, load=load)


def _pool(rows, owners=None):
    return AccountPool([{"node_id": n, "channel": c, "account_group": g, "c": k}
                        for n, c, g, k in rows], owners)


# ---------------------------------------------------------------------------
# 派任务规则（不连库）
# ---------------------------------------------------------------------------

def test_goes_to_the_machine_that_has_the_account():
    pool = _pool([("B", "xiaohongshu", "default", 1), ("A", "douyin", "default", 2)])
    node, _ = plan(_task("xiaohongshu"), [_slot("A"), _slot("B")], pool)
    assert node.node_id == "B"
    node, _ = plan(_task("douyin"), [_slot("A"), _slot("B")], pool)
    assert node.node_id == "A"


def test_waits_for_the_busy_machine_instead_of_one_without_accounts():
    """号只在 B 上，B 满了：等 B，不派给没号的 A（派过去也是排队等号）。"""
    pool = _pool([("B", "xiaohongshu", "default", 1)])
    node, reason = plan(_task(), [_slot("A"), _slot("B", max_running=1, load=1)], pool)
    assert node is None and "满负荷" in reason


def test_least_loaded_among_equals():
    pool = _pool([("A", "weibo", "default", 1), ("B", "weibo", "default", 1)])
    node, _ = plan(_task("weibo"), [_slot("A", 3, 2), _slot("B", 3, 0)], pool)
    assert node.node_id == "B"


def test_account_group_counts():
    pool = _pool([("A", "weibo", "default", 3), ("B", "weibo", "vip", 1)])
    node, _ = plan(_task("weibo", account_group="vip"), [_slot("A"), _slot("B")], pool)
    assert node.node_id == "B"


def test_named_account_pins_to_its_machine():
    pool = _pool([("A", "douyin", "default", 1), ("B", "douyin", "default", 1)],
                 owners={("douyin", "dy_B"): "B"})
    node, _ = plan(_task("douyin", account_name="dy_B"), [_slot("A"), _slot("B", 3, 2)], pool)
    assert node.node_id == "B"
    node, reason = plan(_task("douyin", account_name="dy_B"), [_slot("A")], pool)
    assert node is None and "dy_B" in reason


def test_pinned_machine():
    pool = _pool([("A", "weibo", "default", 1)])
    node, _ = plan(_task("weibo", node_id="B"), [_slot("A"), _slot("B")], pool)
    assert node.node_id == "B", "指定了机器就只派那台，哪怕它没号"
    node, reason = plan(_task("weibo", node_id="C"), [_slot("A")], pool)
    assert node is None and "C" in reason


def test_unassigned_accounts_count_for_every_machine():
    pool = _pool([("", "weibo", "default", 1)])
    node, _ = plan(_task("weibo"), [_slot("A", 2, 1), _slot("B", 2, 0)], pool)
    assert node.node_id == "B"


def test_no_login_platform_goes_to_least_loaded():
    node, _ = plan(_task("ctrip"), [_slot("A", 2, 1), _slot("B", 2, 0)], _pool([]))
    assert node.node_id == "B"


def test_no_workers():
    node, reason = plan(_task(), [], _pool([]))
    assert node is None and "没有在线" in reason


# ---------------------------------------------------------------------------
# 请求转发：哪些请求该去哪台（不连库）
# ---------------------------------------------------------------------------

class _FakeAccounts:
    def __init__(self, owners):
        self.owners = owners

    async def node_of(self, channel, name):
        return self.owners.get((channel, name))


class _FakeTasks:
    def __init__(self, tasks):
        self._tasks = tasks

    async def get(self, task_id):
        return self._tasks.get(task_id)


def _fake_cluster(role="all", online=("A", "B")):
    async def node_url(node_id):
        return ("http://x", "") if node_id in online else ("", "离线")

    async def online_workers():
        return [{"node_id": "B"}]

    return SimpleNamespace(
        me=NodeIdentity("A", "A", role, "http://a", "host", 1),
        accounts=_FakeAccounts({("douyin", "dy_B"): "B", ("douyin", "dy_A"): "A",
                                ("douyin", "dy_C"): "C"}),
        tasks=_FakeTasks({"t9": {"status": "running", "node_id": "B"},
                          "t8": {"status": "completed", "node_id": "B"}}),
        node_url=node_url, online_workers=online_workers,
    )


@pytest.mark.asyncio
async def test_forward_targets():
    from app.cluster.forward import resolve_target

    c = _fake_cluster()

    async def body(payload=b"{}"):
        return payload

    async def target(kind, method, path, query="", payload=b"{}"):
        return await resolve_target(c, kind, method, path, query, lambda: body(payload))

    assert await target("http", "POST", "/api/accounts/douyin/dy_B/login-session") == "B"
    assert await target("http", "POST", "/api/accounts/douyin/dy_B/harvest") == "B"
    assert await target("http", "GET", "/api/accounts/douyin/dy_B/cookies") == "", "只读库的不转"
    assert await target("http", "POST", "/api/accounts/douyin/dy_B/cookies") == "B", "导入 Cookie 要写 profile"
    assert await target("http", "DELETE", "/api/accounts/douyin/dy_B") == "B"
    assert await target("http", "DELETE", "/api/accounts/douyin/dy_C") == "", "机器离线了也要能删"
    assert await target("http", "DELETE", "/api/accounts/login-session/B~abc") == "B"
    assert await target("http", "POST", "/api/accounts/login-session/B~abc/save") == "B"
    assert await target("websocket", "", "/ws/browser/B~abc") == "B"
    assert await target("websocket", "", "/ws/tasks/t9/logs") == "B"
    assert await target("websocket", "", "/ws/tasks/t8/logs") == "", "跑完的任务看本机的历史日志"
    assert await target("websocket", "", "/ws/live/t9::douyin::dy_B") == "B"
    assert await target("http", "GET", "/api/live/views", "task_id=t9") == "B"
    assert await target("http", "POST", "/api/accounts", payload=b'{"channel":"douyin","account_name":"new","node_id":"B"}') == "B"
    assert await target("http", "POST", "/api/accounts", payload=b'{"channel":"douyin","account_name":"dy_B","node_id":"B"}') == "", \
        "编辑已有账号只改库，不用转"
    assert await target("http", "GET", "/api/tasks") == ""
    assert await target("http", "POST", "/api/accounts/probe-all") == "", "本机能跑任务就本机做"

    master_only = _fake_cluster(role="master")
    assert await resolve_target(master_only, "http", "POST", "/api/accounts/probe-all", "",
                                lambda: body()) == "B", "只协调的主机把试搜交给从机"


# ---------------------------------------------------------------------------
# 真实 MySQL
# ---------------------------------------------------------------------------

@pytest.fixture()
async def env():
    old = os.environ.get("SMC_MYSQL_DATABASE")
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"

    from app.core.config import load_config, reset_cache
    from app.core.db import Database
    from app.repositories.account_repo import AccountRepository
    from app.repositories.setting_repo import SettingRepository
    from app.repositories.task_repo import TaskRepository

    reset_cache()
    config = load_config(use_cache=False)
    db = Database(config)
    try:
        await db.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"没有 MySQL：{exc}")

    def node(node_id, role="all"):
        from app.cluster import Cluster
        accounts = AccountRepository(db, node_id=node_id)
        return Cluster(config, db, tasks=TaskRepository(db), accounts=accounts,
                       settings=SettingRepository(db),
                       identity=NodeIdentity(node_id, f"机器{node_id}", role,
                                             f"http://{node_id}", f"host-{node_id}", 1))

    try:
        yield SimpleNamespace(db=db, config=config, node=node, tasks=TaskRepository(db),
                              accounts=AccountRepository(db))
    finally:
        await db.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        await db.close()
        if old is None:
            os.environ.pop("SMC_MYSQL_DATABASE", None)
        else:
            os.environ["SMC_MYSQL_DATABASE"] = old
        reset_cache()


async def _account(env, channel, name, node_id, status="active"):
    await env.accounts.create({"channel": channel, "account_name": name, "status": status,
                               "node_id": node_id})


async def _once(env, channel="xiaohongshu", **params) -> str:
    return await env.tasks.create({"task_name": f"{channel}-测试", "channels": [channel],
                                   "keywords": ["武夷山"], "schedule_type": "once",
                                   "params": params})


@pytest.mark.asyncio
async def test_lease_only_one_master_and_takeover(env):
    a, b = env.node("A"), env.node("B")
    assert await a.nodes.acquire_lease("A", 10)
    assert await a.nodes.acquire_lease("A", 10), "同一秒续两次也得认为还在自己手里"
    assert not await b.nodes.acquire_lease("B", 10), "租约没过期，别人抢不走"
    await env.db.execute("UPDATE `src_opinion_cluster_lease` SET expires_at = NOW() - INTERVAL 1 SECOND")
    assert await b.nodes.acquire_lease("B", 10), "过期了就能接手"
    assert await a.nodes.lease_holder() == "B"
    await b.nodes.release_lease("B")
    assert await a.nodes.acquire_lease("A", 10), "主动交出后立刻能接手"


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_cancel_races_start(env):
    task_id = await _once(env)
    assert await env.tasks.claim_for_node(task_id, "A")
    assert not await env.tasks.claim_for_node(task_id, "B"), "派出去了不能再派一次"
    assert [t["task_id"] for t in await env.tasks.assigned_to("A")] == [task_id]
    assert await env.tasks.assigned_to("B") == []

    # 还没开跑就取消：直接取消，从机再来开跑会失败
    assert await env.tasks.request_cancel(task_id) == "canceled"
    assert not await env.tasks.start_assigned(task_id, "A")

    # 开跑后取消：打标记，由从机读到后自己停
    task2 = await _once(env)
    await env.tasks.claim_for_node(task2, "A")
    assert await env.tasks.start_assigned(task2, "A")
    assert await env.tasks.request_cancel(task2) == "flagged"
    assert await env.tasks.should_stop([task2]) == [task2]
    assert await env.tasks.should_stop(["已经删掉的任务"]) == ["已经删掉的任务"]


@pytest.mark.asyncio
async def test_restart_only_resets_own_tasks(env):
    t_a, t_b = await _once(env), await _once(env)
    await env.tasks.claim_for_node(t_a, "A")
    await env.tasks.claim_for_node(t_b, "B")
    await env.tasks.start_assigned(t_b, "B")
    assert await env.tasks.reset_stuck_running(["A"]) == 1
    assert (await env.tasks.get(t_b))["status"] == TaskStatus.RUNNING.value, \
        "A 重启不能把 B 正在跑的任务标成中断"


@pytest.mark.asyncio
async def test_pick_active_only_local_accounts(env):
    from app.repositories.account_repo import AccountRepository

    await _account(env, "douyin", "dy_A", "A")
    await _account(env, "douyin", "dy_B", "B")
    on_a = AccountRepository(env.db, node_id="A")
    assert (await on_a.pick_active("douyin"))["account_name"] == "dy_A"
    assert await on_a.active_names("douyin") == ["dy_A"]
    assert (await on_a.cooling_summary("douyin"))["available"] == 1
    assert len((await env.accounts.list(channel="douyin"))["items"]) == 2, "账号页能看到所有机器的号"
    assert len((await on_a.list(channel="douyin", local_only=True))["items"]) == 1


@pytest.mark.asyncio
async def test_master_dispatches_by_account_location(env):
    master, worker = env.node("A"), env.node("B", role="worker")
    for n in (master, worker):
        await n.heartbeat()
    master.is_leader = True
    await _account(env, "xiaohongshu", "xhs_B", "B")
    await _account(env, "douyin", "dy_A", "A")
    t_xhs, t_dy = await _once(env, "xiaohongshu"), await _once(env, "douyin")

    assert await master.dispatch_due() == 2
    assert (await env.tasks.get(t_xhs))["node_id"] == "B"
    assert (await env.tasks.get(t_dy))["node_id"] == "A"
    logs = await env.tasks.list_logs(t_xhs)
    items = logs["items"] if isinstance(logs, dict) else logs
    assert any("派给机器" in r["message"] for r in items), "派给哪台要写进任务日志"


@pytest.mark.asyncio
async def test_paused_machine_gets_no_new_tasks(env):
    master, worker = env.node("A"), env.node("B", role="worker")
    for n in (master, worker):
        await n.heartbeat()
    await _account(env, "xiaohongshu", "xhs_B", "B")
    await master.nodes.set_accept("B", False)
    task_id = await _once(env, "xiaohongshu")
    await master.dispatch_due()
    task = await env.tasks.get(task_id)
    assert task["node_id"] == "A", "B 暂停接新任务，不往 B 派（A 上没号会在日志里提示）"
    await master.nodes.set_accept("B", True)
    task2 = await _once(env, "xiaohongshu")
    await master.dispatch_due()
    assert (await env.tasks.get(task2))["node_id"] == "B", "恢复后号在哪台就派哪台"


@pytest.mark.asyncio
async def test_offline_machine_tasks_are_reaped(env):
    master, worker = env.node("A"), env.node("B", role="worker")
    for n in (master, worker):
        await n.heartbeat()
    task_id = await _once(env, "weibo")
    await env.tasks.claim_for_node(task_id, "B")
    await env.tasks.start_assigned(task_id, "B")
    assert await master.reap_offline() == 0, "在线的不动"
    await env.db.execute(
        "UPDATE `src_opinion_cluster_node` SET heartbeat_at = NOW() - INTERVAL 5 MINUTE WHERE node_id = 'B'")
    assert await master.reap_offline() == 1
    task = await env.tasks.get(task_id)
    assert task["status"] == TaskStatus.FAILED.value and "掉线" in task["error"]


@pytest.mark.asyncio
async def test_settings_from_another_machine_are_applied(env):
    from app.repositories.setting_repo import SettingRepository

    changed = []
    worker = env.node("B", role="worker")

    async def on_change(keys):
        changed.extend(keys)

    worker.on_settings_changed = on_change
    await worker._sync_settings()                      # 记下初始状态
    await SettingRepository(env.db).save_section("crawl", {"keyword_budget_minutes": 9}, {})
    await worker._sync_settings()
    assert changed == ["crawl"]
    assert worker.config.get("crawl.keyword_budget_minutes") == 9


@pytest.mark.asyncio
async def test_old_accounts_are_claimed_by_the_first_worker(env):
    await _account(env, "douyin", "dy_old", "")
    await _account(env, "ctrip", "placeholder", "")
    worker = env.node("B", role="worker")
    await worker._claim_accounts()
    assert await env.accounts.node_of("douyin", "dy_old") == "B"
    assert await env.accounts.node_of("ctrip", "placeholder") == "", "免登录平台的占位号哪台都能用"


@pytest.mark.asyncio
async def test_worker_runs_assigned_and_honours_remote_cancel(env):
    from app.scheduler.scheduler import TaskScheduler

    worker = env.node("B", role="worker")
    started, stopped = [], []

    class _Runner:
        async def run(self, task, cancel_event):
            started.append(task["task_id"])
            await env.tasks.mark_running(task["task_id"])
            for _ in range(100):
                if cancel_event.is_set():
                    stopped.append(task["task_id"])
                    await env.tasks.mark_finished(task["task_id"], TaskStatus.CANCELED.value)
                    return
                await asyncio.sleep(0.05)

    sched = TaskScheduler(env.config, env.tasks, _Runner(), cluster=worker)
    worker.scheduler = sched
    task_id = await _once(env, "weibo")
    await env.tasks.claim_for_node(task_id, "B")
    await sched._poll()
    await asyncio.sleep(0.1)
    assert started == [task_id]
    assert (await env.tasks.get(task_id))["status"] == TaskStatus.RUNNING.value

    # 别的机器的页面点了取消：本机下一轮轮询读到标记就停
    assert await env.tasks.request_cancel(task_id) == "flagged"
    await sched._poll()
    for _ in range(40):
        if stopped:
            break
        await asyncio.sleep(0.05)
    assert stopped == [task_id]
    await sched.shutdown()
