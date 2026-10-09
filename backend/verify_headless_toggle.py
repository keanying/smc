"""（离线自检，不联网、不需要账号）无头模式下「没找到多列」「筛选点了没反应」的复现与修复验证。

用真的无头 Chromium 跑真的 _ensure_grid_mode / _apply_filters，
页面是按抖音搜索页的时序仿的固定件：
  · 「多列」开关 2.0 秒后才挂到工具栏（无头跑得快，探测会赶在它前面）
  · 「筛选」按钮一开始就在，但点击处理器 2.5 秒后才绑（第一下点空）
  · 点「多列」之后按钮文案翻成「单列」（抖音是"显示对面模式"的语义）
"""
import asyncio, os, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from playwright.async_api import async_playwright
from app.collectors.douyin_browser import DouyinBrowserCollector
from app.collectors.base import CollectContext
from app.core.search_filters import SearchFilters

#: 留空就用 Playwright 自己找到的 Chromium（Windows 上就该留空）
CHROMIUM = os.environ.get("SMC_VERIFY_CHROMIUM", "")

HTML = """<!doctype html><meta charset=utf-8>
<style>body{font-family:sans-serif}#panel{display:none}
#panel.on{display:block}.grp{margin:6px 0}</style>
<body>
<div id=bar><span id=filter>筛选</span></div>
<div id=panel>
  <div class=grp><span>排序依据</span> <span class=opt>综合排序</span>
       <span class=opt>最新发布</span></div>
  <div class=grp><span>发布时间</span> <span class=opt>不限</span>
       <span class=opt>半年内</span></div>
</div>
<script>
window.__clicks = 0; window.__bound = false;

// 「多列」开关 2 秒后才出现
setTimeout(() => {
  const s = document.createElement('span');
  s.id = 'mode'; s.textContent = '多列';
  s.onclick = () => { s.textContent = s.textContent === '多列' ? '单列' : '多列'; };
  document.getElementById('bar').appendChild(s);
}, 2000);

// 「筛选」的事件处理器 2.5 秒后才绑上——在那之前点了也没用
setTimeout(() => {
  window.__bound = true;
  document.getElementById('filter').onclick =
    () => document.getElementById('panel').classList.add('on');
}, 2500);
document.getElementById('filter').addEventListener('click',
  () => { window.__clicks++; }, true);
</script></body>"""


class Log:
    def info(s, m, *a, **k): pass
    warning = warn = error = debug = info


def make(page):
    c = DouyinBrowserCollector.__new__(DouyinBrowserCollector)
    c._page = page
    c._replace_next = False
    c._run = lambda coro: coro          # 测试里就一个循环，直接 await
    return c


async def fresh(browser, html=None):
    """每个场景一张干净的新页面。

    ⚠️ 不要在同一张页面上连着 set_content 两次：上一份文档里
    还没到点的 setTimeout 会在新文档上再挂一个「多列」，
    于是页面上同时出现两个开关——那是固定件造出来的假象，不是被测代码的问题。
    """
    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    await page.set_content(html if html is not None else HTML)
    return page


async def scenario(page, collector, ctx, logs):
    logs.clear()
    t0 = time.monotonic()
    grid = await collector._ensure_grid_mode(ctx)
    label = await page.evaluate(
        "() => (document.getElementById('mode')||{}).textContent || '(没有)'")
    return grid, label, time.monotonic() - t0


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True,
            **({"executable_path": CHROMIUM} if CHROMIUM else {}))
        logs = []
        ctx = CollectContext(scenic_id="S1", scenic_name="盘山", logger=Log())
        ctx.log = lambda m, level="info": logs.append(f"[{level}] {m}")
        ctx.filters = SearchFilters(sort="latest", publish_within="half_year")

        # ---- 0. 先证明"老写法"在这个固定件上确实会失败 ----
        page = await fresh(b)
        old_ok = await page.get_by_text("多列", exact=True).first.is_visible(timeout=3000)
        print("=== 0. 老写法 is_visible(timeout=3000) ===")
        print(f"   返回 {old_ok} —— 按钮 2 秒后才出现，老写法直接判了「没找到」")
        assert old_ok is False, "固定件没复现出问题，后面的验证就没意义了"

        # ---- 1. 新写法：等得到，并且真的切过去了 ----
        print("\n=== 1. _ensure_grid_mode：等工具栏挂上来 ===")
        page = await fresh(b)
        c = make(page)
        ok, label, dt = await scenario(page, c, ctx, logs)
        print(f"   返回 {ok}，{dt:.1f}s 后开关文案 = {label!r}")
        for l in logs: print(f"   {l}")
        assert ok is True, "还是没切到多列"
        assert label == "单列", "点是点了，但没真的切过去（抖音切换后文案应变「单列」）"
        assert c._replace_next is True, "切模式会重搜，必须把上一批作废"
        print("   ✓ 等到了、点到了、并且核对过确实切换成功")

        # ---- 2. 已经是多列：不该再点一次把它切回去 ----
        print("\n=== 2. 已经是多列（开关显示「单列」）：不能再点 ===")
        page = await fresh(b, HTML.replace("'多列';", "'单列';"))
        c = make(page)
        logs.clear()
        ok = await c._ensure_grid_mode(ctx)
        label = await page.evaluate("() => document.getElementById('mode').textContent")
        print(f"   返回 {ok}，开关文案 = {label!r}")
        for l in logs: print(f"   {l}")
        assert ok is True and label == "单列", "把已经是多列的页面又切回单列了"
        assert any("已经是多列" in l for l in logs), "日志没说清是哪种情况"
        print("   ✓ 认出来了，没有误点")

        # ---- 3. 真的没有这个开关：报警要说清后果，且不能崩 ----
        print("\n=== 3. 页面上根本没有列表模式开关 ===")
        c.GRID_TOGGLE_TIMEOUT = 1.5           # 别真等 12 秒
        page = await fresh(b, "<!doctype html><meta charset=utf-8><body><span>筛选</span></body>")
        c = make(page); c.GRID_TOGGLE_TIMEOUT = 1.5
        logs.clear()
        ok = await c._ensure_grid_mode(ctx)
        print(f"   返回 {ok}")
        for l in logs: print(f"   {l}")
        assert ok is False
        assert any("stream/" in l for l in logs), "没告诉用户数据会走哪条兜底路径"
        c.GRID_TOGGLE_TIMEOUT = 12.0

        # ---- 4. 筛选：第一下点空的情况下也要开得起来 ----
        print("\n=== 4. _apply_filters：事件绑定晚于渲染，第一下点空 ===")
        page = await fresh(b)
        c = make(page)
        logs.clear()
        picked = []
        async def fake_pick(ctx_, group, label_):
            picked.append((group, label_)); return True
        c._pick_filter_option = fake_pick
        t0 = time.monotonic()
        ok = await c._apply_filters(ctx)
        dt = time.monotonic() - t0
        clicks, bound = await page.evaluate("() => [window.__clicks, window.__bound]")
        panel_on = await page.evaluate(
            "() => document.getElementById('panel').classList.contains('on')")
        print(f"   返回 {ok}，耗时 {dt:.1f}s，面板展开={panel_on}，"
              f"实际点了 {clicks} 次，选了 {picked}")
        for l in logs: print(f"   {l}")
        assert ok is True and panel_on, "面板没开起来"
        assert picked == [("排序依据", "最新发布"), ("发布时间", "半年内")]
        assert clicks <= 2, f"点了 {clicks} 次，重试太多了"
        # 「事件什么时候绑上」页面不会告诉我们，固定 sleep 再长也只是赌。
        # 所以判据不是"一次点开"，而是：**一定要点开**，且第一次点空
        # 不再当成告警刷屏（那是这个页面的常态）。
        assert not any(l.startswith("[warn]") for l in logs), \
            f"重试成功了却还在报警：{[l for l in logs if l.startswith('[warn]')]}"
        assert any("第 1 次点「筛选」没打开" in l for l in logs)
        print("   ✓ 重试后打开，且第一次点空只记 info，不再刷 warn")

        await b.close()
        print("\n全部通过 ✓")

asyncio.run(main())
