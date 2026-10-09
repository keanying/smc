# 去哪儿景区城市、POI 与评论采集器

**作者：Manus AI**

本项目提供一套可直接运行的 Python 命令行程序，用于采集去哪儿公开景点页面中的城市索引、景区 POI 信息和景区评论。程序优先解析服务端输出的 HTML 与 JSON-LD，不调用未公开接口，也不绕过验证码或登录限制。示例页面当前可直接返回结构化内容，因而无需使用浏览器自动化。[1] [2] [3] [4]

> 需求中的网址均属于去哪儿，而字段说明写的是“携程”。本项目按网址实现去哪儿采集。因此，`city_id` 是去哪儿城市 slug，例如 `guiyang`；`poi_id` 是去哪儿景区 ID，例如 `1703719381`。字段名保持不变，便于接入既有表结构。

## 1. 已实现功能

程序包含三个命令。`cities` 采集去哪儿城市索引，并可生成省、市、区县三级行政区划表。`pois` 按去哪儿城市 slug 分页采集 POI，并继续访问详情页补齐开放时间、电话、介绍、优待政策和服务设施。`comments` 按景区 ID 分页采集评论，并输出用户给定的完整评论字段。

| 模块 | 数据源 | 主要输出 | 去重键 |
| --- | --- | --- | --- |
| 城市索引 | `https://sight.qunar.com/city` | `city_id`、`city_name`、`city_url` | `city_id` |
| 省市区县 | 2026 行政区划三级 JSON，并关联去哪儿城市 | 省、市、区县名称与代码、去哪儿城市映射 | `district_code` |
| 景区 POI | `/list?city={city_id}&page=N` 与 `/{poi_id}` | 用户指定的 11 个 POI 字段 | `poi_id` |
| 景区评论 | `/{scenic_id}/comment?pageNum=N` | 用户指定的 26 个评论字段 | `comment_id` |

行政区划源当前基于民政部地名服务整理的 2026 年三级数据，项目以远程 JSON 方式读取，并在结果中保留 `source_url`。[5] 该数据源不包含港澳台；去哪儿全部 393 个城市仍会完整写入 `cities.csv`。

## 2. 运行方式选择

| Approach | Tradeoffs | Cost | Setup Complexity |
| --- | --- | --- | --- |
| 本地或服务器命令行运行（本项目已实现） | 结构简单、便于审计和断点续采；需要自行启动任务，机器停机时不会运行 | 代码本身免费；仅承担机器与网络成本 | 低 |
| 部署为定时后台任务 | 可按日或按周自动增量采集；需要长期主机、任务监控、日志轮转和合规检查 | 取决于长期主机 | 中 |

当前交付物是第一种方式。它最适合先验证字段和采集规模。若后续需要自动运行，可直接把同一命令放入现有服务器的任务调度器，无需改写解析逻辑。

## 3. 安装

要求 Python 3.10 或更高版本。

```bash
cd qunar_sight_crawler
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows 用户更新旧版本后，应在项目目录重新执行安装，以补充 `tzdata` 时区数据库：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

如果旧代码出现 `ZoneInfoNotFoundError: No time zone found with key Asia/Shanghai`，也可先立即执行：

```powershell
.\.venv\Scripts\python.exe -m pip install tzdata
```

1.0.1 及之后的版本同时内置 UTC+08:00 回退，即使系统和虚拟环境都缺少 IANA 时区文件，错误日志与 `crawl_time` 也不会因此中断。

开发与测试依赖可使用：

```bash
python -m pip install -e '.[dev]'
pytest
```

## 4. 快速开始

### 4.1 城市与省市区县

```bash
qunar-crawl cities \
  --output output/cities.csv \
  --regions-output output/regions.csv
```

`cities.csv` 直接来自去哪儿城市索引。`regions.csv` 是省、市、区县三级明细，并通过规范化名称尽力关联 `qunar_city_id`。名称无法唯一匹配时，去哪儿映射字段留空，程序不会猜测。

如果只需要去哪儿城市，不需要行政区划：

```bash
qunar-crawl cities --skip-regions
```

### 4.2 景区 POI

采集贵阳全部景点：

```bash
qunar-crawl pois guiyang \
  --city-name 贵阳 \
  --output output/pois_guiyang.csv
```

先做一页或少量 POI 的验证：

```bash
qunar-crawl pois guiyang \
  --city-name 贵阳 \
  --max-pages 1 \
  --max-pois 10 \
  --output output/pois_guiyang_sample.csv
```

默认是增量写入。已存在的 `poi_id` 会跳过。`--max-pois` 限制的是本次新增记录数，因此续采时会继续寻找后续未写入的 POI。若要清空结果后重采，添加 `--overwrite`。

### 4.3 景区评论

采集一个景区的全部评论：

```bash
qunar-crawl comments 1703719381 \
  --output output/comments_1703719381.csv
```

只采第一页：

```bash
qunar-crawl comments 1703719381 \
  --max-pages 1 \
  --output output/comments_sample.csv
```

采集第 5 页至第 20 页，起止页均包含：

```bash
qunar-crawl comments 1703719381 \
  --start-page 5 \
  --end-page 20 \
  --output output/comments_5_20.csv
```

从第 21 页开始连续采集 10 页：

```bash
qunar-crawl comments 1703719381 \
  --start-page 21 \
  --max-pages 10 \
  --output output/comments_21_30.csv
```

不指定 `--end-page` 或 `--max-pages` 时，程序读取页面公布的末页页码并自动翻到最后一页。每完成一页，日志都会显示当前页、目标末页、解析条数和新增条数。`--end-page` 与 `--max-pages` 不能同时使用。

批量景区 ID 可通过 CSV 输入。输入文件支持 `scenic_id,scenic_name`，也兼容本项目 POI 文件中的 `poi_id,poi_name`：

```bash
qunar-crawl comments \
  --input output/pois_guiyang.csv \
  --output output/comments_guiyang.csv
```

默认按 `comment_id` 增量去重。若页面未提供源评论 ID，程序会基于景区 ID、用户名、日期和正文生成稳定 SHA-256 标识，并在 ID 前加 `generated-`。

## 5. POI 字段规则

| 字段 | 本项目含义与提取规则 |
| --- | --- |
| `city_id` | 去哪儿城市 slug，例如 `guiyang` |
| `city_name` | 去哪儿城市名称，例如“贵阳” |
| `poi_id` | 去哪儿景区 ID；用于去重 |
| `poi_name` | 详情页 `TouristAttraction.name`，失败时回退到列表卡片 |
| `address` | 详情页 `PostalAddress.streetAddress`，失败时回退到列表地址 |
| `open_time` | `openingHours` 各规则以中文分号连接 |
| `tel` | 公开电话；多个号码以英文分号连接 |
| `scenic_intro` | JSON-LD 描述或图文详情纯文本；不保存 HTML 和图片 |
| `discount_policy` | 每项按“人群｜使用条件｜优待政策”整理，并保留补充说明 |
| `amenity` | 服务设施名称以中文分号连接 |
| `scenic_level` | 列表公开展示的 `1A` 至 `5A`；未显示时留空 |

## 6. 评论字段规则与已知边界

评论页当前在服务端 React 数据中发布 `comment_id`、昵称、日期、评分、地区、头像、票种、来源标签和图片。程序优先读取这些数据，并在失败时回退到 DOM 文本。

| 字段组 | 处理方式 |
| --- | --- |
| 基础关联 | `scenic_id` 使用输入 ID；`scenic_name` 从详情页或评论页提取；`channel` 固定为需求中的 `qunaer`；`work_id` 使用景区 ID |
| 评论层级 | 当前公开页面只展示一级评论，因此 `comment_level=level_1`；父评论为空；`root_comment_id=comment_id`；`sub_comment_count=0` |
| 用户信息 | `commenter_name` 为页面脱敏昵称；`commenter_id` 从公开头像 URL 中提取；页面没有该值时留空 |
| 媒体 | `image_list` 为 JSON 数组；当前页面不发布视频，`video_list=[]` |
| 点赞与回复 | 当前公开页面不发布点赞数和回复树。受非空表结构限制，`likes=0`；限制说明写入 `extra_content` |
| AI 预留字段 | `sentiment_label` 与 `sentiment_score` 留空；三个标签字段写入空 JSON 数组；`label_review_flag=0` |
| 时间 | 只有日期时补为当天 `00:00:00`；`crawl_time` 使用亚洲/上海时区 |
| 扩展信息 | 页面评分、票种、一日游套餐、源标签、源 URL 和限制说明放入 `extra_content` JSON |

MySQL 建表语句见 `schema.sql`。CSV 使用 UTF-8 with BOM，便于 Excel 直接识别中文。

## 7. 稳定性与安全策略

程序默认每次请求至少间隔 1.5 秒，并增加最多 0.5 秒随机抖动。对于 HTTP 429 和 5xx，程序按指数退避重试，并识别 `Retry-After`。程序每个站点首次访问时检查 `robots.txt`；去哪儿当前公开指令允许抓取全站。[6]

程序不会绕过验证码、登录或访问控制。发现验证页时会停止该请求。失败记录写入 `output/errors.jsonl`，正常结果仍会保留。生产运行前应确认网站服务条款、数据授权、个人信息处理依据、保存期限和下游使用范围。

常用控制参数如下：

| 参数 | 作用 |
| --- | --- |
| `--delay 2` | 设置最小请求间隔 |
| `--jitter 1` | 设置随机附加等待上限 |
| `--retries 3` | 设置瞬时错误重试次数 |
| `--max-pages N` | 限制本次页数；`0` 表示全部发现页 |
| `--start-page N` | 从指定页开始 |
| `--end-page N` | 在指定页结束，包含该页；不能与 `--max-pages` 同时使用 |
| `--fail-fast` | 任一页面失败即退出 |
| `--error-log PATH` | 指定 JSON Lines 错误日志 |
| `--overwrite` | 清空对应 POI 或评论文件后重采 |
| `-v` | 输出详细日志 |

## 8. 数据库导入示例

先执行建表脚本：

```bash
mysql --default-character-set=utf8mb4 -u USER -p DATABASE < schema.sql
```

再根据实际 MySQL 权限使用 `LOAD DATA LOCAL INFILE` 或应用侧批量写入。写入时应将空字符串转换为 `NULL` 的字段包括 `sentiment_label`、`sentiment_score` 和可空时间字段。由于 CSV 正文可能包含换行，导入工具必须使用标准 CSV 引号规则，不要按物理行直接切分。

## 9. 测试与维护

```bash
pytest
```

测试覆盖城市解析、行政区划关联、POI 列表和详情解析、评论 React 数据解析、评论 ID、用户 ID、图片数组以及预留字段。网站结构变更后，应先更新 `tests/test_parsers.py` 的固定样例，再修改解析器。大规模采集前建议先使用 `--max-pages 1 --max-pois 10` 验证输出。

## References

[1]: https://sight.qunar.com/city "去哪儿全国景点城市大全"
[2]: https://sight.qunar.com/city/guiyang "去哪儿贵阳景点页"
[3]: https://sight.qunar.com/1703719381 "去哪儿天河潭旅游度假区详情页"
[4]: https://sight.qunar.com/1703719381/comment "去哪儿天河潭旅游度假区评论页"
[5]: https://github.com/kk-418/cn-division "cn-division 中国行政区划数据 2026 版"
[6]: https://sight.qunar.com/robots.txt "去哪儿景点站 robots.txt"
