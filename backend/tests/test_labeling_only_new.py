# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""采到的评论进标注：库里已经有的不再标注，也不更新。

以前每次落库后把**这一批全部**推给标注引擎——同一个景区每天跑一轮，
绝大多数评论上一轮就收过、标过，又被推一遍：白花模型调用，还会把人工复核过的结果盖掉。
"""
from __future__ import annotations

from typing import List

from app.collectors.base import CommentItem
from app.scheduler import runner as runner_module
from app.scheduler.runner import _Buffer


def _comment(cid):
    return CommentItem(channel="douyin", comment_id=cid, work_id="w1", scenic_id="s",
                       scenic_name="景区", content=f"评论{cid}").finalize()


class _Repo:
    """库里已经有 c1：save_comments 跳过它，只把 c2 当新增。"""

    def __init__(self, existing):
        self.existing = set(existing)
        self.skip_existing_seen = None

    async def save_comments(self, items, *, skip_existing=True, new_items=None):
        self.skip_existing_seen = skip_existing
        fresh = [i for i in items if i.comment_id not in self.existing]
        if new_items is not None:
            new_items.extend(fresh)
        self.existing.update(i.comment_id for i in items)
        return len(fresh), len(items) - len(fresh)

    def invalidate_overview_cache(self):
        pass


class _Log:
    def __init__(self):
        self.lines: List[str] = []

    def info(self, message, *a, **kw):
        self.lines.append(message)

    warn = error = info


async def test_only_new_comments_go_to_labeling(monkeypatch):
    sent = []

    async def fake_submit(items):
        sent.extend(i.comment_id for i in items)
        return len(items)

    monkeypatch.setattr(runner_module, "submit_for_labeling", fake_submit)
    log = _Log()
    buffer = _Buffer(_Repo(existing={"c1"}), log)
    for cid in ("c1", "c2"):
        await buffer.add_comment(_comment(cid))
    await buffer.flush()

    assert sent == ["c2"]
    assert buffer.data.skip_existing_seen is True          # 默认也不更新已有的
    assert any("1 条评论库里已有，不重复标注" in line for line in log.lines)


async def test_nothing_new_nothing_labeled(monkeypatch):
    sent = []

    async def fake_submit(items):
        sent.extend(items)
        return len(items)

    monkeypatch.setattr(runner_module, "submit_for_labeling", fake_submit)
    buffer = _Buffer(_Repo(existing={"c1", "c2"}), _Log())
    for cid in ("c1", "c2"):
        await buffer.add_comment(_comment(cid))
    await buffer.flush()
    assert sent == []


def test_box_client_accepts_the_same_arguments_as_the_repository():
    """runner 写评论时盒子和主走的是同一句调用：盒子的远程客户端少一个参数，
    盒子上每次写评论就是 TypeError。"""
    import inspect

    from app.box.client import RemoteDataRepository
    from app.repositories.data_repo import DataRepository

    def kwonly(func):
        return {name for name, p in inspect.signature(func).parameters.items()
                if p.kind is inspect.Parameter.KEYWORD_ONLY}

    assert kwonly(DataRepository.save_comments) <= kwonly(RemoteDataRepository.save_comments)
