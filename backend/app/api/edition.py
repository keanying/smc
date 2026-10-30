# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""版本信息与景区分配包（见 app/edition.py）。

    GET  /api/edition                   不用登录：总部版还是景区版（页面据此换布局、藏菜单）
    GET  /api/edition/assignment        景区版：当前分配了哪些景区、什么时候下发的
    POST /api/edition/bundle/export     总部版：选中的景区导出成分配包
    POST /api/edition/bundle/import     景区版：导入总部下发的分配包（管理员）
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from .. import edition as E
from ..auth import current_name, current_user
from .common import bad_request, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/edition", tags=["版本"])


@router.get("")
async def edition_info(state: AppState = Depends(get_state)):
    return ok(E.info(state.config))


@router.get("/assignment")
async def get_assignment(state: AppState = Depends(get_state)):
    record = await E.assignment(state.settings) or {}
    options = await state.scenics.list_scenic_options(enabled_only=True)
    return ok({**record, "scenics": options})


@router.post("/bundle/export")
async def export_bundle(scenic_ids: List[str] = Body(..., embed=True),
                        box_name: str = Body("", embed=True),
                        state: AppState = Depends(get_state)):
    bundle = await E.export_bundle(state.scenics, scenic_ids, box_name=box_name,
                                   exported_by=current_name())
    # 原样返回包本身（不套 {code,data}），前端直接存成文件
    return JSONResponse(bundle)


@router.post("/bundle/import")
async def import_bundle(bundle: Dict[str, Any] = Body(...), state: AppState = Depends(get_state)):
    if not E.is_scenic():
        raise bad_request("总部版不用导入分配包；分配包是给景区盒子用的")
    user = current_user()
    if user is not None and user.get("role") != "admin":
        raise bad_request("只有管理员能导入分配包")
    result = await E.import_bundle(state.db, state.scenics, state.settings, bundle,
                                   imported_by=current_name())
    # 景区换了：概览的按景区汇总缓存了 30 秒，不清掉的话新景区要过一会儿才有数字
    state.data._overview_cache = None
    note = f"，停用了不再分配的 {len(result['dropped'])} 个" if result["dropped"] else ""
    return ok(result, f"已导入 {len(result['scenic_ids'])} 个景区{note}")
