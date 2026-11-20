# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""账号明明好好的，一到点各平台齐刷刷报「没有可用账号，请先添加」。

线上：八大处公园的任务 19:00 同时报了抖音 / 快手 / 小红书 / 微博，账号管理里每个平台都有正常的号。
同一句话背后可能是好几种原因，这里分别钉住：
  · 派发：所有机器的号都在冷却 / 轮换时按负载挑机器，可能派到一个号都没有的机器
  · 任务级账号分组按平台建（「快手」「小红书」），多平台任务只有一个平台对得上
  · 任务点名的号被删 / 改名了
告警要说出真实原因，派错机器的不打扰人、回队列重新派。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cluster.dispatcher import AccountPool, NodeSlot, needs_of, plan
from app.collectors.base import LoginRequired
from app.repositories.account_repo import AccountRepository
from app.scheduler.runner import AccountsCoolingDown, TaskRunner


# ---------------------------------------------------------------- 派发
def _slot(node_id, load=0, max_running=4):
    return NodeSlot(node_id=node_id, node_name=node_id, max_running=max_running, load=load)


def test_all_cooling_goes_to_the_machine_that_owns_the_accounts():
    """所有号都在冷却：派给**有号**的机器排队，而不是负载更低、一个号都没有的机器。"""
    task = {"channels": ["kuaishou", "weibo"], "params": {}}
    owned = [{"node_id": "opinion", "channel": "kuaishou", "account_group": "快手", "c": 3},
             {"node_id": "opinion", "channel": "weibo", "account_group": "微博", "c": 3}]
    pool = AccountPool([], owned_rows=owned)                 # 能用的 0 个：都在冷却
    node, why = plan(task, [_slot("opinion", load=3), _slot("spare", load=0)], pool)
    assert node is not None and node.node_id == "opinion", why


def test_usable_accounts_still_win_over_owned_ones():
    task = {"channels": ["kuaishou"], "params": {}}
    usable = [{"node_id": "b", "channel": "kuaishou", "account_group": "x", "c": 1}]
    owned = usable + [{"node_id": "a", "channel": "kuaishou", "account_group": "x", "c": 5}]
    node, _ = plan(task, [_slot("a"), _slot("b", load=2)], AccountPool(usable, owned_rows=owned))
    assert node.node_id == "b"


def test_task_level_group_without_accounts_of_this_platform_is_ignored_when_dispatching():
    task = {"channels": ["weibo"], "params": {"account_group": "快手"}}
    rows = [{"node_id": "a", "channel": "weibo", "account_group": "微博", "c": 2}]
    pool = AccountPool(rows, owned_rows=rows)
    need = needs_of(task)[0]
    assert need.group_from_task and pool.usable("a", need) == 2


def test_platform_level_group_stays_strict():
    task = {"channels": ["weibo"],
            "params": {"channel_params": {"weibo": {"account_group": "快手"}}}}
    rows = [{"node_id": "a", "channel": "weibo", "account_group": "微博", "c": 2}]
    need = needs_of(task)[0]
    if need.group:                                            # 平台级分组生效时
        assert not need.group_from_task
        assert AccountPool(rows, owned_rows=rows).usable("a", need) == 0


# ---------------------------------------------------------------- 原因诊断
class _DB:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_all(self, sql, args=None):
        return [dict(r) for r in self.rows]


def _acc(name, node="opinion", group="快手", status="active", enabled=1, cooling=0):
    return {"account_name": name, "node_id": node, "account_group": group,
            "status": status, "enabled": enabled, "cooling": cooling}


async def _why(rows, node="opinion", **kw):
    return await AccountRepository(_DB(rows), node_id=node).explain_unavailable("kuaishou", **kw)


async def test_explains_each_cause():
    rows = [_acc("199 0500 7281"), _acc("199 0500 8632")]
    assert (await _why([]))[0] == "none"
    code, text = await _why(rows, preferred="旧账号")
    assert code == "preferred_missing" and "旧账号" in text and "不存在" in text
    code, text = await _why(rows, group="默认分组")
    assert code == "group_empty" and "快手" in text
    assert (await _why([_acc("a", status="expired")]))[0] == "expired"
    assert (await _why([_acc("a", enabled=0)]))[0] == "disabled"
    code, text = await _why(rows, node="spare")
    assert code == "elsewhere" and "opinion" in text
    assert (await _why([_acc("a", cooling=1)]))[0] == "cooling"
    assert (await _why(rows))[0] == "ok"


# ---------------------------------------------------------------- 调度
class _Accounts:
    def __init__(self, code, text="", groups=(("kuaishou", "快手"),)):
        self.code, self.text, self.groups = code, text, set(groups)

    async def cooling_summary(self, channel, *, preferred="", group="", local=True):
        return {"available": 0, "resume_at": None}

    async def explain_unavailable(self, channel, *, preferred="", group=""):
        return self.code, self.text

    async def has_group(self, channel, group):
        return (channel, group) in self.groups

    async def list(self, **kw):
        return {"total": 3}


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m, *a, **k):
        self.lines.append(m)

    warn = error = info


def _runner(accounts):
    r = TaskRunner.__new__(TaskRunner)
    r.browser_manager = SimpleNamespace(accounts=accounts)
    r.quota = None
    r._channel_accounts = {}
    r.alerts = []

    async def notify(channel, kind, detail="", **kw):
        r.alerts.append({"kind": kind, "detail": detail, **kw})
        return True
    r._notify = notify
    return r


TASK = {"task_id": "T1", "channels": ["kuaishou"], "params": {}}
NO_ACCOUNT = LoginRequired("平台 kuaishou 没有可用账号，请先在账号管理里添加并登录一个账号")


async def test_wrong_machine_requeues_quietly_then_alerts():
    r = _runner(_Accounts("elsewhere", "快手的账号都在机器 opinion 上，本机 spare 没有"))
    for _ in range(TaskRunner.ELSEWHERE_QUIET_RETRIES - 1):
        with pytest.raises(AccountsCoolingDown) as info:
            await r._switch_account_or_queue(TASK, "kuaishou", "s", "八大处公园", _Log(), None, NO_ACCOUNT)
        assert (info.value.resume_at - datetime.now()) < timedelta(minutes=2)
    assert r.alerts == [], "派错机器先悄悄改派，不打扰人"
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(TASK, "kuaishou", "s", "八大处公园", _Log(), None, NO_ACCOUNT)
    assert len(r.alerts) == 1 and "opinion" in r.alerts[0]["detail"], "一直派不对（那台多半离线）才叫人"


async def test_alert_says_the_real_cause_instead_of_add_an_account():
    text = "任务指定的快手账号 [旧账号] 在账号管理里不存在（可能被删除或改了名）"
    r = _runner(_Accounts("preferred_missing", text))
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(TASK, "kuaishou", "s", "八大处公园", _Log(), None, NO_ACCOUNT)
    assert r.alerts[0]["kind"] == "config"
    assert r.alerts[0]["detail"] == text
    assert "请先在账号管理里添加" not in r.alerts[0]["detail"]


async def test_cooling_does_not_alert_even_without_resume_time():
    r = _runner(_Accounts("cooling", "快手的账号都在冷却 / 轮换中"))
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(TASK, "kuaishou", "s", "八大处公园", _Log(), None, NO_ACCOUNT)
    assert r.alerts == []


async def test_task_level_group_without_this_platform_falls_back_to_all_accounts():
    r = _runner(_Accounts("ok"))
    log = _Log()
    task = {"task_id": "T1", "channels": ["weibo", "kuaishou"], "params": {"account_group": "快手"}}
    assert await r._effective_group(task, "weibo", log) == ""
    assert any("不按分组" in line for line in log.lines)
    assert await r._effective_group(task, "kuaishou", log) == "快手"


# ---------------------------------------------------------------- 新旧两个服务同时在跑
async def test_detects_a_second_live_process_on_the_same_node_id(monkeypatch):
    """同一台服务器上老进程没停：编号一样，节点表只有一行，页面看不出来。心跳要能发现。"""
    from app.cluster.service import Cluster
    from app.notify import feishu

    sent = []

    class _N:
        async def send(self, event, *, force=False):
            sent.append(event)
            return True
    monkeypatch.setattr(feishu, "_notifier", _N())

    c = Cluster.__new__(Cluster)
    c.me = SimpleNamespace(node_id="opinion", host="srv", pid=100)
    c.started_at = datetime.now()
    row = {"pid": 100, "host": "srv", "version": "15.0", "started_at": None}

    async def _get(node_id):
        return dict(row)
    c.nodes = SimpleNamespace(get=_get)

    await c._check_twin()                       # 第一次心跳前不判断（可能是刚死掉的老进程留下的行）
    await c._check_twin()                       # 行还是自己的：没事
    assert sent == []
    row.update(pid=42)                          # 期间被另一个进程写了
    await c._check_twin()
    await c._check_twin()                       # 一小时内只通知一次
    assert len(sent) == 1 and "42" in sent[0].detail and "100" in sent[0].detail
    assert "平台：" not in feishu.FeishuNotifier(SimpleNamespace(get=lambda *a, **k: {})).render(sent[0])
