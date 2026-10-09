-- ---------------------------------------------------------------------------
-- 归类关键词字段 aggregation_keyword_tags
--
-- 解决的问题：keyword_tags 是模型从原文抽的表述，同一个意思有无数种写法——
--     人太多 / 人多 / 人山人海 / 人太多了 / 人挤人 / 乌泱泱
-- 做词云、TOP 榜、趋势时它们被算成不同的词，一个真正的高频问题被切成
-- 十几个低频词，榜上根本看不见。
--
-- aggregation_keyword_tags 存的是**归类后的封闭词表**（见 config/aggregation.yaml），
-- 上面那一串全部变成 ["人多拥挤"]，可以直接 GROUP BY。
-- ---------------------------------------------------------------------------

ALTER TABLE `src_opinion_social_work_comment_di`
    ADD COLUMN `aggregation_keyword_tags` JSON NULL
        COMMENT '归类关键词数组：keyword_tags 按聚合词典归并后的结果，如 ["人多拥挤","排队久"]';

-- 如果这张表的其它 JSON 字段用的是 varchar/text（跟 keyword_tags 保持一致更省事），
-- 就用下面这行代替上面那条：
-- ALTER TABLE `src_opinion_social_work_comment_di`
--     ADD COLUMN `aggregation_keyword_tags` VARCHAR(500) NULL
--         COMMENT '归类关键词数组：keyword_tags 按聚合词典归并后的结果';


-- ---------------------------------------------------------------------------
-- 回填历史数据
--
-- 已经标好的行不用重新调模型——归类是纯字典映射，本地算完直接 UPDATE：
--     python scripts/backfill_aggregation.py --dry-run     # 先看看会归成什么样
--     python scripts/backfill_aggregation.py               # 真回填
-- 改了 aggregation.yaml 之后重跑一次即可，历史数据跟着变，一分钱模型费都不花。
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 取数：归类之后才有意义的几个查询
-- ---------------------------------------------------------------------------

-- 1) 归类关键词 TOP 榜（这是加这个字段的主要目的）
--    MySQL 8.0 用 JSON_TABLE 把数组炸开
SELECT jt.tag, COUNT(*) AS cnt
FROM `src_opinion_social_work_comment_di`,
     JSON_TABLE(`aggregation_keyword_tags`, '$[*]'
                COLUMNS (tag VARCHAR(64) PATH '$')) AS jt
WHERE `aggregation_keyword_tags` IS NOT NULL
  AND publish_time >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
GROUP BY jt.tag
ORDER BY cnt DESC;

-- 2) 分景区看负面归类词，直接指向要解决的问题
SELECT scenic_name, jt.tag, COUNT(*) AS cnt
FROM `src_opinion_social_work_comment_di`,
     JSON_TABLE(`aggregation_keyword_tags`, '$[*]'
                COLUMNS (tag VARCHAR(64) PATH '$')) AS jt
WHERE sentiment_score = -1
  AND publish_time >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
GROUP BY scenic_name, jt.tag
HAVING cnt >= 5
ORDER BY scenic_name, cnt DESC;

-- 3) 归类前 vs 归类后的对比：看看"人多"这件事原本被切成了多少个词
SELECT keyword_tags, aggregation_keyword_tags, COUNT(*) AS cnt
FROM `src_opinion_social_work_comment_di`
WHERE JSON_SEARCH(`aggregation_keyword_tags`, 'one', '人多拥挤') IS NOT NULL
GROUP BY keyword_tags, aggregation_keyword_tags
ORDER BY cnt DESC
LIMIT 50;

-- 4) 还没归类上的行（keyword_tags 有值但 aggregation 为空）
--    数量多说明词典该扩了，用 `python scripts/backfill_aggregation.py --report`
--    可以直接看到高频未归类词
SELECT COUNT(*) AS unaggregated
FROM `src_opinion_social_work_comment_di`
WHERE keyword_tags IS NOT NULL AND keyword_tags <> '[]'
  AND (aggregation_keyword_tags IS NULL
       OR JSON_LENGTH(aggregation_keyword_tags) = 0);

-- 5) 低版本 MySQL（5.7 没有 JSON_TABLE）可以退而求其次用 LIKE 统计单个词
SELECT COUNT(*) FROM `src_opinion_social_work_comment_di`
WHERE aggregation_keyword_tags LIKE '%人多拥挤%';
