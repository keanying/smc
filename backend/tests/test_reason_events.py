# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""原因关键事件（app/labeling/reason_events.py）：提示词、解析、校验。不连库、不调模型。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.labeling import reason_events as re_

ROOT = Path(__file__).resolve().parents[2]


def _out(*items):
    return json.dumps({"reasons": [dict(zip(("reason", "target", "sentiment"), i)) for i in items]},
                      ensure_ascii=False)


def test_user_examples_keep_object_and_stay_contiguous():
    text = "舜帝陵可看可不看，没有意义。池神庙建筑没有意义"
    res = re_.extract(_out(("舜帝陵可看可不看", "舜帝陵", "差评"),
                           ("没有意义", "舜帝陵", "差评"),
                           ("池神庙建筑没有意义", "池神庙建筑", "差评")), text)
    assert res.by_sentiment("差评") == ["舜帝陵可看可不看", "池神庙建筑没有意义"]
    assert ("没有意义", "被更长的包含") in res.dropped

    text2 = "螺满天下螺蛳粉好吃，老板娘是广西人，非常正宗实惠"
    res2 = re_.extract(_out(("螺满天下螺蛳粉好吃", "螺蛳粉", "好评"),
                            ("非常正宗实惠", "螺蛳粉", "好评")), text2)
    assert res2.by_sentiment("好评") == ["螺满天下螺蛳粉好吃", "非常正宗实惠"]
    assert [i.target for i in res2.items] == ["螺蛳粉", "螺蛳粉"]


def test_mixed_review_keeps_good_and_bad():
    text = "风景真的绝了，云海太震撼，但是索道排队排了快两小时，厕所也脏"
    res = re_.extract(_out(("云海太震撼", "云海", "正向"), ("厕所也脏", "厕所", "负向"),
                           ("景色一般", "景色", "中性")), text)
    assert res.by_sentiment("好评") == ["云海太震撼"]
    assert res.by_sentiment("差评") == ["厕所也脏"]
    # 「景色一般」原文里没有：编的，丢
    assert ("景色一般", "原文里不连续") in res.dropped


def test_rejects_non_contiguous_patch_and_cross_punct():
    text = "螺蛳粉好吃，老板娘是广西人，非常正宗实惠"
    res = re_.extract(_out(("螺蛳粉非常正宗实惠", "螺蛳粉", "好评"),       # 补了字，原文搜不到
                           ("螺蛳粉好吃，老板娘是广西人", "螺蛳粉", "好评"),  # 跨标点
                           ("好", "螺蛳粉", "好评"),                         # 单字
                           ("非常正宗实惠", "螺蛳粉", "不知道")), text)       # 好坏取值不对
    assert res.items == []
    reasons = dict(res.dropped)
    assert reasons["螺蛳粉非常正宗实惠"] == "原文里不连续"
    assert reasons["螺蛳粉好吃，老板娘是广西人"] == "跨了标点"
    assert reasons["好"] == "太短"
    assert reasons["非常正宗实惠"] == "好坏取值不对"


def test_whitespace_and_edge_punct_tolerated():
    text = "门票 100 有点小贵，不过讲解员讲得挺细的，值回票价。"
    res = re_.extract("```json\n" + _out(("门票100有点小贵", "门票", "差评"),
                                         ("“值回票价。”", "", "好评")) + "\n```", text)
    assert [i.reason for i in res.items] == ["门票100有点小贵", "值回票价"]
    assert res.items[1].target == "景区整体"          # 没写对象的补成景区整体
    assert re_.searchable_in("门票100有点小贵", text)


def test_empty_and_limit():
    assert re_.extract('{"reasons":[]}', "链接放评论区了").items == []
    text = "甲好看乙好看丙好看丁好看"
    res = re_.extract(_out(*[(f"{c}好看", c, "好评") for c in "甲乙丙丁"]), text, max_count=2)
    assert len(res.items) == 2
    assert sum(1 for _, why in res.dropped if why == "超过条数上限") == 2


def test_prompt_shapes():
    prompt = re_.build_system_prompt()
    # 验收用的评论不能出现在示例里，否则模型背答案
    for word in ("舜帝陵", "池神庙", "螺蛳粉", "老板娘", "可看可不看"):
        assert word not in prompt, word
    msgs = re_.build_messages("好玩", "黄山", system_prompt=prompt)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "景区名称：黄山" in msgs[1]["content"][0]["text"]
    one = re_.build_messages("好玩", system_prompt=prompt, system_role="user")
    assert len(one) == 1 and one[0]["role"] == "user"


def test_script_show_prompt_runs():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "reason_sample.py"), "--show-prompt"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "好评 / 中评 / 差评" in r.stdout
