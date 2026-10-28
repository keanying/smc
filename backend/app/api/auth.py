# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""登录 API：登录、退出、改自己的密码、第一次使用时创建管理员。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request, Response

from ..auth import AuthError, current_user, get_auth
from ..auth.passwords import check_strength, verify_password
from ..auth.repo import split_cookie
from ..auth.roles import ROLE_LABELS
from .common import bad_request, ok

router = APIRouter(prefix="/api/auth", tags=["登录"])


def _auth():
    auth = get_auth()
    if auth is None:
        raise bad_request("服务还没启动完")
    return auth


def client_ip(request: Request) -> str:
    """经过 nginx 时取 X-Forwarded-For 的第一个，否则取直连地址。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _set_cookie(response: Response, token: str) -> None:
    auth = _auth()
    response.set_cookie(
        auth.cookie_name, token, max_age=auth.session_hours * 3600, httponly=True,
        samesite="lax", secure=bool(auth.config.get("auth.cookie_secure", False)), path="/")


def _user_out(user):
    return {**user, "role_label": ROLE_LABELS.get(user["role"], user["role"])}


@router.get("/status")
async def status(request: Request):
    """不用登录：页面打开时问一下——要不要登录、是不是第一次用（还没有管理员）、我是谁。"""
    auth = _auth()
    if not auth.enabled:
        return ok({"enabled": False, "needs_setup": False, "user": None, "roles": ROLE_LABELS})
    token = split_cookie(request.headers.get("cookie", ""), auth.cookie_name)
    user = await auth.authenticate(token)
    return ok({
        "enabled": True,
        "needs_setup": await auth.users.count() == 0,
        "user": _user_out(user) if user else None,
        "roles": ROLE_LABELS,
    })


@router.post("/setup")
async def setup(request: Request, response: Response, username: str = Body(...),
                password: str = Body(...), display_name: str = Body("")):
    """第一次使用：还没有任何用户时，创建第一个管理员并直接登录。有用户之后这个接口就不能用了。"""
    auth = _auth()
    if await auth.users.count() > 0:
        raise bad_request("已经有用户了，请直接登录")
    from .users import check_username
    check_username(username)
    if reason := check_strength(password):
        raise bad_request(reason)
    await auth.users.create(username=username.strip(), password=password, role="admin",
                            display_name=display_name.strip())
    token, user = await auth.login(username, password, client_ip(request),
                                   request.headers.get("user-agent", ""))
    _set_cookie(response, token)
    return ok(_user_out(user), "管理员已创建")


@router.post("/login")
async def login(request: Request, response: Response, username: str = Body(...),
                password: str = Body(...)):
    auth = _auth()
    try:
        token, user = await auth.login(username, password, client_ip(request),
                                       request.headers.get("user-agent", ""))
    except AuthError as exc:
        raise bad_request(str(exc))
    _set_cookie(response, token)
    return ok(_user_out(user), "已登录")


@router.post("/logout")
async def logout(request: Request, response: Response):
    auth = _auth()
    await auth.logout(split_cookie(request.headers.get("cookie", ""), auth.cookie_name))
    response.delete_cookie(auth.cookie_name, path="/")
    return ok(message="已退出")


@router.put("/password")
async def change_password(request: Request, old_password: str = Body(...),
                          new_password: str = Body(...)):
    """改自己的密码。改完别的地方的登录都失效，当前这个保留。"""
    auth = _auth()
    user = current_user()
    if not user or not user.get("id"):
        raise bad_request("没有登录")
    if not verify_password(old_password, await auth.users.password_hash_of(user["id"])):
        raise bad_request("原密码不对")
    if reason := check_strength(new_password):
        raise bad_request(reason)
    await auth.users.set_password(user["id"], new_password)
    await auth.users.delete_sessions(
        user["id"], keep_token=split_cookie(request.headers.get("cookie", ""), auth.cookie_name))
    return ok(message="密码已修改，其他设备上的登录已退出")
