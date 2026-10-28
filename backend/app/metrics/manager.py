# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""指标计算在 smc 里的门面：配置、运行记录、调起子进程、定时。

和采集是两条互不相干的线：自己的配置、自己的定时循环、自己的运行记录表，
计算在子进程里跑（worker.py）。采集挂了不影响它，它挂了也不影响采集。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..core.config import Config
from ..core.db import Database
from ..core.logging import get_logger
from ..core.subprocess_loop import SUBPROCESS_LOOP
from ..db import tables as T
from . import config as mc
from .common import EVENT_PREFIX, date_list

logger = get_logger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]
#: 运行日志在库里最多留多少字（只留尾巴：出事的地方一般在最后）
LOG_MAX_CHARS = 200_000
#: 定时循环多久看一次表
TICK_SECONDS = 20
#: 取字段目录（子进程 import pandas + 引擎）最多等多久
CATALOG_TIMEOUT = 120


class MetricsManager:
    def __init__(self, config: Config, db: Database, settings_repo):
        self.config = config
        self.db = db
        self.settings = settings_repo
        self._proc: Optional[asyncio.subprocess.Process] = None
        #: 子进程所在的循环（SUBPROCESS_LOOP），停止时要在它上面发信号
        self._proc_loop: Optional[asyncio.AbstractEventLoop] = None
        self._run_id: Optional[str] = None
        self._cancel_requested = False
        self._catalog: Optional[Dict[str, Any]] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._next_fire: Optional[datetime] = None
        self._next_fire_cron: str = ""

    # ------------------------------------------------------------------ 开关 / 配置
    @property
    def enabled(self) -> bool:
        return bool((self.config.get("metrics") or {}).get("enabled"))

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.config.get("scheduler.timezone", "Asia/Shanghai"))
        except Exception:  # noqa: BLE001
            return ZoneInfo("Asia/Shanghai")

    def _now(self) -> datetime:
        return datetime.now(self._tz()).replace(tzinfo=None)

    async def get_config(self) -> Dict[str, Any]:
        stored = await self.settings.get(mc.SETTING_KEY, None)
        try:
            return mc.normalize(stored if isinstance(stored, dict) else {}, previous=stored)
        except ValueError as exc:
            # 库里那份坏了（手改过 / 版本升级字段变了）：别让整个页面打不开，退回默认值并说出来
            logger.warning("[指标] 库里的指标配置不合法，按默认值处理：%s", exc)
            return mc.default_config()

    async def save_config(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        previous = await self.settings.get(mc.SETTING_KEY, None)
        cfg = mc.normalize(raw, previous=previous if isinstance(previous, dict) else None)
        await self.settings.set(mc.SETTING_KEY, cfg)
        self._next_fire = None              # 定时改了，下一次触发时间重新算
        return cfg

    # ------------------------------------------------------------------ 字段目录
    async def catalog(self) -> List[Dict[str, Any]]:
        """六张表：表名、中文名、表注释、每个字段（名 + 中文说明）、默认主键。"""
        if self._catalog is None:
            events = await self._run_worker(["catalog"], timeout=CATALOG_TIMEOUT)
            found = next((e for e in events if e.get("event") == "catalog"), None)
            if not found:
                err = next((e.get("error") for e in events if e.get("error")), "")
                raise RuntimeError(f"读取指标表字段失败：{err or '子进程没有返回结果'}")
            self._catalog = found["tables"]
        comments = mc.ddl_comments()
        out = []
        for t in mc.TABLES:
            info = self._catalog.get(t["table"]) or {}
            notes = comments.get(t["table"]) or {}
            out.append({
                **t, "comment": notes.get("comment", ""),
                "pk": mc.DEFAULT_PUSH_PK.get(t["key"]) or info.get("pk", []),
                "columns": [{"name": c, "comment": (notes.get("columns") or {}).get(c, "")}
                            for c in info.get("columns", [])],
            })
        return out

    async def _run_worker(self, args: List[str], *, stdin: bytes = b"",
                          timeout: float = 60) -> List[Dict[str, Any]]:
        # ⚠️ 子进程一律在 SUBPROCESS_LOOP 上开：Windows + uvicorn --reload 时服务器循环是
        #    SelectorEventLoop，直接 create_subprocess_exec 会抛 NotImplementedError
        return await SUBPROCESS_LOOP.run(self._collect(args, stdin, timeout))

    async def _collect(self, args: List[str], stdin: bytes, timeout: float) -> List[Dict[str, Any]]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.metrics.worker", *args, cwd=str(BACKEND_DIR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._env())
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"指标子进程 {timeout:.0f} 秒没有返回")
        events = _parse_events(out.decode("utf-8", "replace"))
        if proc.returncode and not events:
            raise RuntimeError(err.decode("utf-8", "replace").strip()[-800:] or "子进程异常退出")
        return events

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in [str(BACKEND_DIR), env.get("PYTHONPATH", "")] if p)
        env["PYTHONIOENCODING"] = "utf-8"
        env["OPINION_IGNORE_LOCAL_SETTINGS"] = "1"
        return env

    # ------------------------------------------------------------------ ADS 表
    def _ads_db(self, cfg: Dict[str, Any]) -> str:
        return cfg["params"].get("ads_database") or self.config.get("mysql.database")

    async def table_status(self) -> List[Dict[str, Any]]:
        cfg = await self.get_config()
        schema = self._ads_db(cfg)
        rows = await self.db.fetch_all(
            "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s", [schema])
        found = {r["TABLE_NAME"]: int(r["TABLE_ROWS"] or 0) for r in rows}
        return [{"key": t["key"], "table": t["table"], "label": t["label"], "database": schema,
                 "exists": t["table"] in found, "approx_rows": found.get(t["table"], 0)}
                for t in mc.TABLES]

    async def init_tables(self) -> List[Dict[str, Any]]:
        """按引擎自带的建表语句建六张 ADS 表（已有的不动）。"""
        cfg = await self.get_config()
        schema = self._ads_db(cfg)
        before = {s["table"]: s["exists"] for s in await self.table_status()}
        await self.db.execute(f"CREATE DATABASE IF NOT EXISTS `{schema}` "
                              f"DEFAULT CHARACTER SET utf8mb4")
        out = []
        for table, stmt in mc.ddl_statements().items():
            if before.get(table):
                out.append({"table": table, "result": "已存在"})
                continue
            qualified = stmt.replace(f"CREATE TABLE IF NOT EXISTS `{table}`",
                                     f"CREATE TABLE IF NOT EXISTS `{schema}`.`{table}`", 1)
            await self.db.execute(qualified)
            out.append({"table": table, "result": "已创建"})
        return out

    # ------------------------------------------------------------------ 报文预览
    async def push_preview(self, key: str) -> Dict[str, Any]:
        """按当前配置组一个真实的推送请求给人看（不发送）：地址、请求头（令牌打码）、报文。

        数据取 ADS 表里最新的一行，按配置裁剪 / 改字段名、换主键名——和真正推送时同一套规则；
        表里还没数据就用空值占位。用来对着下游给的接口样例核对格式。
        """
        table = next((t for t in mc.TABLES if t["key"] == key), None)
        if table is None:
            raise ValueError(f"没有这张表：{key}")
        cfg = await self.get_config()
        push, tc = cfg["push"], cfg["tables"][key]
        columns = next((c["columns"] for c in await self.catalog() if c["key"] == key), [])
        names = [c["name"] for c in columns]

        row: Dict[str, Any] = {}
        note = ""
        status = {s["key"]: s for s in await self.table_status()}
        if status[key]["exists"]:
            found = await self.db.fetch_one(
                f"SELECT * FROM `{status[key]['database']}`.`{table['table']}` "
                f"ORDER BY id DESC LIMIT 1")
            row = {c: _jsonable(found.get(c)) for c in names} if found else {}
        if not row:
            row = {c: None for c in names}
            note = "表里还没有数据，字段值用空值占位"

        fields = tc.get("fields") or []
        if fields:
            rename = {f["column"]: f["name"] for f in fields}
            row = {rename[f["column"]]: row.get(f["column"]) for f in fields}
        else:
            rename = {}
        pk = [rename.get(c, c) for c in (tc.get("pk") or mc.DEFAULT_PUSH_PK.get(key, []))]
        body = {push["body_fields"]["table"]: tc.get("push_name") or table["table"],
                push["body_fields"]["pk"]: pk, push["body_fields"]["data"]: [row],
                **(push.get("extra_fields") or {})}
        headers = {"Content-Type": "application/json"}
        if push.get("token"):
            headers[push["token_header"]] = f"{push['token_prefix']}{_mask(push['token'])}"
        text = json.dumps(body, ensure_ascii=False, indent=2)
        curl = (f"curl --location --request POST '{push['url'] or '（未填推送地址）'}' \\\n"
                + "".join(f"--header '{k}: {v}' \\\n" for k, v in headers.items())
                + f"--data-raw '{text}'")
        return {"url": push["url"], "headers": headers, "body": body, "curl": curl,
                "note": note, "has_token": bool(push.get("token")),
                "push": tc.get("push", True), "enabled": push["enabled"]}

    # ------------------------------------------------------------------ 运行
    @property
    def running_run_id(self) -> Optional[str]:
        return self._run_id

    async def start_run(self, *, trigger: str = "manual", start_date: str = "",
                        end_date: str = "", scenics: Optional[List[str]] = None,
                        no_push: bool = False) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("指标计算没有开启：到「系统设置 → 指标计算」打开")
        if self._run_id:
            raise RuntimeError(f"已经有一次计算在跑（{self._run_id}），等它结束或先停止")
        cfg = await self.get_config()
        start_date = start_date or _yesterday(self._now())
        end_date = end_date or start_date
        date_list(start_date, end_date)                 # 日期不合法在这里就报
        scenics = [s for s in (scenics if scenics is not None else cfg["scenics"]) if s]
        push = bool(cfg["push"]["enabled"]) and not no_push

        # 记录里的时间和 smc 其它地方一样用服务器本地时间；只有 cron 按调度时区算
        run_id = f"mr{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
        await self.db.execute(
            f"INSERT INTO `{T.METRIC_RUN}` (run_id, trigger_type, status, start_date, end_date, "
            f"scenics, push, started_at) VALUES (%s, %s, 'running', %s, %s, %s, %s, %s)",
            [run_id, trigger, start_date, end_date, json.dumps(scenics, ensure_ascii=False),
             int(push), datetime.now()])
        work_dir = Path(self.config.get("server.data_dir")) / "metrics" / run_id
        job = {"config": cfg, "mysql": self.config.get("mysql"), "start_date": start_date,
               "end_date": end_date, "scenics": scenics, "no_push": no_push,
               "work_dir": str(work_dir)}
        self._run_id = run_id
        self._cancel_requested = False
        asyncio.create_task(self._execute(run_id, job))
        logger.info("[指标] 开始计算 %s：%s ~ %s，景区 %s", run_id, start_date, end_date,
                    "、".join(scenics) or "全部")
        return await self.get_run(run_id)

    # ------------------------------------------------------------------ 演示数据
    async def mock_check(self, to_scenic: str) -> Dict[str, Any]:
        """目标景区在本系统里是不是已经有真实数据——有的话，演示数据会覆盖下游里它的真实数据。"""
        to_scenic = str(to_scenic or "").strip()
        cfg = await self.get_config()
        comments = await self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM `src_opinion_social_work_comment_di` WHERE scenic_id = %s",
            [to_scenic])
        ads_rows = 0
        status = {s["key"]: s for s in await self.table_status()}
        if status["core"]["exists"]:
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS c FROM `{status['core']['database']}`."
                f"`ads_trf_social_opinion_comment_core_di` WHERE scenic_spot_code = %s",
                [to_scenic])
            ads_rows = int(row["c"] or 0)
        scenic = await self.db.fetch_one(
            "SELECT scenic_name FROM `src_opinion_social_scenic` WHERE scenic_id = %s", [to_scenic])
        return {"to_scenic": to_scenic, "comments": int(comments["c"] or 0), "ads_rows": ads_rows,
                "in_config": to_scenic in cfg["scenics"],
                "scenic_name": scenic["scenic_name"] if scenic else ""}

    async def start_mock(self, *, from_scenic: str, to_scenic: str, to_name: str,
                         start_date: str, end_date: str, tables: List[str],
                         dry_run: bool = False) -> Dict[str, Any]:
        """演示数据：复制 from_scenic 的结果给 to_scenic 并推送（不写本地库）。和计算共用一个运行位。"""
        if not self.enabled:
            raise RuntimeError("指标计算没有开启：到「系统设置 → 指标计算」打开")
        if self._run_id:
            raise RuntimeError(f"已经有一次计算在跑（{self._run_id}），等它结束或先停止")
        from_scenic, to_scenic, to_name = (str(from_scenic or "").strip(),
                                           str(to_scenic or "").strip(), str(to_name or "").strip())
        if not from_scenic or not to_scenic or not to_name:
            raise ValueError("源景区、目标景区编号、目标景区名称都要填")
        if from_scenic == to_scenic:
            raise ValueError("目标景区编号和源景区一样：这样会用演示数据覆盖真实景区，请换一个编号")
        keys = [t["key"] for t in mc.TABLES]
        bad = [k for k in tables if k not in keys]
        if bad or not tables:
            raise ValueError("要选推哪几张表" if not tables else f"不认识的表：{'、'.join(bad)}")
        if not start_date or not end_date:
            raise ValueError("要选日期区间")
        date_list(start_date, end_date)
        cfg = await self.get_config()
        if not dry_run and not cfg["push"]["url"]:
            raise ValueError("还没配推送地址：到「推送」页填好再来，或者先勾「只组报文不发送」")

        run_id = f"mk{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
        await self.db.execute(
            f"INSERT INTO `{T.METRIC_RUN}` (run_id, trigger_type, status, start_date, end_date, "
            f"scenics, push, started_at) VALUES (%s, 'mock', 'running', %s, %s, %s, 1, %s)",
            [run_id, start_date, end_date,
             json.dumps([from_scenic, to_scenic], ensure_ascii=False), datetime.now()])
        job = {"mode": "mock", "config": cfg, "mysql": self.config.get("mysql"),
               "from_scenic": from_scenic, "to_scenic": to_scenic, "to_name": to_name,
               "start_date": start_date, "end_date": end_date, "tables": tables,
               "dry_run": bool(dry_run),
               "work_dir": str(Path(self.config.get("server.data_dir")) / "metrics" / run_id)}
        self._run_id = run_id
        self._cancel_requested = False
        asyncio.create_task(self._execute(run_id, job))
        logger.info("[指标] 演示数据 %s：%s → %s（%s），%s ~ %s，表 %s%s", run_id, from_scenic,
                    to_scenic, to_name, start_date, end_date, "、".join(tables),
                    "（只组报文不发送）" if dry_run else "")
        return await self.get_run(run_id)

    async def cancel(self, run_id: str) -> bool:
        if run_id != self._run_id or self._proc is None:
            return False
        self._cancel_requested = True
        self._terminate()
        return True

    def _terminate(self) -> None:
        """子进程属于 SUBPROCESS_LOOP，从它自己的循环上发信号。"""
        proc, loop = self._proc, self._proc_loop
        if proc is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(proc.terminate)
        except RuntimeError:              # 循环已经关了：进程也跟着没了
            pass

    async def _execute(self, run_id: str, job: Dict[str, Any]) -> None:
        """调起子进程跑一次计算。

        读子进程输出在 SUBPROCESS_LOOP 上（理由见 _run_worker），写运行记录在服务器循环上
        （数据库连接池属于它）。两边通过 _RunState 交换：子进程那边往里写，这边每 2 秒存一次库。
        """
        state = _RunState()
        status, error = "failed", ""
        saver = asyncio.create_task(self._autosave(run_id, state))
        try:
            Path(job["work_dir"]).mkdir(parents=True, exist_ok=True)
            code = await SUBPROCESS_LOOP.run(self._stream(job, state))
            end_event = state.end_event
            if self._cancel_requested:
                status, error = "canceled", "手动停止"
            elif end_event.get("error"):
                error = str(end_event["error"])[:1000]
            elif code == 0 and end_event.get("ok"):
                status = "success"
            elif state.summary and any(s.get("ok") for s in state.summary):
                status = "partial"
                error = f"{end_event.get('failed', '?')} 个景区失败（详见每个景区的结果）"
            else:
                error = (end_event.get("note") or f"子进程退出码 {code}")[:1000]
        except Exception as exc:  # noqa: BLE001
            logger.exception("[指标] 计算 %s 异常", run_id)
            error = f"{type(exc).__name__}: {exc}"[:1000]
            state.log.add(f"[smc] {error}")
        finally:
            saver.cancel()
            self._proc = self._proc_loop = None
            self._run_id = None
            await self._finish(run_id, status, error, state.summary_copy(), state.log.text())
            logger.info("[指标] 计算 %s 结束：%s %s", run_id, status, error)

    async def _stream(self, job: Dict[str, Any], state: "_RunState") -> int:
        """（跑在 SUBPROCESS_LOOP 上）起子进程、喂作业、逐行读输出。返回退出码。"""
        # 作业（含数据库密码、推送令牌）走 stdin，不放命令行——ps 里看得见命令行
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.metrics.worker", "run", cwd=str(BACKEND_DIR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self._env(), limit=4 * 1024 * 1024)
        self._proc, self._proc_loop = proc, asyncio.get_running_loop()
        if self._cancel_requested:
            proc.terminate()
        proc.stdin.write(json.dumps(job, ensure_ascii=False, default=str).encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            state.feed(raw.decode("utf-8", "replace").rstrip())
        return await proc.wait()

    async def _autosave(self, run_id: str, state: "_RunState") -> None:
        while True:
            await asyncio.sleep(2)
            if state.dirty:
                state.dirty = False
                await self._save_progress(run_id, state.summary_copy(), state.log.text())

    async def _save_progress(self, run_id: str, summary, log_text: str) -> None:
        try:
            await self.db.execute(
                f"UPDATE `{T.METRIC_RUN}` SET summary = %s, log = %s WHERE run_id = %s",
                [json.dumps(summary, ensure_ascii=False), log_text, run_id])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[指标] 保存进度失败（忽略）：%s", exc)

    async def _finish(self, run_id, status, error, summary, log_text) -> None:
        await self.db.execute(
            f"UPDATE `{T.METRIC_RUN}` SET status = %s, error = %s, summary = %s, log = %s, "
            f"finished_at = %s WHERE run_id = %s",
            [status, error, json.dumps(summary, ensure_ascii=False), log_text, datetime.now(),
             run_id])

    async def list_runs(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        total = await self.db.fetch_one(f"SELECT COUNT(*) AS c FROM `{T.METRIC_RUN}`")
        rows = await self.db.fetch_all(
            f"SELECT run_id, trigger_type, status, start_date, end_date, scenics, push, "
            f"summary, error, started_at, finished_at FROM `{T.METRIC_RUN}` "
            f"ORDER BY id DESC LIMIT %s OFFSET %s", [page_size, (page - 1) * page_size])
        return {"items": [_decode_run(r) for r in rows], "total": int(total["c"] or 0)}

    async def get_run(self, run_id: str, *, with_log: bool = False) -> Optional[Dict[str, Any]]:
        cols = "*" if with_log else ("run_id, trigger_type, status, start_date, end_date, "
                                     "scenics, push, summary, error, started_at, finished_at")
        row = await self.db.fetch_one(
            f"SELECT {cols} FROM `{T.METRIC_RUN}` WHERE run_id = %s", [run_id])
        return _decode_run(row) if row else None

    async def recover(self) -> None:
        """服务启动时：上次没跑完的记录标成失败（子进程已经跟着老服务没了）。"""
        n = await self.db.execute(
            f"UPDATE `{T.METRIC_RUN}` SET status = 'failed', error = '服务重启，计算被中断', "
            f"finished_at = %s WHERE status = 'running'", [datetime.now()])
        if n:
            logger.info("[指标] %d 条上次没跑完的计算记录已标为中断", n)

    # ------------------------------------------------------------------ 定时
    async def start(self) -> None:
        await self.recover()
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._schedule_loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
        if self._proc is not None:
            self._cancel_requested = True
            self._terminate()

    async def next_fire(self) -> Optional[datetime]:
        cfg = await self.get_config()
        if not (self.enabled and cfg["schedule"]["enabled"]):
            return None
        cron = cfg["schedule"]["cron"]
        if self._next_fire is None or self._next_fire_cron != cron:
            from croniter import croniter
            self._next_fire = croniter(cron, self._now()).get_next(datetime)
            self._next_fire_cron = cron
        return self._next_fire

    async def _schedule_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                fire = await self.next_fire()
                if fire is None or self._now() < fire:
                    continue
                self._next_fire = None          # 下一次从现在往后算
                if self._run_id:
                    logger.warning("[指标] 定时到了，但上一次计算还没结束（%s），这一次跳过",
                                   self._run_id)
                    continue
                await self.start_run(trigger="schedule")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 定时循环不能因为一次失败就停掉
                logger.warning("[指标] 定时触发失败：%s", exc)

    async def status(self) -> Dict[str, Any]:
        fire = await self.next_fire()
        return {"enabled": self.enabled, "running_run_id": self._run_id,
                "next_fire": fire.strftime("%Y-%m-%d %H:%M") if fire else None}


# ---------------------------------------------------------------------- 小工具
class _LogBuffer:
    """运行日志：只留尾巴，超长就从前面丢。两个线程（两个事件循环）都会碰它，加锁。"""

    def __init__(self) -> None:
        self._parts: List[str] = []
        self._size = 0
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {line}"
        with self._lock:
            self._parts.append(line)
            self._size += len(line) + 1
            while self._size > LOG_MAX_CHARS and len(self._parts) > 1:
                self._size -= len(self._parts.pop(0)) + 1

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._parts)


class _RunState:
    """一次运行的进度：子进程输出在 SUBPROCESS_LOOP 线程里写进来，服务器循环读出去存库。"""

    def __init__(self) -> None:
        self.log = _LogBuffer()
        self.summary: List[Dict[str, Any]] = []
        self.end_event: Dict[str, Any] = {}
        self.dirty = False
        self._lock = threading.Lock()

    def feed(self, line: str) -> None:
        if line.startswith(EVENT_PREFIX):
            event = _parse_event(line)
            if not event:
                return
            self.log.add(_describe(event))
            with self._lock:
                if event.get("event") == "scenic":
                    self.summary.append({k: v for k, v in event.items() if k != "event"})
                elif event.get("event") == "end":
                    self.end_event = event
        else:
            self.log.add(line)
        self.dirty = True

    def summary_copy(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.summary)


def _mask(secret: str) -> str:
    """令牌只露头尾几位，够认出是哪一个，又不至于泄露。"""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:6]}****{secret[-2:]}"


def _jsonable(v: Any) -> Any:
    """库里读出来的值 → 推送报文里的样子（和引擎 pusher._jsonable 一致：数字仍是数字，时间是字符串）。"""
    from decimal import Decimal

    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _yesterday(now: datetime) -> str:
    return (now - timedelta(days=1)).strftime("%Y%m%d")


def _parse_event(line: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(line[len(EVENT_PREFIX):])
    except ValueError:
        return None


def _parse_events(text: str) -> List[Dict[str, Any]]:
    return [e for e in (_parse_event(ln) for ln in text.splitlines()
                        if ln.startswith(EVENT_PREFIX)) if e]


def _describe(event: Dict[str, Any]) -> str:
    """事件 → 日志里给人看的一行。"""
    kind = event.get("event")
    if kind == "start" and event.get("mock"):
        m = event["mock"]
        d = event.get("dates") or ["", ""]
        return (f"演示数据：{m.get('from')} → {m.get('to')}（{m.get('to_name')}），"
                f"日期 {d[0]} ~ {d[-1]}；只推送不写本地库"
                + ("，只组报文不发送" if m.get("dry_run") else ""))
    if kind == "start":
        d = event.get("dates") or ["", ""]
        sc = event.get("scenics") or []
        return (f"开始：目标日期 {d[0]} ~ {d[-1]}（{event.get('days')} 天，含往前重跑），"
                f"景区 {len(sc)} 个；{'写 ADS 表' if event.get('write_db') else '不写库'}"
                f"{'，算完推送' if event.get('push') else ''}")
    if kind == "scenic":
        head = f"[{event.get('index')}/{event.get('total')}] 景区 {event.get('scenic')} "
        if not event.get("ok"):
            return head + "✗ " + "；".join(event.get("errors") or ["失败"])
        rows = sum((event.get("tables") or {}).values())
        timing = event.get("timing") or {}
        stages = "、".join(f"{label} {timing[k]}s" for k, label in (
            ("compute", "计算"), ("drill_filter", "下钻过滤"), ("load", "写库"), ("push", "推送"),
            ("mock", "演示数据"))
            if k in timing)
        text = head + f"✓ 产出 {rows} 行（{event.get('elapsed')}s" + (f"：{stages}" if stages else "") + "）"
        pushes = event.get("push") or []
        if pushes:
            bad = [p for p in pushes if not p.get("ok") and not p.get("skipped")]
            text += "，推送 " + ("✓" if not bad else
                                "✗ " + "；".join(f"{p['table']}：{p['error']}" for p in bad[:3]))
        for m in event.get("mock") or []:
            label = f"\n    └ 演示数据 → {m.get('to_name')}（{m.get('to_scenic')}）："
            if m.get("skipped") and not m.get("push"):
                text += label + f"没推（{m['skipped']}）"
            elif m.get("ok"):
                text += label + f"✓ 推送 {m.get('rows', 0)} 行"
            else:
                bad = [p for p in m.get("push") or [] if not p.get("ok") and not p.get("skipped")]
                text += label + "✗ " + "；".join(f"{p['table']}：{p['error']}" for p in bad[:3])
        return text
    if kind == "end":
        if event.get("error"):
            return f"结束：✗ {event['error']}"
        return (f"结束：{'全部成功' if event.get('ok') else str(event.get('failed')) + ' 个景区失败'}"
                f"，用时 {event.get('elapsed')}s" + (f"（{event['note']}）" if event.get("note") else ""))
    return json.dumps(event, ensure_ascii=False)


def _decode_run(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("scenics", "summary"):
        try:
            out[key] = json.loads(out.get(key) or ("[]"))
        except ValueError:
            out[key] = []
    out["push"] = bool(out.get("push"))
    for key in ("started_at", "finished_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].strftime("%Y-%m-%d %H:%M:%S")
    return out


_manager: Optional[MetricsManager] = None


def get_manager(config: Config = None, db: Database = None, settings_repo=None) -> MetricsManager:
    global _manager
    if _manager is None:
        if config is None or db is None or settings_repo is None:
            raise RuntimeError("指标计算管理器还没初始化")
        _manager = MetricsManager(config, db, settings_repo)
    return _manager
