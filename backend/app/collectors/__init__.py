# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""导入即注册：确保 CollectorRegistry 里有全部平台。"""
from .base import (  # noqa: F401
    AuthorItem,
    BaseCollector,
    CollectContext,
    CollectorRegistry,
    CollectTarget,
    CommentItem,
    LoginRequired,
    TaskCancelled,
    WorkItem,
)

# 免登录平台
from . import ctrip  # noqa: F401
from . import qunar  # noqa: F401
from . import tongcheng  # noqa: F401

# 社媒平台
from . import douyin  # noqa: F401
from . import kuaishou  # noqa: F401
from . import xhs  # noqa: F401
from . import weibo  # noqa: F401

__all__ = [
    "AuthorItem", "BaseCollector", "CollectContext", "CollectorRegistry",
    "CollectTarget", "CommentItem", "LoginRequired", "TaskCancelled", "WorkItem",
]
