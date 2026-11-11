# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""采集任务运行记录（表 src_opr_crawl_task_run_di）：任务每执行一次一条。

- 开始时 begin() 插一条「运行中」，记下这时任务日志表里的最大 id
- 结束时 finish() 写结局、耗时、采集量，再记一次最大 id——两个 id 之间就是这次运行的日志，
  不用改老的日志表（src_opinion_crawl_task_log 没有运行编号这一列）
- 服务重启时 mark_interrupted() 把本机还挂着「运行中」的记成「中断」

写运行记录失败**不能**影响任务本身：调用方（scheduler/runner.py）都包了 try。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core.db import Database
from ..db import tables

__all__ = ["RunRepository", "RUN_STATUS_LABELS", "TRIGGER_LABELS", "TRIGGER_HINTS", "trigger_of_task"]

RUN_STATUS_LABELS = {
    "running": "运行中", "completed": "完成", "failed": "失败", "canceled": "已取消",
    "waiting": "排队等账号", "interrupted": "中断",
}
#: 触发方式的叫法：一律四个字，表格里一行放得下；细的说明放在 TRIGGER_HINTS（悬停提示）
TRIGGER_LABELS = {
    "manual": "手动执行", "schedule": "定时触发", "once": "立即执行",
    "resume": "排队恢复", "dispatch": "集群派发",
}
TRIGGER_HINTS = {
    "manual": "在任务列表 / 任务详情里点了「执行」",
    "schedule": "到了定时设置的时间自动跑（定时一次、固定间隔、cron）",
    "once": "任务的调度方式是「立即执行一次」，保存后马上跑",
    "resume": "上次没有可用账号在排队，等到账号后自动接着跑",
    "dispatch": "集群主机把任务派给了这台机器",
}

def trigger_of_task(task: Dict[str, Any]) -> str:
    """按启动 / 派发**之前**任务在库里的状态，判断这次是怎么触发的（不含手动，手动由调用方直接给）。"""
    status = task.get("status")
    if status == "waiting":
        return "resume"
    if task.get("schedule_type") == "once" and status == "pending":
        return "once"
    return "schedule"


_LIST_COLUMNS = (
    "id", "run_no", "task_id", "task_name", "scenic_id", "scenic_name", "channel_code",
    "trigger_type", "run_status", "start_time", "end_time", "duration_seconds",
    "new_work_qty", "updated_work_qty", "new_comment_qty", "updated_comment_qty",
    "error_message", "error_log_qty", "warn_log_qty", "wait_retry_qty", "node_id", "travel_date",
)


class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ 写
    async def _max_log_id(self, task_id: str) -> int:
        return int(await self.db.fetch_value(
            f"SELECT COALESCE(MAX(id), 0) AS m FROM `{tables.TASK_LOG}` WHERE task_id = %s",
            [task_id], 0) or 0)

    async def begin(self, task: Dict[str, Any], *, trigger_type: str, node_id: str,
                    scenic_name: str = "") -> str:
        """插一条「运行中」，返回运行编号。

        这个任务最近一条记录还是「排队等账号」时，**接着用那一条**（重试次数 +1），不再新建：
        以前排队期间每分钟试一次、每次都新建一条，运行记录被同一个任务刷满一屏。
        排队恢复、在运行记录 / 任务列表里点「立即开始」都一样——点的那一行直接变成运行中。
        """
        reused = await self._reuse_waiting(task["task_id"], node_id)
        if reused:
            return reused
        now = datetime.now()
        run_no = f"R{now:%Y%m%d%H%M%S}{uuid.uuid4().hex[:4]}"
        channels = task.get("channels") or []
        if isinstance(channels, str):
            try:
                channels = json.loads(channels)
            except ValueError:
                channels = [channels]
        params = task.get("params")
        await self.db.execute(
            f"INSERT INTO `{tables.TASK_RUN}` (run_no, task_id, task_name, scenic_id, scenic_name, "
            f"channel_code, trigger_type, run_status, start_time, node_id, log_start_id, "
            f"params_snapshot, travel_date) VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s, %s, %s, %s, %s)",
            [run_no, task["task_id"], (task.get("task_name") or "")[:200],
             (task.get("scenic_id") or "")[:100], (scenic_name or "")[:100],
             ",".join(str(c) for c in channels)[:100], trigger_type[:20], now, (node_id or "")[:100],
             await self._max_log_id(task["task_id"]),
             json.dumps(params, ensure_ascii=False, default=str) if params else None,
             f"{now:%Y%m%d}"])
        return run_no

    async def _reuse_waiting(self, task_id: str, node_id: str) -> str:
        row = await self.db.fetch_one(
            f"SELECT run_no, run_status FROM `{tables.TASK_RUN}` WHERE task_id = %s "
            f"ORDER BY id DESC LIMIT 1", [task_id])
        if not row or row.get("run_status") != "waiting":
            return ""
        await self.db.execute(
            f"UPDATE `{tables.TASK_RUN}` SET run_status = 'running', end_time = NULL, "
            f"error_message = NULL, wait_retry_qty = wait_retry_qty + 1, node_id = %s "
            f"WHERE run_no = %s AND run_status = 'waiting'", [(node_id or "")[:100], row["run_no"]])
        return str(row["run_no"])

    async def cancel_waiting(self, task_id: str, reason: str = "排队中被手动取消") -> int:
        """任务的排队被取消了：它那条「排队等账号」的记录记成「已取消」（不然一直挂着排队）。"""
        return int(await self.db.execute(
            f"UPDATE `{tables.TASK_RUN}` SET run_status = 'canceled', end_time = NOW(), "
            f"error_message = %s WHERE task_id = %s AND run_status = 'waiting'",
            [reason[:4000], task_id]) or 0)

    async def task_states(self, task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """这些任务现在的状态（运行记录列表上的「立即开始 / 停止 / 暂停定时」要按它决定）。"""
        ids = [t for t in dict.fromkeys(task_ids) if t]
        if not ids:
            return {}
        marks = ", ".join(["%s"] * len(ids))
        rows = await self.db.fetch_all(
            f"SELECT task_id, status, schedule_type, schedule_enabled FROM `{tables.TASK}` "
            f"WHERE task_id IN ({marks})", ids)
        return {r["task_id"]: r for r in rows}

    async def set_scenic_name(self, run_no: str, scenic_name: str) -> None:
        await self.db.execute(
            f"UPDATE `{tables.TASK_RUN}` SET scenic_name = %s WHERE run_no = %s",
            [(scenic_name or "")[:100], run_no])

    async def finish(self, run_no: str, task_id: str, *, status: str, started: datetime,
                     stats: Optional[Dict[str, int]] = None, error: str = "") -> None:
        stats = stats or {}
        end = datetime.now()
        run = await self.db.fetch_one(
            f"SELECT log_start_id FROM `{tables.TASK_RUN}` WHERE run_no = %s", [run_no])
        log_end = await self._max_log_id(task_id)
        # 错误 / 警告条数：状态是「完成」也可能某个平台被跳过了，列表上要看得出来
        counts = await self.db.fetch_one(
            f"SELECT SUM(level = 'ERROR') AS e, SUM(level IN ('WARN', 'WARNING')) AS w "
            f"FROM `{tables.TASK_LOG}` WHERE task_id = %s AND id > %s AND id <= %s",
            [task_id, int((run or {}).get("log_start_id") or 0), log_end]) or {}
        await self.db.execute(
            f"UPDATE `{tables.TASK_RUN}` SET run_status = %s, end_time = %s, duration_seconds = %s, "
            f"new_work_qty = %s, updated_work_qty = %s, new_comment_qty = %s, updated_comment_qty = %s, "
            f"error_message = %s, error_log_qty = %s, warn_log_qty = %s, log_end_id = %s WHERE run_no = %s",
            [status, end, max(0, int((end - started).total_seconds())),
             int(stats.get("new_works", 0)), int(stats.get("updated_works", 0)),
             int(stats.get("new_comments", 0)), int(stats.get("updated_comments", 0)),
             (error or None) and error[:4000], int(counts.get("e") or 0), int(counts.get("w") or 0),
             log_end, run_no])

    async def mark_interrupted(self, node_id: str) -> int:
        """服务重启：本机还挂着「运行中」的，进程已经没了，记成「中断」。"""
        return int(await self.db.execute(
            f"UPDATE `{tables.TASK_RUN}` SET run_status = 'interrupted', end_time = NOW(), "
            f"duration_seconds = GREATEST(TIMESTAMPDIFF(SECOND, start_time, NOW()), 0), "
            f"error_message = COALESCE(error_message, '运行中服务重启，这次运行没有正常结束') "
            f"WHERE run_status = 'running' AND node_id = %s", [node_id]) or 0)

    async def delete(self, run_nos: List[str], *, with_logs: bool = True) -> Dict[str, Any]:
        """删运行记录。还在跑的不删（跑完会回写结局，删了它会写到空处）。

        with_logs：顺带删掉这次运行自己的那段日志（task_log 里 (log_start_id, log_end_id]）。
        ⚠️ 只删**区间是封闭的**：中断 / 异常结束的记录可能没有 log_end_id，
        那样的「区间」往后是开的，会把同一任务之后几次运行的日志也删掉——这种只删记录不删日志。
        """
        run_nos = [str(r) for r in dict.fromkeys(run_nos) if r]
        if not run_nos:
            return {"deleted": 0, "skipped_running": 0, "deleted_logs": 0}
        marks = ", ".join(["%s"] * len(run_nos))
        rows = await self.db.fetch_all(
            f"SELECT run_no, task_id, run_status, log_start_id, log_end_id FROM `{tables.TASK_RUN}` "
            f"WHERE run_no IN ({marks})", run_nos)
        running = [r for r in rows if r.get("run_status") == "running"]
        targets = [r for r in rows if r.get("run_status") != "running"]
        deleted_logs = 0
        if with_logs:
            for r in targets:
                start, end = int(r.get("log_start_id") or 0), int(r.get("log_end_id") or 0)
                if end > start:
                    deleted_logs += int(await self.db.execute(
                        f"DELETE FROM `{tables.TASK_LOG}` WHERE task_id = %s AND id > %s AND id <= %s",
                        [r["task_id"], start, end]) or 0)
        deleted = 0
        if targets:
            keep = ", ".join(["%s"] * len(targets))
            deleted = int(await self.db.execute(
                f"DELETE FROM `{tables.TASK_RUN}` WHERE run_status <> 'running' AND run_no IN ({keep})",
                [r["run_no"] for r in targets]) or 0)
        return {"deleted": deleted, "skipped_running": len(running), "deleted_logs": deleted_logs}

    # ------------------------------------------------------------------ 读
    @staticmethod
    def _where(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
        conds, args = [], []
        for key in ("task_id", "scenic_id", "run_status", "trigger_type"):
            if filters.get(key):
                conds.append(f"`{key}` = %s")
                args.append(filters[key])
        if filters.get("channel"):
            conds.append("FIND_IN_SET(%s, `channel_code`)")
            args.append(filters["channel"])
        if filters.get("keyword"):
            conds.append("(`task_name` LIKE %s OR `run_no` = %s)")
            args += [f"%{filters['keyword']}%", filters["keyword"]]
        for key, op in (("start_date", ">="), ("end_date", "<=")):
            value = filters.get(key)
            if value:
                day = value.strftime("%Y%m%d") if isinstance(value, (date, datetime)) else str(value).replace("-", "")[:8]
                conds.append(f"`travel_date` {op} %s")
                args.append(day)
        return (("WHERE " + " AND ".join(conds)) if conds else ""), args

    async def list(self, filters: Dict[str, Any], *, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        where, args = self._where(filters)
        total = int(await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{tables.TASK_RUN}` {where}", args, 0) or 0)
        page_size = min(max(int(page_size), 1), 200)
        offset = (max(int(page), 1) - 1) * page_size
        rows = await self.db.fetch_all(
            f"SELECT {', '.join(_LIST_COLUMNS)} FROM `{tables.TASK_RUN}` {where} "
            f"ORDER BY id DESC LIMIT %s OFFSET %s", [*args, page_size, offset])
        return {"items": [self._shape(r) for r in rows], "total": total,
                "page": max(int(page), 1), "page_size": page_size}

    async def get(self, run_no: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            f"SELECT * FROM `{tables.TASK_RUN}` WHERE run_no = %s", [run_no])
        if not row:
            return None
        item = self._shape(row)
        try:
            item["params_snapshot"] = json.loads(row.get("params_snapshot") or "null")
        except ValueError:
            item["params_snapshot"] = None
        item["log_start_id"] = int(row.get("log_start_id") or 0)
        item["log_end_id"] = int(row.get("log_end_id") or 0)
        return item

    async def logs(self, run: Dict[str, Any], *, after_id: int = 0, limit: int = 500) -> List[Dict]:
        """这次运行的日志：task_log 里 (log_start_id, log_end_id] 这一段；运行中的不设上界。"""
        lower = max(int(after_id), int(run.get("log_start_id") or 0))
        sql = (f"SELECT id, log_time, level, channel, message FROM `{tables.TASK_LOG}` "
               f"WHERE task_id = %s AND id > %s")
        args: List[Any] = [run["task_id"], lower]
        if run.get("run_status") != "running" and int(run.get("log_end_id") or 0) > 0:
            sql += " AND id <= %s"
            args.append(int(run["log_end_id"]))
        sql += " ORDER BY id ASC LIMIT %s"
        args.append(min(max(int(limit), 1), 2000))
        return await self.db.fetch_all(sql, args)

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        item = {k: row.get(k) for k in _LIST_COLUMNS}
        item["channels"] = [c for c in (row.get("channel_code") or "").split(",") if c]
        item["run_status_label"] = RUN_STATUS_LABELS.get(row.get("run_status") or "", row.get("run_status") or "")
        item["trigger_label"] = TRIGGER_LABELS.get(row.get("trigger_type") or "", row.get("trigger_type") or "")
        item["trigger_hint"] = TRIGGER_HINTS.get(row.get("trigger_type") or "", "")
        if item.get("run_status") == "running" and isinstance(row.get("start_time"), datetime):
            item["duration_seconds"] = max(0, int((datetime.now() - row["start_time"]).total_seconds()))
        return item
