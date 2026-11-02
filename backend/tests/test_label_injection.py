# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""标注灌注：景区管理里给每个景区配的补充标注提示词，标注时拼进正式提示词。

- 只拼进**这个景区**的评论，别的景区不受影响；
- 打开「关键词放宽」：关键词不要求和整体情感同向、可以带书名号保留全称、上限放大；
  没打开的景区，原来的关键词校验一点不变；
- 页面保存后马上推给标注引擎。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.labeling.config_bridge import engine_importable, ensure_on_path

pytestmark = pytest.mark.skipif(not engine_importable(), reason="标注引擎目录不完整")

ENGINE_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "opinion_labeling_engine"
TEST_DB = "scenic_media_label_injection_test"

HAILUO_PROMPT = """一、关键词定义
关键词是能承载评论主题、对象、事件、动作、关系、属性、评价、场景、时空、专有名称的词或短语。
1. 实体词：人物、地名、IP形象名称、影视剧名称、角色名等"""


@pytest.fixture()
def engine():
    ensure_on_path()
    from opinion_labeling_engine.config import LabelingConfig
    from opinion_labeling_engine.domain.taxonomy import Taxonomy
    from opinion_labeling_engine.labeling import scenic_rules
    from opinion_labeling_engine.labeling.postprocess import LabelPostProcessor
    from opinion_labeling_engine.llm.prompt import PromptBuilder

    tax = Taxonomy.load(ENGINE_ROOT / "config" / "taxonomy.yaml")
    yield {"prompt": PromptBuilder(tax), "post": LabelPostProcessor(tax, LabelingConfig()),
           "rules": scenic_rules}
    scenic_rules.set_rules({})


def _record(scenic_id, content):
    from opinion_labeling_engine.domain.models import CommentRecord
    return CommentRecord(comment_id="c1", content=content, scenic_id=scenic_id,
                         scenic_name="海蓝星梦幻城", channel="douyin")


def _cleaned(text):
    from opinion_labeling_engine.preprocess.cleaner import CleanResult
    return CleanResult(valid=True, text=text, original=text)


def test_prompt_only_for_its_own_scenic(engine):
    rules = engine["rules"]
    rules.set_rules({"hlx": rules.ScenicRule(prompt=HAILUO_PROMPT, broad_keywords=True)})
    text = "这是《小城良方》剧里那个海洋馆？"
    mine = engine["prompt"].build_user_content(_record("hlx", text), _cleaned(text))
    assert "本景区的补充标注要求" in mine and "关键词是能承载评论主题" in mine
    assert "不要求和整体情感同向" in mine
    assert mine.index("补充标注要求") < mine.index("评论正文")
    other = engine["prompt"].build_user_content(_record("other", text), _cleaned(text))
    assert "补充标注要求" not in other
    # 修复轮（解析失败重试）也带着
    repair = engine["prompt"].build_repair_messages(_record("hlx", text), _cleaned(text), "{bad", "x")
    assert any("关键词是能承载评论主题" in c["text"] for m in repair for c in m["content"])


def _keywords(engine, scenic_id, text, sentiment, words):
    from opinion_labeling_engine.llm.parser import RawLabel
    raw = RawLabel(relevant=True, sentiment=sentiment, confidence=0.9,
                   keywords=[{"word": w, "polarity": p} for w, p in words])
    return engine["post"].build(_record(scenic_id, text), raw, source_text=text).keyword_tags


def test_broad_keywords(engine):
    rules = engine["rules"]
    text = "小城良方剧组带火海洋馆，海蓝星梦幻城的梦梦好可爱但门票有点贵，工作人员态度一般般啦"
    words = [("《小城良方》", "中性"), ("海蓝星梦幻城", "中性"), ("梦梦好可爱", "正向"),
             ("门票有点贵", "负向"), ("工作人员态度一般般", "负向")]
    # 没配灌注：老规矩——只留和整体情感同向的，书名号的丢掉
    assert _keywords(engine, "hlx", text, "正向", words) == ["梦梦好可爱"]
    # 配了「关键词放宽」：不同向的、书名号全称、超过 12 字的都留
    rules.set_rules({"hlx": rules.ScenicRule(prompt="x", broad_keywords=True)})
    kept = _keywords(engine, "hlx", text, "正向", words)
    assert kept == ["《小城良方》", "海蓝星梦幻城", "梦梦好可爱", "门票有点贵", "工作人员态度一般般"]
    # 放宽也不放过编造的词
    assert _keywords(engine, "hlx", text, "正向", [("迪士尼乐园", "中性")]) == []
    # 只配提示词、没开放宽：校验照旧
    rules.set_rules({"hlx": rules.ScenicRule(prompt="x", broad_keywords=False)})
    assert _keywords(engine, "hlx", text, "正向", words) == ["梦梦好可爱"]


@pytest.fixture(scope="module")
def app_client():
    saved = {k: os.environ.get(k) for k in ("SMC_MYSQL_DATABASE", "SMC_SCHEDULER_ENABLED")}
    os.environ.update({"SMC_MYSQL_DATABASE": TEST_DB, "SMC_SCHEDULER_ENABLED": "false"})
    from app.core.config import load_config, reset_cache
    reset_cache()
    from fastapi.testclient import TestClient
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        import pymysql
        cfg = load_config(use_cache=False)
        try:
            conn = pymysql.connect(host=cfg.get("mysql.host"), port=int(cfg.get("mysql.port")),
                                   user=cfg.get("mysql.user"), password=cfg.get("mysql.password"))
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_cache()


def _ok(r):
    body = r.json()
    assert r.status_code == 200 and body.get("code", 0) == 0, body
    return body.get("data", body)


def test_save_pushes_to_engine(app_client):
    from opinion_labeling_engine.labeling.scenic_rules import rule_for, set_rules
    c = app_client
    _ok(c.post("/api/scenics", json={"scenic_id": "hlx", "scenic_name": "海蓝星梦幻城"}))
    got = _ok(c.get("/api/scenics/hlx/label-prompt"))
    assert got["prompt"] == "" and got["broad_keywords"] is False and got["max_length"] == 5000
    _ok(c.put("/api/scenics/hlx/label-prompt", json={"prompt": HAILUO_PROMPT, "broad_keywords": True}))
    assert rule_for("hlx").prompt == HAILUO_PROMPT and rule_for("hlx").broad_keywords is True
    assert _ok(c.get("/api/scenics/hlx/label-prompt"))["prompt"] == HAILUO_PROMPT
    # 清空：引擎里也没了
    _ok(c.put("/api/scenics/hlx/label-prompt", json={"prompt": "", "broad_keywords": False}))
    assert rule_for("hlx") is None
    assert c.put("/api/scenics/hlx/label-prompt", json={"prompt": "字" * 5001}).status_code == 400
    set_rules({})
