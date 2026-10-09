"""采集实时画面接口。

无头模式下浏览器不弹窗，采集过程只能从日志推断。这组接口把**采集器正在
用的那个页面**的画面取出来，让任务详情页能直接看到"现在页面上是什么"。

默认只读，可以显式接管：前端发 {"type":"takeover","on":true} 之后，
鼠标/键盘事件才会被打回采集页面。为什么要有接管、风险怎么控住的，
写在 `app/browser/live_view.py` 的模块注释里（一句话：采集跑到一半弹
拖动验证时，只读会把人堵死，最后耗到看门狗把任务杀掉）。

三个接口，各自有存在的理由：
    GET  /api/live/views              有哪些页面可以看（任务详情页据此显示入口）
    GET  /api/live/views/{id}/frame.jpg   单帧。WebSocket 走不通时的兜底，
                                          也是给列表当缩略图用的
    WS   /ws/live/{id}                连续推流。只在有人连着的时候才推
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from ..browser import live_view as lv
from ..core.logging import get_logger
from .common import not_found, ok

logger = get_logger(__name__)
router = APIRouter(prefix="/api/live", tags=["实时画面"])
ws_router = APIRouter(tags=["实时画面"])


@router.get("/views")
async def list_views(task_id: str = ""):
    views = lv.for_task(task_id) if task_id else lv.all_views()
    return ok([await v.info() for v in views])


@router.get("/views/{view_id}/frame.jpg")
async def frame(view_id: str):
    view = lv.get(view_id)
    if view is None:
        raise not_found("这个采集会话已经结束了，没有实时画面可看")
    try:
        data = await view.snapshot()
    except Exception as exc:  # noqa: BLE001
        raise not_found(f"取画面失败：{exc}") from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        # 采集页面每秒都在变，任何一级缓存都会让画面卡在旧的一帧上
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache"},
    )


@ws_router.websocket("/ws/live/{view_id}")
async def live_stream(websocket: WebSocket, view_id: str):
    """连续推送采集页面的画面。

    协议（服务端 -> 前端，都是 JSON 文本帧）：
        {"type":"frame","data":"<base64 jpeg>","width":..,"height":..}
        {"type":"meta","data":{...}}      # url / 标题 / 当前步骤，每 3 秒一次
        {"type":"status","state":"...","message":"..."}

    协议（前端 -> 服务端）：
        {"type":"takeover","on":true|false}          进入 / 退出人工接管
        {"type":"mousedown","x":..,"y":..}           以下都**只在接管期间**生效
        {"type":"mousemove","x":..,"y":..}
        {"type":"mouseup","x":..,"y":..}
        {"type":"click","x":..,"y":..}
        {"type":"wheel","deltaX":..,"deltaY":..}
        {"type":"key","key":"Enter"} / {"type":"type","text":"..."}

    x/y 是**帧的像素坐标**，服务端按真实视口换算（见 live_view._handle_input）。
    没开接管时鼠标事件一律丢弃。
    """
    await websocket.accept()
    view = lv.get(view_id)
    if view is None:
        await websocket.send_json({
            "type": "status", "state": "gone",
            "message": "这个采集会话已经结束了（任务跑完或者浏览器已关闭）",
        })
        await websocket.close()
        return

    lock = asyncio.Lock()

    async def on_frame(data: str, width: int, height: int) -> None:
        # WebSocket 不允许并发发送，这里串行化。
        # 帧是从浏览器循环投递过来的，和下面的 meta 心跳是两个来源。
        async with lock:
            await websocket.send_json({
                "type": "frame", "data": data, "width": width, "height": height,
            })

    async def pump_input() -> None:
        """收前端的事件。

        ⚠️ 必须和下面的心跳**并发**跑，不能串在一起：心跳每 3 秒才醒一次，
        把 receive 放进那个循环的话，拖滑块时每个 mousemove 最多要等 3 秒
        才被处理——轨迹全断，验证必然过不了。
        """
        while True:
            event = await websocket.receive_json()
            if not isinstance(event, dict):
                continue
            if event.get("type") == "takeover":
                view.set_takeover(bool(event.get("on")))
                async with lock:
                    await websocket.send_json({
                        "type": "status",
                        "state": "takeover" if view.takeover else "streaming",
                        "message": ("已接管：你的鼠标键盘会直接作用在采集页面上，"
                                    "处理完记得退出接管"
                                    if view.takeover else
                                    "只读预览：这里的点击不会传给采集浏览器"),
                    })
                continue
            await view.handle_input(event)

    token = None
    reader = None
    try:
        token = await view.attach(on_frame)
        async with lock:
            await websocket.send_json({
                "type": "status", "state": "streaming",
                "message": "只读预览：这里的点击不会传给采集浏览器",
            })
        reader = asyncio.create_task(pump_input())
        while True:
            if lv.get(view_id) is None:
                async with lock:
                    await websocket.send_json({
                        "type": "status", "state": "gone", "message": "采集已结束",
                    })
                break
            if reader.done():
                break          # 前端断了，receive 会抛出来
            info = await view.info()
            async with lock:
                await websocket.send_json({"type": "meta", "data": info})
            # ⚠️ 不能用 asyncio.sleep(3)：前端断开后要等这一觉睡完才走到 finally，
            #    也就是**接管还会多开着最多 3 秒**。那几秒里新连上来的人一点画面
            #    就直接作用到采集页面上了，而他根本没点过「接管」。
            #    改成"等心跳周期，但 reader 一结束就立刻醒"。
            await asyncio.wait({reader}, timeout=3)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("实时画面 WebSocket 异常：%s", exc)
    finally:
        # 断开就退出接管：人都走了还留着输入通道，下一个连上来的人
        # 会在完全不知情的情况下"一点就生效"
        try:
            view.set_takeover(False)
        except Exception:  # noqa: BLE001
            pass
        if reader is not None:
            reader.cancel()
        if token is not None:
            try:
                await view.detach(token)
            except Exception:  # noqa: BLE001
                pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
