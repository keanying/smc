# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""用户管理 API（只有管理员能进，见 auth/roles.py）。"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Body

from ..auth import current_user, get_auth
from ..auth.passwords import check_strength
from ..auth.roles import ROLE_LABELS, ROLES
from .common import bad_request, not_found, ok

router = APIRouter(prefix="/api/users", tags=["用户管理"])

_USERNAME = re.compile(r"^[\w.@-]{2,64}$")


def check_username(username: str) -> None:
    if not _USERNAME.match((username or "").strip()):
        raise bad_request("用户名 2~64 位，只能用字母、数字、汉字和 _ . @ -")


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise bad_request(f"角色只能是：{'、'.join(f'{k}（{v}）' for k, v in ROLE_LABELS.items())}")


def _users():
    return get_auth().users


def _out(row):
    return {**row, "role_label": ROLE_LABELS.get(row["role"], row["role"])}


@router.get("")
async def list_users():
    return ok([_out(r) for r in await _users().list()])


@router.post("")
async def create_user(username: str = Body(...), password: str = Body(...),
                      role: str = Body("operator"), display_name: str = Body("")):
    check_username(username)
    _check_role(role)
    if reason := check_strength(password):
        raise bad_request(reason)
    if await _users().get_with_hash(username.strip()):
        raise bad_request(f"用户名 {username} 已经有了")
    await _users().create(username=username.strip(), password=password, role=role,
                          display_name=display_name.strip())
    return ok(message=f"已添加用户 {username}")


async def _target(user_id: int):
    row = await _users().get(user_id)
    if not row:
        raise not_found("用户不存在")
    return row


async def _keep_one_admin(row, *, losing_admin: bool) -> None:
    """系统里至少留一个能用的管理员，不然没人能再管用户。"""
    if losing_admin and row["role"] == "admin" and row["enabled"] \
            and await _users().count_admins(exclude_id=row["id"]) == 0:
        raise bad_request("这是最后一个管理员，不能停用、删除或改成别的角色")


@router.put("/{user_id}")
async def update_user(user_id: int, display_name: Optional[str] = Body(None),
                      role: Optional[str] = Body(None), enabled: Optional[bool] = Body(None)):
    row = await _target(user_id)
    me = current_user() or {}
    fields = {}
    if display_name is not None:
        fields["display_name"] = display_name.strip() or row["username"]
    if role is not None and role != row["role"]:
        _check_role(role)
        await _keep_one_admin(row, losing_admin=role != "admin")
        fields["role"] = role
    if enabled is not None and bool(enabled) != bool(row["enabled"]):
        if not enabled and row["id"] == me.get("id"):
            raise bad_request("不能停用自己")
        await _keep_one_admin(row, losing_admin=not enabled)
        fields["enabled"] = int(enabled)
    await _users().update(user_id, fields)
    if "role" in fields or "enabled" in fields:
        get_auth().forget_user(user_id)
        if fields.get("enabled") == 0:
            await _users().delete_sessions(user_id)
    return ok(_out(await _users().get(user_id)), "已保存")


@router.put("/{user_id}/password")
async def reset_password(user_id: int, password: str = Body(..., embed=True)):
    """管理员重置别人的密码。重置后那个人所有地方的登录都失效。"""
    await _target(user_id)
    if reason := check_strength(password):
        raise bad_request(reason)
    await _users().set_password(user_id, password)
    await _users().delete_sessions(user_id)
    get_auth().forget_user(user_id)
    return ok(message="密码已重置，请把新密码告诉他")


@router.post("/{user_id}/unlock")
async def unlock_user(user_id: int):
    await _target(user_id)
    await _users().unlock(user_id)
    return ok(message="已解锁")


@router.delete("/{user_id}")
async def delete_user(user_id: int):
    row = await _target(user_id)
    if row["id"] == (current_user() or {}).get("id"):
        raise bad_request("不能删除自己")
    await _keep_one_admin(row, losing_admin=True)
    await _users().delete(user_id)
    get_auth().forget_user(user_id)
    return ok(message=f"已删除用户 {row['username']}")
