"""WebSocket：任务实时日志、交互式登录浏览器画面。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..browser.live_session import get_session
from ..core.logging import get_logger
from ..scheduler.task_logger import LOG_HUB
from .deps import get_state

logger = get_logger(__name__)
router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws/tasks/{task_id}/logs")
async def task_log_stream(websocket: WebSocket, task_id: str):
    """任务日志实时流。连上先补最近的历史日志，之后增量推送。"""
    await websocket.accept()
    queue = LOG_HUB.subscribe(task_id)
    try:
        for record in LOG_HUB.recent(task_id):
            await websocket.send_json({"type": "log", "data": record})

        while True:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=30)
                await websocket.send_json({"type": "log", "data": record})
            except asyncio.TimeoutError:
                # 心跳，防止中间层把闲置连接掐掉
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("任务日志 WebSocket 异常：%s", exc)
    finally:
        LOG_HUB.unsubscribe(task_id, queue)


@router.websocket("/ws/browser/{session_id}")
async def browser_stream(websocket: WebSocket, session_id: str):
    """交互式登录：服务端推浏览器画面，前端回传鼠标键盘事件。

    收发协议（都是 JSON 文本帧）：
      服务端 -> 前端  {"type":"frame","data":"<base64 jpeg>","width":..,"height":..}
                      {"type":"status","state":"streaming","message":"..."}
      前端 -> 服务端  {"type":"click","x":100,"y":200}
                      {"type":"type","text":"13800138000"}
                      {"type":"key","key":"Enter"}
                      {"type":"wheel","deltaY":300}
                      {"type":"close"}
    """
    await websocket.accept()
    session = get_session(session_id)
    if session is None:
        await websocket.send_json({
            "type": "status", "state": "error", "message": "登录会话不存在或已关闭",
        })
        await websocket.close()
        return

    send_lock = asyncio.Lock()

    async def on_frame(data_b64: str, width: int, height: int) -> None:
        async with send_lock:
            try:
                await websocket.send_json({
                    "type": "frame", "data": data_b64, "width": width, "height": height,
                })
            except Exception:  # noqa: BLE001 - 前端断开时忽略
                pass

    async def on_status(state: str, message: str) -> None:
        async with send_lock:
            try:
                await websocket.send_json({
                    "type": "status", "state": state, "message": message,
                })
            except Exception:  # noqa: BLE001
                pass

    try:
        await session.start(on_frame=on_frame, on_status=on_status)
        while True:
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") == "close":
                break
            await session.handle_input(event)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        # 有些异常（比如 NotImplementedError）str() 是空的，只打消息会看不出问题，
        # 所以把类型名也带上，并记完整栈供排查
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        logger.exception("浏览器 WebSocket 异常：%s", detail)
        await on_status("error", f"会话异常：{detail}")
    finally:
        result = await session.stop()
        try:
            await websocket.send_json({
                "type": "status",
                "state": "closed",
                "message": "登录成功，登录态已保存" if result["logged_in"] else "会话已关闭",
                "logged_in": result["logged_in"],
            })
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
