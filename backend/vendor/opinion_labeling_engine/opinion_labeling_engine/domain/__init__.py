# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""领域层：数据模型、枚举与标签体系。不依赖任何外部 IO。"""

from .enums import LabelSource, Sentiment, TaskStatus
from .models import CommentRecord, DimensionTag, EntityTag, LabelResult
from .taxonomy import DimensionNode, Taxonomy

__all__ = [
    "LabelSource",
    "Sentiment",
    "TaskStatus",
    "CommentRecord",
    "DimensionTag",
    "EntityTag",
    "LabelResult",
    "DimensionNode",
    "Taxonomy",
]
