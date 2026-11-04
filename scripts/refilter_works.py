# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""按景区现在的关键字重新过滤已采的作品，不相关的连评论一起删掉。

    python scripts/refilter_works.py --scenic PFTSCA01016184            # 预览：只统计、出清单，不删
    python scripts/refilter_works.py --scenic PFTSCA01016184 --apply    # 确认没问题后真删
    python scripts/refilter_works.py --all                              # 所有启用的景区（同样先预览）

判定和采集时一样：作品的标题 + 描述 + 标签里
    含任一「启用的」主关键字或附关键字 → 留；都不含 → 删
    留下的再看过滤关键字，命中任一 → 删
只处理抖音 / 快手 / 小红书 / 微博；携程 / 同程 / 去哪儿的点评不动；按作者主页、作品链接采的默认不动。

每次都会把要删的作品写成一份 CSV 清单（data/refilter/ 下），预览时先翻一翻，有误伤就去调关键字再跑。
直接连 config.yaml 里的数据库（景区盒子的数据在主上，在主上跑）。删之前建议先备份数据库。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


async def main() -> int:
    parser = argparse.ArgumentParser(description="按景区现在的关键字重新过滤已采的作品，删掉不相关的（连评论）")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--scenic", action="append", metavar="景区ID", help="景区ID，可以写多次")
    target.add_argument("--all", action="store_true", help="所有启用的景区")
    parser.add_argument("--apply", action="store_true", help="真的删除（不加只预览）")
    parser.add_argument("--yes", action="store_true", help="加了 --apply 时不再二次确认")
    parser.add_argument("--include-homepage", action="store_true",
                        help="按作者主页 / 作品链接采的作品也过一遍（默认不动）")
    parser.add_argument("--remember", action="store_true",
                        help="删掉的记进「已删除作品」，以后采集搜到直接跳过（默认不记）")
    args = parser.parse_args()

    from app.core.config import load_config
    from app.core.db import Database
    from app.utils.refilter import refilter_scenic

    config = load_config()
    db = Database(config)
    await db.connect()
    try:
        if args.all:
            rows = await db.fetch_all(
                "SELECT scenic_id, scenic_name FROM `src_opinion_social_scenic` WHERE enabled = 1 ORDER BY id")
        else:
            marks = ", ".join(["%s"] * len(args.scenic))
            rows = await db.fetch_all(
                f"SELECT scenic_id, scenic_name FROM `src_opinion_social_scenic` WHERE scenic_id IN ({marks})",
                args.scenic)
            missing = set(args.scenic) - {r["scenic_id"] for r in rows}
            if missing:
                print(f"[错误] 没有这些景区：{'、'.join(sorted(missing))}")
                return 2

        out_dir = Path(config.get("server.data_dir")) / "refilter"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        # 先全部预览一遍，汇总给人看；确认了再删
        previews = []
        for scenic in rows:
            report = await refilter_scenic(db, scenic["scenic_id"], include_homepage=args.include_homepage)
            previews.append((scenic, report))
            _print_report(scenic, report)
            if report["delete"]:
                path = out_dir / f"{scenic['scenic_id']}-{stamp}.csv"
                with path.open("w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["作品主键", "平台", "作品ID", "标题", "删除原因", "评论数"])
                    for d in report["delete"]:
                        writer.writerow([d["id"], d["channel"], d["work_id"], (d["title"] or "")[:200],
                                         d["reason"], d["comments"]])
                print(f"    要删的清单：{path}")

        total_works = sum(len(r["delete"]) for _, r in previews)
        total_comments = sum(d["comments"] for _, r in previews for d in r["delete"])
        print(f"\n合计：要删 {total_works} 条作品、{total_comments} 条评论")
        if not args.apply:
            print("这是预览，什么都没删。翻一下上面的 CSV 清单，没问题后加 --apply 再跑一次。")
            return 0
        if total_works == 0:
            print("没有要删的。")
            return 0
        if not args.yes:
            answer = input(f"确认删除这 {total_works} 条作品和它们的 {total_comments} 条评论？删了恢复不了。输入 yes 继续：")
            if answer.strip().lower() != "yes":
                print("已取消，什么都没删。")
                return 1

        for scenic, _ in previews:
            report = await refilter_scenic(db, scenic["scenic_id"], apply=True,
                                           include_homepage=args.include_homepage, remember=args.remember)
            if report["deleted_works"]:
                print(f"  {scenic['scenic_name']}：删了 {report['deleted_works']} 条作品、"
                      f"{report['deleted_comments']} 条评论")
        print("完成。数据中心的概览数字有半分钟缓存，过一会儿刷新就对了。")
        return 0
    finally:
        await db.close()


def _print_report(scenic, report) -> None:
    print(f"\n== {scenic['scenic_name']}（{scenic['scenic_id']}）")
    if report.get("note"):
        print(f"    {report['note']}")
        return
    keep = report["keep_words"]
    print(f"    保留词（主+附）{len(keep)} 个：{'、'.join(keep[:12])}{' …' if len(keep) > 12 else ''}")
    print(f"    过滤词 {len(report['exclude_words'])} 个")
    reasons = {}
    for d in report["delete"]:
        key = "不含关键字" if d["reason"] == "不含关键字" else d["reason"]
        reasons[key] = reasons.get(key, 0) + 1
    print(f"    检查 {report['scanned']} 条作品：保留 {report['kept']}，要删 {len(report['delete'])}"
          f"（评论 {sum(d['comments'] for d in report['delete'])} 条）"
          + (f"，按主页/链接采的没动 {report['skipped_homepage']}" if report["skipped_homepage"] else ""))
    for reason, n in sorted(reasons.items(), key=lambda x: -x[1])[:10]:
        print(f"      {reason}：{n}")
    for d in report["delete"][:5]:
        print(f"      例：[{d['channel']}] {(d['title'] or '').strip()[:40]} —— {d['reason']}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
