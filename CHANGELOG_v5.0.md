# v5 变更说明（2026-09-05）

这一版做了两件事：**把快手拟人采集真正跑通**，以及**把统一日志版式落进正式逻辑**。

---

## 一、快手：五个真 bug

都是实跑（`verify_kuaishou_browser.py --keyword 盘山风景区`）暴露、
再对照用户提供的真实接口报文确认的。

### 1. 评论一条都出不来 ⭐ 最严重

`_extract_comments_from_body()` 只在 `body["data"]` 里找评论。
**真实响应是平铺的**：

```json
{"result":1, "commentCountV2":305, "pcursorV2":"...",
 "rootCommentsV2":[ ... ]}
```

后果：接口**拦到了**，解析成 0 条。日志显示
「打开评论区后 8 秒没有评论到货」，看起来像页面没点开，
实际上数据早就到手了——这是最难查的一类：现象指向 A，病因在 B。

修：顶层和 `data` 都找，键名优先认 V2；游标改读 `pcursorV2`。

### 2. 关联播点不动

```
Locator.click: Timeout 5000ms exceeded.
  locator resolved to <span aria-disabled="true"
      class="toggle-switch is-active is-disabled size-small">
  - element is not enabled
```

开关所在那块 class 叫 `hover-tip`——**鼠标悬停上去才解锁**。
Playwright 认 `aria-disabled`，不悬停就一直等到超时。

修：先把鼠标挪到控件区 → 轮询等 `aria-disabled`/`is-disabled` 掉 → 再点。
连续 3 条都锁着就不再每条白等 5 秒。

### 3. 搜索是拼 URL，不是打字

原来直接 `goto("/search/{关键字}?source=NewReco")`。
用户要求的动线是：进首页 → 在搜索框里一个字一个字打 → 点「搜索」。

修：按真人动线走，找不到搜索框才退回拼 URL（宁可动线不像，
也别让整个关键字废掉）。

### 4. 作品的评论数永远是 0

搜索接口**给不出**这个数：`photo` 里没有 `commentCount`，
`feed["comment"]` 只有 `{"us_c": 0}`。真值在评论接口的 `commentCountV2`。

修：翻完评论回填到作品行（趁它还在缓冲区没落库）。

### 5. 两个字段名本来就是错的

| 字段 | 原来读 | 真实字段 | 后果 |
|---|---|---|---|
| 视频地址 | `photoUrl` / `mainMvUrls` | `photoUrls` / `photoH265Urls` | `video_list` 一直是空的 |
| 评论点赞 | `likedCount` | `likeCount` | 永远 0 |

话题也改成优先取 `feed["tags"]`（结构化字段），拿不到再从正文抠 `#话题`。

### 接入状态

`integrated` 由 `False` 改为 **`True`**：三个接口路径都由真实抓包确认，
实跑也已经从 `/rest/v/search/feed` 拿到过真实作品。

---

## 二、离线自检本身是错的 ⚠️

`verify_kuaishou_offline.py` 的固定件（`data.rootComments` / `commentId` /
`authorName` / `likedCount`）是**照我自己编的形状写的**，线上根本不存在。
于是它 6 段全绿、实跑却一条评论都出不来——**测出来的绿是假的**。

现在固定件全部照抄真实报文的字段名重建，并且：

- 卡片**只有点了「搜索」才出来**（原来是页面一加载就 `loadFeed()`，
  等于把"输入关键字→点搜索"这段动线整个跳过，拼 URL 也照样绿）
- 联播开关带 `aria-disabled="true"`，**悬停才解锁**（复现实跑的失败）

扩到 8 段。四个修复**逐条还原验证过**，还原后自检都能抓到，
报的就是实跑那个症状：

```
✅ 抓到  改坏「评论解析看顶层」   → 一级评论没采到
✅ 抓到  改坏「关联播先 hover」   → 没关联播
✅ 抓到  改坏「点赞用 likeCount」 → 点赞数没映射上：0
✅ 抓到  改坏「评论总数回填」     → 评论总数没回填：0
```

`verify_kuaishou_browser.py` 的 preflight 从 18 条加到 **24 条**，
新增六条正好钉住这一轮的坑（不连网、不开浏览器，秒出）。

---

## 三、统一日志版式（抖音 / 快手 / 小红书 / 微博）

版式放在 `runner.py`，所以**四个平台自动一个样**，
只有「作品 / 笔记 / 微博」这个称呼跟着平台走。

```
  ┌─ 作品 8/100 ────────────────────────────────────────────────────
  │ work_id    3xzxqey42rgz2iy
  │ 发布时间       2026-04-10 22:17:25
  │ 作者         致水  (3x96xr46png6y2k)
  │ 标题/正文      盘山公路 #风景都在路上而不是终点
  │ 话题标签       风景都在路上而不是终点
  │ 互动         赞 36 / 评 10 / 藏 0 / 转 0
  │ IP 属地      -
  │ 来源关键字      盘山风景区
  │ 作品链接       https://www.kuaishou.com/short-video/3xzxqey42rgz2iy
  ├────────────────────────────────────────────────────────────────
  │      （第 1 屏，展开了 2 处回复）
  │      [  4] 明天更好81273：儿子，我看到老家这些风景，我心像刀绞一样…
  │            赞 0 · 2026-04-11 21:08:19 · level_1 · 1173092536515
  │          ↳ [  5] 致水：家乡才是根，我们在外面也是没办法的
  │          ↳       赞 0 · 2026-04-11 21:31:52 · level_2 · 1173067543199
      └─ 小计 10 条评论
```

顺带修的三处：

1. **回复归位**。原来是"七条一级评论 + 三条不知道挂谁下面的二级评论"——
   回复是后到的（要滚到那儿、点开「查看更多回复」才拉）。
   现在每条作品的评论攒齐再排序打印：顶楼按时间倒序，
   回复挂在自己顶楼下面按时间正序。
   ⚠️ 攒的只是**打印用的引用**，入库还是来一条 `add` 一条，中途取消不丢数据。
2. **「连续 4 屏没有新评论」刷三遍**。上层为保险会再调两轮滚动，
   每轮都喊一次。现在一条作品只说一次，而且改走 `ctx.log`——
   原来是模块 logger，带 `[INFO] app.collectors...` 一长串前缀
   插在方框中间，把版面冲乱。
3. **方框收不了尾**。「今天已经采过」「本平台关闭了评论采集」
   这几个分支以前是另一种格式，方框一直开着。
   现在统一成 `└─ 小计 0 条评论（原因）`。

评论 id 挂在第二行末尾：用户给的版式里没有，但早先明确要过
「评论ID，作者，发布时间」，而 id 是出问题时按行找库的唯一抓手。
放第二行末尾不动第一行；不想要就删 `log_comment_detail` 里那一处。

新增 `tests/test_task_runner_logging.py`（9 条）把版式钉死：
四个平台参数化跑同一套断言，九行字段少一行就红，
还盯着排序、孤儿回复不被吞、时间缺失不炸排序。

---

## 测试

- `pytest tests -q`（排除 UI smoke）：**329 passed**
- `verify_kuaishou_offline.py`：8 段全过
- `verify_kuaishou_browser.py` preflight：24 项全绿
- `verify_xhs_offline.py` / `verify_record_offline.py` / `verify_boot.py`：全过

⚠️ `tests/test_browser_session.py::test_stale_but_present_cookie_is_not_logged_in`
**偶发失败**（报 BrokenPipeError，单独跑和多数全量跑都过）。
它测的是 cookie 过期判定，和这一轮改动无关，但它确实是 flaky 的。

⚠️ UI smoke 那 4 条在我的环境里失败，原因是容器出网被拦
（图片代理去取 `sinaimg.cn` 返回 502），和代码无关，你本地应该正常。

---

## 还没做

- ③ 水印去重（关键字 4 小时 / 作品ID 7 天，都要配置化）
- 「需要重新登录就暂停，等待登录后开始」（现在 `LoginRequired` 只是跳过该平台）
- 概览页面统计——我核对过数字自洽，还没确认你说的是哪个值不对

---

## 安全（每版都提醒）

- 包里**不含** `config/config.yaml` 和 `data/`：前者是你的四套明文口令，
  后者是浏览器配置目录（等于登录态）。解压不会覆盖你本地的。
- 快代理的 `secret_key` 建议轮换（它会产生费用），
  口令挪到环境变量：`SMC_MYSQL_PASSWORD` / `SMC_PROXY_SECRET_KEY` / `SMC_REDIS_PASSWORD`。
- 实时画面页会显示已登录的平台页面，服务本身**没有鉴权**，
  不要暴露到公网（`run.py` 在 `--host 0.0.0.0` 时会警告）。
