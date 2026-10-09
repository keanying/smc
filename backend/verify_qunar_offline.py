"""去哪儿采集器的离线验证：起一个假的 sight.qunar.com，跑完整采集链路。

为什么要有它：真站点会限流、会弹验证页，靠它调试既慢又不可复现。
这里的页面结构**照抄上游测试里的真实快照**（`self.__next_f.push` 里那段
内嵌 JSON、分页链接、JSON-LD），不是我凭印象编的——编一个"看着像"的
结构，解析器照样能跑通，然后真站点上一条都采不到。

用法：python verify_qunar_offline.py
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

TOTAL_PAGES = 3
PER_PAGE = 4


def _comment_payload(page: int, index: int) -> str:
    cid = f"c{page}_{index}"
    return (
        'x:{"comment":{"id":"' + cid + '","user":"游客' + str(index) + '",'
        '"date":"2026-08-2' + str(index % 9) + '","score":' + str(index % 5 + 1) + ','
        '"text":"第' + str(page) + '页第' + str(index) + '条：风景不错，就是人多",'
        '"from":"贵州","headImg":"//qcommons.qunar.com/headshot/headshotsById/17133209'
        + str(index) + '.png?ssl=true&l","ticketName":"$undefined",'
        '"dayTripPackageName":"$undefined","images":1,'
        '"imgs":[{"big":"https://img.example/' + cid + '.jpg"}],"tags":[]}}'
    )


def comment_html(page: int) -> str:
    scripts = "".join(
        "<script>self.__next_f.push(%s)</script>"
        % json.dumps([1, _comment_payload(page, i)], ensure_ascii=False)
        for i in range(1, PER_PAGE + 1)
    )
    return (
        "<title>天河潭旅游度假区怎么样？ - 去哪儿</title>"
        + scripts
        + '<script type="application/ld+json">'
        '{"@type":"ItemList","itemListElement":[{"item":{"itemReviewed":'
        '{"name":"天河潭旅游度假区"}}}]}</script>'
        + f'<a href="/1703719381/comment?pageNum={TOTAL_PAGES}">{TOTAL_PAGES}</a>'
    )


VERIFY_HTML = "<title>访问验证</title><div>请完成安全验证后继续</div>"


class Handler(http.server.BaseHTTPRequestHandler):
    #: 打开之后下一次请求返回验证页，用来验"撞到验证页会停、不会硬打"
    serve_verification = False
    hits: list = []

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        page = int((parse_qs(parsed.query).get("pageNum") or ["1"])[0])
        Handler.hits.append(page)
        body = VERIFY_HTML if Handler.serve_verification else comment_html(
            # ⚠️ 翻过头时**原样再给最后一页**，不是返回空页。
            #    真站点就是这个行为，只判空的话会在最后一页无限循环。
            min(page, TOTAL_PAGES))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def serve():
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def main() -> int:
    from app.core.config import load_config
    from app.collectors.base import CollectContext, CollectTarget
    from app.collectors.qunar import QunarCollector

    srv, base = serve()
    print(f"假去哪儿站点：{base}（共 {TOTAL_PAGES} 页，每页 {PER_PAGE} 条）\n")
    bad: list = []
    config = load_config()

    class _NoProxy:
        async def get_pool(self, channel):
            return None

    collector = QunarCollector(config, _NoProxy())
    collector.base_url = base
    ctx = CollectContext(scenic_id="Q001", scenic_name="天河潭",
                         max_comments_per_work=100, params={})
    target = CollectTarget(target_type="poi", value="1703719381", name="天河潭旅游度假区")

    await collector.prepare(ctx)
    try:
        # ---- ① 正常采集 ----
        work = collector.poi_work(ctx, target)
        print(f"① 合成作品：{work.work_id} / {work.title}")
        if work.channel != "qunar":
            bad.append(f"channel 不对：{work.channel}")

        items = [c async for c in collector.collect_by_poi(ctx, target)]
        print(f"   采到 {len(items)} 条点评（期望 {TOTAL_PAGES * PER_PAGE}）")
        if len(items) != TOTAL_PAGES * PER_PAGE:
            bad.append(f"条数不对：{len(items)}")
        ids = [c.comment_id for c in items]
        if len(set(ids)) != len(ids):
            bad.append("有重复评论")

        one = items[0]
        print(f"   样例：{one.comment_id} | {one.commenter_name} | "
              f"{(one.content or '')[:22]} | 发布 {one.publish_time}")
        print(f"          用户ID={one.commenter_id} 地点={one.location} "
              f"图片={one.image_list}")
        for field, value in (("scenic_id", one.scenic_id), ("work_id", one.work_id),
                             ("channel", one.channel)):
            if not value:
                bad.append(f"{field} 是空的")
        if one.scenic_id != "Q001": bad.append("scenic_id 没带上")
        if one.work_id != "1703719381": bad.append("work_id 不是 POI id")
        if not one.commenter_id: bad.append("没解析出用户 ID")
        if not one.publish_time: bad.append("没解析出发布时间")
        if json.loads(one.image_list or "[]") == []: bad.append("图片没解析出来")
        extra = json.loads(one.extra_content or "{}")
        if not extra.get("likes_unavailable"):
            bad.append("没把「点赞拿不到」写进 extra_content")
        if extra.get("score") is None:
            bad.append("上游的 score 丢了")

        # ---- ② 翻过头不能无限循环 ----
        pages = Handler.hits[:]
        print(f"\n② 实际请求的页码：{pages}")
        if max(pages) > TOTAL_PAGES + 1:
            bad.append(f"翻过头了还在翻：{pages}")

        # ---- ③ 条数上限 ----
        Handler.hits.clear()
        ctx2 = CollectContext(scenic_id="Q001", scenic_name="天河潭",
                              max_comments_per_work=5, params={})
        got = [c async for c in collector.collect_by_poi(ctx2, target)]
        print(f"③ 上限 5 条时采到 {len(got)} 条")
        if len(got) > 5: bad.append(f"没卡住上限：{len(got)}")

        # ---- ④ 撞到验证页要停，不能硬打 ----
        Handler.hits.clear()
        Handler.serve_verification = True
        ctx3 = CollectContext(scenic_id="Q001", scenic_name="天河潭", params={})
        got3 = [c async for c in collector.collect_by_poi(ctx3, target)]
        Handler.serve_verification = False
        print(f"④ 全是验证页时：采到 {len(got3)} 条，请求了 {len(Handler.hits)} 次")
        if got3: bad.append("验证页居然解析出了数据")
        if len(Handler.hits) > 4:
            bad.append(f"撞到验证页还在硬打，请求了 {len(Handler.hits)} 次")
    finally:
        await collector.cleanup()
        srv.shutdown()

    print("\n" + ("❌ " + "；".join(bad) if bad else
                  "✅ 全部通过：采集/去重/上限/翻页收口/验证页保护"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
