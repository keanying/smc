"""（离线自检，不联网、不要账号、不要数据库）record_page.py 的机器部分。

    python verify_record_offline.py


要验的是"录下来的东西到底有没有用"，具体说是四条：
  1. **点击**记得下来（点了哪个元素、第几次点它、坐标）
  2. **hover 引起的变化**记得下来——这正是分步快照拍不到的那类事
  3. "页面多了什么、少了什么"算得对（这是给我看因果关系用的那一栏）
  4. 脱敏没漏（script 内容、query 值、响应内容、密码）
"""
import asyncio, gzip, http.server, json, socket, sys, threading
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from playwright.async_api import async_playwright

import record_page as R

CHROMIUM = __import__("os").environ.get("SMC_VERIFY_CHROMIUM", "")
OUT = Path(__import__("tempfile").mkdtemp(prefix="record_selfcheck_"))

PAGE = """<!doctype html><meta charset=utf-8><title>盘山风景区 - 搜索结果</title>
<style>body{font-family:sans-serif;margin:0;height:2400px}
.filter{cursor:pointer;padding:8px 12px;border:1px solid #ddd;
  position:absolute;right:20px;top:20px}
.panel{display:none;position:absolute;right:20px;top:60px;width:420px;
  height:400px;background:#fff;border:1px solid #eee;z-index:5}
.panel.open{display:block}
.note-item{width:240px;height:300px;border:1px solid #eee;margin:10px;
  display:inline-block}
.mask{display:none;position:fixed;inset:0;background:#fff;z-index:9}
.mask.open{display:block}
</style>
<body>
<input id=q placeholder="搜索"><input id=pwd type=password placeholder="密码">
<div class=filter id=filter><span>筛选</span></div>
<div class=panel id=panel>
  <div>排序依据</div><span class=opt>综合</span><span class=opt>最新</span>
  <div>发布时间</div><span class=opt>一天内</span><span class=opt>半年内</span>
</div>
<section id=list>
  <div class="note-item" data-note-id="aaa1">笔记一</div>
  <div class="note-item" data-note-id="bbb2">笔记二</div>
</section>
<div class=mask id=mask><div class=close id=close>关闭</div>笔记详情</div>
<script>
window.__SECRET__ = {token: "不该外泄"};
const panel = document.getElementById('panel');
const filter = document.getElementById('filter');
// 真站点行为：hover 展开，移开收起
filter.onmouseenter = () => panel.classList.add('open');
// 鼠标移进面板里也算"还在上面"——真站点就是这样，用户正是这么从入口
// 往下移到「最新」再点的。只在 hover 入口时才保持展开的话，
// 固定件比真实情况苛刻，测出来的失败是假的。
const maybeClose = () => setTimeout(() => {
  if (!filter.matches(':hover') && !panel.matches(':hover'))
    panel.classList.remove('open');
}, 60);
filter.onmouseleave = maybeClose;
panel.onmouseleave = maybeClose;
for (const el of document.querySelectorAll('.opt')) {
  el.onclick = () => fetch('/api/sns/web/v2/search/notes?sort=' +
                           encodeURIComponent(el.textContent) + '&token=SECRET123');
}
for (const el of document.querySelectorAll('.note-item')) {
  el.onclick = () => {
    document.getElementById('mask').classList.add('open');
    fetch('/api/sns/web/v1/feed?note_id=' + el.dataset.noteId);
  };
}
document.getElementById('close').onclick =
  () => document.getElementById('mask').classList.remove('open');
</script></body>"""

API_BODY = {"success": True, "code": 0,
            "data": {"has_more": True, "items": [
                {"id": "aaa1", "xsec_token": "TOKEN_SECRET",
                 "note_card": {"display_title": "盘山秋色", "time": 1756000000}}]}}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.startswith("/api/"):
            body = json.dumps(API_BODY).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


async def main():
    import shutil
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    server, port = serve()
    base = f"http://127.0.0.1:{port}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, **({"executable_path": CHROMIUM} if CHROMIUM else {}))
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        rec = R.Recorder(page, OUT, ["/api/sns/web/", "/rest/"], max_snaps=50, shots=True)
        rec.probe.hook_network()
        await context.expose_binding("__smcRecord", rec.on_record)
        js = R.RECORDER_JS.replace("__SETTLE_MS__", str(R.SETTLE_MS))
        await context.add_init_script(js)
        await page.goto(base, wait_until="networkidle")
        rec.prev_sig = await rec._signature()

        print("=== 模拟一段真人操作 ===")
        # 1) 输入（含密码框，验脱敏）
        await page.fill("#q", "盘山风景区")
        await page.fill("#pwd", "hunter2")
        await page.wait_for_timeout(900)
        # 2) 鼠标移到筛选上（面板 hover 出来）
        await page.hover("#filter")
        await page.wait_for_timeout(1200)
        # 3) 往下移到「最新」点一下。
        # ⚠️ 用 mouse.move(steps=) 分步挪，不用 page.click(selector)——
        # 后者是**瞬移**，会先在入口上触发 mouseleave，面板在指针落地前就收了
        # （第一版就是这么失败的，报 "element is not visible"）。
        # 这也正是采集器里改用坐标点击的原因。
        box = await page.locator("text=最新").bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2,
                              box["y"] + box["height"] / 2, steps=10)
        await page.wait_for_timeout(200)
        await page.mouse.click(box["x"] + box["width"] / 2,
                               box["y"] + box["height"] / 2)
        await page.wait_for_timeout(1200)
        # 4) 点开一条笔记
        await page.click('[data-note-id="aaa1"]')
        await page.wait_for_timeout(1200)
        # 5) 关掉
        await page.click("#close")
        await page.wait_for_timeout(1200)
        # 6) 滚动
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(1200)

        rec.stopped = True
        events = rec.events
        print(f"   录到 {len(events)} 条")
        for e in events:
            el = e.get("target") or {}
            print(f"     {e['seq']:>2}. {e['kind']:<6} {(el.get('path') or '-')[:44]:<44}"
                  f" +{e['added_total']} -{e['removed_total']}")

        kinds = [e["kind"] for e in events]
        print("\n=== 1. 点击记下来了吗（含第几次点） ===")
        clicks = [e for e in events if e["kind"] == "click"]
        assert clicks, "一次点击都没记到"
        first = clicks[0]
        print(f"   {first['target']['path']}  坐标=({first.get('x')},{first.get('y')})"
              f"  第 {first.get('nth')} 次点它")
        assert first.get("x") is not None and first.get("nth") == 1
        print("   ✓ 元素路径、坐标、点击次数都有")

        print("\n=== 2. hover 引起的变化记下来了吗（分步快照拍不到的那类） ===")
        hovers = [e for e in events if e["kind"] == "hover"]
        if not hovers:
            print("   录到的全部记录（排查用）：")
            for e in events:
                print("     ", json.dumps({k: e[k] for k in
                      ("seq", "kind", "added_total", "removed_total")},
                      ensure_ascii=False), (e.get("target") or {}).get("path"))
        assert hovers, "hover 一条都没记到——那面板是怎么出来的就说不清了"
        h = hovers[0]
        added = " ".join(h["added"])
        print(f"   移到 {h['target']['path']} 之后，页面多了：{added[:100]}")
        assert "filter" in (h["target"]["path"] or ""), "起因元素认错了"
        assert "排序依据" in added, "面板内容没被算进「多了什么」"
        print("   ✓ 「移到 div.filter 上 → 出现『排序依据』」这个因果关系记下来了")

        print("\n=== 3. 多了什么 / 少了什么 算得对吗 ===")
        opened = [e for e in events
                  if e["kind"] == "click" and any("mask" in a for a in e["added"])]
        closed = [e for e in events
                  if e["kind"] == "click" and any("mask" in r for r in e["removed"])]
        print(f"   开弹窗的点击 {len(opened)} 次，关弹窗的点击 {len(closed)} 次")
        assert opened, "点开笔记之后没记到 mask 出现"
        assert closed, "关闭之后没记到 mask 消失"
        print("   ✓ 出现和消失都算对了")

        print("\n=== 4. 滚动和输入 ===")
        print(f"   动作种类：{sorted(set(kinds))}")
        assert "scroll" in kinds, "滚动没记到"
        inputs = [e for e in events if e["kind"] == "input"]
        vals = [e.get("value") for e in inputs]
        print(f"   输入记录：{vals}")
        assert any(v == "盘山风景区" for v in vals), "搜索词没记到"
        assert not any("hunter2" in str(v) for v in vals), "密码被记下来了！"
        assert any("密码" in str(v) for v in vals), "密码框应该留一条『不记录』的痕迹"
        print("   ✓ 搜索词记了，密码没记")

        print("\n=== 3a. 接口前缀写错就什么都记不到 ===")
        # 快手实测踩过：API_HINT 里只写了 "/graphql"（MediaCrawler 时代的老接口），
        # 而现在的快手 web 走 /rest/v/...，于是 network.jsonl 一直是 0 字节，
        # 录了半天一个接口都没记到——"静悄悄什么都没有"最难发现。
        assert isinstance(R.API_HINT["kuaishou"], list), "接口前缀应该支持多个"
        assert "/rest/" in R.API_HINT["kuaishou"], \
            "快手现在走 /rest/v/...，只认 /graphql 会一个接口都记不到"
        net = (OUT / "network.jsonl").read_text(encoding="utf-8")
        assert net.strip(), "接口一个都没记到"
        recs = [json.loads(l) for l in net.splitlines() if l.strip()]
        print(f"   记到 {len(recs)} 个接口请求；带请求体结构的 "
              f"{sum(1 for r in recs if 'request_shape' in r)} 个")
        assert R.VERSION, "没有版本号——发回来的录制没法确认是哪一版录的"
        print(f"   录制器版本 {R.VERSION}（会写进 SUMMARY.md 顶上）")
        print("   ✓ 多前缀生效、接口记到了、版本号有了")

        print("\n=== 3b. 手快的时候不许丢动作 ===")
        # 用户实测：搜索完点作品、关联播、翻评论，录下来只有 5 条。
        # 原因是 on_record 里 `if self.busy: return` —— 而 busy 期间正是在
        # 截图 + 取 DOM + 列元素，一次一两秒，这期间的操作全丢。
        before = len(rec.events)
        was_stopped, rec.stopped = rec.stopped, False   # 上一节把它停了
        rec.busy = True                      # 假装正在截图
        fake = [{"actions": [{"kind": "click", "at": 0,
                              "el": {"tag": "div", "path": f"div#fast{i}",
                                     "text": "", "ownText": "", "attrs": {}},
                              "x": 1, "y": 1, "nth": 1}],
                 "hover": None, "url": base, "title": "t", "scrollY": 0}
                for i in range(5)]
        for item in fake:
            await rec.on_record(None, item)   # busy 期间连来 5 个
        print(f"   忙的时候来了 5 个动作，队列里攒了 {len(rec._inbox)} 个")
        assert len(rec._inbox) == 5, "忙的时候动作被丢掉了"
        rec.busy = False
        await rec.on_record(None, {"actions": [], "hover": None,
                                   "url": base, "title": "t", "scrollY": 0})
        got = len(rec.events) - before
        print(f"   处理完之后多记了 {got} 条")
        assert got >= 5, f"排队的动作没有被补记，只多了 {got} 条"
        assert not rec._inbox, "队列没抽干"
        rec.stopped = was_stopped
        print("   ✓ 忙的时候排队，不丢动作")

        print("\n=== 4a. 录制器必须是单文件、零依赖 ===")
        # 用户实测炸过：AttributeError: module 'probe_page' has no attribute 'Probe'
        # ——本地那份 probe_page.py 是旧的，录制器整个跑不起来，
        # 而这恰恰是最需要它的时候（页面改版、采集器失灵）。
        src = (Path(__file__).resolve().parent / "record_page.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines()
                          if not l.lstrip().startswith("#"))
        assert "import probe_page" not in code, \
            "record_page.py 又去 import probe_page 了——旁边那个文件版本不对就会整个废掉"
        for name in ("Probe", "COLLECT_JS", "render_elements", "scrub_html"):
            assert hasattr(R, name), f"{name} 没内联进来"
        print("   ✓ 不再 import probe_page，Probe/COLLECT_JS/脱敏都在本文件里")

        print("\n=== 4b. 中途被打断也要留下能用的记录 ===")
        # 用户实测丢过一整份录制：操作全做完了，Ctrl+C 之后目录里只剩
        # 两帧快照，events.jsonl 和 SUMMARY.md 一个都没有——因为它们
        # 原来只在正常收工时才写。现在每记一条就立刻追加。
        partial = (OUT / "events.jsonl")
        assert partial.exists(), "还没收工就该有 events.jsonl 了（边录边写）"
        lines = [l for l in partial.read_text(encoding="utf-8").splitlines() if l.strip()]
        print(f"   收工之前 events.jsonl 已经有 {len(lines)} 行")
        assert len(lines) == len(events), \
            f"边录边写的行数（{len(lines)}）和实际记录数（{len(events)}）对不上"
        one = json.loads(lines[0])
        assert one.get("kind") and "seq" in one, "追加的记录不是完整的一条"
        print("   ✓ 进程哪怕这时候被强杀，已经录到的部分也在盘上")

        print("\n=== 5. 落盘的文件 + 脱敏 ===")
        rec.write_files("xiaohongshu")
        names = sorted(p.name for p in OUT.iterdir())
        print(f"   {len(names)} 个文件，例如：{names[:4]}")
        assert (OUT / "events.jsonl").exists() and (OUT / "SUMMARY.md").exists()
        gz = sorted(OUT.glob("*.html.gz"))
        assert gz, "DOM 没存"
        html = gzip.open(gz[0], "rt", encoding="utf-8").read()
        assert "不该外泄" not in html and "已清空" in html, "script 内容没清干净"
        assert 'data-note-id="aaa1"' in html, "DOM 结构被清过头了"
        net = (OUT / "network.jsonl").read_text(encoding="utf-8")
        assert "SECRET123" not in net and "TOKEN_SECRET" not in net, "接口记录漏了敏感值"
        assert "sort=<值>" in net, "参数名应该留着"
        assert "display_title" in net, "响应结构应该记下来"
        assert "盘山秋色" not in net, "响应的真实内容不该记"
        raw = (OUT / "events.jsonl").read_text(encoding="utf-8")
        assert "hunter2" not in raw
        print("   ✓ script 清空、query 只留参数名、响应只留结构、密码没落盘")

        print("\n=== 6. SUMMARY.md 能看懂 ===")
        summary = (OUT / "SUMMARY.md").read_text(encoding="utf-8")
        print("   " + "\n   ".join(summary.splitlines()[:5]))
        assert "操作序列" in summary and "隐私" in summary
        print("   ✓ 有了")

        await browser.close()
    server.shutdown()
    print(f"\n输出在 {OUT}")
    print("全部通过 ✓")


asyncio.run(main())
