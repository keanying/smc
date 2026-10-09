"""单账号配额与冷却，以及「跳登录页要发通知」。

背景：小红书采集会触发平台的机器判定然后账号被踢下线。查下来两件事：

1. 节奏本身不激进（小红书 2~10 秒一条），但**一个账号的总量和连续
   在线时长完全没有约束**——可以被连着用到看门狗 6 小时超时。
2. 用户复现"小红书跳登录页"时**没收到通知**。原因是三个浏览器采集器里
   `LoginRequired` 出现 **0 次**：跳登录页之后页面上没有卡片，采集器
   只是走"列表里没有新卡片了，结束"静默收尾。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.account_quota import (                    # noqa: E402
    CHANNEL_DEFAULTS, AccountQuota, _seconds_to_midnight,
)


class FakeConfig:
    def __init__(self, quota=None):
        self._quota = quota if quota is not None else {"enabled": True}

    def get(self, key, default=None):
        if key == "crawl":
            return {"account_quota": self._quota}
        return default


class FakeRedis:
    def __init__(self, broken=False):
        self.store = {}
        self.broken = broken

    def key(self, *parts):
        return "smc:" + ":".join(parts)

    def get(self, key):
        if self.broken:
            raise RuntimeError("Redis 断了")
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if self.broken:
            raise RuntimeError("Redis 断了")
        self.store[key] = value
        return True


class FakeDB:
    """只实现配额用到的那几个方法。"""

    def __init__(self):
        self.works = {}          # (channel, account, date) -> 条数
        self.cooldowns = {}      # (channel, account) -> datetime
        self.executed = []

    async def fetch_value(self, sql, args=None, default=None):
        args = args or []
        if "works" in sql:
            return self.works.get((args[0], args[1], args[2]), default)
        return default

    async def fetch_one(self, sql, args=None):
        args = args or []
        if "cooldown_until" in sql:
            until = self.cooldowns.get((args[0], args[1]))
            return {"cooldown_until": until} if until else {"cooldown_until": None}
        return None

    async def fetch_all(self, sql, args=None):
        return []

    async def execute(self, sql, args=None):
        args = args or []
        self.executed.append((sql, args))
        if "INSERT INTO" in sql and "account_quota" in sql:
            key = (args[0], args[1], args[2])
            self.works[key] = self.works.get(key, 0) + args[3]
        elif "SET cooldown_until = %s" in sql:
            self.cooldowns[(args[2], args[3])] = args[0]
        elif "cooldown_until = NULL" in sql:
            self.cooldowns.pop((args[0], args[1]), None)
        return 1


def _quota(quota_cfg=None, redis=None, db=None) -> AccountQuota:
    return AccountQuota(FakeConfig(quota_cfg), redis or FakeRedis(), db or FakeDB())


# ---------------------------------------------------------------- 上限解析
def test_channel_defaults_are_conservative_for_xhs():
    """小红书的出厂上限必须比别的平台紧。

    它是这次出事的平台——如果哪天有人把它调得和微博一样松，
    这条会拦下来。
    """
    xhs = CHANNEL_DEFAULTS["xiaohongshu"]
    weibo = CHANNEL_DEFAULTS["weibo"]
    assert xhs["daily_works"] < weibo["daily_works"]
    assert xhs["session_minutes"] < weibo["session_minutes"]
    assert xhs["cooldown_minutes"] > weibo["cooldown_minutes"]


def test_user_config_overrides_factory_default():
    q = _quota({"enabled": True, "daily_works": 42})
    assert q.limits_for("xiaohongshu")["daily_works"] == 42
    # 没覆盖的项仍然走平台出厂默认
    assert q.limits_for("xiaohongshu")["session_minutes"] == 45


def test_per_channel_beats_global():
    q = _quota({"enabled": True, "daily_works": 42,
                "per_channel": {"xiaohongshu": {"daily_works": 7}}})
    assert q.limits_for("xiaohongshu")["daily_works"] == 7
    assert q.limits_for("douyin")["daily_works"] == 42


def test_zero_means_use_factory_default_not_unlimited():
    """页面上三个数字框默认是 0，提示写的是"留 0 = 按平台出厂默认"。

    ⚠️ 如果后端把 0 当成"不限制"，用户什么都不改点一下保存，
    小红书的 150 条/天就被悄悄改成无限制，而页面上还显示着
    "按平台默认"——那比没有这个功能更危险。
    要彻底关掉配额请用 enabled: false。
    """
    q = _quota({"enabled": True, "daily_works": 0,
                "session_minutes": 0, "cooldown_minutes": 0})
    limits = q.limits_for("xiaohongshu")
    assert limits == CHANNEL_DEFAULTS["xiaohongshu"], (
        f"填 0 之后小红书的上限变成了 {limits}，出厂默认被冲掉了")


def test_garbage_config_falls_back():
    """配置里写了个字符串不能把采集弄崩。"""
    q = _quota({"enabled": True, "daily_works": "很多"})
    assert q.limits_for("douyin")["daily_works"] == CHANNEL_DEFAULTS["douyin"]["daily_works"]


# ---------------------------------------------------------------- 日配额
@pytest.mark.asyncio
async def test_daily_quota_trips_and_starts_cooldown():
    db = FakeDB()
    q = _quota({"enabled": True, "daily_works": 3, "cooldown_minutes": 30}, db=db)

    for _ in range(3):
        assert (await q.check("xiaohongshu", "a")).ok
        await q.record_works("xiaohongshu", "a", 1)

    verdict = await q.check("xiaohongshu", "a")
    assert not verdict.ok
    assert "日配额" in verdict.reason or "配额" in verdict.reason
    assert ("xiaohongshu", "a") in db.cooldowns, "超配额却没有进入冷却"


@pytest.mark.asyncio
async def test_cooling_account_is_rejected_until_it_expires():
    db = FakeDB()
    q = _quota({"enabled": True}, db=db)
    db.cooldowns[("xiaohongshu", "a")] = datetime.now() + timedelta(minutes=5)
    assert not (await q.check("xiaohongshu", "a")).ok

    # 到期之后自动恢复，不需要任何人去清
    db.cooldowns[("xiaohongshu", "a")] = datetime.now() - timedelta(minutes=1)
    assert (await q.check("xiaohongshu", "a")).ok


@pytest.mark.asyncio
async def test_manual_clear_releases_the_account():
    db = FakeDB()
    q = _quota({"enabled": True}, db=db)
    db.cooldowns[("xiaohongshu", "a")] = datetime.now() + timedelta(hours=3)
    await q.clear_cooldown("xiaohongshu", "a")
    assert (await q.check("xiaohongshu", "a")).ok


# ---------------------------------------------------------------- 连续时长
@pytest.mark.asyncio
async def test_session_cap_trips():
    db = FakeDB()
    q = _quota({"enabled": True, "daily_works": 0, "session_minutes": 30}, db=db)
    q.start_session("xiaohongshu", "a")
    assert (await q.check("xiaohongshu", "a")).ok
    # 把开始时刻往前拨，等价于已经连续跑了 31 分钟
    q._session_start["xiaohongshu:a"] -= 31 * 60
    verdict = await q.check("xiaohongshu", "a")
    assert not verdict.ok
    assert "连续" in verdict.reason


@pytest.mark.asyncio
async def test_cooldown_ends_the_session_clock():
    """进冷却时要把计时清掉，否则恢复之后立刻又判超时。"""
    db = FakeDB()
    q = _quota({"enabled": True, "session_minutes": 30}, db=db)
    q.start_session("xiaohongshu", "a")
    q._session_start["xiaohongshu:a"] -= 31 * 60
    await q.check("xiaohongshu", "a")
    assert q.session_minutes("xiaohongshu", "a") == 0


# ---------------------------------------------------------------- Redis 退化
@pytest.mark.asyncio
async def test_counts_survive_redis_going_down():
    """Redis 断了不能让配额闸门悄悄放开。

    这是最危险的失效模式：Redis 一挂，计数读不到就当成 0，
    配额永远不会触发，而日志上什么异常都没有。
    """
    db = FakeDB()
    db.works[("xiaohongshu", "a", __import__("datetime").date.today().isoformat())] = 99

    q = _quota({"enabled": True, "daily_works": 10}, redis=FakeRedis(broken=True), db=db)
    verdict = await q.check("xiaohongshu", "a")
    assert not verdict.ok, "Redis 断了就把配额放开了"


@pytest.mark.asyncio
async def test_redis_miss_falls_back_to_db():
    db = FakeDB()
    db.works[("xiaohongshu", "a", __import__("datetime").date.today().isoformat())] = 7
    q = _quota({"enabled": True, "daily_works": 10}, db=db)
    assert await q.used_today("xiaohongshu", "a") == 7


@pytest.mark.asyncio
async def test_disabled_quota_never_blocks():
    db = FakeDB()
    db.cooldowns[("xiaohongshu", "a")] = datetime.now() + timedelta(hours=3)
    q = _quota({"enabled": False}, db=db)
    assert (await q.check("xiaohongshu", "a")).ok


def test_daily_counter_ttl_expires_at_midnight():
    """日计数的 TTL 要跨天自动清零，不能是固定 24 小时。"""
    assert 60 <= _seconds_to_midnight() <= 24 * 3600


# ---------------------------------------------------------------- 结构性
def test_every_account_picker_skips_cooling_accounts():
    """挑账号的每个入口都要排掉冷却中的账号。

    漏掉一处的后果不是报错，是那条路径照样把冷却中的账号挑出来用——
    配额闸门等于不存在，而现象和没加过闸门一模一样。
    """
    src = (Path(__file__).resolve().parents[1]
           / "app" / "repositories" / "account_repo.py").read_text(encoding="utf-8")
    import re

    pickers = re.findall(
        r"(async def (?:pick_active|active_names|list_active)\b.*?)(?=\n    async def |\n    @|\Z)",
        src, re.S)
    assert len(pickers) == 3, f"挑账号的入口数量变了（找到 {len(pickers)} 个），这条测试要跟着更新"
    for body in pickers:
        name = body.split("(")[0].replace("async def ", "")
        assert "_NOT_COOLING" in body, f"{name} 没有排掉冷却中的账号"


def test_browser_collectors_detect_login_redirect():
    """三个浏览器采集器都要在"一条都没采到"时复查登录态。

    ⚠️ 这条测试就是为那个 bug 立的：在修之前，
    xhs_browser / kuaishou_browser / douyin_browser 里
    `LoginRequired` 出现 **0 次**——跳登录页之后页面上没有卡片，
    采集器只当成"这个关键字没内容"，静默收尾，一条通知都不发。
    """
    root = Path(__file__).resolve().parents[1] / "app" / "collectors"
    for name in ("xhs_browser", "kuaishou_browser", "douyin_browser"):
        text = (root / f"{name}.py").read_text(encoding="utf-8")
        assert "recheck_login" in text, (
            f"{name}.py 没有复查登录态——跳登录页会被当成「没搜到内容」静默跳过")


def test_recheck_login_raises_login_required():
    """复查本身必须抛 LoginRequired，才能接上 runner 的通知链路。

    抛别的异常只会被记成一条"采集错误"，走不到「暂停等人处理」那条路。
    """
    text = (Path(__file__).resolve().parents[1] / "app" / "collectors"
            / "browser_capture.py").read_text(encoding="utf-8")
    body = text[text.index("async def recheck_login"):]
    body = body[:body.index("\n    def ")]
    assert body.count("raise LoginRequired") >= 2, "URL 跳转和 Cookie 失效两种情况都要抛"


def test_xhs_verification_codes_need_human():
    """小红书 461/471 是"要人来验证"，必须抛 LoginRequired。

    抛 RuntimeError 的话会被取详情那个兜底 except 吞掉，
    表现成"每条笔记都少字段"，真正的原因一条通知都不会发。
    """
    text = (Path(__file__).resolve().parents[1] / "app" / "collectors"
            / "xhs.py").read_text(encoding="utf-8")
    assert "_NEED_HUMAN_STATUS" in text
    assert "raise LoginRequired(f\"{hint}" in text
    # 取详情的兜底 except 之前要先把 LoginRequired 放行
    detail = text[text.index("data = await self._post(FEED_URI, payload)"):]
    assert detail.index("except LoginRequired") < detail.index("except Exception"), \
        "LoginRequired 会被兜底 except 吞掉"


class BrokenDB:
    """模拟"老库"：没有 src_opinion_account_quota 表、也没有 cooldown_until 列。

    `CREATE TABLE IF NOT EXISTS` 能补表，但补不了别人已有表上的新列，
    而且用户也可能把升级脚本跳过去。这种库上，配额的每一句 SQL 都报错。
    """

    def __init__(self):
        self.calls = 0

    async def _boom(self, *a, **kw):
        self.calls += 1
        raise RuntimeError("Table 'smc.src_opinion_account_quota' doesn't exist")

    fetch_value = fetch_one = fetch_all = execute = _boom


@pytest.mark.asyncio
async def test_old_database_does_not_take_collection_down():
    """配额表不存在时，闸门失效，但采集必须照常跑。

    ⚠️ 这是这套功能最容易搞砸的地方：为了"防封号"加的防护，
    如果在老库上抛异常冒到 runner，结果就是用户升级完发现
    **采集整个不工作了**。宁可闸门这一轮不生效（日志里有 warning），
    也不能让主功能停摆。
    """
    db = BrokenDB()
    quota = _quota({"enabled": True, "daily_works": 5}, db=db)

    # 四个入口一个都不许抛
    verdict = await quota.check("xiaohongshu", "a")
    await quota.record_works("xiaohongshu", "a", 1)
    assert await quota.used_today("xiaohongshu", "a") >= 0
    assert await quota.usage("xiaohongshu", "a") == []
    assert await quota.cooling_until("xiaohongshu", "a") is None
    assert db.calls > 0, "测试本身没打到数据库，等于什么都没验"
    assert verdict.ok, "读不到用量时应当放行，而不是把账号一律判死"


def test_runner_survives_a_missing_quota():
    """TaskRunner 没装上配额时（__new__ 造出来的半成品）不能崩。

    取 quota 必须走 getattr，直接 self.quota 会 AttributeError，
    而那个异常会把整条关键字循环打断——表现成"只采到一条就停了"。
    """
    text = (Path(__file__).resolve().parents[1] / "app" / "scheduler"
            / "runner.py").read_text(encoding="utf-8")
    assert 'getattr(self, "quota", None)' in text
    body = text[text.index("async def _collect_keyword"):]
    assert "self.quota." not in body, "关键字循环里不能直接点 self.quota"
