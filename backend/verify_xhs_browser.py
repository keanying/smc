#!/usr/bin/env python3
"""小红书浏览器模拟采集 —— 独立验证脚本。

和 verify_douyin_browser.py 是同一套：绕过任务系统，直接把采集器跑起来，
每一步打印结果，跑完给一份能看懂的分步判定。

在 backend 目录下跑（和 app/ 同级）：

    python verify_xhs_browser.py --keyword 盘山风景区

常用参数：
    --keyword    要搜的关键字（默认 盘山风景区）
    --account    用哪个小红书账号，留空 = 自动挑一个可用的
    --works      最多采几条笔记（不填 = 用 config 里的 crawl.default_max_works）
    --comments   每条笔记最多采几条评论（不填 = 用 config 里的默认值）
    --sort       general / latest / most_like（默认 latest）
    --within     unlimited / day / week / half_year（默认 half_year）
                 ⚠️ 默认**不再是** unlimited。unlimited 的含义是"任务没设
                    时间窗"，此时「一天内 / 一周内 / 半年内」本来就不会去点，
                    看日志的人容易误以为"时间那一项点失败了"。
                    给个真实的时间窗，这一步才真的被验到。
    --headless   无头跑（默认有头，验证时建议有头，能亲眼看到点了什么）

跑之前确认：
    1. MySQL 起着（脚本要读账号表）
    2. 账号管理里有一个已登录的小红书账号
    3. 那个账号的登录窗口没开着（profile 目录不能被两个 Chromium 同时占）

⚠️ 关于「筛选」这一步

筛选**入口**（div.filter）已经按快照写准了，但**面板里的选项文案**没拍到——
那个面板一失焦就收起，快照里只剩"入口从「筛选」变成「已筛选」"的痕迹。
所以采集器会把面板里实际有哪些选项打进日志（「筛选面板里的选项：…」那一行），
这一步万一红了，照着那行改 xhs_browser.py 的 SORT_CANDIDATES /
TIME_CANDIDATES 就行，不用再拍一次。
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
from app.collectors.xhs_browser import XhsBrowserCollector
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
    """一条笔记的完整字段，按落库时的样子打印。"""
    print()
    print(f"  ┌─ 笔记 {idx}/{total} " + "─" * 52)
    rows = [
        ("work_id", w.work_id),
        ("发布时间", _t(w.publish_time)),
        ("作者", f"{w.author_name}  ({w.author_id})"),
        ("标题/正文", (w.title or "").replace("\n", " ")[:60]),
        ("话题标签", w.label or "-"),
        ("互动", f"赞 {w.likes} / 评 {w.comment_cnt} / 藏 {w.collection_cnt} / 转 {w.shares}"),
        ("IP 属地", w.location or "-"),
        ("来源关键字", w.source_keyword or "-"),
        ("笔记链接", w.work_url or "-"),
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

    流程在前面就崩了、压根没走到这一步的话，判它失败会把注意力引向错的地方。
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



def xhs_src() -> str:
    """读一遍采集器源码——有几条只能靠"文件里有没有这行"来判断。"""
    import inspect
    return pathlib.Path(
        inspect.getfile(XhsBrowserCollector)).read_text(encoding="utf-8")


def preflight() -> bool:
    """代码版本自检：确认跑的是新版采集器，而不是旧文件。

    小红书这边尤其要紧——旧的那份 xhs_browser.py **从来没有真正跑起来过**
    （`_to_work` 的参数顺序就是错的，一跑就 TypeError），
    所以"能导入"完全不代表"是能用的那一版"。
    """
    import inspect
    import re
    from app.collectors import browser_capture as bc
    from app.collectors.xhs import XhsCollector

    print("\n实际加载的文件：")
    print(f"  xhs_browser.py     {inspect.getfile(XhsBrowserCollector)}")
    print(f"  browser_capture.py {inspect.getfile(bc)}")
    print("\n代码版本自检：")

    stub = XhsBrowserCollector.__new__(XhsBrowserCollector)

    patterns = XhsBrowserCollector._target_url_patterns.fget(stub)
    order_ok = [p["url_contains"] for p in patterns][:2] == [
        "/api/sns/web/v2/comment/sub/page", "/api/sns/web/v2/comment/page"]
    check("拦截顺序：sub/page 在 comment/page 之前", order_ok,
          "反了的话所有子评论都会被当成一级评论——两个 URL 有包含关系")

    check("已接入（integrated=True）", XhsBrowserCollector.integrated,
          "False 的话调度层会直接跳过这个平台")

    check("Playwright 调用走浏览器循环（_run）",
          hasattr(bc.BrowserCaptureCollector, "_run"),
          "少了它，每次 page.goto 都会永久挂起——日志停在某行不动")

    src = xhs_src()
    bad_loop = [ln.strip() for ln in src.splitlines()
                if re.search(r"await\s+self\._page\.", ln)
                and not ln.strip().startswith("#")]
    check("没有绕过 _run 直接 await self._page", not bad_loop,
          "；".join(b[:60] for b in bad_loop) or "跨循环 await 会永久挂起")

    bad_expect = [ln for ln in src.splitlines()
                  if "expect_response" in ln and not ln.strip().startswith("#")]
    check("没有跨循环的 expect_response", not bad_expect,
          "、".join(b.strip()[:50] for b in bad_expect) or "会卡死在点击之后")

    # 字段映射是"借用" XhsCollector 的实现，它在 self 上用到的每个属性
    # 这边都必须有。缺了不会在导入时报错，要等真跑到映射那一步才炸
    # （web_host、_video_urls 就是这么漏的），所以先静态查一遍。
    lacking = []
    for name in ("_to_work", "_to_comment"):
        body = inspect.getsource(getattr(XhsCollector, name))
        for attr in sorted(set(re.findall(r"self\.(\w+)", body))):
            if not hasattr(XhsBrowserCollector, attr):
                lacking.append(f"{name} 用到 self.{attr}")
    check("字段映射依赖的属性齐全", not lacking,
          "、".join(lacking) or "web_host / _video_urls 等")

    # 参数顺序对不上是旧文件最典型的症状：搜索、拦截全跑完，
    # 到映射那一步才 TypeError，前面的功夫全白费
    sig_api = list(inspect.signature(XhsCollector._to_work).parameters)
    sig_bro = list(inspect.signature(XhsBrowserCollector._to_work).parameters)
    check("_to_work 的参数顺序和接口版一致", sig_api[:5] == sig_bro[:5],
          f"接口版 {sig_api[:5]}  /  拟人版 {sig_bro[:5]}")

    check("打开笔记会带 xsec_token", "xsec_token=" in src,
          "不带 token 的笔记页打得开，但一条评论都不请求")

    check("有「笔记不可浏览」的识别", hasattr(XhsBrowserCollector, "_looks_unavailable"),
          "这种情况服务端回的是 200，只看状态码分辨不出来")

    # ↓ 以下四条来自 2026-09-03 的真实录制，都是"能导入但跑不出东西"的那类
    from app.collectors import xhs_browser as xb
    check("认得 AI 版首页的搜索框",
          "search-input-in-feeds" in " ".join(xb.SEARCH_INPUT_SELECTORS),
          "这个账号的首页没有 #search-input，只认经典版的话"
          "每次都会静悄悄退回直开 URL")

    dupes = [n for n in ("_click_option", "_scroll_comments", "_element_box")
             if len(re.findall(rf"\n    async def {n}\(", src)) > 1]
    check("没有重名方法互相覆盖", not dupes,
          "、".join(dupes) or "后定义的会悄悄盖掉前一个，文件读起来是对的、跑的是另一份")

    check("滚列表之前会先把鼠标挪到列表上",
          hasattr(XhsBrowserCollector, "_feed_point")
          and "_feed_point" in src.split("_scroll_search")[0],
          "滚轮滚的是指针底下那个元素；关完弹窗指针停在左上角关闭按钮那儿，"
          "直接滚会滚到边栏上，表现是「列表里没有新卡片了」")

    check("滚评论之前会先把鼠标挪进评论栏",
          hasattr(XhsBrowserCollector, "_park_over_comments"),
          "弹窗左边是图片区，滚轮落在那儿翻的是图，评论会永远停在第一页")

    check("打开笔记后会往下滚到评论区",
          hasattr(XhsBrowserCollector, "_comments_in_view"),
          "弹窗右栏一开停在正文顶部；不滚下去，「展开 N 条回复」全在视口外，"
          "一个都点不到")

    check("列表里的非笔记卡片会被跳过", "query-note-wrapper" in src or
          "openable" in src,
          "「大家都在搜」推荐位同样是 section.note-item[data-note-id]，"
          "点了不会有弹窗")

    check("「0 条评论」分得清是真没有还是 runner 没来采",
          "_comments_attempted_for" in src,
          "两种情况原因天差地别，只打「0 条」看不出是哪一种")

    check("两条笔记之间有 1~8 秒的动态停顿",
          getattr(XhsBrowserCollector, "PACE_RANGE", None) == (1.0, 8.0)
          and "_pace_bag" in src,
          "匀速点是最扎眼的机器特征")

    check("搜索卡片的发布时间（corner_tag_info）有兜底",
          hasattr(__import__("app.collectors.xhs", fromlist=["x"]).XhsCollector,
                  "_corner_publish_time"),
          "卡片里没有 time 字段，「2天前」「08-28」这种角标才是发布时间")

    from app.scheduler import runner as _runner
    # ⚠️ 取的是 _collect_one_keyword（真正逐条产出作品的那个），
    # 不是外层按关键字循环的 _collect_keyword
    _kw_src = inspect.getsource(_runner.TaskRunner._collect_one_keyword)
    check("采一条存一条：每条作品采完立刻落库",
          _kw_src.count("await buffer.flush()") >= 2,
          "攒批的话，拟人模式一个多小时的采集会在任务被停/进程被杀时全丢，"
          "而日志里明明一条条都采到了")

    check("写库时会报平台 / 表名 / 作品 id",
          "tables.WORKS" in inspect.getsource(_runner._Buffer.flush),
          "不然「到底入库没有」只能去翻数据库")

    check("作品和评论的明细日志全平台统一",
          hasattr(_runner, "log_work_detail")
          and hasattr(_runner, "log_comment_detail"),
          "写在各个采集器里的话，抖音和小红书的日志长得不一样，"
          "排查得用两套方法")

    check("没等到 feed 的警告只在真的没等到时打",
          "self._feed_source = \"搜索卡片\"" in src,
          "写错分支的话，feed 成功到货时反而会喊「只能用残缺字段」")

    ok = all(c[1] for c in CHECKS)
    if not ok:
        print("\n⚠️  上面有失败项，说明加载的不是预期的那一版文件，"
              "后面的实跑结果参考价值有限")
    return ok

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="盘山风景区")
    ap.add_argument("--account", default="")
    # 默认值取自 config.yaml 的 crawl 段（笔记 100 / 每条笔记评论 500），
    # 传 0 或不传就用配置里的值，和真实任务跑起来完全一致
    ap.add_argument("--works", type=int, default=0,
                    help="笔记上限，默认取 crawl.default_max_works")
    ap.add_argument("--comments", type=int, default=0,
                    help="每条笔记评论上限，默认取 crawl.default_max_comments_per_work")
    ap.add_argument("--sort", default="latest",
                    choices=["general", "latest", "most_like"])
    ap.add_argument("--within", default="half_year",
                    choices=["unlimited", "day", "week", "half_year"],
                    help="unlimited 表示任务没设时间窗，那一项本来就不会点")
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
        filters=SearchFilters(channel="xiaohongshu", sort=args.sort,
                              publish_within=args.within),
        params={"browser_manager": browser_manager},
        logger=log,
    )

    collector = XhsBrowserCollector(config, proxy_manager)
    works: list = []
    comments_total = 0
    reached_comments = False

    print(f"\n{'='*78}\n关键字：{args.keyword}   排序：{args.sort}   时间：{args.within}"
          f"\n笔记上限：{args.works} 条   每条笔记评论上限：{args.comments} 条"
          f"\n{'='*78}\n")
    try:
        print("[1/3] 启动浏览器、注入 network 拦截 ……")
        await collector.prepare(ctx)
        check("浏览器启动 + 拦截就绪", True, f"账号 {collector._session.account_name}")

        print("\n[2/3] 边滚边采：产出一条笔记 → 立刻采它的评论 → 关弹窗 → 下一条")
        # ⚠️ **必须交错**，不能"先把笔记全采完再回头采评论"。
        # 上一版就是两段式，后果有两条，都很致命：
        #   1. 搜索阶段每条笔记都会打「这条没有进入评论采集」——因为那时候
        #      确实没人来采，日志看起来像是全被过滤掉了；
        #   2. 等搜索阶段跑完再回头开笔记，**卡片早被虚拟列表回收了**
        #      （录制实测 DOM 里只留 29 张的滚动窗口），于是条条点不开。
        # runner 就是交错跑的，这个脚本必须和它同构，否则验了个寂寞。
        reached_comments = False
        for_idx = 0
        async for work in collector.collect_by_keyword(ctx, args.keyword):
            works.append(work)
            for_idx += 1
            print_work(for_idx, ctx.max_works, work)
            reached_comments = True
            got = 0
            async for cm in collector.collect_comments(ctx, work):
                got += 1
                print_comment(got, cm)
            comments_total += got
            print(f"      └─ 小计 {got} 条评论"
                  + ("（这条笔记没有评论）" if got == 0 else ""))
        check("采到笔记", bool(works), f"{len(works)} 条")
        check("采到评论", comments_total > 0, f"共 {comments_total} 条")

    except LoginRequired as exc:
        check("登录态", False, str(exc))
    except Exception as exc:  # noqa: BLE001
        print("\n出错了：")
        traceback.print_exc()
        check("流程跑完", False, f"{type(exc).__name__}: {exc}")
    finally:
        print("\n[3/3] 收尾 ……")
        try:
            await collector.cleanup()
        except Exception:  # noqa: BLE001
            pass
        await db.close()

    # ---------------- 分步判定 ----------------
    # 每一步都有专属日志，靠日志判定比靠"最后有没有数据"精确得多：
    # 采不到数据可能是任何一步坏了，逐条看才知道是哪一步。
    print("\n" + "=" * 66 + "\n分步判定\n" + "=" * 66)

    # 排序和时间是**两项**，要分开判：用户那次实跑里排序点中了、时间
    # 压根没点（任务里填的是 unlimited），只看"有没有点中过"会误判成全绿。
    WANT = {"latest": "最新", "most_like": "最多点赞",
            "day": "一天内", "week": "一周内", "half_year": "半年内"}
    for group, value in (("排序", args.sort), ("发布时间", args.within)):
        if value in ("general", "unlimited"):
            check(f"{group}：任务没设（{value}），本来就不点", True,
                  "日志里应该有一句说明，不该让人以为是点失败了")
            continue
        want = WANT.get(value, value)
        check(f"{group}：点中了「{want}」", log.saw(f"已选中筛选项「{want}」"),
              f"面板里有这个词但没点中的话，看「筛选面板里的选项」那一行"
              f"照着改 {'SORT' if group == '排序' else 'TIME'}_CANDIDATES")

    if args.sort != "general" or args.within != "unlimited":
        if log.saw("已选中筛选项"):
            check("筛选生效", True, "")
        else:
            # 筛选入口 div.filter 已经有快照了，但**面板里的选项文案**没拍到
            # （面板一失焦就收起）。所以采集器会把面板里实际有什么打进日志，
            # 失败时照着那行改 SORT_CANDIDATES / TIME_CANDIDATES 即可。
            opts = [ln for ln in log.lines if "筛选面板里的选项" in ln]
            check("筛选生效", False,
                  (opts[-1] if opts else
                   "连面板都没展开——看上面有没有「没找到筛选入口 div.filter」"))

    check("搜到笔记", bool(works), f"{len(works)} 条")
    if works:
        with_token = sum(1 for w in works if collector._tokens.get(w.work_id))
        check("拿到 xsec_token", with_token > 0,
              f"{with_token}/{len(works)} 条有 token；"
              f"没有 token 的笔记页不会加载评论，会被跳过")

    if reached_comments:
        check("打开笔记页", not log.saw("打开笔记页失败"), "看上面是超时还是被拦")
        blocked = [ln for ln in log.lines if "打不开（页面提示不可浏览" in ln]
        if blocked:
            check("笔记可浏览", False,
                  f"{len(blocked)} 条提示不可浏览——通常是 token 过期")
        check("采到评论", comments_total > 0, f"共 {comments_total} 条")
    else:
        skip("打开笔记页", "没走到采评论那一步")
        skip("采到评论", "没走到采评论那一步")

    ok = all(c[1] for c in CHECKS)
    print(f"\n{'='*66}")
    print("结论：" + ("全部通过 ✅" if ok else
                    "有失败项 ❌ —— 把上面整段输出发给我，我按行定位"))
    print(f"{'='*66}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
