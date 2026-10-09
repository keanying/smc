-- ---------------------------------------------------------------------------
-- 可选：人工复核标记列
--
-- 需求 5.2.2「参考分析结果表」里有「人工复核标记（0未复核，1正确，2错误）」。
-- 源表 src_opinion_social_work_comment_di 目前没有这一列。
-- 如果要落这个标记，执行下面的语句加列，然后在 config.yaml 里配置：
--     storage.review_column: label_review_flag
-- 不加列就把 review_column 留空，引擎会自动跳过这一列，不影响其余五个字段的写入。
-- ---------------------------------------------------------------------------

ALTER TABLE `src_opinion_social_work_comment_di`
    ADD COLUMN `label_review_flag` TINYINT NOT NULL DEFAULT 0
        COMMENT 'AI标注或人工复核标记：0未标注 1人工复核正确 2人工复核错误 3复核成功（人工复核） 4AI标注成功 5AI标注错误 6未人工复核';

-- 引擎只写 4/5/6，1/2/3 留给人工：
--   4 AI标注成功      标出结果且置信度达标（含纯表情判中性、景区无关判中性——那是确定的结论）
--   5 AI标注错误      模型调用或解析失败。**五个标注字段一个都不动**，只置这个标记
--   6 未人工复核      标出来了但置信度低于阈值，等人看一眼

-- 跑批按标记筛：
--   python scripts/label_from_table.py                  # flag=0 且未标注的
--   python scripts/label_from_table.py --mode failed    # flag=5 的重标
--   python scripts/label_from_table.py --mode neutral   # 中性的重标（清理历史兜底假数据）


-- ---------------------------------------------------------------------------
-- 写回性能：引擎按 storage.key_columns 定位行，务必保证这组列上有索引，
-- 否则每条 UPDATE 都会全表扫描。
-- 如果 comment_id 本身已经唯一，可以把 key_columns 简化成 [comment_id]，
-- 并建一个唯一索引，写回会更快。
-- ---------------------------------------------------------------------------

-- 组合键（默认配置 storage.key_columns = [channel, scenic_id, comment_id] 对应的索引）
CREATE INDEX `idx_label_key`
    ON `src_opinion_social_work_comment_di` (`channel`, `scenic_id`, `comment_id`);

-- 或者：comment_id 全局唯一时用这个（然后把 key_columns 改成 [comment_id]）
-- CREATE UNIQUE INDEX `uk_comment_id`
--     ON `src_opinion_social_work_comment_di` (`comment_id`);


-- 按标记筛数据时也需要索引
CREATE INDEX `idx_label_review_flag`
    ON `src_opinion_social_work_comment_di` (`label_review_flag`);


-- ---------------------------------------------------------------------------
-- 补跑用：查看标注进度
-- ---------------------------------------------------------------------------
SELECT
    scenic_id,
    channel,
    COUNT(*)                                                            AS total,
    SUM(sentiment_label IS NULL OR sentiment_label = '')                AS unlabeled,
    SUM(sentiment_score = 1)                                            AS positive,
    SUM(sentiment_score = 0)                                            AS neutral,
    SUM(sentiment_score = -1)                                           AS negative,
    SUM(keyword_tags = '[]')                                            AS empty_keyword,
    SUM(dimension_tags = '[]')                                          AS empty_dimension,
    SUM(label_review_flag = 4)                                          AS ai_ok,
    SUM(label_review_flag = 5)                                          AS ai_failed,
    SUM(label_review_flag = 6)                                          AS need_review,
    SUM(label_review_flag IN (1, 2, 3))                                 AS human_reviewed
FROM `src_opinion_social_work_comment_di`
WHERE publish_time >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
GROUP BY scenic_id, channel
ORDER BY unlabeled DESC;


-- ---------------------------------------------------------------------------
-- 排查：写回时大量"未命中"
--
-- 先确认这不是误报：pymysql 默认返回的是"实际改变的行数"而不是"匹配到的行数"，
-- 把同样的值再写一遍会返回 0。引擎已经加了 CLIENT.FOUND_ROWS 修正这一点，
-- 所以升级后再看到未命中才是真的没匹配上。
--
-- 真未命中最常见的原因是**源表有同键重复行**：写回按主键 UPDATE 会把一组重复行
-- 一次全部更新，第二次再标同一个键自然就"没有行需要改"。
-- ---------------------------------------------------------------------------

-- 1) 有多少组重复键？（引擎默认会按主键批内去重，这里是确认数据本身的情况）
SELECT COUNT(*) AS dup_groups, SUM(cnt) AS dup_rows, SUM(cnt) - COUNT(*) AS redundant
FROM (
    SELECT channel, scenic_id, comment_id, COUNT(*) AS cnt
    FROM `src_opinion_social_work_comment_di`
    GROUP BY channel, scenic_id, comment_id
    HAVING COUNT(*) > 1
) t;

-- 2) 看几组具体的重复
SELECT channel, scenic_id, comment_id, COUNT(*) AS cnt,
       GROUP_CONCAT(DISTINCT work_id) AS work_ids
FROM `src_opinion_social_work_comment_di`
GROUP BY channel, scenic_id, comment_id
HAVING COUNT(*) > 1
ORDER BY cnt DESC
LIMIT 20;

-- 3) 主键列有没有前后空格？（utf8mb4_0900_ai_ci 是 NO PAD，尾部空格参与比较）
SELECT COUNT(*) AS padded_rows
FROM `src_opinion_social_work_comment_di`
WHERE channel <> TRIM(channel)
   OR scenic_id <> TRIM(scenic_id)
   OR comment_id <> TRIM(comment_id);

-- 4) 渠道名大小写是否一致
SELECT channel, COUNT(*) FROM `src_opinion_social_work_comment_di`
GROUP BY channel ORDER BY 2 DESC;

-- 5) 标注进度总览（提交数对不上时先看这个）
SELECT
    COUNT(*)                                                   AS total_rows,
    COUNT(DISTINCT channel, scenic_id, comment_id)             AS distinct_keys,
    SUM(sentiment_label IS NULL OR sentiment_label = '')       AS unlabeled,
    SUM(label_review_flag = 4)                                 AS ai_ok,
    SUM(label_review_flag = 5)                                 AS ai_failed,
    SUM(label_review_flag = 6)                                 AS need_review
FROM `src_opinion_social_work_comment_di`;
