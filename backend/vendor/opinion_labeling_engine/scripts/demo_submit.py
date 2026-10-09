#!/usr/bin/env python3
"""联调用示例：模拟采集侧「一边采集一边推」的调用方式。

用法：
    # 干跑（不连库，结果只打日志）——先用它验证提示词与标注质量
    python scripts/demo_submit.py --dry-run

    # 连库跑（会真的 UPDATE src_opinion_social_work_comment_di）
    python scripts/demo_submit.py

这就是采集侧需要写的全部代码：构造 engine → start → 边采边 submit → stop。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opinion_labeling_engine import LabelingEngine   # noqa: E402


# 一批样例数据：入参就是约定的六个字段
# （最后一条额外带了 extra_content，演示"可选字段传了会提高准确率"）
# 覆盖「正向 / 混合 / 先抑后扬 / 纯表情 / 广告无关」五种典型
SAMPLE_ROWS = [
    {
        "channel": "ctrip", "work_id": "76471",
        "scenic_id": "PFTSCA01009835", "scenic_name": "天山天池",
        "comment_id": "805353431",
        "content": "完美的一天",
        # 可选：携程的结构化补充信息，对这种短评帮助很大
        "extra_content": json.dumps({"score": 5.0, "landscape_score": 5.0,
                                     "tourist_type": "朋友出游"}, ensure_ascii=False),
    },
    {
        "channel": "xhs", "work_id": "w-1001",
        "scenic_id": "PFTSCA01009835", "scenic_name": "天山天池",
        "comment_id": "demo-002",
        "content": "风景是真的美，云海绝了，但是索道排队排了快两小时，厕所也脏得下不去脚。",
    },
    {
        "channel": "douyin", "work_id": "w-1002",
        "scenic_id": "PFTSCA01009835", "scenic_name": "天山天池",
        "comment_id": "demo-003",
        "content": "门票有点小贵，不过讲解员讲得挺细的，值回票价。带娃去的，推车能进。",
    },
    {
        "channel": "weibo", "work_id": "w-1003",
        "scenic_id": "PFTSCA01009835", "scenic_name": "天山天池",
        "comment_id": "demo-004",
        "content": "😀😀😀[比心][比心]",          # 纯表情 → 不调模型，直接中性
    },
    {
        "channel": "kuaishou", "work_id": "w-1004",
        "scenic_id": "PFTSCA01009835", "scenic_name": "天山天池",
        "comment_id": "demo-005",
        "content": "家人们谁懂啊，我这条裙子才89，链接放评论区了，速抢！",   # 与景区无关 → 中性
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="不连库，结果只打日志")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--sync", action="store_true", help="不走队列，逐条同步标注")
    args = parser.parse_args()

    engine = LabelingEngine.from_config(args.config, enable_db=not args.dry_run)

    if args.sync:
        # 同步模式：适合先看效果、调提示词
        for row in SAMPLE_ROWS:
            result = engine.label_sync(row, write=not args.dry_run)
            print(result.summary())
            if result.dropped:
                print(f"    丢弃：{json.dumps(result.dropped, ensure_ascii=False)}")
        return 0

    # 异步模式：采集侧的真实用法
    with engine:                       # __enter__ 即 start()
        for row in SAMPLE_ROWS:
            engine.submit(row)         # 立即返回，不阻塞采集
            time.sleep(0.05)           # 模拟采集节奏
        engine.wait_idle(timeout=120)
        print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
