# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""角色与权限。

    admin     管理员：全部功能，含用户管理、系统设置、集群管理
    operator  操作员：日常操作（景区、任务、账号、复核、指标计算……），不能管用户、改系统设置、管集群
    viewer    只读：只能看

按「方法 + 路径」在中间件里统一判断，不在每个接口上各写一遍——漏写一个就是一个越权口子。
"""
from __future__ import annotations

ROLES = ("admin", "operator", "viewer")
ROLE_LABELS = {"admin": "管理员", "operator": "操作员", "viewer": "只读"}

_READ = ("GET", "HEAD", "OPTIONS")

#: 只读用户也能做的写操作：管自己的登录态
_SELF_SERVICE = ("/api/auth/logout", "/api/auth/password")


def denied_reason(role: str, method: str, path: str, websocket: bool = False) -> str:
    """不允许返回原因，允许返回空串。"""
    if path.startswith(_SELF_SERVICE):
        return ""
    if role == "admin":
        return ""
    if path.startswith("/api/users"):
        return "用户管理只有管理员能用"
    if role == "viewer":
        if websocket:
            # 看日志、看实时画面可以；操作登录浏览器不行
            return "只读用户不能操作登录浏览器" if path.startswith("/ws/browser/") else ""
        return "" if method in _READ else "只读用户不能修改，请找管理员开操作权限"
    # operator
    if method in _READ or websocket:
        return ""
    if path.startswith("/api/settings"):
        return "系统设置只有管理员能改"
    if path.startswith("/api/cluster"):
        return "集群管理只有管理员能操作"
    return ""
