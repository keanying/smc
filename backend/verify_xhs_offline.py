"""（离线自检，不联网、不需要账号、不需要数据库）
小红书拟人采集：真无头浏览器 + 一个照着真实录制仿的小红书，跑完整流程。

    python verify_xhs_offline.py

固定件不是凭印象编的，是照着**两份真实产出**一条条对出来的：
probe_page.py 的快照 + record_page.py 的操作录制（2026-09-03，
关键字「盘山风景区」，同一个账号）。

接口与数据形状：
  · 搜索走 POST /api/sns/web/v2/search/notes（**v2**，不是 v1）
  · 搜索响应的 note_card 是**残缺**的：没有 note_id、没有 desc、没有 time，
    display_title 还可有可无；note_id 和 xsec_token 都在**外层 item** 上
  · 完整数据只在 POST /api/sns/web/v1/feed 里（note_id/title/desc/time/tag_list）
  · 「展开 N 条回复」点了打 sub/page，而 sub/page 的响应体里**没有**
    root_comment_id，只在 query 里

页面结构（这一版新加的，全部来自录制）：
  · 这个账号跑的是 **AI 版式**（html.ai-layout-active）：首页**根本没有**
    `#search-input`，只有 `textarea#search-input-in-feeds`；从首页搜过去
    落的是 `/search_result_ai`。以前只认经典版，于是每次都静悄悄地
    退回"直开 URL"，从没走通过输入这条路。
  · 筛选面板是 **hover 现建、移开现删**——不是 display:none。录制的 150 帧
    DOM 快照里没有一帧含「半年内」，因为拍的时候鼠标都已经离开了。
    面板结构：div.filter-container > div.filters-wrapper > … > div.tags > span
  · 卡片是**回收式虚拟列表**：DOM 里稳定 29 张，每滚一屏换掉 5 张。
  · 左侧边栏宽 164px 且自己能滚；笔记弹窗左边是图片区、右边
    `div.note-scroller` 才是评论区，评论翻页挂在它自己的 scroll 上。
    **滚轮滚的是指针底下那个元素**，所以鼠标停哪儿是功能性的，不是细节。

要验的核心是一条：**work 必须等 feed 到货之后才产出**。
搜索卡片没有 desc / time，而 runner 拿到 work 立刻要用它们做关键字过滤和
时间窗过滤——早产的 work 会被过滤器整片丢掉，现象是"明明搜得到却一条不入库"。
"""
import asyncio, http.server, json, socket, sys, threading, urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from playwright.async_api import async_playwright

from app.collectors.base import CollectContext
from app.collectors.xhs_browser import XhsBrowserCollector
from app.core.search_filters import SearchFilters

CHROMIUM = __import__("os").environ.get("SMC_VERIFY_CHROMIUM", "")

NOTES = [
    {"id": "n1", "token": "T-n1", "title": "盘山秋色绝了", "likes": "1024"},
    {"id": "n2", "token": "T-n2", "title": "",             "likes": "77"},   # 无 display_title
    {"id": "n3", "token": "T-n3", "title": "盘山缆车",      "likes": "5"},
]
PAGE2 = [{"id": "n4", "token": "T-n4", "title": "盘山日出", "likes": "3000"}]
ALL = {n["id"]: n for n in NOTES + PAGE2}

hits = {"search": 0, "feed": 0, "comment": 0, "sub": 0, "search_after_filter": 0}
filter_applied = {"on": False}


def search_item(n):
    """照搬快照：note_id 和 xsec_token 在外层，note_card 里没有 desc / time。"""
    card = {
        "type": "normal",
        "user": {"user_id": "u_" + n["id"], "nickname": "作者" + n["id"]},
        "interact_info": {"liked_count": n["likes"], "comment_count": "3",
                          "collected_count": "9", "share_count": "1"},
        "cover": {"url_default": f"https://img/{n['id']}.jpg"},
        "image_list": [{"url_default": f"https://img/{n['id']}.jpg"}],
        "corner_tag_info": [],
    }
    if n["title"]:
        card["display_title"] = n["title"]
    return {"id": n["id"], "model_type": "note", "xsec_token": n["token"],
            "note_card": card}


def feed_item(note_id):
    n = ALL[note_id]
    return {"id": note_id, "model_type": "note", "ignore": False, "note_card": {
        "note_id": note_id,
        "type": "normal",
        "title": n["title"] or "无标题笔记",
        # 正文里带关键字——runner 的关键字过滤就靠它
        "desc": f"这是 {note_id} 的正文，讲的是盘山风景区的玩法",
        "time": 1756000000,
        "last_update_time": 1756000500,
        "ip_location": "天津",
        "user": {"user_id": "u_" + note_id, "nickname": "作者" + note_id},
        "interact_info": {"liked_count": n["likes"], "comment_count": "3",
                          "collected_count": "9", "share_count": "1"},
        "image_list": [{"url_default": f"https://img/{note_id}.jpg"}],
        "tag_list": [{"name": "盘山", "type": "topic"}],
        "at_user_list": [],
    }}


def comment(cid, note_id, content, *, subs=0, more_subs=0):
    c = {"id": cid, "note_id": note_id, "content": content,
         "create_time": 1788398738000, "like_count": "6", "ip_location": "天津",
         "user_info": {"user_id": "cu_" + cid, "nickname": "评论者" + cid},
         "sub_comment_count": str(subs + more_subs), "sub_comments": [],
         "sub_comment_has_more": more_subs > 0, "at_users": []}
    for i in range(subs):
        c["sub_comments"].append({
            "id": f"{cid}-r{i}", "content": f"{cid} 的回复{i}", "note_id": note_id,
            "create_time": 1788399376000, "like_count": "1",
            "user_info": {"user_id": "ru", "nickname": "回复者"},
            "target_comment": {"id": cid}})
    return c


# 每条笔记的评论分页
COMMENTS = {
    "n1": [{"comments": [comment("c1", "n1", "好看", subs=1, more_subs=2),
                         comment("c2", "n1", "想去")], "has_more": True},
           {"comments": [comment("c3", "n1", "打卡")], "has_more": False}],
    "n2": [{"comments": [comment("c9", "n2", "路线呢")], "has_more": False}],
    "n3": [{"comments": [], "has_more": False}],
    "n4": [{"comments": [comment("c20", "n4", "日出真美")], "has_more": False}],
}
# 「展开回复」返回的那两条，响应体里**没有** root_comment_id
SUB_COMMENTS = [
    {"id": "c1-x0", "content": "补的回复0", "note_id": "n1",
     "create_time": 1788395971000, "like_count": "0",
     "user_info": {"user_id": "ru2", "nickname": "回复者2"},
     "target_comment": {"id": "c1"}},
    {"id": "c1-x1", "content": "补的回复1", "note_id": "n1",
     "create_time": 1788395972000, "like_count": "0",
     "user_info": {"user_id": "ru3", "nickname": "回复者3"},
     "target_comment": {"id": "c1"}},
    # ⚠️ 关键的一条：**回复的回复**。它的 target_comment 是上面那条回复
    # （c1-x0），而不是顶楼 c1。只有这种数据才能验出
    # "target 是直接父级、root 是顶楼"这个区别——只有一条回复时两者恰好相等。
    {"id": "c1-x2", "content": "回复的回复", "note_id": "n1",
     "create_time": 1788395973000, "like_count": "0",
     "user_info": {"user_id": "ru4", "nickname": "回复者4"},
     "target_comment": {"id": "c1-x0"}},
]

SEARCH_HTML = """<!doctype html><meta charset=utf-8><title>盘山风景区 - 小红书搜索</title>
<style>
html,body{font-family:sans-serif;margin:0}
/* 左侧边栏：真站点实测宽 164px，而且**自己能滚**。
   鼠标停在它上面滚轮，滚的是边栏，笔记列表纹丝不动。 */
.side-bar{position:fixed;left:0;top:0;width:164px;height:100%;
  overflow-y:auto;background:#fafafa;z-index:3}
.side-bar .pad{height:4000px}
.main{margin-left:164px}
.search-layout__top{height:40px;position:relative}
.filter{cursor:pointer;padding:8px 12px;border:1px solid #ddd;
  position:absolute;right:12px;top:0;width:76px}
/* 面板：hover 才建，移开就从 DOM 里删。录制里 150 帧 DOM 快照没有
   一帧含「半年内」，就是因为拍快照时鼠标早离开了——不是 display:none。 */
.filter-container{position:absolute;right:12px;top:40px;width:450px;height:570px;
  background:#fff;border:1px solid #eee;z-index:5}
.feeds-container{padding:8px}
.note-item{width:249px;display:block;margin:12px;height:420px}
.cover{display:block;height:300px;background:#eee}
.note-detail-mask{display:none;position:fixed;inset:0;background:#fff;z-index:9}
.note-detail-mask.open{display:flex}
.close-circle{position:absolute;left:24px;top:24px;width:40px;height:40px;
  cursor:pointer;border:1px solid #333;z-index:11}
/* 弹窗左边是图片区，右边才是评论。评论区**自己滚**——
   滚轮落在左边图片上，评论一页都不会再加载。 */
.note-container{display:flex;margin:24px 24px 24px 88px;width:1104px;height:752px}
.media-container{width:704px;background:#ddd}
.interaction-container{width:400px;display:flex;flex-direction:column}
.note-scroller{flex:1;overflow-y:auto}
.show-more{cursor:pointer;color:#38f}
</style>
<body>
<div class="side-bar side-bar-ai"><div class=pad>边栏</div></div>
<div class="main main-content with-side-bar"><div class="ai-feeds-page">
 <div class="search-layout">
  <div class="search-layout__top">
    <div class=filter id=filter><span id=flabel>筛选</span></div>
  </div>
  <div class="search-layout__main"><div class="feeds-wrapper">
    <div class="feeds-container" id=list></div>
  </div></div>
 </div>
</div></div>
<div class="note-detail-mask" id=mask>
  <div class=close-circle id=close>×</div>
  <div class="note-container" id=noteContainer>
    <div class="media-container" id=media>图片区（在这儿滚轮是翻图，不是滚评论）</div>
    <div class="interaction-container">
      <div class="note-scroller" id=scroller>
        <!-- 正文比一屏高得多：真笔记就是这样。弹窗一打开右栏停在正文顶部，
             评论区在**视口外**——不主动往下滚就永远看不到评论区。
             用户反馈"没看见作品页滚动到评论位置"说的正是这个。 -->
        <div style="height:1400px" id=note>正文</div>
        <!-- 没有评论时真站点会渲染一块「还没有评论」的占位
             （probe 07 实测 div.comments-el > div.no-comments），
             所以 comments-el 永远是有高度的，不会是个 0 高度的空壳 -->
        <div class="comments-el">
          <div class="no-comments" id=nocmt style="height:120px">
            还没有评论哦~</div>
          <div class="comments-container" id=cmts></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
let page = 0, loading = false;
async function loadPage() {
  if (loading || page >= 2) return;
  loading = true; page++;
  const r = await fetch('/api/sns/web/v2/search/notes', {method:'POST'});
  const j = await r.json();
  for (const it of j.data.items) {
    const s = document.createElement('section');
    s.className = 'note-item';
    s.setAttribute('data-note-id', it.id);
    const a = document.createElement('a');
    a.className = 'cover mask ld';
    a.href = '/search_result/' + it.id + '?xsec_token=' +
             encodeURIComponent(it.xsec_token) + '&xsec_source=';
    a.textContent = (it.note_card.display_title || '(无标题)');
    a.onclick = (e) => { e.preventDefault(); openNote(it.id, it.xsec_token); };
    s.appendChild(a);
    document.getElementById('list').appendChild(s);
  }
  loading = false;
}
loadPage();
// ⚠️ 列表里会混进「大家都在搜」这种**推荐位**：class 同样是 note-item，
// data-note-id 也有（UUID#时间戳那种），但里面是 div.query-note-wrapper，
// 没有 a.cover 也点不开。录制实测 113 张卡片里有 5 张是这种。
// 用户那次实跑就是挑中了它，日志报「点了卡片 …#1788434889907 但弹窗没出现」。
(function addQueryCard() {
  const s = document.createElement('section');
  s.className = 'note-item';
  s.setAttribute('data-note-id',
                 '97f78c57-0fb4-46f6-a225-ee0d6a65d58f#1788425134971');
  const w = document.createElement('div');
  w.className = 'query-note-wrapper';
  w.textContent = '大家都在搜';
  s.appendChild(w);
  document.getElementById('list').appendChild(s);
})();
// ⚠️ 真站点是**回收式虚拟列表**：DOM 里最多留一个固定窗口的卡片，
// 滚出去的整个删掉。录制实测 DOM 里稳定 29 张，每滚一屏换掉 5 张。
function recycle() {
  for (const el of document.querySelectorAll('section.note-item')) {
    const r = el.getBoundingClientRect();
    if (r.bottom < -600 || r.top > innerHeight + 600) el.remove();
  }
}
addEventListener('scroll', () => {
  recycle();
  if (innerHeight + scrollY > document.body.offsetHeight - 400) loadPage();
});

// ⚠️ 面板**只认 hover，而且是现建现删**：移上去才创建，移开立刻从 DOM 移除。
const filterEl = document.getElementById('filter');
const OPTS = [['排序依据', ['综合','最新','最多点赞','最多评论','最多收藏']],
              ['笔记类型', ['不限','视频','图文']],
              ['发布时间', ['一天内','一周内','半年内']],
              ['搜索范围', ['已看过','未看过']],
              ['位置距离', ['同城','附近']]];
let panelEl = null;
function buildPanel() {
  if (panelEl) return;
  panelEl = document.createElement('div');
  panelEl.className = 'filter-container';
  const wrap = document.createElement('div');
  wrap.className = 'filters-wrapper';
  for (const [title, opts] of OPTS) {
    const g = document.createElement('div'); g.className = 'filters';
    const h = document.createElement('div'); h.textContent = title;
    g.appendChild(h);
    const tc = document.createElement('div'); tc.className = 'tag-container';
    const tags = document.createElement('div'); tags.className = 'tags';
    for (const o of opts) {
      const sp = document.createElement('span');
      sp.textContent = o;
      sp.onclick = async () => {
        document.getElementById('flabel').textContent = '已筛选';
        filterEl.classList.add('active');
        killPanel();
        await fetch('/api/sns/web/v2/search/notes?filtered=1', {method:'POST'});
      };
      tags.appendChild(sp);
    }
    tc.appendChild(tags); g.appendChild(tc); wrap.appendChild(g);
  }
  panelEl.appendChild(wrap);
  panelEl.onmouseleave = maybeKill;
  document.querySelector('.search-layout__top').appendChild(panelEl);
}
function killPanel() { if (panelEl) { panelEl.remove(); panelEl = null; } }
function maybeKill() {
  setTimeout(() => {
    if (!filterEl.matches(':hover') && !(panelEl && panelEl.matches(':hover')))
      killPanel();
  }, 60);
}
filterEl.onmouseenter = buildPanel;
filterEl.onmouseleave = maybeKill;
filterEl.onclick = () => { if (panelEl) killPanel(); else buildPanel(); };

// 点卡片：开弹窗 + 打 feed 和 comment/page（快照 07 就是这个行为）
let cursor = 0, more = true, curId = '';
async function openNote(id, token) {
  curId = id; cursor = 0; more = true;
  history.pushState({}, '', '/explore/' + id + '?xsec_token=' +
    encodeURIComponent(token) + '&xsec_source=pc_search');
  document.getElementById('mask').classList.add('open');
  document.getElementById('cmts').innerHTML = '';
  document.getElementById('nocmt').style.display = '';
  document.getElementById('scroller').scrollTop = 0;
  await fetch('/api/sns/web/v1/feed?note_id=' + id, {method:'POST'});
  await loadComments();
}
document.getElementById('close').onclick = () => {
  document.getElementById('mask').classList.remove('open');
  history.pushState({}, '', '/search_result_ai?keyword=x&source=web_explore_feed');
  curId = '';
};
async function loadComments() {
  if (!curId || !more) return;
  const r = await fetch('/api/sns/web/v2/comment/page?note_id=' + curId +
                        '&cursor=' + cursor + '&xsec_token=T');
  const j = await r.json();
  more = j.data.has_more; cursor++;
  if (j.data.comments.length)
    document.getElementById('nocmt').style.display = 'none';
  for (const cm of j.data.comments) {
    const d = document.createElement('div');
    d.className = 'parent-comment';
    d.style.height = '160px'; d.textContent = cm.content;
    if (cm.sub_comment_has_more) {
      const rc = document.createElement('div');
      rc.className = 'reply-container';
      const m = document.createElement('div');
      m.className = 'show-more';
      m.textContent = '展开 2 条回复';
      m.onclick = () => fetch('/api/sns/web/v2/comment/sub/page?note_id=' + curId +
        '&root_comment_id=' + cm.id + '&num=10&cursor=');
      rc.appendChild(m); d.appendChild(rc);
    }
    document.getElementById('cmts').appendChild(d);
  }
}
// ⚠️ 评论翻页挂在**评论栏自己**的 scroll 上，不是 window。
// 鼠标停在左边图片区滚轮，这个事件永远不会触发——评论就卡在第一页。
document.getElementById('scroller').addEventListener('scroll', loadComments);
</script></body>"""

NOTE_HTML = """<!doctype html><meta charset=utf-8><title>笔记</title>
<body style="font-family:sans-serif">
<div style="height:500px">笔记正文 __ID__</div><div id=cmts></div>
<script>
const curId = "__ID__";
let cursor = 0, more = true;
async function loadComments() {
  if (!more) return;
  const r = await fetch('/api/sns/web/v2/comment/page?note_id=' + curId +
                        '&cursor=' + cursor + '&xsec_token=T');
  const j = await r.json();
  more = j.data.has_more; cursor++;
  for (const cm of j.data.comments) {
    const d = document.createElement('div');
    d.style.height = '160px'; d.textContent = cm.content;
    document.getElementById('cmts').appendChild(d);
  }
}
// ⚠️ 整页加载笔记页**不触发 /v1/feed**（用户实测：104 条条条等了 8 秒都没等到）。
// 这里照实复现：只加载评论，不打 feed。于是"直开 URL"这条路拿不到正文和时间。
loadComments();
addEventListener('scroll', loadComments);
</script></body>"""

BLOCKED_HTML = """<!doctype html><meta charset=utf-8><body>
<div>当前笔记暂时无法浏览</div></body>"""

HOME_HTML = """<!doctype html><meta charset=utf-8><title>小红书 - 你的生活兴趣社区</title>
<body style="font-family:sans-serif">
<!-- ⚠️ 这是 **AI 版首页**，照 2026-09-03 的 probe 01_home 和录制 006_click 复刻。
     这个账号上**根本没有** #search-input，也没有 .input-button .search-icon：
     搜索框是个 textarea#search-input-in-feeds，提交是右下角那张 <img>。
     以前只认经典版，于是每一次跑都静悄悄退回"直开 URL"，
     日志里只有一句"DOM 里压根没有 #search-input"——从没走通过输入这条路。 -->
<div class="search-area">
 <div class="input-box search-box-in-content">
  <div class="wendian-wrapper search-input">
   <div class="textarea-container">
    <div class="textarea-wrapper">
      <textarea id="search-input-in-feeds" name="aiSearchTextarea" class="textarea"
                placeholder="三明泰宁九龙潭"></textarea>
    </div>
    <div class="bottom-box"><div class="bottom-box-right">
      <div class="bottom-box-right-submit-button">
        <div class="submit-button-wrapper custom-icon">
          <img class="submit-button submit-button-image" alt=""
               style="width:32px;height:32px;background:#f33">
        </div>
      </div>
    </div></div>
   </div>
  </div>
 </div>
</div>
<script>
  const go = () => {
    const kw = document.getElementById('search-input-in-feeds').value;
    // 真站点从首页搜过去落的是 **/search_result_ai**（录制 seq 13 实测）
    location.href = '/search_result_ai?keyword=' + encodeURIComponent(kw) +
                    '&source=web_explore_feed';
  };
  document.querySelector('img.submit-button').onclick = go;
  document.getElementById('search-input-in-feeds').onkeydown =
    (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } };
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
        self.send(json.dumps({"success": True, "code": 0, "msg": "成功",
                              "data": obj}), "application/json")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/sns/web/v2/search/notes":
            hits["search"] += 1
            if "filtered=1" in (parsed.query or ""):
                filter_applied["on"] = True
                hits["search_after_filter"] += 1
                return self.js({"has_more": False,
                                "items": [search_item(n) for n in NOTES]})
            items = [search_item(n) for n in (NOTES if hits["search"] <= 1 else PAGE2)]
            return self.js({"has_more": hits["search"] <= 1, "items": items})
        if parsed.path == "/api/sns/web/v1/feed":
            hits["feed"] += 1
            nid = urllib.parse.parse_qs(parsed.query).get("note_id", [""])[0]
            if nid not in ALL:
                return self.js({"items": []})
            return self.js({"items": [feed_item(nid)], "current_time": 1})
        return self.send("{}", "application/json")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/sns/web/v2/comment/page":
            hits["comment"] += 1
            nid = q.get("note_id", [""])[0]
            cur = int(q.get("cursor", ["0"])[0])
            pages = COMMENTS.get(nid, [])
            payload = pages[cur] if cur < len(pages) else {"comments": [],
                                                           "has_more": False}
            return self.js(payload)
        if parsed.path == "/api/sns/web/v2/comment/sub/page":
            hits["sub"] += 1
            return self.js({"comments": SUB_COMMENTS, "has_more": False,
                            "cursor": ""})
        # 首页：模拟真人输入那条路的起点
        if parsed.path in ("/", ""):
            return self.send(HOME_HTML, "text/html; charset=utf-8")
        # 独立笔记页：_open_note 点不到卡片时走的兜底路径。
        # 真站点没 token 会渲染"当前笔记暂时无法浏览"且 HTTP 依然 200，这里照搬。
        if parsed.path.startswith("/explore/"):
            nid = parsed.path.rsplit("/", 1)[-1]
            if not q.get("xsec_token", [""])[0]:
                return self.send(BLOCKED_HTML, "text/html; charset=utf-8")
            return self.send(NOTE_HTML.replace("__ID__", nid),
                             "text/html; charset=utf-8")
        return self.send(SEARCH_HTML, "text/html; charset=utf-8")


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

    class Local(XhsBrowserCollector):
        """只把域名换成本地站点，其余逻辑一行都不改。"""
        web_host = base
        HOME_URL = base

        @property
        def _search_url_template(self):
            return base + "/search_result?keyword={keyword}&type=51"

    from app.core.config import load_config
    from app.proxy.manager import ProxyManager
    config = load_config(use_cache=False)
    config.set("redis.enabled", False)
    collector = Local(config, ProxyManager(config, None))

    logs = Log()
    ctx = CollectContext(
        scenic_id="S1", scenic_name="盘山风景区", task_id="t-xhs",
        max_works=10, max_comments_per_work=100,
        filters=SearchFilters(channel="xiaohongshu", sort="latest",
                              publish_within="unlimited"),
        logger=logs)
    ctx.log = lambda m, level="info": logs.lines.append((level, str(m)))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, **({"executable_path": CHROMIUM} if CHROMIUM else {}))
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        collector._page = page
        collector._run = lambda coro: coro          # 测试里就一条循环
        collector.PACE_RANGE = (0.01, 0.05)         # 停顿逻辑单独验，这里别真等
        collector.note_step = lambda s: None
        await collector._setup_network_capture(ctx)

        print("=== 1. 搜索：v2 接口 + 外层 id/token ===")
        works = []
        by_work = {}
        async for w in collector.collect_by_keyword(ctx, "盘山风景区"):
            works.append(w)
            # ⚠️ 和 runner 同构：产出一条就立刻采它的评论，不是全采完再回头。
            # 顺序错了的话"一条笔记只打开一次"这条根本测不出来。
            by_work[w.work_id] = [c async for c in collector.collect_comments(ctx, w)]
        ids = [w.work_id for w in works]
        print(f"   search {hits['search']} 次 / feed {hits['feed']} 次，"
              f"采到 {len(works)} 条：{ids}")
        if set(ids) != {"n1", "n2", "n3", "n4"}:
            print("   _order =", collector._order)
            for lv, m in logs.lines: print(f"     [{lv}] {m}")
        assert hits["search"] >= 2, "v2 搜索接口没被拦到（按 v1 拦就是这个下场）"
        assert set(ids) == {"n1", "n2", "n3", "n4"}, f"实际 {ids}"
        fallbacks = [m for lv, m in logs.lines if "点不开" in m]
        # 固定件是**虚拟列表**（滚出视口的卡片会被删掉），而且直开 URL
        # 不触发 feed。所以只要有一条"点不开"，publish_time 那条断言就会跟着红——
        # 这两条合起来才证明"边滚边开"真的战胜了虚拟列表。
        assert not fallbacks, f"有卡片点不开（被虚拟列表回收了）：{fallbacks[:2]}"
        assert collector._tokens["n2"] == "T-n2", "token 没从外层 item 取到"
        print("   ✓ v2 拦到了，note_id 和 token 都从外层 item 取的")

        typed = [m for lv, m in logs.lines if "已在搜索框输入" in m]
        fell_back = [m for lv, m in logs.lines if "改为直开 URL" in m]
        print(f"   {typed[0] if typed else '（没走输入框！）'}")
        assert typed and not fell_back, \
            f"没走「首页逐字输入」这条路：{fell_back[:1]}"
        print("   ✓ 是从首页搜索框敲进去的，不是每次精确落在结果页")

        print("\n=== 2. 关键：work 必须等 feed 到货才产出 ===")
        for w in works:
            print(f"   {w.work_id}  标题={w.title!r}  发布={w.publish_time}  "
                  f"正文={ (w.description or '')[:16]!r}")
        assert all(w.publish_time is not None for w in works), \
            "有 work 没有 publish_time——runner 的时间窗过滤会把它整片丢掉"
        assert all("盘山" in (w.description or "") for w in works), \
            "有 work 没有正文——runner 的关键字过滤会把它丢掉"
        n2 = next(w for w in works if w.work_id == "n2")
        assert n2.title == "无标题笔记", \
            "搜索卡片没有 display_title 的那条，标题没从 feed 补上"
        print("   ✓ 标题/正文/发布时间都来自 feed，过滤器有东西可用")

        print("\n=== 3. 筛选：面板选项进日志 + 点完要等新的搜索响应 ===")
        opts = [m for lv, m in logs.lines if "筛选面板里的选项" in m]
        picked = [m for lv, m in logs.lines if "已选中筛选项" in m]
        print("   " + (opts[0] if opts else "（没记录选项！）"))
        print("   " + (picked[0] if picked else "（没选中！）"))
        assert opts and "最新" in opts[0], "没把面板里的真实选项打出来"
        assert picked and "「最新」" in picked[0]
        for word in ("排序依据", "最多评论", "最多收藏", "位置距离"):
            assert word in opts[0], f"面板里的真实文案 {word} 没被记下来"
        assert filter_applied["on"], "筛选没真的打到服务端"
        # 面板是绝对定位、盖住右边一大片的（真站点约 450×570）。
        # 筛完不把鼠标挪开让它收起来，接下来右边两列的卡片就会点到面板上。
        still_open = await page.evaluate(
            "() => !!document.querySelector('div.filter-container')")
        assert not still_open, "筛选完面板还在 DOM 里——会盖住右边的卡片"
        # 鼠标落点在第 8 节单独验——这里读 collector._mouse 读到的是采集
        # 全程跑完之后的值，早被后面的滚动覆盖了，验不出筛选那一步的落点。
        # 「已筛选」这个入口态是筛选真的生效的判据（probe 05/06 逐字核对）
        active = await page.evaluate(
            "() => !!document.querySelector('div.filter.active')")
        assert active, "入口没变成 div.filter.active"
        assert not [m for lv, m in logs.lines if "很可能没落到搜索条件上" in m]
        print("   ✓ 筛完面板收起、鼠标停在列表上、入口变成了「已筛选」")
        print("   ✓ 选项可见、点中了、并且确认了结果刷新")

        print("\n=== 4. 评论：一级 + 白送的回复 + 展开出来的回复 ===")
        for w in works:
            got = by_work[w.work_id]
            print(f"   {w.work_id}: {len(got)} 条 "
                  f"{[(c.comment_id, c.comment_level) for c in got]}")
        a = by_work["n1"]
        got_ids = [c.comment_id for c in a]
        assert "c1" in got_ids and "c1-r0" in got_ids, "一级评论或白送的回复丢了"
        assert "c1-x0" in got_ids and "c1-x2" in got_ids, \
            "「展开 N 条回复」没点开，或者 sub/page 没拦到"
        assert hits["sub"] >= 1, "sub/page 一次都没被触发"
        # c3 在评论的**第二页**上。评论区是弹窗右栏自己的滚动容器，
        # 鼠标停在左边图片区滚轮翻的是图，评论会永远停在第一页——
        # 少了这条断言，"滚不到第二页"这个 bug 测不出来。
        assert "c3" in got_ids, \
            "评论只拿到第一页——滚轮多半落在弹窗左边的图片区上了"
        print("   ✓ 三种来源的评论都在，而且翻到了第二页")

        print("\n=== 5. 子评论的 root 必须来自 URL（响应体里没有） ===")
        for cid in ("c1-x0", "c1-x2"):
            c = next(x for x in a if x.comment_id == cid)
            print(f"   {cid}: root={c.root_comment_id}  parent={c.comment_parent_id}"
                  f"  level={c.comment_level}")
        x0 = next(c for c in a if c.comment_id == "c1-x0")
        x2 = next(c for c in a if c.comment_id == "c1-x2")
        # 两条回复的 root 都是顶楼 c1（来自 URL 的 root_comment_id）
        assert x0.root_comment_id == "c1" and x2.root_comment_id == "c1", \
            "root 没从 query 的 root_comment_id 取——评论树会被压成平的"
        # 但 parent 不同：x0 回复顶楼，x2 回复的是 x0
        assert x0.comment_parent_id == "c1", "直接父级取错了"
        assert x2.comment_parent_id == "c1-x0", \
            "「回复的回复」的父级应该是那条回复，不是顶楼——"\
            "拿 target_comment 当 root 用就会错在这里"
        assert x0.comment_level == "level_2" and x2.comment_level == "level_2"
        # 一级评论不该有父级，哪怕响应里带了 target_comment
        top = next(c for c in a if c.comment_id == "c1")
        assert top.comment_parent_id == "" and top.comment_level == "level_1"
        print("   ✓ root 是顶楼、parent 是直接回复的那条，两者区分开了；"
              "一级评论没有父级")

        print("\n=== 5b. 评论时间是**毫秒**，别当成秒解析 ===")
        # 用户实测：note_card.time 是秒（1756000000），
        # 而 comment.create_time 是毫秒（1788398738000）。
        # 当成秒解析会算到公元 58000 年，入库直接被截断或报错。
        for cid in ("c1", "c1-r0", "c1-x2"):
            c = next(x for x in a if x.comment_id == cid)
            print(f"   {cid}: {c.publish_time}")
            assert c.publish_time is not None, f"{cid} 没有评论时间"
            assert c.publish_time.year == 2026, \
                f"{cid} 的时间是 {c.publish_time}——毫秒被当成秒了"
        # 笔记那边仍然是秒，两条路都要对
        assert works[0].publish_time.year == 2025
        print("   ✓ 评论的毫秒和笔记的秒都解析对了")

        print("\n=== 6. 没评论的笔记：不空转，也不串到别人名下 ===")
        assert by_work["n3"] == []
        assert all(c.work_id == "n2" for c in by_work["n2"]), "评论串了"
        print(f"   n3 没有评论；comment 接口共 {hits['comment']} 次")
        assert hits["comment"] <= 14, f"翻页停不下来：{hits['comment']} 次"
        print("   ✓ 该停就停，各归各的")

        print("\n=== 7. 采评论时不重复打开同一条笔记 ===")
        # collect_by_keyword 已经打开过每条笔记，collect_comments 不该再开一遍
        print(f"   feed 一共 {hits['feed']} 次，笔记 {len(works)} 条")
        assert hits["feed"] == len(works), \
            f"每条笔记应该只打开一次，实际 feed {hits['feed']} 次"
        print("   ✓ 一条笔记只打开一次，作品详情和评论一起拿到")

        print("\n=== 8b. 列表里的「大家都在搜」推荐位要跳过，不能当笔记点 ===")
        skipped = [m for lv, m in logs.lines if "非笔记卡片" in m]
        dead = [m for lv, m in logs.lines if "没出现——多半是点空了" in m]
        print(f"   跳过的非笔记卡片 {collector._skipped_not_note} 张；"
              f"点空的报警 {len(dead)} 条")
        assert collector._skipped_not_note >= 1, \
            "混进列表的推荐位没被识别出来——会被当成笔记去点，然后报「点空了」"
        assert not dead, f"还是有卡片点空：{dead[:1]}"
        assert "97f78c57" not in " ".join(ids), "推荐位被当成笔记产出了"
        print("   ✓ 推荐位跳过了，没有一次「点空」")

        print("\n=== 8b2. 打开笔记之后要真的滚到评论区 ===")
        # 正文 1400px、右栏 752px：不滚的话 comments-el 根本不在视口里
        # 弹窗关掉之后 scrollTop 会被重置，事后量不到，所以看日志：
        # 每条笔记打开时都该有一行「已滚到评论区（往下滚了 N 下）」
        scroll_lines = [m for lv, m in logs.lines if "已滚到评论区" in m]
        for m in scroll_lines[:2]: print("   " + m)
        assert len(scroll_lines) == len(works), \
            f"每条笔记都该滚到评论区，实际 {len(scroll_lines)} / {len(works)} 条"
        # 正文 1400px、右栏 752px：不滚是绝对到不了的，所以次数必须 > 0
        assert not any("往下滚了 0 下" in m for m in scroll_lines), \
            "报告说滚了 0 下就看见评论区了——固定件没复现出「评论区在视口外」，"\
            "这条就白验了"
        print("   ✓ 每条笔记都往下滚到了评论区，不是一开就在那儿")

        print("\n=== 8c. 采集侧日志：进度行 + 字段来源 + 计数 ===")
        head_lines = [m for lv, m in logs.lines if "】笔记 " in m]
        count_lines = [m for lv, m in logs.lines if "计数：【" in m]
        for m in head_lines[:2]: print("   " + m)
        for m in count_lines[:2]: print("   " + m)
        assert len(head_lines) == len(works), \
            f"每条笔记该有一行进度，实际 {len(head_lines)} / {len(works)}"
        assert any("【1】笔记 n1" in m for m in head_lines), "进度行格式不对"
        assert all("字段来自" in m for m in head_lines), "没写明字段是从哪来的"
        assert len(count_lines) == len(works), \
            f"每条笔记该有且只有一行计数，实际 {len(count_lines)} 行"
        for w in works:
            i = next(k for k, (lv, m) in enumerate(logs.lines)
                     if f"笔记 {w.work_id}" in m)
            line = next(m for lv, m in logs.lines[i:] if "计数：【" in m)
            assert f"计数：【{len(by_work[w.work_id])}】" in line, \
                f"{w.work_id} 的计数对不上：{line}"
        # 字段明细归 runner 打，采集器这边不许重复打
        assert not [m for lv, m in logs.lines if m.startswith("[小红书]      作者")], \
            "采集器又打了一遍字段明细——会和 runner 的输出重复"
        print("   ✓ 进度、字段来源、计数都对，且没有和 runner 重复打字段")

        print("\n=== 8c2. feed 到货时不许喊「没等到 feed」 ===")
        # 上一版把这句警告误写在 elif 分支里，于是 feed **成功到货**时反而报警。
        # 用户看到的是"明明有正文有真实发布时间，却在喊只能用残缺字段"。
        false_alarm = [m for lv, m in logs.lines if "没等到 /v1/feed" in m]
        print(f"   feed 相关的提示 {len(false_alarm)} 条（固定件里 feed 每条都到货，应为 0）")
        for m in false_alarm[:2]: print("   " + m)
        assert not false_alarm, "feed 明明到了却在报「没等到」"
        assert all("来自 /v1/feed" in m for lv, m in logs.lines
                   if "      发布 " in m), "发布时间的来源标错了"
        print("   ✓ feed 到货时不报警，而且日志标明了字段来自 /v1/feed")

        print("\n=== 8c3. 两条笔记之间的停顿：1~8 秒轮换，不是固定值 ===")
        collector.PACE_RANGE = (1.0, 8.0)
        collector._pace_bag = []
        seen_delays = []
        real_sleep = asyncio.sleep

        async def _fake_sleep(sec):          # 只记时长，不真的等
            seen_delays.append(sec)
            await real_sleep(0)

        asyncio.sleep = _fake_sleep
        try:
            for _ in range(16):
                pass
                await collector._pace(ctx)
                # 每次 _pace 会拆成若干 0.5 秒的小段睡，加起来才是这一次的时长
                pass
        finally:
            asyncio.sleep = real_sleep
        # 把每次 _pace 的总时长还原出来
        totals = []
        acc = 0.0
        for d in seen_delays:
            acc += d
            if d < 0.5:          # 每次 _pace 的最后一小段一定小于 0.5
                totals.append(round(acc, 2)); acc = 0.0
        if acc:
            totals.append(round(acc, 2))
        print(f"   16 次停顿：{totals}")
        assert len(totals) == 16, f"停顿次数对不上：{len(totals)}"
        assert all(0.5 <= t <= 8.5 for t in totals), f"停顿超出 1~8 秒区间：{totals}"
        assert len(set(totals)) >= 8, f"停顿值太集中，不像轮换：{sorted(set(totals))}"
        assert max(totals) - min(totals) > 4, \
            f"最长和最短差不到 4 秒，等于没轮换：{min(totals)}~{max(totals)}"
        print("   ✓ 长短都有、值不重复，不是固定间隔也不是恒定均值")

        print("\n=== 8c4. 借来的字段映射，依赖也要借齐 ===")
        # XhsBrowserCollector 不继承 XhsCollector，只是借它的 _to_work。
        # 借函数就得把它 self 上用到的东西全补上，否则跑到映射那一步才
        # AttributeError——前面搜索、点开、采评论的功夫全白费。
        card_only = {"type": "normal", "display_title": "只有卡片的笔记",
                     "user": {"user_id": "u", "nickname": "某人"},
                     "interact_info": {"liked_count": "1"},
                     "corner_tag_info": [{"type": "publish_time", "text": "2天前"}]}
        w = collector._to_work(ctx, card_only, "card1", "T", source_keyword="盘山")
        print(f"   只有搜索卡片时：publish_time={w.publish_time}")
        assert w.publish_time is not None, \
            "拟人版没借到 _corner_publish_time——卡片角标的发布时间用不上"
        assert (datetime.now() - w.publish_time).total_seconds() > 3600 * 47
        print("   ✓ 角标发布时间在拟人版这条路上也生效")

        print("\n=== 8c5. 连续点不开就放弃这个关键字，别空转到看门狗超时 ===")
        # 用户实测：连着 12 条「点了卡片但弹窗没出现」，每条还要等 8 秒 feed
        # + 1~8 秒停顿，就这么空转到 6 小时看门狗把整个任务强杀。
        import app.collectors.xhs_browser as XB
        assert XB.XhsBrowserCollector.OPEN_FAIL_STREAK_LIMIT >= 1
        logs.lines.clear()
        hits["search"] = 0                  # 让固定件重新发第一批卡片
        collector._seen_work_ids.clear()
        collector._order.clear(); collector._cards.clear(); collector._details.clear()
        collector.PACE_RANGE = (0.01, 0.05)
        # 固定件一共只有 4 张卡片，把阈值调到 3 才验得到"没试完就收手"
        collector.OPEN_FAIL_STREAK_LIMIT = 3
        # 让每条笔记都打不开：点击永远返回 False
        original = collector._click_card

        async def _never_opens(ctx_, note_id_):
            return False

        collector._click_card = _never_opens
        got = []
        async for w in collector.collect_by_keyword(ctx, "盘山风景区"):
            got.append(w)
        collector._click_card = original
        gave_up = [m for lv, m in logs.lines if "放弃关键字" in m]
        print("   " + (gave_up[0][:110] if gave_up else "（没有放弃，一直在空转！）"))
        assert gave_up, "连续打不开却没有放弃这个关键字——会一直空转到看门狗超时"
        assert not got, f"一条都打不开却产出了 {len(got)} 条"
        # 而且要在**限额之内**就停，不能把 max_works 条全试一遍
        print(f"   连续失败 {collector.OPEN_FAIL_STREAK_LIMIT} 条就收手，"
              f"没有把 max_works={ctx.max_works} 条全试一遍")
        print("   ✓ 连续打不开会放弃这个关键字，让 runner 去跑下一个")

        print("\n=== 8d. 「0 条评论」要分清是真没有、还是 runner 没来采 ===")
        # runner 的关键字/时间过滤会把一部分 work 直接丢掉，压根不来采评论。
        # 这两种情况都是"0 条"，但原因天差地别，日志必须说得出区别。
        hits["search"] = 0
        collector._seen_work_ids.clear()
        collector._order.clear()
        collector._cards.clear()
        collector._details.clear()
        logs.lines.clear()
        got = []
        async for w in collector.collect_by_keyword(ctx, "盘山风景区"):
            got.append(w)          # ← 故意不采评论，模拟被过滤掉的 work
            if len(got) >= 2:
                break
        zero = [m for lv, m in logs.lines if "调用方没有紧接着采" in m]
        for m in zero[:2]: print("   " + m)
        assert zero, "被 runner 丢掉的 work 也只打「0 条评论」，看日志分不出原因"
        print("   ✓ 说清了是「没进入评论采集」，而不是「这条没评论」")
        # 上面是从循环里 break 出来的，弹窗还开着、页面还停在半路，
        # 下一节要量滚动，先收拾干净
        await collector._close_modal()
        await page.evaluate("() => scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        print("\n=== 8. 鼠标该停哪儿：滚轮滚的是指针底下那个元素 ===")
        fx, fy = await collector._feed_point()
        print(f"   列表落点 ({fx}, {fy})")
        assert fx > 164, "列表落点落在左侧边栏上了"
        # 收筛选面板那一下的落点也要验：单独调一次，当场读，不读跑完的残值
        await page.mouse.move(600, 300)
        collector._mouse = (600, 300)
        await collector._dismiss_filter_panel()
        dx, dy = collector._mouse
        print(f"   收筛选面板后鼠标停在 ({dx}, {dy})")
        assert dx > 164, f"收面板时把鼠标挪到左侧边栏上了（x={dx}）"
        # 反过来证一次：把指针挪到边栏上再滚，列表纹丝不动
        await page.evaluate("() => scrollTo(0, 0)")
        await page.mouse.move(60, 400)
        await page.mouse.wheel(0, 800)
        await asyncio.sleep(0.6)
        on_bar = await page.evaluate("() => scrollY")
        await page.mouse.move(fx, fy)
        await page.mouse.wheel(0, 800)
        await asyncio.sleep(0.6)
        on_feed = await page.evaluate("() => scrollY")
        print(f"   指针在边栏上滚完 scrollY={on_bar}；挪到列表上再滚 scrollY={on_feed}")
        assert on_bar == 0, "固定件没复现出「边栏吃掉滚轮」，这条就白验了"
        assert on_feed > 0, "指针在列表上却还是滚不动"
        print("   ✓ 停在边栏上真的滚不动，停在列表上才滚得动——所以落点必须算")

        await browser.close()
    server.shutdown()
    print("\n全部通过 ✓")


asyncio.run(main())
