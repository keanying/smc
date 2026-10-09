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
