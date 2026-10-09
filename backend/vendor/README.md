# vendor/ —— 原样引入的第三方组件

这里的东西**不改**。改了就无法跟上游同步，也无法保证它自己那 92 项单测还成立。

## opinion_labeling_engine

景区舆情标注引擎（用户提供，已完成）。采集侧只做两件事：

1. **喂配置**：它的 `config/config.yaml` 里全是 `${ENV}` 插值。
   我们不去改那份文件，而是由 `app/labeling/config_bridge.py` 用
   smc 自己的 mysql / redis / 模型配置**生成一份运行时配置**
   （落在 `data/labeling/engine.runtime.yaml`），再用
   `LabelingEngine.from_config(那个路径)` 装配。
   引擎自带的 `config/config.yaml` 保持出厂状态，只作为字段说明的参考。

2. **调 API**：`submit()` / `label_sync()` / `stats()` / `repository.fetch_unlabeled()`。
   都是它 README 里写明的公开契约。

⚠️ 引擎是**线程模型**（worker 线程池 + PyMySQL 连接池），smc 是 asyncio。
所有会阻塞的调用（`start`、`label_sync`、`stop`、`fetch_unlabeled`）
必须走 `asyncio.to_thread`，否则会把整个事件循环卡住。
`submit()` 本身是非阻塞的（只入队），可以直接调。
