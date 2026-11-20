# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""采集时账号要登录 / 要验证：马上换下一个能用的号；没号可用就进排队。

以前是发通知后**占着名额和浏览器干等最多 30 分钟**，别的号明明能用也不换。

告警规则（客户要求）：
  · 正在用的号跳登 / 失效 / 要验证：告警 + 自动换号，告警里写明换没换成
  · 其余号只是在冷却 / 轮换期内、暂时没号可用：不告警，排队到点自己接着跑
  · 一个都等不来（都要人重新登录）：才告警
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.collectors.base import LoginRequired
from app.scheduler.runner import AccountsCoolingDown, TaskRunner


class _Accounts:
    def __init__(self, available=0, resume_at=None):
        self.available = available
        self.resume_at = resume_at
        self.expired = []

    async def cooling_summary(self, channel, *, preferred="", group="", local=True):
        return {"available": self.available, "resume_at": self.resume_at}

    async def get(self, channel, name):
        return {"status": "active"}

    async def mark_expired(self, channel, name, reason="", *, notify=True):
        assert notify is False, "换号时由调度层发一条带处理结果的告警，账号仓库别再单独喊"
        self.expired.append(name)
        self.available = max(0, self.available - 1)


class _Quota:
    def __init__(self):
        self.cooled = []

    async def cool_down(self, channel, account, reason, limits=None):
        self.cooled.append(account)
        return datetime.now() + timedelta(hours=2)


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m, *a, **k):
        self.lines.append(m)

    warn = error = info


def _runner(accounts, quota=None, runs=()):
    r = TaskRunner.__new__(TaskRunner)
    r.browser_manager = SimpleNamespace(accounts=accounts)
    r.quota = quota
    r.notified = []
    r._channel_accounts = {("T1", "weibo"): "号A"}
    attempts = list(runs)

    async def run_channel(task, channel, scenic_id, scenic_name, log, cancel_event):
        outcome = attempts.pop(0)
        if isinstance(outcome, Exception):
            r._channel_accounts[("T1", "weibo")] = "号B"
            raise outcome
        return outcome

    async def notify(channel, kind, detail="", **kw):
        r.notified.append(kind)
        r.alerts.append({"kind": kind, **kw})
        return True

    r.alerts = []

    r._run_channel = run_channel
    r._notify = notify
    return r


TASK = {"task_id": "T1", "channels": ["weibo"], "params": {}}


async def test_switches_to_next_account_immediately():
    accounts = _Accounts(available=2)
    r = _runner(accounts, runs=[{"new_works": 5}])
    stats = await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                             LoginRequired("账号 [号A] 登录态已失效"))
    assert stats == {"new_works": 5}
    assert accounts.expired == ["号A"]
    # 客户要求：跳登要告警 + 自动换号，告警里点名账号、写明已经换了
    assert len(r.alerts) == 1
    assert r.alerts[0]["account"] == "号A"
    assert "已自动切换" in r.alerts[0]["action"]


async def test_no_usable_account_queues_until_cooldown_ends():
    resume = datetime.now() + timedelta(minutes=40)
    accounts = _Accounts(available=1, resume_at=resume)
    r = _runner(accounts)
    with pytest.raises(AccountsCoolingDown) as info:
        await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("登录态已失效"))
    assert abs((info.value.resume_at - resume).total_seconds()) < 10
    assert r.notified                              # 通知人去登录


async def test_nobody_usable_queues_and_checks_again_soon():
    r = _runner(_Accounts(available=1))
    with pytest.raises(AccountsCoolingDown) as info:
        await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("登录态已失效"))
    wait = (info.value.resume_at - datetime.now()).total_seconds() / 60
    assert 9 <= wait <= 11 and "重新登录" in info.value.reason


async def test_pinned_account_is_not_switched():
    accounts = _Accounts(available=3)
    r = _runner(accounts, runs=[{"new_works": 1}])
    task = {**TASK, "params": {"account_name": "号A"}}
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(task, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("登录态已失效"))


async def test_captcha_cools_the_account_instead_of_expiring_it():
    accounts, quota = _Accounts(available=2), _Quota()
    r = _runner(accounts, quota=quota, runs=[{"new_works": 2}])
    await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                     LoginRequired("需要滑块验证码"))
    assert quota.cooled == ["号A"] and accounts.expired == []


async def test_gives_up_after_too_many_bad_accounts():
    accounts = _Accounts(available=99)
    r = _runner(accounts, runs=[LoginRequired("又失效")] * 10)
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("登录态已失效"))
    assert len(accounts.expired) == TaskRunner.MAX_ACCOUNT_SWITCHES


async def test_platform_without_any_account_is_skipped_not_queued():
    """一个号都没配：排队也等不来，照旧报错、跳过这个平台（任务本身不失败）。"""
    accounts = _Accounts(available=0)

    async def no_accounts(**kw):
        return {"total": 0, "items": []}

    accounts.list = no_accounts
    r = _runner(accounts)
    log = _Log()
    assert await r._switch_account_or_queue(TASK, "weibo", "s", "景区", log, None,
                                            LoginRequired("没有可用账号，请到账号管理添加")) == {}
    assert any("一个账号都没配" in line for line in log.lines)


# ---------------------------------------------------------------- 告警别太敏感
async def test_all_accounts_resting_in_rotation_does_not_alert():
    """其余号都在冷却 / 轮换期内，暂时没号可用：排队等着就行，不告警。"""
    resume = datetime.now() + timedelta(minutes=30)
    r = _runner(_Accounts(available=0, resume_at=resume))
    r._channel_accounts = {}                       # 还没挑到号就发现都在冷却
    with pytest.raises(AccountsCoolingDown) as info:
        await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("平台 weibo 没有可用账号"))
    assert r.alerts == [], "账号只是在轮换 / 冷却，不该告警"
    assert abs((info.value.resume_at - resume).total_seconds()) < 10


async def test_nobody_can_recover_by_itself_alerts_once():
    """一个都等不来（都要人重新登录）才叫人。"""
    r = _runner(_Accounts(available=0))
    r._channel_accounts = {}
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("平台 weibo 没有可用账号"))
    assert len(r.alerts) == 1 and "重新登录" in r.alerts[0]["action"]


async def test_kicked_to_login_while_others_rest_alerts_once_with_queue_info():
    """在用的号跳登、其余号都在冷却：点名告警一次（写明排到几点），不再补汇总。"""
    resume = datetime.now() + timedelta(minutes=40)
    r = _runner(_Accounts(available=1, resume_at=resume))
    with pytest.raises(AccountsCoolingDown):
        await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                         LoginRequired("页面跳到了登录页"))
    assert len(r.alerts) == 1
    assert r.alerts[0]["account"] == "号A"
    assert resume.strftime("%H:%M") in r.alerts[0]["action"]


async def test_already_expired_account_is_not_alerted_again():
    """本来就是失效的号再失败一次不重复喊（它当初变失效时已经告警过）。"""
    accounts = _Accounts(available=2)

    async def expired_row(channel, name):
        return {"status": "expired"}
    accounts.get = expired_row
    r = _runner(accounts, runs=[{"new_works": 1}])
    await r._switch_account_or_queue(TASK, "weibo", "s", "景区", _Log(), None,
                                     LoginRequired("登录态已失效"))
    assert r.alerts == []
