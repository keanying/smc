"""表名的唯一出处。

数据库里的表名是**对外契约**——下游数仓、BI、别的系统都按这个名字取数，
所以名字一旦定下来就不该散落在几十条 SQL 字符串里。这里集中一份，
schema.sql 里的建表语句和仓储层的查询都以它为准。

⚠️ 历史包袱：早期用的是 `task` / `scenic` / `social_media_works` 这种短名字。
改名后老库里的数据不能丢，所以 `LEGACY_RENAMES` 保留了新旧对照，
启动建表前会先按它把老表 RENAME 过来（见 app/core/db.py 的 _rename_legacy_tables）。
"""
from __future__ import annotations

from typing import Dict

#: 社媒平台账号
ACCOUNT = "src_opinion_social_account"
#: 平台景区
SCENIC = "src_opinion_social_scenic"
#: 平台景区采集关键字
SCENIC_KEYWORD = "src_opinion_scenic_keyword"
#: 平台景区采集目标（POI / 创作者主页）
SCENIC_TARGET = "src_opinion_scenic_platform_target"
#: 景区的附关键字 / 过滤关键字。
#:
#: 为什么另起一张表而不是给 SCENIC_KEYWORD 加个 kind 列：那张表上有
#: `uk_scenic_keyword (scenic_id, keyword)` 这个唯一键，加 kind 之后
#: 同一个词就没法既当主关键字又当附关键字了，而改唯一键要在**用户已有的
#: 库上**先 DROP 再 CREATE——中间那一瞬间没有唯一约束，而且失败了不好回退。
#: 新表零风险，代价只是多一张表。
SCENIC_FILTER_WORD = "src_opinion_scenic_filter_word"
#: 去哪儿景区档案（开放时间 / 电话 / 介绍 / 优待政策 / 服务设施 / 评级）。
#:
#: 为什么单独一张表而不是往 SCENIC 上加列：这些是**平台侧的资料**，
#: 跟着去哪儿的页面走，会随平台改版增减；而 SCENIC 是本系统自己的主数据。
#: 混在一起的话，以后接了别的平台的档案（携程也有这套字段），
#: 要么再加一堆前缀列，要么互相覆盖。
SCENIC_POI_INFO = "src_opinion_scenic_poi_info"
#: 省市区县三级行政区划，附带尽力匹配上的去哪儿城市ID。
#:
#: 数据不是从去哪儿爬的——去哪儿只给一份扁平城市列表，没有省份归属。
#: 这张表让"按省份筛城市"和"给导入的景区回填 province"成为可能；
#: 名字对不上的行政区，qunar_city_id 留空，**不猜**。
QUNAR_REGION = "src_opinion_qunar_region"
#: 景区档案的"可采集区域"清单，三个渠道各自一份。
#:
#: 为什么不复用 QUNAR_REGION：那张表是**行政区划**（省市区县三级，
#: 国家标准代码），这张是**平台侧的区域标识**——携程是 districtId、
#: 同程是 pid、去哪儿是城市 slug，三套编号互不相通，而且会随平台改版变。
#: 混在一起的话，行政区划这份基础数据会被平台的编号污染。
ARCHIVE_REGION = "src_opinion_archive_region"
#: 账号每日用量（配额闸门的事实来源）。
#:
#: 为什么冷却状态不放这张表：冷却是**账号级**的、不属于某一天，
#: 而这张表按天分片。冷却放在账号表上，`pick_active` 一条 SQL
#: 就能把冷却中的账号排掉，不用 join。
ACCOUNT_QUOTA = "src_opinion_account_quota"
#: 社交媒体平台创作者
AUTHORS = "src_opinion_social_authors"
#: 社媒平台作品
WORKS = "src_opinion_social_work_di"
#: 社媒平台评论
COMMENTS = "src_opinion_social_work_comment_di"
#: 系统设置
SETTING = "src_opinion_sys_setting"
#: 景区采集任务
TASK = "src_opinion_crawl_task"
#: 系统任务日志
TASK_LOG = "src_opinion_crawl_task_log"

#: 旧表名 -> 新表名。只用于启动时的一次性改名，不参与任何查询。
LEGACY_RENAMES: Dict[str, str] = {
    "platform_account": ACCOUNT,
    "scenic": SCENIC,
    "scenic_keyword": SCENIC_KEYWORD,
    "scenic_platform_target": SCENIC_TARGET,
    "social_media_authors": AUTHORS,
    "social_media_works": WORKS,
    "social_media_comments": COMMENTS,
    "sys_setting": SETTING,
    "task": TASK,
    "task_log": TASK_LOG,
}

#: 允许导出的表（导出接口拿它做白名单，别让表名从请求参数直接进 SQL）
EXPORTABLE = {WORKS, COMMENTS}

#: 后加的列：(表名, 列名, 列定义)
#:
#: ⚠️ 与 EXTRA_INDEXES 同理：`CREATE TABLE IF NOT EXISTS` 对已存在的表
#: 什么都不做，往 schema.sql 里加一列老库是加不上的——之后读写引用
#: 新列直接报 Unknown column。启动时按 (表, 列) 比对，缺哪个补哪个。
EXTRA_COLUMNS = [
    # 账号分组：同组账号轮换采集，分摊单账号的请求量与风控压力
    (ACCOUNT, "account_group",
     "VARCHAR(100) NOT NULL DEFAULT 'default' COMMENT '账号分组：同组账号轮换采集，分摊风控压力'"),
    # 人工复核：标注引擎只写 sentiment_*/dimension_tags/entity_tags/keyword_tags 五列，
    # 这三列是采集侧自己维护的审核状态，引擎不碰。
    # 老库升级必须补上，否则审核页面一开就是 Unknown column。
    (COMMENTS, "label_review_flag",
     "TINYINT NOT NULL DEFAULT 0 COMMENT 'AI标注/人工复核标记：0未标注 1人工复核正确 2人工复核错误 3复核成功 4AI标注成功 5AI标注错误 6未人工复核'"),
    (COMMENTS, "label_review_by",
     "VARCHAR(100) NOT NULL DEFAULT '' COMMENT '复核人'"),
    (COMMENTS, "label_review_time",
     "DATETIME NULL DEFAULT NULL COMMENT '复核时间'"),
    # 景区档案从"去哪儿专用"扩成三渠道（同程/携程/去哪儿）时新增的列。
    # v10 已经发出去过，老库里这张表是旧结构，必须在这里补。
    (SCENIC_POI_INFO, "province",
     "VARCHAR(50) NOT NULL DEFAULT '' COMMENT '省份，档案页按省筛选用'"),
    (SCENIC_POI_INFO, "region_id",
     "VARCHAR(100) NOT NULL DEFAULT '' COMMENT '采到这条时用的平台区域标识（携程districtId/同程pid/去哪儿城市slug）'"),
    (SCENIC_POI_INFO, "detail_status",
     "VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '详情采集状态：pending/success/error/unsupported'"),
    (SCENIC_POI_INFO, "detail_error",
     "VARCHAR(500) NOT NULL DEFAULT '' COMMENT '详情采集最后一次的失败原因'"),
    (SCENIC_POI_INFO, "detail_time",
     "DATETIME NULL DEFAULT NULL COMMENT '详情最后一次采集成功的时间'"),
    # 账号冷却：配额用满 / 连续工作超时后，这个账号歇到什么时候。
    # 放在账号表而不是用量表，是为了让 pick_active 一条 SQL 排掉冷却中的账号。
    (ACCOUNT, "cooldown_until",
     "DATETIME NULL DEFAULT NULL COMMENT '冷却到期时间，NULL=不在冷却'"),
    (ACCOUNT, "cooldown_reason",
     "VARCHAR(200) NOT NULL DEFAULT '' COMMENT '进入冷却的原因，给页面显示'"),
    # 账号轮换锁的**每账号覆盖值**。锁本身在 Redis 里（到点自动过期，
    # 不需要清理），这里只存"这个号要锁多久"。0 = 跟随系统设置里的全局值。
    (ACCOUNT, "rotate_lock_hours",
     "INT NOT NULL DEFAULT 0 COMMENT '轮换锁时长（小时），0=用系统设置里的全局值'"),
    # 任务第一次进「等待账号恢复」队列的时间。恢复后按它先来先跑；
    # 恢复时又撞上冷却、再排一次，也保留最初的位置，不会被后来的任务插队。
    (TASK, "queued_at",
     "DATETIME NULL DEFAULT NULL COMMENT '进入等待队列的时间（先来先跑），跑完清空'"),
]

#: 后加的索引：(表名, 索引名, 索引定义)
#:
#: ⚠️ `CREATE TABLE IF NOT EXISTS` 对**已存在**的表什么都不做——
#: 往 schema.sql 里加一个 KEY，老库是加不上的。用户升级后
#: 表结构还是旧的，查询照样慢，而且完全没有任何提示。
#: 所以新增索引必须同时登记在这里，启动时按名字比对补建。
EXTRA_INDEXES = [
    # 数据中心默认视图：无筛选、按发布时间倒序翻页。
    # 缺这个就是全表扫 + filesort，几十万行时首屏要好几秒。
    (WORKS, "idx_works_publish", "(`publish_time`, `id`)"),
    (WORKS, "idx_works_channel_publish", "(`channel`, `publish_time`)"),
    # 展开某条作品的一级评论
    (COMMENTS, "idx_comments_work_level_time",
     "(`channel`, `work_id`(191), `comment_level`, `publish_time`)"),
    # 点开某条评论的回复
    (COMMENTS, "idx_comments_parent_time",
     "(`channel`, `comment_parent_id`(191), `publish_time`)"),
    # ⚠️ 标注引擎按 (channel, scenic_id, comment_id) 定位行做 UPDATE。
    # 没有这个索引，每标一条都是一次全表扫描——几十万行的表上，
    # 补标几千条能跑一整天，而且现象只是"很慢"，看不出原因。
    (COMMENTS, "idx_comments_label_key",
     "(`channel`, `scenic_id`, `comment_id`(191))"),
    # 审核页面：按景区筛未复核/已确认
    (COMMENTS, "idx_comments_review", "(`scenic_id`, `label_review_flag`)"),
    # 数据页支持按点赞/评论数排序（allowed_order 里就有这两个），
    # 但表上没有对应索引——筛完之后必然是一次全量 filesort。
    (WORKS, "idx_works_scenic_likes", "(`scenic_id`, `likes`)"),
    (WORKS, "idx_works_scenic_comments", "(`scenic_id`, `comment_cnt`)"),
    # 作者列表：先在作品表里按景区取 DISTINCT(channel, author_id) 再 JOIN。
    # idx_works_scenic_channel 不含 author_id，所以要回表；带上它就能走覆盖索引。
    (WORKS, "idx_works_scenic_author",
     "(`scenic_id`, `channel`, `author_id`(191))"),
    # 作者列表固定按粉丝数倒序，这张表上原来一个相关索引都没有
    (AUTHORS, "idx_authors_fans", "(`fans_count`, `id`)"),
]
