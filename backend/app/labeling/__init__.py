# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""标注引擎接入层。

引擎本体在 `backend/vendor/opinion_labeling_engine`，**一行都不改**。
这一层只做三件事：喂配置、把它的线程模型桥到 asyncio、把它的能力暴露成 API。
"""
from .manager import LabelingManager, get_manager

__all__ = ["LabelingManager", "get_manager"]
