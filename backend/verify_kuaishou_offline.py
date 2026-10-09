"""（离线自检，不联网、不要账号、不要数据库）
快手拟人采集：真无头浏览器 + 一个照着真实录制仿的快手，跑完整流程。

    python verify_kuaishou_offline.py


固定件不是凭结构编的，是照着 2026-09-05 的 record_page.py 录制
（149 个动作，关键字「盘山风景区」）一条条对出来的：

  · 全程 URL **不变**：search/{关键字}?source=NewReco
    视频详情是同页的 swiper 覆盖层，不是导航——所以不能靠 URL 判断状态
  · 卡片：div.cards > div.card-container > div.photo-card > … > img.cover-img
    **卡片上没有作品 id**，只能按位置点，id 从搜索接口取
  · 打开视频后第一件事是**关联播**：
    div.hover-tip.autoPlay span.toggle-switch（带 is-active = 开着）
  · 评论按钮在右侧：div.video-interact-panel div.hover-tip.commentPanel div.comment
  · 展开回复：div.comment-root div.btns span.expand，文案「查看更多回复」，
    展开后原地变「收起」——只点前者，否则会把刚展开的又收回去
  · 关视频：div.swiper-slide-active div.video-sidebar-layout div.close.circle-btn

接口按现有代码里的 REST 路径仿（/rest/v/search/feed、/rest/v/photo/comment/list、
/rest/v/photo/comment/sublist）。⚠️ 这三个路径**还没有真实录制确认过**，
所以这份自检证明的是"照这个约定跑得通"，不是"线上就是这样"。
"""
import asyncio, http.server, json, socket, sys, threading, urllib.parse
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from playwright.async_api import async_playwright

from app.collectors.base import CollectContext
from app.collectors.kuaishou_browser import KuaishouBrowserCollector
from app.core.search_filters import SearchFilters

CHROMIUM = __import__("os").environ.get("SMC_VERIFY_CHROMIUM", "")

PHOTOS = [
    {"id": "p1", "caption": "雨中的山景太美啦 #蓟州", "author": "金川影像", "likes": 262},
    {"id": "p2", "caption": "费县山里藏着绝美彩虹盘山公路", "author": "小尹呀", "likes": 427},
    {"id": "p3", "caption": "盘山风景区一日游", "author": "追梦感", "likes": 1152},
]
# ⚠️ 下面这些**字段名全部照抄用户提供的真实响应**（2026-09-05），
# 不是我编的形状。上一版固定件写的是 data.rootComments / commentId /
# authorName / likedCount —— 那套形状线上根本不存在，于是这份自检
# 全绿、实跑却一条评论都出不来。固定件编错了，测出来的绿是假的。
#
# 真实一级评论：{"comment_id": 1174060219763, "author_id": ..., "author_name": ...,
#                "content": ..., "timestamp": 1788542450634, "likeCount": 0,
#                "commentCount": 0, "hasSubComments": false, "reply_to": "0"}
# 真实响应外层：{"result":1, "commentCountV2":305, "pcursorV2":"...",
#                "rootCommentsV2":[...]}   ← **平铺，没有 data 这一层**
COMMENTS = {
    "p1": [{"comment_id": 1174060219763, "content": "有山？？？",
            "author_id": "3xaidfifngkckxy", "author_name": "过桥米线",
            "timestamp": 1788542450634, "likeCount": 7,
            "commentCount": 2, "hasSubComments": True, "reply_to": "0"},
           {"comment_id": 1173662595484, "content": "这是哪里？",
            "author_id": "3xx57vgcqmtkxc9", "author_name": "柿柿如意",
            "timestamp": 1788420254347, "likeCount": 0,
            "commentCount": 0, "hasSubComments": False, "reply_to": "0"}],
    "p2": [{"comment_id": 1173574178967, "content": "路线呢",
            "author_id": "3x5m8vqg3ddrzkw", "author_name": "凌烟梁",
            "timestamp": 1788373046090, "likeCount": 3,
            "commentCount": 0, "hasSubComments": False, "reply_to": "0"}],
    "p3": [],
}
#: 作品的**评论总数**只有评论接口给得出（commentCountV2），
#: 搜索接口里 feed["comment"] 只有 {"us_c": 0}
COMMENT_TOTALS = {"p1": 305, "p2": 42, "p3": 0, "q1": 7, "q2": 0}

#: 第二个关键字。⚠️ 这一段是本轮加的，因为**单个关键字永远测不出**
#: 顺序表不清空这个 bug —— 表是空的，下标天然对齐。
#: 用户实跑跑了八个关键字才发现：第二个关键字开始，日志里的作品
#: 和页面上点开的那条就对不上了。
KEYWORD2 = "八大处缆车"
PHOTOS2 = [
    {"id": "q1", "caption": "下山第一视角 #缆车", "author": "冯主任", "likes": 11},
    {"id": "q2", "caption": "恐高症也能坐的窝囊版缆车", "author": "罗庄融媒", "likes": 2079},
]
SUBS = [{"comment_id": 1166472031416, "content": "蓟州你都没去过？",
         "author_id": "3x9gcpv2x5wuewg", "author_name": "追逐梦想",
         "timestamp": 1786707934383, "likeCount": 1,
         "commentCount": 0, "hasSubComments": False,
         "reply_to": "3xns9bjph4d426u", "replyToUserName": "邓伟茂"},
        {"comment_id": 1166472031417, "content": "就在天津",
         "author_id": "3x9gcpv2x5wuewh", "author_name": "路人",
         "timestamp": 1786707934999, "likeCount": 0,
         "commentCount": 0, "hasSubComments": False,
         "reply_to": "3xns9bjph4d426u", "replyToUserName": "邓伟茂"}]

#: 第三个关键字：**分页**用的。前两个关键字一次就把作品全给了，
#: 永远测不出"滚到底 → 加载下一页"这条路——而用户实跑卡的正是这里
#: （日志「连续 3 次滑不动了，滑了 3 次共产出 16 条」，16 条就是第一屏）。
KEYWORD3 = "八大处红叶"
PHOTOS3 = [{"id": f"r{i}", "caption": f"八大处红叶第 {i} 条 #红叶",
            "author": "红叶播报", "likes": 100 + i} for i in range(1, 7)]
PHOTOS3_MORE = [{"id": f"r{i}", "caption": f"八大处红叶第 {i} 条 #红叶",
                 "author": "红叶播报", "likes": 100 + i} for i in range(7, 11)]

hits = {"search": 0, "comment": 0, "sub": 0, "profile": 0, "keyword": ""}

#: 「TA 的作品」里那些和关键字无关的作品。实跑时它们混进了顺序表，
#: 于是日志里连着七条「胡同小天地」的斑鸠/榴莲/二手书店。
OTHER_WORKS = [
    {"id": "other1", "caption": "小斑鸠咕咕越来越淘气了 #鸟",
     "author": "胡同小天地", "likes": 150},
    {"id": "other2", "caption": "盒马榴莲，买一赠一 #薅羊毛",
     "author": "胡同小天地", "likes": 22},
]


def feed_item(p):
    # 真实搜索响应：photo 里**没有 commentCount**，视频地址叫 photoUrls
    # （[{cdn,url}]），话题在 feed["tags"]，评论数只有 comment.us_c=0
    return {"type": 1,
            "tags": [{"name": "蓟州旅游", "type": 1}, {"name": "旅行推荐官", "type": 1}],
            "photo": {"id": p["id"], "caption": p["caption"],
                      "timestamp": 1788500000000, "likeCount": p["likes"],
                      "viewCount": 10000, "duration": 15000, "collectCount": 0,
                      "coverUrl": f"https://img/{p['id']}.jpg",
                      "photoUrls": [{"cdn": "a", "url": f"https://v/{p['id']}.mp4"}],
                      "photoH265Urls": [{"cdn": "b", "url": f"https://v/{p['id']}_h265.mp4"}]},
            "author": {"id": "u_" + p["id"], "name": p["author"],
                       "headerUrl": "https://h/u.jpg"},
            "comment": {"us_c": 0}}


PAGE = """<!doctype html><meta charset=utf-8><title>盘山风景区 - 快手</title>
<style>
html,body{margin:0;font-family:sans-serif;height:3000px}
header.navbar{height:80px;background:#fafafa}
/* ⚠️ 作品列表**自己滚**，body 不滚 —— 真站点就是这个结构，
   所以基类那句 window.scrollBy 打下去 scrollY 纹丝不动，
   实跑表现为「连续 3 次滑不动了（已经到底）」，其实一次都没滚过。 */
.video-list{height:400px;overflow-y:auto}
.cards{padding:8px;width:420px}
.photo-card{width:187px;display:inline-block;margin:8px;vertical-align:top}
.cover-img{width:187px;height:241px;background:#ddd;display:block}
/* 视频详情：同页覆盖层，不是新页面 */
.swiper-feed{display:none;position:fixed;inset:0;background:#111;z-index:30}
.swiper-feed.open{display:block}
/* ⚠️ 真站点的遮罩：视频开着时它盖住整页（顶栏也盖），
   所以搜索框和「搜索」按钮都点不动。视频本身在它**上面**（z-index 更高），
   所以播放器里的控件照样能点——这正是真站点的层次，也是这个 bug
   难发现的原因：视频里一切正常，只有顶栏被挡住。关掉视频遮罩才消失。 */
.player-pop-mask{display:none;position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.01)}
.player-pop-mask.open{display:block}
.close.circle-btn{position:absolute;left:160px;top:112px;width:32px;height:32px;
  background:#666;cursor:pointer}
.video-interact-panel{position:absolute;right:120px;top:400px}
.hover-tip.commentPanel .comment{width:27px;height:27px;background:#4af;cursor:pointer}
.control-area{position:absolute;left:840px;top:670px}
.toggle-switch{display:inline-block;width:30px;height:17px;background:#999;cursor:pointer}
.toggle-switch.is-active{background:#0c0}
/* 评论面板：右侧，自己滚 */
.comment-side{display:none;position:absolute;right:0;top:120px;width:438px;
  height:540px;background:#fff;overflow-y:auto}
.comment-side.open{display:block}
.comment-root{padding:8px;border-bottom:1px solid #eee}
.expand{color:#38f;cursor:pointer}
</style>
<body>
<header class=navbar><div class=right><div class=search-container><div class=search>
  <input class=input type=text placeholder="搜索你感兴趣的内容">
  <div class=search-text>搜索</div>
</div></div></div></header>
<div class="workbench search-view"><div class=video-list id=vlist><div class=cards id=cards></div></div></div>

<div class="player-pop-mask" data-v-bb88b204 id=mask></div>
<div class="swiper swiper-feed" id=feed><div class=swiper-wrapper>
  <div class="swiper-slide swiper-slide-active">
    <div class=video-sidebar-layout><div class="close circle-btn" id=closeBtn></div></div>
    <div class=slot-wrap><div class="control-area control-wrapper"><div class=right>
      <div class="hover-tip autoPlay"><div class=auto-play-btn>
        <span class="toggle-switch is-active is-disabled size-small" role=switch
              aria-disabled="true" id=autoplay></span>
      </div></div>
    </div></div></div>
    <div class=video-interact-panel><div class=rb><div class=photo-btns>
      <div class="hover-tip commentPanel"><div class=comment id=cmtBtn><img alt=""></div></div>
    </div></div></div>
    <div class=content><div class=side-area><div class=content>
      <div class=comment-side id=cside><div class=comment-list id=clist></div></div>
    </div></div></div>
  </div>
</div></div>
<script>
let cur = '', cursor = 0, more = true;
let feedCursor = 0, feedMore = true, feedLoading = false;
// ⚠️ 卡片**不是**一进页面就有的：必须先在搜索框里打字、再点「搜索」。
// 上一版固定件是页面一加载就 loadFeed()，等于把"输入关键字→点搜索"
// 这段动线整个跳过了，拼 URL 也照样绿。
async function loadFeed(reset) {
  const kw = document.querySelector('.search-container input.input').value;
  if (!kw) return;
  if (reset) {
    // 新一轮搜索：卡片**整批换掉**（真站点就是这样，不是往后追加）
    feedCursor = 0; feedMore = true;
    document.getElementById('cards').innerHTML = '';
  }
  if (!feedMore || feedLoading) return;
  feedLoading = true;
  const r = await fetch('/rest/v/search/feed', {method:'POST',
      body: JSON.stringify({keyword: kw, pcursor: String(feedCursor)})});
  const j = await r.json();
  feedMore = j.pcursor !== 'no_more';
  feedCursor++;
  feedLoading = false;
  for (const f of j.feeds) {
    const c = document.createElement('div'); c.className = 'card-container';
    const p = document.createElement('div'); p.className = 'photo-card';
    p.setAttribute('data-like-count', '⭐ ' + f.photo.likeCount);
    const cov = document.createElement('div'); cov.className = 'cover';
    const ad = document.createElement('div'); ad.className = 'aspect-div';
    const img = document.createElement('img');
    img.className = 'cover-img'; img.alt = f.photo.caption;
    img.onclick = () => openVideo(f.photo.id);
    ad.appendChild(img); cov.appendChild(ad); p.appendChild(cov);
    const cap = document.createElement('div'); cap.className='caption';
    cap.textContent = f.photo.caption; p.appendChild(cap);
    c.appendChild(p); document.getElementById('cards').appendChild(c);
  }
}
document.querySelector('.search-container .search-text').onclick = () => loadFeed(true);
document.querySelector('.search-container input.input')
  .addEventListener('keydown', e => { if (e.key === 'Enter') loadFeed(true); });
// 滚到底就追加下一页（真站点的无限滚动）
document.getElementById('vlist').addEventListener('scroll', () => {
  const el = document.getElementById('vlist');
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) loadFeed(false);
});

function openVideo(id) {
  cur = id; cursor = 0; more = true;
  // ⚠️ 真站点行为：点开视频，右边「TA 的作品」页签会把作者的其他作品拉回来。
  // 这些**不是搜索结果**，绝不能混进采集顺序里。
  fetch('/rest/v/profile/feed', {method:'POST',
      body: JSON.stringify({principalId: 'u_' + id})});
  document.getElementById('feed').classList.add('open');
  document.getElementById('mask').classList.add('open');   // 遮罩跟着上来
  closeArmed = false;                                      // 每次开都要重新"预热"
  document.getElementById('cside').classList.remove('open');
  document.getElementById('clist').innerHTML = '';
}
// ⚠️ 第一次点故意不响应：真站点的叉是带动画出现的，点在动画中间会落空。
// 这样才能验到「点完要确认遮罩真的没了，没了才算关掉」那一层——
// 只点一下就 return 的话，遮罩还在，下一个关键字照样被挡。
let closeArmed = false;
document.getElementById('closeBtn').onclick = () => {
  if (!closeArmed) { closeArmed = true; return; }
  document.getElementById('feed').classList.remove('open');
  document.getElementById('mask').classList.remove('open');
  document.getElementById('cside').classList.remove('open');
  cur = '';
};
// Escape 也能关（真站点支持）
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('closeBtn').click();
});
// 联播：**鼠标悬停在 .hover-tip.autoPlay 上才解锁**——实跑就是这样：
// 刚打开视频时开关是 aria-disabled="true"，Playwright 判定"元素不可用"，
// 一直等到超时报 element is not enabled。不悬停就点不动。
const apArea = document.querySelector('.hover-tip.autoPlay');
const apSw = document.getElementById('autoplay');
apArea.addEventListener('mouseenter', () => {
  apSw.classList.remove('is-disabled'); apSw.setAttribute('aria-disabled', 'false');
});
apArea.addEventListener('mouseleave', () => {
  apSw.classList.add('is-disabled'); apSw.setAttribute('aria-disabled', 'true');
});
apSw.onclick = (e) => {
  if (e.target.getAttribute('aria-disabled') === 'true') return;  // 锁着就不响应
  e.target.classList.toggle('is-active');
};

document.getElementById('cmtBtn').onclick = async () => {
  document.getElementById('cside').classList.add('open');
  await loadComments();
};
async function loadComments() {
  if (!cur || !more) return;
  const r = await fetch('/rest/v/photo/comment/list', {method:'POST',
      body: JSON.stringify({photoId: cur, pcursor: String(cursor)})});
  const j = await r.json();
  more = j.pcursorV2 !== 'no_more'; cursor++;
  for (const cm of (j.rootCommentsV2 || [])) {
    const d = document.createElement('div');
    d.className = 'comment-root'; d.style.height = '120px';
    d.textContent = cm.author_name + '：' + cm.content;
    if (cm.commentCount > 0) {
      const inner = document.createElement('div'); inner.className='comment-inner';
      const btns = document.createElement('div'); btns.className='btns';
      const ex = document.createElement('span');
      ex.className = 'expand'; ex.textContent = '查看更多回复';
      ex.onclick = async () => {
        ex.textContent = '收起';          // 真站点就是原地变「收起」
        await fetch('/rest/v/photo/comment/sublist', {method:'POST',
            body: JSON.stringify({photoId: cur, rootCommentId: cm.comment_id})});
      };
      btns.appendChild(ex); inner.appendChild(btns); d.appendChild(inner);
    }
    document.getElementById('clist').appendChild(d);
  }
}
document.getElementById('cside').addEventListener('scroll', loadComments);
</script></body>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, body, ctype):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def js(self, obj):
        self.send(json.dumps(obj), "application/json")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/rest/v/search/feed":
            hits["search"] += 1
            kw = body.get("keyword", "")
            hits["keyword"] = kw
            cur = int(body.get("pcursor") or 0)
            if kw == KEYWORD3:
                # 分页：第 1 页 6 条，滚到底才给第 2 页的 4 条
                rows = PHOTOS3 if cur == 0 else (PHOTOS3_MORE if cur == 1 else [])
                return self.js({
                    "result": 1, "feeds": [feed_item(p) for p in rows],
                    "pcursor": "1" if cur == 0 else "no_more"})
            # 第二个关键字返回**另一批**作品——和真站点一样，
            # 换关键字后页面上的卡片是整批换掉的
            rows = PHOTOS2 if kw == KEYWORD2 else PHOTOS
            return self.js({"result": 1, "feeds": [feed_item(p) for p in rows],
                            "pcursor": "no_more"})
        if path == "/rest/v/photo/comment/list":
            hits["comment"] += 1
            pid = body.get("photoId", "")
            cur = int(body.get("pcursor") or 0)
            rows = COMMENTS.get(pid, []) if cur == 0 else []
            # ⚠️ 平铺，没有 data 这一层；键名带 V2 —— 真实响应就长这样
            return self.js({"result": 1,
                            "commentCountV2": COMMENT_TOTALS.get(pid, 0),
                            "rootCommentsV2": rows,
                            "pcursorV2": "no_more" if cur > 0 or not rows else "1"})
        if path == "/rest/v/profile/feed":
            hits["profile"] += 1
            # 作者的其他作品：内容和搜索关键字完全无关
            return self.js({"result": 1, "pcursor": "no_more",
                            "feeds": [feed_item(p) for p in OTHER_WORKS]})
        if path == "/rest/v/photo/comment/sublist":
            hits["sub"] += 1
            return self.js({"result": 1, "subCommentsV2": SUBS,
                            "pcursorV2": "no_more"})
        return self.js({"result": 1})

    def do_GET(self):
        self.send(PAGE, "text/html; charset=utf-8")


def serve():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


class Log:
    def __init__(s): s.lines = []
    def info(s, m, *a, **k): s.lines.append(("info", str(m)))
    def warning(s, m, *a, **k): s.lines.append(("warn", str(m)))
    warn = warning
    def error(s, m, *a, **k): s.lines.append(("error", str(m)))
    def debug(s, m, *a, **k): pass


async def main():
    server, port = serve()
    base = f"http://127.0.0.1:{port}"

    class Local(KuaishouBrowserCollector):
        integrated = True          # 自检里强制打开，验的是逻辑不是接入开关
        host = base
        HOME_URL = base
        # ⚠️ 让"已经在站内"这个判断在自检里也成立。
        # 不覆盖的话每次搜索都会整页重载，而重载会把遮罩一起冲掉——
        # 「上一条视频没关，挡住下一个关键字」这个 bug 就永远测不出来。
        SITE_HOST_MARK = "127.0.0.1"
        # 自检里接口是本地的，卡片渲染是毫秒级；实跑那 6 秒是留给
        # 真站点的网络往返的。不压短的话，每个"到底了"的关键字要白等
        # 3 × 6 秒。
        LIST_LOAD_WAIT_SECONDS = 1.5

        @property
        def _search_url_template(self):
            return base + "/search/{keyword}?source=NewReco"

        @property
        def _work_url_template(self):
            return base + "/short-video/{work_id}"

    from app.core.config import load_config
    from app.proxy.manager import ProxyManager
    config = load_config(use_cache=False)
    config.set("redis.enabled", False)
    collector = Local(config, ProxyManager(config, None))

    logs = Log()
    ctx = CollectContext(
        scenic_id="S1", scenic_name="盘山风景区", task_id="t-ks",
        max_works=10, max_comments_per_work=100,
        filters=SearchFilters(channel="kuaishou"), logger=logs)
    ctx.log = lambda m, level="info": logs.lines.append((level, str(m)))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, **({"executable_path": CHROMIUM} if CHROMIUM else {}))
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        collector._page = page
        collector._run = lambda coro: coro
        collector.note_step = lambda s: None
        await collector._setup_network_capture(ctx)

        print("=== 1. 搜索：拦到 /rest/v/search/feed，作品按顺序进队 ===")
        works, by_work = [], {}
        async for w in collector.collect_by_keyword(ctx, "盘山风景区"):
            works.append(w)
            by_work[w.work_id] = [c async for c in collector.collect_comments(ctx, w)]
        ids = [w.work_id for w in works]
        print(f"   search {hits['search']} 次，采到 {len(works)} 条：{ids}")
        if set(ids) != {"p1", "p2", "p3"}:
            for lv, m in logs.lines: print(f"     [{lv}] {m}")
        assert set(ids) == {"p1", "p2", "p3"}, f"实际 {ids}"
        assert collector._order[:3] == ["p1", "p2", "p3"], \
            f"顺序表不对：{collector._order}——卡片没有 id，全靠这个按位置点"
        # 固定件里卡片只有"点了搜索"才出来，所以能走到这儿就说明
        # 关键字是**在搜索框里打进去、再点「搜索」**触发的，不是拼 URL 跳过去的
        typed = await page.evaluate(
            "() => document.querySelector('.search-container input.input').value")
        print(f"   搜索框里的字：{typed!r}；接口收到的关键字：{hits['keyword']!r}")
        assert typed == "盘山风景区", f"搜索框里不是打进去的关键字：{typed!r}"
        assert hits["keyword"] == "盘山风景区", \
            f"接口收到的关键字是 {hits['keyword']!r}——不是从输入框来的"
        print("   ✓ 关键字是打进搜索框再点「搜索」触发的，不是拼 URL")

        print("\n=== 2. 关联播：只在开着的时候点一次 ===")
        off = [m for lv, m in logs.lines if "已关闭联播" in m]
        print("   " + (off[0] if off else "（没关！）"))
        assert off, "没关联播——一条播完会自动跳下一条，评论会记到别人名下"
        klass = await page.evaluate(
            "() => document.getElementById('autoplay').className")
        print(f"   开关现在的 class：{klass}")
        assert "is-active" not in klass, "点完之后联播还开着"
        assert len(off) == 1, f"联播关了不止一次（{len(off)}）——再点等于又打开"
        print("   ✓ 关了，而且只关了一次")

        print("\n=== 3. 评论：一级 + 展开出来的回复 ===")
        for w in works:
            got = by_work[w.work_id]
            print(f"   {w.work_id}: {len(got)} 条 "
                  f"{[(c.comment_id, c.comment_level) for c in got]}")
        a = by_work["p1"]
        got_ids = [c.comment_id for c in a]
        assert "1174060219763" in got_ids and "1173662595484" in got_ids, \
            "一级评论没采到"
        assert "1166472031416" in got_ids, "「查看更多回复」没点开，或者 sublist 没拦到"
        assert hits["sub"] >= 1, "sublist 一次都没被触发"
        assert by_work["p3"] == [], "没有评论的作品不该凭空多出评论"
        assert all(c.work_id == "p2" for c in by_work["p2"]), "评论串到别的作品了"
        print("   ✓ 一级和回复都在，各归各的")

        print("\n=== 4. 「查看更多回复」只点一次，不能把展开的又收回去 ===")
        # 固定件里只有 p1 的第一条带回复（commentCount=2），
        # 也就是全程只该有 **1 个**展开按钮、**1 次** sublist 请求。
        # 展开后按钮原地变「收起」——真站点就是这样。要是把「收起」也点了，
        # sublist 会被打第二次，而且回复会被收回去。
        print(f"   sublist 一共被请求了 {hits['sub']} 次（应该正好 1 次）")
        assert hits["sub"] == 1, (
            f"sublist 被打了 {hits['sub']} 次——多出来的那次说明把「收起」也点了"
        )
        expanded_ids = [c.comment_id for c in a if c.comment_level == "level_2"]
        print(f"   展开拿到的回复：{expanded_ids}")
        assert set(expanded_ids) == {"1166472031416", "1166472031417"}, expanded_ids
        # 回复的顶楼必须是 c1（来自请求里的 rootCommentId），不是它自己
        for cid in expanded_ids:
            c = next(x for x in a if x.comment_id == cid)
            assert c.root_comment_id == "1174060219763", \
                f"{cid} 的顶楼记成了 {c.root_comment_id}，评论树会被压平"
        print("   ✓ 只点了「查看更多」，回复挂在正确的顶楼下面")

        print("\n=== 5. 关视频：回到列表，下一条还点得开 ===")
        opened = [m for lv, m in logs.lines if "视频没打开" in m]
        assert not opened, f"有作品点不开：{opened[:2]}"
        feed_open = await page.evaluate(
            "() => document.getElementById('feed').classList.contains('open')")
        print(f"   最后一条采完，覆盖层还开着吗：{feed_open}")
        print("   ✓ 三条都点开了（点不开的话第 2、3 条会直接失败）")

        print("\n=== 6. 评论字段真的映射上了（不是空壳） ===")
        first = next(c for c in a if c.comment_id == "1174060219763")
        print(f"   {first.comment_id} | {first.commenter_name} | 赞 {first.likes} "
              f"| {first.publish_time} | {first.content}")
        assert first.commenter_name == "过桥米线", \
            f"作者名没映射上：{first.commenter_name!r}（真实字段是 author_name）"
        assert first.likes == 7, \
            f"点赞数没映射上：{first.likes}（真实字段是 likeCount，不是 likedCount）"
        assert first.content == "有山？？？", "内容没映射上"
        assert first.publish_time is not None, "发布时间没映射上（timestamp 是毫秒）"
        print("   ✓ 作者/点赞/内容/时间都对得上真实字段名")

        print("\n=== 7. 作品的评论总数回填（搜索接口给不出这个数） ===")
        # 搜索响应里 photo 没有 commentCount，feed["comment"] 只有 {"us_c": 0}，
        # 真值在评论接口的 commentCountV2 里。不回填的话作品行永远写 0，
        # 和评论表里几百条对不上——用户就是这么发现的。
        p1_work = next(w for w in works if w.work_id == "p1")
        print(f"   p1 评论数 = {p1_work.comment_cnt}（接口给的 commentCountV2 是 "
              f"{COMMENT_TOTALS['p1']}）")
        assert p1_work.comment_cnt == COMMENT_TOTALS["p1"], \
            f"评论总数没回填：{p1_work.comment_cnt}"
        print("   ✓ 评论总数取自评论接口，不是搜索接口的 0")

        print("\n=== 8. 「TA 的作品」不能混进采集顺序 ===")
        # 真实故障（2026-09-05 八大处公园）：点开视频后快手顺手拉了作者的
        # 其他作品（/rest/v/profile/feed），上一版把它当搜索结果收了。
        # 后果是顺序表里混进搜索结果里根本没有的作品、下标越走越偏、
        # **点开的卡片和日志里那条对不上**，评论按 photoId 一过滤全被丢掉。
        print(f"   profile/feed 被请求了 {hits['profile']} 次（页面确实拉了）")
        assert hits["profile"] >= 1, "固定件没有模拟「TA 的作品」，这一段没意义"
        polluted = [w for w in works if w.work_id.startswith("other")]
        print(f"   采到的作品：{[w.work_id for w in works]}")
        assert not polluted, (
            f"主页作品混进搜索结果了：{[w.work_id for w in polluted]}——"
            f"顺序表会错位，最后点开的卡片和日志对不上"
        )
        assert collector._order == ["p1", "p2", "p3"], (
            f"顺序表被污染了：{collector._order}"
        )
        print("   ✓ 顺序表只有搜索结果，主页作品被挡在外面")

        print("\n=== 9. 点错卡片必须喊出来，不能记成「这条没有评论」 ===")
        fake = works[0]
        collector._comment_photo_id = "别人的作品id"
        before = len(logs.lines)
        collector._check_opened_work(ctx, fake)
        said = [m for lv, m in logs.lines[before:] if "点开的不是这一条" in m]
        print("   " + (said[0][:78] + " …" if said else "（一声没吭！）"))
        assert said, (
            "点错了卡片却不吭声——画面上放着 A、日志记的是 B，"
            "A 的评论按 photoId 全被丢掉，最后报 B「没有评论」"
        )
        collector._comment_photo_id = fake.work_id
        before = len(logs.lines)
        collector._check_opened_work(ctx, fake)
        assert len(logs.lines) == before, "对得上的时候不该报警"
        print("   ✓ 对不上就报警，对得上不吵")

        print("\n=== 10. 换关键字：顺序表必须清空，下标不能串位 ===")
        # ⚠️ 这一段是本轮加的。**单个关键字永远测不出这个 bug**——
        # 表是空的，下标天然对齐，所以 verify_kuaishou_browser.py（只跑一个
        # 关键字）一直是绿的，而用户跑第二个关键字就开始点错作品。
        logs.lines.clear()
        works2 = []
        async for w in collector.collect_by_keyword(ctx, KEYWORD2):
            works2.append(w)
            [c async for c in collector.collect_comments(ctx, w)]
        ids2 = [w.work_id for w in works2]
        print(f"   第二个关键字采到：{ids2}")
        print(f"   顺序表：{collector._order}")
        assert ids2 == ["q1", "q2"], f"实际 {ids2}"
        assert collector._order == ["q1", "q2"], (
            f"顺序表没清空：{collector._order}——"
            f"下标会从上一个关键字的长度接着算，点开的卡片和日志全对不上"
        )
        # 下标错位时 _locate_card 会按文案兜底并留下这句话；
        # 顺序表清干净了就**一次都不该出现**
        rescued = [m for lv, m in logs.lines if "文案对不上" in m]
        print("   文案兜底触发次数：", len(rescued))
        assert not rescued, (
            f"还是错位了（靠文案救回来的）：{rescued[:2]}——"
            f"兜底是保险，不该变成常态"
        )
        wrong = [m for lv, m in logs.lines if "点开的不是这一条" in m]
        assert not wrong, f"点错了作品：{wrong[:2]}"
        print("   ✓ 换关键字后下标从头对齐，没有触发任何兜底")

        print("\n=== 11. 采过的作品也要占顺序表的位置 ===")
        # 页面上的卡片不管我们采没采过都在那儿占位。
        # 原来 _order.append 在"采过就 continue"的后面，于是重复命中的那些
        # 不进表，后面每一条的下标都往前串一位。
        collector._order.clear()
        collector._seen_work_ids.add("q1")          # 假装 q1 上个关键字采过
        collector._seen_work_ids.discard("q2")      # q2 是这轮新的（上面那段刚采过它）
        collector._captured_works.extend(
            [feed_item(x) for x in PHOTOS2])
        again = await collector._extract_new_works(ctx, KEYWORD2)
        print(f"   顺序表：{collector._order}；这轮新产出：{[w.work_id for w in again]}")
        assert collector._order == ["q1", "q2"], (
            f"采过的 q1 没进顺序表：{collector._order}——"
            f"q2 的下标会变成 0，可它在页面上是第 2 张卡片"
        )
        assert [w.work_id for w in again] == ["q2"], "采过的不该重复产出"
        print("   ✓ 顺序表按页面记，产出按去重记，两件事分开")

        print("\n=== 12. 上一条视频没关，不能挡住下一个关键字的搜索 ===")
        # 实跑现场（2026-09-05 20:03）：
        #   [快手] 往搜索框打字出错：Locator.click: Timeout 8000ms exceeded.
        #   <div class="player-pop-mask"> intercepts pointer events
        # 上一个关键字最后那条视频没关干净，遮罩把顶栏整个盖住，
        # 搜索框和「搜索」按钮都点不动，重试 19 次干等 8 秒。
        # 难发现的地方在于：视频里一切正常，只有顶栏被挡。
        await collector._goto_work(ctx, works2[0])       # 故意开着一条视频
        assert await collector._mask_present(), "固定件没盖上遮罩，这一段没意义"
        logs.lines.clear()
        collector._seen_work_ids.clear()     # 前面采过了，不清就没有新产出可看
        works3 = []
        async for w in collector.collect_by_keyword(ctx, "盘山风景区"):
            works3.append(w)
        blocked = [m for lv, m in logs.lines
                   if "intercepts pointer events" in m or "被覆盖层挡住" in m]
        print(f"   开着视频直接搜下一个关键字 → 采到 {len(works3)} 条")
        assert not blocked, f"被遮罩挡住了：{blocked[:1]}"
        assert works3, "开着视频就搜不动了——遮罩没被清掉"
        assert not await collector._mask_present(), "搜完遮罩还在"
        print("   ✓ 搜索前会先把视频关干净，遮罩不再挡路")

        print("\n=== 14. 作品列表一直往下滑，滑到底才出下一页 ===")
        # ⚠️ 用户实跑现场：
        #   「连续 3 次滑不动了（已经到底），滑了 3 次共产出 16 条，结束」
        #   「关键字 [西山八大处公园] 采集到 0 条作品，另有 13 条不在时间范围内
        #     被跳过，3 条不含关键字被内容过滤丢弃」
        # 16 条正好是第一屏 —— 一次都没真的往下滚过。两个原因叠在一起：
        #   a) 基类用 window.scrollBy，而快手搜索页滚的是**内部容器**，
        #      window.scrollY 永远是 0（固定件现在也是这个结构）；
        #   b) 时间窗和关键字过滤都在上层做，第一屏可能一条都留不下。
        print(f"   默认滑动次数：{Local.DEFAULT_LIST_SCROLL_TIMES}")
        assert Local.DEFAULT_LIST_SCROLL_TIMES == 20, "默认应该是 20 次"
        config.set("crawl.list_scroll_times", 9)
        assert collector.list_scroll_times == 9, \
            f"全局配置没读到：{collector.list_scroll_times}"
        config.set("platforms.kuaishou.list_scroll_times", 5)
        assert collector.list_scroll_times == 5, \
            f"平台级配置该优先于全局：{collector.list_scroll_times}"
        print(f"   crawl=9 + platforms.kuaishou=5 → 生效 "
              f"{collector.list_scroll_times} 次（平台级优先）")

        logs.lines.clear()
        collector._seen_work_ids.clear()
        works4 = []
        async for w in collector.collect_by_keyword(ctx, KEYWORD3):
            works4.append(w)
        ids4 = [w.work_id for w in works4]
        print(f"   采到 {len(ids4)} 条：{ids4}")

        said = [m for lv, m in logs.lines if "作品列表最多往下滑" in m]
        print("   " + (said[0][:78] if said else "（没说要滑几次！）"))
        assert said and "5 次" in said[0], f"滑动次数没生效：{said[:1]}"

        # 第 2 页只有滚到底才会来。拿到 r7~r10 就证明**真的往下滚了**
        assert len(ids4) == len(PHOTOS3) + len(PHOTOS3_MORE), (
            f"只采到 {len(ids4)} 条（第一屏是 {len(PHOTOS3)} 条）——"
            f"没滚到底，第二页没加载出来。这就是实跑那个 bug"
        )
        assert "r10" in ids4, f"第二页的作品一条都没有：{ids4}"

        grew = [m for lv, m in logs.lines if "列表新加载" in m]
        print("   " + (grew[0] if grew else "（列表一次都没变多！）"))
        assert grew, "没有一次滚动让卡片变多——说明列表压根没往下滚"

        where = [m for lv, m in logs.lines if "列表滚动容器" in m]
        print("   " + (where[0][:100] if where else "（没报滚动容器）"))
        assert where and "video-list" in where[0], (
            f"没认出真正在滚的容器：{where[:1]}——"
            f"认不出来就会退回 window.scrollBy，而它对这个结构无效"
        )

        done = [m for lv, m in logs.lines
                if "作品列表已经滑了" in m or "都没有加载出新卡片" in m
                or "搜索接口说没有更多了" in m]
        print("   " + (done[0][:90] if done else "（结束时一声不吭）"))
        assert done, "滑到上限、到底了、接口说没有了，都该留一句话说清楚为什么结束"

        config.set("platforms.kuaishou.list_scroll_times", "")
        config.set("crawl.list_scroll_times", "")
        assert collector.list_scroll_times == 20, "配置清空后该回到默认 20"
        print("   ✓ 滚的是内部容器、滚到底能出下一页、次数可配、结束有交代")

        print("\n=== 13. URL 全程不变（视频是同页覆盖层，不是导航） ===")
        print(f"   当前 URL：{page.url}")
        assert "/short-video/" not in page.url, \
            "跳到独立视频页了——那样会整页重载，把搜索结果冲掉"
        print("   ✓ 没有跳 URL")

        await browser.close()
    server.shutdown()
    print("\n全部通过 ✓")


asyncio.run(main())
