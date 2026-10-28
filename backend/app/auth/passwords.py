# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""密码哈希：PBKDF2-SHA256，标准库自带，不引新依赖。"""
from __future__ import annotations

import hashlib
import hmac
import secrets

ALGO = "pbkdf2_sha256"
ITERATIONS = 240_000

#: 用户名不存在时也算一遍哈希，别让"用户名对不对"能从响应快慢里看出来
_DUMMY = f"{ALGO}${ITERATIONS}$00$00"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), ITERATIONS)
    return f"{ALGO}${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = (stored or _DUMMY).split("$", 3)
        if algo != ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(),
                                     int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def check_strength(password: str) -> str:
    """不合格返回原因，合格返回空串。规则从简：够长、不是纯数字。"""
    if len(password or "") < 8:
        return "密码至少 8 位"
    if password.isdigit():
        return "密码不能是纯数字"
    if len(password) > 128:
        return "密码太长了（最多 128 位）"
    return ""
