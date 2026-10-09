"""标注引擎接入层。

引擎本体在 `backend/vendor/opinion_labeling_engine`，**一行都不改**。
这一层只做三件事：喂配置、把它的线程模型桥到 asyncio、把它的能力暴露成 API。
"""
from .manager import LabelingManager, get_manager

__all__ = ["LabelingManager", "get_manager"]
