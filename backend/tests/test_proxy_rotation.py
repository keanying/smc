"""验证 ProxiedClient 的核心承诺：被限流时自动换 IP 并重试成功。

用本地 HTTP 服务器模拟目标站点，用假的 pool 模拟快代理，
不依赖任何外网，可在 CI 里稳定跑。
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core.config import load_config
from app.core.redis_client import RedisClient
from app.proxy.manager import ProxiedClient, ProxyManager


class _FlakyHandler(BaseHTTPRequestHandler):
    """前 N 次返回 429（模拟出口 IP 被限流），之后返回 200。"""

    fail_times = 2
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        if type(self).hits <= type(self).fail_times:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"code":200,"result":{"items":[{"id":1}]}}')

    def log_message(self, *args):  # 静音
        pass


@pytest.fixture()
def flaky_server():
    _FlakyHandler.hits = 0
    server = HTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api"
    server.shutdown()


class _FakePool:
    """假的快代理池：不联网，每次给一个递增的假 IP。"""

    def __init__(self):
        self.counter = 0
        self.invalidations = []

    async def get_bundle(self, force_new: bool = False):
        from app.proxy.fingerprint import pick_profile
        self.counter += 1
        # 返回 None 当 proxy_url，让 httpx 直连本地测试服务器；
        # 我们要验证的是"换 IP 这个动作有没有发生"，不是代理链路本身
        return None, pick_profile(platform="mobile")

    def mark_invalid(self, reason: str = ""):
        self.invalidations.append(reason)

    def current_ip(self):
        return f"10.0.0.{self.counter}:8000"

    def remaining_ttl(self):
        return 600


@pytest.mark.asyncio
async def test_rotates_ip_and_succeeds_after_429(flaky_server, monkeypatch):
    config = load_config(use_cache=False)
    redis = RedisClient(config)
    manager = ProxyManager(config, redis)

    fake_pool = _FakePool()
    async def _get_pool(channel):
        return fake_pool
    monkeypatch.setattr(manager, "get_pool", _get_pool)

    async with ProxiedClient(config, manager, "ctrip", max_retries=5) as client:
        data = await client.get_json(flaky_server)

    assert data["code"] == 200
    assert data["result"]["items"] == [{"id": 1}]
    # 两次 429 应触发两次弃用当前 IP
    assert len(fake_pool.invalidations) == 2, fake_pool.invalidations
    assert all("429" in reason for reason in fake_pool.invalidations)


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(flaky_server, monkeypatch):
    """一直 429 时应抛 ProxyBlocked，而不是无限重试或静默返回空。"""
    from app.proxy.pool import ProxyBlocked

    _FlakyHandler.fail_times = 999
    try:
        config = load_config(use_cache=False)
        manager = ProxyManager(config, RedisClient(config))
        fake_pool = _FakePool()
        async def _get_pool(channel):
            return fake_pool
        monkeypatch.setattr(manager, "get_pool", _get_pool)

        async with ProxiedClient(config, manager, "ctrip", max_retries=3) as client:
            with pytest.raises(ProxyBlocked):
                await client.get_json(flaky_server)
        assert len(fake_pool.invalidations) == 3
    finally:
        _FlakyHandler.fail_times = 2


@pytest.mark.asyncio
async def test_fingerprint_changes_with_ip(monkeypatch):
    """换 IP 必须同时换指纹，不能出现"新 IP 老指纹"。"""
    config = load_config(use_cache=False)
    manager = ProxyManager(config, RedisClient(config))
    fake_pool = _FakePool()
    async def _get_pool(channel):
        return fake_pool
    monkeypatch.setattr(manager, "get_pool", _get_pool)

    async with ProxiedClient(config, manager, "ctrip") as client:
        seen = set()
        for _ in range(20):
            seen.add(client.profile_name)
            await client.rotate("测试轮换")
        # 20 次轮换应覆盖到多个移动端指纹
        assert len(seen) > 1, f"指纹没有跟着轮换：{seen}"
        assert all("Safari" in n or "Chrome" in n for n in seen)
