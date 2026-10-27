"""指标计算接入层。

引擎本体在 `backend/vendor/opinion_metric_engine`（舆情指标引擎，pandas 全内存计算），
smc 不 import 它，计算在子进程里跑（worker.py）。这一层只管：页面维护的配置、
定时、调起子进程、记录每次运行的结果。
"""
from .manager import MetricsManager, get_manager

__all__ = ["MetricsManager", "get_manager"]
