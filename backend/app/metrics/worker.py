"""指标计算子进程：`python -m app.metrics.worker run|catalog`，作业从 stdin 读 JSON。

为什么单独一个进程
------------------
引擎是 pandas 全内存计算，一个大景区算半年数据要几百 MB、几分钟 CPU。
放进采集服务的进程里：内存一冲高、或者底层库崩了，采集服务跟着一起倒；
算的那几分钟还会把事件循环的线程池占着。子进程里跑，最坏也只是这一次计算失败。
主服务也因此不需要 import pandas。

和主进程怎么说话
----------------
stdout 一行一个事件，前缀 EVENT_PREFIX + JSON（开始 / 每个景区算完 / 推送结果 / 结束）；
引擎自己的日志走 stderr，主进程原样收进运行日志。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from dataclasses import replace
from typing import Any, Dict, List, Optional

from .common import EVENT_PREFIX, date_list
from .config import DEFAULT_PUSH_PK as _SMC_PK, ENGINE_DIR, TABLES

#: smc 这边的默认主键（对齐下游网关），按表名查
DEFAULT_PK_BY_TABLE = {t["table"]: _SMC_PK[t["key"]] for t in TABLES if t["key"] in _SMC_PK}


def emit(event: str, **data: Any) -> None:
    print(EVENT_PREFIX + json.dumps({"event": event, **data}, ensure_ascii=False, default=str),
          flush=True)


def _load_engine():
    # 上游的 settings_local.py 是线上库和线上推送凭据，绝不能被读进来；配置全部由 smc 传入
    os.environ["OPINION_IGNORE_LOCAL_SETTINGS"] = "1"
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))


# ---------------------------------------------------------------- 字段目录
def catalog() -> Dict[str, Any]:
    """每张表引擎实际产出的列和默认主键（页面上维护推送字段用）。"""
    _load_engine()
    from engin_cli.mock import TABLE_COLUMNS
    from engin_cli.pusher import DEFAULT_PUSH_PK

    return {t["table"]: {"columns": list(TABLE_COLUMNS.get(t["table"], [])),
                         "pk": list(DEFAULT_PUSH_PK.get(t["table"], []))}
            for t in TABLES}


# ---------------------------------------------------------------- 计算
def build_settings(job: Dict[str, Any]):
    """smc 的配置 → 引擎的 EtlSettings。推送在这里一律关掉：推送由 push_tables() 做字段映射后再推。"""
    from engin_cli.settings import DBConfig, EtlSettings

    mysql, p = job["mysql"], job["config"]["params"]
    st = EtlSettings()
    st.db = DBConfig(host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
                     password=mysql.get("password") or "", database=mysql["database"],
                     charset=mysql.get("charset") or "utf8mb4",
                     ads_database=p.get("ads_database") or None)
    st.lookback_days = int(p["lookback_days"])
    st.rerun_days = int(p["rerun_days"])
    st.backfill_mode = p["backfill_mode"]
    st.backfill_lookback = int(p["backfill_lookback"])
    st.enable_drill_analysis = bool(p["enable_drill_analysis"])
    st.drill_keyword_filter = bool(p["drill_keyword_filter"])
    st.drill_mask_content = bool(p["drill_mask_content"])
    st.enable_macro_metric = bool(p["enable_macro_metric"])
    st.content_top_words = int(p["content_top_words"])
    st.score_weights = dict(p["score_weights"])
    st.write_db = bool(p["write_db"])
    st.write_csv = False
    st.output_dir = job["work_dir"]
    st.push_enabled = False
    st.push_log_dir = job["work_dir"]
    return st


def shape_for_push(table: str, df, table_cfg: Dict[str, Any], default_pk: List[str]):
    """按页面上配的字段裁剪 / 改名。返回 (DataFrame, 下游主键)。

    fields 为空 = 全部字段原名推送。主键按本地列名配置，跟着字段改名一起换成下游的名字。
    """
    fields = table_cfg.get("fields") or []
    pk = list(table_cfg.get("pk") or default_pk)
    if not fields:
        return df, pk
    keep = [f for f in fields if f["column"] in df.columns]
    rename = {f["column"]: f["name"] for f in keep}
    out = df[[f["column"] for f in keep]].rename(columns=rename)
    return out, [rename.get(c, c) for c in pk]


def push_tables(st, tables: Dict[str, Any], cfg: Dict[str, Any]):
    """把一个景区算出来的表按配置推出去。返回 engine 的 PushReport。"""
    from engin_cli.pusher import DEFAULT_PUSH_PK, push_all

    push = cfg["push"]
    by_table = {t["table"]: cfg["tables"][t["key"]] for t in TABLES}
    shaped, pk_map, names = {}, {}, {}
    for table, df in tables.items():
        tc = by_table.get(table)
        if tc is None or not tc.get("push", True):
            continue
        default_pk = DEFAULT_PK_BY_TABLE.get(table) or DEFAULT_PUSH_PK.get(table, [])
        shaped[table], pk_map[table] = shape_for_push(table, df, tc, default_pk)
        if tc.get("push_name"):
            names[table] = tc["push_name"]
    headers = {}
    if push.get("token"):
        headers[push.get("token_header") or "Authorization"] = \
            f"{push.get('token_prefix') or ''}{push['token']}"
    pst = replace(
        st, push_enabled=True, push_url=push["url"], push_headers=headers or None,
        push_body_fields=dict(push["body_fields"]),
        push_extra_fields=dict(push.get("extra_fields") or {}) or None,
        push_table_names=names or None, push_pk=pk_map, push_tables=list(shaped),
        push_batch_size=int(push["batch_size"]), push_timeout=float(push["timeout"]),
        push_retries=int(push["retries"]), push_dry_run=bool(push.get("dry_run")),
        push_success_field=push.get("success_field") or "")
    return push_all(shaped, settings=pst)


def run(job: Dict[str, Any]) -> int:
    _load_engine()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")   # 时间由主进程统一加
    from engin_cli.db import MySQL
    from engin_cli.pipeline import compute_load_range, run as run_pipeline
    from engin_cli.source import list_scenic_codes

    cfg = job["config"]
    st = build_settings(job)
    dates = date_list(job["start_date"], job["end_date"], int(cfg["params"]["rerun_days"]))
    push_on = bool(cfg["push"]["enabled"]) and not job.get("no_push")
    t0 = time.time()

    db = MySQL(st.db)
    try:
        scenics = list(job.get("scenics") or cfg.get("scenics") or [])
        if not scenics:
            lo, hi = compute_load_range(dates, st)
            scenics = sorted(list_scenic_codes(db, lo, hi))
        emit("start", dates=[dates[0], dates[-1]], days=len(dates), scenics=scenics,
             push=push_on, write_db=st.write_db)
        if not scenics:
            emit("end", ok=True, elapsed=round(time.time() - t0, 1),
                 note="取数区间内没有任何景区的评论")
            return 0

        failed = 0
        for i, sc in enumerate(scenics, 1):
            t1 = time.time()
            item: Dict[str, Any] = {"scenic": sc, "index": i, "total": len(scenics)}
            try:
                res = run_pipeline(st, dates, scenic_codes=[sc], db=db)
                item.update(
                    ok=res.ok, errors=res.errors[:5], notes=res.notes[:20],
                    tables={t: int(len(df)) for t, df in res.tables.items()},
                    loads=[{"table": r.table, "rows": r.rows, "deleted": r.deleted}
                           for r in res.load_results])
                if push_on and res.ok:
                    rep = push_tables(st, res.tables, cfg)
                    item["push"] = [{"table": r.table, "rows": r.rows, "batches": r.batches,
                                     "failed_batches": r.failed_batches, "ok": r.ok,
                                     "skipped": r.skipped, "error": r.error,
                                     "warning": r.warning} for r in rep.results]
                    item["push_ok"] = rep.ok
                    if any(r.unreachable for r in rep.results):
                        # 地址连不上：后面的景区推也是白等，只算、只落库
                        push_on = False
                        item["notes"].append("推送地址连不上，后面的景区只计算不推送")
            except Exception as exc:            # noqa: BLE001 —— 一个景区炸了不拖累其他景区
                logging.getLogger(__name__).exception("景区 %s 计算异常", sc)
                item.update(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
            item["elapsed"] = round(time.time() - t1, 1)
            if not item.get("ok") or item.get("push_ok") is False:
                failed += 1
            emit("scenic", **item)
        emit("end", ok=failed == 0, failed=failed, elapsed=round(time.time() - t0, 1))
        return 0 if failed == 0 else 1
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    try:
        if cmd == "catalog":
            emit("catalog", tables=catalog())
            return 0
        if cmd == "run":
            return run(json.loads(sys.stdin.read()))
        print(f"不认识的命令：{cmd}", file=sys.stderr)
        return 2
    except Exception as exc:                    # noqa: BLE001
        traceback.print_exc()
        emit("end", ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

