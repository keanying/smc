"""全局常量：平台标识、任务状态、评论等级等。

需求文档里平台标识写的是 douying（笔误），这里统一用 douyin。
如果下游系统必须收到 douying，只改本文件 CHANNEL_DOUYIN 一处即可。
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List

CHANNEL_WEIBO = "weibo"
CHANNEL_DOUYIN = "douyin"
CHANNEL_KUAISHOU = "kuaishou"
CHANNEL_XHS = "xiaohongshu"
CHANNEL_CTRIP = "ctrip"
CHANNEL_TONGCHENG = "tongcheng"
# ⚠️ 上游那份采集器里写的是 qunaer（多了个 e）。这里统一用正确拼写，
#    和文件开头 douying/douyin 那条先例一致。下游要 qunaer 只改这一处。
CHANNEL_QUNAR = "qunar"

ALL_CHANNELS: List[str] = [
    CHANNEL_DOUYIN,
    CHANNEL_KUAISHOU,
    CHANNEL_XHS,
    CHANNEL_WEIBO,
    CHANNEL_CTRIP,
    CHANNEL_TONGCHENG,
    CHANNEL_QUNAR,
]

CHANNEL_LABELS: Dict[str, str] = {
    CHANNEL_DOUYIN: "抖音",
    CHANNEL_KUAISHOU: "快手",
    CHANNEL_XHS: "小红书",
    CHANNEL_WEIBO: "微博",
    CHANNEL_CTRIP: "携程",
    CHANNEL_TONGCHENG: "同程",
    CHANNEL_QUNAR: "去哪儿",
}

# 有作品/帖子/笔记概念的平台；携程、同程只有点评，没有作品
CHANNELS_WITH_WORKS: List[str] = [
    CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_XHS, CHANNEL_WEIBO,
]

# 需要登录态才能采集的平台
CHANNELS_NEED_LOGIN: List[str] = [
    CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_XHS, CHANNEL_WEIBO,
]

# 按 POI/景区点评接口采集的平台（不走关键字搜索）
CHANNELS_POI_BASED: List[str] = [CHANNEL_CTRIP, CHANNEL_TONGCHENG, CHANNEL_QUNAR]


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    PAUSED = "paused"


class ScheduleType(str, Enum):
    ONCE = "once"        # 创建后立即执行一次
    AT = "at"            # 指定时刻执行一次
    INTERVAL = "interval"  # 固定间隔重复
    CRON = "cron"        # cron 表达式


class CollectType(str, Enum):
    KEYWORD = "keyword"  # 关键字搜索采集
    CREATOR = "creator"  # 指定用户主页全量采集
    POI = "poi"          # 景区点评采集（携程/同程）
    DETAIL = "detail"    # 指定作品链接采集


class AccountStatus(str, Enum):
    NEVER_LOGIN = "never_login"
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


def comment_level(depth: int) -> str:
    """depth 从 1 开始：1 -> level_1，2 -> level_2。"""
    return f"level_{max(1, int(depth))}"


def level_depth(level: str) -> int:
    try:
        return int(str(level).rsplit("_", 1)[-1])
    except (ValueError, TypeError):
        return 1
