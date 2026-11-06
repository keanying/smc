# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""删账号 / 退出登录：浏览器 profile 被删掉、删不干净、所在机器连不上时，账号都要能删掉。

以前的 bug：删账号先调 clear_profile → profile_dir()，而 profile_dir() 会**把目录建回来**；
目录被手动删掉、原位置又建不回来（服务用户没写权限 / 路径不在了）时直接抛错，
库里的记录根本走不到删除那一步——这个号就永远删不掉了。
"""
from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from app.api import accounts as accounts_api
from app.browser import live_session
from app.browser.manager import BrowserSessionManager


class _Cfg:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Repo:
    """只记调用，不连库。"""

    def __init__(self):
        self.updated = []
        self.deleted = []

    async def update(self, channel, name, fields):
        self.updated.append((channel, name, fields))

    async def delete(self, channel, name):
        self.deleted.append((channel, name))

    async def get(self, channel, name):
        return {"channel": channel, "account_name": name}


def _manager(profiles_dir, repo=None):
    return BrowserSessionManager(_Cfg({"browser.profiles_dir": str(profiles_dir)}), repo or _Repo())


def _unbuildable(tmp_path):
    """profile 根目录已经没了，而且建不回来：父路径是个普通文件（效果和没写权限一样）。"""
    blocker = tmp_path / "data"
    blocker.write_text("")
    return blocker / "browser_profiles"


def test_profile_path_does_not_create_directory(tmp_path):
    m = _manager(tmp_path / "profiles")
    path = m.profile_path("douyin", "a/b c")
    assert path == tmp_path / "profiles" / "douyin" / "a_b_c"
    assert not path.exists()


def test_remove_profile_files_when_directory_already_gone(tmp_path):
    m = _manager(_unbuildable(tmp_path))
    # 原来这里会先 mkdir 再删，mkdir 失败直接抛 NotADirectoryError / PermissionError
    assert m.remove_profile_files("douyin", "acc") == ""


def test_remove_profile_files_deletes_including_readonly(tmp_path):
    m = _manager(tmp_path / "profiles")
    directory = m.profile_dir("douyin", "acc")
    locked = directory / "Default" / "Cookies"
    locked.parent.mkdir(parents=True)
    locked.write_text("x")
    os.chmod(locked, 0o444)
    assert m.remove_profile_files("douyin", "acc") == ""
    assert not directory.exists()


def test_remove_profile_files_reports_instead_of_raising(tmp_path, monkeypatch):
    m = _manager(tmp_path / "profiles")
    directory = m.profile_dir("douyin", "acc")

    def stuck(path, *args, **kwargs):          # 删不动：被别的进程占着 / 没权限
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "rmtree", stuck)
    message = m.remove_profile_files("douyin", "acc")
    assert directory.exists()
    assert "没删干净" in message and str(directory) in message


async def test_clear_profile_survives_unbuildable_directory(tmp_path):
    repo = _Repo()
    m = _manager(_unbuildable(tmp_path), repo)
    assert await m.clear_profile("douyin", "acc") == ""
    # 登录态照样清掉
    assert repo.updated and repo.updated[0][2]["cookies"] is None


async def test_close_account_browsers_closes_only_that_account(monkeypatch):
    stopped = []

    def fake_session(channel, name):
        async def stop():
            stopped.append((channel, name))
        return SimpleNamespace(channel=channel, account_name=name, stop=stop)

    monkeypatch.setattr(live_session, "LIVE_SESSIONS", {
        "s1": fake_session("douyin", "acc"), "s2": fake_session("douyin", "other"),
        "s3": fake_session("kuaishou", "acc"),
    })
    m = _manager("/nonexistent")
    assert await m.close_account_browsers("douyin", "acc") == 1
    assert stopped == [("douyin", "acc")]


def _state(manager, repo):
    return SimpleNamespace(browser_manager=manager, accounts=repo)


async def test_delete_account_works_after_profile_deleted(tmp_path):
    repo = _Repo()
    m = _manager(_unbuildable(tmp_path), repo)
    result = await accounts_api.delete_account("douyin", "acc", state=_state(m, repo))
    assert repo.deleted == [("douyin", "acc")]
    assert result["code"] == 0


async def test_delete_account_still_deletes_when_profile_cleanup_breaks(tmp_path):
    repo = _Repo()
    m = _manager(tmp_path / "profiles", repo)

    async def boom(*_):
        raise RuntimeError("浏览器关不掉")

    m.close_account_browsers = boom
    result = await accounts_api.delete_account("douyin", "acc", state=_state(m, repo))
    assert repo.deleted == [("douyin", "acc")]
    assert "浏览器关不掉" in result["data"]["profile_left"]


async def test_forward_delete_falls_back_to_local_when_node_unreachable():
    from app.cluster import forward

    handled = []
    sent = []

    async def local():
        handled.append(True)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "DELETE", "path": "/api/accounts/douyin/acc",
             "raw_path": b"/api/accounts/douyin/acc", "query_string": b"", "headers": []}
    cluster = SimpleNamespace(me=SimpleNamespace(node_id="here"))
    # 127.0.0.1:9 一般没有服务，连接会被拒绝
    await forward._forward_http(cluster, scope, b"", send, "http://127.0.0.1:9", "gone",
                                on_unreachable=local)
    assert handled == [True] and sent == []


async def test_forward_other_requests_still_report_502():
    from app.cluster import forward

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/accounts/douyin/acc/check",
             "raw_path": b"/api/accounts/douyin/acc/check", "query_string": b"", "headers": []}
    cluster = SimpleNamespace(me=SimpleNamespace(node_id="here"))
    await forward._forward_http(cluster, scope, b"", send, "http://127.0.0.1:9", "gone")
    assert sent[0]["status"] == 502


@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else True,
                    reason="root 不受目录权限限制，复现不了「没写权限」")
def test_remove_profile_files_without_write_permission(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    os.chmod(root, 0o555)
    try:
        assert _manager(root).remove_profile_files("douyin", "acc") == ""
    finally:
        os.chmod(root, 0o755)
