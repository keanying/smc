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
