"""实时画面的人工接管。

这个模块原来是**刻意只读**的（理由见 live_view.py 模块注释：观众随手点一下，
采集器的下一步就落空）。开接管是因为采集跑到一半弹**拖动验证**时，只读会把
人堵死：页面等人拖、采集器等页面，最后耗到看门狗把任务杀掉。

所以这里锁的全是"闸门"：没开接管就一个事件都不能过。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser.live_view import LiveView          # noqa: E402


class _FakeMouse:
    def __init__(self):
        self.calls = []

    async def move(self, x, y, **kw):
        self.calls.append(("move", x, y))

    async def down(self, **kw):
        self.calls.append(("down", kw.get("button", "left")))

    async def up(self, **kw):
        self.calls.append(("up", kw.get("button", "left")))

    async def click(self, x, y, **kw):
        self.calls.append(("click", x, y))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class _FakeKeyboard:
    def __init__(self):
        self.calls = []

    async def press(self, key):
        self.calls.append(("press", key))

    async def type(self, text, delay=0):
        self.calls.append(("type", text))


def _view(viewport=(1280, 800), frame=(1280, 800)):
    page = SimpleNamespace(
        mouse=_FakeMouse(), keyboard=_FakeKeyboard(),
        viewport_size={"width": viewport[0], "height": viewport[1]},
        url="http://x", title=lambda: "t",
    )
    v = LiveView(view_id="v", page=page, context=None, task_id="T",
                 task_name="n", channel="douyin", account_name="a",
                 scenic_name="s")
    v._last_frame = {"width": frame[0], "height": frame[1]}
    return v, page


async def _send(view, *events):
    for e in events:
        await view._handle_input(e) if view.takeover else await view.handle_input(e)


# ---------------------------------------------------------------- 闸门
@pytest.mark.asyncio
async def test_input_is_dropped_when_not_taken_over():
    """默认只读：手滑点一下画面不能有任何事。"""
    view, page = _view()
    assert view.takeover is False
    await view.handle_input({"type": "click", "x": 10, "y": 10})
    await view.handle_input({"type": "mousedown", "x": 10, "y": 10})
    assert page.mouse.calls == []
    assert view.injected == 0


@pytest.mark.asyncio
async def test_input_works_after_takeover():
    view, page = _view()
    view.set_takeover(True)
    await view.handle_input({"type": "click", "x": 100, "y": 50})
    assert ("click", 100.0, 50.0) in page.mouse.calls
    assert view.injected == 1


@pytest.mark.asyncio
async def test_input_stops_again_after_release():
    view, page = _view()
    view.set_takeover(True)
    await view.handle_input({"type": "click", "x": 1, "y": 1})
    view.set_takeover(False)
    before = len(page.mouse.calls)
    await view.handle_input({"type": "click", "x": 2, "y": 2})
    assert len(page.mouse.calls) == before


def test_injected_count_survives_release():
    """退出接管**不清**计数——那是这一轮的审计数据，
    采集出问题时要靠它回答"有没有人动过页面"。"""
    view, _ = _view()
    view.set_takeover(True)
    view.injected = 7
    view.set_takeover(False)
    assert view.injected == 7


# ---------------------------------------------------------------- 坐标换算
@pytest.mark.asyncio
async def test_coords_are_scaled_from_frame_to_viewport():
    """⚠️ 视口比推流上限大时，CDP 会把画面缩小再发，帧坐标 ≠ 视口坐标。

    不换算的话点击会整体偏移，现象是"点了没反应"或者"点到旁边的东西"，
    极难往"坐标没换算"上想。
    """
    view, page = _view(viewport=(1920, 1200), frame=(1280, 800))
    view.set_takeover(True)
    await view.handle_input({"type": "click", "x": 640, "y": 400})
    # 640 * (1920/1280) = 960；400 * (1200/800) = 600
    assert ("click", 960.0, 600.0) in page.mouse.calls


@pytest.mark.asyncio
async def test_no_frame_info_falls_back_to_raw_coords():
    """还没收到过帧时不瞎缩放——原样传，至少 1:1 的情形是对的。"""
    view, page = _view()
    view._last_frame = None
    view.set_takeover(True)
    await view.handle_input({"type": "click", "x": 33, "y": 44})
    assert ("click", 33.0, 44.0) in page.mouse.calls


# ---------------------------------------------------------------- 拖拽
@pytest.mark.asyncio
async def test_drag_produces_a_real_trajectory():
    """滑块验证看的是**轨迹**：只有起点终点等于瞬移，必然判机器。

    所以中间的每个 mousemove 都要落到 page.mouse.move 上。
    """
    view, page = _view()
    view.set_takeover(True)
    await view.handle_input({"type": "mousedown", "x": 10, "y": 10})
    for i in range(1, 21):
        await view.handle_input({"type": "mousemove", "x": 10 + i * 5, "y": 10})
    await view.handle_input({"type": "mouseup", "x": 110, "y": 10})

    moves = [c for c in page.mouse.calls if c[0] == "move"]
    assert len(moves) >= 20, f"轨迹点只有 {len(moves)} 个，滑块过不了"
    assert page.mouse.calls[0][0] == "move"      # down 之前先移到位
    assert ("down", "left") in page.mouse.calls
    assert ("up", "left") in page.mouse.calls


@pytest.mark.asyncio
async def test_mouseup_moves_to_final_position_first():
    """松手前先移到最终位置：不然滑块停在差几像素的地方，
    判定不过而且完全看不出为什么。"""
    view, page = _view()
    view.set_takeover(True)
    await view.handle_input({"type": "mouseup", "x": 500, "y": 60})
    assert page.mouse.calls[0] == ("move", 500.0, 60.0)
    assert page.mouse.calls[1] == ("up", "left")


# ---------------------------------------------------------------- 键盘
@pytest.mark.asyncio
async def test_keyboard_events():
    view, page = _view()
    view.set_takeover(True)
    await view.handle_input({"type": "type", "text": "AB12"})
    await view.handle_input({"type": "key", "key": "Enter"})
    assert ("type", "AB12") in page.keyboard.calls
    assert ("press", "Enter") in page.keyboard.calls


@pytest.mark.asyncio
async def test_keyboard_is_gated_too():
    view, page = _view()
    await view.handle_input({"type": "type", "text": "偷偷打字"})
    assert page.keyboard.calls == []


# ---------------------------------------------------------------- 健壮性
@pytest.mark.asyncio
async def test_injection_failure_does_not_crash_the_stream():
    """页面正在导航、或者采集器刚好把它关了——注入失败不能把推流带崩。"""
    view, page = _view()

    async def boom(*a, **k):
        raise RuntimeError("Target closed")
    page.mouse.click = boom
    view.set_takeover(True)
    await view.handle_input({"type": "click", "x": 1, "y": 1})   # 不抛
    assert view.injected == 0


@pytest.mark.asyncio
async def test_unknown_event_type_is_ignored():
    view, page = _view()
    view.set_takeover(True)
    await view.handle_input({"type": "刪除硬盘"})
    assert page.mouse.calls == [] and view.injected == 0


def test_takeover_state_is_recorded_for_the_ui():
    """前端按钮要以**服务端**状态为准显示。

    两边分开是有原因的：连接断了服务端会自动退出接管，只看本地状态的话
    按钮还显示"已接管"，用户以为在操作、其实每一下都被丢弃。
    """
    view, _ = _view()
    assert view.takeover is False and view.takeover_at == 0.0
    view.set_takeover(True)
    assert view.takeover is True and view.takeover_at > 0
    view.set_takeover(False)
    assert view.takeover is False and view.takeover_at == 0.0
