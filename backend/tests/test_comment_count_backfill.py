# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""作品「评论数」回填：翻完评论才知道总数时，作品可能已经先写进库了。

以前快手接口 / 混合模式采的作品，库里评论数永远是 0，下面却挂着上百条评论。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.repositories.data_repo import WORK_COLUMNS, WORK_UPDATE_COLUMNS, _upsert_sql
from app.scheduler.runner import _Buffer


class _Data:
    def __init__(self):
        self.updated = []

    async def update_work_comment_cnt(self, work):
        self.updated.append((work.work_id, work.comment_cnt))
        return 1


class _Log:
    def warn(self, message):
        pass


def _work(cnt=0):
    return SimpleNamespace(work_id="w1", scenic_id="s", channel="kuaishou", comment_cnt=cnt)


async def test_already_flushed_work_gets_update():
    data = _Data()
    buffer = _Buffer(data, _Log())
    work = _work(305)
    await buffer.sync_comment_cnt(work, 0)
    assert data.updated == [("w1", 305)]


async def test_work_still_in_buffer_needs_no_update():
    data = _Data()
    buffer = _Buffer(data, _Log())
    work = _work(305)
    buffer.works.append(work)
    await buffer.sync_comment_cnt(work, 0)
    assert data.updated == []


async def test_unchanged_count_needs_no_update():
    data = _Data()
    buffer = _Buffer(data, _Log())
    await buffer.sync_comment_cnt(_work(12), 12)
    await buffer.sync_comment_cnt(_work(0), 0)
    assert data.updated == []


def test_upsert_keeps_stored_comment_count_when_new_value_is_zero():
    sql = _upsert_sql("t", WORK_COLUMNS, WORK_UPDATE_COLUMNS)
    assert "`comment_cnt` = IF(VALUES(`comment_cnt`) > 0, VALUES(`comment_cnt`), `comment_cnt`)" in sql
    # 别的计数照常覆盖
    assert "`likes` = VALUES(`likes`)" in sql
