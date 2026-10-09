#!/usr/bin/env python3
"""环境自检：一次性确认 MySQL、Redis、代理、浏览器、目标站点是否都通。

用法：
    python scripts/selfcheck.py            # 全量检查（含联网访问目标站点）
    python scripts/selfcheck.py --quick    # 只查 MySQL / Redis，启动脚本用
    python scripts/selfcheck.py --sites    # 只查目标站点连通性

开发沙箱访问不到携程/同程等站点，所以站点连通性这一项必须在你的机器上跑。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

GREEN, RED, YELLOW, DIM, NC = "\033[0;32m", "\033[0;31m", "\033[0;33m", "\033[2m", "\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = f"{GREEN}[OK]{NC}" if ok else f"{RED}[NG]{NC}"
    print(f"  {mark} {name}" + (f"  {DIM}{detail}{NC}" if detail else ""))


async def check_mysql(config) -> None:
    from app.core.db import Database
    db = Database(config)
    try:
        await db.connect()
        version = await db.fetch_value("SELECT VERSION() AS v")
        tables = await db.fetch_all("SHOW TABLES")
        record("MySQL", True, f"{version}，{len(tables)} 张表")
        await db.close()
    except Exception as exc:  # noqa: BLE001
        record("MySQL", False, str(exc)[:200])


def check_redis(config) -> None:
    from app.core.redis_client import RedisClient
    client = RedisClient(config)
    if not config.get("redis.enabled"):
        record("Redis", True, "已在配置里关闭（代理与去重退化为进程内缓存）")
        return
    if client.ping() and client.is_real_redis:
        record("Redis", True, f"连接正常，key 前缀 {client.prefix}")
    else:
        record("Redis", False, "连不上，已退化为进程内缓存——多进程部署时代理状态不共享")


async def check_proxy(config) -> None:
    proxy = config.get("proxy", {})
    if not proxy.get("enabled"):
        record("快代理", True, "未启用（采集将直连）")
        return
    from app.core.redis_client import RedisClient
    from app.proxy.pool import KdlProxyPool
    pool = KdlProxyPool(
        RedisClient(config), scope="selfcheck",
        secret_id=proxy.get("secret_id", ""), secret_key=proxy.get("secret_key", ""),
        username=proxy.get("username", ""), password=proxy.get("password", ""),
        auth_mode=proxy.get("auth_mode", "token"), fetch_retries=1,
    )
    try:
        ip, ttl = await pool._fetch_valid_proxy()
        record("快代理", True, f"提取到 {ip}，有效期约 {ttl} 秒，连通性校验通过")
    except Exception as exc:  # noqa: BLE001
        record("快代理", False, str(exc)[:200])
    finally:
        RedisClient(config).delete(pool.current_key, pool.lock_key)


async def check_signers(config) -> None:
    """各平台签名能力：抖音要 Node + douyin.cjs，小红书要 xhshow。"""
    # 抖音 a_bogus
    try:
        from app.utils.jsvm import get_js_runtime, resolve_node_bin, shutdown_js_runtime

        node_bin = resolve_node_bin()
        runtime = get_js_runtime()
        signature = await runtime.call(
            "douyin.cjs", "sign_datail",
            ["aid=6383&keyword=test", "Mozilla/5.0 Chrome/131.0.0.0"],
        )
        await shutdown_js_runtime()
        record("抖音签名 a_bogus", len(signature) > 50,
               f"Node={node_bin}，签名长度 {len(signature)}")
    except Exception as exc:  # noqa: BLE001
        record("抖音签名 a_bogus", False,
               f"{str(exc)[:160]}  → 确认 backend/libs/douyin.cjs 存在")

    # 小红书 X-S
    try:
        from xhshow import Xhshow

        headers = Xhshow().sign_headers_get(
            uri="/api/sns/web/v2/comment/page", cookies="a1=test", params={"note_id": "x"},
        )
        record("小红书签名 X-S", bool(headers.get("x-s")),
               f"纯 Python 实现，产出 {len(headers)} 个签名头")
    except ImportError:
        record("小红书签名 X-S", False, "缺少依赖 → pip install xhshow")
    except Exception as exc:  # noqa: BLE001
        record("小红书签名 X-S", False, str(exc)[:160])

    # 快手签名依赖活页面，只能在真实采集时验证
    record("快手签名 __NS_hxfalcon", True,
           "依赖登录后的活页面环境，无法离线自检；首次采集时会自动校验")


async def check_browser(config) -> None:
    # 走和服务里完全一样的路径：Playwright 提交到专用事件循环执行。
    # 如果直接在自检脚本的循环上跑，Windows 下这里会通过、服务里却失败——
    # 因为 uvicorn 带 --reload 时会把服务器循环换成不支持子进程的那种。
    from app.core.subprocess_loop import SUBPROCESS_LOOP

    executable = config.get("browser.executable_path") or None

    async def _launch():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=executable,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            version = browser.version
            await browser.close()
            return version

    try:
        version = await SUBPROCESS_LOOP.run(_launch())
        record("Playwright 浏览器", True, f"Chromium {version}（经专用事件循环启动）")
    except Exception as exc:  # noqa: BLE001
        record(
            "Playwright 浏览器", False,
            f"{str(exc)[:150]}  → 执行 playwright install chromium",
        )


async def check_sites(config) -> None:
    """目标站点连通性。这一项只能在能上网的机器上跑。"""
    import httpx
    from app.proxy.fingerprint import build_headers, pick_profile

    targets = [
        ("携程 cid 接口", "https://m.ctrip.com/restapi/soa2/10290/createclientid"
                          "?systemcode=09&createtype=3&contentType=json", "mobile"),
        ("同程点评接口", "https://www.ly.com/scenery/AjaxHelper/DianPingAjax.aspx"
                          "?action=GetDianPingList&sid=32289&page=1&pageSize=10&labId=6&sort=0", "mobile"),
        ("抖音首页", "https://www.douyin.com", "desktop"),
        ("快手首页", "https://www.kuaishou.com", "desktop"),
        ("小红书首页", "https://www.xiaohongshu.com", "desktop"),
        ("微博 m 站", "https://m.weibo.cn", "mobile"),
    ]

    proxy_url = None
    proxy_config = config.get("proxy", {})
    if proxy_config.get("enabled"):
        from app.core.redis_client import RedisClient
        from app.proxy.pool import KdlProxyPool
        pool = KdlProxyPool(
            RedisClient(config), scope="selfcheck_sites",
            secret_id=proxy_config.get("secret_id", ""),
            secret_key=proxy_config.get("secret_key", ""),
            username=proxy_config.get("username", ""),
            password=proxy_config.get("password", ""),
            auth_mode=proxy_config.get("auth_mode", "token"),
        )
        try:
            proxy_url, _ = await pool.get_bundle()
            print(f"  {DIM}（经由快代理出口访问）{NC}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {YELLOW}代理不可用，改为直连测试：{exc}{NC}")

    for name, url, platform in targets:
        headers = build_headers(pick_profile(platform=platform))
        headers.pop("Content-Type", None)
        headers["Accept"] = "*/*"
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url, timeout=20, follow_redirects=True, headers=headers
            ) as client:
                response = await client.get(url)
            ok = response.status_code < 400
            record(name, ok, f"HTTP {response.status_code}，{len(response.content)} 字节")
        except Exception as exc:  # noqa: BLE001
            record(name, False, str(exc)[:150])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="只查 MySQL / Redis")
    parser.add_argument("--sites", action="store_true", help="只查目标站点连通性")
    args = parser.parse_args()

    from app.core.config import load_config
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}配置加载失败：{exc}{NC}")
        return 1

    print(f"\n配置文件：{config.source_path or '（未找到，使用内置默认值）'}\n")

    if args.sites:
        print("目标站点连通性：")
        await check_sites(config)
    elif args.quick:
        print("依赖服务：")
        await check_mysql(config)
        check_redis(config)
    else:
        print("依赖服务：")
        await check_mysql(config)
        check_redis(config)
        print("\n采集环境：")
        await check_browser(config)
        await check_signers(config)
        await check_proxy(config)
        print("\n目标站点连通性：")
        await check_sites(config)

    try:
        from app.core.subprocess_loop import SUBPROCESS_LOOP
        SUBPROCESS_LOOP.stop()
    except Exception:  # noqa: BLE001
        pass

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"{RED}未通过 {len(failed)} 项：{'、'.join(failed)}{NC}\n")
        return 1
    print(f"{GREEN}全部 {len(results)} 项检查通过{NC}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
