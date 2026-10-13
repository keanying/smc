"""数据中心：作品是因为哪个关键字收录的 + 删除作品（连同评论）。

客户原话：
  · "记录一下是哪个关键字命中作品从而收录……多个命中的话要在标题最右侧展示出来"
  · "采集到的作品要支持删除，如复核觉得不对，点击删除作品，顺带评论也一起删除"
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.collectors.base import CommentItem, WorkItem, merge_words
from app.core.content_filter import MODE_KEYWORD_PLUS, ContentFilter

TEST_DB = "scenic_media_work_hits"


# ---------------------------------------------------------------- 纯逻辑
def test_merge_words_dedups_keeps_order_and_never_cuts_a_word():
    assert merge_words("八大处,灵光寺", "灵光寺,西山", "") == "八大处,灵光寺,西山"
    assert merge_words(["a", " b "], "b,c") == "a,b,c"
    long = merge_words(*[f"词{i:03d}" for i in range(400)])
    assert len(long) <= 1000 and long.split(",")[-1].startswith("词"), "超长丢整词，不截半个"


def test_hits_records_search_word_and_aux_words_that_appear():
    rules = ContentFilter(mode=MODE_KEYWORD_PLUS, extra_keywords=["灵光寺", "西山", "八大处"])
    main, aux = rules.hits("周末去西山灵光寺爬山", "八大处公园")
    assert main == ["八大处公园"], "搜到它的那个词就是主关键字，正文里没出现也算"
    assert aux == ["灵光寺", "西山"], "附关键字只记正文里真出现了的"
    assert ContentFilter().hits("随便什么", "") == ([], []), "主页采集没有关键字"


# ---------------------------------------------------------------- 真实 MySQL
@pytest.fixture()
async def repo():
    old = os.environ.get("SMC_MYSQL_DATABASE")
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"
    from app.core.config import load_config, reset_cache
    from app.core.db import Database
    from app.repositories.data_repo import DataRepository

    reset_cache()
    db = Database(load_config(use_cache=False))
    try:
        await db.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"没有 MySQL：{exc}")
    try:
        yield SimpleNamespace(db=db, data=DataRepository(db))
    finally:
        await db.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        await db.close()
        if old is None:
            os.environ.pop("SMC_MYSQL_DATABASE", None)
        else:
            os.environ["SMC_MYSQL_DATABASE"] = old
        reset_cache()


def _work(work_id="w1", main="", aux="", channel="douyin", title="八大处的秋天"):
    return WorkItem(channel=channel, work_id=work_id, scenic_id="S1", scenic_name="八大处",
                    title=title, source_keyword=main.split(",")[0] if main else "",
                    hit_keywords=main, hit_aux_keywords=aux).finalize()


def _comment(work_id, cid, channel="douyin"):
    return CommentItem(channel=channel, work_id=work_id, comment_id=cid, scenic_id="S1",
                       scenic_name="八大处", content="不错").finalize()


async def _hits(repo, work_id):
    return await repo.db.fetch_one(
        "SELECT hit_keywords, hit_aux_keywords, source_keyword FROM src_opinion_social_work_di "
        "WHERE work_id = %s", [work_id])


@pytest.mark.asyncio
async def test_second_keyword_is_merged_not_overwritten(repo):
    await repo.data.save_works([_work(main="八大处", aux="灵光寺")])
    await repo.data.save_works([_work(main="八大处公园", aux="西山")])   # 另一个任务又搜到
    await repo.data.save_works([_work()])                              # 主页采集：没有关键字
    row = await _hits(repo, "w1")
    assert row["hit_keywords"] == "八大处,八大处公园"
    assert row["hit_aux_keywords"] == "灵光寺,西山"
    assert row["source_keyword"] == "八大处", "首次来源不变"


@pytest.mark.asyncio
async def test_same_task_duplicate_hit_is_recorded(repo):
    """同一个任务里第二个关键字又搜到它：不重写作品、不重翻评论，但命中词要补上。"""
    from app.scheduler.runner import _Buffer

    log = SimpleNamespace(info=lambda *a, **k: None, warn=lambda *a, **k: None)
    buf = _Buffer(repo.data, log)
    assert await buf.add_work(_work(main="八大处")) is True
    assert await buf.add_work(_work(main="八大处公园", aux="西山")) is False   # 还在缓冲里
    await buf.flush()
    assert await buf.add_work(_work(main="灵光寺")) is False                  # 已经落库
    await buf.flush()
    row = await _hits(repo, "w1")
    assert row["hit_keywords"] == "八大处,八大处公园,灵光寺"
    assert row["hit_aux_keywords"] == "西山"


@pytest.mark.asyncio
async def test_delete_work_removes_its_comments_and_is_remembered(repo):
    await repo.data.save_works([_work("w1", main="八大处"), _work("w2", main="八大处")])
    await repo.data.save_comments([_comment("w1", "c1"), _comment("w1", "c2"),
                                   _comment("w2", "c3")])
    pk = (await repo.db.fetch_one(
        "SELECT id FROM src_opinion_social_work_di WHERE work_id = 'w1'"))["id"]

    result = await repo.data.delete_work(pk)
    assert result["comments_deleted"] == 2
    left = await repo.db.fetch_all(
        "SELECT work_id, comment_id FROM src_opinion_social_work_comment_di ORDER BY comment_id")
    assert [(r["work_id"], r["comment_id"]) for r in left] == [("w2", "c3")], "别的作品的评论不能动"
    assert await _hits(repo, "w1") is None
    assert await repo.data.delete_work(pk) is None, "删过的再删一次 → 不存在"

    # 记下来了：下一轮采集再搜到它会跳过
    from app.repositories.data_repo import work_uk
    from app.scheduler.runner import _Buffer
    keys = await repo.data.deleted_work_keys("S1", "douyin")
    assert keys == {work_uk("S1", "douyin", "w1")}
    buf = _Buffer(repo.data, SimpleNamespace(info=print, warn=print))
    buf.deleted = keys
    assert buf.is_deleted(_work("w1")) and not buf.is_deleted(_work("w2"))


@pytest.mark.asyncio
async def test_api_refuses_to_delete_ota_review_container(repo):
    from fastapi import HTTPException

    from app.api.data import delete_work

    await repo.data.save_works([_work("poi-1", channel="ctrip", title="携程点评")])
    await repo.data.save_comments([_comment("poi-1", "r1", channel="ctrip")])
    pk = (await repo.db.fetch_one(
        "SELECT id FROM src_opinion_social_work_di WHERE work_id = 'poi-1'"))["id"]
    with pytest.raises(HTTPException) as err:
        await delete_work(pk, state=SimpleNamespace(db=repo.db, data=repo.data))
    assert err.value.status_code == 400
    assert await _hits(repo, "poi-1") is not None, "景区点评一条都不能少"

    with pytest.raises(HTTPException) as err:
        await delete_work(999999, state=SimpleNamespace(db=repo.db, data=repo.data))
    assert err.value.status_code == 404
