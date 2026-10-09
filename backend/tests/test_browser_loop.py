"""浏览器专用事件循环的回归测试。

背景：Windows 上 uvicorn 带 --reload 或 --workers>1 时会把事件循环策略切成
SelectorEventLoop，而它不支持创建子进程，Playwright 启动浏览器直接抛
NotImplementedError。这组测试锁住修复方式：**所有 Playwright 调用都必须
在专用循环（playwright-loop 线程）上执行，与服务器循环无关**。

只要有人把 Playwright 调用改回服务器循环上直接跑，这里就会失败。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from app.browser.loop import BROWSER_LOOP, BrowserLoop, current_loop_supports_subprocess

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


@pytest.mark.asyncio
async def test_runs_on_dedicated_thread():
    """提交的协程必须落在 playwright-loop 线程上，而不是调用方线程。"""
    caller_thread = threading.current_thread().name
    caller_loop = asyncio.get_running_loop()

    async def probe():
        return threading.current_thread().name, asyncio.get_running_loop()

    worker_thread, worker_loop = await BROWSER_LOOP.run(probe())

    assert worker_thread == "playwright-loop", f"跑在了 {worker_thread}"
    assert worker_thread != caller_thread
    assert worker_loop is not caller_loop, "复用了调用方的循环，等于没修"


@pytest.mark.asyncio
async def test_returns_values_and_propagates_exceptions():
    async def ok():
        return 42

    async def boom():
        raise ValueError("来自浏览器循环的错误")

    assert await BROWSER_LOOP.run(ok()) == 42
    with pytest.raises(ValueError, match="来自浏览器循环的错误"):
        await BROWSER_LOOP.run(boom())


@pytest.mark.asyncio
async def test_to_caller_loop_delivers_back():
    """浏览器循环里产生的回调要能安全地送回调用方循环执行。"""
    caller_loop = asyncio.get_running_loop()
    seen: list[tuple[str, object]] = []

    async def callback(value: str) -> None:
        seen.append((threading.current_thread().name, asyncio.get_running_loop()))

    forward = BROWSER_LOOP.to_caller_loop(callback)

    async def inside_browser_loop():
        # 确认自己确实在浏览器循环里，然后往回投递
        assert threading.current_thread().name == "playwright-loop"
        await forward("hello")

    await BROWSER_LOOP.run(inside_browser_loop())

    assert len(seen) == 1
    thread_name, loop = seen[0]
    assert loop is caller_loop, "回调没有回到调用方循环"
    assert thread_name != "playwright-loop"


def test_windows_uses_proactor_loop():
    """Windows 上必须显式用 ProactorEventLoop —— 只有它能开子进程。"""
    loop = BrowserLoop._new_loop()
    try:
        if sys.platform == "win32":
            assert type(loop).__name__ == "ProactorEventLoop"
        else:
            assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_survives_caller_loop_that_cannot_spawn_subprocess(monkeypatch):
    """模拟 Windows Selector 循环：调用方循环开子进程会抛 NotImplementedError，
    但通过 BROWSER_LOOP 提交的 Playwright 任务照样能跑起来。"""
    caller_loop = asyncio.get_running_loop()

    async def _refuse(*args, **kwargs):
        raise NotImplementedError

    # 把调用方循环打残：它现在开不了子进程
    monkeypatch.setattr(caller_loop, "_make_subprocess_transport", _refuse, raising=False)
    monkeypatch.setattr(caller_loop, "subprocess_exec", _refuse, raising=False)

    with pytest.raises(NotImplementedError):
        await caller_loop.subprocess_exec(asyncio.SubprocessProtocol, "echo", "hi")

    # 浏览器循环不受影响
    async def spawn():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('ok')",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        return stdout.decode().strip()

    assert await BROWSER_LOOP.run(spawn()) == "ok"


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="沙箱缺 Chromium")
@pytest.mark.asyncio
async def test_playwright_launches_through_browser_loop():
    """真起一次 Chromium，确认整条链路在专用循环上可用。"""
    async def launch():
        from playwright.async_api import async_playwright

        assert threading.current_thread().name == "playwright-loop"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, executable_path=CHROMIUM,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            version = browser.version
            await browser.close()
            return version

    version = await BROWSER_LOOP.run(launch())
    assert version, "没拿到浏览器版本"


@pytest.mark.asyncio
async def test_silent_refresh_uses_browser_loop(monkeypatch, tmp_path):
    """BrowserSessionManager 的 Playwright 部分必须走专用循环，
    数据库部分必须留在调用方循环——两者绝不能互换。"""
    import os

    from app.browser import specs as spec_module
    from app.browser.manager import BrowserSessionManager
    from app.browser.specs import LoginSpec
    from app.core.config import load_config

    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles")
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "looptest", LoginSpec(
        channel="looptest", home_url="http://127.0.0.1:1/", session_cookies=["x"],
    ))

    caller_loop = asyncio.get_running_loop()
    where = {}

    class _Repo:
        async def save_session(self, channel, account_name, *, cookies, profile_dir="", **kw):
            where["db"] = (threading.current_thread().name, asyncio.get_running_loop())

        async def mark_expired(self, *a, **kw):
            where["db"] = (threading.current_thread().name, asyncio.get_running_loop())

    manager = BrowserSessionManager(load_config(use_cache=False), _Repo())

    async def fake_grab(spec, directory):
        where["browser"] = (threading.current_thread().name, asyncio.get_running_loop())
        return [{"name": "x", "value": "1"}]

    monkeypatch.setattr(manager, "_grab_cookies", fake_grab)
    await manager.silent_refresh("looptest", "acc")

    assert where["browser"][0] == "playwright-loop", "浏览器动作没走专用循环"
    assert where["db"][1] is caller_loop, "数据库操作跑到别的循环上了，连接池会坏掉"
