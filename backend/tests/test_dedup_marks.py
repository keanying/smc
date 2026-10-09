"""采集水印：关键字 4 小时、作品 3 天，都落 Redis。

用户的诉求是两件不同的事：
  - 关键字水印：任务被反复重启时别把刚采完的关键字再跑一遍
  - 作品水印：同一条作品被多个关键字命中是常态，命中就跳过、连评论都不翻
"""
from __future__ import annotations

import pytest

from app.core.dedup_marks import (
    DEFAULT_KEYWORD_TTL, DEFAULT_WORK_TTL, DedupMarks,
)


class _FakeRedis:
    """够用的假 Redis：只要 key/get/set(nx,ex)/delete/ttl。"""

    def __init__(self):
        self.data = {}
        self.ttls = {}
        self.prefix = "smc"

    def key(self, *parts):
        return ":".join([self.prefix, *[str(p) for p in parts]])

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.data.pop(k, None) is not None else 0
        return n

    def ttl(self, key):
        return self.ttls.get(key, -1)


class _Cfg:
    def __init__(self, dedup=None):
        self._crawl = {"dedup": dedup} if dedup is not None else {}

    def get(self, path, default=None):
        return {"crawl": self._crawl}.get(path, default)


def test_出厂默认是_4小时和3天():
    """用户明确指定的两个数字，别被"顺手调优"改掉。"""
    m = DedupMarks(_FakeRedis(), _Cfg())
    assert m.keyword_ttl == DEFAULT_KEYWORD_TTL == 4 * 3600
    assert m.work_ttl == DEFAULT_WORK_TTL == 3 * 24 * 3600


def test_关键字水印打了就跳过():
    r = _FakeRedis()
    m = DedupMarks(r, _Cfg())
    assert not m.keyword_done("S1", "kuaishou", "八大处公园")
    m.mark_keyword("S1", "kuaishou", "八大处公园")
    assert m.keyword_done("S1", "kuaishou", "八大处公园")
    assert m.keyword_ttl_left("S1", "kuaishou", "八大处公园") == 4 * 3600
    # 换平台、换景区、换词都算不同的水印
    assert not m.keyword_done("S1", "douyin", "八大处公园")
    assert not m.keyword_done("S2", "kuaishou", "八大处公园")
    assert not m.keyword_done("S1", "kuaishou", "八大处缆车")


def test_作品水印跨景区共享():
    """同一条作品被两个景区的关键字命中，也只该采一次。

    所以作品的 key 里**不能**带 scenic_id——带了去重就失效了。
    """
    r = _FakeRedis()
    m = DedupMarks(r, _Cfg())
    assert m.claim_work("kuaishou", "3xw7gtmaqjkb2as") is True
    assert m.claim_work("kuaishou", "3xw7gtmaqjkb2as") is False
    assert m.work_done("kuaishou", "3xw7gtmaqjkb2as")
    # 不同平台的同名 id 是两条作品
    assert m.claim_work("douyin", "3xw7gtmaqjkb2as") is True


def test_claim_是原子的():
    """两个任务同时跑：只能有一个拿到。

    先 get 再 set 的写法在这里会双双放行，然后同一条作品被采两遍。
    """
    r = _FakeRedis()
    a = DedupMarks(r, _Cfg())
    b = DedupMarks(r, _Cfg())          # 另一个任务，共用同一个 Redis
    got = [a.claim_work("kuaishou", "W1"), b.claim_work("kuaishou", "W1")]
    assert got.count(True) == 1, f"两个任务都拿到了：{got}"


def test_可以分别关掉两级水印():
    r = _FakeRedis()
    m = DedupMarks(r, _Cfg({"keyword_enabled": False}))
    m.mark_keyword("S1", "kuaishou", "词")
    assert not m.keyword_done("S1", "kuaishou", "词"), "关了还在拦"
    assert m.claim_work("kuaishou", "W1") is True      # 作品级不受影响

    m2 = DedupMarks(r, _Cfg({"work_enabled": False}))
    assert m2.claim_work("kuaishou", "W9") is True
    assert m2.claim_work("kuaishou", "W9") is True, "关了还在拦"


def test_总开关关掉就完全不去重():
    m = DedupMarks(_FakeRedis(), _Cfg({"enabled": False}))
    m.mark_keyword("S1", "kuaishou", "词")
    assert not m.keyword_done("S1", "kuaishou", "词")
    assert m.claim_work("kuaishou", "W1") is True
    assert m.claim_work("kuaishou", "W1") is True


def test_ttl_可配():
    m = DedupMarks(_FakeRedis(), _Cfg({"keyword_ttl_seconds": 60,
                                       "work_ttl_seconds": 120}))
    assert (m.keyword_ttl, m.work_ttl) == (60, 120)


def test_超长关键字用哈希兜底():
    r = _FakeRedis()
    m = DedupMarks(r, _Cfg())
    long_kw = "八大处" * 100
    m.mark_keyword("S1", "kuaishou", long_kw)
    assert m.keyword_done("S1", "kuaishou", long_kw)
    key = next(iter(r.data))
    assert len(key) < 200, f"key 太长了：{len(key)}"


class _BoomRedis(_FakeRedis):
    def get(self, key):
        raise RuntimeError("Redis 挂了")

    def set(self, key, value, ex=None, nx=False):
        raise RuntimeError("Redis 挂了")


def test_redis_挂了要放行而不是拦住采集():
    """水印是优化，不是必需。

    Redis 挂了应该"当作没采过"照常采，而不是当作"采过了"全跳过——
    后者会让一整轮任务什么都不采，而且日志上看着一切正常。
    """
    m = DedupMarks(_BoomRedis(), _Cfg())
    assert not m.keyword_done("S1", "kuaishou", "词"), "Redis 挂了却说采过"
    assert m.claim_work("kuaishou", "W1") is True, "Redis 挂了却把作品拦下来"
    m.mark_keyword("S1", "kuaishou", "词")     # 不该抛


def test_describe_能看出配置():
    m = DedupMarks(_FakeRedis(), _Cfg())
    assert "4 小时" in m.describe() and "3 天" in m.describe()
    off = DedupMarks(_FakeRedis(), _Cfg({"enabled": False}))
    assert "已关闭" in off.describe()
