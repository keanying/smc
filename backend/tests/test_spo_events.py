# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主谓宾方案（app/labeling/spo_events.py）：提示词、解析、校验。不连库、不调模型。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.labeling import spo_events as spo

ROOT = Path(__file__).resolve().parents[2]


def _out(triples, keywords=()):
    keys = ("subject", "predicate", "object", "adjunct", "sentiment")
    return json.dumps({"triples": [dict(zip(keys, t)) for t in triples], "keywords": list(keywords)},
                      ensure_ascii=False)


def test_user_examples():
    text = "舜帝陵可看可不看，没有意义。池神庙建筑没有意义"
    res = spo.extract(_out([("舜帝陵", "可看可不看", "", "", "中评"),
                            ("舜帝陵", "没有", "意义", "", "差评"),
                            ("池神庙建筑", "没有", "意义", "", "差评")],
                           ["舜帝陵", "池神庙", "没有意义"]), text)
    assert [t.display() for t in res.by_sentiment("差评")] == ["舜帝陵·没有·意义", "池神庙建筑·没有·意义"]
    assert res.keywords == ["舜帝陵", "池神庙", "没有意义"]
    # 「舜帝陵没有意义」拼回去原文里没有（中间隔着「可看可不看，」）
    hits = [spo.joined_searchable(t, text) for t in res.triples]
    assert hits == [True, False, True]

    text2 = "螺满天下螺蛳粉好吃，老板娘是广西人，非常正宗实惠"
    res2 = spo.extract(_out([("螺蛳粉", "好吃", "", "", "好评"),
                             ("螺蛳粉", "正宗实惠", "", "非常", "好评")]), text2)
    assert [t.display() for t in res2.triples] == ["螺蛳粉·好吃", "螺蛳粉·正宗实惠（非常）"]


def test_rejects_rewritten_slots_and_keeps_whole_subject():
    text = "排了很久的队，太值了"
    res = spo.extract(_out([("游客", "排队", "", "很久", "差评"),     # 主语「游客」原文没有
                            ("", "值", "", "太", "好评"),              # 主语空 → 景区整体
                            ("景区整体", "", "", "", "好评"),          # 没谓语
                            ("队", "排了", "", "很久", "未知")],       # 好坏取值不对
                           ["很久的队，太值", "值"]), text)
    assert [t.display() for t in res.triples] == ["景区整体·值（太）"]
    why = dict(res.dropped)
    assert why["游客·排队（很久）"] == "主语原文里没有"
    assert why["景区整体"] == "没有谓语"
    assert why["队·排了（很久）"] == "好坏取值不对"
    assert why["很久的队，太值"] == "关键字太长" or why["很久的队，太值"] == "关键字跨了标点"
    assert why["值"] == "关键字太短"


def test_prompt_has_no_acceptance_examples():
    prompt = spo.build_system_prompt()
    for word in ("舜帝陵", "池神庙", "螺蛳粉", "老板娘", "可看可不看"):
        assert word not in prompt, word
    assert '"triples"' in prompt


def test_script_show_prompt_runs():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "spo_sample.py"), "--show-prompt"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "主谓宾" in r.stdout
