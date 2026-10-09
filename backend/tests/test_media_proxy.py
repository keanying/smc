"""图片代理：绕开平台防盗链，同时不能变成一个通用的对外代理。

由来：数据中心里微博图片一片"加载失败"，但把地址复制到浏览器能正常打开。
这个现象极具迷惑性——让人以为是采到的地址不对，实际是 sinaimg 校验 Referer。
页面上的 `<meta name="referrer">` 只能解决一部分（有的 CDN 要的是自家 Referer，
而且浏览器扩展/企业策略会覆盖页面策略），所以最终由服务端代取。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.api.media import ALLOWED_HOSTS, _is_public_host, _referer_for


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host,expected_referer", [
    ("wx1.sinaimg.cn", "https://weibo.com/"),
    ("sinaimg.cn", "https://weibo.com/"),
    ("p3-pc.douyinpic.com", "https://www.douyin.com/"),
    ("sns-img-qc.xhscdn.com", "https://www.xiaohongshu.com/"),
    ("tx2.a.kwimgs.com", "https://www.kuaishou.com/"),
    ("dimg04.c-ctrip.com", ""),
])
def test_known_cdn_hosts_get_the_right_referer(host, expected_referer):
    assert _referer_for(host) == expected_referer


@pytest.mark.parametrize("host", [
    "evil.com",
    "169.254.169.254",             # 云厂商元数据接口
    "localhost",
    "sinaimg.cn.evil.com",         # 后缀匹配不能被这样绕过
    "notsinaimg.cn",               # 也不能被这样绕过
])
def test_everything_else_is_refused(host):
    """这个接口只服务于"显示采到的图片"。

    放开成通用代理就等于把内网暴露出去——云厂商的元数据接口
    （169.254.169.254）能读出临时凭据，这是最典型的 SSRF 打法。
    """
    assert _referer_for(host) is None, host


def test_suffix_match_requires_a_dot_boundary():
    """`notsinaimg.cn` 不能因为"以 sinaimg.cn 结尾"就放行。"""
    assert _referer_for("notsinaimg.cn") is None
    assert _referer_for("a.sinaimg.cn") == "https://weibo.com/"


@pytest.mark.parametrize("host,public", [
    ("127.0.0.1", False),
    ("10.0.0.5", False),
    ("192.168.1.1", False),
    ("169.254.169.254", False),
    ("8.8.8.8", True),
    ("wx1.sinaimg.cn", True),      # 不是 IP，交给白名单判断
])
def test_private_addresses_are_rejected(host, public):
    assert _is_public_host(host) is public


# ---------------------------------------------------------------------------
# 真实转发
# ---------------------------------------------------------------------------

class _FakeCdn(BaseHTTPRequestHandler):
    """模拟防盗链：Referer 不对就 403。"""

    seen_headers: dict = {}

    def do_GET(self):  # noqa: N802
        type(self).seen_headers = dict(self.headers)
        referer = self.headers.get("Referer", "")
        if self.path.startswith("/hotlink") and referer != "https://weibo.com/":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/nothtml"):
            body = b"<html>not an image</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_cdn(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeCdn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 用 localhost 而不是 127.0.0.1：写成 IP 字面量会被 _is_public_host 拦掉
    # （那一层专门挡"白名单里塞了个内网 IP"）。这也顺带说明了一件事：
    # 域名形式的内网地址挡不住，安全边界靠的是白名单本身。
    base = f"http://localhost:{server.server_port}"
    monkeypatch.setitem(ALLOWED_HOSTS, "localhost", "https://weibo.com/")
    yield base
    server.shutdown()


@pytest.fixture()
def client():
    import os
    os.environ["SMC_MYSQL_DATABASE"] = "scenic_media_media_proxy"
    os.environ["SMC_REDIS_ENABLED"] = "false"
    os.environ["SMC_SCHEDULER_ENABLED"] = "false"

    from app.core.config import reset_cache
    reset_cache()
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    reset_cache()


def test_proxy_adds_the_referer_the_cdn_wants(client, fake_cdn):
    """核心：浏览器直连拿 403，服务端带上平台自己的 Referer 就能取到。"""
    response = client.get("/api/media/image", params={"url": f"{fake_cdn}/hotlink/a.jpg"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/")
    assert response.content.startswith(b"\x89PNG")
    assert _FakeCdn.seen_headers.get("Referer") == "https://weibo.com/"
    # 图片内容不会变，让浏览器缓存，别每次翻页都回源
    assert "max-age" in response.headers.get("cache-control", "")


def test_proxy_refuses_unknown_hosts(client):
    response = client.get(
        "/api/media/image", params={"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert response.status_code == 400
    assert "不支持代理" in response.text


def test_proxy_refuses_non_http_schemes(client):
    response = client.get("/api/media/image", params={"url": "file:///etc/passwd"})
    assert response.status_code == 400


def test_proxy_refuses_non_image_responses(client, fake_cdn):
    """就算域名在白名单里，返回的不是图片也不能原样转出去。"""
    response = client.get("/api/media/image", params={"url": f"{fake_cdn}/nothtml"})
    assert response.status_code == 502
    assert "不是图片" in response.text
