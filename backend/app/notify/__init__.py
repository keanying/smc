"""通知：采集出问题时把人叫来。"""
from .feishu import FeishuNotifier, NotifyEvent, get_notifier

__all__ = ["FeishuNotifier", "NotifyEvent", "get_notifier"]
