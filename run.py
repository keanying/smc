#!/usr/bin/env python
"""统一启动入口：在**任何目录**下 `python run.py` 都能起来。

    python run.py                # 正常启动（推荐）
    python run.py --reload       # 改代码自动重启（开发用）
    python run.py --port 8080    # 换端口
    python run.py --host 0.0.0.0 # 允许局域网访问（注意下面的安全提示）

为什么要有这个文件——直接敲 uvicorn 有两个很容易踩的坑：

1) `python -m uvicorn backend.app.main:app --reload` 在**项目根目录**下跑，
   uvicorn 会把整个 `smc/` 目录交给 WatchFiles 监视，其中包括
   `data/browser_profiles/`——那是 Chrome 的用户数据目录，用久了有
   几万个小文件。监视器启动前要先把整棵树遍历一遍，Windows 上这一步
   能卡几分钟，表现就是"打印完 Started reloader process 之后再没动静，
   页面打不开"。而且采集过程中 Chrome 一直在写这个目录，
   会不停触发无谓的重启。
   这里显式把监视范围限定在 `backend/`，并排除 data / __pycache__ /
   node_modules / frontend/dist。

2) `app.main:app` 和 `backend.app.main:app` 是**两条不同的导入路径**，
   混用会得到两份模块实例（配置、注册表、浏览器循环各一份）。
   这里统一走 `app.main:app`，和 scripts/start.sh、start.bat 一致。

⚠️ 安全：这个服务**没有登录鉴权**，而且实时画面里能看到已登录的平台页面。
   只在本机用，别用 --host 0.0.0.0 暴露到公网。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"


def main() -> int:
    if not BACKEND.is_dir():
        print(f"[错误] 找不到 backend 目录：{BACKEND}")
        return 2
    sys.path.insert(0, str(BACKEND))

    parser = argparse.ArgumentParser(add_help=True, description="启动采集服务")
    parser.add_argument("--host", default=None, help="默认取 config 的 server.host")
    parser.add_argument("--port", type=int, default=None,
                        help="默认取 config 的 server.port")
    parser.add_argument("--reload", action="store_true",
                        help="改代码自动重启（只监视 backend/ 下的 .py）")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    try:
        from app.core.config import load_config
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        # 配置有问题时说人话，别甩一堆 traceback——最常见的就是没建 config.yaml
        print(f"[错误] 读取配置失败：{exc}")
        print(f"       配置文件应该在：{ROOT / 'config' / 'config.yaml'}")
        print(f"       没有的话，复制一份 config/config.example.yaml 再填数据库密码")
        return 2

    host = args.host or config.get("server.host", "127.0.0.1")
    port = args.port or int(config.get("server.port", 8000))
    log_level = args.log_level or str(config.get("server.log_level", "info")).lower()

    import uvicorn

    options = dict(host=host, port=port, log_level=log_level)
    if args.reload:
        # ⚠️ 只监视 backend/，而且排除会被频繁写入的目录。
        # 少了这几行，watchfiles 会去遍历 data/browser_profiles，
        # 那里是 Chrome 的用户数据，几万个文件，Windows 上能把启动卡死。
        options.update(
            reload=True,
            reload_dirs=[str(BACKEND)],
            reload_includes=["*.py"],
            reload_excludes=[
                "*/data/*", "*/__pycache__/*", "*/node_modules/*",
                "*/.venv/*", "*/frontend/dist/*", "*.log",
            ],
        )
        print("[提示] 已开启自动重启，只监视 backend/ 下的 .py 文件")

    # 前端有没有构建，启动前就说清楚——不然服务起来了页面还是空白，
    # 很容易以为是后端没起
    dist = ROOT / "frontend" / "dist" / "index.html"
    if not dist.is_file():
        print("[注意] 没有找到 frontend/dist —— 接口能用，但**页面是空白的**。")
        print("       构建一次：cd frontend && npm install && npm run build")

    print(f"[启动] http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
          f"   （接口文档 /docs）")
    # ⚠️ 这一行是给人看的：Windows 上冷启动要 import playwright / pandas /
    # aiomysql 这一堆，几十秒很正常。在看到 uvicorn 打出
    # "Application startup complete" 之前，浏览器打开是 ERR_CONNECTION_REFUSED，
    # 那是**还没起来**，不是起不来。别急着刷新。
    print("[提示] 首次启动要加载浏览器驱动等依赖，可能要几十秒。"
          "看到 Application startup complete 再打开浏览器。")
    if host == "0.0.0.0":
        print("[注意] 监听了所有网卡。本服务没有登录鉴权，"
              "实时画面里能看到已登录的平台页面，别暴露到公网。")

    uvicorn.run("app.main:app", **options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
