# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主谓宾方案 · 小样本验证（只读，不改库）。

把评论里每一处评价拆成「主语 / 谓语 / 宾语 / 补充 + 好评中评差评」，另提一组关键字，
连同现有的情感和关键词写成 CSV。抽样、模型配置、参数和 reason_sample.py 完全一样。

    python scripts/spo_sample.py --text "舜帝陵可看可不看，没有意义。池神庙建筑没有意义"
    python scripts/spo_sample.py --limit 300 --seed 1
    python scripts/spo_sample.py --limit 300 --seed 1 --compare     # 同一批评论两种方案都跑，放在一个 CSV 里逐行对比
    python scripts/spo_sample.py --show-prompt

- 数据库只读，现有标注不受影响；模型、Key、QPS、extra_body 沿用「AI 标注」的配置。
- 结果在 data/reason_sample/ 下（文件名 spo_sample-… / compare-…）。
- --compare 时每条评论调两次模型，费用也是两份，汇总里分开算。
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reason_sample as rs  # noqa: E402  抽样、配置、模型客户端都复用它的，两边口径一致

SPO_HEADER = [
    "评论ID", "景区ID", "景区", "平台", "现有情感", "现有关键词", "评论原文",
    "主谓宾·好评", "主谓宾·中评", "主谓宾·差评", "关键字", "主谓宾明细",
    "主谓宾·未通过校验", "主谓宾拼回原文搜得到", "主谓宾·状态",
    "主谓宾·输入tokens", "主谓宾·输出tokens", "主谓宾·耗时ms", "主谓宾·错误",
]
REASON_HEADER = ["原因·好评", "原因·中评", "原因·差评", "原因明细(含对象)", "原因·未通过校验",
                 "原因·状态", "原因·输入tokens", "原因·输出tokens", "原因·错误"]


def _process_spo(row: Dict[str, Any], client, cleaner, llm_cfg, system_prompt: str) -> Dict[str, Any]:
    from app.labeling import spo_events as spo

    out: Dict[str, Any] = {"row": row, "status": "", "error": "", "result": None,
                           "in_tokens": 0, "out_tokens": 0, "ms": 0}
    cleaned = cleaner.clean(row.get("content"))
    if not cleaned.valid:
        out["status"] = f"跳过（{cleaned.reason}）"
        return out
    messages = spo.build_messages(cleaned.text, row.get("scenic_name") or "",
                                  system_prompt=system_prompt,
                                  system_role=getattr(llm_cfg, "system_role", "system"))
    try:
        resp = client.complete(messages)
    except Exception as exc:  # noqa: BLE001
        out["status"], out["error"] = "模型失败", str(exc)[:300]
        return out
    out["in_tokens"], out["out_tokens"], out["ms"] = resp.prompt_tokens, resp.completion_tokens, resp.latency_ms
    try:
        out["result"] = spo.extract(resp.text, cleaned.text)
    except Exception as exc:  # noqa: BLE001
        out["status"], out["error"] = "解析失败", f"{exc}｜原始输出：{resp.text[:300]}"
        return out
    out["status"] = "已提取" if out["result"].triples else "无可提取"
    return out


def _spo_cols(o: Dict[str, Any]) -> List[Any]:
    from app.labeling import spo_events as spo

    row, res = o["row"], o["result"]
    good = neutral = bad = kws = detail = dropped = searchable = ""
    if res is not None:
        good = "；".join(t.display() for t in res.by_sentiment(spo.GOOD))
        neutral = "；".join(t.display() for t in res.by_sentiment(spo.NEUTRAL))
        bad = "；".join(t.display() for t in res.by_sentiment(spo.BAD))
        kws = "；".join(res.keywords)
        detail = json.dumps([t.to_dict() for t in res.triples], ensure_ascii=False) if res.triples else ""
        dropped = "；".join(f"{w}({why})" for w, why in res.dropped)
        if res.triples:
            hit = sum(1 for t in res.triples if spo.joined_searchable(t, row.get("content") or ""))
            searchable = f"{hit}/{len(res.triples)}"
    return [row.get("id", ""), row.get("scenic_id", ""), row.get("scenic_name", ""), row.get("channel", ""),
            row.get("sentiment_label") or "", row.get("keyword_tags") or "", row.get("content") or "",
            good, neutral, bad, kws, detail, dropped, searchable, o["status"],
            o["in_tokens"], o["out_tokens"], o["ms"], o["error"]]


def _reason_cols(o: Dict[str, Any]) -> List[Any]:
    full = rs._csv_row(o)
    # rs.CSV_HEADER：7 好评 8 中评 9 差评 10 明细 11 未通过 13 状态 14 输入 15 输出 17 错误
    return [full[7], full[8], full[9], full[10], full[11], full[13], full[14], full[15], full[17]]


def _summary_spo(results: List[Dict[str, Any]], args, elapsed: float) -> None:
    from app.labeling import spo_events as spo

    total = len(results)
    ok = [o for o in results if o["result"] is not None]
    has = [o for o in ok if o["result"].triples]
    counts = {s: sum(len(o["result"].by_sentiment(s)) for o in ok) for s in spo.SENTIMENTS}
    n_triples = sum(counts.values())
    with_adjunct = sum(1 for o in ok for t in o["result"].triples if t.adjunct)
    filled = sum(1 for o in ok for t in o["result"].triples if t.subject == spo.WHOLE)
    searchable = sum(1 for o in ok for t in o["result"].triples
                     if spo.joined_searchable(t, o["row"].get("content") or ""))
    n_kw = sum(len(o["result"].keywords) for o in ok)
    dropped: Dict[str, int] = {}
    for o in ok:
        for _, why in o["result"].dropped:
            dropped[why] = dropped.get(why, 0) + 1
    tin = sum(o["in_tokens"] for o in results)
    tout = sum(o["out_tokens"] for o in results)
    n_tok = max(1, sum(1 for o in results if o["in_tokens"]))
    failed = sum(1 for o in results if o["status"] in ("模型失败", "解析失败"))

    print("\n================ 主谓宾 · 汇总 ================")
    print(f"评论 {total} 条：调模型 {n_tok if tin else 0}，失败 {failed}")
    print(f"拆出主谓宾的 {len(has)} 条，没有可拆的 {len(ok) - len(has)} 条")
    print(f"三元组共 {n_triples} 个：好评 {counts[spo.GOOD]}，中评 {counts[spo.NEUTRAL]}，差评 {counts[spo.BAD]}")
    if n_triples:
        print(f"  带补充（程度/时长/原因…）的 {with_adjunct} 个；主语是「景区整体」的 {filled} 个；"
              f"主谓宾拼回去能在原文搜到的 {searchable} 个（{searchable * 100 // n_triples}%）")
    print(f"关键字共 {n_kw} 个（平均 {n_kw / max(1, len(ok)):.1f} 个/条）")
    if dropped:
        print("校验丢掉的：" + "，".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1])))
    print(f"token：输入 {tin}（平均 {tin // n_tok}/条），输出 {tout}（平均 {tout // n_tok}/条）")
    if args.price_in or args.price_out:
        cost = (tin * args.price_in + tout * args.price_out) / 1_000_000
        per = cost / n_tok
        print(f"费用：本次约 {cost:.4f} 元，平均 {per:.6f} 元/条，折合每万条约 {per * 10000:.2f} 元")
    print(f"耗时 {elapsed:.1f}s")


async def main() -> int:
    parser = rs.build_parser("主谓宾方案 · 小样本验证（只读，不改库）")
    parser.add_argument("--compare", action="store_true",
                        help="同一批评论把「原因关键事件」方案也跑一遍，放在同一个 CSV 里对比（费用两份）")
    args = parser.parse_args()

    from app.labeling import reason_events, spo_events as spo

    spo_prompt = spo.build_system_prompt()
    reason_prompt = reason_events.build_system_prompt(max_len=args.max_len, max_count=args.max_count)
    if args.show_prompt:
        print(spo_prompt)
        return 0

    config, db = await rs._load_config(need_db=not args.text)
    try:
        try:
            client, cleaner, llm_cfg = rs._build_client(config)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 模型配置不可用：{exc}\n请先在「系统设置 → AI 标注」里配好模型和 Key")
            return 2

        if args.text:
            rows = [{"id": f"text-{i}", "scenic_name": args.scenic_name, "content": t, "channel": ""}
                    for i, t in enumerate(args.text, start=1)]
        else:
            print(f"抽样中（目标 {args.limit} 条）…")
            rows = await rs._sample(db, args)
            if not rows:
                print("[错误] 没抽到评论，检查 --scenic / --channel 条件")
                return 2
            print(f"抽到 {len(rows)} 条，开始调模型（并发 {args.workers}"
                  f"{'，两种方案各一次' if args.compare else ''}）…")

        started = time.monotonic()
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            spo_futs = [loop.run_in_executor(pool, _process_spo, r, client, cleaner, llm_cfg, spo_prompt)
                        for r in rows]
            reason_futs = ([loop.run_in_executor(pool, rs._process, r, client, cleaner, llm_cfg,
                                                 reason_prompt, args) for r in rows]
                           if args.compare else [])
            spo_results: List[Dict[str, Any]] = []
            for i, fut in enumerate(spo_futs, start=1):
                spo_results.append(await fut)
                if not args.text and (i % 20 == 0 or i == len(rows)):
                    print(f"  主谓宾 {i}/{len(rows)}")
            reason_results: List[Dict[str, Any]] = []
            for i, fut in enumerate(reason_futs, start=1):
                reason_results.append(await fut)
                if not args.text and (i % 20 == 0 or i == len(rows)):
                    print(f"  原因 {i}/{len(rows)}")
        elapsed = time.monotonic() - started

        if args.text:
            for idx, o in enumerate(spo_results):
                print(f"\n评论：{o['row']['content']}")
                print(f"  【主谓宾】{o['status']}  {o['error']}")
                if o["result"] is not None:
                    for t in o["result"].triples:
                        print(f"    [{t.sentiment}] 主={t.subject}  谓={t.predicate}  宾={t.object or '-'}"
                              f"  补={t.adjunct or '-'}")
                    print(f"    关键字：{'、'.join(o['result'].keywords) or '-'}")
                    for w, why in o["result"].dropped:
                        print(f"    ×丢弃 {w}（{why}）")
                if args.compare:
                    r = reason_results[idx]
                    print(f"  【原因】{r['status']}  {r['error']}")
                    if r["result"] is not None:
                        for it in r["result"].items:
                            print(f"    [{it.sentiment}] {it.reason}    （对象：{it.target}）")

        prefix = "compare" if args.compare else "spo_sample"
        out = Path(args.out) if args.out else (
            Path(config.get("server.data_dir") or rs.ROOT / "data") / "reason_sample"
            / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(SPO_HEADER + (REASON_HEADER if args.compare else []))
            for i, o in enumerate(spo_results):
                w.writerow(_spo_cols(o) + (_reason_cols(reason_results[i]) if args.compare else []))

        _summary_spo(spo_results, args, elapsed)
        if args.compare:
            print("\n（下面是同一批评论的「原因关键事件」方案）")
            rs._summary(reason_results, args, elapsed)
        print(f"结果：{out}")
        return 0
    finally:
        if db is not None:
            await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
