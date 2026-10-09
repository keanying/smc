"""前端冒烟测试：用真实 Chromium 打开已构建的前端，走一遍主要页面。

验证的是「前端能连上后端并正确渲染」这件事——构建通过不代表运行时不报错，
比如接口字段名对不上、响应解包写错，只有真跑起来才会暴露。

跳过条件：前端没构建（frontend/dist 不存在）或沙箱没有 Chromium。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

pytestmark = pytest.mark.skipif(
    not FRONTEND_DIST.exists() or not Path(CHROMIUM).exists(),
    reason="前端未构建（npm run build）或缺少 Chromium",
)

TEST_DB = "scenic_media_ui_smoke"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _drop_test_db() -> None:
    """跑之前把冒烟库删掉，让每一轮都从空库开始。

    ⚠️ 这不是洁癖，是**可重复性**：这些用例断言的是"编辑任务不该多出一条"
    「Cookie 导入前应显示未登录」这类**全局状态**。上一轮留下的数据会让
    第二次跑直接红 4 个，而代码一个字没改——这种假红比不测还糟，
    查半天最后发现是残留，下次就不敢信测试结果了。

    删库失败不拦着往下跑（可能是没有 DROP 权限），
    最多退回原来那种"只有第一次准"的状态。
    """
    try:
        import pymysql
        import yaml

        cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml")
                             .read_text(encoding="utf-8"))
        m = cfg["mysql"]
        conn = pymysql.connect(host=m["host"], port=int(m["port"]),
                               user=os.getenv("SMC_MYSQL_USER") or m["user"],
                               password=os.getenv("SMC_MYSQL_PASSWORD") or m["password"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        finally:
            conn.close()
    except Exception as exc:            # noqa: BLE001
        print(f"[ui_smoke] 清空 {TEST_DB} 失败（继续跑，但残留数据可能造成假红）：{exc}")


@pytest.fixture(scope="module")
def server():
    """起一个真实的 uvicorn 进程，前端和 API 都从它出。"""
    _drop_test_db()
    port = _free_port()
    env = {
        **os.environ,
        "SMC_MYSQL_DATABASE": TEST_DB,
        "SMC_REDIS_ENABLED": "false",
        "SMC_SCHEDULER_ENABLED": "false",
        "SMC_BROWSER_EXECUTABLE_PATH": CHROMIUM,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        if process.poll() is not None:
            output = (process.stdout.read() or b"").decode("utf-8", "ignore")
            pytest.fail(f"服务启动失败：\n{output[-2000:]}")
        time.sleep(0.5)
    else:
        process.kill()
        pytest.fail("服务 60 秒内没起来")

    # 铺一点数据，页面才有东西可渲染
    httpx.post(f"{base}/api/scenics", json={
        "scenic_id": "UI001", "scenic_name": "UI测试景区",
        "province": "浙江", "city": "杭州",
    }, timeout=10)
    httpx.post(f"{base}/api/scenics/UI001/keywords",
               json={"keywords": ["西湖", "断桥残雪"]}, timeout=10)
    httpx.post(f"{base}/api/scenics/UI001/targets", json={
        "channel": "ctrip", "target_type": "poi",
        "target_id": "32289", "target_name": "西湖",
    }, timeout=10)
    httpx.post(f"{base}/api/tasks", json={
        "task_name": "UI测试任务", "scenic_id": "UI001",
        "channels": ["ctrip"], "collect_type": "poi", "schedule_type": "cron",
        "cron_expression": "0 3 * * *",
    }, timeout=10)

    yield base

    process.kill()
    process.wait(timeout=10)


@pytest.fixture(scope="module")
def page_factory(server):
    from playwright.async_api import async_playwright

    async def _run(routine):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, executable_path=CHROMIUM,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            page.on("console", lambda msg: errors.append(f"console: {msg.text}")
                    if msg.type == "error" else None)
            # 控制台的 "Failed to load resource" 不带 URL，单独记一份带 URL 的
            page.on("response", lambda resp: errors.append(f"http {resp.status}: {resp.url}")
                    if resp.status >= 400 else None)
            try:
                return await routine(page, errors)
            finally:
                await context.close()
                await browser.close()

    return _run


def _run(page_factory, routine):
    return asyncio.get_event_loop().run_until_complete(page_factory(routine))


@pytest.mark.asyncio
async def test_all_pages_render_without_errors(server, page_factory):
    """逐个打开主要页面，确认标题渲染出来且控制台没有报错。"""
    pages = [
        ("/#/", "概览"),
        ("/#/scenics", "景区管理"),
        ("/#/tasks", "任务管理"),
        ("/#/tasks/new", "新建采集任务"),
        ("/#/data", "数据中心"),
        ("/#/accounts", "账号管理"),
        ("/#/settings", "系统设置"),
    ]

    async def routine(page, errors):
        results = []
        for path, expected_title in pages:
            errors.clear()
            await page.goto(f"{server}{path}", wait_until="networkidle")
            await page.wait_for_timeout(700)
            heading = await page.locator("h2.page-title").first.inner_text()
            body = await page.locator("body").inner_text()
            results.append({
                "path": path,
                "heading": heading,
                "expected": expected_title,
                "errors": [e for e in errors if "favicon" not in e.lower()],
                "body": body,
            })
        return results

    results = await page_factory(routine)

    for item in results:
        assert item["expected"] in item["heading"], \
            f"{item['path']} 标题不对：{item['heading']}"
        assert not item["errors"], \
            f"{item['path']} 控制台报错：{item['errors'][:3]}"


@pytest.mark.asyncio
async def test_scenic_page_shows_seeded_data(server, page_factory):
    """景区列表要能显示后端的数据，展开后能看到关键字。"""
    async def routine(page, errors):
        await page.goto(f"{server}/#/scenics", wait_until="networkidle")
        await page.wait_for_timeout(900)
        body = await page.locator("body").inner_text()
        # 展开第一行看关键字
        expander = page.locator("td.el-table__expand-column .el-table__expand-icon").first
        await expander.click()
        await page.wait_for_timeout(900)
        expanded = await page.locator("body").inner_text()
        return body, expanded

    body, expanded = await page_factory(routine)
    assert "UI测试景区" in body
    assert "UI001" in body
    assert "西湖" in expanded, "展开后没看到关键字"
    assert "断桥残雪" in expanded


@pytest.mark.asyncio
async def test_new_task_page_pulls_keywords_and_previews_cron(server, page_factory):
    """新建任务页：从景区带入关键字 + cron 下次执行时间预览。"""
    async def routine(page, errors):
        await page.goto(f"{server}/#/tasks/new", wait_until="networkidle")
        await page.wait_for_timeout(800)

        # 选景区
        await page.locator(".el-select").first.click()
        await page.wait_for_timeout(400)
        await page.locator(".el-select-dropdown__item").first.click()
        await page.wait_for_timeout(800)

        # 勾携程
        await page.get_by_text("携程", exact=False).first.click()
        await page.wait_for_timeout(500)

        # 切到 cron 并填表达式
        await page.get_by_text("cron 表达式", exact=False).first.click()
        await page.wait_for_timeout(400)
        cron_input = page.locator('input[placeholder="0 2 * * *"]')
        await cron_input.fill("0 3 * * *")
        await page.wait_for_timeout(1500)

        return await page.locator("body").inner_text()

    body = await page_factory(routine)
    # 预览应该给出 03:00:00 的下次执行时间
    assert "03:00:00" in body, f"cron 预览没出来：{body[-600:]}"


@pytest.mark.asyncio
async def test_tasks_page_lists_cron_task(server, page_factory):
    async def routine(page, errors):
        await page.goto(f"{server}/#/tasks", wait_until="networkidle")
        await page.wait_for_timeout(900)
        return await page.locator("body").inner_text()

    body = await page_factory(routine)
    assert "UI测试任务" in body
    assert "cron" in body.lower() or "0 3 * * *" in body
    assert "携程" in body


@pytest.mark.asyncio
async def test_settings_page_masks_secrets(server, page_factory):
    """系统设置页不能把密钥明文回显——后端返回的是 ***，前端要清成空。"""
    async def routine(page, errors):
        await page.goto(f"{server}/#/settings", wait_until="networkidle")
        await page.wait_for_timeout(900)
        secret_input = page.locator('input[placeholder="留空表示不修改"]').first
        value = await secret_input.input_value()
        body = await page.locator("body").inner_text()
        return value, body

    value, body = await page_factory(routine)
    assert value == "", f"密钥输入框不该回显内容，实际是 {value!r}"
    assert "快代理" in body


# ---------------------------------------------------------------------------
# 需求 4 / 5 的界面部分
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_task_page_switches_to_per_channel_counts(server, page_factory):
    """新建任务页：作品型和点评型平台的字段不一样。

    ⚠️ 平台已经改成**单选**（一个任务只跑一个平台），所以这里分两次选，
    各看各的面板，而不是一次勾两个。
    """
    async def routine(page, errors):
        await page.goto(f"{server}/#/tasks/new", wait_until="networkidle")
        await page.wait_for_timeout(800)

        radios = page.locator(".el-radio-group .el-radio-button")
        # 微博：有作品，两层
        await radios.filter(has_text="微博").first.click()
        await page.wait_for_timeout(400)
        await page.get_by_text("按平台分别设置", exact=True).first.click()
        await page.wait_for_timeout(700)
        weibo_panel = await page.locator(".collect-params").inner_text()

        # 携程：只有点评一层（文案是「携程（点评）」，按容器筛不用精确匹配）
        await radios.filter(has_text="携程").first.click()
        await page.wait_for_timeout(700)
        ctrip_panel = await page.locator(".collect-params").inner_text()

        return weibo_panel, ctrip_panel, [e for e in errors if "favicon" not in e.lower()]

    weibo_panel, ctrip_panel, errors = await page_factory(routine)
    assert not errors, errors[:3]
    # 微博是两层：作品数 + 每作品评论数
    assert "每关键字作品数" in weibo_panel, weibo_panel
    assert "每作品评论数" in weibo_panel, weibo_panel
    assert "作品 + 作品下的评论两层" in weibo_panel, weibo_panel
    # 携程只有一层
    assert "采集点评数" in ctrip_panel, ctrip_panel
    assert "只有景区点评一层" in ctrip_panel, ctrip_panel


@pytest.mark.asyncio
async def test_task_can_be_edited_without_recreating(server, page_factory):
    """任务列表的「编辑」：改运行模式和数量，任务 ID 不变。"""
    before = httpx.get(f"{server}/api/tasks", params={"keyword": "UI测试任务"}, timeout=10)
    task_id = before.json()["data"]["items"][0]["task_id"]

    async def routine(page, errors):
        await page.goto(f"{server}/#/tasks", wait_until="networkidle")
        await page.wait_for_timeout(900)

        await page.get_by_role("button", name="编辑").first.click()
        await page.wait_for_timeout(800)

        dialog = page.locator(".el-dialog")
        visible = await dialog.is_visible()

        # 改成固定间隔
        await dialog.get_by_text("固定间隔", exact=True).first.click()
        await page.wait_for_timeout(500)

        # 改采集点评数（携程任务，只有点评一层）。
        # 按 label 定位，不要用 .last —— 弹窗里最后一个数字输入框是「最深评论层级」
        number_input = (
            dialog.locator(".el-form-item")
            .filter(has_text="景区点评数")
            .locator("input")
            .first
        )
        await number_input.fill("77")
        await number_input.press("Enter")
        await page.wait_for_timeout(400)

        await dialog.get_by_role("button", name="保存").click()
        await page.wait_for_timeout(1500)

        return visible, [e for e in errors if "favicon" not in e.lower()]

    visible, errors = await page_factory(routine)
    assert visible, "编辑弹窗没打开"
    assert not errors, errors[:3]

    after = httpx.get(f"{server}/api/tasks", params={"keyword": "UI测试任务"}, timeout=10)
    items = after.json()["data"]["items"]
    assert len(items) == 1, "编辑不该产生第二条任务"
    assert items[0]["task_id"] == task_id, "任务 ID 变了，说明是重建而不是修改"
    assert items[0]["schedule_type"] == "interval"

    limits = {row["channel"]: row for row in items[0]["collect_limits"]}
    assert limits["ctrip"]["max_comments"] == 77, limits


def _seed_works(count: int) -> None:
    """直接往作品表塞几十条，把作品区撑到必须滚。

    走接口塞不进来——作品只有采集器会写。这里只是给布局测试造点高度。
    """
    import pymysql

    from app.core.config import load_config
    from app.db.tables import WORKS

    mysql = load_config(use_cache=False).mysql
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=TEST_DB, autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT IGNORE INTO `{WORKS}` "
                "(scenic_id, scenic_name, channel, work_id, title, author_name, publish_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
                [("ui-scenic", "冒烟景区", "douyin", f"ui-work-{i}",
                  f"布局冒烟用的作品 {i}", "冒烟作者") for i in range(count)],
            )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_data_center_layout(server, page_factory):
    """数据中心：筛选条钉在顶部，只有作品区自己滚，两个控件不许再混淆。

    用户原话：「列表高度 是设置评论翻页的吧」——控件叫「列表高度」、
    又跟作品分页并排放在页脚，谁看都会以为是评论的翻页设置。
    所以这里除了查元素在不在，还查**两条布局契约**：
      1. 页面自身不长高（.app-main 不出现滚动条），高度全给作品区
      2. 分页那一行里不许再出现「高度」两个字
    """
    _seed_works(40)   # 内容够多，"整页不滚"才是真结论而不是数据太少

    async def routine(page, errors):
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.goto(f"{server}/#/data", wait_until="networkidle")
        await page.wait_for_timeout(1200)
        pager = page.locator(".data-pager .el-pagination")

        # 页面本身别长高：外层 .app-main 才是滚动容器
        page_scrolls = await page.evaluate(
            "() => { const m = document.querySelector('.app-main');"
            " return m ? m.scrollHeight - m.clientHeight > 4 : true }"
        )
        list_overflow = await page.evaluate(
            "() => getComputedStyle(document.querySelector('.work-list')).overflowY"
        )
        # 作品区的底不能跑到屏幕外面去，否则"固定窗口"是假的
        list_bottom_in_view = await page.evaluate(
            "() => { const el = document.querySelector('.work-list');"
            " return el.getBoundingClientRect().bottom <= window.innerHeight + 2 }"
        )
        return {
            "stat_boxes": await page.locator(".stat-box").count(),
            "has_filter_bar": await page.locator(".filter-card .filter-row").count(),
            # 景区×平台明细面板已经去掉了，改成筛选下拉 + 彩色分布条
            "old_panel": await page.locator(".scenic-panel").count(),
            "head_text": await page.locator(".work-list-head").inner_text(),
            "pager_text": await page.locator(".data-pager").inner_text(),
            "pager_visible": await pager.is_visible(),
            "page_scrolls": page_scrolls,
            "list_overflow": list_overflow,
            "list_bottom_in_view": list_bottom_in_view,
            "errors": [e for e in errors if "favicon" not in e.lower()],
        }

    result = await page_factory(routine)
    assert not result["errors"], result["errors"][:3]
    assert result["stat_boxes"] == 4, "顶部统计条没渲染完整"
    assert result["has_filter_bar"] == 1, "筛选栏没渲染"
    assert result["old_panel"] == 0, "占地方的景区×平台明细面板应该已经去掉"
    # 没有数据时分页也要在（之前是 total 超过一页才显示，用户看不到页码控件）
    assert result["pager_visible"], "数据中心分页控件没渲染"

    # ---- 布局契约 ----
    assert result["list_overflow"] == "auto", "作品区必须是自己滚的窗口"
    assert not result["page_scrolls"], (
        "整页不该出现滚动条——筛选条会被滚走，用户改个条件要一路往回滚"
    )
    assert result["list_bottom_in_view"], "作品区的底跑到屏幕外了，等于没固定窗口"

    # ---- 别再混淆 ----
    assert "评论" in result["head_text"], "评论窗口高度的控件要说清楚是评论的"
    assert "列表高度" not in result["head_text"], (
        "「列表高度」这个叫法已经去掉了：用户会以为是评论翻页"
    )
    assert "高度" not in result["pager_text"], (
        "分页那一行只放作品分页，放高度控件就会跟翻页混在一起"
    )
    assert "作品分页" in result["pager_text"]


@pytest.mark.asyncio
async def test_work_images_go_through_the_proxy(server, page_factory):
    """作品图片必须指向 /api/media/image，不能直连平台 CDN。

    直连会被防盗链拦成 403：页面里一片"加载失败"，
    可把地址复制到浏览器又能正常打开（那样不带 Referer）——
    这个反差让人以为是采到的地址不对，其实是 Referer 的问题。
    """
    _seed_works_with_images()

    async def routine(page, errors):
        # 看**页面实际发出去的请求**，比查 DOM 稳：
        # el-image 加载失败时会把 <img> 换成错误占位，src 就查不到了，
        # 而"请求发到哪儿"才是这条测试真正关心的事。
        requested: list = []
        page.on("request", lambda req: requested.append(req.url))

        await page.goto(f"{server}/#/data", wait_until="networkidle")
        await page.wait_for_timeout(2000)
        return {
            "requested": list(requested),
            "errors": [e for e in errors if "favicon" not in e.lower()],
        }

    result = await page_factory(routine)
    urls = result["requested"]
    direct = [u for u in urls if "sinaimg.cn" in u and "/api/media/image" not in u]
    assert not direct, f"这些图片还在直连平台 CDN，会被防盗链拦掉：{direct[:3]}"
    proxied = [u for u in urls if "/api/media/image" in u]
    assert proxied, f"没有任何图片走代理，请求列表：{urls[-8:]}"
    assert "sinaimg.cn" in proxied[0], proxied[0]


def _seed_works_with_images() -> None:
    """塞一条带图片的作品，用来检查前端有没有走代理。"""
    import json as _json

    import pymysql

    from app.core.config import load_config
    from app.db.tables import WORKS

    mysql = load_config(use_cache=False).mysql
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=TEST_DB, autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT IGNORE INTO `{WORKS}` "
                "(scenic_id, scenic_name, channel, work_id, title, author_name, "
                " image_list, publish_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                ("ui-scenic", "冒烟景区", "weibo", "ui-work-with-image",
                 "带图的微博", "冒烟作者",
                 _json.dumps(["https://wx1.sinaimg.cn/mw2000/abc.jpg"])),
            )
    finally:
        conn.close()


#: ⚠️ 这个账号名每次跑都不一样，而且跑之前会把它的 profile 目录删掉。
#:
#: 原因：这条用例断言的是「导入之前应该显示未登录」，属于**全局状态**。
#: 账号的登录态有两处：数据库里的 cookies 列，以及磁盘上的浏览器 profile
#: （data/browser_profiles/<平台>/<账号名>/）。前者随冒烟库一起清掉了，
#: 后者不会——上一轮跑完留在那儿，下一轮这条就红，而代码一个字没改。
#: 靠"清干净"很难保证不漏，靠"每次换个没人用过的名字"才是真的独立。
_COOKIE_ACCOUNT = f"UI账号{int(time.time())}"


@pytest.mark.asyncio
async def test_account_cookie_dialog_imports_and_reports_state(server, page_factory):
    """账号页的 Cookie 弹窗：能看出没登录，粘贴导入后变成已登录。"""
    profile = PROJECT_ROOT / "data" / "browser_profiles" / "douyin" / _COOKIE_ACCOUNT
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    httpx.post(f"{server}/api/accounts", json={
        "channel": "douyin", "account_name": _COOKIE_ACCOUNT, "nickname": "冒烟",
    }, timeout=10)

    async def routine(page, errors):
        await page.goto(f"{server}/#/accounts", wait_until="networkidle")
        await page.wait_for_timeout(900)

        await page.get_by_role("button", name="Cookie", exact=True).first.click()
        await page.wait_for_timeout(900)

        dialog = page.locator(".el-dialog")
        before = await dialog.inner_text()

        # 粘贴一份带登录凭据的 Cookie 串
        await dialog.locator("textarea").fill(
            "sessionid=ui_smoke_session;sid_tt=ui_smoke_sid;uid_tt=ui_smoke_uid"
        )
        await dialog.get_by_role("button", name="导入 Cookie").click()
        await page.wait_for_timeout(2500)

        after = await dialog.inner_text()
        return before, after, [e for e in errors if "favicon" not in e.lower()]

    before, after, errors = await page_factory(routine)
    assert not errors, errors[:3]
    # 导入前：明确告诉用户没有登录凭据，并列出需要哪些 key
    assert "未登录" in before, before[:400]
    assert "sessionid" in before
    # 导入后：状态翻过来
    assert "已登录" in after, after[:400]

    state = httpx.get(
        f"{server}/api/accounts/douyin/{_COOKIE_ACCOUNT}/cookies", timeout=10
    ).json()["data"]
    assert state["logged_in"] is True
    assert "sessionid" in state["names"]


@pytest.mark.asyncio
async def test_new_task_shows_platform_specific_filters(server, page_factory):
    """新建任务页：排序选项按平台变；快手没有排序，要明说。"""
    async def routine(page, errors):
        await page.goto(f"{server}/#/tasks/new", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        radios = page.locator(".el-radio-group .el-radio-button")
        await radios.filter(has_text="小红书").first.click()
        await page.wait_for_timeout(800)
        with_xhs = await page.locator(".collect-params").inner_text()

        # 单选：直接点快手就换过去了
        await radios.filter(has_text="快手").first.click()
        await page.wait_for_timeout(800)
        with_ks = await page.locator(".collect-params").inner_text()

        return with_xhs, with_ks, [e for e in errors if "favicon" not in e.lower()]

    with_xhs, with_ks, errors = await page_factory(routine)
    assert not errors, errors[:3]

    # 小红书有排序和时间筛选
    assert "排序依据" in with_xhs, with_xhs[:500]
    assert "发布时间" in with_xhs
    # 快手接口没有排序，界面要如实说，而不是给个假的下拉框
    assert "接口不支持排序" in with_ks, with_ks[:500]
    assert "发布时间" in with_ks, "时间范围仍然要能设（本地过滤）"


@pytest.mark.asyncio
async def test_new_task_custom_date_range_appears(server, page_factory):
    """选「自定义日期区间」才显示日期选择器。"""
    async def routine(page, errors):
        await page.goto(f"{server}/#/tasks/new", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        await page.locator(".el-radio-group .el-radio-button").filter(
            has_text="微博").first.click()
        await page.wait_for_timeout(700)

        panel = page.locator(".collect-params")
        before = await panel.locator(".el-date-editor").count()

        # 按 label 定位「发布时间」那个下拉，别用 .last —— 表单里 select 不止一个
        time_item = panel.locator(".el-form-item").filter(has_text="发布时间").first
        await time_item.locator(".el-select").first.click()
        await page.wait_for_timeout(500)
        await page.locator(".el-select-dropdown__item:visible").filter(
            has_text="自定义日期区间").first.click()
        await page.wait_for_timeout(700)

        after = await panel.locator(".el-date-editor").count()
        return before, after, [e for e in errors if "favicon" not in e.lower()]

    before, after, errors = await page_factory(routine)
    assert not errors, errors[:3]
    assert before == 0, "默认不该显示日期选择器"
    assert after == 1, "选了自定义之后应该出现日期区间选择器"
