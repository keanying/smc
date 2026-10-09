-- ===================================================================
-- 景区社媒采集系统 —— MySQL 表结构
-- 字符集统一 utf8mb4 / utf8mb4_general_ci，引擎 InnoDB，行格式 DYNAMIC
--
-- 命名与字段以需求文档为准，以下几处做了工程性调整（README 有说明）：
--   1) publish_time / crawl_time 用 DATETIME 而非 TIMESTAMP：
--      TIMESTAMP 上限 2038-01-19，且会随会话时区做隐式转换，
--      跨容器部署时同一条数据读出来的时间会变。DATETIME 无这两个问题。
--      create_time / update_time 仍用 TIMESTAMP，因为要靠它自动维护。
--   2) 长字段（work_id/author_id 等 VARCHAR(600)）建索引时用前缀索引，
--      否则单个索引键最长 2400 字节，既浪费空间又拖慢写入。
--   3) 唯一键用 MD5 生成列（work_uk / comment_uk），
--      让 INSERT ... ON DUPLICATE KEY UPDATE 能直接工作。
--   4) channel 取值用 douyin（需求文档里写的 douying 是笔误，
--      如需保持原样只改 app/core/constants.py 一处即可）。
--
-- 表名统一 src_opinion_ 前缀（表名是对下游数仓/BI 的契约，集中定义在
-- app/db/tables.py，这里的建表语句和仓储层的 SQL 都以那份为准）：
--   src_opinion_social_account          社媒平台账号
--   src_opinion_social_scenic           平台景区
--   src_opinion_scenic_keyword          平台景区采集关键字
--   src_opinion_scenic_platform_target  平台景区采集目标
--   src_opinion_social_authors          社交媒体平台创作者
--   src_opinion_social_work_di          社媒平台作品
--   src_opinion_social_work_comment_di  社媒平台评论
--   src_opinion_sys_setting             系统设置
--   src_opinion_crawl_task              景区采集任务
--   src_opinion_crawl_task_log          系统任务日志
-- 老库里的 task / scenic / social_media_works 等短表名，启动时会自动
-- RENAME 成上面的名字（见 app/core/db.py 的 _rename_legacy_tables），数据不动。
-- ===================================================================

-- -------------------------------------------------------------------
-- 一、景区维度
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `src_opinion_social_scenic` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `scenic_id`   VARCHAR(100) NOT NULL                COMMENT '景区ID（业务主键，由使用方导入）',
  `scenic_name` VARCHAR(200) NOT NULL                COMMENT '景区名称',
  `province`    VARCHAR(50)      NULL DEFAULT NULL   COMMENT '省份',
  `city`        VARCHAR(50)      NULL DEFAULT NULL   COMMENT '城市',
  `remark`      VARCHAR(500)     NULL DEFAULT NULL   COMMENT '备注',
  `enabled`     TINYINT      NOT NULL DEFAULT 1      COMMENT '1=启用 0=停用',
  `create_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_scenic_scenic_id` (`scenic_id`),
  KEY `idx_scenic_name` (`scenic_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='平台景区';

CREATE TABLE IF NOT EXISTS `src_opinion_scenic_keyword` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `scenic_id`   VARCHAR(100) NOT NULL                COMMENT '所属景区ID',
  `keyword`     VARCHAR(200) NOT NULL                COMMENT '搜索关键字',
  `enabled`     TINYINT      NOT NULL DEFAULT 1,
  `sort_order`  INT          NOT NULL DEFAULT 0      COMMENT '排序，越小越靠前',
  `create_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_scenic_keyword` (`scenic_id`, `keyword`),
  KEY `idx_scenic_keyword_scenic` (`scenic_id`, `enabled`, `sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='平台景区采集关键字';

-- 景区在各平台的采集目标：
--   target_type=poi     携程 POI_ID / 同程 sid
--   target_type=creator 指定用户主页（抖音 sec_uid、小红书 user_id、微博 uid 等）
CREATE TABLE IF NOT EXISTS `src_opinion_scenic_poi_info` (
  `id`              BIGINT       NOT NULL AUTO_INCREMENT,
  `channel`         VARCHAR(20)  NOT NULL                COMMENT '来源平台，目前只有 qunar',
  `poi_id`          VARCHAR(100) NOT NULL                COMMENT '平台侧景区ID',
  `scenic_id`       VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '关联到本系统的景区ID，没导入时为空',
  `poi_name`        VARCHAR(200) NOT NULL DEFAULT ''     COMMENT '景区名称',
  `city_id`         VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '平台侧城市标识，如 guiyang',
  `city_name`       VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '城市名称',
  `address`         VARCHAR(500) NOT NULL DEFAULT ''     COMMENT '地址',
  `open_time`       TEXT             NULL                COMMENT '开放时间',
  `tel`             VARCHAR(200) NOT NULL DEFAULT ''     COMMENT '联系电话',
  `scenic_level`    VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '评级，如 5A',
  `scenic_intro`    LONGTEXT         NULL                COMMENT '景区介绍',
  `discount_policy` LONGTEXT         NULL                COMMENT '优待政策',
  `amenity`         LONGTEXT         NULL                COMMENT '服务设施',
  `source_url`      VARCHAR(500) NOT NULL DEFAULT ''     COMMENT '来源页面',
  `province`        VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '省份，档案页按省筛选用',
  `region_id`       VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '采到这条时用的平台区域标识',
  `detail_status`   VARCHAR(20)  NOT NULL DEFAULT 'pending' COMMENT '详情采集状态：pending/success/error/unsupported',
  `detail_error`    VARCHAR(500) NOT NULL DEFAULT ''     COMMENT '详情采集最后一次的失败原因',
  `detail_time`     DATETIME         NULL                COMMENT '详情最后一次采集成功的时间',
  `crawl_time`      DATETIME         NULL                COMMENT '采集时间',
  `create_time`     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_poi_info` (`channel`, `poi_id`),
  KEY `idx_poi_info_city` (`channel`, `city_id`),
  KEY `idx_poi_info_scenic` (`scenic_id`),
  KEY `idx_poi_info_province` (`channel`, `province`),
  KEY `idx_poi_info_detail` (`channel`, `detail_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='景区档案（同程/携程/去哪儿）';


CREATE TABLE IF NOT EXISTS `src_opinion_account_quota` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `channel`        VARCHAR(20)  NOT NULL                COMMENT '平台',
  `account_name`   VARCHAR(100) NOT NULL                COMMENT '账号标识',
  `stat_date`      DATE         NOT NULL                COMMENT '统计日期',
  `works`          INT          NOT NULL DEFAULT 0      COMMENT '当天采到的作品数',
  `last_active_at` DATETIME         NULL                COMMENT '当天最后一次采集时间',
  `create_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_account_quota` (`channel`, `account_name`, `stat_date`),
  KEY `idx_account_quota_date` (`stat_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='账号每日用量（配额闸门）';


CREATE TABLE IF NOT EXISTS `src_opinion_archive_region` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `channel`       VARCHAR(20)  NOT NULL                COMMENT '渠道：tongcheng / ctrip / qunar',
  `region_id`     VARCHAR(100) NOT NULL                COMMENT '平台侧区域标识：携程districtId / 同程pid / 去哪儿城市slug',
  `region_name`   VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '区域名称',
  `level`         VARCHAR(20)  NOT NULL DEFAULT 'province' COMMENT 'province / city',
  `poi_count`     INT          NOT NULL DEFAULT 0      COMMENT '上次采集采到多少条',
  `last_sync_time` DATETIME        NULL                COMMENT '上次采完的时间，没采过为 NULL',
  `create_time`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_archive_region` (`channel`, `region_id`),
  KEY `idx_archive_region_channel` (`channel`, `level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='景区档案的可采集区域清单';


CREATE TABLE IF NOT EXISTS `src_opinion_qunar_region` (
  `id`               BIGINT       NOT NULL AUTO_INCREMENT,
  `province_code`    VARCHAR(20)  NOT NULL DEFAULT ''     COMMENT '省级行政区划代码',
  `province_name`    VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '省/直辖市/自治区',
  `city_code`        VARCHAR(20)  NOT NULL DEFAULT ''     COMMENT '市级代码',
  `city_name`        VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '地级市',
  `district_code`    VARCHAR(20)  NOT NULL DEFAULT ''     COMMENT '区县代码',
  `district_name`    VARCHAR(50)  NOT NULL DEFAULT ''     COMMENT '区县',
  `qunar_city_id`    VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '匹配上的去哪儿城市标识，匹配不上留空（不猜）',
  `qunar_city_name`  VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '匹配上的去哪儿城市名',
  `source_url`       VARCHAR(500) NOT NULL DEFAULT ''     COMMENT '行政区划数据来源',
  `sync_time`        DATETIME         NULL                COMMENT '同步时间',
  `create_time`      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_region` (`province_code`, `city_code`, `district_code`),
  KEY `idx_region_qunar_city` (`qunar_city_id`),
  KEY `idx_region_province` (`province_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='省市区县行政区划（含去哪儿城市映射）';


CREATE TABLE IF NOT EXISTS `src_opinion_scenic_filter_word` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `scenic_id`   VARCHAR(100) NOT NULL                COMMENT '所属景区ID',
  `kind`        VARCHAR(20)  NOT NULL                COMMENT 'aux=附关键字（命中就留存）/ exclude=过滤关键字（命中就丢弃）',
  `word`        VARCHAR(200) NOT NULL                COMMENT '词本身',
  `enabled`     TINYINT      NOT NULL DEFAULT 1,
  `sort_order`  INT          NOT NULL DEFAULT 0      COMMENT '排序，越小越靠前',
  `create_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_scenic_filter_word` (`scenic_id`, `kind`, `word`),
  KEY `idx_scenic_filter_word` (`scenic_id`, `kind`, `enabled`, `sort_order`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='景区的附关键字与过滤关键字';


CREATE TABLE IF NOT EXISTS `src_opinion_scenic_platform_target` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `scenic_id`   VARCHAR(100) NOT NULL,
  `channel`     VARCHAR(20)  NOT NULL                COMMENT 'douyin/kuaishou/weibo/xiaohongshu/ctrip/tongcheng',
  `target_type` VARCHAR(20)  NOT NULL DEFAULT 'poi'  COMMENT 'poi | creator',
  `target_id`   VARCHAR(600) NOT NULL                COMMENT 'POI_ID / sid / 作者ID',
  `target_name` VARCHAR(300)     NULL DEFAULT NULL,
  `target_url`  VARCHAR(600)     NULL DEFAULT NULL,
  `extra`       TEXT             NULL DEFAULT NULL   COMMENT '平台特有参数，JSON',
  `enabled`     TINYINT      NOT NULL DEFAULT 1,
  `create_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_scenic_target` (`scenic_id`, `channel`, `target_type`, `target_id`(191)),
  KEY `idx_target_channel` (`channel`, `target_type`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='平台景区采集目标（POI/主页）';

-- -------------------------------------------------------------------
-- 二、账号与系统设置
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `src_opinion_social_account` (
  `id`                BIGINT       NOT NULL AUTO_INCREMENT,
  `channel`           VARCHAR(20)  NOT NULL,
  `account_name`      VARCHAR(120) NOT NULL          COMMENT '账号标识，用户自定义，唯一定位一个浏览器 profile',
  `nickname`          VARCHAR(200)     NULL DEFAULT NULL,
  `avatar`            VARCHAR(600)     NULL DEFAULT NULL,
  `uid`               VARCHAR(200)     NULL DEFAULT NULL COMMENT '平台侧用户ID',
  `login_type`        VARCHAR(20)  NOT NULL DEFAULT 'qrcode' COMMENT 'qrcode|phone|cookie|none',
  `account_group`     VARCHAR(100) NOT NULL DEFAULT 'default' COMMENT '账号分组：同组账号轮换采集，分摊风控压力',
  `status`            VARCHAR(20)  NOT NULL DEFAULT 'never_login'
                      COMMENT 'never_login|active|expired|disabled',
  `profile_dir`       VARCHAR(500)     NULL DEFAULT NULL COMMENT '持久化浏览器 profile 目录（保存指纹与登录态）',
  `fingerprint`       TEXT             NULL DEFAULT NULL COMMENT '该账号绑定的设备指纹，JSON',
  `cookies`           LONGTEXT         NULL DEFAULT NULL COMMENT '最近一次静默提取的 Cookie，JSON 数组',
  `cookie_updated_at` DATETIME         NULL DEFAULT NULL,
  `last_check_time`   DATETIME         NULL DEFAULT NULL,
  `last_error`        VARCHAR(500)     NULL DEFAULT NULL,
  `enabled`           TINYINT      NOT NULL DEFAULT 1,
  `create_time`       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_account` (`channel`, `account_name`),
  KEY `idx_account_pick` (`channel`, `status`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='社媒平台账号';

-- 系统设置：页面上改的配置存这里，优先级高于 config.yaml
CREATE TABLE IF NOT EXISTS `src_opinion_sys_setting` (
  `setting_key`   VARCHAR(120) NOT NULL,
  `setting_value` LONGTEXT         NULL DEFAULT NULL COMMENT 'JSON',
  `update_time`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`setting_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='系统设置（代理等）';

-- -------------------------------------------------------------------
-- 三、任务
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `src_opinion_crawl_task` (
  `id`                  BIGINT       NOT NULL AUTO_INCREMENT,
  `task_id`             VARCHAR(64)  NOT NULL,
  `task_name`           VARCHAR(200) NOT NULL,
  `scenic_id`           VARCHAR(100)     NULL DEFAULT NULL COMMENT '任务归属景区，可为空表示跨景区',
  `channels`            VARCHAR(300) NOT NULL DEFAULT '[]'  COMMENT '本任务涉及的平台，JSON 数组',
  `collect_type`        VARCHAR(20)  NOT NULL DEFAULT 'keyword' COMMENT 'keyword|creator|poi|detail',
  `keywords`            TEXT             NULL DEFAULT NULL COMMENT 'JSON 数组',
  `targets`             TEXT             NULL DEFAULT NULL COMMENT 'JSON 数组：POI/主页目标',
  `params`              TEXT             NULL DEFAULT NULL COMMENT '本次采集参数覆盖，JSON',
  `status`              VARCHAR(20)  NOT NULL DEFAULT 'pending'
                        COMMENT 'pending|queued|running|completed|failed|canceled|paused',
  `progress`            INT          NOT NULL DEFAULT 0,
  `schedule_type`       VARCHAR(20)  NOT NULL DEFAULT 'once' COMMENT 'once|at|interval|cron',
  `schedule_enabled`    TINYINT      NOT NULL DEFAULT 0,
  `schedule_at`         DATETIME         NULL DEFAULT NULL COMMENT 'once/at 模式的执行时刻',
  `schedule_interval_seconds` BIGINT     NULL DEFAULT NULL COMMENT 'interval 模式的间隔秒数',
  `cron_expression`     VARCHAR(120)     NULL DEFAULT NULL COMMENT 'cron 模式：5 或 6 段表达式',
  `timezone`            VARCHAR(64)  NOT NULL DEFAULT 'Asia/Shanghai',
  `next_run_time`       DATETIME         NULL DEFAULT NULL,
  `queued_at`           DATETIME         NULL DEFAULT NULL COMMENT '进入等待队列的时间（先来先跑），跑完清空',
  `last_run_time`       DATETIME         NULL DEFAULT NULL,
  `runs_count`          INT          NOT NULL DEFAULT 0,
  `start_time`          DATETIME         NULL DEFAULT NULL,
  `end_time`            DATETIME         NULL DEFAULT NULL,
  `stat_new_works`      INT          NOT NULL DEFAULT 0,
  `stat_updated_works`  INT          NOT NULL DEFAULT 0,
  `stat_new_comments`   INT          NOT NULL DEFAULT 0,
  `stat_updated_comments` INT        NOT NULL DEFAULT 0,
  `error`               TEXT             NULL DEFAULT NULL,
  `created_by`          VARCHAR(100)     NULL DEFAULT NULL,
  `create_time`         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_task_id` (`task_id`),
  KEY `idx_task_status` (`status`, `create_time`),
  KEY `idx_task_schedule` (`schedule_enabled`, `next_run_time`),
  KEY `idx_task_scenic` (`scenic_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='景区采集任务';

CREATE TABLE IF NOT EXISTS `src_opinion_crawl_task_log` (
  `id`       BIGINT      NOT NULL AUTO_INCREMENT,
  `task_id`  VARCHAR(64) NOT NULL,
  `log_time` DATETIME(3) NOT NULL,
  `level`    VARCHAR(16) NOT NULL DEFAULT 'INFO',
  `channel`  VARCHAR(20)     NULL DEFAULT NULL,
  `message`  TEXT            NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_task_log_task` (`task_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='系统任务日志';

-- -------------------------------------------------------------------
-- 四、核心数据表
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `src_opinion_social_work_di` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `scenic_id`      VARCHAR(100) NOT NULL DEFAULT ''   COMMENT '景区ID',
  `scenic_name`    VARCHAR(100) NOT NULL DEFAULT ''   COMMENT '景区名字',
  `channel`        VARCHAR(20)  NOT NULL              COMMENT 'weibo/douyin/kuaishou/xiaohongshu/ctrip/tongcheng',
  `work_id`        VARCHAR(600) NOT NULL              COMMENT '作品ID、帖子ID、笔记ID',
  `work_url`       VARCHAR(600) NOT NULL DEFAULT ''   COMMENT '作品/帖子/笔记链接',
  `author_id`      VARCHAR(600) NOT NULL DEFAULT ''   COMMENT '作者ID',
  `author_name`    VARCHAR(600) NOT NULL DEFAULT ''   COMMENT '作者名称',
  `title`          TEXT             NULL DEFAULT NULL COMMENT '发布的文字内容',
  `description`    TEXT             NULL DEFAULT NULL COMMENT '描述',
  `label`          TEXT             NULL DEFAULT NULL COMMENT '标签',
  `image_list`     LONGTEXT         NULL DEFAULT NULL COMMENT '图片列表，JSON 数组',
  `video_list`     LONGTEXT         NULL DEFAULT NULL COMMENT '视频列表，JSON 数组',
  `likes`          BIGINT       NOT NULL DEFAULT 0    COMMENT '点赞数',
  `collection_cnt` BIGINT       NOT NULL DEFAULT 0    COMMENT '收藏数',
  `comment_cnt`    BIGINT       NOT NULL DEFAULT 0    COMMENT '评论数',
  `shares`         BIGINT       NOT NULL DEFAULT 0    COMMENT '分享数',
  `location`       VARCHAR(50)  NOT NULL DEFAULT ''   COMMENT '发布地址',
  `publish_time`   DATETIME         NULL DEFAULT NULL COMMENT '发布时间',
  `crawl_time`     DATETIME         NULL DEFAULT NULL COMMENT '采集时间',
  -- 以下为需求字段之外的工程补充列
  `extra_content`  LONGTEXT         NULL DEFAULT NULL COMMENT '平台特有字段，JSON（避免原始信息丢失）',
  `source_keyword` VARCHAR(200) NOT NULL DEFAULT ''   COMMENT '命中的搜索关键字，空=非关键字来源',
  `task_id`        VARCHAR(64)  NOT NULL DEFAULT ''   COMMENT '产生该条记录的任务',
  `create_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `work_uk`        CHAR(32) AS (MD5(CONCAT(`scenic_id`, 0x1F, `channel`, 0x1F, `work_id`))) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_works` (`work_uk`),
  KEY `idx_works_scenic_channel` (`scenic_id`, `channel`, `publish_time`),
  KEY `idx_works_channel_work` (`channel`, `work_id`(191)),
  KEY `idx_works_author` (`channel`, `author_id`(191)),
  KEY `idx_works_keyword` (`scenic_id`, `source_keyword`),
  KEY `idx_works_task` (`task_id`),
  KEY `idx_works_crawl_time` (`crawl_time`),
  -- 数据中心默认视图：不带任何筛选、按发布时间倒序翻页。
  -- 没有这个索引就是全表扫 + filesort，几十万行时首屏要好几秒。
  -- 带上 id 是为了让 `ORDER BY publish_time DESC, id DESC` 整条排序都走索引。
  KEY `idx_works_publish` (`publish_time`, `id`),
  -- 只按平台筛（不选景区）也是常用组合
  KEY `idx_works_channel_publish` (`channel`, `publish_time`),
  -- 数据页支持按点赞数/评论数排序，没有这两个就是筛完之后全量 filesort
  KEY `idx_works_scenic_likes` (`scenic_id`, `likes`),
  KEY `idx_works_scenic_comments` (`scenic_id`, `comment_cnt`),
  -- 作者列表：先在这张表上按景区取 DISTINCT(channel, author_id) 再 JOIN。
  -- idx_works_scenic_channel 不含 author_id，要回表；带上它才是覆盖索引。
  KEY `idx_works_scenic_author` (`scenic_id`, `channel`, `author_id`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='社媒平台作品';

CREATE TABLE IF NOT EXISTS `src_opinion_social_work_comment_di` (
  `id`                BIGINT       NOT NULL AUTO_INCREMENT,
  `scenic_id`         VARCHAR(100) NOT NULL DEFAULT ''  COMMENT '景区ID',
  `scenic_name`       VARCHAR(100) NOT NULL DEFAULT ''  COMMENT '景区名字',
  `channel`           VARCHAR(20)  NOT NULL,
  `work_id`           VARCHAR(600) NOT NULL DEFAULT ''  COMMENT '作品ID、帖子ID、笔记ID',
  `comment_level`     VARCHAR(20)  NOT NULL DEFAULT 'level_1' COMMENT 'level_1|level_2|level_3...',
  `comment_parent_id` VARCHAR(600) NOT NULL DEFAULT ''  COMMENT '父评论ID，一级评论为空',
  `comment_id`        VARCHAR(300) NOT NULL            COMMENT '评论ID',
  `commenter_id`      VARCHAR(600) NOT NULL DEFAULT ''  COMMENT '评论用户ID',
  `image_list`        LONGTEXT         NULL DEFAULT NULL COMMENT '图片列表，JSON 数组',
  `video_list`        LONGTEXT         NULL DEFAULT NULL COMMENT '视频列表，JSON 数组',
  `location`          VARCHAR(50)  NOT NULL DEFAULT ''  COMMENT '发布地址',
  `content`           LONGTEXT         NULL DEFAULT NULL COMMENT '评论内容',
  `likes`             BIGINT       NOT NULL DEFAULT 0   COMMENT '点赞数',
  `extra_content`     LONGTEXT         NULL DEFAULT NULL COMMENT '其他内容，JSON',
  -- AI 标注字段：本期只建列不写值，后续接入标注服务时无需改表
  `sentiment_label`   VARCHAR(50)      NULL DEFAULT NULL COMMENT '整体情感标签（AI 标注，待接入）',
  `sentiment_score`   INT              NULL DEFAULT NULL COMMENT '整体情感得分（AI 标注，待接入）',
  `dimension_tags`    TEXT             NULL DEFAULT NULL COMMENT '维度标签数组（AI 标注，待接入）',
  `entity_tags`       TEXT             NULL DEFAULT NULL COMMENT '实体标签数组（AI 标注，待接入）',
  `keyword_tags`      TEXT             NULL DEFAULT NULL COMMENT '关键词数组（AI 标注）',
  -- AI标注 / 人工复核标记。**引擎和采集侧共用这一列**：
  --   0 未标注        1 人工复核正确   2 人工复核错误   3 复核成功（人工复核）
  --   4 AI标注成功    5 AI标注错误     6 未人工复核（AI标了但置信度低）
  -- 1/2/3 由审核页面写，4/5/6 由标注引擎写（见引擎 storage.review_flags）。
  -- 跑批默认只取 0（pending_flags），已标过的不重复花钱。
  `label_review_flag` TINYINT      NOT NULL DEFAULT 0   COMMENT 'AI标注/人工复核标记：0未标注 1人工复核正确 2人工复核错误 3复核成功 4AI标注成功 5AI标注错误 6未人工复核',
  `label_review_by`   VARCHAR(100) NOT NULL DEFAULT ''  COMMENT '复核人',
  `label_review_time` DATETIME         NULL DEFAULT NULL COMMENT '复核时间',
  `publish_time`      DATETIME         NULL DEFAULT NULL COMMENT '发布时间',
  `crawl_time`        DATETIME         NULL DEFAULT NULL COMMENT '采集时间',
  -- 以下为需求字段之外的工程补充列
  `commenter_name`    VARCHAR(600) NOT NULL DEFAULT ''  COMMENT '评论用户昵称（列表展示需要）',
  `root_comment_id`   VARCHAR(300) NOT NULL DEFAULT ''  COMMENT '所属一级评论ID，便于一次查出整条会话',
  `sub_comment_count` BIGINT       NOT NULL DEFAULT 0   COMMENT '子评论数，前端据此决定是否显示展开',
  `task_id`           VARCHAR(64)  NOT NULL DEFAULT '',
  `create_time`       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `comment_uk`        CHAR(32) AS (MD5(CONCAT(`scenic_id`, 0x1F, `channel`, 0x1F, `work_id`, 0x1F, `comment_id`))) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_comments` (`comment_uk`),
  KEY `idx_comments_work` (`channel`, `work_id`(191), `comment_level`),
  KEY `idx_comments_parent` (`channel`, `comment_parent_id`(191)),
  KEY `idx_comments_root` (`channel`, `root_comment_id`),
  KEY `idx_comments_scenic` (`scenic_id`, `channel`, `publish_time`),
  KEY `idx_comments_task` (`task_id`),
  KEY `idx_comments_sentiment` (`scenic_id`, `sentiment_label`),
  -- 标注引擎按 (channel, scenic_id, comment_id) 定位行做 UPDATE。
  -- 没有这个索引，每标一条都是一次全表扫描。
  KEY `idx_comments_label_key` (`channel`, `scenic_id`, `comment_id`(191)),
  -- 审核页面：按景区筛未复核/已确认
  KEY `idx_comments_review` (`scenic_id`, `label_review_flag`),
  -- 展开某条作品的一级评论：channel + work_id + comment_level 过滤，
  -- 再按 publish_time 倒序。把排序列也放进索引，避免每次展开都 filesort。
  KEY `idx_comments_work_level_time`
      (`channel`, `work_id`(191), `comment_level`, `publish_time`),
  -- 点开某条评论的回复
  KEY `idx_comments_parent_time` (`channel`, `comment_parent_id`(191), `publish_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='社媒平台评论';

-- -------------------------------------------------------------------
-- 五、创作者（数据中心「作品→创作者」维度展示用）
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `src_opinion_social_authors` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT,
  `channel`        VARCHAR(20)  NOT NULL,
  `author_id`      VARCHAR(600) NOT NULL,
  `author_name`    VARCHAR(600) NOT NULL DEFAULT '',
  `avatar`         VARCHAR(600) NOT NULL DEFAULT '',
  `signature`      TEXT             NULL DEFAULT NULL,
  `gender`         VARCHAR(20)  NOT NULL DEFAULT '',
  `location`       VARCHAR(50)  NOT NULL DEFAULT '',
  `home_url`       VARCHAR(600) NOT NULL DEFAULT '',
  `fans_count`     BIGINT       NOT NULL DEFAULT 0,
  `follow_count`   BIGINT       NOT NULL DEFAULT 0,
  `works_count`    BIGINT       NOT NULL DEFAULT 0,
  `liked_count`    BIGINT       NOT NULL DEFAULT 0,
  `extra_content`  LONGTEXT         NULL DEFAULT NULL,
  `crawl_time`     DATETIME         NULL DEFAULT NULL,
  `create_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `author_uk`      CHAR(32) AS (MD5(CONCAT(`channel`, 0x1F, `author_id`))) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_authors` (`author_uk`),
  KEY `idx_authors_name` (`author_name`(191)),
  -- 作者列表固定按粉丝数倒序，没有它必然 filesort
  KEY `idx_authors_fans` (`fans_count`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='社交媒体平台创作者';
