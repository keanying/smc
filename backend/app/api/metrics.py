# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""指标计算 API：配置（景区 / 参数 / 定时 / 推送 / 推送表与字段）、手动计算、运行记录、ADS 建表。"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Query, WebSocket, WebSocketDisconnect

from ..metrics import config as mc
from ..metrics import get_manager
from .common import bad_request, not_found, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/metrics", tags=["指标计算"])
ws_router = APIRouter(tags=["指标计算"])


def _manager(state: AppState):
    return get_manager(state.config, state.db, state.settings)


@router.get("/status")
async def status(state: AppState = Depends(get_state)):
    return ok(await _manager(state).status())


@router.get("/config")
async def get_config(state: AppState = Depends(get_state)):
    """页面维护的整份配置。推送令牌不回显（push.has_token 表示填没填过）。"""
    return ok(mc.redact(await _manager(state).get_config()))


@router.put("/config")
async def save_config(payload: dict = Body(...), state: AppState = Depends(get_state)):
    try:
        cfg = await _manager(state).save_config(payload)
    except ValueError as exc:
        raise bad_request(str(exc))
    return ok(mc.redact(cfg), "已保存，下次计算按新配置")


@router.get("/catalog")
async def catalog(state: AppState = Depends(get_state)):
    """六张指标表的字段（名称 + 中文说明）和默认主键，维护推送字段用。"""
    try:
        return ok(await _manager(state).catalog())
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc))


@router.get("/scenics")
async def scenics(state: AppState = Depends(get_state)):
    """可选的景区：景区管理里建的全部景区。"""
    rows = await state.db.fetch_all(
        "SELECT scenic_id, scenic_name FROM `src_opinion_social_scenic` ORDER BY scenic_name")
    return ok(rows)


@router.get("/tables")
async def table_status(state: AppState = Depends(get_state)):
    """六张 ADS 表在不在、大概多少行。"""
    return ok(await _manager(state).table_status())


@router.post("/tables/init")
async def init_tables(state: AppState = Depends(get_state)):
    """按引擎自带的建表语句建 ADS 表，已有的不动。"""
    try:
        return ok(await _manager(state).init_tables(), "建表完成")
    except Exception as exc:  # noqa: BLE001
        raise bad_request(f"建表失败：{exc}")


@router.get("/push/preview")
async def push_preview(table: str = Query("drill"), state: AppState = Depends(get_state)):
    """按当前配置组一个推送请求给人看（不发送），对着下游的接口样例核对格式。"""
    try:
        return ok(await _manager(state).push_preview(table))
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc))


@router.post("/runs")
async def start_run(payload: dict = Body(default={}), state: AppState = Depends(get_state)):
    """手动计算。start_date / end_date 是 yyyyMMdd，不填 = 昨天；scenics 不填 = 配置里的景区。"""
    scenics: Optional[List[str]] = payload.get("scenics")
    try:
        run = await _manager(state).start_run(
            trigger="manual",
            start_date=str(payload.get("start_date") or "").replace("-", ""),
            end_date=str(payload.get("end_date") or "").replace("-", ""),
            scenics=[str(s) for s in scenics] if isinstance(scenics, list) and scenics else None,
            no_push=bool(payload.get("no_push")))
    except (RuntimeError, ValueError) as exc:
        raise bad_request(str(exc))
    return ok(run, "已开始计算")


@router.get("/mock/check")
async def mock_check(to_scenic: str = Query(...), state: AppState = Depends(get_state)):
    """演示数据的目标景区在本系统里有没有真实数据（有的话推出去会覆盖下游里它的数据）。"""
    return ok(await _manager(state).mock_check(to_scenic))


@router.post("/mock")
async def start_mock(payload: dict = Body(...), state: AppState = Depends(get_state)):
    """演示数据：把一个景区的指标结果复制给另一个景区编号并推送（不写本地库）。"""
    try:
        run = await _manager(state).start_mock(
            from_scenic=str(payload.get("from_scenic") or ""),
            to_scenic=str(payload.get("to_scenic") or ""),
            to_name=str(payload.get("to_name") or ""),
            start_date=str(payload.get("start_date") or "").replace("-", ""),
            end_date=str(payload.get("end_date") or "").replace("-", ""),
            tables=[str(t) for t in (payload.get("tables") or [])],
            dry_run=bool(payload.get("dry_run")))
    except (RuntimeError, ValueError) as exc:
        raise bad_request(str(exc))
    return ok(run, "演示数据已开始推送")


@router.get("/runs")
async def list_runs(page: int = Query(1), page_size: int = Query(20),
                    state: AppState = Depends(get_state)):
    return ok(await _manager(state).list_runs(page, page_size))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, state: AppState = Depends(get_state)):
    run = await _manager(state).get_run(run_id, with_log=True)
    if run is None:
        raise not_found("运行记录不存在")
    return ok(run)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, state: AppState = Depends(get_state)):
    if not await _manager(state).cancel(run_id):
        raise not_found("这次计算不在运行中")
    return ok(None, "已停止")


@ws_router.websocket("/ws/metrics/runs/{run_id}/logs")
async def run_log_stream(websocket: WebSocket, run_id: str):
    """一次计算的实时日志。

    服务端 -> 页面（JSON 文本帧）：
        {"type":"snapshot","lines":[...],"summary":[...],"status":"running"}  连上先给已有的
        {"type":"log","line":"..."}                                          之后每来一行推一行
        {"type":"summary","summary":[...]}                                   一个景区算完
        {"type":"end","status":"success","error":"","summary":[...]}         结束，随后关闭
        {"type":"ping"}                                                      30 秒心跳
    不在跑（或跑在别的机器上又没转过来）的，给库里存的日志和结果后直接 end。
    """
    import asyncio

    await websocket.accept()
    manager = get_manager()
    live = manager.subscribe(run_id)
    try:
        if live is None:
            run = await manager.get_run(run_id, with_log=True)
            if not run:
                await websocket.send_json({"type": "end", "status": "missing", "error": "运行记录不存在"})
                return
            await websocket.send_json({"type": "snapshot", "status": run["status"],
                                       "lines": (run.get("log") or "").splitlines(),
                                       "summary": run.get("summary") or []})
            await websocket.send_json({"type": "end", "status": run["status"],
                                       "error": run.get("error") or "",
                                       "summary": run.get("summary") or []})
            return
        lines, summary, queue = live
        await websocket.send_json({"type": "snapshot", "status": "running", "lines": lines,
                                   "summary": summary})
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(message)
            if message.get("type") == "end":
                return
    except WebSocketDisconnect:
        pass
    finally:
        if live is not None:
            manager.unsubscribe(live[2])
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 —— 页面那头已经断了
            pass
