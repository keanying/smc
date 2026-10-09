"""账号轮换锁：一个号采过了，下一轮先换别人——除非只有一个号。

这套测试盯着两件容易搞砸的事：
  1. 轮换是"换个人先上"，**不是"把人踢掉"**。所有号都锁着（包括最常见的
     "就一个号"）时，候选名单必须还是满的，否则采集直接停摆。
  2. 锁在 Redis 里。Redis 挂了/退化了，最坏是"又挑回了老样子"，
     绝不能因此报错或者把号判死。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.account_rotation import DEFAULT_LOCK_HOURS, AccountRotation


class FakeConfig:
    def __init__(self, cfg=None):
        self._cfg = {"crawl": {"account_quota": cfg if cfg is not None else {}}}

    def get(self, key, default=None):
        return self._cfg.get(key, default)


class FakeRedis:
    """够用就行：只实现 key / set / ttl / delete。"""

    def __init__(self):
        self.data = {}      # key -> 到期时间戳

    def key(self, *parts):
        return ":".join(["smc", *[str(p) for p in parts]])

    def set(self, key, value, ex=None, nx=False):
        self.data[key] = time.time() + (ex or 0)
        return True

    def ttl(self, key):
        if key not in self.data:
            return -2
        left = self.data[key] - time.time()
        return int(left) if left > 0 else -2

    def delete(self, *keys):
        return sum(1 for k in keys if self.data.pop(k, None) is not None)


class DeadRedis:
    """连 key() 之后每个操作都炸——模拟 Redis 真的没了。"""

    def key(self, *parts):
        return ":".join(str(p) for p in parts)

    def set(self, *a, **kw):
        raise RuntimeError("Connection refused")

    def ttl(self, *a, **kw):
        raise RuntimeError("Connection refused")

    def delete(self, *a, **kw):
        raise RuntimeError("Connection refused")


def _rot(cfg=None, redis=None):
    return AccountRotation(FakeConfig(cfg), redis or FakeRedis())


def _rows(*names):
    return [{"account_name": n, "rotate_lock_hours": 0} for n in names]


# ---------------- 配置 ----------------

def test_default_is_twelve_hours():
    assert _rot().global_hours() == DEFAULT_LOCK_HOURS == 12.0


def test_global_hours_can_be_configured():
    assert _rot({"rotate_lock_hours": 6}).global_hours() == 6.0


def test_zero_or_garbage_falls_back_to_factory_default():
    """0 和填错都退回 12 小时，不是"不锁"。

    想彻底关掉轮换有专门的开关（rotate_lock_enabled），
    把时长填成 0 更可能是手滑。
    """
    for bad in (0, "", None, "abc", -3):
        assert _rot({"rotate_lock_hours": bad}).global_hours() == 12.0


def test_account_level_override_beats_global():
    """这就是用户说的"可以在账号侧配置"。"""
    rot = _rot({"rotate_lock_hours": 6})
    assert rot.hours_for({"rotate_lock_hours": 2}) == 2.0
    assert rot.hours_for({"rotate_lock_hours": 0}) == 6.0   # 0 = 跟随全局
    assert rot.hours_for(None) == 6.0


def test_absurd_lock_is_capped():
    """填 99999 小时等于把号永久雪藏，多半是填错了。"""
    assert _rot({"rotate_lock_hours": 99999}).global_hours() == 24 * 14


# ---------------- 排序 ----------------

def test_used_account_goes_to_the_back():
    rot = _rot()
    rot.mark_used("xiaohongshu", "A")
    ordered = [r["account_name"] for r in rot.order("xiaohongshu", _rows("A", "B", "C"))]
    assert ordered[0] != "A", "刚采过的 A 不该还排第一"
    assert ordered[-1] == "A"
    assert set(ordered) == {"A", "B", "C"}, "轮换是换个人先上，不是把人踢掉"


def test_single_account_is_still_returned_while_locked():
    """⚠️ 用户原话：「除非只有一个账号」。

    做成硬过滤的话，只有一个号时候选名单会变空，采集直接停摆。
    """
    rot = _rot()
    rot.mark_used("weibo", "只有这一个")
    ordered = rot.order("weibo", _rows("只有这一个"))
    assert [r["account_name"] for r in ordered] == ["只有这一个"]


def test_all_locked_still_returns_everyone_earliest_first():
    """全锁着的时候也得给人选，挑歇得最久（锁最早到期）的那个。"""
    rot = _rot()
    rot.redis.set(rot._key("douyin", "晚"), "1", ex=9000)
    rot.redis.set(rot._key("douyin", "早"), "1", ex=100)
    ordered = [r["account_name"] for r in rot.order("douyin", _rows("晚", "早"))]
    assert ordered == ["早", "晚"]
    assert len(ordered) == 2


def test_unlocked_accounts_keep_their_original_order():
    """没锁的那几个保持原来的 last_check_time 顺序，别瞎打乱。"""
    rot = _rot()
    rot.mark_used("kuaishou", "B")
    ordered = [r["account_name"] for r in rot.order("kuaishou", _rows("A", "B", "C", "D"))]
    assert ordered == ["A", "C", "D", "B"]


def test_expired_lock_frees_the_account():
    rot = _rot()
    rot.redis.set(rot._key("xiaohongshu", "A"), "1", ex=-1)   # 已过期
    assert rot.locked_seconds("xiaohongshu", "A") == 0
    assert [r["account_name"] for r in rot.order("xiaohongshu", _rows("A", "B"))] == ["A", "B"]


def test_manual_release_unlocks_immediately():
    rot = _rot()
    rot.mark_used("xiaohongshu", "A")
    assert rot.locked_seconds("xiaohongshu", "A") > 0
    rot.release("xiaohongshu", "A")
    assert rot.locked_seconds("xiaohongshu", "A") == 0


def test_switch_off_means_no_reordering():
    rot = _rot({"rotate_lock_enabled": False})
    rot.mark_used("xiaohongshu", "A")
    assert [r["account_name"] for r in rot.order("xiaohongshu", _rows("A", "B"))] == ["A", "B"]


def test_per_account_hours_are_used_when_locking():
    rot = _rot({"rotate_lock_hours": 12})
    rot.mark_used("xiaohongshu", "A", {"rotate_lock_hours": 1})
    left = rot.locked_seconds("xiaohongshu", "A")
    assert 3000 < left <= 3600, f"该按账号自己的 1 小时锁，实际 {left} 秒"


# ---------------- Redis 挂了 ----------------

def test_dead_redis_never_raises_and_never_blocks():
    """Redis 没了，轮换退回原顺序——但一个异常都不许往上抛。

    锁是**优化**不是**约束**：丢了锁最坏是"又挑回了老样子"，不会超采，
    真正的上限由配额和冷却把着（那两个以 MySQL 为准）。
    """
    rot = _rot(redis=DeadRedis())
    rot.mark_used("xiaohongshu", "A")            # 不许抛
    rot.release("xiaohongshu", "A")              # 不许抛
    assert rot.locked_seconds("xiaohongshu", "A") == 0
    assert [r["account_name"] for r in rot.order("xiaohongshu", _rows("A", "B"))] == ["A", "B"]


# ---------------- 结构性：挑号入口必须走轮换 ----------------

def test_both_account_pickers_go_through_rotation():
    """pick_active 和 active_names 两个入口都得过 _rotate。

    漏掉任何一个，那条路径上的轮换就等于不存在，而现象跟没做一模一样
    （号有三个，压力全在一个上）。
    """
    text = (Path(__file__).resolve().parents[1] / "app" / "repositories"
            / "account_repo.py").read_text(encoding="utf-8")
    for name in ("pick_active", "active_names"):
        body = text[text.index(f"async def {name}"):]
        body = body[:body.index("\n    async def ", 10)]
        assert "_rotate(" in body, f"{name} 没走轮换锁"


def test_rotation_never_shrinks_the_candidate_list():
    """_rotate 必须校验条数没变，变了就用原顺序。

    这是最后一道保险：轮换实现哪天写错把人弄丢了，宁可不轮换，
    也不能让候选名单变空——那是直接把采集弄停。
    """
    text = (Path(__file__).resolve().parents[1] / "app" / "repositories"
            / "account_repo.py").read_text(encoding="utf-8")
    body = text[text.index("def _rotate"):]
    body = body[:body.index("\n    async def ", 10)]
    assert "len(ordered) == len(rows)" in body


def test_runner_marks_the_account_after_the_quota_gate():
    """被配额拦下来的那一轮压根没采，不该上锁。"""
    text = (Path(__file__).resolve().parents[1] / "app" / "scheduler"
            / "runner.py").read_text(encoding="utf-8")
    assert "mark_rotation_used" in text
    assert text.index("quota.start_session") < text.index("mark_rotation_used")


def test_cookie_path_records_the_account_it_picked():
    """Cookie 那条路上挑完号要写回 ctx，不然配额和轮换全落空。"""
    text = (Path(__file__).resolve().parents[1] / "app" / "scheduler"
            / "runner.py").read_text(encoding="utf-8")
    assert 'ctx.account_name = account["account_name"]' in text
