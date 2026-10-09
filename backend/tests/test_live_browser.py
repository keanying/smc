"""验证实时流交互浏览器：能推出画面帧，能回放点击并真的改变页面。

沙箱里没有显示器，测试强制用无头模式跑——CDP screencast 在无头下同样工作，
被验证的是推流与输入回放这套管道本身。生产环境登录用有头模式。
"""
from __future__ import annotations

import asyncio
import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.browser import specs as spec_module
from app.browser.live_session import LiveBrowserSession
from app.core.subprocess_loop import SUBPROCESS_LOOP
from app.browser.manager import BrowserSessionManager
from app.browser.specs import LoginSpec
from app.core.config import load_config

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
pytestmark = pytest.mark.skipif(
    not Path(CHROMIUM).exists(), reason="沙箱 Chromium 不存在时跳过"
)

PAGE_HTML = """
<html><body style="margin:0;background:#fff">
  <h1 id="title" style="font-size:48px">未登录</h1>
  <button id="btn" style="width:300px;height:120px;font-size:32px"
          onclick="document.getElementById('title').textContent='已登录';
                   document.cookie='fake_session=live123; path=/'">
    点我登录
  </button>
</body></html>
"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def site():
    server = HTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class _StubRepo:
    def __init__(self):
        self.saved = None

    async def save_session(self, channel, account_name, *, cookies, profile_dir="", **kw):
        self.saved = {"channel": channel, "account": account_name, "cookies": cookies}

    async def mark_expired(self, *a, **kw):
        pass


@pytest.mark.asyncio
async def test_screencast_streams_frames_and_replays_click(site, monkeypatch, tmp_path):
    import os
    os.environ["SMC_BROWSER_EXECUTABLE_PATH"] = CHROMIUM
    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles")

    monkeypatch.setitem(spec_module.LOGIN_SPECS, "livetest", LoginSpec(
        channel="livetest",
        home_url=f"{site}/",
        login_url=f"{site}/",
        session_cookies=["fake_session"],
    ))

    config = load_config(use_cache=False)
    repo = _StubRepo()
    manager = BrowserSessionManager(config, repo)
    # 沙箱无显示器，强制无头
    original = manager._launch_kwargs
    monkeypatch.setattr(manager, "_launch_kwargs", lambda headless: original(True))

    frames = []
    statuses = []
    callback_loops = set()
    caller_loop = asyncio.get_running_loop()

    async def on_frame(data_b64, width, height):
        # 关键：帧是在浏览器循环里产生的，但回调必须在调用方（WebSocket）循环上执行，
        # 否则往 WebSocket 里写数据会跨循环出问题
        callback_loops.add(asyncio.get_running_loop())
        frames.append((data_b64, width, height))

    async def on_status(state, message):
        callback_loops.add(asyncio.get_running_loop())
        statuses.append((state, message))

    session = LiveBrowserSession(manager, "livetest", "acc_live")
    await session.start(on_frame=on_frame, on_status=on_status)
    try:
        # 等首帧
        for _ in range(50):
            if frames:
                break
            await asyncio.sleep(0.2)
        assert frames, "screencast 没有推出任何画面帧"

        data_b64, width, height = frames[0]
        raw = base64.b64decode(data_b64)
        assert raw[:2] == b"\xff\xd8", "帧不是合法 JPEG"
        assert len(raw) > 1000, "帧数据过小，可能是空白画面"
        assert width > 0 and height > 0

        # 回放一次点击到按钮中心（按钮在左上角，300x120，中心约 (150, 145)）
        before = len(frames)
        await session.handle_input({"type": "click", "x": 150, "y": 145})

        # 页面属于浏览器专用循环，跨循环直接 await 是错的。
        # 用轮询而不是固定 sleep：机器忙的时候 screencast 推帧可能慢到 1 秒开外，
        # 固定等待会让这条测试偶发失败（并行跑整套测试时遇到过）。
        title = ""
        for _ in range(50):
            title = await SUBPROCESS_LOOP.run(session._page.inner_text("#title"))
            if title == "已登录" and len(frames) > before:
                break
            await asyncio.sleep(0.2)

        assert title == "已登录", f"点击没有被回放到页面上，标题仍是 {title}"
        # 画面变化应该带来新帧
        assert len(frames) > before, "点击后页面变了，但没有推出新帧"

        # 后台轮询应检测到登录并保存 Cookie
        for _ in range(30):
            if session.logged_in:
                break
            await asyncio.sleep(0.5)
        assert session.logged_in, f"未检测到登录态，状态流：{statuses}"
        assert repo.saved is not None
        names = {c["name"] for c in repo.saved["cookies"]}
        assert "fake_session" in names
    finally:
        result = await session.stop()
        assert result["logged_in"] is True

    assert [s[0] for s in statuses][:3] == ["starting", "navigating", "streaming"]
    # 所有回调都必须落在调用方循环上，一个都不能跑到浏览器循环里去
    assert callback_loops == {caller_loop}, (
        f"回调跑到了别的循环上：{callback_loops}"
    )


@pytest.mark.asyncio
async def test_keyboard_input_is_replayed(site, monkeypatch, tmp_path):
    import os
    os.environ["SMC_BROWSER_EXECUTABLE_PATH"] = CHROMIUM
    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles2")

    monkeypatch.setitem(spec_module.LOGIN_SPECS, "livetest", LoginSpec(
        channel="livetest", home_url=f"{site}/", session_cookies=["nope"],
    ))
    config = load_config(use_cache=False)
    manager = BrowserSessionManager(config, _StubRepo())
    original = manager._launch_kwargs
    monkeypatch.setattr(manager, "_launch_kwargs", lambda headless: original(True))

    session = LiveBrowserSession(manager, "livetest", "acc_kb")
    await session.start(on_frame=lambda *a: asyncio.sleep(0), on_status=lambda *a: asyncio.sleep(0))
    try:
        await SUBPROCESS_LOOP.run(session._page.set_content(
            '<input id="box" style="font-size:40px;width:600px">'
        ))
        await session.handle_input({"type": "click", "x": 100, "y": 30})
        await session.handle_input({"type": "type", "text": "13800138000"})
        await asyncio.sleep(0.5)
        value = await SUBPROCESS_LOOP.run(session._page.input_value("#box"))
        assert value == "13800138000", f"键盘输入没被回放，实际值：{value}"
    finally:
        await session.stop()


# ---------------------------------------------------------------------------
# 登录弹窗（微博 PC 端的登录就是 window.open 出来的新窗口）
# ---------------------------------------------------------------------------

POPUP_HOST_HTML = """
<html><body style="margin:0;background:#eee">
  <h1 id="title" style="font-size:40px">主页面</h1>
  <button id="open" style="width:300px;height:100px;font-size:28px"
          onclick="window.open('/popup','_blank','width=600,height=500')">
    打开登录弹窗
  </button>
</body></html>
"""

POPUP_HTML = """
<html><body style="margin:0;background:#fff">
  <h1 style="font-size:56px;color:#f60">扫码登录</h1>
  <script>
    document.cookie = 'fake_session=from_popup; path=/';
  </script>
</body></html>
"""


class _PopupSite(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (POPUP_HTML if self.path.startswith("/popup")
                else POPUP_HOST_HTML).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def popup_site():
    server = HTTPServer(("127.0.0.1", 0), _PopupSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _headless_manager(config, repo, monkeypatch):
    manager = BrowserSessionManager(config, repo)
    original = manager._launch_kwargs
    monkeypatch.setattr(manager, "_launch_kwargs", lambda headless: original(True))
    return manager


@pytest.mark.asyncio
async def test_screencast_follows_the_login_popup(popup_site, monkeypatch, tmp_path):
    """登录弹窗打开后，推流和输入都要切到弹窗上。

    ⚠️ 微博 PC 端的登录是弹窗：
    `passport.weibo.com/sso/signin?...&disp=popup` 会 window.open 一个新窗口。
    推流绑死在主页面上的话，用户在界面里看到的是 weibo.com 首页，
    二维码在另一个看不见的窗口里——根本没法扫。
    """
    import os
    os.environ["SMC_BROWSER_EXECUTABLE_PATH"] = CHROMIUM
    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles")

    monkeypatch.setitem(spec_module.LOGIN_SPECS, "popuptest", LoginSpec(
        channel="popuptest",
        home_url=f"{popup_site}/",
        session_cookies=["fake_session"],
    ))

    config = load_config(use_cache=False)
    repo = _StubRepo()
    manager = _headless_manager(config, repo, monkeypatch)

    statuses = []

    async def on_frame(*a):
        pass

    async def on_status(state, message):
        statuses.append(state)

    session = LiveBrowserSession(manager, "popuptest", "acc_popup")
    await session.start(on_frame=on_frame, on_status=on_status)
    try:
        # 在主页面上点按钮弹出新窗口
        await session.handle_input({"type": "click", "x": 150, "y": 90})
        for _ in range(50):
            if session._popup is not None:
                break
            await asyncio.sleep(0.2)

        assert session._popup is not None, "没有捕获到登录弹窗"
        assert "/popup" in (session._popup.url or "")
        assert session._active_page is session._popup, "推流应该切到弹窗上"
        assert "popup" in statuses, statuses

        # 弹窗关掉之后要切回主页面
        await SUBPROCESS_LOOP.run(session._popup.close())
        for _ in range(50):
            if session._active_page is session._page:
                break
            await asyncio.sleep(0.2)
        assert session._active_page is session._page, "弹窗关了要切回主页面"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_save_now_lets_the_human_decide(popup_site, monkeypatch, tmp_path):
    """「我已登录，保存」：不依赖自动判定，人说了算。

    自动判定再准也有失手的时候，而"到底登没登上"人看一眼画面就知道。
    没有这个入口，判定一失手用户就只能干瞪眼，下次打开还得重扫。
    """
    import os
    os.environ["SMC_BROWSER_EXECUTABLE_PATH"] = CHROMIUM
    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles")

    monkeypatch.setitem(spec_module.LOGIN_SPECS, "savetest", LoginSpec(
        channel="savetest",
        home_url=f"{popup_site}/",
        session_cookies=["fake_session"],
    ))

    config = load_config(use_cache=False)
    repo = _StubRepo()
    manager = _headless_manager(config, repo, monkeypatch)

    session = LiveBrowserSession(manager, "savetest", "acc_save")
    await session.start(on_frame=lambda *a: asyncio.sleep(0),
                        on_status=lambda *a: asyncio.sleep(0))
    try:
        # 还没登录：不能存，而且要把当前有哪些 Cookie 说出来
        result = await session.save_now()
        assert result["saved"] is False
        assert "登录凭据" in result["message"]
        assert repo.saved is None

        # 打开弹窗（弹窗里会种下 fake_session），再点保存
        await session.handle_input({"type": "click", "x": 150, "y": 90})
        for _ in range(50):
            if session._popup is not None:
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)

        result = await session.save_now()
        assert result["saved"] is True, result
        assert repo.saved is not None
        assert any(c["name"] == "fake_session" for c in repo.saved["cookies"])
        assert session.logged_in is True
    finally:
        await session.stop()
