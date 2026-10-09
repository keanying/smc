# opinion_labeling_engine —— 景区舆情标注引擎

对采集侧推过来的景区评论做**结构化标注**，把五个字段写回 `src_opinion_social_work_comment_di`：

```
sentiment_label  sentiment_score  dimension_tags  entity_tags  keyword_tags
```

输出格式与线上样例数据严格一致，可直接被下游 `opinion_metric_engine` 消费。

```
采集侧 engine.submit(row)
        │
        ▼
   队列（Redis / 内存，不依赖业务表）
        │
        ▼  ① 清洗：纯表情 / 纯符号 / 灌水 / 广告占位 —— 不调模型，直接中性
        ▼  ② 大模型：提示词带完整标签体系，输出 JSON
        ▼  ③ 解析：四级容错抠 JSON，坏了走修复轮
        ▼  ④ 后处理：白名单校验 + 关键词方向过滤 + 幻觉过滤
        │
        ▼
   攒批 UPDATE MySQL（幂等，按主键）
```

---

## 快速开始

```bash
pip install -r requirements.txt

# 1) 配置：库连接与 API Key 走环境变量，不落文件
export ARK_API_KEY=ark-xxxxxxxx
export MYSQL_HOST=10.0.0.1 MYSQL_USER=xxx MYSQL_PASSWORD=xxx MYSQL_DATABASE=opinion
export QUEUE_BACKEND=redis REDIS_HOST=10.0.0.2

# 2) 启动前自检：配置 / 标签体系 / Redis / MySQL / 表结构，一项不过就退非零码
python -m opinion_labeling_engine check

# 3) 先看提示词长什么样（人工审阅，不花 token）
python -m opinion_labeling_engine prompt --text "风景很美但厕所太差"

# 4) 拿几条真实数据干跑一遍，只打日志不写库
python -m opinion_labeling_engine label --csv sample.csv --limit 20 --out result.csv

# 5a) 直接把表里已有的评论标一遍（不接采集侧）
python scripts/label_from_table.py

# 5b) 或者常驻消费，等采集侧推数据
python -m opinion_labeling_engine serve
```

---

## 调用契约

### 你传什么

```python
from opinion_labeling_engine import LabelingEngine

engine = LabelingEngine.from_config()
engine.start()

for row in crawler.stream():        # 一边采集一边推
    engine.submit({
        "channel":     "ctrip",              # 渠道       ★ 写回主键
        "work_id":     "76471",              # 作品ID
        "scenic_id":   "PFTSCA01009835",     # 景区ID     ★ 写回主键
        "scenic_name": "天山天池",            # 景区名称
        "comment_id":  "805353431",          # 评论ID     ★ 写回主键
        "content":     "完美的一天",          # 评论内容
    })                                        # 立即返回，不阻塞采集

engine.stop()                       # 或者 with engine: 自动停机
```

`submit_many(rows)` 批量提交；`label_sync(row)` 不走队列直接返回结果（联调用）。

| 字段 | 必填 | 缺失时 | 用途 |
|---|---|---|---|
| `channel` | **是** | **拒绝入队**并抛 `ValueError` | 写回主键 |
| `scenic_id` | **是** | **拒绝入队** | 写回主键 |
| `comment_id` | **是** | **拒绝入队** | 写回主键 |
| `scenic_name` | 建议 | 告警，继续 | 模型判「是否与景区相关」的依据。缺了「天池的水真清」会被误判成无关 |
| `work_id` | 建议 | 告警，继续 | 随记录流转，便于下钻回溯；**不进提示词**（对标注无价值，白烧 token） |
| `content` | 建议 | 告警，继续 | 标注对象。为空会被清洗判为无效内容 → 中性 |

主键缺失**在入队时就拒绝**，不是等写库时才发现。这类问题放过去只会表现为
「UPDATE 影响 0 行」的一条 WARNING，排查要翻半天日志；在入口拒绝直接告诉你缺哪个字段。
`submit_many` 遇到非法行只跳过该条并记日志，不中断整批。

其余字段（`likes` / `extra_content` / `publish_time` / `commenter_name` …）全部可选，
传了会作为辅助上下文提高准确率（携程的 `extra_content` 里有评分和游客类型，
对「完美的一天」这种短评帮助很大），不传也不影响。多传未定义的字段直接忽略，不报错。

契约写在 `config.yaml` 的 `storage.required_input_fields` 里，改这一行即可调整。

### 引擎写什么

标注完按 **`channel` + `scenic_id` + `comment_id`** 定位行，UPDATE 五个字段：

```sql
UPDATE src_opinion_social_work_comment_di
SET sentiment_label = %s, sentiment_score = %s,
    dimension_tags  = %s, entity_tags     = %s, keyword_tags = %s
WHERE channel = %s AND scenic_id = %s AND comment_id = %s
```

只 UPDATE 不 INSERT（行是采集侧写进去的），幂等，攒批执行。

---

## 直接从表里跑：`scripts/label_from_table.py`

不接采集侧、直接把表里已有的评论标一遍，用这个脚本。它就是上面那段接入代码的成品版：
`SELECT 六个字段 → engine.submit() → 写回`。

```bash
# 最常用：把没标注的全标了，跑到没有为止
python scripts/label_from_table.py

# 先干跑 30 条看看标得对不对，结果落 CSV 人工抽验
python scripts/label_from_table.py --limit 30 --dry-run --out check.csv

# 按景区 / 渠道 / 时间范围挑
python scripts/label_from_table.py --scenic-id PFTSCA01009835 --channel ctrip xhs
python scripts/label_from_table.py --since 2026-08-01 --until 2026-09-01

# 全量重标（含已标注的行），要指定一个唯一可排序的游标列
python scripts/label_from_table.py --mode all --order-by comment_id
```

### 两种取数模式

**`pending`（默认）** —— 每轮 `SELECT ... WHERE sentiment_label IS NULL OR '' LIMIT 500`，
这批写回后就不再匹配条件，下一轮自然取到新的一批，像抽水一样把表抽干。
不需要任何排序列，不怕中途新增数据，断点续跑天然支持（重跑一次接着抽）。
代价是每批之间要等写回完成才能取下一批。

**`all`** —— 重标所有行。过滤条件不随处理收缩，所以用游标翻页
（`WHERE comment_id > 上批最大值 ORDER BY comment_id LIMIT n`）。
`--order-by` 指定的列**必须唯一且可排序**，否则会漏行或重复。
中断了用 `--resume-from <脚本最后打印的游标值>` 接着跑。

### 几个刻意的选择

- **不用 `SSCursor` 流式游标读全表。** worker 正在往同一张表写回，
  开一个长事务游标去读一张正在被大量 UPDATE 的表，既拉长 undo 链拖慢写入，
  也可能读到自己刚写的行。分批短读在这里更稳。
- **`content` 为空的行默认也捞出来**，让它走一遍清洗被判无效、写成中性。
  在 SQL 层滤掉看着省事，但那些行的 `sentiment_label` 会永远是 NULL，
  下游取数又要单独处理一次 NULL，等于把问题往后推。要跳过用 `--skip-empty-content`。
- **整批入队失败会停下来。** 连续 3 批一条都入不了队（通常是主键有空值），
  脚本会报错退出而不是空转——`pending` 模式下这些行永远匹配过滤条件，
  不设这个闸就是死循环。
- **`--out` 只在 `--dry-run` 下可用**，且会自动切到同步标注：
  异步路径的结果在 worker 里就直接写库了，拿不到逐条结果来出 CSV。

Ctrl-C 会停止取数、等在途任务处理完再退出；再按一次强制退出。

---

## 标注失败：不写库，进 Redis 失败池

**只有 AI 真标出结果的才写库。** 模型调用失败、输出解析失败的，五个标注字段一个都不动。

以前是写个"中性"兜底，那是错的：假中性会把行占掉，以后既认不出它是失败的，
下游指标还会把它当成一条真实的中性评价，把景区得分往 5 分拉。

```
标注失败
   ├─ 队列内重试 max_attempts(3) 次        ← 瞬时抖动在这一层就消化了
   └─ 还是不行
        ├─ Redis 失败池：存整条评论 + 累计失败次数 + 最后失败原因
        └─ label_review_flag = 5           ← 只置标记，不碰标注字段
```

Redis 管重标，`label_review_flag` 管盘点，两边互为补充。

```bash
python -m opinion_labeling_engine failures     # 看失败池里有什么
python -m opinion_labeling_engine retry        # 重标它们
python scripts/label_from_table.py --mode failed   # 或者从表里按 flag=5 捞
```

失败池**固定走 Redis**，与 `queue.backend` 无关——队列可以是 memory，
但失败记录必须跨进程重启存活，否则"后续再标注"无从谈起。
Redis 连不上时引擎**拒绝启动**：静默降级会让失败记录悄悄丢光，那比启动失败糟得多。

几个刻意的选择：

- **重标取出即从池中移除。** 又失败会被重新写回（`attempts` 继续累加），
  所以并发跑两个重标进程也不会把同一条领两次。
- **`attempts` 跨轮次累加**，靠 `retry_count` 随记录在队列里流转。
  否则每轮重标都从 1 开始数，永远看不出哪条是怎么标都标不出来的钉子户。
- **一轮重标只处理"本轮开始前就在池子里"的记录。** 不设这条线，
  这轮又失败的记录立刻满足取数条件、同一轮里被反复捞出来——死循环。
- **刚失败的先晾 `retry_min_age_seconds`（60秒）再重试。** 模型正在抽风时
  立刻重试只是白烧 token。

要退回老行为（失败也写中性），把 `labeling.write_on_model_failure` 打开。

---

## `label_review_flag`：标注状态

引擎只写 4/5/6，1/2/3 留给人工：

| 值 | 含义 | 谁写 |
|---|---|---|
| 0 | 未标注 | 建表默认 |
| 1 / 2 / 3 | 人工复核正确 / 错误 / 复核成功 | **人工** |
| 4 | AI标注成功 | 引擎：标出结果且置信度达标 |
| 5 | AI标注错误 | 引擎：模型失败，**标注字段一个都不动** |
| 6 | 未人工复核 | 引擎：标出来了但置信度低于阈值 |

纯表情判中性、景区无关判中性都算 **4**——它们是确定的结论，不是"没标出来"。

取值可在 `storage.review_flags` 里改，不用动代码。

### 跑批按状态筛

```bash
python scripts/label_from_table.py                   # 默认：flag=0 且未标注的。已标注的绝不再动
python scripts/label_from_table.py --mode failed     # flag=5，重标 AI 失败的
python scripts/label_from_table.py --mode neutral    # 重标中性的，清理历史兜底假数据
python scripts/label_from_table.py --mode all        # 全量重标
```

`--mode neutral` 有个必须知道的前提：**历史上兜底写进去的假中性，和真正的
纯表情/景区无关中性，在数据里长得一模一样，分不出来。** 好在整批重标它们几乎不花钱
——纯表情的连模型都不调，结论也和上次一致。人工复核过的（flag 1/2/3）默认不动，
那是人的结论，不该被 AI 覆盖；要一起重标加 `--include-reviewed`。

---

## Redis 版本

失败池与队列只用 `LPUSH / BRPOPLPUSH / HSET / ZADD`，**没有任何 RESP3 特性**，
所以客户端一律按 RESP2 建连，并关掉 `CLIENT SETINFO` 客户端信息上报。

这么做是因为新版 redis-py（>=5，尤其 6/8）默认要发 RESP3 握手命令 `HELLO`（Redis 6.0 才有），
连上后还会发 `CLIENT SETINFO`（Redis 7.2 才有）。Windows 上常见的
Redis 3.0.504 移植版会直接回 `unknown command 'HELLO'`。

**推荐还是用 Redis 6+**（Windows 上 Memurai 或 WSL 都行）；上面这层兼容是给
一时换不掉老服务端的情况兜底的，对新版没有任何副作用。

参数按已安装 redis-py 的签名过滤，4.x 到 8.x 都能跑
（硬写 `protocol=2` 在 redis-py 4.x 上会直接 TypeError）。
连不上时的报错会指明是版本问题、鉴权问题还是端口不通，不再只甩一句原始异常。

---

## 怎么"继续标注"

**就是同一条命令，重跑即续跑：**

```bash
python scripts/label_from_table.py
```

默认 `pending` 模式取的是「还没标过的行」。标完的行不再匹配取数条件，
所以中断了、机器重启了、跑了一半停了，重跑这条命令自然接着往下标，
不会重标已完成的部分，也不需要记住上次跑到哪。

另外两条命令解决不同的问题：

```bash
python -m opinion_labeling_engine retry      # 重标失败池里的（模型失败的那些）
python -m opinion_labeling_engine failures   # 只看不改：失败池里有什么
python -m opinion_labeling_engine serve      # 常驻，等采集侧实时推数据
```

`python -m opinion_labeling_engine` 后面必须跟子命令，光敲包名会打印用法。

---

## 已标注数量卡住不动 / 怀疑在重复标注

先跑体检，它把这件事拆成能核对的数字：

```bash
python scripts/label_from_table.py --diagnose
```

输出总行数、不同主键数、各 `label_review_flag` 的分布、本次配置下的待标注数，
最后拿一条真实待标注行**验证它能不能被自己的写回 SQL 命中**——
「标了写不进去」这个根因会在这一步直接暴露。

### 卡住的机制

抽干式取数（每轮 `SELECT 待处理的前 N 条`）能收敛，靠的是**处理完这批就不再匹配条件**。
一旦某些行标完却写不回去（主键对不上、行被删了），它们仍然匹配条件，
而 `LIMIT n` 没有 `ORDER BY`，每次都从扫描顺序的开头拿——**同一批行被无限重标**。
表现就是：跑了好几批，已标注数量却卡在某个值不动，钱一直在烧。

现在这条路被堵死了：

- 取数按写回主键做**跨批去重**，标过的行不会被重新送进模型
- 整批都是这种行时**直接停批**并打印排查顺序，不再空转
- 写回日志带上 `comment_id`，另有**重复写回检测**：同一主键在本进程被写第二次就告警，
  最终数量进跑批报告。「是不是在重复标注」现在有证据，不用靠感觉

### 另外两个会让数字对不上的正常原因

**同键重复行。** 写回按 `(channel, scenic_id, comment_id)` UPDATE，
**一次就更新一整组**，所以「提交数」天然大于「新增标注行数」。取数默认批内去重
（`--no-dedupe` 关闭），体检里的「总行数 vs 不同主键数」就是看这个。

**`命中 0` 曾经是误报。** pymysql 默认返回「实际**改变**的行数」而不是「**匹配**到的行数」，
把同样的值再写一遍就返回 0。已改成 `client_flag=CLIENT.FOUND_ROWS`。
升级后再报未命中才是真没匹配上，日志会自动回查一次说清是哪一列对不上。

---

## 哪些行算「待标注」

`storage.pending_flags` 决定，默认 `[0]`——只标从没标过的，**已标注的绝不重复花钱**。

```bash
python scripts/label_from_table.py --pending-flags 0 4   # 连 AI 标过的一起重标
```

把 `4` 放进来有个后果必须知道：**引擎标成功后写的就是 4**，
所以这些行标完仍然匹配取数条件，抽干式取数会无限重标同一批。
脚本检测到这种配置会**自动切换成游标翻页**（`ORDER BY comment_id`），
靠游标只往前走保证每行只处理一次；中断了用 `--resume-from` 接着跑。

标记为 `0` 的行还额外要求标注字段是空的——标记列是后加的，
历史上标过、那时还没这一列的行标记也是 0，不能当成没标过再标一遍。

## 什么会被判成中性

需求要求「全是表情包、特殊字符、与景区无关的默认中性」，实现上分两条路径：

| 输入 | 判定路径 | 调不调模型 | `source` |
|---|---|---|---|
| `😀😀😀` `[比心][doge]` | 清洗层 | **不调** | `invalid` |
| `。。。。。` `！！！???` | 清洗层 | **不调** | `invalid` |
| `666666` `哈哈哈哈哈` | 清洗层（单字符灌水） | **不调** | `invalid` |
| `123456` | 清洗层（纯数字） | **不调** | `invalid` |
| 纯链接 | 清洗层 | **不调** | `invalid` |
| `此用户没有填写评价` | 清洗层（平台占位文案） | **不调** | `invalid` |
| 带货广告、招聘、纯打招呼、与景区无关的闲聊 | 模型判 `relevant=false` → 代码强制中性 | 调 | `irrelevant` |
| 模型连续失败 | 兜底 | 调过但失败 | `fallback` |

前六种在**进模型之前**就拦掉了，一次 token 都不花；只有「语义上与景区无关」这一类
必须让模型看一眼才能判断。四类的结果一致：`中性 / 0 / dimension_tags=[] / keyword_tags=[]`。

`source` 字段区分是哪条路径来的，抽样复核时能直接筛。

---

## 需求里的四条硬约束，落在哪

| 要求 | 实现位置 | 做法 |
|---|---|---|
| **1. 关键词只能与整体情感同方向** | `labeling/postprocess.py::_filter_keywords` | 模型输出的每个关键词带 `polarity`，与整体 `sentiment` 不一致的直接丢弃。正向评论的 `keyword_tags` 里出现不了任何负向词，反之亦然 |
| **2. dimension_tags 必须合理** | `labeling/postprocess.py::_filter_dimensions` + `domain/taxonomy.py::resolve_dimension` | 三级路径必须命中 `taxonomy.yaml` 白名单；dim3 漏写/近义写错会尝试对齐，对不上就丢弃而不是硬猜；去重 + 限量 |
| **3. 与景区无关 → 统一中性** | `labeling/postprocess.py::_apply_relevance` | 提示词要求模型输出 `relevant`，为 false 时代码强制 `中性/0`，并清空维度与关键词。这是最后一步，覆盖前面所有结论 |
| **4. 关键词不得是单字或幻觉** | `labeling/postprocess.py::_keyword_reject_reason` | 长度 ≥2、≤12；不含标点（挡住整句）；不在通用词黑名单；**必须能在原文中找到依据**（原文包含 / 按序子序列 / 字符全覆盖三级匹配，都不满足即判幻觉丢弃） |

所有被丢弃的内容都记进 `LabelResult.dropped`，抽样复核时能一眼看出模型在哪类上系统性犯错。

---

## 情感取值口径

**用 `正向 / 中性 / 负向`，不是需求文档里的「正面 / 负面」** —— 以你给的线上样例数据为准：

```csv
sentiment_score,sentiment_label,dimension_tags,entity_tags,keyword_tags
-1,负向,"[{""dim1"":""游玩体验"",...,""sentiment"":-1}]","[{""type"":""景区地名"",""value"":""瑶琳仙境""}]","[""太差""]"
```

模型若吐出「正面/积极/positive/1」，由 `taxonomy.yaml` 的 `sentiment.aliases` 统一归一。
要改字面值只改 `taxonomy.yaml`，代码不用动。

> 样例里的 `["太差"]` 是单字段的两字词，合规；但如果历史数据里存在单字关键词，
> 本引擎新标的数据不会再产生，口径上会和历史数据有细微差异 —— 这是需求要求 4 的直接结果。

---

## 分层与文件职责

```
opinion_labeling_engine/
├── config/
│   ├── config.yaml          运行配置（环境变量插值，敏感信息不落文件）
│   └── taxonomy.yaml     ★ 标签体系唯一真源：6 大维度树 + 实体表 + 关键词黑名单
├── opinion_labeling_engine/
│   ├── config.py            配置加载 + 启动期校验（配置错就不让启动）
│   ├── domain/              数据模型 / 情感枚举 / 标签体系          —— 无 IO
│   │   ├── models.py        CommentRecord（入）· LabelResult（出）
│   │   └── taxonomy.py      白名单校验 + 提示词素材，两者共用一份数据
│   ├── preprocess/
│   │   └── cleaner.py       内容清洗与有效性判定                    —— 无 IO
│   ├── llm/
│   │   ├── prompt.py     ★ 提示词工程（准确率主战场）
│   │   ├── client.py        Ark responses 客户端：限流 / 重试 / 退避
│   │   └── parser.py        输出提取：四级容错抠 JSON
│   ├── labeling/
│   │   ├── service.py       单条标注流程编排
│   │   └── postprocess.py ★ 四条硬约束的落地处
│   ├── queues/              base / memory / redis_queue（语义一致，配置切换）
│   ├── storage/             mysql 连接池 + 写回仓储
│   ├── worker/
│   │   ├── engine.py     ★ 对外门面 LabelingEngine
│   │   └── consumer.py      消费线程池：标注 → 攒批 → 先写库后 ack
│   └── cli.py               check / serve / label / backfill / prompt
├── sql/optional_review_column.sql   可选复核列 + 写回索引 + 进度查询
├── scripts/
│   ├── label_from_table.py      ★ 从源表跑批：SELECT 六字段 → submit → 写回
│   └── demo_submit.py           采集侧接入示例
└── tests/                           92 项单测，不连任何外部依赖
```

依赖方向单向向下：`worker → labeling → {llm, preprocess, domain}`，
`storage` / `queues` 只被 `worker` 使用。换模型供应商只动 `llm/client.py`。

---

## 队列：不依赖业务表

按你的要求，源表只在最后一步被 UPDATE，不承担任务状态职责。

| 后端 | 适用 | 说明 |
|---|---|---|
| `redis` | 生产 | LPUSH / BRPOPLPUSH 可靠投递；多进程多机连同一个 Redis 即可水平扩展 |
| `memory` | 联调、单测、小量单机 | 语义完全一致，切换只改一行配置；进程重启会丢队列内任务 |

**投递语义 at-least-once**：

- 消费者拿到租约后必须显式 ack；`BRPOPLPUSH` 原子搬到 processing 队列，进程被 `kill -9` 任务也还在
- 超过 `visibility_timeout`（默认 300s）未 ack 的任务由 reclaim 线程捞回重投
- 重试 `max_attempts`（默认 3）次仍失败进死信队列，`drain_dead()` 可取出补跑
- **先写库后 ack**：顺序反过来会丢数据。重复消费只是多花一次模型调用，写回是幂等 UPDATE，无害

---

## 失败时会发生什么

这套东西跑在生产上，失败路径比正常路径更值得先看清楚：

| 情况 | 行为 |
|---|---|
| 内容是纯表情 / 纯符号 / 灌水 / 平台默认好评文案 | **不调模型**，直接中性落库，`source=invalid`。省 token 也避免被瞎标成正向 |
| 模型超时 / 429 / 5xx | 指数退避 + 抖动重试（听 `Retry-After`），默认 3 次 |
| 模型 401 / 参数错 | **不重试**，立刻失败——重试鉴权错误没有意义 |
| 模型输出不是 JSON | 把错误回喂给模型走一次「修复轮」，只要求它重出 JSON。比整轮重试省 token 且成功率更高 |
| 修复轮还是失败 | 中性兜底落库，`source=fallback`，计入 `failed` 统计。**不让一条脏数据卡死队列** |
| 模型自创维度 / 编造关键词 | 后处理丢弃，记进 `dropped`，不进库 |
| 写库失败 | 整批 nack 重投；重试耗尽进死信 |
| worker 进程崩溃 | 缓冲里未 ack 的任务由可见性超时回收重投，不丢 |
| 置信度 < 0.6 | 正常落库，但打 INFO 日志并计入 `low_confidence`，供运营挑复核池 |

`LabelingService.label()` **不抛异常**——任何失败都收敛成确定的终态。

---

## 请求体形状（方舟 responses 协议的坑）

方舟对 `input` 里的 item 有额外要求，形状不对直接 400、跑批当场停摆：

```
MissingParameter: The request failed because it is missing `input.status` parameter
```

原因是把 assistant 轮（少样本示例、修复轮）回传给模型时，item 上必须带 `type` 和 `status`。
现在默认配置绕开了这个问题：

| 配置 | 默认 | 作用 |
|---|---|---|
| `llm.fewshot_style` | `inline` | 示例折进 system 段，`input` 里**不出现任何 assistant item** |
| `llm.system_role` | `system` | system 段作为独立 item；改成 `user` 则整个提示词压成**一个** user item |

默认发出去的请求体是这个形状，与方舟文档最小样例逐字段一致：

```json
{"model": "...", "stream": false, "tools": [],
 "input": [{"role": "system", "content": [{"type": "input_text", "text": "..."}]},
           {"role": "user",   "content": [{"type": "input_text", "text": "..."}]}]}
```

换了网关又报 `input` 相关的参数错，按这个顺序退：
`system_role: user`（只剩一个 user item，退无可退）→ 还不行就是模型名或 Key 的问题。

`fewshot_style: message` 保留着（示例走独立 user/assistant 轮，模型学得略准一点），
assistant item 已按协议补上 `type` + `status`，但它不是默认值。
`tests/test_request_shape.py` 把这些形状全钉死了，改提示词不会再把请求格式改坏。

---

## 输出预算（推理模型的坑）

`deepseek-v4-flash` 会先输出思考过程，**思考也吃 `max_output_tokens` 这份额度**。
预算给小了会出现两种表现，日志里长这样：

```
incomplete_details: {'reason': 'length'}   →  正文一个字都没出来
Expecting value: line 49 column 7          →  JSON 出到一半被切断
```

三处应对：

| 措施 | 位置 |
|---|---|
| 默认预算 **4096**（原来 1024 不够推理模型用） | `llm.max_output_tokens` |
| 检测到截断就**把预算翻倍重试**，最多涨到 `max_output_tokens_cap`（16384） | `llm/client.py::is_truncated` |
| 提示词要求**输出压成一行 JSON**、不要输出思考过程 | `llm/prompt.py` |

第二条是关键：原样重试没有意义——同样的预算还会断在同一个地方，白烧三次调用。
现在第一次断了立刻加预算再试，且不走退避（这不是服务端过载，没必要等）。
涨到上限还是拿不到正文会直接抛错并说明是预算问题，不会伪装成网络故障。

### 想省钱可以关掉思考链

标注任务不需要推理，关掉能省一半以上输出 token。参数名以你们的方舟版本为准，
在 `config.yaml` 里透传即可，代码不用改：

```yaml
llm:
  extra_body: {"thinking": {"type": "disabled"}}
  # 或者
  extra_body: {"reasoning": {"effort": "minimal"}}
```

`extra_body` 会原样并进请求体。**先用 `--limit 3 --dry-run` 验证不报 400 再放量**——
参数名猜错会 400，而 4xx 是不重试直接失败的。默认为空，不配就不发这个字段。

---

## 成本与吞吐

- 单条一次调用，system + few-shot 约 **6000 字符（≈3.5k tokens）**，加评论正文
- 关掉 few-shot（`llm.with_fewshot: false`）省约 1.5k tokens/条，但准确率会掉，建议先抽验再关
- 吞吐由 `llm.rate_limit_qps`（全局令牌桶）和 `worker.concurrency` 共同决定，
  `concurrency` 调大之前先把 `mysql.pool_size` 调到不小于它（配置校验会拦）
- 写库攒批：满 `flush_batch_size`（50）或超 `flush_interval`（3s）就 flush

---

## 需要你确认 / 待办

1. **API Key 看着是截断的。** 你给的 `ark-30981b8f-27f5-43ba-882d` 只有 28 位，
   方舟 Key 通常是 36 位 UUID 形态。请用完整 Key 设 `ARK_API_KEY` 环境变量再跑 `check`。
2. **`(channel, scenic_id, comment_id)` 上必须有索引**，否则每条 UPDATE 全表扫描
   （建索引语句见 `sql/optional_review_column.sql`）。如果 `comment_id` 本身全局唯一，
   把 `storage.key_columns` 和 `required_input_fields` 一起改成单列会更快。
   写回未命中时会打 WARNING 并给出样例 `comment_id`，不会静默吞掉。
3. **人工复核标记列源表里没有。** 需求 5.2.2 提到这个字段，`sql/` 里给了加列语句；
   不加就把 `storage.review_column` 留空，引擎自动跳过。
4. **`entity_tags` 加了「景区地名」这个开放取值类型**，因为你给的样例数据里有
   `{"type":"景区地名","value":"瑶琳仙境"}`，而需求的实体表里没有它。景点专名无法枚举，
   所以这一类只校验 type、不校验 value。
5. **实体表里「具体人员」「餐厅名称」标了 `enabled: false`**（需求写的「本期暂不涉及」）。
   二期放开只需把这两行的 `enabled` 去掉，代码不用改。
6. **二期的「标签维护」**（新增/编辑/停用标签、配置同义词库、层级调整）直接改 `taxonomy.yaml` 即可生效，
   要做成管理界面的话，把这份 YAML 换成一张表、在 `Taxonomy.load` 里换个数据源就行，其余不动。

---

## 测试

```bash
python -m pytest          # 242 项，不连 Redis / MySQL / 大模型
```

覆盖：清洗的每种无效形态、JSON 四级容错解析、四条硬约束逐条、入参契约与主键校验、
队列租约与死信、请求体形状、输出截断与预算翻倍、失败池与重标、Redis 版本兼容、
跑批脚本的取数与抽干循环（sqlite 假库真跑 SQL），
以及用假模型跑通的端到端链路（含写库失败重投、模型输出坏掉兜底）。
