# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""指标计算接入层。

引擎本体在 `backend/vendor/opinion_metric_engine`（舆情指标引擎，pandas 全内存计算），
smc 不 import 它，计算在子进程里跑（worker.py）。这一层只管：页面维护的配置、
定时、调起子进程、记录每次运行的结果。
"""
from .manager import MetricsManager, get_manager

__all__ = ["MetricsManager", "get_manager"]
