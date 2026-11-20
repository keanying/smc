# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""发现「旧的编译文件挡住了新代码」。

编译交付（scripts/build_so.py）是把 .py 换成同名 .so。之后如果又用源码（git pull、
拷 .py）更新，旧的 .so 还留在原地——**同一个目录里 .so 和 .py 同名时，Python 先加载 .so**。
结果代码明明更新了、服务也重启了，跑的还是老逻辑，发的还是老格式的通知，
查起来非常绕（线上就这么绕过一回）。启动时扫一遍，发现就大声说出来。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

BACKEND = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("app", "vendor")


def find_shadowed(root: Path = BACKEND) -> List[Dict[str, object]]:
    """同目录下同名的编译模块（.so / .pyd）和 .py 并存的清单。

    newer_source=True：.py 比 .so 新——几乎可以肯定是"源码更新了、旧编译文件没删"，
    此时运行的是 .so 里的老代码。
    """
    found: List[Dict[str, object]] = []
    for sub in SCAN_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for ext in base.rglob("*"):
            if ext.suffix not in (".so", ".pyd") or "__pycache__" in ext.parts:
                continue
            source = ext.with_name(ext.name.split(".", 1)[0] + ".py")
            if not source.exists():
                continue
            try:
                newer = source.stat().st_mtime > ext.stat().st_mtime
            except OSError:
                newer = False
            found.append({"source": source.relative_to(root).as_posix(),
                          "compiled": ext.relative_to(root).as_posix(),
                          "newer_source": newer})
    return found


def describe(found: List[Dict[str, object]], limit: int = 8) -> str:
    names = [str(f["source"]) for f in found[:limit]]
    more = f" 等 {len(found)} 个" if len(found) > limit else ""
    stale = sum(1 for f in found if f["newer_source"])
    return (f"{len(found)} 个模块同时有编译文件和源码（其中 {stale} 个源码更新）："
            f"{'、'.join(names)}{more}。Python 会先加载编译文件，运行的是旧代码。"
            f"处理：删掉 backend/app、backend/vendor 下的 *.so / *.pyd 后重启；"
            f"或者重新编译整包、先删掉旧 backend 再放新包（不要覆盖）")
