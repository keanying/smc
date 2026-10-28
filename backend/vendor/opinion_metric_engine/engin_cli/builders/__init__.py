# -*- coding: utf-8 -*-
# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""四张 ADS 表的计算 builder。

每个 builder 的签名统一为 build(facts..., ctx) -> pd.DataFrame，
返回的列名与列序**严格等于**目标表 DDL（除自增 id），loader 直接按列名写入。
"""

from .core import build_core
from .platform import build_platform
from .dimension import build_dimension
from .content import build_content

__all__ = ["build_core", "build_platform", "build_dimension", "build_content"]
