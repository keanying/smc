# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""盒子这边：页面上的数据中心、标注审核请求，原样转给主。

盒子本地不存采到的数据，查看和修改都在主上做；主按盒子令牌只给它看分到的景区
（box/gateway.py）。页面不需要知道数据在哪——还是调本机的 /api/data、/api/labeling。

放在登录检查里面：先在盒子上认人（盒子自己的用户），再转；转的时候带上这个人的名字，
主那边记复核人用。不带盒子用户的 Cookie 过去。
"""
from __future__ import annotations

import json

from ..core.logging import get_logger
from .sync import get_box_client

logger = get_logger(__name__)

PREFIXES = ("/api/data/", "/api/labeling/")
_KEEP_REQUEST = {b"content-type", b"accept", b"accept-language"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection",
                  "set-cookie"}
TIMEOUT_SECONDS = 300            # 导出、AI 重标都可能要一会儿


class BoxForwarder:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        client = get_box_client()
        if (client is None or scope["type"] != "http"
                or not scope.get("path", "").startswith(PREFIXES)):
            return await self.app(scope, receive, send)

        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        user = (scope.get("state") or {}).get("user") or {}
        headers = {k.decode("latin-1"): v.decode("latin-1")
                   for k, v in scope.get("headers") or [] if k.lower() in _KEEP_REQUEST}
        headers.update(client.headers(user.get("display_name") or user.get("username") or ""))
        url = client.master_url + (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode("latin-1")

        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5)) as http:
                resp = await http.request(scope["method"], url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("转给主 %s %s 失败：%s", scope["method"], scope["path"], exc)
            return await _reply(send, 502, f"连不上主（{client.master_url}），数据在主上，暂时看不了：{exc}")
        if resp.status_code == 401:
            # 主说令牌不对：别让页面以为是盒子上的登录过期了（页面见 401 会跳登录页）
            return await _reply(send, 502, "主不认这个盒子的令牌（可能在主上被停用或重置了），请联系主的管理员")
        content = resp.content
        out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers.multi_items()
               if k.lower() not in _DROP_RESPONSE]
        out.append((b"content-length", str(len(content)).encode()))
        await send({"type": "http.response.start", "status": resp.status_code, "headers": out})
        await send({"type": "http.response.body", "body": content})


async def _reply(send, status: int, message: str):
    content = json.dumps({"code": status, "data": None, "message": message},
                         ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(content)).encode())]})
    await send({"type": "http.response.body", "body": content})
