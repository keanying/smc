# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""当前请求是谁。中间件认出用户后放进来，接口里要记「谁做的」时取。"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Optional

_current: ContextVar[Optional[Dict[str, Any]]] = ContextVar("smc_current_user", default=None)


def set_current_user(user: Optional[Dict[str, Any]]):
    return _current.set(user)


def reset_current_user(token) -> None:
    _current.reset(token)


def current_user() -> Optional[Dict[str, Any]]:
    return _current.get()


def current_name(default: str = "") -> str:
    """复核人、任务创建人等处写的名字：显示名优先，没有就登录名。没登录（关了登录）返回 default。"""
    user = _current.get()
    if not user:
        return default
    return str(user.get("display_name") or user.get("username") or default)
