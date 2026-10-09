#!/usr/bin/env python3
"""快手浏览器模拟采集 —— 独立验证脚本。

和 verify_xhs_browser.py 是同一套：绕过任务系统，直接把采集器跑起来，
每一步打印结果，跑完给一份能看懂的分步判定。

在 backend 目录下跑（和 app/ 同级）：

    python verify_kuaishou_browser.py --keyword 盘山风景区

常用参数：
    --keyword    要搜的关键字（默认 盘山风景区）
    --account    用哪个快手账号，留空 = 自动挑一个可用的
    --works      最多采几条作品（不填 = 用 config 里的 crawl.default_max_works）
    --comments   每条作品最多采几条评论（不填 = 用 config 里的默认值）
    --headless   无头跑（默认有头，验证时建议有头，能亲眼看到点了什么）

跑之前确认：
    1. MySQL 起着（脚本要读账号表）
    2. 账号管理里有一个已登录的快手账号
    3. 那个账号的登录窗口没开着（profile 目录不能被两个 Chromium 同时占）

────────────────────────────────────────────────────────────
⚠️ 这个脚本现在还有一件**未确认**的事，而且它是成败关键
────────────────────────────────────────────────────────────
DOM 那一层（点作品 / 关联播 / 开评论 / 展开回复 / 关视频）全部来自
2026-09-05 的真实录制，已经写死并有离线自检兜着
（`python verify_kuaishou_offline.py`）。

但**接口路径还没有被真实录制确认过**：

    /rest/v/search/feed
    /rest/v/photo/comment/list
    /rest/v/photo/comment/sublist

它们来自项目里更早的代码，两次录制的 network.jsonl 都是空的
（录制器把快手接口前缀写成了 /graphql，现已修正但还没拿到新录制）。
**拦不到接口 = 作品和评论一条都出不来。**

所以这个脚本会把**实际拦到了什么**打出来：跑完看「拦截统计」那一节，
如果三个接口都是 0 次，就把那一节的输出发回来，照着真实路径改
`kuaishou_browser._target_url_patterns` 即可。
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
from app.collectors.kuaishou_browser import KuaishouBrowserCollector
from app.core.config import load_config
from app.core.db import init_db
from app.core.logging import setup_logging
from app.core.redis_client import init_redis
from app.core.search_filters import SearchFilters
from app.proxy.manager import ProxyManager

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




def ks_src() -> str:
    import inspect
    from app.collectors import kuaishou_browser as kb
    return inspect.getsource(kb)


def preflight() -> bool:
    """代码版本自检：确认跑的是新版采集器，而不是旧文件。

    快手这边尤其要紧——旧的那份 kuaishou_browser.py 里，
    DOM 选择器全是 `[class*="comment"]` 这种通配猜测，
    而且**借来的 _to_work / _to_comment 参数对不上**（一调用就 TypeError）。
    "能导入"完全不代表"是能用的那一版"。
    """
    import inspect
    import re
    from app.collectors import browser_capture as bc
    from app.collectors.kuaishou import KuaishouCollector

    print("\n实际加载的文件：")
    print(f"  kuaishou_browser.py {inspect.getfile(KuaishouBrowserCollector)}")
    print(f"  browser_capture.py  {inspect.getfile(bc)}")
    print("\n代码版本自检：")

    stub = KuaishouBrowserCollector.__new__(KuaishouBrowserCollector)
    src = ks_src()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))

    patterns = [p["url_contains"] for p in
                KuaishouBrowserCollector._target_url_patterns.fget(stub)]
    check("拦截顺序：sublist 在 comment/list 之前",
          patterns[:2] == ["/rest/v/photo/comment/sublist",
                           "/rest/v/photo/comment/list"],
          "反了的话所有子评论都会被当成一级评论——两个 URL 有包含关系")

    check("Playwright 调用走浏览器循环（_run）",
          hasattr(bc.BrowserCaptureCollector, "_run"),
          "少了它，每次 page.goto 都会永久挂起——日志停在某行不动")

    bad_loop = [ln.strip() for ln in src.splitlines()
                if re.search(r"await\s+self\._page\.", ln)
                and not ln.strip().startswith("#")]
    check("没有绕过 _run 直接 await self._page", not bad_loop,
          "；".join(b[:60] for b in bad_loop) or "跨循环 await 会永久挂起")

    # ---- 借函数就得把签名对齐：这两条各自抓到过一个真 bug ----
    sig_api_w = list(inspect.signature(KuaishouCollector._to_work).parameters)
    sig_bro_w = list(inspect.signature(KuaishouBrowserCollector._to_work).parameters)
    check("_to_work 的参数和接口版一致", sig_api_w[:3] == sig_bro_w[:3],
          f"接口版 {sig_api_w[:4]}  /  拟人版 {sig_bro_w[:4]}")

    # 注意：这里必须**按位置**比，不能按名字比。
    # 接口版第 4 个形参叫 photo_id、拟人版叫 work_id，名字不同但位置一致，
    # 调用方一律用位置参数传，所以这是等价的；按名字比会误报。
    sig_api_c = list(inspect.signature(KuaishouCollector._to_comment).parameters.values())
    sig_bro_c = list(inspect.signature(KuaishouBrowserCollector._to_comment).parameters.values())
    # 拟人版给 depth/parent_id 加默认值是可以的（只影响它自己怎么被调），
    # 会炸的是个数或种类对不上、以及透传的关键字在接口版里根本不存在。
    same_shape = (
        len(sig_api_c) == len(sig_bro_c)
        and all(a.kind == b.kind for a, b in zip(sig_api_c, sig_bro_c))
    )
    check("_to_comment 的参数位置和接口版对齐", same_shape,
          (f"接口版 {[p.name for p in sig_api_c]}  /  "
           f"拟人版 {[p.name for p in sig_bro_c]}"
           + ("" if same_shape else
              " —— 个数或种类对不上，透传会 TypeError，且要跑到映射那一步才炸"))
          + ("（第 4 个形参名字不同但位置一致，按位置传，等价）" if same_shape else ""))

    # 透传出去的关键字必须在接口版里真的存在（原来多传了个 what，就是这么炸的）
    api_names = {p.name for p in sig_api_c}
    fwd = inspect.getsource(KuaishouBrowserCollector._to_comment)
    fwd = fwd[fwd.index("KuaishouCollector._to_comment("):] if "KuaishouCollector._to_comment(" in fwd else ""
    bogus = sorted(set(re.findall(r"(\w+)\s*=", fwd)) - api_names)
    check("_to_comment 透传的关键字接口版都认", not bogus,
          f"接口版没有 {bogus} 这些参数，一调用就 TypeError" if bogus else
          "多传一个不存在的关键字就会 TypeError，且搜索/点开/翻评论全做完才暴露")

    lacking = []
    for name in ("_to_work", "_to_comment"):
        body = inspect.getsource(getattr(KuaishouCollector, name))
        for attr in sorted(set(re.findall(r"self\.(\w+)", body))):
            if not hasattr(KuaishouBrowserCollector, attr):
                lacking.append(f"{name} 用到 self.{attr}")
    check("字段映射依赖的属性齐全", not lacking,
          "、".join(lacking) or "借函数就得把它用到的东西一起借齐")

    # ---- 选择器必须来自实测，不能是通配猜测 ----
    check("选择器不是 [class*=...] 通配猜测", "[class*=" not in code,
          "通配选择器实跑必错，而且失败时看不出原因")
    for label, name, must in (
        ("点作品：封面图", "CARD_COVER_SELECTOR", "img.cover-img"),
        ("关联播：开关", "AUTOPLAY_SWITCH_SELECTOR", "autoPlay"),
        ("打开评论：右侧按钮", "COMMENT_BUTTON_SELECTOR", "commentPanel"),
        ("展开回复", "EXPAND_REPLY_SELECTOR", "span.expand"),
        ("关掉视频", "CLOSE_VIDEO_SELECTOR", "circle-btn"),
    ):
        value = getattr(KuaishouBrowserCollector, name, "")
        check(f"{label}（{name}）", must in value, value or "（没有这个常量）")

    check("到达作品是**点卡片**不是跳 URL",
          "_goto_work" in code and "swiper" in code,
          "录制实测：全程 URL 不变，视频是同页覆盖层。"
          "跳 /short-video/ 会整页重载，把搜索结果冲掉")

    check("卡片顺序表（卡片上没有作品 id，只能按位置点）",
          "_order" in code,
          "快手的卡片 DOM 上没有 id，photo_id 只能从搜索接口取，"
          "再靠顺序对应到第几张卡片")

    check("滚评论前会把鼠标挪进评论面板",
          hasattr(KuaishouBrowserCollector, "_park_over_comments"),
          "滚轮滚的是指针底下那个元素；停在视频上滚，滚的是视频")

    # ---- 下面四条都是 2026-09-05 实跑暴露出来的真 bug，各自钉一条 ----
    body_fn = inspect.getsource(KuaishouBrowserCollector._extract_comments_from_body)
    check("评论解析看**顶层**，不是只看 body['data']",
          "for scope in (body, body.get(\"data\"))" in body_fn
          or "(body, body.get('data'))" in body_fn,
          "真实响应是平铺的（rootCommentsV2 直接挂在顶层）。只看 data = "
          "接口拦到了也解析成 0 条，日志报「8 秒没有评论到货」，像是页面没点开")

    check("评论列表键名认 V2",
          "rootCommentsV2" in code and "subCommentsV2" in code,
          "真实响应用的就是 rootCommentsV2 / subCommentsV2")

    check("游标读 pcursorV2", "pcursorV2" in code,
          "真实字段是 pcursorV2；读 pcursor 永远读不到，"
          "「评论到底了」这个判断就一直不成立")

    goto_search = inspect.getsource(KuaishouBrowserCollector._goto_search)
    check("搜索是**打字 + 点搜索**，不是拼 URL",
          (".type(" in goto_search or "press_sequentially" in goto_search)
          and "SEARCH_BUTTON_SELECTOR" in goto_search,
          "用户明确要求：手动输入关键字，然后点击搜索。"
          "拼 /search/{关键字} 跳过去，页面状态和真人搜出来的不一样")

    autoplay = inspect.getsource(KuaishouBrowserCollector._disable_autoplay)
    check("关联播前先 hover、并等它解锁",
          "_hover_autoplay" in autoplay and "_switch_unlocked" in autoplay,
          "实跑报错 element is not enabled：开关刚打开视频时是 "
          "aria-disabled=\"true\"，它那块叫 hover-tip，鼠标悬停才解锁")

    check("翻完评论回填作品的评论总数",
          "_apply_comment_count" in code and "commentCountV2" in code,
          "搜索接口给不出评论数（feed[\"comment\"] 只有 us_c=0），"
          "不回填的话作品行的评论数永远写 0，和评论表里几百条对不上")

    handler = inspect.getsource(KuaishouBrowserCollector._handle_captured_response)
    check("「TA 的作品」不混进搜索结果",
          'matched_type == "kuaishou_profile"' in handler
          and "_current_keyword" in handler,
          "点开视频时快手会拉作者的其他作品（/rest/v/profile/feed）。"
          "收了它 → 顺序表混进搜索结果里没有的作品 → 下标错位 → "
          "点开的卡片和日志对不上 → 评论按 photoId 全被丢掉")

    check("点卡片前用文案核一遍",
          "_locate_card" in code and "_card_caption" in code,
          "卡片上没有作品 id，只靠下标点。下标一偏就点错卡片，"
          "而且没有任何直接迹象——用 caption 核对能自愈")

    check("点错了要喊出来",
          "_check_opened_work" in code and "_comment_photo_id" in code,
          "评论接口请求里的 photoId 是页面自己发的，是「实际打开了哪条」的唯一证据。"
          "对不上必须报警，不能安静地记成「这条没有评论」")

    close_src = inspect.getsource(KuaishouBrowserCollector._close_video)
    check("关视频要确认遮罩真的没了",
          "_mask_present" in close_src and "player-pop-mask" in code,
          "点了叉不等于关掉了。遮罩还在的话，顶栏的搜索框和「搜索」按钮"
          "全被它挡着（intercepts pointer events），换关键字直接卡死")

    goto_src = inspect.getsource(KuaishouBrowserCollector._goto_search)
    check("搜索前先把上一条视频关掉", "_close_video" in goto_src,
          "上一个关键字最后那条视频不关，遮罩会盖住顶栏")

    check("被遮罩挡住时会清掉再点", "_click_through_mask" in code,
          "干等没有意义——遮罩不会自己消失，Playwright 重试 19 次也是白等 8 秒")

    # ⚠️ 这里传的必须是 load_config() 的**结果**，不是 load_config 这个函数。
    #    传函数进去，describe() 里一调 config.get 就是
    #    AttributeError: 'function' object has no attribute 'get'，
    #    而这一句在 preflight 的最后，前面 30 多项全绿了还是崩。
    #    整段包 try：取不到节奏配置只是这一项没数显示，不该拖垮自检。
    try:
        from app.core.pacing import Pacer
        pace_note = Pacer(load_config(), "kuaishou").describe()
    except Exception as exc:  # noqa: BLE001
        pace_note = f"（取不到节奏配置：{type(exc).__name__}: {exc}）"
    check("采集节奏可配（crawl.pace）",
          hasattr(KuaishouBrowserCollector, "pacer") or "pacer" in code,
          f"当前：{pace_note}")

    # ---- 作品列表要陆续往下滑（本轮加的） ----
    from app.collectors.browser_capture import BrowserCaptureCollector
    list_src = inspect.getsource(KuaishouBrowserCollector.collect_by_keyword)
    check("作品列表按**滑动次数**收口，不是「没新作品就结束」",
          (KuaishouBrowserCollector.collect_by_keyword
           is not BrowserCaptureCollector.collect_by_keyword)
          and "list_scroll_times" in list_src,
          "快手搜索接口没有时间范围参数，时间段是本地过滤的；"
          "第一屏常常一条都不在时间段内，一屏又有 18 张卡片，"
          "基类那句「连续 3 轮没有新作品就结束」正好会在第一屏之后收工——"
          "现象就是「一条作品都没点开」，像点击坏了")

    stub2 = KuaishouBrowserCollector.__new__(KuaishouBrowserCollector)
    stub2.config = load_config()
    check("滑动次数可配（config.yaml）",
          isinstance(KuaishouBrowserCollector.list_scroll_times, property)
          and KuaishouBrowserCollector.DEFAULT_LIST_SCROLL_TIMES == 20,
          f"当前生效 {stub2.list_scroll_times} 次；"
          f"改 {KuaishouBrowserCollector.LIST_SCROLL_CONFIG_KEYS[0]}"
          f" 或 {KuaishouBrowserCollector.LIST_SCROLL_CONFIG_KEYS[1]}，0 = 不限")

    check("不拿 max_works 卡候选条数",
          "LIST_OVERFETCH_FACTOR" in list_src,
          "ctx.max_works 是「要留下多少条」，而时间窗和关键字过滤都在 "
          "runner 里、runner 自己会 break。采集器这边数的是过滤**之前**的，"
          "拿它收口就会出现「产出 16 条、上层留 0 条，采集器却以为够了」")

    check("滑列表前先把视频覆盖层关掉",
          "_mask_present" in list_src and "_close_video" in list_src,
          "上一条作品采完评论后视频还开着。覆盖层开着时滚的是覆盖层，"
          "列表一张新卡片都不会加载——表现是「滑了半天还是第一屏那些作品」")

    scroll_src = inspect.getsource(KuaishouBrowserCollector._scroll_list_once)
    push_src = inspect.getsource(KuaishouBrowserCollector._push_list_to_bottom)
    check("滚的是**真正在滚的那个容器**，不是 window",
          "overflowY" in push_src and "scrollTop" in push_src,
          "实跑「连续 3 次滑不动了（已经到底），滑了 3 次共产出 16 条」——"
          "16 条正好是第一屏，一次都没真的滚过。快手搜索页滚的是内部容器，"
          "body 上 overflow:hidden，window.scrollBy 打下去 scrollY 恒为 0")

    check("接口说 no_more 就别空滑三轮",
          "_search_no_more" in code and "_search_no_more" in list_src,
          "滑到底之后还要连等三轮才收工，一个关键字白耗小半分钟。"
          "但它只能当**辅助**信号：必须这一屏也没有新卡片才收工，"
          "单看它的话，接口偶发一个 no_more 就把整个关键字提前结束了")

    check("「还能不能往下滑」按**卡片数**判，不看滚动位置",
          "_list_card_count" in scroll_src
          and "LIST_LOAD_WAIT_SECONDS" in scroll_src,
          "滚动位置在内部容器场景下不可靠（恒 0），会被误判成到底了；"
          "而且懒加载要发一次 search/feed 再渲染，滚完立刻去数必然还是原来那些")

    check("展开回复会跳过「收起」",
          "收起" in code,
          "展开后按钮原地变「收起」，再点一次等于把刚展开的收回去")

    ok = all(c[1] for c in CHECKS)
    if not ok:
        print("\n⚠️  上面有失败项，说明加载的不是预期的那一版文件，"
              "后面的实跑结果参考价值有限")
    return ok


class _Tap:
    """把**实际拦到了什么**记下来。

    这是这个脚本目前最重要的产出：三个接口路径还没有被真实录制确认过，
    如果一个都没拦到，作品和评论必然是 0 条，而光看"0 条"分不清是
    "路径写错了"还是"页面没点开"。
    """

    def __init__(self):
        self.by_type: dict[str, int] = {}
        self.seen_urls: list[str] = []

    def note(self, matched_type: str, url: str) -> None:
        self.by_type[matched_type] = self.by_type.get(matched_type, 0) + 1

    def note_any(self, url: str) -> None:
        path = url.split("?")[0]
        if any(k in path for k in ("/rest/", "/graphql", "/api/")) and \
                not any(path.endswith(e) for e in
                        (".js", ".css", ".png", ".jpg", ".webp", ".svg", ".mp4")):
            if path not in self.seen_urls:
                self.seen_urls.append(path)

    def report(self) -> None:
        print("\n" + "=" * 66 + "\n拦截统计\n" + "=" * 66)
        if self.by_type:
            for name, n in sorted(self.by_type.items()):
                print(f"  ✅ {name:<24} 拦到 {n} 次")
        else:
            print("  ❌ 一个目标接口都没拦到")
        print(f"\n这一轮页面实际请求过的接口路径（前 25 个，去重）：")
        for url in self.seen_urls[:25]:
            print(f"     {url}")
        if not self.by_type and self.seen_urls:
            print("\n  ⚠️ 目标路径一个都没命中，但页面确实在打接口。"
                  "\n     把上面这份清单发回来，照着改 kuaishou_browser."
                  "_target_url_patterns 就行。")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keyword", default="盘山风景区")
    parser.add_argument("--account", default="")
    parser.add_argument("--works", type=int, default=0)
    parser.add_argument("--comments", type=int, default=0)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

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
    config.set("browser.headless", bool(args.headless))
    print(f"\n浏览器模式：{'无头' if args.headless else '有头（窗口会弹出来）'}")

    db = init_db(config)
    await db.connect()
    redis = init_redis(config)
    from app.repositories.account_repo import AccountRepository
    account_repo = AccountRepository(db)
    proxy_manager = ProxyManager(config, redis)
    browser_manager = BrowserSessionManager(config, account_repo)

    log = StepLogger()
    ctx = CollectContext(
        scenic_id="VERIFY", scenic_name="快手拟人验证",
        task_id=f"verify-ks-{int(time.time())}",
        max_works=args.works, max_comments_per_work=args.comments,
        account_name=args.account,
        filters=SearchFilters(channel="kuaishou"),
        params={"browser_manager": browser_manager},
        logger=log,
    )

    collector = KuaishouBrowserCollector(config, proxy_manager)
    # ⚠️ 采集器里 integrated=False（接口路径还没确认），
    # 但验证脚本就是来确认它的，这里强制打开
    collector.integrated = True

    tap = _Tap()
    works: list = []
    comments_total = 0
    reached_comments = False

    print(f"\n{'='*78}\n关键字：{args.keyword}   （快手没有排序/时间筛选）"
          f"\n作品上限：{args.works} 条   每条作品评论上限：{args.comments} 条"
          f"\n{'='*78}\n")
    try:
        print("[1/3] 启动浏览器、注入 network 拦截 ……")
        await collector.prepare(ctx)
        check("浏览器启动 + 拦截就绪", True,
              f"账号 {collector._session.account_name}")

        # 把拦截到的东西记一份，同时把页面实际打的接口也全记下来
        original = collector._handle_captured_response

        async def spy(ctx_, matched_type, url, body, response):
            tap.note(matched_type, url)
            await original(ctx_, matched_type, url, body, response)

        collector._handle_captured_response = spy
        collector._page.on("response", lambda r: tap.note_any(r.url))

        print("\n[2/3] 边搜边采：产出一条作品 → 立刻采它的评论 → 关视频 → 下一条")
        idx = 0
        async for work in collector.collect_by_keyword(ctx, args.keyword):
            works.append(work)
            idx += 1
            print_work(idx, ctx.max_works, work)
            reached_comments = True
            got = 0
            async for cm in collector.collect_comments(ctx, work):
                got += 1
                print_comment(got, cm)
            comments_total += got
            print(f"      └─ 小计 {got} 条评论"
                  + ("（这条作品没有评论）" if got == 0 else ""))
        check("采到作品", bool(works), f"{len(works)} 条")
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
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            pass

    tap.report()

    print("\n" + "=" * 66 + "\n分步判定\n" + "=" * 66)
    if log.saw("已关闭联播") or log.saw("联播本来就是关的"):
        check("关联播", True, "")
    elif reached_comments:
        check("关联播", False,
              "没看到关联播的日志——一条播完会自动跳下一条，评论会记到别人名下")
    else:
        skip("关联播", "还没打开过作品")

    if reached_comments:
        opened = not log.saw("评论面板没出现")
        check("打开评论面板", opened,
              "" if opened else "看上面有没有「没找到评论按钮」")
    else:
        skip("打开评论面板", "还没打开过作品")

    dead = [ln for ln in log.lines if "视频没打开" in ln]
    check("作品点得开", not dead, dead[0][:80] if dead else "")

    ok = all(c[1] for c in CHECKS)
    print("\n" + ("结论：全部通过 ✅" if ok else
                  "结论：有失败项 ❌ —— 把上面整段输出（尤其是「拦截统计」）发回来"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
