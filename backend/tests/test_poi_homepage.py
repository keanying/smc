"""携程/同程/去哪儿的景区主页：手工指定的优先，没指定才自动拼。

合成作品的 work_url 会原样写进 src_opinion_social_work_di.work_url，
使用方要的是他们自己选的那个页面，而不是按 POI ID 拼出来的默认页。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.schemas import TargetIn                      # noqa: E402
from app.collectors.base import CollectContext, CollectTarget  # noqa: E402
from app.collectors.ctrip import CtripCollector           # noqa: E402
from app.collectors.qunar import QunarCollector           # noqa: E402
from app.collectors.tongcheng import TongchengCollector   # noqa: E402
from app.core.config import load_config                   # noqa: E402
from app.repositories.scenic_repo import ScenicRepository  # noqa: E402

DEFAULTS = {
    CtripCollector: "https://you.ctrip.com/sight/32289.html",
    TongchengCollector: "https://www.ly.com/scenery/BookSceneryTicket_32289.html",
    QunarCollector: f"{QunarCollector.base_url}/32289",
}
CUSTOM = "https://you.ctrip.com/sight/yongzhou1018/86442.html"


def _work(cls, url=""):
    collector = cls(load_config(use_cache=False), SimpleNamespace())
    ctx = CollectContext(scenic_id="S1", scenic_name="九嶷山", params={})
    target = CollectTarget(target_type="poi", value="32289", name="九嶷山", url=url)
    return collector.poi_work(ctx, target)


@pytest.mark.parametrize("cls", list(DEFAULTS))
def test_specified_homepage_wins(cls):
    assert _work(cls, CUSTOM).work_url == CUSTOM


@pytest.mark.parametrize("cls", list(DEFAULTS))
def test_specified_homepage_is_trimmed(cls):
    assert _work(cls, f"  {CUSTOM}\n").work_url == CUSTOM


@pytest.mark.parametrize("cls,expected", list(DEFAULTS.items()))
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_falls_back_to_auto_url_when_not_specified(cls, expected, blank):
    assert _work(cls, blank).work_url == expected


# ---------------------------------------------------------------- 入口校验
def test_target_url_blank_becomes_none():
    assert TargetIn(channel="ctrip", target_id="1", target_url="  ").target_url is None


def test_target_url_is_trimmed():
    assert TargetIn(channel="ctrip", target_id="1",
                    target_url=f" {CUSTOM} ").target_url == CUSTOM


@pytest.mark.parametrize("bad", ["you.ctrip.com/sight/1.html", "javascript:alert(1)",
                                 "https://x.com/" + "a" * 600])
def test_target_url_rejects_garbage(bad):
    with pytest.raises(ValueError):
        TargetIn(channel="ctrip", target_id="1", target_url=bad)


# ---------------------------------------------------------------- 自动导入不覆盖手填
class _FakeDb:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, args=None):
        self.sql.append(sql)


def _upsert_sql(**kwargs):
    db = _FakeDb()
    asyncio.run(ScenicRepository(db).upsert_target({
        "scenic_id": "S1", "channel": "ctrip", "target_id": "32289",
        "target_url": "https://you.ctrip.com/sight/32289.html",
    }, **kwargs))
    return " ".join(db.sql[0].split())


def test_manual_save_overwrites_url():
    """页面上手工保存：填什么存什么，清空也要生效。"""
    assert "target_url = VALUES(target_url)," in _upsert_sql()


def test_auto_import_keeps_existing_url():
    """档案导入/去哪儿导入带的是自动拼的链接，不能把手填的主页盖掉。"""
    sql = _upsert_sql(keep_url=True)
    assert "target_url = IF(target_url IS NULL OR target_url = ''" in sql
    assert "target_url = VALUES(target_url)," not in sql
