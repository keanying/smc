# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""原因关键事件 · 小样本验证（只读，不改库）。

从评论表抽一批真实评论，用单独的提示词让模型摘出「好评原因 / 中评原因 / 差评原因」，
连同现有的情感和关键词一起写成 CSV，给人对照着看准不准、算一条要花多少 token。

    python scripts/reason_sample.py --text "舜帝陵可看可不看，没有意义。池神庙建筑没有意义"   # 先试几条，不连库也行
    python scripts/reason_sample.py --limit 300                          # 全库按「平台 × 现有情感」分层抽 300 条
    python scripts/reason_sample.py --limit 200 --scenic 景区ID --channel ctrip
    python scripts/reason_sample.py --limit 300 --price-in 1 --price-out 4   # 顺便按单价（元/百万 token）估费用
    python scripts/reason_sample.py --show-prompt                        # 只打印提示词

- 数据库只读：只有 SELECT，不写任何一列，现有标注不受影响。
- 模型、Key、QPS、extra_body 全部沿用「AI 标注」的配置（config.yaml + 系统设置页面）。
- 结果在 data/reason_sample/ 下，Excel 直接打开。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

COMMENTS = "src_opinion_social_work_comment_di"

CSV_HEADER = [
    "评论ID", "景区ID", "景区", "平台", "现有情感", "现有关键词", "评论原文",
    "好评原因", "中评原因", "差评原因", "原因明细(含对象)", "未通过校验",
    "原文全部搜得到", "状态", "输入tokens", "输出tokens", "耗时ms", "错误",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="原因关键事件 · 小样本验证（只读，不改库）")
    p.add_argument("--text", action="append", metavar="评论", help="直接给评论试，可写多次；给了就不抽库")
    p.add_argument("--scenic-name", default="", help="配合 --text：景区名称")
    p.add_argument("--limit", type=int, default=300, help="抽多少条（默认 300）")
    p.add_argument("--scenic", action="append", metavar="景区ID", help="只抽这些景区，可写多次")
    p.add_argument("--channel", action="append", metavar="平台", help="只抽这些平台（douyin/ctrip/…），可写多次")
    p.add_argument("--min-len", type=int, default=6, help="正文至少多少字才抽（默认 6）")
    p.add_argument("--seed", type=int, default=None, help="随机种子，固定后每次抽到同一批")
    p.add_argument("--workers", type=int, default=4, help="并发调模型的线程数（默认 4，总 QPS 仍受标注配置限制）")
    p.add_argument("--max-len", type=int, default=20, help="每条原因最长多少字（默认 20）")
    p.add_argument("--max-count", type=int, default=8, help="每条评论最多几条原因（默认 8）")
    p.add_argument("--price-in", type=float, default=0.0, help="输入单价，元/百万 token（填了才估费用）")
    p.add_argument("--price-out", type=float, default=0.0, help="输出单价，元/百万 token")
    p.add_argument("--out", default="", help="CSV 输出路径（默认 data/reason_sample/ 下按时间命名）")
    p.add_argument("--show-prompt", action="store_true", help="只打印系统提示词，不调模型")
    return p.parse_args()


# ---------------------------------------------------------------------------- 配置与模型
async def _load_config(need_db: bool):
    """读 config.yaml，能连上库就把系统设置页面上改的（模型 Key 等）合并进来。"""
    from app.core.config import load_config
    from app.core.db import Database
    from app.repositories.setting_repo import SettingRepository

    config = load_config()
    db = Database(config)
    try:
        await db.connect()
        await SettingRepository(db).apply_overrides(config)
    except Exception as exc:  # noqa: BLE001
        if need_db:
            raise
        print(f"[提示] 连不上数据库（{exc}），只用 config.yaml / 环境变量里的模型配置")
        db = None
    return config, db


def _build_client(config):
    """沿用标注引擎的模型配置和客户端（不改引擎）。"""
    from app.labeling import config_bridge

    config_bridge.ensure_on_path()
    from opinion_labeling_engine.config import load_config as load_engine_config
    from opinion_labeling_engine.llm.client import ArkClient
    from opinion_labeling_engine.preprocess.cleaner import ContentCleaner

    path = config_bridge.write_runtime_config(config)
    cfg = load_engine_config(str(path), require_db=False)
    return ArkClient(cfg.llm), ContentCleaner(cfg.cleaning), cfg.llm


# ---------------------------------------------------------------------------- 抽样
async def _sample(db, args) -> List[Dict[str, Any]]:
    """按「平台 × 现有情感」分层，每层平均分名额，差评这种少的也能抽到。"""
    where = ["content IS NOT NULL", "content <> ''"]
    params: List[Any] = []
    if args.scenic:
        where.append(f"scenic_id IN ({', '.join(['%s'] * len(args.scenic))})")
        params += args.scenic
    if args.channel:
        where.append(f"channel IN ({', '.join(['%s'] * len(args.channel))})")
        params += args.channel
    cond = " AND ".join(where)

    strata = await db.fetch_all(
        f"SELECT channel, IFNULL(sentiment_label, '') AS s, COUNT(*) AS n FROM `{COMMENTS}` "
        f"WHERE {cond} GROUP BY channel, IFNULL(sentiment_label, '')", params)
    strata = [s for s in strata if s["n"]]
    if not strata:
        return []

    # 平均分名额；某层不够就把剩下的名额匀给其它层
    quota = {(s["channel"], s["s"]): 0 for s in strata}
    avail = {(s["channel"], s["s"]): int(s["n"]) for s in strata}
    left = args.limit
    while left > 0:
        open_keys = [k for k in quota if quota[k] < avail[k]]
        if not open_keys:
            break
        share = max(1, left // len(open_keys))
        for k in open_keys:
            add = min(share, avail[k] - quota[k], left)
            quota[k] += add
            left -= add
            if left <= 0:
                break

    rows: List[Dict[str, Any]] = []
    rnd = random.Random(args.seed)
    for (channel, senti), want in quota.items():
        if want <= 0:
            continue
        # 多取几倍再在本地按长度筛：正文太短的（「好」「[赞]」）没有验证价值
        p = min(1.0, want * 5 / avail[(channel, senti)])
        seed = rnd.randint(1, 2 ** 31 - 1)
        ids = await db.fetch_all(
            f"SELECT id FROM `{COMMENTS}` WHERE {cond} AND channel = %s "
            f"AND IFNULL(sentiment_label, '') = %s AND RAND({seed}) < %s LIMIT %s",
            params + [channel, senti, p, want * 5])
        if not ids:
            continue
        id_list = [r["id"] for r in ids]
        got = await db.fetch_all(
            f"SELECT id, scenic_id, scenic_name, channel, content, sentiment_label, keyword_tags "
            f"FROM `{COMMENTS}` WHERE id IN ({', '.join(['%s'] * len(id_list))})", id_list)
        got = [g for g in got if len((g["content"] or "").strip()) >= args.min_len]
        rnd.shuffle(got)
        rows += got[:want]
    rnd.shuffle(rows)
    return rows


# ---------------------------------------------------------------------------- 单条处理
def _process(row: Dict[str, Any], client, cleaner, llm_cfg, system_prompt: str, args) -> Dict[str, Any]:
    from app.labeling import reason_events as re_

    out: Dict[str, Any] = {"row": row, "status": "", "error": "", "result": None,
                           "in_tokens": 0, "out_tokens": 0, "ms": 0}
    cleaned = cleaner.clean(row.get("content"))
    if not cleaned.valid:
        out["status"] = f"跳过（{cleaned.reason}）"   # 和标注一样：纯表情、太短的不调模型
        return out
    messages = re_.build_messages(cleaned.text, row.get("scenic_name") or "",
                                  system_prompt=system_prompt,
                                  system_role=getattr(llm_cfg, "system_role", "system"))
    try:
        resp = client.complete(messages)
    except Exception as exc:  # noqa: BLE001
        out["status"], out["error"] = "模型失败", str(exc)[:300]
        return out
    out["in_tokens"], out["out_tokens"], out["ms"] = resp.prompt_tokens, resp.completion_tokens, resp.latency_ms
    try:
        out["result"] = re_.extract(resp.text, cleaned.text, max_len=args.max_len, max_count=args.max_count)
    except Exception as exc:  # noqa: BLE001
        out["status"], out["error"] = "解析失败", f"{exc}｜原始输出：{resp.text[:300]}"
        return out
    out["status"] = "已提取" if out["result"].items else "无可提取"
    return out


def _csv_row(o: Dict[str, Any]) -> List[Any]:
    from app.labeling import reason_events as re_

    row, res = o["row"], o["result"]
    good = neutral = bad = detail = dropped = ""
    searchable = ""
    if res is not None:
        good = "；".join(res.by_sentiment(re_.GOOD))
        neutral = "；".join(res.by_sentiment(re_.NEUTRAL))
        bad = "；".join(res.by_sentiment(re_.BAD))
        detail = json.dumps([i.to_dict() for i in res.items], ensure_ascii=False) if res.items else ""
        dropped = "；".join(f"{w}({why})" for w, why in res.dropped)
        if res.items:
            ok = all(re_.searchable_in(i.reason, row.get("content") or "") for i in res.items)
            searchable = "是" if ok else "否"
    return [row.get("id", ""), row.get("scenic_id", ""), row.get("scenic_name", ""), row.get("channel", ""),
            row.get("sentiment_label") or "", row.get("keyword_tags") or "", row.get("content") or "",
            good, neutral, bad, detail, dropped, searchable, o["status"],
            o["in_tokens"], o["out_tokens"], o["ms"], o["error"]]


def _summary(results: List[Dict[str, Any]], args, elapsed: float) -> None:
    from app.labeling import reason_events as re_

    total = len(results)
    called = [o for o in results if o["in_tokens"] or o["status"] in ("模型失败",)]
    ok = [o for o in results if o["result"] is not None]
    has = [o for o in ok if o["result"].items]
    counts = {s: sum(len(o["result"].by_sentiment(s)) for o in ok) for s in re_.SENTIMENTS}
    dropped: Dict[str, int] = {}
    for o in ok:
        for _, why in o["result"].dropped:
            dropped[why] = dropped.get(why, 0) + 1
    tin = sum(o["in_tokens"] for o in results)
    tout = sum(o["out_tokens"] for o in results)
    n_tok = max(1, sum(1 for o in results if o["in_tokens"]))

    print("\n================ 汇总 ================")
    print(f"评论 {total} 条：调模型 {len(called)}，跳过 {total - len(called)}，"
          f"失败 {sum(1 for o in results if o['status'] in ('模型失败', '解析失败'))}")
    print(f"提出原因的 {len(has)} 条，没有可提取的 {len(ok) - len(has)} 条")
    print(f"原因共 {sum(counts.values())} 条：好评 {counts[re_.GOOD]}，中评 {counts[re_.NEUTRAL]}，差评 {counts[re_.BAD]}")
    if dropped:
        print("校验丢掉的：" + "，".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1])))
    print(f"token：输入 {tin}（平均 {tin // n_tok}/条），输出 {tout}（平均 {tout // n_tok}/条）")
    if args.price_in or args.price_out:
        cost = (tin * args.price_in + tout * args.price_out) / 1_000_000
        per = cost / n_tok
        print(f"费用：本次约 {cost:.4f} 元，平均 {per:.6f} 元/条，折合每万条约 {per * 10000:.2f} 元")
    print(f"耗时 {elapsed:.1f}s")


# ---------------------------------------------------------------------------- 入口
async def main() -> int:
    args = _parse_args()
    from app.labeling import reason_events as re_

    system_prompt = re_.build_system_prompt(max_len=args.max_len, max_count=args.max_count)
    if args.show_prompt:
        print(system_prompt)
        return 0

    config, db = await _load_config(need_db=not args.text)
    try:
        try:
            client, cleaner, llm_cfg = _build_client(config)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 模型配置不可用：{exc}\n请先在「系统设置 → AI 标注」里配好模型和 Key")
            return 2

        if args.text:
            rows = [{"id": f"text-{i}", "scenic_name": args.scenic_name, "content": t, "channel": ""}
                    for i, t in enumerate(args.text, start=1)]
        else:
            print(f"抽样中（目标 {args.limit} 条）…")
            rows = await _sample(db, args)
            if not rows:
                print("[错误] 没抽到评论，检查 --scenic / --channel 条件")
                return 2
            print(f"抽到 {len(rows)} 条，开始调模型（并发 {args.workers}）…")

        started = time.monotonic()
        results: List[Optional[Dict[str, Any]]] = [None] * len(rows)
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = [loop.run_in_executor(pool, _process, r, client, cleaner, llm_cfg, system_prompt, args)
                    for r in rows]
            done = 0
            for i, fut in enumerate(futs):
                results[i] = await fut
                done += 1
                if not args.text and (done % 20 == 0 or done == len(rows)):
                    print(f"  {done}/{len(rows)}")
        elapsed = time.monotonic() - started
        final = [r for r in results if r is not None]

        if args.text:
            for o in final:
                print(f"\n评论：{o['row']['content']}")
                print(f"  状态：{o['status']}  {o['error']}")
                if o["result"] is not None:
                    for it in o["result"].items:
                        print(f"  [{it.sentiment}] {it.reason}    （对象：{it.target}）")
                    for w, why in o["result"].dropped:
                        print(f"  ×丢弃 {w}（{why}）")

        out = Path(args.out) if args.out else (
            Path(config.get("server.data_dir") or ROOT / "data") / "reason_sample"
            / f"reason_sample-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADER)
            for o in final:
                w.writerow(_csv_row(o))

        _summary(final, args, elapsed)
        print(f"结果：{out}")
        return 0
    finally:
        if db is not None:
            await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
