#!/usr/bin/env python3
# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途。

"""把 backend 下的 .py 全部用 Cython 编成 .so，产出一份不含源码的部署包。

特点：
- 不改原始代码：全程只在拷贝出来的输出目录里操作，backend/ 一个字都不动。
- 原地替换：目录结构保持不变，只是把 .py 换成同名 .so，删掉 .py。
  （Cython 的 .so 保留真实磁盘路径，所以靠 __file__ 定位目录的代码照样对，
   vendor 引擎的动态导入也照样找得到。）
- 非 .py 文件（yaml / sql / json 等）原样保留；tests 不打进去。
- 少数必须保持明文的入口文件（见 KEEP_AS_SOURCE）保留 .py。

★ 必须在**和部署机一样**的环境上运行本脚本：
    同样 Linux、同样 CPU 架构、同样 Python 小版本（本项目是 Python 3.11）。
    .so 认版本和平台，3.11 编的拿到 3.12 跑不了。

依赖：
    pip install cython
    系统要有 gcc 和 python3-dev（Ubuntu: apt install gcc python3.11-dev）

用法（在项目根目录）：
    python scripts/build_so.py                  # 输出到 ./release
    python scripts/build_so.py --out /some/dir  # 指定输出目录
    python scripts/build_so.py --jobs 4         # 指定并行数（默认按 CPU 核数）
    python scripts/build_so.py --keep-c         # 保留中间 .c，排查编译错误用

产出：
    <输出目录>/backend/   —— app、vendor 里全是 .so，入口文件保持 .py
部署（客户那边）：
    把 release/backend 覆盖到他们的 backend；照常 pip install -r requirements.txt；
    照常 python run.py 启动。运行方式、配置（config.yaml / 环境变量）都不变。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

# 要编译的子目录（相对 backend）。backend 下的业务代码都在这两处。
COMPILE_DIRS = ["app", "vendor"]

# 必须保持 .py 明文的文件（相对 backend）。编了会跑不起来：
#   - app/metrics/worker.py: 用 `python -m app.metrics.worker` 当子进程拉起，
#     python -m 对编译后的 .so 不可靠，保持明文。它只是读 stdin 调引擎，没有核心逻辑。
KEEP_AS_SOURCE = {
    "app/metrics/worker.py",
}

# 这些目录整个不打进部署包
EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache", ".git"}


def log(msg: str) -> None:
    print(msg, flush=True)


def copy_tree(out: Path) -> Path:
    """把 backend 拷到输出目录（排除 tests / 缓存 / .env / 日志）。返回拷贝出来的 backend 路径。"""
    dst = out / "backend"
    if dst.exists():
        shutil.rmtree(dst)

    def ignore(dirpath, names):
        drop = set()
        for n in names:
            if n in EXCLUDE_DIRS or n.endswith(".pyc") or n.endswith(".so"):
                drop.add(n)
            elif n.startswith(".env") or n.endswith(".log"):
                drop.add(n)
        return drop

    shutil.copytree(BACKEND, dst, ignore=ignore)
    return dst


def targets(backend_copy: Path) -> list[Path]:
    """要编译的 .py 列表（排除保持明文的入口）。"""
    out: list[Path] = []
    for sub in COMPILE_DIRS:
        base = backend_copy / sub
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            if any(part in EXCLUDE_DIRS for part in py.parts):
                continue
            rel = py.relative_to(backend_copy).as_posix()
            if rel in KEEP_AS_SOURCE:
                continue
            out.append(py)
    return out


def compile_one(py: Path, inc: str, ext_suffix: str, keep_c: bool) -> tuple[Path, bool, str]:
    """一个 .py → .c（cython）→ .so（gcc）。成功后删掉 .py 和 .c。返回 (文件, 成功?, 错误)。"""
    c_file = py.with_suffix(".c")
    so_file = py.with_name(py.stem + ext_suffix)
    try:
        r = subprocess.run(
            # annotation_typing=False：不把类型注解当成运行时强制检查。
            # 普通 Python 代码（没为 Cython 特意写）必须关掉它，否则像
            # `def register(c: type)` 遇到 ABCMeta 元类的类、`Dict[tuple, int]`
            # 这类注解都会让 Cython 报错或在运行时拒绝参数。
            [sys.executable, "-m", "cython", "-3", "--no-docstrings",
             "-X", "language_level=3", "-X", "annotation_typing=False",
             str(py), "-o", str(c_file)],
            capture_output=True, text=True)
        if r.returncode != 0:
            return py, False, "cython: " + (r.stderr.strip().splitlines() or [""])[-1]

        cc = os.environ.get("CC", "gcc")
        cflags = ["-shared", "-fPIC", "-O2", "-fvisibility=hidden", f"-I{inc}"]
        # Python 自身的编译参数（取到正确的 ABI 宏等）
        extra = (sysconfig.get_config_var("CFLAGS") or "").split()
        extra = [f for f in extra if not f.startswith(("-g", "-O", "-Werror"))]
        g = subprocess.run([cc, *cflags, *extra, str(c_file), "-o", str(so_file)],
                           capture_output=True, text=True)
        if g.returncode != 0:
            return py, False, "gcc: " + (g.stderr.strip().splitlines() or [""])[-1]

        py.unlink()
        return py, True, ""
    finally:
        if not keep_c:
            c_file.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="把 backend 的 .py 编成 .so，产出无源码部署包")
    ap.add_argument("--out", default=str(ROOT / "release"), help="输出目录，默认 ./release")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4, help="并行编译数")
    ap.add_argument("--keep-c", action="store_true", help="保留中间 .c 文件（排错用）")
    args = ap.parse_args()

    try:
        import Cython  # noqa: F401
    except ImportError:
        log("[错误] 没装 Cython。先 pip install cython")
        return 2
    if not shutil.which(os.environ.get("CC", "gcc")):
        log("[错误] 找不到 gcc。Ubuntu: apt install gcc python3-dev")
        return 2
    inc = sysconfig.get_path("include")
    if not Path(inc, "Python.h").exists():
        log(f"[错误] 找不到 Python.h（{inc}）。装 python3-dev / python3.11-dev")
        return 2

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")  # 如 .cpython-311-x86_64-linux-gnu.so
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    log(f"Python {sys.version.split()[0]} | 产物后缀 {ext_suffix}")
    log(f"拷贝 backend → {out}/backend …")
    backend_copy = copy_tree(out)

    files = targets(backend_copy)
    log(f"要编译 {len(files)} 个文件，并行 {args.jobs} …\n")

    start = time.time()
    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(compile_one, f, inc, ext_suffix, args.keep_c): f for f in files}
        for i, fut in enumerate(as_completed(futs), 1):
            py, good, err = fut.result()
            rel = py.relative_to(backend_copy).as_posix()
            if good:
                ok += 1
            else:
                failed.append((rel, err))
                log(f"[失败] {rel}\n        {err}")
            if i % 40 == 0:
                log(f"  …已处理 {i}/{len(files)}")

    # 清理编译残留目录
    for junk in backend_copy.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)

    dur = time.time() - start
    log(f"\n完成：成功编译 {ok} 个，失败 {len(failed)} 个，用时 {dur:.0f}s")
    log(f"保持明文的入口文件：{', '.join(sorted(KEEP_AS_SOURCE))}")
    if failed:
        log("\n⚠️ 下面这些没编成，输出里仍是 .py 源码，请排查（--keep-c 看 .c）：")
        for rel, err in failed:
            log(f"   {rel}")
        log("\n编译失败不代表功能有问题，但源码没被保护。确认原因后再交付。")
    # 还剩多少 .py（应只剩 KEEP_AS_SOURCE 和失败的）
    left = [p.relative_to(backend_copy).as_posix()
            for sub in COMPILE_DIRS for p in (backend_copy / sub).rglob("*.py")
            if (backend_copy / sub).exists() and "__pycache__" not in p.parts]
    log(f"\n产物在 {out}/backend/，其中还剩 {len(left)} 个 .py（明文入口 + 未编成的）")
    log("★ 交付前务必：把 release/backend 放到同环境，python run.py 起服务、点几个接口、"
        "跑一次指标计算，确认都正常。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
