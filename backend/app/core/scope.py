# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""景区范围：这次请求只能看 / 改哪些景区的数据。

景区盒子通过主的 API 查看、修改数据（见 app/box/）。主收到盒子的请求时把
「这个盒子分到的景区」放进这里，数据中心和标注的查询**在仓库层**统一加上
`scenic_id IN (...)`——不靠每个接口自己记得加，漏一个就能看到别的景区的数据。

None = 不限（主自己的页面、采集写库）；空元组 = 一个景区都没分到，什么都查不到。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Iterable, List, Optional, Tuple

_scope: ContextVar[Optional[Tuple[str, ...]]] = ContextVar("smc_scenic_scope", default=None)


def set_scope(scenic_ids: Optional[Iterable[str]]):
    return _scope.set(None if scenic_ids is None else tuple(scenic_ids))


def reset_scope(token) -> None:
    _scope.reset(token)


def current_scope() -> Optional[Tuple[str, ...]]:
    return _scope.get()


def in_scope(scenic_id: Optional[str]) -> bool:
    scope = _scope.get()
    return scope is None or (scenic_id or "") in scope


def scope_sql(column: str = "`scenic_id`") -> Tuple[str, List[str]]:
    """返回 (条件, 参数)。不限范围时条件是空串，调用方自己决定要不要拼。"""
    scope = _scope.get()
    if scope is None:
        return "", []
    if not scope:
        return "1 = 0", []
    return f"{column} IN ({', '.join(['%s'] * len(scope))})", list(scope)
