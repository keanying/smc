# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""采集时账号要登录 / 要验证：马上换下一个能用的号；没号可用就通知人、进排队。

以前是发通知后**占着名额和浏览器干等最多 30 分钟**，别的号明明能用也不换。
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

    async def mark_expired(self, channel, name, reason=""):
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
        return True

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
    assert r.notified == []                       # 换号成功，不用打扰人


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
