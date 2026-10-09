"""采集节奏（频率控制）：每个平台一套，可配置。

为什么要有这个
--------------
拟人采集慢是**故意的**——真人不会一秒点开五条视频。但"慢多少"不该
写死在采集器里：不同平台的风控松紧不一样，同一个平台在不同时段、
不同账号下的容忍度也不一样。用户实跑反馈「快手有些时候会等待很长时间」，
而这个等待长度以前散落在各个采集器的 `random.uniform(...)` 里，
既看不出来也改不了。

现在统一到 `crawl.pace`，按平台覆盖：

    crawl:
      pace:
        default:            # 没单独配的平台走这个
          work_seconds: [1.0, 8.0]      # 采完一条作品，停多久再看下一条
          scroll_seconds: [0.6, 1.2]    # 每次滚动之间
          action_seconds: [0.3, 0.9]    # 点击/输入这类小动作之间
        kuaishou:
          work_seconds: [1.0, 4.0]      # 快手可以快一点
        xiaohongshu:
          work_seconds: [2.0, 10.0]     # 小红书风控紧，慢一点

三个档位而不是一个总开关，是因为它们的量级差着一个数量级：
作品之间是"秒"级、滚动是"零点几秒"级。混成一个数，要么滚动慢得离谱，
要么作品之间快得不像真人。

停顿是**分段睡**的（每段最多 0.5 秒），中间检查取消信号 ——
否则用户点了停止，要等这一觉睡完（最长 10 秒）才有反应。
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Optional, Tuple

from .logging import get_logger

logger = get_logger(__name__)

#: 一次睡多久检查一次取消信号
NAP_SECONDS = 0.5

#: 兜底默认值。配置里没写、或者写坏了都用它。
DEFAULT_PACE: Dict[str, Tuple[float, float]] = {
    "work_seconds": (1.0, 8.0),
    "scroll_seconds": (0.6, 1.2),
    "action_seconds": (0.3, 0.9),
}

#: 各平台的出厂默认。用户在 config.yaml / 系统设置里改的会覆盖它。
#: 这些值来自实跑观感，不是玄学：小红书风控最紧、微博最松。
CHANNEL_DEFAULTS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "xiaohongshu": {"work_seconds": (2.0, 10.0)},
    "kuaishou":    {"work_seconds": (1.0, 5.0)},
    "douyin":      {"work_seconds": (1.5, 6.0)},
    "weibo":       {"work_seconds": (0.8, 3.0)},
}


def _as_range(value: Any, fallback: Tuple[float, float]) -> Tuple[float, float]:
    """把配置里的值读成 (低, 高)。

    容忍三种写法：[1, 8] / 5（固定值）/ {"min": 1, "max": 8}。
    读不出来就用兜底值——**配置写错不该让采集直接停**，
    但要在日志里说清楚，否则用户改了没生效也不知道。
    """
    try:
        if isinstance(value, dict):
            low = float(value.get("min", fallback[0]))
            high = float(value.get("max", fallback[1]))
        elif isinstance(value, (list, tuple)):
            if not value:            # 写成空列表：当没配
                return fallback
            if len(value) == 1:
                low = high = float(value[0])
            else:
                low, high = float(value[0]), float(value[1])
        elif isinstance(value, (int, float)):
            low = high = float(value)
        else:
            return fallback
    except (TypeError, ValueError):
        logger.warning("[节奏] 配置值读不出来：%r，改用默认 %s", value, fallback)
        return fallback
    if low < 0 or high < 0:
        return fallback
    if low > high:
        low, high = high, low
    return (low, high)


class Pacer:
    """一个平台的节奏控制器。

    ⚠️ 不要在采集器里再写裸的 `asyncio.sleep(random.uniform(...))`——
    那种停顿用户既看不见也改不掉，正是"有时候等很久"说不清的原因。
    """

    def __init__(self, config: Any, channel: str):
        self.channel = channel
        crawl = {}
        try:
            crawl = getattr(config, "crawl", None) or {}
        except Exception:  # noqa: BLE001
            crawl = {}
        pace_cfg = crawl.get("pace") or {}
        base = dict(DEFAULT_PACE)
        base.update(CHANNEL_DEFAULTS.get(channel, {}))
        merged = dict(base)
        for scope in (pace_cfg.get("default") or {}, pace_cfg.get(channel) or {}):
            if not isinstance(scope, dict):
                continue
            for key, value in scope.items():
                if key in DEFAULT_PACE:
                    merged[key] = _as_range(value, base.get(key, DEFAULT_PACE[key]))
        self.ranges: Dict[str, Tuple[float, float]] = {
            key: _as_range(merged.get(key), DEFAULT_PACE[key]) for key in DEFAULT_PACE
        }
        #: 「洗牌袋」：把区间等分成 8 份打乱了轮流用。
        #: 纯随机会出现连着三次都停 7 秒这种扎眼的巧合；
        #: 洗牌袋保证 8 次之内长短都摊到，看起来更像人。
        self._bags: Dict[str, List[float]] = {}

    def describe(self) -> str:
        """一行说明，任务开始时打进日志——用户改了配置能立刻确认生效。"""
        w = self.ranges["work_seconds"]
        s = self.ranges["scroll_seconds"]
        a = self.ranges["action_seconds"]
        return (f"采集节奏：作品之间 {w[0]:.1f}~{w[1]:.1f}s，"
                f"滚动 {s[0]:.1f}~{s[1]:.1f}s，动作 {a[0]:.1f}~{a[1]:.1f}s")

    def next_delay(self, kind: str = "work_seconds") -> float:
        low, high = self.ranges.get(kind, DEFAULT_PACE["work_seconds"])
        if high <= low:
            return max(0.0, low)
        bag = self._bags.get(kind)
        if not bag:
            step = (high - low) / 7
            bag = [low + step * i for i in range(8)]
            random.shuffle(bag)
            self._bags[kind] = bag
        base = bag.pop()
        jitter = (high - low) * 0.05
        return max(0.0, base + random.uniform(-jitter, jitter))

    async def wait(self, kind: str = "work_seconds", ctx: Any = None,
                   note: str = "") -> float:
        """停一下。**分段睡**，中间检查取消信号。

        返回实际停的秒数，方便调用方打日志或统计。
        """
        delay = self.next_delay(kind)
        remaining = delay
        while remaining > 0:
            if ctx is not None:
                check = getattr(ctx, "raise_if_cancelled", None)
                if callable(check):
                    check()
            nap = min(NAP_SECONDS, remaining)
            await asyncio.sleep(nap)
            remaining -= nap
        if note and delay >= 1.0:
            logger.info("[%s] 停 %.1f 秒%s", self.channel, delay, note)
        return delay


def make_pacer(config: Any, channel: str) -> Pacer:
    return Pacer(config, channel)
