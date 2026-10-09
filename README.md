# 景区社媒采集系统

按**景区维度**采集抖音、快手、小红书、微博、携程、同程六个平台的**作品**与**评论**，
统一入 MySQL，支持关键字采集与指定用户主页采集，全平台走同一套代理 IP 轮换，
Web 界面部署，任务支持 一次 / 定时 / 间隔 / cron 四种调度。

---

## 一、当前进度

| 模块 | 状态 | 说明 |
|---|---|---|
| 配置系统 config.yaml | ✅ | 四层覆盖：默认值 < config.yaml < 环境变量 < 页面设置 |
| MySQL 数据模型 | ✅ | 10 张表，已在真实 MySQL 验证建表与 upsert |
| 代理池（快代理 + Redis） | ✅ | 从携程脚本抽成全平台公用组件，轮换逻辑有测试覆盖 |
| **携程** 采集器 | ✅ | 翻页、去重、多级评论、字段映射均有测试 |
| **同程** 采集器 | ✅ | 字段映射已按线上真实响应对齐，含真实样本回归测试 |
| **抖音** 采集器 | ✅ | 搜索 / 主页 / 两级评论；a_bogus 走 Node 调 douyin.cjs，**综合搜索不签名**（见第八节） |
| **快手** 采集器 | ✅ | 搜索 / 主页 / 两级评论；__NS_hxfalcon 签名走页面环境，含限流退避 |
| **小红书** 采集器 | ✅ | 搜索 / 主页 / 两级评论；X-S 纯 Python 算法，不需要浏览器 |
| **微博** 采集器 | ✅ | 搜索 / 主页 / 两级评论；无签名，纯 Cookie |
| 账号 profile + 静默 Cookie | ✅ | 真实 Chromium 验证：静默刷新不再弹登录页 |
| 手动导入 Cookie | ✅ | Cookie-Editor 的 JSON / 请求头串都认，并写进浏览器 profile |
| 实时流交互浏览器登录 | ✅ | CDP screencast 推流 + 鼠标键盘回放，已验证 |
| 任务调度（含 cron） | ✅ | 四种模式，cron 支持 5 段标准与 6 段 Quartz |
| 按平台设采集数量 | ✅ | 作品型平台设「作品数 + 每作品评论数」，携程/同程设「点评数」 |
| 搜索排序与时间范围 | ✅ | 抖音/小红书/微博走接口筛选，快手本地过滤；时间窗全平台兜底 |
| 任务在线编辑 | ✅ | 运行中的任务也能改运行模式和采集数量，不用重建 |
| REST API + WebSocket | ✅ | 53 个接口，`/docs` 可交互调试 |
| CSV 导出 | ✅ | 流式输出，中/英文表头，Excel 直接打开不乱码 |
| 部署（Docker + 裸机） | ✅ | compose 一键起，或 start.sh / start.bat |
| 前端 Web 界面 | ✅ | Vue3 + Element Plus，7 个页面；真实浏览器冒烟测试覆盖 |

测试：**282 项全部通过**

| 文件 | 项 | 覆盖 |
|---|---|---|
| `test_end_to_end.py` | 36 | 建景区 → 建任务 → 执行 → 落库 → 查询 → 导出，含任务在线编辑与 Cookie 导入 |
| `test_collect_params.py` | 19 | 按平台的采集数量解析、清洗、生效值汇总 |
| `test_search_filters.py` | 26 | 排序与时间范围：各平台能力、时间窗换算、本地过滤与提前停 |
| `test_social_collectors.py` | 46 | 抖音/快手/小红书/微博的参数与字段映射，含微博登录态自检、小红书详情回查 |
| `test_ctrip_collector.py` | 14 | 携程翻页/去重/多级评论 + **线上真实响应回归** |
| `test_subprocess_loop.py` | 10 | 子进程事件循环，含"有没有人绕过它"的结构性扫描 |
| `test_ui_smoke.py` | 12 | 真实 Chromium 打开每个页面，含分平台数量表单与任务编辑 |
| `test_tongcheng_collector.py` | 8 | 同程，含线上真实响应回归 |
| `test_cookie_import.py` | 36 | Cookie 解析（两种格式）与登录态判定，含"游客 Cookie 被当成已登录"的复现 |
| `test_browser_session.py` / `test_live_browser.py` / `test_proxy_rotation.py` | 27 | 静默 Cookie、跨域登录、**过期 Cookie 识别**、profile 单例锁、推流登录、代理轮换 |
| `test_table_rename.py` | 4 | 老表改名 + 索引补建：真实 MySQL 验证数据跟着走、重复启动幂等 |
| `test_dedup.py` | 8 | 跨关键字去重、重复作品不重复采评论、单条评论失败不拖垮整个平台 |
| `test_resilience.py` | 12 | 单条数据解析失败跳过、关键字级重试、取消任务收尾、定时体检避让 |
| `test_media_proxy.py` | 22 | 图片代理：绕开防盗链，以及不能变成通用代理（SSRF）|

---

## 二、快速开始

### 方式 A：Docker Compose（推荐）

```bash
cp docker/.env.example docker/.env      # 按需改密码和端口
cp config/config.example.yaml config/config.yaml
docker compose -f docker/docker-compose.yml up -d
```

起来之后：

- 前端：http://localhost
- 接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health

MySQL 和 Redis 都在 compose 里，**首次启动自动建库建表**，不用手工执行 SQL。

### 方式 B：裸机

前提：Python 3.10+、MySQL 8.0+、Redis 6+（Redis 可选）

```bash
# Linux / macOS
./scripts/start.sh

# Windows
scripts\start.bat
```

首次运行会自动生成 `config/config.yaml` 并提示你填写，填完再跑一次即可。

### 环境自检

```bash
python scripts/selfcheck.py           # 全量：MySQL / Redis / 浏览器 / 代理 / 目标站点
python scripts/selfcheck.py --quick   # 只查 MySQL / Redis
python scripts/selfcheck.py --sites   # 只查目标站点连通性
```

---

## 三、配置

配置优先级（后者覆盖前者）：

```
代码内默认值  <  config/config.yaml  <  环境变量 SMC_*  <  页面上的系统设置
```

页面设置放在最后，是因为需求要求「在系统设置里配代理」——改完必须立刻生效，
不能被文件里的旧值盖回去。可在页面改的段：`proxy`、`crawl`、`browser`、`export`、
`scheduler`、`platforms`；`mysql`、`redis`、`server` 只能改文件，避免把服务改挂。

环境变量命名：`SMC_<段>_<键>`，嵌套用双下划线：

```bash
SMC_MYSQL_PASSWORD=xxx
SMC_PROXY_ENABLED=true
SMC_PLATFORMS__CTRIP__MAX_PAGES=50
```

### 代理

`config.yaml` 的 `proxy` 段填快代理的 `secret_id` / `secret_key` / 私密代理用户名密码即可。
关键项：

- `pool_scope: channel` — 每个平台各持有一个出口 IP，一个平台被封不牵连其他平台
- `pool_scope: global` — 全平台共用一个 IP，省提取次数
- `rotate_every_n_requests` — 大于 0 时每 N 次请求主动换 IP；0 表示只按 TTL 与失败信号换

出口 IP 缓存在 Redis 里带 TTL，多进程用 `SET NX` 锁避免重复提取浪费次数。
遇到连接异常 / 超时 / 403 / 407 / 429 / 5xx 会立即弃用当前 IP 并换新，**同时换设备指纹**。

---

## 四、界面

七个页面，前后端同端口部署（后端直接托管前端构建产物）：

| 页面 | 作用 |
|---|---|
| 概览 | 景区数、作品/评论总量、最近任务、六平台接入与账号状态 |
| 景区管理 | 景区增删改 / CSV 导入；展开行里管关键字和各平台采集目标 |
| 任务管理 | 任务列表、执行/取消、定时开关、**编辑**（改运行模式与采集数量），5 秒自动刷新 |
| 新建任务 | 选景区 → 选平台 → 从景区批量带关键字 → **按平台设采集数量与排序/时间范围** → 四种调度（cron 带下次执行预览） |
| 任务详情 | WebSocket 实时日志终端 + 采集量统计 + 各平台生效上限，可直接修改 |
| 数据中心 | 顶部统计 + 景区×平台明细（点一下即筛选）；作品卡片列表，展开后在**固定高度窗口**里逐级看评论；图片视频可看可播；导出 CSV |
| 账号管理 | 六平台账号；**账号分组**（同组轮换采集）；打开浏览器（实时流）登录；采集 Cookie；粘贴导入 Cookie；**试搜**（用真实链路跑一个关键字看到底哪一步出问题）；**全平台体检**（六个平台挨个试搜一遍） |
| 系统设置 | 代理配置与测试、采集参数、调度与浏览器；密钥不回显 |

## 五、使用流程

```
1. 景区管理  →  导入景区（CSV 或手工添加 景区ID + 景区名称）
2. 景区管理  →  给景区添加关键字
3. 景区管理  →  给景区添加平台采集目标
                 携程填 POI_ID，同程填 sid，社媒平台填作者主页ID
4. 账号管理  →  按平台添加账号并登录一次（携程、同程无需登录）
5. 新建任务  →  选景区、选平台、从景区带入关键字（默认上限 100 个）
                 设采集数量与搜索筛选：统一设 或 按平台分别设
                 选调度方式：立即 / 定时 / 间隔 / cron
6. 任务管理  →  跑起来之后要改？点「编辑」改运行模式和数量，不用重建任务
7. 数据中心  →  按景区 + 平台看 作品 → 创作者 → 评论（可逐级展开）
8. 导出      →  CSV
```

### 排序与时间范围

各平台的能力差别很大，界面上按平台显示对应的选项，**不支持的直接说不支持**，
不给假的下拉框：

| 平台 | 服务端排序 | 服务端时间筛选 |
|---|---|---|
| 抖音 | 综合 / 最新发布 / 最多点赞 | 不限 / 一天内 / 一周内 / 半年内 |
| 小红书 | 综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏 | 不限 / 一天内 / 一周内 / 半年内 |
| 微博 | 综合 / 实时 / 热门 | **任意日期区间**（`timescope=custom:开始:结束`） |
| 快手 | ❌ 接口没有这个参数 | ❌ 接口没有这个参数 |

快手那一行不是没做——它的搜索接口（MediaCrawler 的 `visionSearchPhoto` GraphQL
和新版 `/rest/v/search/feed`）只收 `keyword / pcursor / searchSessionId / page`
四个字段，压根没有排序和时间。

所以**时间范围是两层实现的**：

1. 接口能筛的在接口筛 —— 少翻页、少请求、少被风控盯上
2. 不管接口支不支持，落库前都按时间窗**再过滤一遍**

这样「按时间范围采集」在四个平台上都成立，而不是变成"抖音小红书能筛、快手不能"
这种参差的体验。任务日志会写明这一轮是接口侧筛的还是本地过滤的。

选「最新发布」排序时还会**提前收工**：一旦翻到比时间窗下界更早的内容，
后面只会更早，继续翻纯属浪费请求。综合排序下顺序是乱的，所以不做这个优化。

发布时间解析不出来的内容一律放行——宁可多收一条，也不要因为某个平台的时间格式
没认出来就把数据默默丢掉。

### 采集数量怎么设

两类平台的数据结构不一样，所以能设的数量也不一样：

| 平台 | 层级 | 可设的数量 |
|---|---|---|
| 抖音 / 快手 / 小红书 / 微博 | 作品 → 作品下的评论 | 每关键字作品数、每作品评论数、是否采评论 |
| 携程 / 同程 | 只有景区点评一层 | 采集点评数、最多翻几页 |

携程/同程的「最多翻几页」以前写死 300 页，现在任务里能设，
**填 0 表示一直翻到接口没有数据为止**——首次全量拉取通常想要这个。

界面上先选「所有平台统一」还是「按平台分别设置」。存进任务的 `params` 长这样：

```jsonc
{
  "max_works": 100,               // 顶层默认，没配平台级时用它
  "max_comments_per_work": 500,
  "max_comment_level": 3,
  "channel_params": {             // 平台级覆盖，只写想改的键
    "douyin":    { "max_works": 50, "max_comments_per_work": 200 },
    "weibo":     { "collect_comments": false },
    "ctrip":     { "max_comments": 3000 }
  }
}
```

优先级 `channel_params[平台] > 顶层 > config.yaml 的 crawl.*`。
`0` 表示不限制。每轮任务开始时，日志第一行会打印本平台生效的上限，
改完之后跑一次就能确认有没有生效。

### 改一个已经建好的任务

`PATCH /api/tasks/{task_id}`，只传要改的字段（任务列表和详情页的「编辑」走的就是它）：

```bash
curl -X PATCH http://localhost:8000/api/tasks/xxxx \
  -H 'Content-Type: application/json' \
  -d '{"schedule_type":"cron","cron_expression":"0 */4 * * *",
       "params":{"channel_params":{"ctrip":{"max_comments":2000}}}}'
```

正在运行的任务也能改：本轮已经开始的平台仍用启动时那份配置，
**改动从下一轮开始生效**，接口返回的 message 会说明这一点。
任务跑完排下一次时间时会重新读数据库，所以运行中改的调度不会被旧快照覆盖回去。

---

## 六、数据表

### 查询性能

数据中心是唯一会被反复刷新的页面，三处针对性优化：

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| 概览（景区×平台） | 每次都把作品表和评论表**各整个聚合一遍**，另加两次 `SELECT DISTINCT channel` 全表扫 | 平台清单改用常量（就那六个，是已知的），结果缓存 30 秒，写库后主动失效 |
| 作品列表默认视图 | 无筛选 + `ORDER BY publish_time DESC`，没有对应索引 → 全表扫 + filesort | `idx_works_publish (publish_time, id)`，排序整条走索引 |
| 展开评论 | `channel + work_id + comment_level` 过滤后按时间倒序，排序列不在索引里 | `idx_comments_work_level_time`，把排序列也放进去 |

概览缓存 30 秒是有意的：采集持续在跑，数字本来就时刻在变，
晚 30 秒看到和实时看到对用户没有任何区别，但省掉的是全系统最贵的一个查询。

> ⚠️ **新增索引必须同时登记到 `app/db/tables.py` 的 `EXTRA_INDEXES`**。
> `CREATE TABLE IF NOT EXISTS` 对已存在的表**什么都不做**——只往 schema.sql 里加一个 KEY，
> 老库是加不上的，用户升级完表结构还是旧的、页面照样慢，而且没有任何提示。
> 启动时会按索引名比对，缺哪个补哪个（`_ensure_extra_indexes`），
> `test_table_rename.py` 里有一条测试盯着这件事。


核心两张表字段与需求文档一致，另有 8 张支撑表。表名统一 `src_opinion_` 前缀：

| 表 | 中文名 | 用途 |
|---|---|---|
| `src_opinion_social_work_di` | 社媒平台作品 | 作品 / 帖子 / 笔记 |
| `src_opinion_social_work_comment_di` | 社媒平台评论 | 含多级评论 |
| `src_opinion_social_authors` | 社交媒体平台创作者 | 创作者档案 |
| `src_opinion_social_scenic` | 平台景区 | 景区主数据 |
| `src_opinion_scenic_keyword` | 平台景区采集关键字 | 每个景区的搜索词 |
| `src_opinion_scenic_platform_target` | 平台景区采集目标 | 携程 POI_ID / 同程 sid / 作者主页 |
| `src_opinion_social_account` | 社媒平台账号 | 账号与登录态 |
| `src_opinion_crawl_task` | 景区采集任务 | 任务定义与调度 |
| `src_opinion_crawl_task_log` | 系统任务日志 | 每次运行的过程日志 |
| `src_opinion_sys_setting` | 系统设置 | 页面上改的配置 |

**表名只在 `backend/app/db/tables.py` 定义一处**。表名是给下游数仓和 BI 用的契约，
散在几十条 SQL 字符串里迟早会改漏，所以建表脚本和仓储层的查询都引用那份常量。

> **从旧版本升级**：早期用的是 `task` / `scenic` / `social_media_works` 这种短表名。
> 启动时会自动 `RENAME TABLE` 成上面的名字（`app/core/db.py` 的 `_rename_legacy_tables`），
> **数据原样保留，不需要手工迁移**。改名发生在建表之前——顺序反了的话
> `CREATE TABLE IF NOT EXISTS` 会先把空的新表建出来，老表就成了孤儿，
> 界面上看就是"以前采的数据全没了"。

### 相对需求文档的几处工程性调整

1. **`publish_time` / `crawl_time` 用 `DATETIME` 而不是 `TIMESTAMP`**
   `TIMESTAMP` 上限是 2038-01-19，且会随会话时区做隐式转换——
   同一条数据在不同时区的容器里读出来时间会变。`DATETIME` 没这两个问题。
   `create_time` / `update_time` 仍用 `TIMESTAMP`，因为要靠它自动维护。

2. **唯一键用 MD5 生成列**
   `work_uk = MD5(scenic_id  ␟  channel  ␟  work_id)`，
   `comment_uk = MD5(scenic_id  ␟  channel  ␟  work_id  ␟  comment_id)`。
   这样 `INSERT ... ON DUPLICATE KEY UPDATE` 能直接工作，重复采集不会产生重复行。
   注意唯一性**带上了 scenic_id**：一条同时提到两个景区的内容会在两个景区下各存一份，
   否则按景区看数据时会丢。

3. **平台标识用 `douyin`**
   需求文档里写的是 `douying`（笔误）。如果下游系统必须收到 `douying`，
   只改 `backend/app/core/constants.py` 里 `CHANNEL_DOUYIN` 一处即可。

4. **携程/同程会生成一条"合成作品"**
   这两个平台没有作品概念，但数据中心是「景区 → 平台 → 作品 → 评论」这棵树。
   所以每个 POI 会生成一条作品记录作为点评的挂载点，
   `extra_content` 里标了 `"synthetic": true`，
   查询作品列表时传 `include_synthetic=false` 即可过滤掉。

5. **需求字段之外补了几列**（都不影响原有字段）
   - 作品：`extra_content`（平台特有字段，避免信息丢失）、`source_keyword`（命中的关键字）、`task_id`
   - 评论：`commenter_name`（列表要显示昵称）、`root_comment_id`（一次查出整条会话）、
     `sub_comment_count`（前端据此决定是否显示"展开回复"）、`task_id`

6. **AI 标注字段只建列不写值**
   `sentiment_label`、`sentiment_score`、`dimension_tags`、`entity_tags`、`keyword_tags`
   按你的要求本期不写，但列已经建好，后续接标注服务时不用改表。

7. **`image_list` / `video_list` 存 JSON 数组，一个资源只存一条地址**
   各平台一张图往往会给出多个尺寸（携程的 `imageSrcUrl` 原图 + `imageThumbUrl` 缩略图，
   同程的 `originalImgUrl` / `imgUrl` / `smallImgUrl`），全收下来会变成同一张图存三条。
   归一化规则是「**一个 dict = 一个资源，取优先级最高的那个 URL 键**」，
   优先原图；抖音 `url_list` 那种同一资源的多个 CDN 镜像只取第一个可用的。
   协议相对地址（`//pic5.40017.cn/...`）会补成 `https:`，否则前端拼不出可访问链接。
   空列表存 `NULL` 而不是 `"[]"`，前端判空简单。

### 携程点评的字段对应

线上响应里有几个键名和直觉不一样，踩过坑，列在这里：

| 响应字段 | 含义 | 落到哪 |
|---|---|---|
| `images[].imageSrcUrl` | 原图（`imageThumbUrl` 是 180×180 缩略图） | `image_list` |
| `videos[].videoUrl` | 视频 | `video_list` |
| `replyInfo[]` | 回复列表（**不是** `replyList`） | 二级评论 |
| `replyContent` / `replyTime` | 只有一条商家回复时的扁平写法 | 二级评论 |
| `publishTime` | `/Date(1787631227000+0800)/` .NET 格式 | `publish_time` |
| `userInfo.userNick` | 昵称（**不是** `nickName`） | `commenter_name` |
| `userMember` / `jumpH5Url` / `collectCnt` / `fromTypeText` | 携程特有 | `extra_content` |

`backend/tests/fixtures/ctrip_real_page.json` 就是你给的那份线上返回，
`test_ctrip_collector.py` 里有针对它的回归测试——键名再猜错会当场失败。

---

## 七、cron 表达式

支持两种写法：

| 段数 | 格式 | 例子 |
|---|---|---|
| 5 段 | `分 时 日 月 周` | `0 2 * * *` 每天 2 点 |
| 6 段 | `秒 分 时 日 月 周`（Quartz 风格） | `0 0 2 * * *` 每天 2 点整 |

新建任务页有「预览下次执行时间」，可以先确认写对了再保存。

> 注意：croniter 默认把 6 段表达式的秒放在**末尾**，与国内常见的 Quartz 写法相反。
> 系统里已显式指定秒在开头，`30 * * * * *` 会被正确理解成"每分钟第 30 秒"。

---

## 八、各平台签名方式

四个社媒平台拿数据的门槛不一样，系统按各自最省资源的方式处理：

| 平台 | 签名参数 | 怎么算的 | 采集时要不要开浏览器 |
|---|---|---|---|
| 抖音 | `a_bogus` + `msToken` | Node 执行 `backend/libs/douyin.cjs`；msToken 读页面 localStorage 的 `xmst` | **要**（常驻无头页面） |
| 快手 | `__NS_hxfalcon` | 只能调页面里已加载的签名环境 `window.__ks_realm.$encode` | **要**（常驻无头页面） |
| 小红书 | `X-S` / `X-T` / `x-s-common` | 纯 Python 算法（`xhshow` 包） | 不要 |
| 微博 | 无 | 带 Cookie 直接请求 | 不要 |
| 携程 / 同程 | 无 | 只需要一个 cid / 随机 iid | 不要 |

几个实现上的关键点：

- **Node 不用单独装**。抖音签名需要 Node，但 Playwright 自带一个
  （`playwright/driver/node`），`PATH` 里没有 `node` 时会自动用它。
  想指定别的，设环境变量 `SMC_NODE_BIN`。
- **抖音的 UA 被钉死**成浏览器页面的 UA。`a_bogus` 是拿 UA 一起算的，
  换 IP 时如果连 UA 一起换掉，签名立刻对不上——所以代理轮换只换 IP 不换 UA。
- **小红书 GET 请求的查询串手工拼接**，逗号保持不编码。
  签名是对这个串算的，交给 httpx 自动编码会把 `,` 变成 `%2C`，接口直接返回 406。
- **小红书的 xsec_token** 只能从搜索/主页列表里拿到，且与 note_id 一一对应。
  采到作品时就存进 `extra_content`，采评论时再取出来用。
- **快手限流**（`result:2`）会指数退避重试；签名未通过（`result:50`）
  会直接提示"请重新登录该账号"，不做无意义重试。

### ⚠️ 抖音综合搜索接口不能带 a_bogus

`/aweme/v1/web/general/search/single/` 是个例外：**签了反而拿不到数据**。
MediaCrawler 里这一行是显式排除的：

```python
if "/v1/web/general/search" not in uri:
    params["a_bogus"] = a_bogus
```

带上 `a_bogus` 之后抖音**不会报错**，而是返回 `200` + `data: []`——
日志里就是「关键字 [xxx] 第 1 页无数据，结束 / 采集到 0 条作品 / 任务完成」，
三行全是正常语气，完全看不出哪里错了。

评论、作品详情、主页作品这些接口**必须**签名，别把排除范围放大。
`tests/test_social_collectors.py` 里两条测试分别盯着这两侧。

其余对齐 MediaCrawler 的细节：搜索参数固定带
`from_group_id=7378810571505847586`、`count=15`、`list_type=multi`；
Referer 是 `https://www.douyin.com/search/{关键字}?aid=f594bbd9-…&type=general`。

### 交互式登录：弹窗、以人为准、别让登录态白丢

三条都来自同一个反馈——「我扫码登录成功了，再打开还要登录」。

**1. 登录窗口开首页，不开登录页。**
`weibo.com/login.php` 无论登没登录都渲染登录界面。直接开它，
用户每次点「打开浏览器」看到的都是"请登录"，自然以为登录态没保住
（其实 profile 里好好的）。开首页的话，还登录着就直接看到时间线，
状态栏也会写明「这个账号还登录着，不用重新扫码」。

**2. 登录弹窗要跟上。**
微博 PC 端的登录是 `window.open`：`passport.weibo.com/sso/signin?…&disp=popup`。
推流绑死在主页面上的话，界面里显示的是 weibo.com 首页，
**二维码在另一个看不见的窗口里，根本没法扫**。
现在 `context.on("page")` 捕获弹窗，推流和输入一起切过去，
弹窗关闭后切回主页面并刷新一次。

**3. 「我已登录，保存」——以人为准。**
自动判定再准也有失手的时候（平台改接口、网络抖动、弹窗流程和预期不一样），
而"到底登没登上"这件事**人看一眼画面就知道**。
没有这个按钮，判定一失手用户就只能干瞪眼，下次打开还得重扫。
唯一的把关是 Cookie 里得真有登录凭据，否则会告诉你当前都有哪些 key。

配套的兜底：**关窗时即使自动判定说没登录，只要 Cookie 里有登录凭据就照样保存**，
并记一条 warning。丢掉的代价是"下次还得重扫"，收下的代价只是可能存了一份没用的——
后者由采集前的登录态自检兜底，代价小得多。

### 微博：只走 PC 网页端（weibo.com）

**用户两次明确要求「走 web 端就行，不用考虑 m.weibo.cn」**，而这也是他那套
跑通的采集链路的做法（`opinion-hub-all` 的配置里 `base_url: https://weibo.com`、
`search_url: https://s.weibo.com/weibo?q={keyword}`）。

曾经走 m.weibo.cn，两个代价：

1. **登录在 `.weibo.com`、采集在 `.weibo.cn`，是两个不同的注册域、两套独立会话。**
   移动站那套靠 SSO 从 PC 站换过来，有自己的有效期。换不过来就被判成
   「登录态已失效」——用户的原话是「我其实已经登录的呢，老出现重新登录」。
   更隐蔽的是两个域**各有一个叫 `SUB` 的 Cookie，值完全不同**，
   拼请求头时不按域过滤就会发错。
2. **移动端容器接口挑剔得多。** 往 containerid 里多塞一个 `timescope`，
   「宝珠洞索道」这类小众词就退化成一张 `card_type=4` 的空提示卡，一条都搜不到。
   而 `timescope` 是**桌面搜索页的参数**——移动端容器接口不认它。
   （这一条是自己推断出来的"移动端就是把桌面版查询串搬过来"，
   没有任何参考实现这么做，教训见下。）

现在全在一个域上：

| 用途 | 接口 |
|---|---|
| 搜索 | `GET s.weibo.com/weibo`（综合/热门 `xsort=hot`）、`/realtime`（最新）→ **HTML**，抠出 mid |
| 详情 | `GET weibo.com/ajax/statuses/show?id={mid}` |
| 主页 | `GET weibo.com/ajax/statuses/mymblog?uid=&page=&feature=0` |
| 评论 | `GET weibo.com/ajax/statuses/buildComments`，一级 `flow=1&fetch_level=0`、二级 `flow=0&fetch_level=1`，游标 `max_id` |
| 登录态自检 | `GET weibo.com/ajax/profile/info?uid=1`，失效时会被重定向到登录页 |

三个容易踩的地方：

- **搜索结果是 HTML，不是 JSON**，且 mid 有三种写法（`mid="…"`、`/detail/…`、
  `action-data="…mid=…"`），只认第一种会漏掉一部分卡片。
- **正文优先 `longTextContent` → `text_raw` → `text`。** 只读 `text` 的话，
  长微博会被截断成「…全文」。
- **`buildComments` 的 `data` 和 `max_id` 是平级的。** 在公共层"顺手把 data 剥出来"
  会把游标丢掉，表现是评论永远只采第一页——而且不报错，看着一切正常。

### ⚠️ 「第 1 页就没有结果」不该总是当成失败

这个报错本来是为了抓"静默失败"（拿游客 Cookie 空跑，日志里只有一句
「采集到 0 条作品」）。但小众关键字本来就可能真的没有内容，
当成失败的话会白白重试三轮，日志里还留一大段吓人的红字。

分水岭是**登录态有没有被真正验证过**（`BaseCollector.login_verified`）：
验证过 → 静默失败基本排除，记一条警告继续下一个关键字；
没验证过 → 仍然报错，否则用户无从判断问题出在哪。

### ⚠️ 图片显示不出来：防盗链，以及为什么 meta 标签不够

**现象**：数据中心里图片一片"加载失败"，但把地址复制到浏览器**能正常打开**。

这个反差特别容易把人带偏——看着像采到的地址不对。实际上各平台的图片 CDN
（微博 sinaimg、抖音 douyinpic、小红书 xhscdn、快手 kwimgs）都做了防盗链：
请求头里带着别的站点的 `Referer` 就回 403，而地址栏直接打开不带 Referer，所以正常。

**两层措施**（缺一不可）：

1. `index.html` 里 `<meta name="referrer" content="no-referrer">`
2. **服务端代取**：`GET /api/media/image?url=…`（`app/api/media.py`）

只做第 1 层不够：有的 CDN 要的不是"没有 Referer"而是"**自家域名的 Referer**"，
不发反而更糟；而且浏览器扩展、企业策略、上层网关都可能把页面的 referrer policy 覆盖掉。
交给服务端就没这些不确定性——想发什么头就发什么头，浏览器那边只是一个同源请求。

> **这是一个"按 URL 取内容"的接口，天生有 SSRF 风险。** 三条防线必须守住：
> 只允许 http/https；**域名白名单**（绝不能变成通用对外代理，否则内网地址和
> 云厂商元数据接口 `169.254.169.254` 都能被读出来）；响应必须是图片且有大小上限。
> 白名单是后缀匹配但要求点边界，`notsinaimg.cn` / `sinaimg.cn.evil.com` 都进不来。
> `test_media_proxy.py` 里 22 条测试盯着这些。

（视频不能内嵌播放是同类问题，但视频 CDN 还额外校验 token 和 UA，
所以做法是给一个"在新窗口打开原始地址"的链接，不代理。）

### 采集容错：三层，谁都不能拖垮整轮任务

线上真实发生过的三种事故，各对应一层：

| 层 | 出了什么事 | 现在怎么处理 |
|---|---|---|
| **一条数据** | 微博某条作品的 `pics` 元素是字符串不是字典，字段映射抛 `AttributeError` | `BaseCollector.safe_map` 兜住，跳过这一条继续下一条 |
| **一条作品的评论** | 评论区关掉 / 作品被删，平台回普通接口错误 | `_collect_comments_for` 兜住，跳过这条作品的评论 |
| **一个关键字 / 一个目标** | 接口抽风、限流、网络抖动 | `_retrying` 重试 `crawl.max_retries` 次（默认 3，指数退避 2/4/8s），还不行就跳过这个词，继续下一个 |

两个例外**永远不重试、直接往上抛**：任务取消（否则用户点了停止要等三轮才停得下来）、
登录态失效（再采下去每条都会失败，重试只是浪费）。

重试整个关键字看着浪费，其实很便宜——`_Buffer` 会把已经收过的作品判成重复，
既不重复入库、也不重复采它的评论，第二次跑基本只是把翻过的页面快速走一遍。

> ⚠️ 微博用 `ok=0` **同时**表达"出错了"和"这里就是空的"。
> 「还没有人评论哦~快来抢沙发！」被当成错误的话，日志里会刷一大片红色，
> 把真正的问题淹掉。这类 msg 现在按空结果处理。
> 另外所有接口报错都带**完整请求地址（含查询串）**，能直接复制到浏览器重放。

### 跨关键字去重

一个景区通常配好几个关键字（"天山天池""天池风景区""天山天池景区"…），
它们命中**同一条作品**是常态。不去重的代价有两层：

1. 同一条作品被反复 upsert，写库量翻几倍
2. 更贵的是**它的评论会被重新翻一遍** —— 几十上百个请求白打，还多消耗风控额度

去重放在 `_Buffer`（`app/scheduler/runner.py`），粒度和数据库唯一键**完全一致**
（`work_uk` / `comment_uk`，都带 `scenic_id`），所以"重复"在内存里和库里是同一个定义，
不会出现两边判断不一致。同一条微博同时命中两个景区时，两边各存一份——这是对的。

| 情况 | 行为 |
|---|---|
| 本次任务里已收过这条作品 | 不再入库，**也不再翻它的评论**（两次命中相隔几秒，评论区不会变） |
| 本次任务里已收过这条评论 | 不再入库 |
| 库里已有这条评论 | 默认不重写（`crawl.refresh_existing_comments: false`） |

最后一条的取舍：评论正文发出来就不会变，会变的只有点赞数。
每天跑一轮的话，全量 upsert 等于把几万行原样重写一遍，只为刷新老评论的赞数。
需要赞数也跟着刷新就把那个开关打开。

任务日志里会报出跳过了多少：`快手 本轮跳过重复：作品 37 条，评论 210 条`。

### ⚠️ 一条作品的评论失败，不能把整个平台带走

评论区关掉、作品被删、作者设了权限——平台回的都是普通接口错误
（快手是 `result != 1`）。这个错原先会一路冒到 `_run_channel` 的 except，
于是「一条作品评论区关了」== 「这个平台剩下的关键字全不采了」。
现在 `_collect_comments_for` 按**单条作品**兜底，只有两种例外往上抛：
任务取消（否则用户点停止会停不下来）、登录态失效（再采下去每条都会失败）。

### ⚠️ Cookie 还在，但已经失效了

**症状**：微博点「打开浏览器」，7 秒就提示「交互式登录成功，保存 11 个 Cookie」——
根本来不及扫码。然后采集回来 0 条，日志是
`第 1 页就没有结果……card_type：4`。下次打开又要登录，循环往复。

**根因**：登录态判定是**看 Cookie 里有没有某几个 key**。
这套判据有个治不好的毛病：它只知道字段在不在，不知道值还有没有效。
浏览器 profile 会把上次登录的 Cookie 一直留着，过期了 key 也还在——
于是登录窗口一开就判定"已登录"，把这份废 Cookie 存进库，关窗，
采集时微博不报错，只是把搜索结果退化成一张提示卡。

微博还有一层：**它给游客也发 `SUB`**（访客系统）。所以"Cookie 里有 SUB"
这件事本身完全说明不了问题。

**修法**：`LoginSpec` 加 `verify_url` + `verify_truthy_path`，
直接问平台"我登录了吗"。微博用的是 `m.weibo.cn/api/config`，
返回 `{"ok":1,"data":{"login":true,...}}`——取自用户自己那套跑通的脚本
（它的 `check_cookie()` 问的就是这个接口）。

| 层 | 做法 |
|---|---|
| 浏览器侧 | `context.request.get(verify_url)`（和上下文共用 Cookie），登录窗口轮询时按 8 秒节流，免得在用户扫码那几十秒里高频打接口 |
| 采集侧 | `WeiboCollector.prepare()` 里先问一次，`login=false` 直接报"未登录"，不去搜 |
| 兜底 | 接口连不上/字段对不上时退回 key 判定——诊断手段不能变成故障点 |

`test_stale_but_present_cookie_is_not_logged_in` 用真实 Chromium 复现：
profile 里 Cookie 齐全，服务端说 `login=false`，必须判为未登录。
把 `verify_url` 那段关掉，这条测试立刻红（已验证）。

### 微博：几处照用户自己那套跑通的脚本对齐

| 项 | 值 | 说明 |
|---|---|---|
| 排序 → `containerid` 的 type | 综合 `1` / 实时 `61` / 热门 `21` | |
| 卡片解析 | `card_group` 里**只看有没有 `mblog`** | 组内元素的 `card_type` 不一定是 9，按 9 过滤会一条都取不到 |
| `ok == -100` | 未登录 | 报成"登录态失效，请重新登录"，不是业务错误 |

⚠️ **卡片解析那条踩过坑**：接口明明回了 `cardlistInfo` + `cards`，
我们却报"第 1 页就没有结果"。因为组内元素按 `card_type == 9` 过滤，
而线上有一部分带的是别的 card_type。判据应该是"有没有 mblog"——那才是要的东西。

现在这种情况会把卡片类型分布打出来（`接口回了 N 张卡片但没有一条微博正文（card_type：…）`），
一眼能看出是结构变了还是登录态不够。

### 「第 1 页就没结果」一律按失败处理

四个社媒平台都改了：第一页返回空**不再安静结束**，而是抛错，
并把线索一起摆出来（msToken/页面指纹在不在、Cookie 有几个、
接口返回了哪些顶层字段、下一步该试什么）。

理由很简单：第一页就空，基本不可能是"采完了"。当成正常结束的话，
用户只会看到"采集到 0 条"，无从判断是这个词真没内容、被风控了、还是参数不对。

---

## 九、账号与登录

### 静默 Cookie 是怎么做到"用户无感知"的

每个账号一个持久化浏览器目录 `data/browser_profiles/{平台}/{账号}/`，
Playwright 会把 Cookie、localStorage、IndexedDB 和指纹相关状态都留在里面。
用户**只在账号管理里显式登录一次**，之后任务运行时：

```
a. DB 里的 Cookie 还新鲜   →  直接用，完全不启动浏览器（最快）
b. Cookie 过期但 profile 在 →  无头静默打开 profile 取新 Cookie
                                （profile 里有登录态，不会弹登录页）
c. profile 也失效           →  标记账号 expired，前端提示重新登录那一个账号
```

a、b 两条路径对用户完全无感知。新鲜度阈值由 `browser.session_check_interval_seconds` 控制。

### 实时流交互浏览器

服务部署在服务器上，用户看不到服务端弹出的浏览器窗口，所以登录走推流：

```
POST /api/accounts/{channel}/{account}/login-session  →  拿到 session_id
WS   /ws/browser/{session_id}                         →  看到画面并直接操作
```

服务端用 CDP `Page.startScreencast` 推 JPEG 帧，前端回传的点击/输入/滚动
用 `page.mouse` / `page.keyboard` 重放。登录成功后自动保存登录态并可关闭窗口。

### 手动导入 Cookie（推荐用于抖音、快手）

扫码登录不总是走得通：会过期、会被风控拦、服务器上也未必有人扫。
所以留了一条最直接的路——在**你自己的浏览器**里登录好，导出 Cookie 贴进来。

账号管理 → 该账号的「**Cookie**」按钮 → 粘贴 → 导入。两种格式都认：

| 来源 | 样子 | 说明 |
|---|---|---|
| Cookie-Editor 的 **Export**（推荐） | `[{"name":"sessionid","value":"…","domain":".douyin.com",…}]` | 带域名和过期时间，最完整 |
| Export as Header String / devtools 里复制的 Cookie 头 | `sessionid=xxx;sid_tt=yyy` | 分号后有没有空格都行；域名按平台自动补 |

导入时会做三件事：

1. **校验有没有登录凭据** —— 只有游客 Cookie 的话直接拒绝，
   不会让你以为导入成功了，跑任务才发现不行
2. **补过期时间** —— 没有 `expires` 的是会话 Cookie，Chromium 根本不往
   profile 里写，不补的话导入等于白做
3. **写进该账号的浏览器 profile** —— 抖音的 msToken 在页面 localStorage 里、
   快手的签名要调页面里的函数，所以**页面本身**必须是登录态，
   光把 Cookie 串塞进请求头不够

采集时 `PageSession` 还会再把库里的 Cookie 当种子注入一次，
所以即使 profile 被清掉，导入过的登录态也能自动救回来。

### 登录态是怎么判定的

`app/browser/specs.py` 里每个平台列了一组 `session_cookies`，
出现任意一个（值非空）就算已登录。

⚠️ **只能放"登录后才会出现"的 key。** 抖音的 `passport_csrf_token`
对匿名访客也会下发，早期把它当成了登录凭据，结果 profile 里只有 4 个游客
Cookie 也被判成已登录，任务照常跑，直到抖音返回
`status_code=2483 请先登录，再继续搜索吧` 才暴露——而那个报错指向采集器，
不指向账号，很难查。

判断某个 key 能不能放进来：开一个全新的无痕窗口，什么都不登，打开站点首页，
看这个 key 在不在。在，就不能放。

现在采集器发第一个请求前会先本地校验一遍（`missing_login_cookies`），
缺凭据就直接报"这个账号没有真正登录，请重新登录或导入 Cookie"，不去撞平台接口。

### 定时采集 Cookie
`browser.auto_refresh_cookie_minutes` 设成大于 0，调度器就会按这个间隔
把每个启用账号的 Cookie 重抓一遍，失效的自动标成 `expired`，
账号管理页直接能看到。默认 0（关闭），只在采集时按需刷新。

### 账号分组与多账号轮换
一个平台可以挂多个账号，每个账号属于一个**分组**（`account_group`，默认 `default`）。
轮换规则：
- **不指定分组**（任务上留空）：该平台所有「启用 + 登录态正常」的账号按
  「最久没校验过的先用」轮换，请求量自然分摊，不会把一个账号用到被封
- **指定分组**（新建任务时的「账号分组」下拉）：只用该组里的账号，
  不同业务线各用各的账号，互不挤占
- 也可以在任务 params 里写 `channel_params.{平台}.account_group` 做平台级覆盖

分组在「账号管理」页维护：添加账号时选已有分组或直接输入新组名；
页面顶部的分组卡片显示每组的账号数和可用数，点卡片即按组筛选。

> ⚠️ 老库升级：`account_group` 列由启动时的 `_ensure_extra_columns`
> 自动补上（`ALTER TABLE ... ADD COLUMN`），不需要手工迁移。

### 全平台体检
账号管理页右上角的「**全平台体检**」：拿真实采集链路把六个平台挨个试搜一遍
（每个平台只取一条、不写库），每个平台出一份诊断报告——
Cookie 有没有、代理走没走、HTTP 状态码、响应摘要、样例数据。
部署完、换账号后、任务突然没数据时，先跑一遍这个，比翻任务日志快得多。
接口：`POST /api/accounts/probe-all?keyword=…&ctrip_target=…&tongcheng_target=…`。

### 任务看门狗
平台风控把连接挂住（不回包也不断开）时，请求超时、重试、取消信号都可能失效——
任务看起来在跑，实际一条数据也出不来，还占着并发槽位。
`crawl.task_timeout_minutes`（默认 360 分钟，0 = 关闭）是看门狗：
运行超过这个时长的任务先被置取消信号让它自己收尾，
再过 5 分钟还没退出就强制 cancel，把槽位让出来。

### ⚠️ 跨域登录：登录站点和采集站点不是一个域

微博是典型：**登录在 `passport.weibo.com`，采集走 `m.weibo.cn`** ——
`weibo.com` 和 `weibo.cn` 是两个不同的注册域，浏览器不会把 Cookie 从一边带到另一边。

而且两边都有一个叫 `SUB` 的 Cookie。所以早期版本判定登录态时把 context 里
所有 Cookie 一起看，passport 那份 `SUB` 让判定通过了，提示"登录态已保存"，
可采集时 `m.weibo.cn` 根本收不到它——用户被反复要求重新登录，
直到手动导入 Cookie 才好。

现在 `LoginSpec` 多了两个字段：

- `cookie_urls` —— 判定登录态和保存 Cookie **只看这些 URL 收得到的那部分**
- `post_login_url` —— 登录成功后先跳一次采集域，让站点把 Cookie 种过去

`tests/test_browser_session.py::test_login_state_is_judged_on_the_collect_domain`
用两个真实主机名（127.0.0.1 / localhost）复现这件事，去掉域限定就会红。

接新平台时如果登录页和采集接口不同域，这两个字段必须填。

### 有头还是无头

两个独立开关，都在系统设置页：

| 配置 | 管什么 | 默认 |
|---|---|---|
| `browser.headless` | 采集、「采集 Cookie」 | `true`（无头） |
| `browser.headless_login` | 交互式登录窗口 | `false`（有头） |

登录默认有头，是因为无头模式下大部分平台的风控会直接拦掉，二维码也常常渲染不出来。
但服务跑在自己电脑上时，那个窗口会实实在在弹在桌面上打扰人，而画面本来就通过推流
看得到——所以给了 `headless_login` 让你关掉。

拿不准当前生效的是什么，看 `GET /api/health` 的 `browser_headless` 字段，
里面直接写着 collect 和 login 各自的实际值。

⚠️ 同一个 profile 不能被两个 Chromium 同时打开。登录窗口开着的时候跑采集任务，
会拿到一个**全新的空 profile**——表现就是"刚扫完码，任务却说没登录"。
现在登录窗口打开期间会登记占用，采集任务撞上会直接提示"请先关闭登录窗口"。

---

## 十、目录结构

```
scenic-media-collector/
├── config/
│   ├── config.example.yaml     配置模板（进版本库）
│   └── config.yaml             实际配置（不进版本库）
├── backend/
│   ├── app/
│   │   ├── core/               配置、日志、MySQL、Redis、常量、子进程循环、采集数量解析
│   │   ├── db/schema.sql       建表脚本
│   │   ├── proxy/              代理池、设备指纹、带轮换的 HTTP 客户端
│   │   ├── browser/            账号 profile、静默 Cookie、实时流登录
│   │   ├── collectors/         采集器基类 + 各平台实现
│   │   ├── repositories/       数据访问
│   │   ├── scheduler/          任务执行引擎、调度器、任务日志
│   │   ├── api/                REST + WebSocket
│   │   ├── utils/              字段归一化、CSV 导出
│   │   └── main.py             服务入口
│   └── tests/
│       └── fixtures/           携程/同程的线上真实响应，用于回归测试
├── frontend/                   Web 前端（Vue3 + Element Plus）
│   ├── src/api/                接口客户端（axios + WebSocket）
│   ├── src/views/              7 个页面
│   ├── src/components/         评论树、图片视频画廊、采集数量表单、任务编辑弹窗、实时流登录浏览器
│   └── dist/                   构建产物，被后端直接托管
├── docker/                     Dockerfile / compose / nginx
├── scripts/                    start.sh、start.bat、selfcheck.py、probe_tongcheng.py
└── docs/
```

---

## 十一、开发

测试会连真实 MySQL（用独立的 `scenic_media_e2e` 库，跑完不影响开发库）。
连接信息从 `config/config.yaml` 读；也可以用环境变量临时指定：

```bash
export SMC_MYSQL_USER=你的用户 SMC_MYSQL_PASSWORD=你的密码

cd backend
python -m pytest tests/ -q                    # 全部测试
python -m pytest tests/test_ctrip_collector.py -v   # 单个模块
python -m uvicorn app.main:app --reload       # 开发模式热重载（Windows 下也可用，见排障一节）
```

前端开发：

```bash
cd frontend
npm install
npm run dev        # 开发服务器 5173，接口自动转发到后端 8000
npm run build      # 构建到 dist/，后端会自动托管
npm run typecheck  # 类型检查
```

新增一个平台采集器只需三步：

1. 在 `app/collectors/` 里继承 `BaseCollector`，实现
   `collect_by_keyword` / `collect_by_creator` / `collect_comments`
2. 用 `@CollectorRegistry.register` 注册
3. 需要登录的话，在 `app/browser/specs.py` 加一条登录规则

代理轮换、账号登录态、入库、导出、调度全部由框架处理，采集器不用关心。

---

## 十二、排障

### `NotImplementedError`（登录窗口打不开 / 抖音采集一开始就失败）

```
File "asyncio\base_events.py", line 503, in _make_subprocess_transport
    raise NotImplementedError
```

**原因**：系统里有两处要 fork 子进程，而 Windows 上只有 `ProactorEventLoop`
支持创建子进程：

| 位置 | 用途 | 症状 |
|---|---|---|
| Playwright | 启动浏览器前拉起 node 驱动进程 | 账号登录窗口打不开 |
| `app/utils/jsvm.py` | 常驻 Node 执行 `douyin.cjs` 算 `a_bogus` 签名 | 任务跑到「搜索关键字」立刻失败，栈里有 `jsvm.py` |

uvicorn 有这么一段：

```python
# uvicorn/config.py
use_subprocess = bool(self.reload or self.workers > 1)
# uvicorn/loops/asyncio.py
if sys.platform == "win32" and use_subprocess:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

也就是说，Windows 上只要带 `--reload` 或 `--workers > 1` 启动，
服务器主循环就变成了开不了子进程的 SelectorEventLoop。

**现在已经修好**：上面两处**都**改为提交到 `app/core/subprocess_loop.py`
维护的专用事件循环（`subprocess-loop` 线程，Windows 下固定用 ProactorEventLoop），
和服务器怎么启动无关。带 `--reload` 也能正常登录和采集。

升级后重启服务即可；启动日志里会看到：

```
子进程事件循环已就绪（ProactorEventLoop，线程 subprocess-loop）
```

> 新增任何 `asyncio.create_subprocess_*` 或 Playwright 调用时，都必须走这个循环。
> `tests/test_subprocess_loop.py` 里有一条结构性测试会把漏网的模块直接点名——
> 第一次修复就是只处理了 Playwright、漏了 jsvm，才导致同一个问题换个地方复发。

### 建表时表已存在但结构不对

`CREATE TABLE IF NOT EXISTS` 遇到同名表会**静默跳过**。
如果 `mysql.database` 指向的库里已经有同名表，建表会被跳过，之后写入报 unknown column。
确认一下：

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = DATABASE() AND table_name LIKE 'src\_opinion\_%';
```

应该正好 10 行。有冲突就换一个独立的库。
（`src_opinion_` 这个前缀本身就是为了降低撞名概率——上一版的 `task` / `scenic`
在共用库里几乎必然撞车。）

### 先用「试搜」定位问题

账号管理每行有个 **试搜** 按钮。它用**真实的采集链路**（同一套登录态、代理、签名）
跑一个关键字，只取一条就停，不写库，然后把每一步摊开给你看：

- 卡在哪一步（准备登录态 / 发请求 / 解析）
- 走的哪个出口 IP、什么设备指纹
- HTTP 状态码、响应体大小、**原始响应前 600 字符**
- 采集器自己打的过程日志

绝大多数"没数据"的问题看一眼这个报告就清楚了。报告右下角可以一键复制成 JSON。

### 抖音 `status_code=2483 请先登录，再继续搜索吧`

日志里往前翻一行，看 `采集页面已就绪，N 个 Cookie`：

- **N 很小（个位数）** —— profile 里只有游客 Cookie，账号其实没登录成功。
  去账号管理点该账号的「Cookie」看一眼，弹窗会直接告诉你缺哪些 key。
  最快的解法是导入 Cookie（见「九、账号与登录」）。
- **登录窗口还开着** —— 同一个 profile 不能被两个 Chromium 打开，
  采集会拿到一个空 profile。关掉登录窗口再跑。

现在这两种情况都会在发请求前被拦下来，报错直接说"这个账号没有真正登录"，
不会再让你看到平台自己的 2483。

### 采集报「没有可用账号」

抖音、快手、小红书、微博需要登录。到「账号管理」加一个账号并完成登录；
携程、同程免登录，不需要账号。

### 小红书返回 300012 / 300011

- `300012` 出口 IP 被判异常 → 开代理或换 IP
- `300011` 触发风控限流 → 调大「系统设置 → 采集参数 → 请求间隔」，或多配几个账号轮换
