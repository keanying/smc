# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""opinion_labeling_engine —— 景区舆情标注引擎。

对外只暴露一个门面：

    from opinion_labeling_engine import LabelingEngine

    engine = LabelingEngine.from_config()
    engine.start()
    engine.submit(row)          # row 为采集侧的一行原始评论（dict）
    ...
    engine.stop()

分层：
    domain      数据模型 / 枚举 / 标签体系      —— 无 IO
    preprocess  内容清洗                        —— 无 IO
    llm         模型客户端 / 提示词 / 输出解析  —— 只与模型交互
    labeling    标注编排 + 后处理规则          —— 业务核心
    queues      内存 / Redis 队列              —— 只与队列交互
    storage     连接池 + 写回                  —— 只与 MySQL 交互
    worker      引擎门面 + 消费线程池          —— 编排以上各层
"""

__version__ = "1.0.0"

from .config import AppConfig, ConfigError, load_config
from .domain import CommentRecord, DimensionTag, EntityTag, LabelResult, Sentiment, Taxonomy
from .worker.engine import LabelingEngine

__all__ = [
    "__version__",
    "AppConfig",
    "ConfigError",
    "load_config",
    "CommentRecord",
    "DimensionTag",
    "EntityTag",
    "LabelResult",
    "Sentiment",
    "Taxonomy",
    "LabelingEngine",
]
