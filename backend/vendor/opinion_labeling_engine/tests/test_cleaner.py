"""内容清洗单测：纯表情 / 纯符号 / 灌水 必须被拦在模型之前。"""

from __future__ import annotations

import pytest

from opinion_labeling_engine.config import CleaningConfig
from opinion_labeling_engine.preprocess.cleaner import ContentCleaner, InvalidReason


@pytest.fixture()
def cleaner() -> ContentCleaner:
    return ContentCleaner(CleaningConfig())


@pytest.mark.parametrize("text,reason", [
    ("", InvalidReason.EMPTY),
    ("   ", InvalidReason.EMPTY),
    ("😀😀😀", InvalidReason.EMOJI_ONLY),
    ("👍🏻👍🏻", InvalidReason.EMOJI_ONLY),
    ("[微笑R][赞R]", InvalidReason.EMOJI_ONLY),
    ("[doge][doge][doge]", InvalidReason.EMOJI_ONLY),
    ("。。。。。。", InvalidReason.SYMBOL_ONLY),
    ("！！！???", InvalidReason.SYMBOL_ONLY),
    ("——————", InvalidReason.SYMBOL_ONLY),
    ("666666", InvalidReason.REPEAT_ONLY),
    ("哈哈哈哈哈哈", InvalidReason.REPEAT_ONLY),
    ("123456", InvalidReason.DIGIT_ONLY),
    ("https://example.com/abc", InvalidReason.URL_ONLY),
    ("此用户没有填写评价", InvalidReason.PLACEHOLDER),
])
def test_invalid_content_is_rejected(cleaner, text, reason):
    result = cleaner.clean(text)
    assert result.valid is False
    assert result.reason == reason


@pytest.mark.parametrize("text", [
    "完美的一天",
    "风景很美但厕所太差、太脏",
    "排队两小时，玩了五分钟😭",
    "[微笑R]性价比挺高的",
    "服务态度很好 👍",
])
def test_valid_content_passes(cleaner, text):
    result = cleaner.clean(text)
    assert result.valid is True
    assert result.text


def test_emoji_is_stripped_but_text_kept(cleaner):
    result = cleaner.clean("风景真美😍😍，值得再来[赞R]")
    assert result.valid is True
    assert "😍" not in result.text
    assert "[赞R]" not in result.text
    assert "风景真美" in result.text
    assert result.emoji_stripped >= 2


def test_original_is_preserved_for_traceability(cleaner):
    raw = "太坑了😡"
    result = cleaner.clean(raw)
    assert result.original == raw


def test_truncation():
    cfg = CleaningConfig(max_content_length=10)
    cleaner = ContentCleaner(cfg)
    result = cleaner.clean("一" + "二三四五六七八九十百千万")
    assert result.valid is True
    assert len(result.text) == 10
    assert result.truncated is True


def test_min_length_counts_meaningful_chars_only():
    cfg = CleaningConfig(min_valid_length=3)
    cleaner = ContentCleaner(cfg)
    # "好!!!" 只有 1 个有信息量字符，应判太短
    assert cleaner.clean("好！！！").valid is False
    assert cleaner.clean("还不错").valid is True


def test_full_width_is_normalized(cleaner):
    result = cleaner.clean("ＷＩＦＩ信号很差")
    assert result.valid is True
    assert "WIFI" in result.text
