#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
@Time ：2026/11/5 11:59
@Auth ：keanying
@File ：build_release.py
@IDE  ：PyCharm
@Motto：ABC(Always Be Coding)


运行：
python build_release.py
出来的 release/ 里，backend/app/** 全是 .so，.py 都没了（除了 run.py）。

第三步：客户机上部署
把 release/ 拷过去，照常配好 config.yaml（数据库密码、模型 Key——这些本来就是配置，不加密），然后：

pip install -r backend/requirements.txt
python run.py

跑起来和明文版一模一样。
"""
import shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "backend"
OUT = ROOT / "release"
# 要编译的目录（真正的业务逻辑）
COMPILE_DIRS = ["app", "vendor", "tags"]
# 不编译、原样保留的后缀
KEEP_SUFFIX = {".yaml", ".yml", ".sql", ".json", ".js", ".txt", ".md", ".svg", ".ico", ".png"}
# 入口文件保持明文（否则没法启动）
KEEP_PY = {"run.py"}

def compile_one(py: Path):
    """把一个 .py 编成同目录的 .so，成功后删掉 .py 和中间产物。"""
    r = subprocess.run([sys.executable, "-m", "cython", "-3", "--embed=False", str(py)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[跳过] {py} 转 C 失败，保留源码：{r.stderr.splitlines()[-1] if r.stderr else ''}")
        return False
    c = py.with_suffix(".c")
    import sysconfig
    inc = sysconfig.get_path("include")
    so = py.with_suffix("").as_posix() + ".so"
    g = subprocess.run(["gcc", "-shared", "-fPIC", "-O2", f"-I{inc}", str(c), "-o", so],
                       capture_output=True, text=True)
    c.unlink(missing_ok=True)
    if g.returncode != 0:
        print(f"[跳过] {py} 编译失败，保留源码：{g.stderr.splitlines()[-1] if g.stderr else ''}")
        return False
    py.unlink()                       # 删掉源码
    return True

def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    # 1. 整个 backend 拷到 release，连同前端产物、配置模板
    shutil.copytree(SRC, OUT / "backend", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "tests", ".env*", "*.log"))
    shutil.copytree(ROOT / "frontend" / "dist", OUT / "frontend" / "dist")
    shutil.copy(ROOT / "run.py", OUT / "run.py")
    shutil.copytree(ROOT / "config", OUT / "config", dirs_exist_ok=True)

    # 2. 编译
    ok = fail = kept = 0
    for d in COMPILE_DIRS:
        base = OUT / "backend" / d if not d.startswith("vendor") else OUT / "backend" / d
        for py in base.rglob("*.py"):
            if py.name in KEEP_PY:
                kept += 1
                continue
            if compile_one(py):
                ok += 1
            else:
                fail += 1
    print(f"\n编译完成：成功 {ok}，失败(保留源码) {fail}，入口保留 {kept}")
    print(f"产物在 {OUT}/")

if __name__ == "__main__":
    main()