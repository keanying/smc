# -*- coding: utf-8 -*-
# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""opinion_metric_engine · 舆情指标引擎

景区社媒舆情指标计算：src → ADS，全内存，无中间表。
"""

__version__ = "1.9.2"

# 进程被底层库（numpy / pandas 的 DLL 等）直接崩掉时，Windows 上什么都不打印就退出了，
# 看起来像「执行完没反应」。打开 faulthandler，崩溃时至少在终端留下出事的位置。
# 必须放在导入 pandas 之前（下面的 settings / context 会间接导入它）。
import faulthandler as _faulthandler
import sys as _sys

if not _faulthandler.is_enabled() and getattr(_sys, "stderr", None) is not None:
    try:
        _faulthandler.enable()
    except Exception:                    # noqa: BLE001 —— 没有可写的 stderr（如 pythonw）就算了
        pass

from .settings import EtlSettings, load_settings   # noqa: F401
from .context import RunContext                     # noqa: F401

__all__ = ["EtlSettings", "load_settings", "RunContext", "__version__"]
