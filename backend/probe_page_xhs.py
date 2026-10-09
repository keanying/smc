#!/usr/bin/env python3
"""页面快照工具：把某个平台的真实页面结构拍下来，供写选择器用。

## 为什么要有它

拟人采集器里的每一个选择器都必须来自**真实页面**，不能靠猜。
平台改版、A/B 分流、账号等级不同，页面结构都会不一样；
猜出来的选择器在你的环境里跑不通，而且失败时看不出是猜错了还是改版了。

这个脚本用**你自己的登录账号**打开页面，一步一步拍快照：
每一步存下截图、DOM、可交互元素清单，以及那一步触发了哪些接口。

## 怎么用（在 backend 目录下，和 app/ 同级）

    python probe_page.py --channel xiaohongshu --keyword 盘山风景区

默认**有头**运行：窗口会弹出来，脚本走到它自己搞不定的地方
（比如"筛选面板长什么样"——面板的按钮在哪里正是我们要找的东西）
会停下来让你在窗口里点一下，然后回终端敲回车，它接着拍。

跑完看 `probe_out/<平台>_<时间>/` 目录，把整个目录打包发回来即可。

## 参数

    --channel    xiaohongshu / douyin / kuaishou（默认 xiaohongshu）
    --keyword    搜什么（默认 盘山风景区）
    --account    用哪个账号，留空 = 自动挑一个已登录的
    --auto       不停下来等人工，只拍脚本自己能到达的状态
                 （拿不到筛选面板和笔记弹窗，信息会少一大半，不推荐）
    --headless   无头跑。只在服务器上没有图形界面时才用，配合 --auto

## 跑之前确认

    1. MySQL 起着（要读账号表）
    2. 账号管理里有一个该平台**已登录**的账号
    3. 那个账号的登录窗口没开着（profile 目录不能被两个 Chromium 同时占）

## 关于隐私

输出目录里会有你自己账号的昵称、头像地址这类信息（页面上本来就显示着）。
脚本已经做了这些处理：
  · 所有 `<script>` 标签的内容都被清空（那里面有整页的初始状态，最敏感）
  · 请求头里的 Cookie / Authorization / 各种 token 一律不记录
  · 接口响应**只记结构不记内容**（记"有哪些字段"，不记字段的值）
发回来之前你可以自己翻一遍 SUMMARY.md 列出的文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.browser.manager import BrowserSessionManager
from app.browser.page_session import PageSession
from app.core.config import load_config
from app.core.constants import CHANNEL_LABELS
from app.core.db import init_db
from app.core.logging import setup_logging
from app.core.subprocess_loop import SUBPROCESS_LOOP
from app.repositories.account_repo import AccountRepository

# ---------------------------------------------------------------------------
# 各平台的入口和"要拍哪几步"
#
# 步骤文案写得具体一点：这是给人照着做的，写"点一下筛选"不如写
# "点搜索框右边那个「筛选」，把面板展开"。
# ---------------------------------------------------------------------------
CHANNELS: Dict[str, Dict[str, Any]] = {
    "xiaohongshu": {
        "home": "https://www.xiaohongshu.com",
        "search": "https://www.xiaohongshu.com/search_result?keyword={keyword}",
        "api_hint": "/api/sns/web/",
        # 脚本自己点得动的步骤：点完**立刻**拍，不等人回终端敲回车。
        # ⚠️ 这一条是被上一轮快照逼出来的：筛选面板是"点开就会因为失焦收起"的
        # 那种下拉，人跑回终端敲回车的这几秒里它已经关了——
        # 04_filter_panel 拍到的是"面板已收起、入口从「筛选」变成「已筛选」"，
        # 面板里到底有哪些选项一个都没留下。
        "auto_clicks": [
            ("04a_filter_open", "脚本自动点开「筛选」面板并立刻拍（面板会失焦收起）",
             "div.filter"),
        ],
        "manual_steps": [
            # 04_filter_panel 这一步删掉了：面板已经由上面的 auto_clicks 拍到了，
            # 再让人手动展开一次拍到的还是"已经收起"的样子（上一轮就是这样）。
            ("05_sorted_latest",
             "重新展开「筛选」面板，选「最新」排序，等结果刷新出来"),
            ("06_time_filter",
             "如果面板里有发布时间（一天内/一周内/半年内之类），选一个；没有就直接回车"),
            ("07_note_modal",
             "点开第一条笔记（页面上会弹出笔记详情，不要新开标签页）"),
            ("08_comments",
             "把笔记弹窗里的评论区往下滚两屏，让评论都加载出来"),
            ("09_reply_expanded",
             "找一条有回复的评论，点开它的「展开 N 条回复」"),
            ("10_modal_closed",
             "关掉笔记弹窗，回到搜索结果列表"),
        ],
    },
    "douyin": {
        "home": "https://www.douyin.com/jingxuan",
        "search": "https://www.douyin.com/jingxuan/search/{keyword}?type=general",
        "api_hint": "/aweme/v1/web/",
        "manual_steps": [
            ("04_grid_mode", "把列表切成「多列」"),
            ("05_filter_panel", "展开「筛选」面板"),
            ("06_sorted_latest", "选「最新发布」，等结果刷新"),
            ("07_work_modal", "点开第一条作品"),
            ("08_comments", "打开评论区并往下滚两屏"),
            ("09_reply_expanded", "展开一条评论的回复"),
            ("10_modal_closed", "关掉作品弹窗"),
        ],
    },
    "kuaishou": {
        "home": "https://www.kuaishou.com",
        "search": "https://www.kuaishou.com/search/video?searchKey={keyword}",
        "api_hint": "/graphql",
        "manual_steps": [
            ("04_filter_panel", "展开筛选/排序（如果有）"),
            ("05_sorted_latest", "选「最新」，等结果刷新"),
            ("06_work_modal", "点开第一条视频"),
            ("07_comments", "打开评论区并往下滚两屏"),
            ("08_reply_expanded", "展开一条评论的回复"),
            ("09_modal_closed", "关掉视频弹窗"),
        ],
    },
}

#: DOM 摘要里最多列多少个元素。太多了没人看，也不利于我定位。
MAX_ELEMENTS = 400
#: 单条元素的文本截断长度
MAX_TEXT = 60

# ---------------------------------------------------------------------------
# 在页面里跑的采集脚本：把"看起来能点的、或者带文字的"元素列出来
#
# 为什么要自己写而不是存 HTML 就完事：HTML 有几百 KB，而且看不出
# 哪个元素**当前可见**、在屏幕的什么位置。写选择器最需要的恰恰是这两点。
# ---------------------------------------------------------------------------
COLLECT_JS = """(maxText) => {
  const out = [];
  const seen = new Set();
  // Vue 的 scoped 样式会给**每一个**元素挂 data-v-xxxxxx，
  // 那不是页面语义，只是构建产物——既不该当成"值得记录"的理由，
  // 也不该占着属性列表。这里统一排除。
  const meaningfulData = (el) =>
    Array.from(el.attributes).some(
      a => a.name.startsWith('data-') && !a.name.startsWith('data-v-'));
  const interesting = (el) => {
    const tag = el.tagName.toLowerCase();
    if (['script', 'style', 'meta', 'link', 'head', 'html'].includes(tag)) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return false;
    // 只要"自己的文字"，避免把每一层容器都算成有文字
    const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').trim();
    const clickable = tag === 'a' || tag === 'button' || tag === 'input' ||
      tag === 'textarea' || tag === 'svg' || tag === 'img' ||
      st.cursor === 'pointer' || el.onclick !== null ||
      el.hasAttribute('role') || meaningfulData(el);
    // ⚠️ meaningfulData 这一条是必须的：小红书/抖音的卡片容器本身通常
    // **没有文字也不"可点"**（文字在子元素里，点击靠事件委托），
    // 但 note_id / aweme_id 恰恰挂在这个容器的 data-* 上。
    // 只按"有文字或可点"筛的话，最关键的那个元素正好会被漏掉。
    return own.length > 0 || clickable;
  };
  const attrs = (el) => {
    const keep = {};
    for (const a of el.attributes) {
      if (['class', 'id', 'role', 'title', 'href', 'placeholder', 'alt',
           'aria-label', 'type', 'name'].includes(a.name) ||
          (a.name.startsWith('data-') && !a.name.startsWith('data-v-'))) {
        // class 太长的截断——构建产物里常有十几个原子类
        keep[a.name] = a.value.length > 120 ? a.value.slice(0, 120) + '…' : a.value;
      }
    }
    return keep;
  };
  const path = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) { s += '#' + cur.id; parts.unshift(s); break; }
      const cls = (cur.className || '').toString().trim().split(/\\s+/)
        .filter(Boolean).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  for (const el of document.querySelectorAll('*')) {
    if (!interesting(el)) continue;
    const r = el.getBoundingClientRect();
    const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').trim();
    const key = path(el) + '|' + own.slice(0, 20) + '|' + Math.round(r.x) + ',' + Math.round(r.y);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      tag: el.tagName.toLowerCase(),
      text: own.slice(0, maxText),
      attrs: attrs(el),
      path: path(el),
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      inViewport: r.top < innerHeight && r.bottom > 0,
    });
  }
  return { url: location.href, title: document.title, total: out.length, elements: out };
}"""


def shape_of(value: Any, depth: int = 0) -> Any:
    """把 JSON 变成"只有结构没有内容"的形状。

    ⚠️ 这是隐私边界所在：接口响应里有笔记正文、评论内容、用户昵称。
    我需要的只是"哪个字段装着 note_id、哪个字段装着评论列表"，
    所以这里只记类型和字段名，一个真实的值都不带出去。
    """
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: shape_of(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        if not value:
            return []
        return [shape_of(value[0], depth + 1), f"…共 {len(value)} 项"]
    return type(value).__name__


class Probe:
    def __init__(self, page, out_dir: Path, api_hint: str):
        self.page = page
        self.out = out_dir
        self.api_hint = api_hint
        self.pending: List[Dict[str, Any]] = []
        self.net_log = out_dir / "network.jsonl"
        self.index: List[Dict[str, str]] = []

    # ---------------- 网络 ----------------
    def hook_network(self) -> None:
        page = self.page

        async def on_response(response) -> None:
            url = response.url
            if self.api_hint not in url:
                return
            record: Dict[str, Any] = {
                "at": round(time.time(), 2),
                "method": response.request.method,
                "status": response.status,
                # ⚠️ 只留 path + query 的**键**，不留 query 的值：
                # 值里有 xsec_token、search_id 这类跟账号绑定的东西
                "url": strip_query_values(url),
                "content_type": (response.headers or {}).get("content-type", ""),
            }
            try:
                text = await response.text()
                record["bytes"] = len(text)
                record["shape"] = shape_of(json.loads(text))
            except Exception as exc:  # noqa: BLE001
                record["shape"] = f"（不是 JSON 或读不到：{type(exc).__name__}）"
            self.pending.append(record)

        def sync_on_response(response) -> None:
            asyncio.create_task(on_response(response))

        page.on("response", sync_on_response)

    def flush_network(self, step: str) -> int:
        got, self.pending = self.pending, []
        with self.net_log.open("a", encoding="utf-8") as fh:
            for record in got:
                record["step"] = step
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(got)

    # ---------------- 快照 ----------------
    async def snap(self, name: str, note: str = "") -> None:
        print(f"\n  ▸ 拍快照 {name} …", flush=True)
        # 先给网络回调一点时间落地——刚点完的请求可能还在路上
        await asyncio.sleep(1.5)

        png = self.out / f"{name}.png"
        try:
            await self.page.screenshot(path=str(png), full_page=True)
        except Exception as exc:  # noqa: BLE001
            # 页面很长时 full_page 会失败，退回可视区
            print(f"     整页截图失败（{exc}），改截可视区")
            await self.page.screenshot(path=str(png))

        html = await self.page.content()
        (self.out / f"{name}.html").write_text(scrub_html(html), encoding="utf-8")

        try:
            data = await self.page.evaluate(COLLECT_JS, MAX_TEXT)
        except Exception as exc:  # noqa: BLE001
            data = {"url": "", "title": "", "total": 0, "elements": [],
                    "error": str(exc)}
        (self.out / f"{name}.elements.txt").write_text(
            render_elements(name, note, data), encoding="utf-8")

        n = self.flush_network(name)
        self.index.append({"name": name, "note": note,
                           "url": data.get("url", ""),
                           "elements": str(data.get("total", 0)),
                           "requests": str(n)})
        print(f"     截图 + DOM + {data.get('total', 0)} 个可见元素 + {n} 个接口请求")


def strip_query_values(url: str) -> str:
    """`a=1&b=2` → `a=<值>&b=<值>`。留下参数名（我要知道接口收哪些参数），去掉值。"""
    if "?" not in url:
        return url
    path, _, query = url.partition("?")
    keys = [kv.split("=", 1)[0] for kv in query.split("&") if kv]
    return path + "?" + "&".join(f"{k}=<值>" for k in keys)


def scrub_html(html: str) -> str:
    """清空 script 内容。

    那里面是整页的初始状态（`window.__INITIAL_STATE__` 之类），
    既是文件里最大的一块，也是最敏感的一块——账号信息、推荐流全在里面。
    我要的是 DOM 结构，不需要它。
    """
    def blank(match: re.Match) -> str:
        body = match.group(2)
        return f"{match.group(1)}/* 已清空 {len(body)} 字节 */{match.group(3)}"

    return re.sub(r"(<script\b[^>]*>)(.*?)(</script>)", blank, html,
                  flags=re.S | re.I)


def render_elements(name: str, note: str, data: Dict[str, Any]) -> str:
    lines = [
        f"# {name}",
        f"# {note}" if note else "",
        f"# URL   : {data.get('url', '')}",
        f"# 标题  : {data.get('title', '')}",
        f"# 可见元素 {data.get('total', 0)} 个（下面最多列 {MAX_ELEMENTS} 个，视口内的排前面）",
        "#",
        "# 每行： [是否在视口内] 标签  x,y,宽,高  自己的文字  属性  DOM 路径",
        "",
    ]
    if data.get("error"):
        lines.append(f"!! 采集脚本报错：{data['error']}")
    elements = data.get("elements", [])
    # 视口内的排前面：写选择器时最先要看的就是"屏幕上现在有什么"
    elements.sort(key=lambda e: (not e.get("inViewport"), e["box"][1], e["box"][0]))
    for el in elements[:MAX_ELEMENTS]:
        x, y, w, h = el["box"]
        flag = "●" if el.get("inViewport") else "○"
        attrs = " ".join(f'{k}="{v}"' for k, v in el["attrs"].items())
        text = el["text"].replace("\n", "⏎")
        lines.append(f"{flag} {el['tag']:<8} {x:>5},{y:>5},{w:>4},{h:>4}  "
                     f"{text!r:<30} {attrs}")
        lines.append(f"    ↳ {el['path']}")
    if len(elements) > MAX_ELEMENTS:
        lines.append(f"\n…还有 {len(elements) - MAX_ELEMENTS} 个没列（改 MAX_ELEMENTS 可以放宽）")
    return "\n".join(lines)


def write_summary(out: Path, channel: str, keyword: str, probe: Probe,
                  manual: bool) -> None:
    label = CHANNEL_LABELS.get(channel, channel)
    lines = [
        f"# {label} 页面快照  ({datetime.now():%Y-%m-%d %H:%M})",
        "",
        f"- 关键字：`{keyword}`",
        f"- 方式：{'有人工引导（信息完整）' if manual else '全自动（只有脚本能自己到达的状态）'}",
        "",
        "## 拍到了什么",
        "",
        "| 步骤 | 说明 | 可见元素 | 触发的接口 |",
        "|---|---|---|---|",
    ]
    for row in probe.index:
        lines.append(f"| `{row['name']}` | {row['note'] or '-'} | "
                     f"{row['elements']} | {row['requests']} |")
    lines += [
        "",
        "## 每一步有三个文件",
        "",
        "| 文件 | 是什么 |",
        "|---|---|",
        "| `NN_xxx.png` | 整页截图 |",
        "| `NN_xxx.html` | 页面 DOM（`<script>` 内容已清空） |",
        "| `NN_xxx.elements.txt` | **最有用的一个**：当前可见的元素清单，带坐标、文字、属性、DOM 路径 |",
        "",
        "另外 `network.jsonl` 记录了每一步触发的接口：URL（参数值已去掉）、"
        "状态码，以及响应的**结构**（只有字段名和类型，没有任何一个真实值）。",
        "",
        "## 隐私",
        "",
        "- `<script>` 内容已全部清空（初始状态、账号信息都在那里）",
        "- 请求头（Cookie / Authorization）一个字都没记",
        "- 接口响应只记结构不记内容",
        "- 截图和 HTML 里仍然会有页面上本来就显示着的东西：你的昵称、头像、"
        "笔记标题和作者名。介意的话把 `.png` 删掉再发，`.elements.txt` "
        "和 `network.jsonl` 是写选择器真正需要的两个。",
        "",
        "## 把整个目录打包发回来即可",
        "",
        f"```\nzip -r probe_{channel}.zip {out.name}\n```",
    ]
    (out / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


async def run_auto_clicks(page, probe: "Probe", spec: Dict[str, Any]) -> List[str]:
    """脚本自己点得动的步骤：点完**立刻**拍，不等人回终端敲回车。

    存在的理由是上一轮踩过的坑：筛选面板是"一失焦就收起"的下拉，
    人跑回终端敲回车的那几秒里它已经关了——拍到的是"面板已收起、
    入口从「筛选」变成「已筛选」"，面板里到底有哪些选项一个都没留下。

    单步失败不影响后面：拍快照本来就是"能拍多少是多少"，
    为一个按钮没找到就中断，前面拍好的也白搭。
    返回成功拍下的步骤名，给调用方和测试用。
    """
    done: List[str] = []
    for name, note, selector in spec.get("auto_clicks", ()):
        try:
            target = page.locator(selector).first
            await target.wait_for(state="visible", timeout=8000)
            await target.click(timeout=8000)
            await asyncio.sleep(0.8)
            await probe.snap(name, note)
            done.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"\n  ⚠️  自动步骤 {name} 没做成（{selector}）：{exc}")
            print("     不影响后面的步骤，接着走")
    return done


async def wait_enter(prompt: str) -> None:
    """在窗口里操作完之后回车。用线程等输入，别把事件循环堵死。"""
    print(f"\n  ⏸  {prompt}")
    print("     做完之后回到这里按回车（直接回车跳过这一步）… ", end="", flush=True)
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)


async def main() -> int:
    parser = argparse.ArgumentParser(description="拍页面快照，供写拟人采集器的选择器用")
    parser.add_argument("--channel", default="xiaohongshu", choices=sorted(CHANNELS))
    parser.add_argument("--keyword", default="盘山风景区")
    parser.add_argument("--account", default="")
    parser.add_argument("--auto", action="store_true",
                        help="不等人工操作。拿不到筛选面板和详情弹窗，不推荐")
    parser.add_argument("--headless", action="store_true", help="无头（配合 --auto）")
    parser.add_argument("--out", default="probe_out")
    args = parser.parse_args()

    spec = CHANNELS[args.channel]
    label = CHANNEL_LABELS.get(args.channel, args.channel)
    manual = not args.auto

    config = load_config()
    setup_logging("WARNING", Path(config.get("server.data_dir", "./data")) / "logs")
    config.ensure_dirs()
    config.set("browser.headless", bool(args.headless))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) / f"{args.channel}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 74}")
    print(f"{label} 页面快照   关键字：{args.keyword}")
    print(f"输出目录：{out.resolve()}")
    print(f"模式：{'有头 + 人工引导' if manual and not args.headless else '自动'}")
    if manual and args.headless:
        print("⚠️  无头 + 人工引导没有意义（你看不到窗口），要么去掉 --headless，"
              "要么加 --auto")
        return 2
    print(f"{'=' * 74}\n")

    db = init_db(config)
    await db.connect()
    account_repo = AccountRepository(db)
    manager = BrowserSessionManager(config, account_repo)

    session = PageSession(manager, args.channel, args.account)
    try:
        await session.start()
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 打不开浏览器：{exc}")
        print("   常见原因：这个平台还没有已登录的账号；或者该账号的登录窗口正开着")
        await db.close()
        return 1

    print(f"✓ 浏览器已启动，账号 {session.account_name}")

    async def run_probe() -> None:
        """全部页面操作都在浏览器循环上跑——page 属于那条循环。"""
        page = session._page
        probe = Probe(page, out, spec["api_hint"])
        probe.hook_network()

        await page.goto(spec["home"], wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(3)
        await probe.snap("01_home", "首页（顺便确认登录态：右上角应该是你的头像）")

        url = spec["search"].format(keyword=args.keyword)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(4)
        await probe.snap("02_search_result", f"搜索「{args.keyword}」的结果页（默认排序）")

        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(2.5)
        await probe.snap("03_scrolled", "往下滚一屏（看滚动加载走哪个接口）")

        # 脚本点得动的先自己点：点完立刻拍，不给面板失焦收起的机会
        await run_auto_clicks(page, probe, spec)

        if manual:
            print(f"\n{'-' * 74}")
            print("下面这几步脚本自己做不了——要找的正是这些按钮在哪里。")
            print("请在弹出来的浏览器窗口里操作，每做完一步回终端按回车。")
            print(f"{'-' * 74}")
            for name, note in spec["manual_steps"]:
                await wait_enter(note)
                await probe.snap(name, note)
        else:
            print("\n（--auto：跳过所有需要人工的步骤）")

        write_summary(out, args.channel, args.keyword, probe, manual)

    try:
        await SUBPROCESS_LOOP.run(run_probe())
    finally:
        await session.close()
        await db.close()

    files = sorted(p.name for p in out.iterdir())
    print(f"\n{'=' * 74}")
    print(f"完成，{len(files)} 个文件在：{out.resolve()}")
    print("先看 SUMMARY.md，然后把整个目录打包发回来")
    print(f"{'=' * 74}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已中断")