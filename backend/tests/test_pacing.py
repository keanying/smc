"""采集节奏（频率控制）：四个平台一套机制，按平台可配。

用户实跑反馈「快手有些时候会等待很长时间」。以前这些停顿散落在各个
采集器的 `random.uniform(...)` 里，既看不出来也改不了。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.pacing import CHANNEL_DEFAULTS, DEFAULT_PACE, Pacer, _as_range


class _Cfg:
    def __init__(self, pace=None):
        self.crawl = {"pace": pace or {}}


def test_每个平台都有出厂默认():
    for channel in ("douyin", "kuaishou", "xiaohongshu", "weibo"):
        p = Pacer(_Cfg(), channel)
        low, high = p.ranges["work_seconds"]
        assert 0 < low <= high, f"{channel} 的节奏区间不合法：{low}~{high}"
        assert p.ranges["scroll_seconds"][1] < p.ranges["work_seconds"][1], (
            f"{channel}：滚动的停顿不该和作品之间一个量级——"
            f"滚动在循环里，一条作品能滚几十屏"
        )


def test_小红书比微博慢():
    """风控松紧不同，出厂默认要体现出来，不能四个平台一个数。"""
    xhs = Pacer(_Cfg(), "xiaohongshu").ranges["work_seconds"]
    wb = Pacer(_Cfg(), "weibo").ranges["work_seconds"]
    assert xhs[1] > wb[1], f"小红书风控最紧，上限该更大：{xhs} vs {wb}"


def test_配置能覆盖出厂默认():
    p = Pacer(_Cfg({"kuaishou": {"work_seconds": [0.2, 0.4]}}), "kuaishou")
    assert p.ranges["work_seconds"] == (0.2, 0.4)
    # 没配的档位保持默认
    assert p.ranges["scroll_seconds"] == DEFAULT_PACE["scroll_seconds"]
    # 别的平台不受影响
    assert Pacer(_Cfg({"kuaishou": {"work_seconds": [0.2, 0.4]}}),
                 "douyin").ranges["work_seconds"] == CHANNEL_DEFAULTS["douyin"]["work_seconds"]


def test_default_段对所有平台生效():
    p = Pacer(_Cfg({"default": {"scroll_seconds": [3, 4]}}), "weibo")
    assert p.ranges["scroll_seconds"] == (3.0, 4.0)


def test_平台配置优先于_default():
    cfg = _Cfg({"default": {"work_seconds": [9, 9]},
                "kuaishou": {"work_seconds": [1, 2]}})
    assert Pacer(cfg, "kuaishou").ranges["work_seconds"] == (1.0, 2.0)
    assert Pacer(cfg, "weibo").ranges["work_seconds"] == (9.0, 9.0)


@pytest.mark.parametrize("bad", [None, "很快", [], {}, [-1, -2], "1~8"])
def test_配置写坏了不能把采集带崩(bad):
    """配置写错该退回默认并在日志里说，不该让整个采集停下来。"""
    assert _as_range(bad, (1.0, 8.0)) == (1.0, 8.0)


def test_写反了会自动纠正():
    assert _as_range([8, 1], (1.0, 2.0)) == (1.0, 8.0)


def test_写一个数当固定值():
    assert _as_range(3, (1.0, 8.0)) == (3.0, 3.0)
    assert _as_range([3], (1.0, 8.0)) == (3.0, 3.0)


def test_停顿落在配置区间内():
    p = Pacer(_Cfg({"kuaishou": {"work_seconds": [2.0, 4.0]}}), "kuaishou")
    seen = [p.next_delay("work_seconds") for _ in range(40)]
    assert all(1.8 <= d <= 4.2 for d in seen), f"越界了：{min(seen)}~{max(seen)}"
    # 洗牌袋：8 次之内长短都该摊到，不能一直是同一个数
    assert len(set(round(d, 1) for d in seen[:8])) >= 4, "停顿值太集中，不像人"


@pytest.mark.asyncio
async def test_停顿中途可以取消():
    """分段睡，取消信号要能立刻打断——否则点了停止要等这一觉睡完。"""
    class _Boom(Exception):
        pass

    class _Ctx:
        def __init__(self):
            self.calls = 0

        def raise_if_cancelled(self):
            self.calls += 1
            if self.calls > 1:
                raise _Boom()

    p = Pacer(_Cfg({"kuaishou": {"work_seconds": [5.0, 5.0]}}), "kuaishou")
    ctx = _Ctx()
    started = asyncio.get_event_loop().time()
    with pytest.raises(_Boom):
        await p.wait("work_seconds", ctx)
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 2.0, f"取消没能打断停顿，等了 {elapsed:.1f} 秒"


@pytest.mark.asyncio
async def test_固定为_0_时不睡():
    p = Pacer(_Cfg({"default": {"work_seconds": 0}}), "weibo")
    started = asyncio.get_event_loop().time()
    await p.wait("work_seconds")
    assert asyncio.get_event_loop().time() - started < 0.2


def test_describe_能看出配置生效():
    p = Pacer(_Cfg({"kuaishou": {"work_seconds": [2.5, 3.5]}}), "kuaishou")
    text = p.describe()
    assert "2.5" in text and "3.5" in text, f"日志里看不出实际值：{text}"
