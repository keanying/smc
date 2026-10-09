"""（服务自检）确认服务能起来、页面和接口都拿得到。

    python verify_boot.py            # 正常模式
    python verify_boot.py --reload   # 顺带验一下自动重启模式

「打不开页面」这个现象的原因太多了：配置没填、数据库没起、端口被占、
前端没构建、启动命令不对……这个脚本把它们一条条分开报，
而不是让人对着一个空白页面猜。

⚠️ 它会真的起一个服务进程（用随机端口，跑完自动关掉），所以需要
   MySQL 能连上——数据库本来就是服务启动的前置条件。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    CHECKS.append((name, ok, note))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {note}" if note else ""))
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reload", action="store_true",
                        help="用 --reload 模式启动（验自动重启会不会卡住）")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND))
    print("启动前检查：")

    cfg_path = ROOT / "config" / "config.yaml"
    if not check("配置文件存在", cfg_path.is_file(), str(cfg_path)):
        print(f"\n  复制一份再填数据库密码："
              f"\n    copy config\\config.example.yaml config\\config.yaml")
        return 2

    try:
        from app.core.config import load_config
        config = load_config(use_cache=False)
        check("配置能读出来", True, "")
    except Exception as exc:  # noqa: BLE001
        check("配置能读出来", False, str(exc))
        return 2

    # 数据库——服务启动的硬前置，连不上 lifespan 会直接失败
    try:
        import pymysql
        conn = pymysql.connect(
            host=config.get("mysql.host", "127.0.0.1"),
            port=int(config.get("mysql.port", 3306)),
            user=config.get("mysql.user", "root"),
            password=config.get("mysql.password", ""),
            connect_timeout=5,
        )
        conn.close()
        check("MySQL 能连上", True,
              f"{config.get('mysql.host')}:{config.get('mysql.port')}")
    except Exception as exc:  # noqa: BLE001
        check("MySQL 能连上", False, f"{exc}")
        print("\n  服务起不来最常见的就是这一条。检查 MySQL 是否已启动、"
              "config.yaml 的 mysql 段用户名密码对不对。")
        return 2

    dist = ROOT / "frontend" / "dist" / "index.html"
    check("前端已构建", dist.is_file(),
          str(dist) if dist.is_file()
          else "缺 frontend/dist —— 接口能用但页面是空的，"
               "去 frontend 目录跑 npm install && npm run build")

    entry = ROOT / "run.py"
    check("统一启动入口存在", entry.is_file(),
          "任何目录下 python run.py 都能起；"
          "直接敲 uvicorn 容易踩 --reload 监视 data/ 的坑")

    # ---------------- 真起一个服务 ----------------
    port = free_port()
    cmd = [sys.executable, str(entry), "--port", str(port)]
    if args.reload:
        cmd.append("--reload")
    print(f"\n启动服务（端口 {port}{'，--reload' if args.reload else ''}）……")
    env = dict(os.environ, SMC_SCHEDULER_ENABLED="false")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        import httpx
        started_at = time.time()
        alive = False
        while time.time() - started_at < args.timeout:
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", "ignore")
                check("服务启动", False, "进程直接退出了")
                print("\n---- 进程输出 ----\n" + out[-3000:])
                return 1
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    alive = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        took = time.time() - started_at
        if not check("服务启动", alive, f"{took:.1f} 秒"):
            print(f"\n  {args.timeout:.0f} 秒还没起来。"
                  f"{'--reload 模式下多半是监视器在遍历目录——' if args.reload else ''}"
                  f"先不带 --reload 跑一次看看报什么错。")
            return 1

        page = httpx.get(base + "/", timeout=10)
        check("首页能打开", page.status_code == 200 and len(page.text) > 100,
              f"HTTP {page.status_code}，{len(page.text)} 字节")
        for path, label in (("/api/tasks?page=1&page_size=1", "任务列表接口"),
                            ("/api/scenics/channels", "景区平台接口")):
            try:
                r = httpx.get(base + path, timeout=10)
                check(label, r.status_code == 200, f"HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001
                check(label, False, str(exc))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()

    ok = all(c[1] for c in CHECKS)
    print("\n全部通过 ✓" if ok else "\n有失败项，按上面的提示处理")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
