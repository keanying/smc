# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""用户和登录会话的读写。"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, List, Optional, Tuple

from ..core.db import Database
from ..db import tables as T
from .passwords import hash_password

#: 返回给页面的列：不含密码哈希
_PUBLIC = ("id, username, display_name, role, enabled, failed_count, locked_until, "
           "last_login_at, last_login_ip, password_changed_at, create_time")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ 用户
    async def count(self) -> int:
        return int(await self.db.fetch_value(f"SELECT COUNT(*) AS c FROM `{T.SYS_USER}`", [], 0) or 0)

    async def count_admins(self, exclude_id: int = 0) -> int:
        return int(await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{T.SYS_USER}` WHERE role = 'admin' AND enabled = 1 AND id <> %s",
            [exclude_id], 0) or 0)

    async def list(self) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(f"SELECT {_PUBLIC} FROM `{T.SYS_USER}` ORDER BY id ASC")

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(f"SELECT {_PUBLIC} FROM `{T.SYS_USER}` WHERE id = %s", [user_id])

    async def get_with_hash(self, username: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT {_PUBLIC}, password_hash, locked_until > NOW() AS is_locked "
            f"FROM `{T.SYS_USER}` WHERE username = %s", [username])

    async def password_hash_of(self, user_id: int) -> str:
        row = await self.db.fetch_one(
            f"SELECT password_hash FROM `{T.SYS_USER}` WHERE id = %s", [user_id])
        return str(row["password_hash"]) if row else ""

    async def create(self, *, username: str, password: str, role: str,
                     display_name: str = "") -> int:
        return await self.db.execute_returning_id(
            f"INSERT INTO `{T.SYS_USER}` (username, display_name, password_hash, role, "
            f"password_changed_at) VALUES (%s, %s, %s, %s, NOW())",
            [username, display_name or username, hash_password(password), role])

    async def update(self, user_id: int, fields: Dict[str, Any]) -> int:
        allowed = {k: v for k, v in fields.items() if k in ("display_name", "role", "enabled")}
        if not allowed:
            return 0
        sets = ", ".join(f"`{k}` = %s" for k in allowed)
        return await self.db.execute(
            f"UPDATE `{T.SYS_USER}` SET {sets} WHERE id = %s", [*allowed.values(), user_id])

    async def set_password(self, user_id: int, password: str) -> None:
        await self.db.execute(
            f"UPDATE `{T.SYS_USER}` SET password_hash = %s, password_changed_at = NOW(), "
            f"failed_count = 0, locked_until = NULL WHERE id = %s",
            [hash_password(password), user_id])

    async def unlock(self, user_id: int) -> None:
        await self.db.execute(
            f"UPDATE `{T.SYS_USER}` SET failed_count = 0, locked_until = NULL WHERE id = %s", [user_id])

    async def delete(self, user_id: int) -> None:
        await self.delete_sessions(user_id)
        await self.db.execute(f"DELETE FROM `{T.SYS_USER}` WHERE id = %s", [user_id])

    async def record_failure(self, user_id: int, max_failed: int, lock_minutes: int) -> bool:
        """记一次输错。返回这次是不是把账号锁上了。"""
        await self.db.execute(
            f"UPDATE `{T.SYS_USER}` SET failed_count = failed_count + 1, "
            f"locked_until = CASE WHEN failed_count >= %s THEN NOW() + INTERVAL %s MINUTE "
            f"ELSE locked_until END WHERE id = %s",
            [max(1, max_failed), max(1, lock_minutes), user_id])
        row = await self.db.fetch_one(
            f"SELECT locked_until > NOW() AS locked FROM `{T.SYS_USER}` WHERE id = %s", [user_id])
        return bool(row and row["locked"])

    async def record_success(self, user_id: int, ip: str) -> None:
        await self.db.execute(
            f"UPDATE `{T.SYS_USER}` SET failed_count = 0, locked_until = NULL, "
            f"last_login_at = NOW(), last_login_ip = %s WHERE id = %s", [ip[:64], user_id])

    # ------------------------------------------------------------------ 会话
    async def create_session(self, user_id: int, hours: int, ip: str, user_agent: str) -> str:
        token = secrets.token_urlsafe(32)
        await self.db.execute(
            f"INSERT INTO `{T.SYS_USER_SESSION}` (token_hash, user_id, expires_at, ip, user_agent) "
            f"VALUES (%s, %s, NOW() + INTERVAL %s HOUR, %s, %s)",
            [token_hash(token), user_id, max(1, hours), ip[:64], user_agent[:300]])
        # 顺手清掉过期的，表不会无限长
        await self.db.execute(f"DELETE FROM `{T.SYS_USER_SESSION}` WHERE expires_at < NOW()")
        return token

    async def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        """令牌对应的用户（会话没过期、用户没停用）。"""
        return await self.db.fetch_one(
            f"SELECT u.id, u.username, u.display_name, u.role, s.last_seen_at "
            f"FROM `{T.SYS_USER_SESSION}` s JOIN `{T.SYS_USER}` u ON u.id = s.user_id "
            f"WHERE s.token_hash = %s AND s.expires_at > NOW() AND u.enabled = 1",
            [token_hash(token)])

    async def touch_session(self, token: str, hours: int) -> None:
        await self.db.execute(
            f"UPDATE `{T.SYS_USER_SESSION}` SET last_seen_at = NOW(), "
            f"expires_at = NOW() + INTERVAL %s HOUR WHERE token_hash = %s",
            [max(1, hours), token_hash(token)])

    async def delete_session(self, token: str) -> None:
        await self.db.execute(
            f"DELETE FROM `{T.SYS_USER_SESSION}` WHERE token_hash = %s", [token_hash(token)])

    async def delete_sessions(self, user_id: int, keep_token: str = "") -> None:
        """改密码 / 停用 / 删除用户后，让他在所有地方的登录都失效（当前这个可以留着）。"""
        keep = token_hash(keep_token) if keep_token else ""
        await self.db.execute(
            f"DELETE FROM `{T.SYS_USER_SESSION}` WHERE user_id = %s AND token_hash <> %s",
            [user_id, keep])


def split_cookie(header: str, name: str) -> str:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


__all__: Tuple[str, ...] = ("UserRepository", "split_cookie", "token_hash")
