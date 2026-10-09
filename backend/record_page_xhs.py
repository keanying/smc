#!/usr/bin/env python3
"""操作录制器：你在浏览器里正常操作，它把「你干了什么」和「页面因此变成什么样」全程记下来。

## 和 probe_page.py 的分工

`probe_page.py` 是**我列步骤、你照做**——问题是我只能问我想得到的东西。
这个脚本反过来：**你随便操作，它全程记录**，包括我压根不知道该问的部分。
小红书那个"筛选面板要 hover 才出来、鼠标一移开就收"，就是分步快照拍不到、
但录制能自然抓到的那类事。

## 记什么

每当页面**发生变化**（新元素出现/消失），就记一条：

  · 是什么动作引起的：点击 / 鼠标移上去 / 滚动 / 输入 / 导航
  · 点的是哪个元素：标签、class、data-*、文字、DOM 路径、坐标、第几次点它
  · 页面多了什么、少了什么（这是最有用的一栏——
    "鼠标移到 div.filter 上之后多了『排序依据』"这种因果关系，一眼可见）
  · 那一刻的截图、DOM、可见元素清单
  · 这期间触发了哪些接口

⚠️ **鼠标移上去（hover）只有在它真的引起了页面变化时才记**。
不然满屏都是无意义的 mouseover，反而把有用的信息淹了。

## 怎么用（在 backend 目录下，和 app/ 同级）

    python record_page_xhs.py --channel xiaohongshu

窗口弹出来之后你就正常操作：搜索、点筛选、选排序、点开笔记、翻评论、
关掉笔记…… 想停的时候回到终端按回车。

跑完看 `record_out/<平台>_<时间>/`，整个目录打包发回来。

## 参数

    --channel     xiaohongshu / douyin / kuaishou（默认 xiaohongshu）
    --account     用哪个账号，留空 = 自动挑一个已登录的
    --url         开局打开哪个地址，默认该平台首页
    --max-snaps   最多存多少张快照（默认 150，防止跑太久撑爆目录）
    --max-minutes 最多录多久（默认 30 分钟）
    --no-shot     不存截图，只存 DOM 和元素清单（目录会小很多）

## 跑之前确认

    1. MySQL 起着（要读账号表）
    2. 账号管理里有一个该平台**已登录**的账号
    3. 那个账号的登录窗口没开着（profile 目录不能被两个 Chromium 同时占）

## 关于隐私

和 probe_page.py 同一套，而且更严一些：
  · `<script>` 内容全部清空
  · 请求头一个字不记；URL 的 query **只留参数名不留值**
  · 接口响应**只记结构不记内容**（字段名 + 类型 + 数组长度）
  · 输入框：密码类型的**连长度都不记**；其它输入框记内容
    （搜索词是有用的，但你要是在里面打了别的，跑完自己删掉那几行）
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_page as P  # 脱敏、元素清单、网络记录全部复用，保证口径一致
from app.browser.manager import BrowserSessionManager
from app.browser.page_session import PageSession
from app.core.config import load_config
from app.core.constants import CHANNEL_LABELS
from app.core.db import init_db
from app.core.logging import setup_logging
from app.core.subprocess_loop import SUBPROCESS_LOOP
from app.repositories.account_repo import AccountRepository

HOME = {
    "xiaohongshu": "https://www.xiaohongshu.com",
    "douyin": "https://www.douyin.com/jingxuan",
    "kuaishou": "https://www.kuaishou.com",
}
API_HINT = {
    "xiaohongshu": "/api/sns/web/",
    "douyin": "/aweme/v1/web/",
    "kuaishou": "/graphql",
}

#: 页面安静多久算"这次变化结束了"（毫秒）。太短会把一次变化拆成好几条，
#: 太长会把两次操作并成一条。
SETTLE_MS = 500
#: 两次快照之间至少隔多久（秒）。用户狂点的时候不至于把磁盘写满。
MIN_SNAP_INTERVAL = 0.8
#: 页面变化里，多/少几个元素才算"值得记一笔"。
#: 1 个的变化多半是计时器、红点、懒加载的图片。
MIN_DELTA = 2

# ---------------------------------------------------------------------------
# 注入页面的录制脚本
#
# 思路：
#   · 记住最近一次点击 / 鼠标移过的元素（可能的"起因"）
#   · MutationObserver 看到变化 → 等页面安静 500ms → 回调 Python
#   · Python 那边再对比"变化前后可见元素集合"，算出多了什么少了什么
# hover 只有在真的引起变化时才会被记下来——因为它只是作为"起因"被引用，
# 本身不触发记录。
# ---------------------------------------------------------------------------
RECORDER_JS = r"""
(() => {
  if (window.__smcRecorderInstalled) return;
  window.__smcRecorderInstalled = true;

  const clickCounts = new Map();

  const path = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) { s += '#' + cur.id; parts.unshift(s); break; }
      const cls = (cur.className || '').toString().trim().split(/\s+/)
        .filter(Boolean).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };

  const describe = (el) => {
    if (!el || el.nodeType !== 1) return null;
    const attrs = {};
    for (const a of el.attributes || []) {
      if (['class', 'id', 'role', 'title', 'href', 'placeholder', 'alt',
           'aria-label', 'type', 'name'].includes(a.name) ||
          (a.name.startsWith('data-') && !a.name.startsWith('data-v-'))) {
        attrs[a.name] = a.value.length > 120 ? a.value.slice(0, 120) + '…' : a.value;
      }
    }
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.textContent || '').trim().slice(0, 60),
      ownText: Array.from(el.childNodes).filter(n => n.nodeType === 3)
        .map(n => n.textContent).join('').trim().slice(0, 60),
      attrs, path: path(el),
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    };
  };

  // ---------------------------------------------------------------------
  // 模型：**动作进队列，hover 只留最后一个**
  //
  // 第一版用"一个 lastTrigger + 谁优先级高谁覆盖"，两头不讨好：
  // 窗口设长了，隔半秒去 hover 筛选也被当成噪声挡掉，面板怎么出来的没录到；
  // 设短了又变成同一次点击被上报好几遍。根子在于一个变量装不下
  // "这段时间里发生的若干个动作"。
  //
  // 改成队列之后各归各的：真人动作按发生顺序全部留下；
  // hover 不进队列（不然满屏 mouseover），只在**这段时间里一个动作都没有**
  // 的时候，才拿它当这次页面变化的起因——那正是"移上去面板就弹出来"的情形。
  // ---------------------------------------------------------------------
  const queue = [];
  let lastHover = null;
  let timer = null, pending = false;

  const flush = () => {
    clearTimeout(timer);       // 一定要清，不然等下还会再触发一次
    timer = null;
    if (!pending && !queue.length) return;
    pending = false;
    const payload = {
      actions: queue.splice(0, queue.length),
      hover: lastHover,
      url: location.href, title: document.title,
      scrollY: Math.round(scrollY),
    };
    try { window.__smcRecord(payload); } catch (err) { /* 页面卸载中 */ }
  };
  const schedule = () => {
    pending = true;
    clearTimeout(timer);
    timer = setTimeout(flush, __SETTLE_MS__);
  };
  const push = (kind, el, extra) => {
    queue.push({kind, at: Date.now(), el: describe(el), ...(extra || {})});
    schedule();
  };

  document.addEventListener('click', (e) => {
    const el = e.target;
    const key = path(el) + '|' + (el.textContent || '').trim().slice(0, 20);
    clickCounts.set(key, (clickCounts.get(key) || 0) + 1);
    push('click', el, {
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      nth: clickCounts.get(key),
    });
  }, true);

  document.addEventListener('mouseover', (e) => {
    // 只记着，不进队列。它只有在"这段时间没有任何动作"时才会被当成起因。
    lastHover = {kind: 'hover', at: Date.now(), el: describe(e.target)};
  }, true);

  let lastScrollAt = 0;
  addEventListener('scroll', () => {
    const now = Date.now();
    if (now - lastScrollAt < 400) return;   // 滚动是连续事件，节流
    lastScrollAt = now;
    push('scroll', document.scrollingElement, {scrollY: Math.round(scrollY)});
  }, true);

  document.addEventListener('input', (e) => {
    const el = e.target;
    const isPwd = (el.type || '').toLowerCase() === 'password';
    const value = isPwd ? '(密码，不记录)' : String(el.value || '').slice(0, 60);
    // 同一个输入框连续敲字只留最后一次，不然一个字一条
    const last = queue[queue.length - 1];
    if (last && last.kind === 'input' && (last.el || {}).path === path(el)) {
      last.value = value;
      last.at = Date.now();
      schedule();
      return;
    }
    push('input', el, {value});
  }, true);

  document.addEventListener('keydown', (e) => {
    if (['Enter', 'Escape', 'Tab'].includes(e.key)) push('key', e.target, {key: e.key});
  }, true);

  // 导航（SPA 用 pushState 换 URL，不会触发 load）
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      const from = lastUrl;
      lastUrl = location.href;
      push('nav', document.body, {from, to: location.href});
    }
  }, 300);

  // ⚠️ 观测目标必须是 `document`，不能是 `document.documentElement`。
  // add_init_script 是在**文档刚创建**时执行的，那一刻 documentElement
  // 还是 null，observe(null, …) 会抛异常。而它是 IIFE 的最后一句，
  // 抛了之后前面注册的点击/滚动/输入监听全都还在——于是表现极其隐蔽：
  // 点击照样记得到（它们自己调 schedule），**只有靠 MutationObserver
  // 触发的 hover 一条都记不到**，而 hover 恰恰是最要紧的那类。
  // 实测就是这么丢的：面板明明开着，回调一次都没来。
  const observer = new MutationObserver(schedule);
  const startObserving = () => {
    try {
      observer.observe(document, {
        childList: true, subtree: true, attributes: true,
        attributeFilter: ['class', 'style'],
      });
      return true;
    } catch (e) { return false; }
  };
  if (!startObserving()) {
    addEventListener('DOMContentLoaded', startObserving, {once: true});
  }
})();
"""

# 变化前后各扫一次"可见元素集合"，差集就是多了什么/少了什么
SIGNATURE_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').trim().slice(0, 30);
    let s = el.tagName.toLowerCase();
    const cls = (el.className || '').toString().trim().split(/\\s+/)
      .filter(Boolean).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    out.push(s + (own ? ' 「' + own + '」' : '') +
             ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
  }
  return out;
}"""


class Recorder:
    def __init__(self, page, out: Path, api_hint: str, *,
                 max_snaps: int, shots: bool):
        self.page = page
        self.out = out
        self.max_snaps = max_snaps
        self.shots = shots
        self.probe = P.Probe(page, out, api_hint)
        self.events: List[Dict[str, Any]] = []
        self.seq = 0
        self.started = time.time()
        self.last_snap = 0.0
        self.prev_sig: Optional[Dict[str, int]] = None
        self.busy = False
        self.stopped = False

    # ---------------- 页面回调 ----------------
    async def on_record(self, source, payload: Dict[str, Any]) -> None:
        # 一次只处理一个：截图和取 DOM 都要时间，重入会把顺序搅乱
        if self.busy or self.stopped or self.seq >= self.max_snaps:
            return
        self.busy = True
        try:
            await self._handle(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  记录出错（继续）：{exc}")
        finally:
            self.busy = False

    async def _handle(self, payload: Dict[str, Any]) -> None:
        sig = await self._signature()
        added, removed = self._diff(sig)
        actions = payload.get("actions") or []

        if not actions:
            # 这段时间一个动作都没有，页面却变了 —— 起因就是鼠标移上去。
            # 这正是"移到筛选上面板才弹出来"那类事，也是分步快照拍不到的。
            hover = payload.get("hover")
            if hover is None or len(added) + len(removed) < MIN_DELTA:
                self.prev_sig = sig     # 红点、计时器、懒加载的图，不值一提
                return
            actions = [hover]

        # 一次上报里可能有好几个动作（比如快速点了两下不同的东西）。
        # 页面变化算在**最后一个**头上——前面那些如果真引起了变化，
        # 它们各自的上报里已经记过了。
        for i, action in enumerate(actions):
            if self.seq >= self.max_snaps:
                return
            last = (i == len(actions) - 1)
            self.seq += 1
            kind = action.get("kind", "?")
            name = f"{self.seq:03d}_{kind}"
            record = {
                "seq": self.seq,
                "at": round(time.time() - self.started, 2),
                "kind": kind,
                "url": payload.get("url", ""),
                "title": payload.get("title", ""),
                "scrollY": payload.get("scrollY"),
                "target": action.get("el"),
                "added": added[:40] if last else [],
                "removed": removed[:40] if last else [],
                "added_total": len(added) if last else 0,
                "removed_total": len(removed) if last else 0,
            }
            for extra in ("x", "y", "nth", "key", "value", "from", "to"):
                if extra in action:
                    record[extra] = action[extra]

            # 只给"最后一个动作"存快照：中间那几个动作之间页面还没稳定下来，
            # 存了也是同一张图，白占地方
            if last:
                await self._snapshot(name, record)
            self.events.append(record)

            el = action.get("el") or {}
            where = el.get("path", "") or "-"
            text = el.get("ownText") or el.get("text") or ""
            print(f"  {self.seq:>3}. {kind:<6} {where[:52]:<52} "
                  f"{('「' + text[:12] + '」') if text else '':<16} "
                  f"{f'+{len(added)} -{len(removed)}' if last else ''}")

        self.prev_sig = sig
        self.last_snap = time.time()

    async def _signature(self) -> Dict[str, int]:
        try:
            got = await self.page.evaluate(SIGNATURE_JS)
        except Exception:  # noqa: BLE001
            return {}
        counts: Dict[str, int] = {}
        for item in got or []:
            counts[item] = counts.get(item, 0) + 1
        return counts

    def _diff(self, sig: Dict[str, int]) -> tuple:
        if self.prev_sig is None:
            return [], []
        added, removed = [], []
        for key, n in sig.items():
            delta = n - self.prev_sig.get(key, 0)
            if delta > 0:
                added.append(key if delta == 1 else f"{key} ×{delta}")
        for key, n in self.prev_sig.items():
            delta = n - sig.get(key, 0)
            if delta > 0:
                removed.append(key if delta == 1 else f"{key} ×{delta}")
        return added, removed

    async def _snapshot(self, name: str, record: Dict[str, Any]) -> None:
        if self.shots:
            try:
                # 只截可视区、用 JPEG：整页 PNG 一张就好几百 KB，
                # 录一次几十上百张，目录会大到没法发
                await self.page.screenshot(path=str(self.out / f"{name}.jpg"),
                                           type="jpeg", quality=55)
            except Exception as exc:  # noqa: BLE001
                record["shot_error"] = str(exc)[:80]
        try:
            html = P.scrub_html(await self.page.content())
            with gzip.open(self.out / f"{name}.html.gz", "wt", encoding="utf-8") as fh:
                fh.write(html)
        except Exception as exc:  # noqa: BLE001
            record["html_error"] = str(exc)[:80]
        try:
            data = await self.page.evaluate(P.COLLECT_JS, P.MAX_TEXT)
            (self.out / f"{name}.elements.txt").write_text(
                P.render_elements(name, record.get("kind", ""), data),
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            record["elements_error"] = str(exc)[:80]
        record["requests"] = self.probe.flush_network(name)

    # ---------------- 收尾 ----------------
    def write_files(self, channel: str) -> None:
        with (self.out / "events.jsonl").open("w", encoding="utf-8") as fh:
            for record in self.events:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        (self.out / "SUMMARY.md").write_text(
            self._summary(channel), encoding="utf-8")

    def _summary(self, channel: str) -> str:
        label = CHANNEL_LABELS.get(channel, channel)
        lines = [
            f"# {label} 操作录制  ({datetime.now():%Y-%m-%d %H:%M})",
            "",
            f"- 共 {len(self.events)} 条记录，历时 "
            f"{(time.time() - self.started) / 60:.1f} 分钟",
            "",
            "## 操作序列",
            "",
            "| # | 秒 | 动作 | 点/移到哪里 | 文字 | 页面多了 | 少了 | 接口 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in self.events:
            el = r.get("target") or {}
            text = (el.get("ownText") or el.get("text") or "").replace("|", "/")
            lines.append(
                f"| {r['seq']} | {r['at']:.0f} | {r['kind']} | "
                f"`{(el.get('path') or '-')[:46]}` | {text[:16]} | "
                f"{r['added_total']} | {r['removed_total']} | {r.get('requests', 0)} |")
        lines += [
            "",
            "## 每条记录有三到四个文件",
            "",
            "| 文件 | 是什么 |",
            "|---|---|",
            "| `NNN_动作.jpg` | 那一刻的可视区截图 |",
            "| `NNN_动作.html.gz` | 那一刻的 DOM（`<script>` 已清空，gzip） |",
            "| `NNN_动作.elements.txt` | 当时可见的元素清单（坐标/文字/属性/路径） |",
            "| `events.jsonl` | **最有用的**：每条记录的完整信息，含「页面多了什么、少了什么」 |",
            "",
            "`network.jsonl` 记录每一步触发的接口：URL（参数值已去掉）、"
            "状态码、响应结构（只有字段名和类型，没有真实值）。",
            "",
            "## 隐私",
            "",
            "- `<script>` 内容已全部清空",
            "- 请求头（Cookie / Authorization）一个字都没记",
            "- URL 的 query 只留参数名不留值",
            "- 接口响应只记结构不记内容",
            "- 密码输入框的内容和长度都不记；其它输入框记了内容，"
            "介意的话跑完在 `events.jsonl` 里搜 `\"kind\": \"input\"` 自己删掉",
            "- 截图和 DOM 里仍会有页面上本来就显示着的东西（昵称、笔记标题）",
            "",
            f"## 打包发回来\n\n```\nzip -r record_{channel}.zip {self.out.name}\n```",
        ]
        return "\n".join(lines)


async def wait_enter(prompt: str) -> None:
    print(f"\n{prompt}")
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)


async def main() -> int:
    parser = argparse.ArgumentParser(description="录制你在页面上的操作和页面的变化")
    parser.add_argument("--channel", default="xiaohongshu", choices=sorted(HOME))
    parser.add_argument("--account", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--max-snaps", type=int, default=150)
    parser.add_argument("--max-minutes", type=float, default=30.0)
    parser.add_argument("--no-shot", action="store_true", help="不存截图")
    parser.add_argument("--out", default="record_out")
    args = parser.parse_args()

    label = CHANNEL_LABELS.get(args.channel, args.channel)
    config = load_config()
    setup_logging("WARNING", Path(config.get("server.data_dir", "./data")) / "logs")
    config.ensure_dirs()
    # 录制必须有头——你要能看见页面才能操作
    config.set("browser.headless", False)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) / f"{args.channel}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 74}")
    print(f"{label} 操作录制")
    print(f"输出目录：{out.resolve()}")
    print(f"{'=' * 74}\n")

    db = init_db(config)
    await db.connect()
    manager = BrowserSessionManager(config, AccountRepository(db))
    session = PageSession(manager, args.channel, args.account)
    try:
        await session.start()
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 打不开浏览器：{exc}")
        print("   常见原因：这个平台还没有已登录的账号；或者该账号的登录窗口正开着")
        await db.close()
        return 1
    print(f"✓ 浏览器已启动，账号 {session.account_name}\n")

    holder: Dict[str, Any] = {}

    async def setup() -> None:
        page = session._page
        context = session._context
        rec = Recorder(page, out, API_HINT[args.channel],
                       max_snaps=args.max_snaps, shots=not args.no_shot)
        holder["rec"] = rec
        rec.probe.hook_network()
        # expose_binding 要在导航之前挂，而且要挂在 context 上——
        # 挂在 page 上的话，页面一跳转就没了
        await context.expose_binding("__smcRecord", rec.on_record)
        await context.add_init_script(
            RECORDER_JS.replace("__SETTLE_MS__", str(SETTLE_MS)))
        url = args.url or HOME[args.channel]
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # 首次导航发生在注入脚本之前，这里补一次
        await page.evaluate(RECORDER_JS.replace("__SETTLE_MS__", str(SETTLE_MS)))
        rec.prev_sig = await rec._signature()

    await SUBPROCESS_LOOP.run(setup())
    rec: Recorder = holder["rec"]

    print("-" * 74)
    print("现在在窗口里正常操作就行（搜索、点筛选、选排序、点开笔记、翻评论……）。")
    print("每次页面有变化都会在下面打一行；想停就回到这里按回车。")
    print(f"上限：{args.max_snaps} 条记录 / {args.max_minutes:.0f} 分钟。")
    print("-" * 74)
    print(f"  {'#':>3}  {'动作':<6} {'元素':<52} {'文字':<16} 变化")

    stop = asyncio.create_task(wait_enter(""))
    deadline = time.time() + args.max_minutes * 60
    while not stop.done():
        if time.time() > deadline:
            print("\n到时间了，停止录制")
            break
        if rec.seq >= args.max_snaps:
            print(f"\n已录满 {args.max_snaps} 条，停止录制")
            break
        await asyncio.sleep(0.5)
    rec.stopped = True
    stop.cancel()

    rec.write_files(args.channel)
    try:
        await session.close()
    finally:
        await db.close()

    files = sorted(p.name for p in out.iterdir())
    print(f"\n{'=' * 74}")
    print(f"完成：{len(rec.events)} 条记录，{len(files)} 个文件")
    print(f"目录：{out.resolve()}")
    print("先看 SUMMARY.md 和 events.jsonl，然后把整个目录打包发回来")
    print(f"{'=' * 74}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已中断")
