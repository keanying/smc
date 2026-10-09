"""label_review_flag 的七档语义。

这一列**引擎和采集侧共用**：
  0未标注 1人工复核正确 2人工复核错误 3复核成功 4AI标注成功 5AI标注错误 6未人工复核
1/2/3 由审核页面写，4/5/6 由标注引擎写。写串了就分不清一条数据
是 AI 标的还是人改的。
"""
from __future__ import annotations

import inspect

import pytest

from app.repositories.labeling_repo import (
    FLAG_AI_FAILED, FLAG_AI_LOW_CONF, FLAG_AI_OK, FLAG_HUMAN_FIXED,
    FLAG_HUMAN_RIGHT, FLAG_HUMAN_WRONG, FLAG_LABELS, FLAG_UNLABELED,
    HUMAN_FLAGS, LabelingRepository,
)


def test_七档取值和用户给的定义一一对应():
    assert FLAG_UNLABELED == 0
    assert FLAG_HUMAN_RIGHT == 1
    assert FLAG_HUMAN_WRONG == 2
    assert FLAG_HUMAN_FIXED == 3
    assert FLAG_AI_OK == 4
    assert FLAG_AI_FAILED == 5
    assert FLAG_AI_LOW_CONF == 6
    assert FLAG_LABELS == {
        0: "未标注", 1: "人工复核正确", 2: "人工复核错误", 3: "复核成功",
        4: "AI标注成功", 5: "AI标注错误", 6: "未人工复核",
    }


def test_人工只能写_123():
    assert set(HUMAN_FLAGS) == {1, 2, 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, 4, 5, 6, 9])
async def test_人工写引擎的档位要被拒绝(bad):
    """4/5/6 是引擎的状态。人工覆盖了，"这条是谁标的"就永远说不清了。"""
    repo = LabelingRepository.__new__(LabelingRepository)
    with pytest.raises(ValueError, match="人工复核只能写"):
        await repo.save_review(1, flag=bad)


def test_撤销复核要回到_0_而不是_4():
    """引擎跑批只取 pending_flags（默认 [0]）。

    回到 4 的话这条就再也进不了跑批了——用户点了"撤销"却发现
    AI 再也不标它，这种 bug 极难查。
    """
    src = inspect.getsource(LabelingRepository.reset_review)
    assert "FLAG_UNLABELED" in src, "撤销没回到 0"
    assert "= 4" not in src


def test_引擎配置里的三档和这边对得上():
    """引擎那份 config 是我们生成的，两边必须同一套语义。"""
    from app.core.config import load_config
    from app.labeling.config_bridge import build_engine_config

    cfg = build_engine_config(load_config(use_cache=False))
    flags = cfg["storage"]["review_flags"]
    assert flags["ai_success"] == FLAG_AI_OK
    assert flags["ai_failed"] == FLAG_AI_FAILED
    assert flags["ai_low_confidence"] == FLAG_AI_LOW_CONF
    assert cfg["storage"]["review_column"] == "label_review_flag", (
        "没接上这一列的话引擎会跳过整套标记逻辑，审核页面按状态筛就全是空的"
    )
    # pending_flags 里不能有 4：引擎标成功写的就是 4，会无限重标
    assert FLAG_AI_OK not in cfg["storage"]["pending_flags"], (
        "pending_flags 含 4 会导致跑批无限重标（取数条件不随处理收缩）"
    )


def test_表结构里那一列的注释是七档语义():
    """老库靠 EXTRA_COLUMNS 补列，注释写错会误导以后查库的人。"""
    from app.db.tables import EXTRA_COLUMNS

    row = [c for c in EXTRA_COLUMNS if c[1] == "label_review_flag"]
    assert row, "label_review_flag 没登记在 EXTRA_COLUMNS 里，老库升级会缺列"
    definition = row[0][2]
    for token in ("0未标注", "4AI标注成功", "6未人工复核"):
        assert token in definition, f"列注释缺 {token}：{definition}"
