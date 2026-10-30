# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主这边：盒子来的请求从这里进。

- 带 X-SMC-Box-Token 头的请求（以及所有 /api/box/*）都要认盒子令牌，认不出回 401；
- 盒子只能调这几类接口：上报 / 拉分配（/api/box/*）、数据中心的查和删、标注的查和改。
  别的（系统设置、景区管理、用户……）一律 403；
- 认出来的盒子把「分到的景区」放进景区范围（core/scope.py），查询在仓库层统一收口；
- 复核人记盒子上登录的那个人的名字（盒子转发时带 X-SMC-Box-User），不记盒子名——
  主的页面上不显示数据是哪个盒子来的。

放在最外层：盒子请求不走主的登录检查（AuthMiddleware 看到 state.box 就放过）。
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote

from ..auth.context import reset_current_user, set_current_user
from ..core.scope import reset_scope, set_scope
from .repo import get_box_repo

TOKEN_HEADER = b"x-smc-box-token"
USER_HEADER = b"x-smc-box-user"

_ALLOWED = (
    (None, re.compile(r"^/api/box/")),
    ("GET", re.compile(r"^/api/data/(trend|overview|works|comments|comments/thread|authors|export/[^/]+)$")),
    ("DELETE", re.compile(r"^/api/data/works/\d+$")),
    ("GET", re.compile(r"^/api/labeling/(status|stats|comments|comments/\d+)$")),
    ("PUT", re.compile(r"^/api/labeling/comments/\d+$")),
    ("POST", re.compile(r"^/api/labeling/comments/\d+/(reset|relabel)$")),
)


def box_allowed(method: str, path: str) -> bool:
    return any((m is None or m == method) and pattern.match(path) for m, pattern in _ALLOWED)


class BoxGateway:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = dict(scope.get("headers") or [])
        token = headers.get(TOKEN_HEADER, b"").decode("latin-1")
        if not token and not path.startswith("/api/box/"):
            return await self.app(scope, receive, send)

        from .. import edition
        repo = get_box_repo()
        if repo is None or edition.is_scenic():
            return await _reject(send, 404, "这里不是主：景区盒子要连主的地址")
        client = scope.get("client") or ("", 0)
        box = await repo.authenticate(token, client[0] or "")
        if box is None:
            return await _reject(send, 401, "盒子令牌不对，或者这个盒子已经在主上停用 / 删除了")
        method = scope.get("method", "GET")
        if not box_allowed(method, path):
            return await _reject(send, 403, "景区盒子不能调这个接口")

        who = unquote(headers.get(USER_HEADER, b"").decode("latin-1")).strip()[:50]
        user = {"id": 0, "username": f"box:{box['box_id']}",
                "display_name": who or "景区用户", "role": "operator"}
        state = scope.setdefault("state", {})
        state["box"] = box
        state["user"] = user
        user_ctx = set_current_user(user)
        scope_ctx = set_scope(box["scenic_ids"])
        try:
            return await self.app(scope, receive, send)
        finally:
            reset_scope(scope_ctx)
            reset_current_user(user_ctx)


async def _reject(send, status: int, message: str):
    body = json.dumps({"code": status, "data": None, "message": message},
                      ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
