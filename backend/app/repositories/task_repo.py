"""任务与任务日志的读写，含 cron/interval/at 四种调度模式的下次运行时间计算。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from croniter import croniter

from ..core.constants import ScheduleType, TaskStatus
from ..core.db import Database
from ..core.logging import get_logger

logger = get_logger(__name__)

JSON_FIELDS = ("channels", "keywords", "targets", "params")


def new_task_id() -> str:
    return uuid.uuid4().hex


def compute_next_run(
    schedule_type: str,
    *,
    schedule_at: Optional[datetime] = None,
    interval_seconds: Optional[int] = None,
    cron_expression: Optional[str] = None,
    timezone: str = "Asia/Shanghai",
    base_time: Optional[datetime] = None,
) -> Optional[datetime]:
    """算出下一次该跑的时间；once（立即执行一次）返回 None，由创建时直接入队。"""
    now = base_time or datetime.now()

    if schedule_type == ScheduleType.ONCE.value:
        return None

    if schedule_type == ScheduleType.AT.value:
        if not schedule_at:
            raise ValueError("定时执行必须指定 schedule_at")
        return schedule_at if schedule_at > now else None

    if schedule_type == ScheduleType.INTERVAL.value:
        if not interval_seconds or interval_seconds <= 0:
            raise ValueError("间隔执行必须指定大于 0 的 schedule_interval_seconds")
        return now + timedelta(seconds=int(interval_seconds))

    if schedule_type == ScheduleType.CRON.value:
        if not cron_expression:
            raise ValueError("cron 模式必须指定 cron_expression")
        validate_cron(cron_expression)
        try:
            tz = ZoneInfo(timezone)
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("Asia/Shanghai")
        local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
        # second_at_beginning=True：6 段表达式按 Quartz/Spring 习惯解析成
        # "秒 分 时 日 月 周"。croniter 默认把秒放在末尾，与国内常见写法相反，
        # 不显式指定会让 "30 * * * * *" 被理解成"每分钟第 30 分"这种荒谬结果。
        iterator = croniter(cron_expression, local_now, second_at_beginning=True)
        return iterator.get_next(datetime).replace(tzinfo=None)

    raise ValueError(f"不支持的调度类型：{schedule_type}")


def validate_cron(expression: str) -> None:
    """校验 cron 表达式。

    支持两种：
      5 段 "分 时 日 月 周"      —— 标准 crontab，例如 0 2 * * *（每天 2 点）
      6 段 "秒 分 时 日 月 周"   —— Quartz 风格，例如 0 0 2 * * *（每天 2 点整）
    """
    if not expression or not expression.strip():
        raise ValueError("cron 表达式不能为空")
    fields = expression.split()
    if len(fields) not in (5, 6):
        raise ValueError(
            f"cron 表达式必须是 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周），"
            f"当前是 {len(fields)} 段：{expression}"
        )
    if not croniter.is_valid(expression, second_at_beginning=len(fields) == 6):
        raise ValueError(f"cron 表达式非法：{expression}")


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---------------- 增删改 ----------------
    async def create(self, data: Dict[str, Any]) -> str:
        task_id = data.get("task_id") or new_task_id()
        schedule_type = data.get("schedule_type", ScheduleType.ONCE.value)
        schedule_at = data.get("schedule_at")
        if isinstance(schedule_at, str) and schedule_at:
            schedule_at = datetime.fromisoformat(schedule_at)

        next_run = compute_next_run(
            schedule_type,
            schedule_at=schedule_at,
            interval_seconds=data.get("schedule_interval_seconds"),
            cron_expression=data.get("cron_expression"),
            timezone=data.get("timezone", "Asia/Shanghai"),
        )
        # once 模式创建后立刻排队；其余模式等调度器到点触发
        schedule_enabled = schedule_type != ScheduleType.ONCE.value

        await self.db.execute(
            """
            INSERT INTO `src_opinion_crawl_task`
                (task_id, task_name, scenic_id, channels, collect_type, keywords, targets,
                 params, status, schedule_type, schedule_enabled, schedule_at,
                 schedule_interval_seconds, cron_expression, timezone, next_run_time, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                task_id,
                data["task_name"],
                data.get("scenic_id"),
                json.dumps(data.get("channels") or [], ensure_ascii=False),
                data.get("collect_type", "keyword"),
                json.dumps(data.get("keywords") or [], ensure_ascii=False),
                json.dumps(data.get("targets") or [], ensure_ascii=False),
                json.dumps(data.get("params") or {}, ensure_ascii=False),
                TaskStatus.PENDING.value,
                schedule_type,
                int(schedule_enabled),
                schedule_at,
                data.get("schedule_interval_seconds"),
                data.get("cron_expression"),
                data.get("timezone", "Asia/Shanghai"),
                next_run,
                data.get("created_by"),
            ],
        )
        return task_id

    async def update(self, task_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        sets: List[str] = []
        args: List[Any] = []
        for key, value in fields.items():
            if key in JSON_FIELDS and not isinstance(value, str):
                value = json.dumps(value or ([] if key != "params" else {}), ensure_ascii=False)
            sets.append(f"`{key}` = %s")
            args.append(value)
        args.append(task_id)
        await self.db.execute(
            f"UPDATE `src_opinion_crawl_task` SET {', '.join(sets)} WHERE task_id = %s", args
        )

    async def delete(self, task_id: str) -> None:
        async with self.db.transaction() as cur:
            await cur.execute("DELETE FROM `src_opinion_crawl_task_log` WHERE task_id = %s", [task_id])
            await cur.execute("DELETE FROM `src_opinion_crawl_task` WHERE task_id = %s", [task_id])

    # ---------------- 查询 ----------------
    async def get(self, task_id: str) -> Optional[Dict]:
        row = await self.db.fetch_one("SELECT * FROM `src_opinion_crawl_task` WHERE task_id = %s", [task_id])
        return _decode(row) if row else None

    async def list(
        self, *, status: str = "", scenic_id: str = "", keyword: str = "",
        page: int = 1, page_size: int = 20,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        args: List[Any] = []
        if status:
            clauses.append("status = %s")
            args.append(status)
        if scenic_id:
            clauses.append("scenic_id = %s")
            args.append(scenic_id)
        if keyword:
            clauses.append("task_name LIKE %s")
            args.append(f"%{keyword}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = await self.db.fetch_value(f"SELECT COUNT(*) AS c FROM `src_opinion_crawl_task` {where}", args, 0)
        rows = await self.db.fetch_all(
            f"SELECT * FROM `src_opinion_crawl_task` {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            [*args, int(page_size), max(0, (page - 1) * page_size)],
        )
        return {
            "total": int(total or 0), "page": page, "page_size": page_size,
            "items": [_decode(r) for r in rows],
        }

    async def due_tasks(self, now: Optional[datetime] = None, limit: int = 50) -> List[Dict]:
        """取到点该跑的周期任务。"""
        moment = now or datetime.now()
        rows = await self.db.fetch_all(
            "SELECT * FROM `src_opinion_crawl_task` WHERE schedule_enabled = 1 AND next_run_time IS NOT NULL "
            "AND next_run_time <= %s AND status NOT IN ('running', 'queued') "
            "ORDER BY next_run_time ASC LIMIT %s",
            [moment, int(limit)],
        )
        return [_decode(r) for r in rows]

    async def pending_once_tasks(self, limit: int = 50) -> List[Dict]:
        """取创建后待立即执行的一次性任务。"""
        rows = await self.db.fetch_all(
            "SELECT * FROM `src_opinion_crawl_task` WHERE schedule_type = 'once' AND status = 'pending' "
            "ORDER BY id ASC LIMIT %s",
            [int(limit)],
        )
        return [_decode(r) for r in rows]

    # ---------------- 状态流转 ----------------
    async def mark_running(self, task_id: str) -> None:
        await self.db.execute(
            "UPDATE `src_opinion_crawl_task` SET status = %s, start_time = %s, end_time = NULL, progress = 0, "
            "error = NULL, last_run_time = %s, runs_count = runs_count + 1 "
            "WHERE task_id = %s",
            [TaskStatus.RUNNING.value, datetime.now(), datetime.now(), task_id],
        )

    async def mark_finished(
        self, task_id: str, status: str, *, error: Optional[str] = None,
        stats: Optional[Dict[str, int]] = None,
    ) -> None:
        stats = stats or {}
        # ⚠️ progress 是 NOT NULL DEFAULT 0 的列，不能写 NULL。
        # 原来这里对"非完成"状态传的是 None，于是**取消任务时**
        # 收尾这一步直接抛 IntegrityError(1048, "Column 'progress' cannot be null")：
        # 用户点了取消，任务却卡在 running 状态再也不动，
        # 而真正的报错藏在一个没人 await 的 Task 里（asyncio: Task exception was
        # never retrieved），页面上什么都看不到。
        # 完成 = 100；其余状态保持当前进度不变（COALESCE 自己）。
        completed = status == TaskStatus.COMPLETED.value
        await self.db.execute(
            "UPDATE `src_opinion_crawl_task` SET status = %s, end_time = %s, "
            "progress = " + ("%s" if completed else "progress") + ", error = %s, "
            "stat_new_works = stat_new_works + %s, stat_updated_works = stat_updated_works + %s, "
            "stat_new_comments = stat_new_comments + %s, stat_updated_comments = stat_updated_comments + %s "
            "WHERE task_id = %s",
            [
                status, datetime.now(),
                *([100] if completed else []),
                (error or "")[:2000] or None,
                int(stats.get("new_works", 0)), int(stats.get("updated_works", 0)),
                int(stats.get("new_comments", 0)), int(stats.get("updated_comments", 0)),
                task_id,
            ],
        )

    async def update_progress(self, task_id: str, progress: int) -> None:
        await self.db.execute(
            "UPDATE `src_opinion_crawl_task` SET progress = %s WHERE task_id = %s",
            [max(0, min(100, int(progress))), task_id],
        )

    async def reschedule(self, task: Dict[str, Any]) -> Optional[datetime]:
        """周期任务跑完后算下一次；一次性定时任务跑完就停用。

        必须以数据库里的最新值为准，不能用启动时的那份快照：
        任务跑起来之后用户可能在页面上改了运行模式（比如把 cron 改成了每 2 小时，
        或者干脆改成只跑一次）。用旧快照算，会把刚保存的 next_run_time 覆盖回去，
        用户会看到"改了没生效"。
        """
        latest = await self.get(task["task_id"])
        if latest is not None:
            task = latest

        schedule_type = task["schedule_type"]
        if schedule_type in (ScheduleType.ONCE.value, ScheduleType.AT.value):
            await self.db.execute(
                "UPDATE `src_opinion_crawl_task` SET schedule_enabled = 0, next_run_time = NULL WHERE task_id = %s",
                [task["task_id"]],
            )
            return None

        if not task.get("schedule_enabled", 1):
            # 用户在任务跑的过程中暂停了定时，不要再排下一次
            await self.db.execute(
                "UPDATE `src_opinion_crawl_task` SET next_run_time = NULL WHERE task_id = %s", [task["task_id"]]
            )
            return None

        next_run = compute_next_run(
            schedule_type,
            interval_seconds=task.get("schedule_interval_seconds"),
            cron_expression=task.get("cron_expression"),
            timezone=task.get("timezone", "Asia/Shanghai"),
        )
        await self.db.execute(
            "UPDATE `src_opinion_crawl_task` SET next_run_time = %s WHERE task_id = %s",
            [next_run, task["task_id"]],
        )
        return next_run

    async def reset_stuck_running(self) -> int:
        """服务重启时把残留的 running 任务标记为失败，避免永远卡在运行中。"""
        return await self.db.execute(
            "UPDATE `src_opinion_crawl_task` SET status = 'failed', end_time = %s, "
            "error = '服务重启，任务被中断' WHERE status IN ('running', 'queued')",
            [datetime.now()],
        )

    # ---------------- 日志 ----------------
    async def add_logs(self, rows: Sequence[Dict[str, Any]]) -> None:
        if not rows:
            return
        await self.db.execute_many(
            "INSERT INTO `src_opinion_crawl_task_log` (task_id, log_time, level, channel, message) "
            "VALUES (%s, %s, %s, %s, %s)",
            [
                [r["task_id"], r.get("log_time") or datetime.now(), r.get("level", "INFO"),
                 r.get("channel"), (r.get("message") or "")[:4000]]
                for r in rows
            ],
        )

    async def list_logs(
        self, task_id: str, *, after_id: int = 0, limit: int = 500
    ) -> List[Dict]:
        return await self.db.fetch_all(
            "SELECT * FROM `src_opinion_crawl_task_log` WHERE task_id = %s AND id > %s ORDER BY id ASC LIMIT %s",
            [task_id, int(after_id), int(limit)],
        )

    async def clear_logs(self, task_id: str) -> None:
        await self.db.execute("DELETE FROM `src_opinion_crawl_task_log` WHERE task_id = %s", [task_id])


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    """把存成字符串的 JSON 字段还原成 Python 对象。"""
    result = dict(row)
    for field in JSON_FIELDS:
        value = result.get(field)
        if isinstance(value, str) and value:
            try:
                result[field] = json.loads(value)
            except ValueError:
                result[field] = [] if field != "params" else {}
        elif value is None:
            result[field] = [] if field != "params" else {}
    return result
