"""浏览器并发闸门。

背景：用户全量采集时整机卡死，只能重启。查下来**不是泄漏**——
浏览器关掉之后内存是干净回落的——是根本没有上限。实测数据：

    一个 headless Chromium = 10 个进程 + 约 700MB（about:blank 就这么多）
    8 个并发 = 81 个进程 + 5966MB   ← 压测量到的真实数字

而 scheduler.max_running_tasks 默认是 8。8G 的机器必然换页。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser.capacity import (                     # noqa: E402
    BROWSER_FOOTPRINT_GB, MAX_AUTO, MIN_AUTO, BrowserCapacity, auto_limit,
)


class FakeConfig:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def _cap(**values) -> BrowserCapacity:
    cap = BrowserCapacity()
    cap.configure(FakeConfig(**values))
    return cap


# ---------------------------------------------------------------- 上限
def test_auto_limit_stays_in_range():
    """按内存推算的上限必须落在 [1, 4]。

    推成 0 的话浏览器型任务一条都跑不了；推成十几个又回到了
    "把机器跑死"那个状态——而那正是这整块代码存在的原因。
    """
    assert MIN_AUTO <= auto_limit() <= MAX_AUTO


def test_explicit_limit_wins_over_auto():
    assert _cap(**{"browser.max_concurrent": 2}).limit == 2


def test_zero_means_auto():
    assert _cap(**{"browser.max_concurrent": 0}).limit == auto_limit()


def test_garbage_limit_falls_back_to_auto():
    """配置里写了个字符串也不能把服务弄崩。"""
    assert _cap(**{"browser.max_concurrent": "很多"}).limit == auto_limit()


def test_footprint_is_not_optimistic():
    """一个 Chromium 实测约 0.7GB，估值不能比它小。

    估小的后果是算出一个偏大的上限，机器照样被跑死——
    而这个数字就是用来防止那件事的。
    """
    assert BROWSER_FOOTPRINT_GB >= 0.7


# ---------------------------------------------------------------- 闸门
@pytest.mark.asyncio
async def test_limit_is_actually_enforced():
    cap = _cap(**{"browser.max_concurrent": 2, "browser.min_free_memory_gb": 0})
    live = 0
    peak = 0

    async def worker():
        nonlocal live, peak
        await cap.acquire("t")
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        cap.release("t")

    await asyncio.gather(*[worker() for _ in range(8)])
    assert peak == 2, f"同时开到了 {peak} 个，闸门没拦住"
    assert cap.stats()["active"] == 0


@pytest.mark.asyncio
async def test_release_without_acquire_does_not_inflate_the_limit():
    """重复释放/凭空释放**不能**把名额放大。

    信号量被多 release 一次，上限就永久 +1。跑一天之后闸门形同虚设，
    而且完全看不出来——这比一开始就没有闸门更难查。
    """
    cap = _cap(**{"browser.max_concurrent": 1, "browser.min_free_memory_gb": 0})
    cap.release("没拿过")
    cap.release("没拿过")

    live = 0
    peak = 0

    async def worker():
        nonlocal live, peak
        await cap.acquire("t")
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        cap.release("t")

    await asyncio.gather(*[worker() for _ in range(4)])
    assert peak == 1, f"凭空释放把上限放大到了 {peak}"


@pytest.mark.asyncio
async def test_slot_is_returned_when_memory_wait_is_cancelled():
    """等内存时被取消，名额必须还回去。

    不还的话每取消一次就少一个名额，取消够次数之后所有浏览器任务
    永久卡在排队上，日志里只有"排队中"，看不出是被谁占的。
    """
    cap = _cap(**{"browser.max_concurrent": 1, "browser.min_free_memory_gb": 999})
    task = asyncio.create_task(cap.acquire("会被取消的"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 名额还在，下一个能立刻拿到
    cap._min_free_gb = 0
    await asyncio.wait_for(cap.acquire("后来的"), timeout=2)
    assert cap.stats()["active"] == 1


@pytest.mark.asyncio
async def test_memory_brake_blocks_then_releases():
    """可用内存低于阈值时挡住，恢复后放行。"""
    cap = _cap(**{"browser.max_concurrent": 4, "browser.min_free_memory_gb": 999})
    task = asyncio.create_task(cap.acquire("等内存"))
    await asyncio.sleep(0.2)
    assert not task.done(), "内存不够却直接放行了，等于没有这道闸"
    cap._min_free_gb = 0
    await asyncio.wait_for(task, timeout=15)
    assert cap.stats()["active"] == 1


@pytest.mark.asyncio
async def test_brake_can_be_switched_off():
    """min_free_memory_gb = 0 表示关掉这道闸，不能变成永久等待。"""
    cap = _cap(**{"browser.max_concurrent": 1, "browser.min_free_memory_gb": 0})
    await asyncio.wait_for(cap.acquire("x"), timeout=2)


def test_stats_reports_what_the_incident_needed():
    """卡机那次事后查不到"当时开了几个浏览器"，只能看监控猜。"""
    stats = _cap(**{"browser.max_concurrent": 3}).stats()
    for key in ("active", "limit", "waiting", "peak",
                "memory_available_gb", "memory_total_gb"):
        assert key in stats, f"health 里缺 {key}"


# ---------------------------------------------------------------- 结构性
def test_every_browser_launch_goes_through_the_gate():
    """新增启动 Chromium 的代码时，必须同时过闸门。

    漏一处的后果不是报错，是那条路径可以无限开浏览器——
    整个闸门被绕过去，而现象和改动前一模一样（机器慢慢卡死）。
    所以这里按文件比对：谁调了 launch，谁就得 import 闸门。
    """
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "capacity.py":
            continue
        text = path.read_text(encoding="utf-8")
        launches = [
            line for line in text.splitlines()
            if ("launch_persistent_context(" in line or "chromium.launch(" in line)
            and not line.strip().startswith("#")
            and "await" in line or "= await playwright" in line
        ]
        real = [ln for ln in launches if "await" in ln]
        if real and "BROWSER_CAPACITY" not in text:
            offenders.append(path.relative_to(root).as_posix())
    assert not offenders, (
        f"这些文件会启动浏览器但没过闸门：{offenders}。"
        f"照着 page_session.py 的写法加 acquire/release。")
