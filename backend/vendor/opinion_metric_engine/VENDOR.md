# 舆情指标引擎（vendored）

来源：https://github.com/keanying/opinion_metric_engine
版本：engin_cli 1.9.2，提交 39694681b06d3ae50a847c0a9a8ae68240f99588（2026-10-27）

在 smc 里作为**独立模块**运行：smc 不 import 它，而是在子进程里跑
（`backend/app/metrics/worker.py`），算挂了、内存吃满也不影响采集服务。
smc 这边只负责：配置（页面维护）、定时、调起子进程、记录运行结果。

同步上游时整目录替换即可，**不要**带上上游的 `settings_local.py`（里面是线上库和推送凭据）；
smc 运行时设置 `OPINION_IGNORE_LOCAL_SETTINGS=1`，配置全部由 smc 传入。

没有带过来的：`settings_local*.py`、`.idea/`、`ATTS.JSON` / `TTS.JSON`（与本引擎无关的样例）、
`scripts/run.sh` 等部署脚本、`tests/` 里的几个本地调试文件。

上游 `tests/test_metrics.py` 引用的是改名前的包 `opinion_etl`，在上游就跑不起来；
`tests/test_domain_single_source.py` 的 9 条口径守卫在上游同样失败（`metrics.py` 里有重复公式），与 smc 无关。

smc 这边绕开的一处上游行为：下钻表「只收含采集关键字的评论」时，上游 `source.fetch_scenic_keywords`
把 `src_opinion_scenic_filter_word` 整张表（含 kind=exclude 的过滤关键字）、以及停用的关键字都当成采集关键字。
smc 不用它：`app/metrics/worker.py` 的 `fetch_drill_keywords` 只取启用的主关键字 + 附关键字，
并且把大盘表 `wordcloud_map` 里的词也留在下钻表里（`filter_drill`）。引擎代码未改。
