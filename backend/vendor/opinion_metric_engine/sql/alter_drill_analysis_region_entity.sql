-- 景区社媒采集系统 · Copyright (c) 2026 keanying
-- SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
-- 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
-- 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
-- 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

-- 需求 2.0：下钻分析表新增 region（评论地域）与 entity_tags（实体标签）
--
-- 已经建过 ads_trf_social_opinion_drill_analysis_di 的库执行一次即可；
-- 新库直接用 ads_drill_analysis_ddl.sql，里面已经带了这两列。
--
-- ⚠ 先执行本脚本，再上线新版引擎：引擎会往这两列写数，列不存在时 upsert 报 Unknown column。
-- 历史行这两列为默认值（region='' / entity_tags=NULL），按日期重跑 run 即可回填。

alter table ads_trf_social_opinion_drill_analysis_di
    add column region      varchar(64) default '' null
        comment '评论地域（src.location 归一到省级简称，如 广东/北京；境外保留原文）'
        after dimension_level3,
    add column entity_tags text null
        comment '评论实体标签，JSON 数组（src.entity_tags 原样）'
        after region;
