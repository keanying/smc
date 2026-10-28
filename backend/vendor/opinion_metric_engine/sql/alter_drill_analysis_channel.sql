-- 景区社媒采集系统 · Copyright (c) 2026 keanying
-- SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
-- 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
-- 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
-- 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

-- 下钻表渠道字段改名（客户 2026-10 变更）：platform_code → channel，platform_name → channel_name
--
-- 已经按旧建表语句建过表的库执行一次即可；新建表直接用 ads_drill_analysis_ddl.sql。
-- 只改列名，数据原样保留。下游推送目标表同样要改。
-- 改完之后，settings_local.py 的 push_pk 里下钻表如果写了 platform_code，要换成 channel。

alter table ads_trf_social_opinion_drill_analysis_di
    change column platform_code channel      varchar(64) null comment '平台编码(=src.channel)',
    change column platform_name channel_name varchar(64) null comment '平台名称';
