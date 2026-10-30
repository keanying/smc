# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""盒子和主之间传数据的编码：普通 JSON，日期时间和 Decimal 打个标记，对面原样还原。

不用 pickle：主不能反序列化盒子发来的任意对象（那等于让盒子在主上执行代码）。
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(v) for v in value]
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if len(value) == 1:
            if "$dt" in value:
                return datetime.fromisoformat(value["$dt"])
            if "$date" in value:
                return date.fromisoformat(value["$date"])
            if "$dec" in value:
                return Decimal(value["$dec"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def to_dataclass(cls, data: Any):
    """字典还原成采集条目（WorkItem / CommentItem / AuthorItem）。不认识的键丢掉，不报错。"""
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__} 应该是对象，收到 {type(data).__name__}")
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in decode(data).items() if k in names})
