"""主进程和子进程共用的一小撮东西（不 import pandas，也不 import 主进程的模块）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List

#: 子进程 stdout 上事件行的前缀，后面跟一个 JSON
EVENT_PREFIX = "@@METRIC@@ "


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y%m%d")


def date_list(start: str, end: str, rerun_days: int = 0) -> List[str]:
    """[start, end]（yyyyMMdd）再往前多带 rerun_days 天。"""
    try:
        s = datetime.strptime(start, "%Y%m%d").date() - timedelta(days=max(0, rerun_days))
        e = datetime.strptime(end, "%Y%m%d").date()
    except ValueError:
        raise ValueError(f"日期要写成 yyyyMMdd：{start} ~ {end}")
    if e < s:
        raise ValueError(f"结束日期 {end} 早于开始日期 {start}")
    return [(s + timedelta(days=i)).strftime("%Y%m%d") for i in range((e - s).days + 1)]
