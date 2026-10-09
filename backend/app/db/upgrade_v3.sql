-- ===================================================================
-- v3 升级 DDL
--
-- ⚠️ 正常情况下**不需要手工执行**：服务启动时会自动比对并补建
--    （app/core/db.py 的 _ensure_extra_columns / _ensure_extra_indexes，
--     清单在 app/db/tables.py 的 EXTRA_COLUMNS / EXTRA_INDEXES）。
--    这个文件是给"生产库不允许应用自己改表结构、要 DBA 先执行"的场景准备的，
--    内容和自动补建完全一致。
--
-- 本次**没有新增表、没有新增列、没有改过任何列的类型**。
-- 新增的配置（采集模式 collect_engine、内容过滤 content_filter）都存在
-- 任务表已有的 `params` 列里（TEXT，存 JSON），所以表结构不用动。
--
-- 唯一的结构变更是 4 个索引，纯性能，加不加都不影响功能。
--
-- MySQL 5.7 / 8.0 都没有 `CREATE INDEX IF NOT EXISTS`，
-- 所以下面用 information_schema 判断一下再建，重复执行安全。
-- 库名按需要改；不改就用当前 USE 的库。
-- ===================================================================

-- -------------------------------------------------------------------
-- 1. 作品表：数据页按「点赞数 / 评论数」排序用
--    没有它们的话，筛完之后是一次全量 filesort
-- -------------------------------------------------------------------
SET @sql := IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'src_opinion_social_work_di'
        AND INDEX_NAME = 'idx_works_scenic_likes') > 0,
    'SELECT ''idx_works_scenic_likes 已存在，跳过'' AS msg',
    'ALTER TABLE `src_opinion_social_work_di`
       ADD KEY `idx_works_scenic_likes` (`scenic_id`, `likes`)');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'src_opinion_social_work_di'
        AND INDEX_NAME = 'idx_works_scenic_comments') > 0,
    'SELECT ''idx_works_scenic_comments 已存在，跳过'' AS msg',
    'ALTER TABLE `src_opinion_social_work_di`
       ADD KEY `idx_works_scenic_comments` (`scenic_id`, `comment_cnt`)');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- -------------------------------------------------------------------
-- 2. 作品表：作者列表要先在这里按景区取 DISTINCT(channel, author_id)。
--    已有的 idx_works_scenic_channel 不含 author_id，必须回表；
--    带上它之后是覆盖索引。
-- -------------------------------------------------------------------
SET @sql := IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'src_opinion_social_work_di'
        AND INDEX_NAME = 'idx_works_scenic_author') > 0,
    'SELECT ''idx_works_scenic_author 已存在，跳过'' AS msg',
    'ALTER TABLE `src_opinion_social_work_di`
       ADD KEY `idx_works_scenic_author` (`scenic_id`, `channel`, `author_id`(191))');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- -------------------------------------------------------------------
-- 3. 作者表：作者列表固定按粉丝数倒序，这张表原来一个相关索引都没有
-- -------------------------------------------------------------------
SET @sql := IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'src_opinion_social_authors'
        AND INDEX_NAME = 'idx_authors_fans') > 0,
    'SELECT ''idx_authors_fans 已存在，跳过'' AS msg',
    'ALTER TABLE `src_opinion_social_authors`
       ADD KEY `idx_authors_fans` (`fans_count`, `id`)');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- ===================================================================
-- 执行完之后核对一下（应该 4 行都在）
-- ===================================================================
SELECT TABLE_NAME, INDEX_NAME
  FROM information_schema.STATISTICS
 WHERE TABLE_SCHEMA = DATABASE()
   AND INDEX_NAME IN ('idx_works_scenic_likes', 'idx_works_scenic_comments',
                      'idx_works_scenic_author', 'idx_authors_fans')
 GROUP BY TABLE_NAME, INDEX_NAME;


-- ===================================================================
-- 附：更早版本升上来的话，还需要这一列（同样会自动补建）
--     v2 加的账号分组。已经在跑 v2 的库不用管。
-- ===================================================================
-- SET @sql := IF(
--     (SELECT COUNT(*) FROM information_schema.COLUMNS
--       WHERE TABLE_SCHEMA = DATABASE()
--         AND TABLE_NAME = 'src_opinion_social_account'
--         AND COLUMN_NAME = 'account_group') > 0,
--     'SELECT ''account_group 已存在，跳过'' AS msg',
--     'ALTER TABLE `src_opinion_social_account`
--        ADD COLUMN `account_group` VARCHAR(100) NOT NULL DEFAULT ''default''
--        COMMENT ''账号分组：同组账号轮换采集，分摊风控压力''');
-- PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
