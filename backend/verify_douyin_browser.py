#!/usr/bin/env python3
"""抖音浏览器模拟采集 —— 独立验证脚本。

为什么需要它：浏览器模式目前只有"API 连续失败后自动降级"这一个入口，
没法主动触发。这个脚本绕过任务系统，直接把采集器跑起来，
每一步都打印结果，跑完给一份能看懂的判定。

在 backend 目录下跑（和 app/ 同级）：

    python verify_douyin_browser.py --keyword 盘山风景区

常用参数：
    --keyword    要搜的关键字（必填项，默认盘山风景区）
    --account    用哪个抖音账号，留空 = 自动挑一个可用的
    --works      最多采几条作品（默认 3，验证用不需要多）
    --comments   每条作品最多采几条评论（默认 20）
    --sort       general / latest / most_like（默认 latest，正好验证筛选）
    --within     unlimited / day / week / half_year（默认 half_year）
    --headless   无头跑（默认有头，验证时建议有头，能亲眼看到点了什么）

跑之前确认：
    1. MySQL 起着（脚本要读账号表）
    2. 账号管理里有一个已登录的抖音账号
    3. 那个账号的登录窗口没开着（profile 目录不能被两个 Chromium 同时占）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import pathlib
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.browser.manager import BrowserSessionManager
from app.collectors.base import CollectContext, LoginRequired
from app.collectors.douyin_browser import DouyinBrowserCollector
from app.core.config import load_config
from app.core.db import init_db
from app.core.logging import setup_logging
from app.core.redis_client import init_redis
from app.core.search_filters import SearchFilters
from app.proxy.manager import ProxyManager
from app.repositories.account_repo import AccountRepository

CHECKS: list[tuple[str, bool, str]] = []


def _t(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else (v or "-")


def print_work(idx: int, total: int, w) -> None:
    """一条作品的完整字段，按落库时的样子打印。"""
    print()
    print(f"  ┌─ 作品 {idx}/{total} " + "─" * 52)
    rows = [
        ("work_id", w.work_id),
        ("发布时间", _t(w.publish_time)),
        ("作者", f"{w.author_name}  ({w.author_id})"),
        ("标题/正文", (w.title or "").replace("\n", " ")[:60]),
        ("话题标签", w.label or "-"),
        ("互动", f"赞 {w.likes} / 评 {w.comment_cnt} / 藏 {w.collection_cnt} / 转 {w.shares}"),
        ("IP 属地", w.location or "-"),
        ("来源关键字", w.source_keyword or "-"),
        ("作品链接", w.work_url or "-"),
    ]
    for k, v in rows:
        print(f"  │ {k:<10} {v}")
    print("  ├" + "─" * 64)


def print_comment(no: int, c) -> None:
    """一条评论。二级评论缩进，一眼看出层级。"""
    lvl = getattr(c, "comment_level", "") or ""
    indent = "      " if lvl.endswith("1") else "          ↳ "
    content = (c.content or "").replace("\n", " ")[:52]
    print(f"  │{indent}[{no:>3}] {c.commenter_name}：{content}")
    print(f"  │{indent}      赞 {c.likes} · {_t(c.publish_time)} · {lvl or '-'}")


def check(name: str, ok: bool, note: str = "") -> None:
    CHECKS.append((name, ok, note))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {note}" if note else ""))


def skip(name: str, why: str) -> None:
    """这一步压根没跑到——不算通过，也不算失败。

    上一轮「关闭连播 ❌」就是假红：流程在采集作品时就崩了，
    根本没走到打开评论区那一步，判它失败会把注意力引向错的地方。
    """
    print(f"  ⏭️  {name}   未执行（{why}）")


class StepLogger:
    """把采集器里的 ctx.log 直接打到终端，并记下关键步骤有没有发生。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, msg: str) -> None:
        self.lines.append(str(msg))
        print(f"    {msg}")

    info = warning = warn = error = debug = _record

    def saw(self, *keys: str) -> bool:
        """某一行里同时出现了这些关键词？"""
        return any(all(k in line for k in keys) for line in self.lines)



def dy_src_early() -> str:
    """读一遍采集器源码——自检里有几条只能靠"文件里有没有这行"来判断。"""
    import inspect
    return pathlib.Path(
        inspect.getfile(DouyinBrowserCollector)).read_text(encoding="utf-8")


def preflight() -> bool:
    """代码版本自检：确认跑的是新版采集器，而不是旧文件。

    为什么需要这个：新文件没覆盖到位时，脚本照样能跑起来，
    只是走的是旧流程——症状是「打开搜索页 .../search/xxx」然后卡住，
    看着像新代码有 bug，其实压根没加载到。这里先把话说死。
    """
    import inspect
    from app.collectors import browser_capture as bc

    print("\n实际加载的文件：")
    print(f"  douyin_browser.py  {inspect.getfile(DouyinBrowserCollector)}")
    print(f"  browser_capture.py {inspect.getfile(bc)}")
    print("\n代码版本自检：")

    stub = DouyinBrowserCollector.__new__(DouyinBrowserCollector)
    stub._current_keyword = ""

    patterns = DouyinBrowserCollector._target_url_patterns.fget(stub)
    check("拦截 5 个接口（含 search/stream/）",
          any("search/stream" in p["url_contains"] for p in patterns),
          f"当前 {len(patterns)} 个；旧版是 4 个")

    tpl = DouyinBrowserCollector._search_url_template.fget(stub)
    check("搜索走 /jingxuan/search/", "/jingxuan/search/" in tpl, tpl)

    check("有 _disable_autoplay（关联播）",
          hasattr(DouyinBrowserCollector, "_disable_autoplay"))
    check("有 _goto_search / _prepare_search 钩子",
          hasattr(bc.BrowserCaptureCollector, "_goto_search")
          and hasattr(bc.BrowserCaptureCollector, "_prepare_search"))

    check("Playwright 调用走浏览器循环（_run）",
          hasattr(bc.BrowserCaptureCollector, "_run"),
          "少了它，每次 page.goto 都会永久挂起——日志停在某行不动")

    # 字段映射是"借用" DouyinCollector 的实现，它在 self 上用到的每个属性
    # 这边都必须有。缺了不会在导入时报错，要等真跑到映射那一步才炸
    # （_first_url、host 就是这么漏的），所以在这里先静态查一遍。
    import re
    from app.collectors.douyin import DouyinCollector
    lacking = []
    for name in ("_to_work", "_to_comment"):
        body = inspect.getsource(getattr(DouyinCollector, name))
        for attr in sorted(set(re.findall(r"self\.(\w+)", body))):
            if not hasattr(DouyinBrowserCollector, attr):
                lacking.append(f"{name} 用到 self.{attr}")
    check("字段映射依赖的属性齐全", not lacking, "、".join(lacking) or "_first_url / host 等")

    # expect_response / expect_navigation 这类上下文管理器要在"页面所属的循环"
    # 上等结果，跨循环用会永久挂起，连 timeout 都不触发。整个采集器都不该出现。
    dy_src = dy_src_early()
    bad = [ln for ln in dy_src.splitlines()
           if "expect_response" in ln and not ln.strip().startswith("#")]
    check("没有跨循环的 expect_response", not bad,
          "、".join(b.strip()[:50] for b in bad) or "会卡死在点击之后")

    # 这两条对应用户实测出来的两个顺序 bug，都是"跑的是旧文件"时最容易复现的
    check("筛选确认看响应落地时刻（不是数序号）",
          hasattr(DouyinBrowserCollector, "_wait_search_quiet")
          and "_last_search_resp_at" in dy_src_early(),
          "旧版用序号，会被上一次点击的迟到响应顶替，筛选链错开一拍")

    check("同一次搜索的续页只追加（_should_replace）",
          hasattr(DouyinBrowserCollector, "_should_replace"),
          "旧版搜索阶段一律替换，会被同一次搜索的第二页把最新的一批抹掉")

    check("打开作品靠点卡片（不跳 URL）",
          hasattr(DouyinBrowserCollector, "_goto_work")
          and hasattr(bc.BrowserCaptureCollector, "_goto_work"),
          "跳 URL 会重载页面，把筛选状态冲掉，modal 也打不开")

    src = pathlib.Path(inspect.getfile(bc)).read_text(encoding="utf-8")
    # 只查真正的调用参数，别把解释用的注释也算进去
    check("已去掉 networkidle 等待", 'wait_until="networkidle"' not in src,
          "networkidle 在抖音上永远不成立，goto 会卡到超时")

    if all(c[1] for c in CHECKS):
        return True
    print("\n" + "!" * 66)
    print("自检没过 —— 说明新文件没覆盖到上面打印的那两个路径。")
    print("把 douyin_browser.py 和 browser_capture.py 复制过去，")
    print("再删掉同目录下的 __pycache__ 文件夹，然后重跑。")
    print("!" * 66 + "\n")
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="盘山风景区")
    ap.add_argument("--account", default="")
    # 默认值取自 config.yaml 的 crawl 段（作品 100 / 每条作品评论 500），
    # 传 0 或不传就用配置里的值，和真实任务跑起来完全一致
    ap.add_argument("--works", type=int, default=0,
                    help="作品上限，默认取 crawl.default_max_works")
    ap.add_argument("--comments", type=int, default=0,
                    help="每条作品评论上限，默认取 crawl.default_max_comments_per_work")
    ap.add_argument("--sort", default="latest",
                    choices=["general", "latest", "most_like"])
    ap.add_argument("--within", default="half_year",
                    choices=["unlimited", "day", "week", "half_year"])
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if not preflight():
        return 2
    CHECKS.clear()

    config = load_config()
    if not args.works:
        args.works = int(config.get("crawl.default_max_works", 100))
    if not args.comments:
        args.comments = int(config.get("crawl.default_max_comments_per_work", 500))
    setup_logging("INFO", Path(config.get("server.data_dir", "./data")) / "logs")
    config.ensure_dirs()

    # 验证时默认有头：这套流程的价值就在于"能亲眼看到它点了哪里"
    config.set("browser.headless", bool(args.headless))
    print(f"\n浏览器模式：{'无头' if args.headless else '有头（窗口会弹出来）'}")

    db = init_db(config)
    await db.connect()
    redis = init_redis(config)
    account_repo = AccountRepository(db)
    proxy_manager = ProxyManager(config, redis)
    browser_manager = BrowserSessionManager(config, account_repo)

    log = StepLogger()
    ctx = CollectContext(
        scenic_id="VERIFY", scenic_name="浏览器模式验证",
        task_id=f"verify-{int(time.time())}",
        max_works=args.works, max_comments_per_work=args.comments,
        account_name=args.account,
        filters=SearchFilters(channel="douyin", sort=args.sort,
                              publish_within=args.within),
        params={"browser_manager": browser_manager},
        logger=log,
    )

    collector = DouyinBrowserCollector(config, proxy_manager)
    works: list = []
    comments_total = 0
    reached_comments = False

    print(f"\n{'='*78}\n关键字：{args.keyword}   排序：{args.sort}   时间：{args.within}"
          f"\n作品上限：{args.works} 条   每条作品评论上限：{args.comments} 条"
          f"\n{'='*78}\n")
    try:
        print("[1/4] 启动浏览器、注入 network 拦截 ……")
        await collector.prepare(ctx)
        check("浏览器启动 + 拦截就绪", True, f"账号 {collector._session.account_name}")

        print("\n[2/4] 搜索（模拟输入 → 切多列 → 点筛选 → 收集 → 按发布时间倒序）……")
        async for work in collector.collect_by_keyword(ctx, args.keyword):
            works.append(work)
        check("采到作品", bool(works), f"{len(works)} 条")

        print(f"\n[3/4] 按倒序逐条点开、采评论（关联播 → 开评论区 → 滚到底 + 展开回复）")
        for idx, w in enumerate(works, 1):
            reached_comments = True
            print_work(idx, len(works), w)
            got = 0
            async for cm in collector.collect_comments(ctx, w):
                got += 1
                print_comment(got, cm)
            comments_total += got
            print(f"      └─ 小计 {got} 条评论"
                  + ("（这条作品没有评论）" if got == 0 else ""))
        check("采到评论", comments_total > 0, f"共 {comments_total} 条")

    except LoginRequired as exc:
        check("登录态", False, str(exc))
    except Exception as exc:  # noqa: BLE001
        print("\n出错了：")
        traceback.print_exc()
        check("流程跑完", False, f"{type(exc).__name__}: {exc}")
    finally:
        print("\n[4/4] 收尾 ……")
        try:
            await collector.cleanup()
        except Exception:  # noqa: BLE001
            pass
        await db.close()

    # ---------------- 分步判定 ----------------
    # 每一步都有专属日志，靠日志判定比靠"最后有没有数据"精确得多：
    # 采不到数据可能是任何一步坏了，逐条看才知道是哪一步。
    print(f"\n{'='*66}\n分步判定\n{'='*66}")
    check("模拟真人输入关键字", log.saw("已输入关键字并点击搜索"),
          "没命中说明退回了直开 URL，看上面有没有「模拟输入失败」")
    check("切换到多列模式", log.saw("已切换到多列模式"),
          "没切成会走 stream/ 接口（有兜底解析，数据不会丢，只是分页方式不同）")

    if args.sort != "general" or args.within != "unlimited":
        if log.saw("筛选面板已展开"):
            check("筛选生效", log.saw("已选中筛选项"),
                  "面板开了但选项没点中——看「筛选未生效」那行说的是哪个分组")
        else:
            check("筛选面板展开", False,
                  "点了「筛选」但面板没出来，重跑 probe_page.py 拍一张筛选面板")

    if log.saw("筛选后拦到"):
        check("只采筛选后的结果", True, "筛选前的已在点击时丢弃")
    elif log.saw("没拦到筛选后的搜索结果"):
        check("只采筛选后的结果", False,
              "筛选生效但没拿到新结果——页面可能用了缓存，没重新请求")

    if reached_comments:
        check("点开作品卡片", log.saw("已点开作品"),
              "看上面是「找不到卡片」还是「点开的是别的作品」")
        # ⚠️ 「本来就是关的」也是成功——上一轮这里判成 ❌ 是我写错了：
        # 只认「已切换」，把"发现已经是关的、不用动"当成了失败。
        # 目标是"连播处于关闭状态"，不是"我必须点一下"。
        autoplay_ok = (log.saw("已关闭连播") or log.saw("已切换连播开关")
                       or log.saw("连播本来就是关的"))
        check("连播已关闭", autoplay_ok,
              "没关成会自动跳下一条，评论可能挂到错误的作品上")
    else:
        skip("关闭连播", "没走到打开作品那一步")

    ok = all(c[1] for c in CHECKS)
    print(f"\n{'='*66}")
    print("结论：" + ("全部通过 ✅" if ok else
                    "有失败项 ❌ —— 把上面整段输出发给我，我按行定位"))
    print(f"{'='*66}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
