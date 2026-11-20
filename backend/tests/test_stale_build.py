# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""旧的编译文件挡住新源码：代码更新了、重启了，跑的还是老逻辑（线上发老格式告警就是这么来的）。"""
from __future__ import annotations

import os
import time

from app.core.stale_build import describe, find_shadowed


def test_finds_compiled_module_next_to_newer_source(tmp_path):
    pkg = tmp_path / "app" / "scheduler"
    pkg.mkdir(parents=True)
    so = pkg / "runner.cpython-311-x86_64-linux-gnu.so"
    so.write_bytes(b"old")
    old = time.time() - 3600
    os.utime(so, (old, old))
    (pkg / "runner.py").write_text("# new")
    (pkg / "other.py").write_text("# 只有源码，正常")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lonely.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")  # 只有编译版，正常

    found = find_shadowed(tmp_path)
    assert found == [{"source": "app/scheduler/runner.py",
                      "compiled": "app/scheduler/runner.cpython-311-x86_64-linux-gnu.so",
                      "newer_source": True}]
    text = describe(found)
    assert "app/scheduler/runner.py" in text and "旧代码" in text


def test_clean_source_tree_reports_nothing():
    assert find_shadowed() == [], "仓库里不该有编译文件和源码并存"
