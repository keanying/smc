"""景区档案采集源。导入本包即完成三个渠道的注册。"""
from .base import (  # noqa: F401
    ARCHIVE_CHANNELS, ArchiveRow, ArchiveSource, Region,
    available, get_source, register,
)
from . import ctrip_source, qunar_source, tongcheng_source  # noqa: F401,E402

__all__ = ["ARCHIVE_CHANNELS", "ArchiveRow", "ArchiveSource", "Region",
           "available", "get_source", "register"]
