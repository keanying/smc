# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""登录检查和权限检查：所有 /api、/ws 请求都过这里。

- 没登录：接口回 401，WebSocket 直接拒绝握手；
- 登录了但角色不够：回 403（规则见 roles.py）；
- 认出来的用户放进 contextvar，接口里记「谁做的」时用（context.py）。
前端静态文件、登录本身、健康检查不用登录。
"""
from __future__ import annotations

import json

from .context import reset_current_user, set_current_user
from .repo import split_cookie
from .roles import denied_reason
from .service import get_auth

INTERNAL_HEADER = b"x-smc-cluster-token"
PUBLIC_PATHS = ("/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/health")


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        auth = get_auth()
        kind = scope["type"]
        path = scope.get("path", "")
        if (auth is None or not auth.enabled or kind not in ("http", "websocket")
                or not (path.startswith("/api/") or path.startswith("/ws/"))
                or path in PUBLIC_PATHS):
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        token = headers.get(INTERNAL_HEADER, b"").decode("latin-1") or split_cookie(
            headers.get(b"cookie", b"").decode("latin-1"), auth.cookie_name)
        user = await auth.authenticate(token)
        if user is None:
            return await _reject(kind, scope, receive, send, 401, "请先登录")
        reason = denied_reason(user["role"], scope.get("method", "GET"), path,
                               websocket=kind == "websocket")
        if reason:
            return await _reject(kind, scope, receive, send, 403, reason)

        scope.setdefault("state", {})["user"] = user
        ctx = set_current_user(user)
        try:
            return await self.app(scope, receive, send)
        finally:
            reset_current_user(ctx)


async def _reject(kind, scope, receive, send, status: int, message: str):
    if kind == "websocket":
        await receive()                       # websocket.connect
        # 握手前关闭 = 浏览器那边收到 403；前端据此跳登录页
        await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403,
                    "reason": message[:100]})
        return
    body = json.dumps({"code": status, "data": None, "message": message},
                      ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
