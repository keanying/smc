# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""登录、认人、退出。

会话存在库里（不是签名 Cookie），这样：
- 集群里几台机器共用一个库，在哪台登录、请求转到哪台都认；
- 改密码、停用用户能让他所有地方的登录立刻失效。
认人的结果在进程里缓存几秒，页面每几秒一次的轮询不用每次都查库。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from ..core.logging import get_logger
from .passwords import verify_password
from .repo import UserRepository

logger = get_logger(__name__)

CACHE_SECONDS = 10
#: 距上次顺延超过这么久才写库顺延一次（每个请求都写太浪费）
TOUCH_SECONDS = 60

#: 集群内部请求（机器之间互相调用）当成这个用户
SYSTEM_USER = {"id": 0, "username": "system", "display_name": "系统", "role": "admin"}


class AuthError(Exception):
    pass


class AuthService:
    def __init__(self, config, users: UserRepository):
        self.config = config
        self.users = users
        #: 集群内部令牌（见 cluster/service.py 的 secret）。机器之间互相调用时带上它
        self.internal_token = ""
        self._cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._touched: Dict[str, float] = {}

    # ------------------------------------------------------------------ 配置
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("auth.enabled", True))

    @property
    def cookie_name(self) -> str:
        return str(self.config.get("auth.cookie_name", "smc_session") or "smc_session")

    @property
    def session_hours(self) -> int:
        return max(1, int(self.config.get("auth.session_hours", 72) or 72))

    # ------------------------------------------------------------------ 登录
    async def login(self, username: str, password: str, ip: str, user_agent: str
                    ) -> Tuple[str, Dict[str, Any]]:
        username = (username or "").strip()
        row = await self.users.get_with_hash(username) if username else None
        # 用户名不存在也照样算一遍哈希：别让响应快慢泄露用户名对不对
        good = verify_password(password or "", row["password_hash"] if row else "")
        if not row:
            raise AuthError("用户名或密码不对")
        if not row["enabled"]:
            raise AuthError("这个用户已被停用，请联系管理员")
        if row.get("is_locked"):
            raise AuthError(f"密码输错次数太多，账号已锁定，{row['locked_until']:%H:%M} 后再试")
        if not good:
            locked = await self.users.record_failure(
                row["id"], int(self.config.get("auth.max_failed_logins", 5) or 5),
                int(self.config.get("auth.lock_minutes", 10) or 10))
            logger.warning("登录失败：用户 %s，来自 %s%s", username, ip, "，已锁定" if locked else "")
            if locked:
                raise AuthError(f"密码输错次数太多，账号锁定 {self.config.get('auth.lock_minutes', 10)} 分钟")
            raise AuthError("用户名或密码不对")
        await self.users.record_success(row["id"], ip)
        token = await self.users.create_session(row["id"], self.session_hours, ip, user_agent)
        logger.info("登录：用户 %s，来自 %s", username, ip)
        user = {k: row[k] for k in ("id", "username", "display_name", "role")}
        return token, user

    async def logout(self, token: str) -> None:
        if token:
            self._cache.pop(token, None)
            await self.users.delete_session(token)

    def forget_user(self, user_id: int) -> None:
        """改了这个用户的角色 / 停用 / 删除：缓存里他的会话马上作废，不等几秒后过期。"""
        for token, (user, _) in list(self._cache.items()):
            if user.get("id") == user_id:
                self._cache.pop(token, None)

    # ------------------------------------------------------------------ 认人
    async def authenticate(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        if self.internal_token and token == self.internal_token:
            return SYSTEM_USER
        now = time.monotonic()
        hit = self._cache.get(token)
        if hit and now - hit[1] < CACHE_SECONDS:
            return hit[0]
        row = await self.users.session_user(token)
        if not row:
            self._cache.pop(token, None)
            return None
        user = {k: row[k] for k in ("id", "username", "display_name", "role")}
        self._cache[token] = (user, now)
        if now - self._touched.get(token, 0) > TOUCH_SECONDS:
            self._touched[token] = now
            await self.users.touch_session(token, self.session_hours)
        if len(self._cache) > 5000:            # 防止长期运行时无限长
            self._cache.clear()
            self._touched.clear()
        return user


_auth: Optional[AuthService] = None


def set_auth(service: Optional[AuthService]) -> None:
    global _auth
    _auth = service


def get_auth() -> Optional[AuthService]:
    return _auth
