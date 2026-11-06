# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""运行记录 API：任务每执行一次一条（列表 / 详情 / 这次运行的日志 / 删除）。"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Body, Depends

from ..repositories.run_repo import RUN_STATUS_LABELS, TRIGGER_LABELS, RunRepository
from .common import bad_request, not_found, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/runs", tags=["运行记录"])


def _repo(state: AppState) -> RunRepository:
    return RunRepository(state.db)


@router.get("")
async def list_runs(
    task_id: str = "", scenic_id: str = "", channel: str = "", status: str = "",
    trigger: str = "", keyword: str = "", start_date: str = "", end_date: str = "",
    page: int = 1, page_size: int = 20, state: AppState = Depends(get_state),
):
    result = await _repo(state).list({
        "task_id": task_id, "scenic_id": scenic_id, "channel": channel, "run_status": status,
        "trigger_type": trigger, "keyword": keyword.strip(),
        "start_date": start_date, "end_date": end_date,
    }, page=page, page_size=page_size)
    # 筛选下拉的选项：状态 / 触发方式的取值和中文名，前端不用再抄一份
    result["status_options"] = [{"value": k, "label": v} for k, v in RUN_STATUS_LABELS.items()]
    result["trigger_options"] = [{"value": k, "label": v} for k, v in TRIGGER_LABELS.items()]
    return ok(result)


@router.get("/{run_no}")
async def get_run(run_no: str, state: AppState = Depends(get_state)):
    run = await _repo(state).get(run_no)
    if run is None:
        raise not_found("运行记录不存在")
    # 任务还在不在（删了的话详情页不给「去任务」的链接）
    run["task_exists"] = bool(await state.tasks.get(run["task_id"]))
    return ok(run)


@router.get("/{run_no}/logs")
async def run_logs(run_no: str, after_id: int = 0, limit: int = 500,
                   state: AppState = Depends(get_state)):
    repo = _repo(state)
    run = await repo.get(run_no)
    if run is None:
        raise not_found("运行记录不存在")
    return ok({"items": await repo.logs(run, after_id=after_id, limit=limit),
               "run_status": run["run_status"]})


def _delete_message(result: dict) -> str:
    parts = [f"已删除 {result['deleted']} 条运行记录"]
    if result["deleted_logs"]:
        parts.append(f"连同 {result['deleted_logs']} 条日志")
    if result["skipped_running"]:
        parts.append(f"{result['skipped_running']} 条还在运行，没删")
    return "，".join(parts)


@router.post("/delete")
async def delete_runs(
    run_nos: List[str] = Body(..., embed=True),
    with_logs: bool = Body(True, embed=True),
    state: AppState = Depends(get_state),
):
    """批量删除（列表勾选）。运行中的跳过。"""
    if not run_nos:
        raise bad_request("没有选中要删除的运行记录")
    if len(run_nos) > 1000:
        raise bad_request("一次最多删 1000 条")
    result = await _repo(state).delete(run_nos, with_logs=with_logs)
    return ok(result, _delete_message(result))


@router.delete("/{run_no}")
async def delete_run(run_no: str, with_logs: bool = True, state: AppState = Depends(get_state)):
    repo = _repo(state)
    run = await repo.get(run_no)
    if run is None:
        raise not_found("运行记录不存在（可能已经删过了）")
    if run["run_status"] == "running":
        raise bad_request("这次运行还没结束，结束后才能删除")
    result = await repo.delete([run_no], with_logs=with_logs)
    return ok(result, _delete_message(result))
