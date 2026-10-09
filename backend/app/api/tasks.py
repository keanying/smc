"""任务 API：创建（含 cron）、列表、详情、立即执行、取消、日志。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from ..core import collect_params, content_filter, search_filters
from ..core.constants import CHANNELS_POI_BASED
from ..repositories.task_repo import compute_next_run, validate_cron
from .common import bad_request, not_found, ok
from .deps import AppState, get_state
from .schemas import TaskIn, TaskPatchIn

router = APIRouter(prefix="/api/tasks", tags=["任务"])


def _content_filter_summary(task: dict) -> dict:
    """内容过滤的当前配置 + 一句人话说明，任务详情页直接显示。

    只存不显示的话，用户改完设置没法确认存对没有——
    而这条设置直接决定"采回来的东西有多少被丢掉"。
    """
    rules = content_filter.ContentFilter.from_params(task.get("params") or {})
    return {**rules.to_params(), "description": rules.describe()}


def _filters_summary(task: dict) -> list:
    """每个平台最终生效的搜索条件，前端直接显示。"""
    params = task.get("params") or {}
    rows = []
    for channel in task.get("channels") or []:
        if channel in CHANNELS_POI_BASED:
            continue          # 点评型平台没有关键字搜索
        resolved = search_filters.resolve(channel, params)
        rows.append({**resolved.to_dict(), "description": resolved.describe()})
    return rows


@router.get("")
async def list_tasks(
    status: str = "", scenic_id: str = "", keyword: str = "",
    page: int = 1, page_size: int = 20,
    state: AppState = Depends(get_state),
):
    result = await state.tasks.list(
        status=status, scenic_id=scenic_id, keyword=keyword,
        page=page, page_size=page_size,
    )
    running = state.scheduler.running_task_ids()
    for item in result["items"]:
        item["is_running"] = item["task_id"] in running
        item["collect_limits"] = collect_params.summarize(
            state.config, item.get("params") or {}, item.get("channels") or []
        )
        item["search_filters"] = _filters_summary(item)
        item["content_filter"] = _content_filter_summary(item)
    return ok(result)


@router.post("")
async def create_task(payload: TaskIn, state: AppState = Depends(get_state)):
    data = payload.model_dump()

    # 关键字模式必须有关键字；没显式传就从景区自动带（默认上限见配置）
    if data["collect_type"] == "keyword" and not data["keywords"]:
        if not data.get("scenic_id"):
            raise bad_request("关键字采集必须提供 keywords，或指定 scenic_id 以便从景区自动带入")
        limit = int(state.config.get("crawl.default_keyword_limit", 100))
        rows = await state.scenics.list_keywords(
            data["scenic_id"], enabled_only=True, limit=limit
        )
        data["keywords"] = [r["keyword"] for r in rows]
        if not data["keywords"]:
            raise bad_request(
                f"景区 {data['scenic_id']} 下没有启用的关键字，请先在景区管理里添加"
            )

    # 携程/同程必须有 POI 目标，否则任务跑起来只会空转
    poi_channels = [c for c in data["channels"] if c in CHANNELS_POI_BASED]
    if poi_channels and data.get("scenic_id") and not data.get("targets"):
        for channel in poi_channels:
            targets = await state.scenics.list_targets(
                data["scenic_id"], channel=channel, target_type="poi", enabled_only=True
            )
            if not targets:
                label = "POI_ID" if channel == "ctrip" else "sid"
                raise bad_request(
                    f"选择了 {channel} 但景区 {data['scenic_id']} 没有配置 {label}，"
                    f"请先到景区管理 → 采集目标里添加"
                )

    if data["schedule_type"] == "cron":
        try:
            validate_cron(data.get("cron_expression") or "")
        except ValueError as exc:
            raise bad_request(str(exc))
    if data["schedule_type"] == "at" and not data.get("schedule_at"):
        raise bad_request("定时执行必须指定 schedule_at")
    if data["schedule_type"] == "interval" and not data.get("schedule_interval_seconds"):
        raise bad_request("间隔执行必须指定 schedule_interval_seconds")

    data["params"] = collect_params.normalize(data.get("params"), data["channels"])

    task_id = await state.tasks.create(data)
    task = await state.tasks.get(task_id)
    task["collect_limits"] = collect_params.summarize(
        state.config, task.get("params") or {}, task.get("channels") or []
    )
    task["search_filters"] = _filters_summary(task)
    task["content_filter"] = _content_filter_summary(task)
    return ok(task, "任务已创建" + ("，将立即执行" if data["schedule_type"] == "once" else ""))


@router.get("/filter-options")
async def filter_options():
    """新建/编辑任务的筛选表单要渲染什么。

    每个平台支持的排序档位不一样，快手干脆没有——前端按这个结果渲染，
    不用在前端硬编码一份，也就不会和后端的实现走偏。
    """
    return ok({
        "channels": search_filters.all_options(),
        "publish_within": search_filters.PUBLISH_WITHIN_OPTIONS,
    })


@router.post("/preview-schedule")
async def preview_schedule(
    schedule_type: str,
    cron_expression: str = "",
    schedule_interval_seconds: Optional[int] = None,
    schedule_at: Optional[str] = None,
    timezone: str = "Asia/Shanghai",
    count: int = Query(5, ge=1, le=20),
):
    """新建任务页面上的"预览下次执行时间"，让用户确认 cron 写对了。"""
    try:
        if schedule_type == "cron":
            validate_cron(cron_expression)
            from croniter import croniter
            fields = cron_expression.split()
            iterator = croniter(
                cron_expression, datetime.now(), second_at_beginning=len(fields) == 6
            )
            runs = [iterator.get_next(datetime).strftime("%Y-%m-%d %H:%M:%S")
                    for _ in range(count)]
        else:
            next_run = compute_next_run(
                schedule_type,
                schedule_at=datetime.fromisoformat(schedule_at) if schedule_at else None,
                interval_seconds=schedule_interval_seconds,
                cron_expression=cron_expression or None,
                timezone=timezone,
            )
            runs = [next_run.strftime("%Y-%m-%d %H:%M:%S")] if next_run else ["创建后立即执行"]
    except ValueError as exc:
        raise bad_request(str(exc))
    return ok({"next_runs": runs})


@router.get("/{task_id}")
async def get_task(task_id: str, state: AppState = Depends(get_state)):
    task = await state.tasks.get(task_id)
    if not task:
        raise not_found(f"任务不存在：{task_id}")
    task["is_running"] = task_id in state.scheduler.running_task_ids()
    task["collect_limits"] = collect_params.summarize(
        state.config, task.get("params") or {}, task.get("channels") or []
    )
    task["search_filters"] = _filters_summary(task)
    task["content_filter"] = _content_filter_summary(task)
    return ok(task)


@router.put("/{task_id}")
async def update_task(task_id: str, payload: TaskIn, state: AppState = Depends(get_state)):
    if not await state.tasks.get(task_id):
        raise not_found(f"任务不存在：{task_id}")
    data = payload.model_dump()
    if data["schedule_type"] == "cron":
        try:
            validate_cron(data.get("cron_expression") or "")
        except ValueError as exc:
            raise bad_request(str(exc))
    next_run = compute_next_run(
        data["schedule_type"],
        schedule_at=datetime.fromisoformat(data["schedule_at"]) if data.get("schedule_at") else None,
        interval_seconds=data.get("schedule_interval_seconds"),
        cron_expression=data.get("cron_expression"),
        timezone=data.get("timezone", "Asia/Shanghai"),
    )
    await state.tasks.update(task_id, {
        "task_name": data["task_name"], "scenic_id": data.get("scenic_id"),
        "channels": data["channels"], "collect_type": data["collect_type"],
        "keywords": data["keywords"], "targets": data["targets"],
        "params": collect_params.normalize(data["params"], data["channels"]),
        "schedule_type": data["schedule_type"],
        "schedule_enabled": int(data["schedule_type"] != "once"),
        "schedule_at": datetime.fromisoformat(data["schedule_at"]) if data.get("schedule_at") else None,
        "schedule_interval_seconds": data.get("schedule_interval_seconds"),
        "cron_expression": data.get("cron_expression"),
        "timezone": data.get("timezone", "Asia/Shanghai"),
        "next_run_time": next_run,
    })
    return ok(await state.tasks.get(task_id), "已更新")


@router.patch("/{task_id}")
async def patch_task(task_id: str, payload: TaskPatchIn, state: AppState = Depends(get_state)):
    """局部修改任务：改运行模式、改采集数量，不用重建任务。

    任务正在跑时也允许改——本轮已经在跑的平台用的是启动时那份配置，
    改动从下一轮开始生效，返回的 message 会说清楚这一点。
    """
    task = await state.tasks.get(task_id)
    if not task:
        raise not_found(f"任务不存在：{task_id}")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise bad_request("没有要修改的内容")

    fields: dict = {}

    for key in ("task_name", "collect_type", "keywords", "channels", "timezone"):
        if key in changes and changes[key] is not None:
            fields[key] = changes[key]

    # 调度相关：改了 schedule_type 就要把整组字段一起算，
    # 否则会留下"模式是 cron 但 cron_expression 还是空"这种半截状态
    schedule_touched = any(
        key in changes for key in
        ("schedule_type", "schedule_at", "schedule_interval_seconds",
         "cron_expression", "schedule_enabled")
    )
    if schedule_touched:
        schedule_type = changes.get("schedule_type") or task["schedule_type"]
        cron_expression = changes.get("cron_expression", task.get("cron_expression"))
        interval = changes.get("schedule_interval_seconds", task.get("schedule_interval_seconds"))
        schedule_at = changes.get("schedule_at")
        if schedule_at is None:
            schedule_at = task.get("schedule_at")
        elif isinstance(schedule_at, str):
            try:
                schedule_at = datetime.fromisoformat(schedule_at)
            except ValueError:
                raise bad_request(f"schedule_at 格式不对：{schedule_at}")
        timezone = fields.get("timezone") or task.get("timezone") or "Asia/Shanghai"

        if schedule_type == "cron":
            try:
                validate_cron(cron_expression or "")
            except ValueError as exc:
                raise bad_request(str(exc))
        if schedule_type == "at" and not schedule_at:
            raise bad_request("定时执行必须指定 schedule_at")
        if schedule_type == "interval" and not interval:
            raise bad_request("间隔执行必须指定 schedule_interval_seconds")

        enabled = changes.get("schedule_enabled")
        if enabled is None:
            enabled = schedule_type != "once"

        try:
            next_run = compute_next_run(
                schedule_type,
                schedule_at=schedule_at,
                interval_seconds=interval,
                cron_expression=cron_expression,
                timezone=timezone,
            ) if enabled else None
        except ValueError as exc:
            raise bad_request(str(exc))

        fields.update({
            "schedule_type": schedule_type,
            "schedule_at": schedule_at,
            "schedule_interval_seconds": interval,
            "cron_expression": cron_expression,
            "schedule_enabled": int(bool(enabled)),
            "next_run_time": next_run,
        })

    # 采集数量：平台列表可能同时被改，用改后的列表来清洗
    if "params" in changes and changes["params"] is not None:
        fields["params"] = collect_params.normalize(
            changes["params"], fields.get("channels") or task.get("channels") or []
        )
    elif "channels" in fields:
        # 平台变了，把已取消平台的残留数量配置一并清掉
        fields["params"] = collect_params.normalize(
            task.get("params") or {}, fields["channels"]
        )

    await state.tasks.update(task_id, fields)
    updated = await state.tasks.get(task_id)
    is_running = task_id in state.scheduler.running_task_ids()
    updated["is_running"] = is_running
    updated["collect_limits"] = collect_params.summarize(
        state.config, updated.get("params") or {}, updated.get("channels") or []
    )
    updated["search_filters"] = _filters_summary(updated)
    updated["content_filter"] = _content_filter_summary(updated)
    return ok(
        updated,
        "已保存；任务正在运行，本次改动从下一轮开始生效" if is_running else "已保存",
    )


@router.delete("/{task_id}")
async def delete_task(task_id: str, state: AppState = Depends(get_state)):
    await state.scheduler.cancel(task_id)
    await state.tasks.delete(task_id)
    return ok(message="任务及其日志已删除")


@router.post("/{task_id}/run")
async def run_task(task_id: str, state: AppState = Depends(get_state)):
    try:
        started = await state.scheduler.trigger_now(task_id)
    except KeyError as exc:
        raise not_found(str(exc))
    except RuntimeError as exc:
        raise bad_request(str(exc))
    return ok({"started": started}, "已开始执行" if started else "任务正在运行中")


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, state: AppState = Depends(get_state)):
    cancelled = await state.scheduler.cancel(task_id)
    return ok({"cancelled": cancelled}, "已发送取消信号" if cancelled else "任务当前不在运行")


@router.put("/{task_id}/schedule-enabled")
async def toggle_schedule(
    task_id: str, enabled: bool = Query(...), state: AppState = Depends(get_state)
):
    await state.tasks.update(task_id, {"schedule_enabled": int(enabled)})
    return ok(message="已启用定时" if enabled else "已暂停定时")


@router.get("/{task_id}/logs")
async def task_logs(
    task_id: str, after_id: int = 0, limit: int = 500,
    state: AppState = Depends(get_state),
):
    return ok(await state.tasks.list_logs(task_id, after_id=after_id, limit=limit))


@router.delete("/{task_id}/logs")
async def clear_logs(task_id: str, state: AppState = Depends(get_state)):
    await state.tasks.clear_logs(task_id)
    return ok(message="日志已清空")
