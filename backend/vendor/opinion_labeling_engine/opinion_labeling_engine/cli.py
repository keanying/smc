"""命令行入口：``python -m opinion_labeling_engine <command>``。

子命令：
    check     启动期自检（配置 / 标签体系 / Redis / MySQL / 表结构），不处理数据
    serve     常驻模式，拉起消费线程池等待采集侧推数据
    label     标注单条或一个 CSV 文件（联调、抽样验收、补跑用）
    backfill  从源表捞未标注的行补跑
    retry     重标失败池里的评论（标注失败的都在那儿）
    failures  查看失败池，不改动任何数据
    prompt    打印最终提示词，人工审阅用
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, load_config
from .domain.models import CommentRecord
from .domain.taxonomy import Taxonomy
from .logging_conf import get_logger, setup_logging
from .worker.engine import LabelingEngine

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opinion_labeling_engine",
        description="景区舆情标注引擎",
    )
    parser.add_argument("-c", "--config", help="配置文件路径，默认 config/config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="启动期自检")

    p_serve = sub.add_parser("serve", help="常驻消费模式")
    p_serve.add_argument("--dry-run", action="store_true", help="不写库，结果只打日志")

    p_label = sub.add_parser("label", help="标注单条或 CSV 文件")
    src = p_label.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="直接给一段评论正文")
    src.add_argument("--csv", help="CSV 文件路径，表头需含 comment_id/content")
    p_label.add_argument("--scenic-name", default="", help="景区名称（--text 时建议给）")
    p_label.add_argument("--scenic-id", default="", help="景区ID")
    p_label.add_argument("--channel", default="", help="渠道")
    p_label.add_argument("--comment-id", default="manual-1", help="评论ID（--text 时用）")
    p_label.add_argument("--write", action="store_true", help="标注后写回数据库")
    p_label.add_argument("--out", help="结果输出 CSV 路径")
    p_label.add_argument("--limit", type=int, default=0, help="只处理前 N 行")

    p_bf = sub.add_parser("backfill", help="从源表捞未标注的行补跑")
    p_bf.add_argument("--limit", type=int, default=500, help="本次最多补跑多少条")
    p_bf.add_argument("--where", default="", help="附加 WHERE 条件，如 \"scenic_id='PFTSCA01009835'\"")
    p_bf.add_argument("--dry-run", action="store_true", help="不写库")

    p_retry = sub.add_parser("retry", help="重标失败池里的评论")
    p_retry.add_argument("--limit", type=int, default=0, help="最多重标多少条，0=全部")
    p_retry.add_argument("--min-age", type=float, default=None,
                         help="只重标失败超过这么多秒的，默认取配置值")
    p_retry.add_argument("--batch-size", type=int, default=None, help="每批取多少条")

    p_fail = sub.add_parser("failures", help="查看失败池")
    p_fail.add_argument("--limit", type=int, default=20, help="显示多少条")

    p_prompt = sub.add_parser("prompt", help="打印提示词")
    p_prompt.add_argument("--text", default="风景不错，就是排队太久了。", help="示例评论")

    return parser


# ---------------------------------------------------------------------------
def cmd_check(args) -> int:
    """逐项自检并打印结果。任何一项失败返回非零退出码，可直接接 CI。"""
    ok = True
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[FAIL] 配置加载：{exc}")
        return 1
    setup_logging(cfg.logging)
    print(f"[ OK ] 配置加载：{cfg.config_path}")

    try:
        tax = Taxonomy.load(cfg.taxonomy_path)
        print(f"[ OK ] 标签体系：v{tax.version}，维度路径 {len(tax.nodes)} 条，"
              f"实体类型 {len(tax.entity_types)} 个")
    except ConfigError as exc:
        print(f"[FAIL] 标签体系：{exc}")
        return 1

    print(f"[INFO] 模型：{cfg.llm.model} @ {cfg.llm.url}")

    if cfg.queue.backend == "redis":
        try:
            from .queues.redis_queue import RedisQueue

            queue = RedisQueue(cfg.queue)
            if queue.ping():
                print(f"[ OK ] Redis：{cfg.queue.redis.host}:{cfg.queue.redis.port} "
                      f"（积压 {queue.size()}，在途 {queue.inflight()}，死信 {queue.dead_size()}）")
            else:
                ok = False
                print("[FAIL] Redis：连接失败")
        except Exception as exc:                     # noqa: BLE001
            ok = False
            print(f"[FAIL] Redis：{exc}")
    else:
        print("[INFO] 队列：memory（进程内，重启丢任务；生产请切 redis）")

    try:
        from .storage.failure_store import create_failure_store

        store = create_failure_store(cfg.failure_store)
        print(f"[ OK ] 失败池：{cfg.failure_store.backend}（{store.count()} 条待重标）")
        store.close()
    except Exception as exc:                         # noqa: BLE001
        ok = False
        print(f"[FAIL] 失败池：{exc}")

    try:
        from .storage.mysql import MySQLPool
        from .storage.repository import LabelRepository

        pool = MySQLPool(cfg.mysql)
        if not pool.ping():
            ok = False
            print("[FAIL] MySQL：连接失败")
        else:
            print(f"[ OK ] MySQL：{cfg.mysql.host}:{cfg.mysql.port}/{cfg.mysql.database}")
            repo = LabelRepository(pool, cfg.storage)
            repo.ensure_schema()
            print(f"[ OK ] 表结构：{cfg.storage.table}")
        pool.close()
    except Exception as exc:                         # noqa: BLE001
        ok = False
        print(f"[FAIL] MySQL/表结构：{exc}")

    return 0 if ok else 1


def cmd_serve(args) -> int:
    engine = LabelingEngine.from_config(args.config, enable_db=not args.dry_run)
    engine.start(check_schema=not args.dry_run)
    print("引擎已启动，等待采集侧推送数据；Ctrl-C 停机。")
    engine.run_forever()
    return 0


def cmd_label(args) -> int:
    engine = LabelingEngine.from_config(args.config, enable_db=args.write)
    records = _load_records(args)
    if not records:
        print("没有可标注的数据")
        return 1

    rows = []
    for record in records:
        result = engine.label_sync(record, write=args.write)
        print(result.summary())
        if result.dropped:
            print(f"    丢弃：{json.dumps(result.dropped, ensure_ascii=False)}")
        rows.append({
            "comment_id": record.comment_id,
            "content": record.content,
            "sentiment_label": result.sentiment_label,
            "sentiment_score": result.sentiment_score,
            "dimension_tags": result.dimension_json(),
            "entity_tags": result.entity_json(),
            "keyword_tags": result.keyword_json(),
            "confidence": result.confidence,
            "source": result.source.value,
            "reason": result.reason,
        })

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n结果已写入 {out_path}")

    print(f"\n统计：{json.dumps(engine.service.stats.snapshot(), ensure_ascii=False)}")
    engine.service.close()
    return 0


def cmd_backfill(args) -> int:
    engine = LabelingEngine.from_config(args.config, enable_db=True)
    if engine.repository is None:                    # pragma: no cover
        print("未启用数据库，无法补跑")
        return 1

    rows = engine.repository.fetch_unlabeled(limit=args.limit, extra_where=args.where)
    if not rows:
        print("没有未标注的数据")
        return 0
    print(f"捞到 {len(rows)} 条未标注数据，开始补跑……")

    engine.start(check_schema=True)
    accepted = engine.submit_many(rows)
    print(f"已入队 {accepted} 条，等待处理完成……")
    engine.wait_idle(timeout=max(60.0, accepted * 5.0))
    print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
    engine.stop()
    return 0


def cmd_retry(args) -> int:
    engine = LabelingEngine.from_config(args.config, enable_db=True)
    pending = engine.failure_store.count()
    if not pending:
        print("失败池是空的，没有需要重标的")
        return 0
    print(f"失败池里有 {pending} 条，开始重标……")

    engine.start(check_schema=True)
    try:
        report = engine.retry_failed(limit=args.limit, min_age_seconds=args.min_age,
                                     batch_size=args.batch_size)
    finally:
        engine.stop()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_failures(args) -> int:
    cfg = load_config(args.config, require_db=False)
    setup_logging(cfg.logging)
    from .storage.failure_store import create_failure_store

    store = create_failure_store(cfg.failure_store)
    total = store.count()
    print(f"失败池共 {total} 条（backend={cfg.failure_store.backend}）")
    if not total:
        return 0

    print(f"\n最早失败的 {min(args.limit, total)} 条：")
    for item in store.peek(args.limit):
        age_h = item.age_seconds() / 3600
        content = (item.record.get("content") or "")[:30]
        print(f"  [{item.comment_id}] 失败 {item.attempts} 次 / {age_h:.1f} 小时前 "
              f"| {content} | {item.error[:80]}")
    print("\n跑 `python -m opinion_labeling_engine retry` 重标它们")
    store.close()
    return 0


def cmd_prompt(args) -> int:
    from .llm.prompt import PromptBuilder
    from .preprocess.cleaner import ContentCleaner

    cfg = load_config(args.config, require_db=False)
    tax = Taxonomy.load(cfg.taxonomy_path)
    builder = PromptBuilder(
        tax,
        keyword_min=cfg.labeling.keyword_min_length,
        keyword_max=cfg.labeling.keyword_max_length,
        keyword_max_count=cfg.labeling.keyword_max_count,
        dimension_max=cfg.labeling.dimension_max_count,
        with_fewshot=cfg.llm.with_fewshot,
        fewshot_style=cfg.llm.fewshot_style,
        system_role=cfg.llm.system_role,
    )
    cleaner = ContentCleaner(cfg.cleaning)
    record = CommentRecord(comment_id="demo", content=args.text, scenic_name="示例景区")
    cleaned = cleaner.clean(args.text)

    print("=" * 30 + " SYSTEM " + "=" * 30)
    print(builder.system_prompt)
    print("=" * 30 + " USER " + "=" * 32)
    print(builder.build_user_content(record, cleaned))
    print("=" * 68)
    print(f"system 段字符数：{len(builder.system_prompt)}")
    return 0


# ---------------------------------------------------------------------------
def _load_records(args) -> List[CommentRecord]:
    if args.text:
        return [CommentRecord(
            comment_id=args.comment_id,
            content=args.text,
            scenic_id=args.scenic_id,
            scenic_name=args.scenic_name,
            channel=args.channel,
        )]

    path = Path(args.csv)
    if not path.exists():
        print(f"CSV 不存在：{path}")
        return []
    records: List[CommentRecord] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for idx, row in enumerate(csv.DictReader(fh), start=1):
            if args.limit and idx > args.limit:
                break
            try:
                records.append(CommentRecord.from_dict(row))
            except ValueError as exc:
                print(f"第 {idx} 行跳过：{exc}")
    return records


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "check": cmd_check,
        "serve": cmd_serve,
        "label": cmd_label,
        "backfill": cmd_backfill,
        "retry": cmd_retry,
        "failures": cmd_failures,
        "prompt": cmd_prompt,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
