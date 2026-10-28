# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""景区档案采集源。导入本包即完成三个渠道的注册。"""
from .base import (  # noqa: F401
    ARCHIVE_CHANNELS, ArchiveRow, ArchiveSource, Region,
    available, get_source, register,
)
from . import ctrip_source, qunar_source, tongcheng_source  # noqa: F401,E402

__all__ = ["ARCHIVE_CHANNELS", "ArchiveRow", "ArchiveSource", "Region",
           "available", "get_source", "register"]
