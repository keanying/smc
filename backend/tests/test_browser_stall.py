# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""拟人采集「停在那儿不动」：浏览器动作不能吃 Playwright 30 秒的默认超时。

实跑（2026-11-07 快手「北京故宫」）：关完联播到「没找到评论按钮」隔了 1 分 42 秒，
之后每翻一轮评论又空等半分钟——都是 `locator.bounding_box()` 没传 timeout，
要找的区域不在页面上时每次等满 30 秒。

这里的假 locator **不传 timeout 就真的等 30 秒**（和 Playwright 一样），
所以只要有人把超时去掉，这些测试就会超时失败。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.collectors.base import CollectContext, WorkItem
from app.collectors.kuaishou_browser import KuaishouBrowserCollector


class _Missing:
    """页面上不存在的元素：和 Playwright 一样，不给 timeout 就等满 30 秒才报错。"""

    async def bounding_box(self, timeout=None):
        await asyncio.sleep(30 if timeout is None else timeout / 1000)
        raise TimeoutError("Timeout exceeded")

    @property
    def first(self):
        return self

    def nth(self, _):
        return self


class _Mouse:
    def __init__(self):
        self.moves = []

    async def move(self, x, y, steps=1):
        self.moves.append((x, y))


class _Page:
    def __init__(self, locators=None):
        self.locators = locators or {}
        self.mouse = _Mouse()

    def locator(self, selector):
        return self.locators.get(selector, _Missing())


class _Ctx:
    def __init__(self):
        self.logs = []

    def log(self, message, level="info"):
        self.logs.append((level, message))


def _collector(page=None, ctx=None):
    c = KuaishouBrowserCollector.__new__(KuaishouBrowserCollector)
    c._page = page or _Page()
    c._ctx = ctx
    c._comment_photo_id = ""
    c._comment_photo_ids = set()
    return c


async def _quick(coro, limit=6.0):
    started = time.monotonic()
    await asyncio.wait_for(coro, timeout=limit)
    return time.monotonic() - started


async def test_box_gives_up_fast_when_element_missing():
    c = _collector()
    assert await asyncio.wait_for(c._box(_Missing()), timeout=3) is None


@pytest.mark.parametrize("step", ["_park_over_comments", "_park_over_list", "_hover_autoplay"])
async def test_mouse_parking_does_not_wait_30_seconds(step):
    # 评论面板没打开、hover 控件条收起了、列表区没找到：都要很快放弃
    spent = await _quick(getattr(_collector(), step)())
    assert spent < 6, f"{step} 等了 {spent:.1f} 秒"


async def test_open_comments_without_button_gives_up_in_seconds():
    """「没找到评论按钮」要在几秒内说出来，而不是一分多钟。"""
    c = _collector(ctx=_Ctx())

    async def never_visible(locator, timeout_s=8.0):
        await asyncio.sleep(min(timeout_s, 0.05))
        return False

    c._wait_visible = never_visible
    spent = await _quick(c._open_comments(), limit=10)
    assert spent < 6
    assert any("没找到评论按钮" in m for _, m in c._ctx.logs)


async def test_slow_step_is_reported_in_task_log(monkeypatch):
    ctx = _Ctx()
    c = _collector(ctx=ctx)
    c.channel = "kuaishou"
    monkeypatch.setattr(KuaishouBrowserCollector, "SLOW_STEP_SECONDS", 0.05)

    async def slow_step():
        await asyncio.sleep(0.1)
        return "done"

    assert await c._run(slow_step()) == "done"
    assert any(level == "warn" and "slow_step" in m and "卡过" in m for level, m in ctx.logs)


async def test_fast_steps_are_not_reported():
    ctx = _Ctx()
    c = _collector(ctx=ctx)
    c.channel = "kuaishou"

    async def fast():
        return 1

    await c._run(fast())
    assert ctx.logs == []


def _work(work_id="3xp8866jj4c997c"):
    return WorkItem(channel="kuaishou", work_id=work_id)


def test_no_false_alarm_when_neighbour_comments_were_prefetched():
    """这条的评论接口出现过，后面又冒出相邻视频的 photoId：不是点错了。"""
    ctx = _Ctx()
    c = _collector()
    c._comment_photo_ids = {"3xp8866jj4c997c", "3xe35tm3yeyqdwa"}
    c._comment_photo_id = "3xe35tm3yeyqdwa"           # 最后一次是邻居的
    c._check_opened_work(ctx, _work())
    assert ctx.logs == []


def test_warns_when_this_work_never_appeared():
    ctx = _Ctx()
    c = _collector()
    c._comment_photo_ids = {"3xe35tm3yeyqdwa"}
    c._comment_photo_id = "3xe35tm3yeyqdwa"
    c._check_opened_work(ctx, _work())
    assert ctx.logs and "3xe35tm3yeyqdwa" in ctx.logs[0][1]


def test_collect_context_still_constructs():
    # 防止上面的假对象和真 ctx 的接口走样
    assert CollectContext(scenic_id="s", scenic_name="n", task_id="t").log
