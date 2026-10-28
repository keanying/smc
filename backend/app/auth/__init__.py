# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""用户与登录：谁能进系统、能做什么。

三种角色：管理员 / 操作员 / 只读（roles.py）。会话存在库里，集群里几台机器通用（service.py）。
关掉登录：config 里 auth.enabled: false（或环境变量 SMC_AUTH_ENABLED=false）。
"""
from .context import current_name, current_user
from .service import AuthError, AuthService, get_auth, set_auth

__all__ = ["AuthError", "AuthService", "current_name", "current_user", "get_auth", "set_auth"]
