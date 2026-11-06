# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""推流画面的鼠标回放：前端只发按下 / 松开（带连击次数），服务端在真实 Chromium 里重放。

盯的几件事（都是线上反馈过的）：
- 一次点击不能变成两次（以前 click + mousedown/mouseup 各执行一遍，播放 / 暂停切两下等于没点）
- 双击要真的触发 dblclick
- 拖动松开在哪就是哪
- 滚轮滚的是鼠标下面那块，不是整页
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser.live_session import LiveBrowserSession   # noqa: E402
from app.browser.live_view import LiveView                 # noqa: E402
from app.browser.remote_input import click_count, mouse_button  # noqa: E402


def _chromium() -> str:
    """找一个能用的 Chromium：先用 Playwright 自己装的，再找沙箱预装的。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and Path(path).exists():
                return path
    except Exception:  # noqa: BLE001
        pass
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else ""


CHROMIUM = _chromium()

PAGE = """<!doctype html><html><body style="margin:0;height:3000px">
<button id="b" style="position:absolute;left:20px;top:20px;width:200px;height:80px">播放</button>
<div id="box" style="position:absolute;left:300px;top:20px;width:300px;height:200px;overflow:auto">
  <div style="height:2000px">长内容</div>
</div>
<div id="track" style="position:absolute;left:20px;top:300px;width:600px;height:40px;background:#eee"></div>
<script>
  window.L = [];
  const b = document.getElementById('b');
  ['click', 'dblclick'].forEach(t => b.addEventListener(t, e => L.push(t + ':' + e.detail)));
  const tr = document.getElementById('track');
  tr.addEventListener('mousedown', e => L.push('down:' + e.clientX));
  window.addEventListener('mouseup', e => L.push('up:' + e.clientX));
</script></body></html>"""


def test_arg_normalizing():
    assert mouse_button({"button": "right"}) == "right"
    assert mouse_button({"button": "weird"}) == "left"
    assert mouse_button({}) == "left"
    assert click_count({"clickCount": 2}) == 2
    assert click_count({"clickCount": 9}) == 3
    assert click_count({"clickCount": "x"}) == 1
    assert click_count({}) == 1


class _Mouse:
    def __init__(self):
        self.calls = []

    async def move(self, x, y, **kw):
        self.calls.append(("move", x, y))

    async def down(self, **kw):
        self.calls.append(("down", kw.get("button"), kw.get("click_count")))

    async def up(self, **kw):
        self.calls.append(("up", kw.get("button"), kw.get("click_count")))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


@pytest.mark.asyncio
async def test_live_view_passes_click_count_and_scrolls_under_the_pointer():
    page = SimpleNamespace(mouse=_Mouse(), keyboard=None,
                           viewport_size={"width": 1280, "height": 800}, url="http://x")
    view = LiveView(view_id="v", page=page, context=None, task_id="T", task_name="n",
                    channel="douyin", account_name="a", scenic_name="s")
    view._last_frame = {"width": 640, "height": 400}       # 帧是视口的一半
    view.takeover = True
    await view._handle_input({"type": "mousedown", "x": 10, "y": 20, "button": "left", "clickCount": 2})
    await view._handle_input({"type": "mouseup", "x": 10, "y": 20, "button": "left", "clickCount": 2})
    await view._handle_input({"type": "wheel", "x": 100, "y": 50, "deltaX": 0, "deltaY": 120})
    assert page.mouse.calls == [
        ("move", 20.0, 40.0), ("down", "left", 2),
        ("move", 20.0, 40.0), ("up", "left", 2),
        ("move", 200.0, 100.0), ("wheel", 0.0, 120.0),       # 先移到鼠标位置再滚
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(not CHROMIUM, reason="没有可用的 Chromium")
async def test_replay_in_real_chromium():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROMIUM)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.set_content(PAGE)
        session = LiveBrowserSession.__new__(LiveBrowserSession)
        session._page = session._active_page = page
        session._closed = False
        send = session._handle_input

        # 1) 单击：前端发的就是 按下 + 松开，页面只能收到一次 click
        await send({"type": "mousedown", "x": 100, "y": 60, "button": "left", "clickCount": 1})
        await send({"type": "mouseup", "x": 100, "y": 60, "button": "left", "clickCount": 1})
        clicks = "L.filter(x => x.startsWith('click') || x.startsWith('dblclick'))"
        assert await page.evaluate(clicks) == ["click:1"]

        # 2) 双击：第二次按下带 clickCount=2，要触发 dblclick
        await page.evaluate("L.length = 0")
        await page.wait_for_timeout(600)                 # 和上一次单击分开，免得被算成三连击
        for n in (1, 2):
            await send({"type": "mousedown", "x": 100, "y": 60, "clickCount": n})
            await send({"type": "mouseup", "x": 100, "y": 60, "clickCount": n})
        assert await page.evaluate(clicks) == ["click:1", "click:2", "dblclick:2"]

        # 3) 拖动：按下 → 移动 → 在终点松开，松开的位置就是终点
        await page.evaluate("L.length = 0")
        await send({"type": "mousedown", "x": 40, "y": 320, "clickCount": 1})
        for x in range(60, 500, 40):
            await send({"type": "mousemove", "x": x, "y": 321})
        await send({"type": "mouseup", "x": 512, "y": 322, "clickCount": 1})
        assert await page.evaluate("L") == ["down:40", "up:512"]

        # 4) 滚轮：在右边那个可滚动的框上滚，滚的是框，不是整页
        await send({"type": "wheel", "x": 450, "y": 120, "deltaX": 0, "deltaY": 300})
        await page.wait_for_timeout(300)
        box_top = await page.evaluate("document.getElementById('box').scrollTop")
        page_top = await page.evaluate("window.scrollY")
        assert box_top > 0 and page_top == 0, (box_top, page_top)

        await browser.close()
