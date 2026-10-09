"""（离线自检，不联网、不需要账号、不需要数据库）采集实时画面：真浏览器 + 真接口 + 真 WebSocket。

不是"看代码觉得会通"，是真开一个无头 Chromium、真注册一个 LiveView、
真从接口和 WebSocket 里把画面取回来看。

重点验证四件事：
  1. 没人看的时候**不推流**——不然这个便利功能会一直向采集收税
  2. 有人连上就开始推，最后一个人走了就停
  3. 单帧接口能独立工作（WebSocket 被反代挡住时的兜底路径）
  4. 采集结束注销之后，接口和 WebSocket 都明确说"结束了"，而不是报错或挂住
"""
import asyncio, base64, os, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.api import live
from app.api.common import fail
from app.browser import live_view as lv
from app.core.subprocess_loop import SUBPROCESS_LOOP

#: 留空就用 Playwright 自己找到的 Chromium（Windows 上就该留空）
CHROMIUM = os.environ.get("SMC_VERIFY_CHROMIUM", "")

# 会自己动的页面：不动的话 screencast 不推帧，测出来的"没收到帧"是假象
PAGE = """<!doctype html><meta charset=utf-8>
<body style="font:40px sans-serif;padding:40px;background:#fff">
<div id=n>0</div>
<script>let i=0; setInterval(()=>{document.getElementById('n').textContent=++i;},120)</script>
</body>"""

state = {}


def on_browser_loop(coro):
    """把协程丢到浏览器循环上跑完再回来（测试是同步代码，没有自己的循环）。"""
    return asyncio.run_coroutine_threadsafe(coro, SUBPROCESS_LOOP.loop).result(120)


async def open_page():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, **({"executable_path": CHROMIUM} if CHROMIUM else {}))
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    await page.set_content(PAGE)
    state.update(pw=pw, browser=browser, ctx=ctx, page=page)
    return ctx, page


async def close_page():
    await state["browser"].close()
    await state["pw"].stop()


def is_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


def main():
    app = FastAPI()
    app.include_router(live.router)
    app.include_router(live.ws_router)

    # 和 main.py 一样把 HTTPException 包成 {code, data, message}，
    # 否则这里测到的错误体和真服务上的不是一个形状，验证就没意义了
    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content=fail(str(exc.detail), code=exc.status_code))

    ctx, page = on_browser_loop(open_page())

    view = lv.register(lv.LiveView(
        view_id=lv.make_view_id("T1", "douyin", "账号A"),
        page=page, context=ctx,
        task_id="T1", task_name="盘山-抖音", channel="douyin",
        account_name="账号A", scenic_name="盘山", engine="human"))
    view.note("搜索「盘山风景区」")

    client = TestClient(app)

    print("=== 1. 没人看的时候不推流 ===")
    assert view._cdp is None
    body = client.get("/api/live/views", params={"task_id": "T1"}).json()
    assert body["code"] == 0 and len(body["data"]) == 1, body
    info = body["data"][0]
    print(f"   列表里有：{info['task_name']} / {info['channel']} / 账号 {info['account_name']}")
    print(f"   step={info['step']!r}  streaming={info['streaming']}  viewers={info['viewers']}")
    assert info["streaming"] is False and info["viewers"] == 0
    assert info["step"] == "搜索「盘山风景区」"
    print("   ✓ 注册了但没开推流，对采集零开销")

    print("\n=== 2. 单帧接口（WebSocket 被反代挡住时的兜底） ===")
    r = client.get(f"/api/live/views/{info['view_id']}/frame.jpg")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert "no-store" in r.headers.get("cache-control", "")
    assert is_jpeg(r.content), "返回的不是一张完整 JPEG"
    print(f"   拿到 {len(r.content)} 字节的 JPEG，Cache-Control={r.headers['cache-control']}")
    assert view._cdp is None, "单帧路径不该顺手把推流打开"
    print("   ✓ 不依赖推流，独立可用；且不会顺手开推流")

    print("\n=== 3. 连上 WebSocket 才开始推流 ===")
    frames, metas = [], []
    with client.websocket_connect(f"/ws/live/{info['view_id']}") as ws:
        first = ws.receive_json()
        print(f"   状态帧：{first['state']} — {first['message']}")
        assert first["type"] == "status" and first["state"] == "streaming"
        deadline = time.time() + 20
        while time.time() < deadline and len(frames) < 3:
            msg = ws.receive_json()
            if msg["type"] == "frame":
                frames.append(msg)
            elif msg["type"] == "meta":
                metas.append(msg["data"])
        print(f"   收到 {len(frames)} 帧，尺寸 {frames[0]['width']}x{frames[0]['height']}，"
              f"首帧 {len(base64.b64decode(frames[0]['data']))} 字节")
        assert len(frames) >= 3, "推流没跟上"
        assert is_jpeg(base64.b64decode(frames[0]["data"]))
        assert view._cdp is not None, "有人在看却没开推流"
        # 画面确实在变，不是同一帧反复推
        assert len({f["data"] for f in frames}) > 1, "推的一直是同一帧"
        print("   ✓ 帧内容在变，说明推的是真实时画面")

    print("\n=== 4. 最后一个观众走了就停止推流 ===")
    for _ in range(40):
        if view._cdp is None:
            break
        time.sleep(0.1)
    print(f"   断开后 _cdp={view._cdp}")
    assert view._cdp is None, "观众走光了还在推，等于一直向采集收税"
    info2 = client.get("/api/live/views").json()["data"][0]
    assert info2["streaming"] is False and info2["viewers"] == 0
    print("   ✓ 停了，开销还给采集")

    print("\n=== 5. 元信息里带着 url / 步骤，画面才对得上日志 ===")
    if metas:
        print(f"   {metas[-1]['step']}  |  running={metas[-1]['running_seconds']}s")
        assert metas[-1]["step"] == "搜索「盘山风景区」"
    view.note("打开作品：盘山秋色")
    info3 = client.get("/api/live/views").json()["data"][0]
    assert info3["step"] == "打开作品：盘山秋色"
    print(f"   换一步之后：{info3['step']}")
    print("   ✓ 步骤能跟着走")

    print("\n=== 6. 采集结束之后：明确说结束，不报错也不挂住 ===")
    asyncio.run(lv.unregister(info["view_id"]))
    assert client.get("/api/live/views").json()["data"] == []
    r = client.get(f"/api/live/views/{info['view_id']}/frame.jpg")
    print(f"   单帧接口 -> {r.status_code} {r.json()['message']}")
    assert r.status_code == 404
    with client.websocket_connect(f"/ws/live/{info['view_id']}") as ws:
        msg = ws.receive_json()
        print(f"   WebSocket -> {msg['state']}：{msg['message']}")
        assert msg["state"] == "gone"
    print("   ✓ 两条路都给了能看懂的说法")

    on_browser_loop(close_page())
    print("\n全部通过 ✓")


main()
