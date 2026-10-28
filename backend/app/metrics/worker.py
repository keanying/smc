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
from .config import DEFAULT_PUSH_PK as _SMC_PK, ENGINE_DIR, TABLES, push_keys_for

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


def push_tables(st, tables: Dict[str, Any], cfg: Dict[str, Any], scenic: str = ""):
    """把一个景区算出来的表按配置推出去。返回 engine 的 PushReport。

    推哪几张表按景区定（push_keys_for）：单独配过的景区按它自己的，其余按各表的推送开关。
    """
    from engin_cli.pusher import DEFAULT_PUSH_PK, push_all

    push = cfg["push"]
    allowed = set(push_keys_for(cfg, scenic))
    by_table = {t["table"]: (t["key"], cfg["tables"][t["key"]]) for t in TABLES}
    shaped, pk_map, names = {}, {}, {}
    for table, df in tables.items():
        key, tc = by_table.get(table, (None, None))
        if tc is None or key not in allowed:
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


def fetch_drill_keywords(db, scenics: List[str]) -> Dict[str, List[str]]:
    """下钻表「只收含采集关键字的评论」用的词：本景区**启用的**主关键字 + 附关键字。

    ⚠️ 不能用引擎自己的 fetch_scenic_keywords：它把 src_opinion_scenic_filter_word 整张表
       都当成采集关键字，不看 kind 也不看 enabled——smc 这张表里还放着「过滤关键字」
       （代运营、加微信这类，命中就该丢的词），照它的查法，含这些词的评论反而会被收进下钻表；
       停用的关键字也照样生效。所以在这里按 smc 的表结构查好，直接交给引擎。
    """
    from engin_cli.source import _keyword_map

    codes = [str(s).strip() for s in scenics if str(s).strip()]
    if not codes:
        return {}
    marks = ",".join(["%s"] * len(codes))
    src = db.cfg.src_db()
    sql = (f"SELECT scenic_id, keyword AS word FROM `{src}`.`src_opinion_scenic_keyword` "
           f"WHERE scenic_id IN ({marks}) AND enabled = 1\n"
           f"UNION\n"
           f"SELECT scenic_id, word FROM `{src}`.`src_opinion_scenic_filter_word` "
           f"WHERE scenic_id IN ({marks}) AND kind = 'aux' AND enabled = 1")
    return _keyword_map(db.query_df(sql, codes + codes))


TABLE_DRILL = "ads_trf_social_opinion_drill_analysis_di"
TABLE_MACRO = "ads_trf_social_opinion_macro_gran_metric_di"


def wordcloud_pairs(macro) -> set:
    """大盘表 wordcloud_map 里出现过的 (词, 情感类型)。情感类型和下钻表 emotion_type 同一套。"""
    from engin_cli.metric_calc_domain import MACRO_WORD_GROUP_KEY, SENTIMENT_TYPE

    group_type = {key: SENTIMENT_TYPE[s] for s, key in MACRO_WORD_GROUP_KEY.items()}
    pairs = set()
    if macro is None or macro.empty or "wordcloud_map" not in macro.columns:
        return pairs
    for raw in macro["wordcloud_map"].dropna().unique():
        try:
            groups = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for key, etype in group_type.items():
            for item in groups.get(key) or []:
                word = str((item or {}).get("word") or "").strip()
                if word:
                    pairs.add((word, etype))
    return pairs


def comments_with_keywords(db, scenic: str, comment_ids, words: List[str]) -> set:
    """这些评论里，评论内容含任一关键字的（不分大小写，和引擎的判定一致；只看评论，不看作品标题）。"""
    lowered = [w.lower() for w in words if w]
    ids = sorted({str(c) for c in comment_ids if str(c)})
    hit: set = set()
    if not lowered or not ids:
        return hit
    src = db.cfg.src_db()
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        df = db.query_df(
            f"SELECT comment_id, content FROM `{src}`.`src_opinion_social_work_comment_di` "
            f"WHERE scenic_id = %s AND comment_id IN ({','.join(['%s'] * len(chunk))})",
            [scenic] + chunk)
        for cid, content in zip(df["comment_id"], df["content"]):
            text = str(content or "").lower()
            if any(w in text for w in lowered):
                hit.add(str(cid))
    return hit


def filter_drill(db, scenic: str, tables: Dict[str, Any]) -> str:
    """「只收含采集关键字的评论」：下钻表留下
        ① 评论内容含本景区启用的主 / 附关键字的评论（这条评论的全部词行），以及
        ② 词（+ 情感）出现在大盘表 wordcloud_map 里的词行——看板上点词云要能下钻到评论。
    原地改 tables，返回一句给日志看的说明。"""
    drill = tables.get(TABLE_DRILL)
    if drill is None or drill.empty:
        return ""
    words = fetch_drill_keywords(db, [scenic]).get(scenic, [])
    passing = comments_with_keywords(db, scenic, drill["comment_id"], words)
    cloud = wordcloud_pairs(tables.get(TABLE_MACRO))
    by_kw = drill["comment_id"].astype(str).isin(passing)
    by_cloud = [(str(w), str(t)) in cloud
                for w, t in zip(drill["emotion_word"], drill["emotion_type"])]
    import pandas as pd
    by_cloud = pd.Series(by_cloud, index=drill.index)
    tables[TABLE_DRILL] = drill[by_kw | by_cloud].reset_index(drop=True)
    head = (f"下钻明细按关键字过滤（只看评论内容）：{'、'.join(words[:10])}"
            f"{' 等 ' + str(len(words)) + ' 个' if len(words) > 10 else ''}" if words
            else "下钻明细按关键字过滤：这个景区没有启用的主关键字 / 附关键字")
    return (f"{head}；保留 {int((by_kw | by_cloud).sum())} 行（评论含关键字 {int(by_kw.sum())} 行，"
            f"另因词在大盘词云里 {int((by_cloud & ~by_kw).sum())} 行），去掉 {int((~(by_kw | by_cloud)).sum())} 行")


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
                drill_note = ""
                if st.drill_keyword_filter and st.enable_drill_analysis:
                    # 下钻表要按「评论含关键字 ∪ 大盘词云里的词」过滤，而引擎是先出下钻表、
                    # 后出大盘表的，没法在它里面做。所以：先全量算、不落库 → 在这里过滤下钻表
                    # → 再用引擎自己的 load_all 落库（同样是按景区 + 日期先删后写、一个事务）
                    res = run_pipeline(replace(st, drill_keyword_filter=False, write_db=False),
                                       dates, scenic_codes=[sc], db=db)
                    drill_note = filter_drill(db, sc, res.tables)
                    if st.write_db:          # 和引擎一致：校验有告警也照常落库
                        from engin_cli.loader import load_all
                        res.load_results = load_all(db, res.tables, settings=st,
                                                    scenics=[sc], dates=sorted(dates))
                else:
                    res = run_pipeline(st, dates, scenic_codes=[sc], db=db)
                item.update(
                    ok=res.ok, errors=res.errors[:5], notes=res.notes[:20],
                    tables={t: int(len(df)) for t, df in res.tables.items()},
                    loads=[{"table": r.table, "rows": r.rows, "deleted": r.deleted}
                           for r in res.load_results])
                if drill_note:
                    item["notes"].append(drill_note)
                if push_on and res.ok:
                    rep = push_tables(st, res.tables, cfg, sc)
                    if sc in (cfg.get("scenic_push") or {}):
                        item["notes"].append("按景区单独配置推送：" + (
                            "、".join(push_keys_for(cfg, sc)) or "这个景区不推送"))
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

