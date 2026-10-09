"""配置加载。

职责边界：
    - 只负责「把 YAML 读成强类型对象 + 做启动期校验」，不含任何业务逻辑。
    - 支持 ``${ENV}`` / ``${ENV:default}`` 环境变量插值，敏感信息不落文件。
    - 校验失败一律在进程启动时抛 ``ConfigError`` 直接退出，不允许带病运行。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

__all__ = [
    "ConfigError",
    "AppConfig",
    "LoggingConfig",
    "LLMConfig",
    "CleaningConfig",
    "LabelingConfig",
    "QueueConfig",
    "FailureStoreConfig",
    "RedisConfig",
    "WorkerConfig",
    "MySQLConfig",
    "StorageConfig",
    "load_config",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TAXONOMY_PATH",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_TAXONOMY_PATH = _PROJECT_ROOT / "config" / "taxonomy.yaml"

# ${NAME} 或 ${NAME:default}；default 允许包含冒号以外的任意字符
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


class ConfigError(RuntimeError):
    """配置缺失、类型错误或取值非法。"""


# ---------------------------------------------------------------------------
# 环境变量插值
# ---------------------------------------------------------------------------
def _interpolate(value: Any) -> Any:
    """递归把字符串里的 ``${ENV:default}`` 替换成环境变量值。

    环境变量不存在且无默认值时替换为空字符串，交由后续 dataclass 校验去报错，
    这样报错信息能落到具体配置项上，而不是一句笼统的 KeyError。
    """
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if not isinstance(value, str):
        return value

    def _sub(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        return os.environ.get(name, default if default is not None else "")

    replaced = _ENV_PATTERN.sub(_sub, value)
    return _coerce_scalar(replaced) if replaced != value else value


def _coerce_scalar(text: str) -> Any:
    """插值之后的字符串按 YAML 标量语义还原成 int/float/bool。

    ``${WORKER_CONCURRENCY:4}`` 插值后是字符串 "4"，不还原会导致 dataclass 拿到 str。
    """
    lowered = text.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    return text


def _section(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"配置节 [{name}] 必须是字典，实际是 {type(section).__name__}")
    return section


# ---------------------------------------------------------------------------
# 各配置节
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    dir: str = "logs"
    file: str = "engine.log"
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 7
    log_content: bool = False


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    endpoint: str = "/responses"
    api_key: str = ""
    model: str = ""
    temperature: float = 0.01
    top_p: float = 0.7
    max_output_tokens: int = 9096
    max_output_tokens_cap: int = 16384
    extra_body: Dict[str, Any] = field(default_factory=dict)
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    max_retries: int = 3
    retry_backoff_base: float = 1.0
    retry_backoff_max: float = 20.0
    rate_limit_qps: float = 8.0
    strategy: str = "single"
    with_fewshot: bool = True
    fewshot_style: str = "inline"
    system_role: str = "system"

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.endpoint.lstrip('/')}"

    def validate(self) -> None:
        if not self.api_key:
            raise ConfigError("llm.api_key 为空：请设置环境变量 ARK_API_KEY 或填入 config.yaml")
        if not self.model:
            raise ConfigError("llm.model 为空")
        if self.fewshot_style not in {"message", "inline"}:
            raise ConfigError(
                f"llm.fewshot_style 只支持 message|inline，当前 {self.fewshot_style!r}")
        if self.system_role not in {"system", "user"}:
            raise ConfigError(
                f"llm.system_role 只支持 system|user，当前 {self.system_role!r}")
        if self.strategy not in {"single", "batch"}:
            raise ConfigError(f"llm.strategy 只支持 single|batch，当前 {self.strategy!r}")
        if self.max_retries < 0:
            raise ConfigError("llm.max_retries 不能为负")
        if self.max_output_tokens_cap < self.max_output_tokens:
            raise ConfigError(
                f"llm.max_output_tokens_cap({self.max_output_tokens_cap}) "
                f"不能小于 max_output_tokens({self.max_output_tokens})")
        if not isinstance(self.extra_body, dict):
            raise ConfigError("llm.extra_body 必须是字典")


@dataclass(frozen=True)
class CleaningConfig:
    min_valid_length: int = 2
    max_content_length: int = 1000
    use_extra_content: bool = True
    invalid_sentiment_label: str = "中性"
    invalid_sentiment_score: int = 0


@dataclass(frozen=True)
class LabelingConfig:
    keyword_min_length: int = 2
    keyword_max_length: int = 12
    keyword_max_count: int = 8
    keyword_must_appear_in_content: bool = True
    keyword_allow_subsequence_match: bool = True
    dimension_max_count: int = 6
    entity_max_count: int = 6
    strict_dimension_whitelist: bool = True
    strict_entity_whitelist: bool = True
    force_neutral_when_irrelevant: bool = True
    # 模型调用/解析失败时是否把"中性"写进库。
    # 默认 False：失败的不写库，转入失败池等重标——写个假中性会把行占掉，
    # 以后既认不出来它是失败的，下游指标还会把它当成一条真实的中性评价。
    write_on_model_failure: bool = False
    overall_sentiment_arbiter: str = "model"
    low_confidence_threshold: float = 0.6

    def validate(self) -> None:
        if self.keyword_min_length < 2:
            raise ConfigError("labeling.keyword_min_length 必须 >= 2（需求：禁止单字关键词）")
        if self.overall_sentiment_arbiter not in {"model", "majority"}:
            raise ConfigError("labeling.overall_sentiment_arbiter 只支持 model|majority")


@dataclass(frozen=True)
class RedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str = ""
    key_prefix: str = "ole"
    socket_timeout: float = 5.0
    visibility_timeout: int = 300
    reclaim_interval: int = 60


@dataclass(frozen=True)
class FailureStoreConfig:
    """失败池：模型标注失败的评论存这里，等后续重标。

    与队列后端解耦——队列可以是 memory，失败池仍然走 Redis，
    否则进程一重启失败记录就没了，"后续再标注"无从谈起。
    """

    enabled: bool = True
    backend: str = "redis"          # redis | memory | none
    redis: RedisConfig = field(default_factory=RedisConfig)
    max_records: int = 200000       # 池子上限，超了从最早的开始丢
    ttl_days: int = 30              # 记录保留天数，0 = 永久
    retry_min_age_seconds: float = 60.0   # 刚失败的先晾一会儿再重试
    retry_batch_size: int = 200

    def validate(self) -> None:
        if self.backend not in {"redis", "memory", "none"}:
            raise ConfigError(
                f"failure_store.backend 只支持 redis|memory|none，当前 {self.backend!r}")
        if self.max_records < 0:
            raise ConfigError("failure_store.max_records 不能为负")
        if self.ttl_days < 0:
            raise ConfigError("failure_store.ttl_days 不能为负")
        if self.enabled and self.backend == "memory":
            # 不是错误，但必须让人知道：进程重启失败记录就没了
            pass


@dataclass(frozen=True)
class QueueConfig:
    backend: str = "memory"
    full_policy: str = "block"
    max_size: int = 100000
    max_attempts: int = 3
    pop_timeout: float = 2.0
    redis: RedisConfig = field(default_factory=RedisConfig)

    def validate(self) -> None:
        if self.backend not in {"memory", "redis"}:
            raise ConfigError(f"queue.backend 只支持 memory|redis，当前 {self.backend!r}")
        if self.full_policy not in {"block", "drop"}:
            raise ConfigError("queue.full_policy 只支持 block|drop")
        if self.max_attempts < 1:
            raise ConfigError("queue.max_attempts 必须 >= 1")


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 4
    flush_batch_size: int = 50
    flush_interval: float = 3.0
    shutdown_timeout: float = 30.0

    def validate(self) -> None:
        if self.concurrency < 1:
            raise ConfigError("worker.concurrency 必须 >= 1")
        if self.flush_batch_size < 1:
            raise ConfigError("worker.flush_batch_size 必须 >= 1")


@dataclass(frozen=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    pool_size: int = 8
    max_retries: int = 3

    def validate(self) -> None:
        if not self.database:
            raise ConfigError("mysql.database 为空")
        if self.pool_size < 1:
            raise ConfigError("mysql.pool_size 必须 >= 1")


@dataclass(frozen=True)
class StorageConfig:
    table: str = "src_opinion_social_work_comment_di"
    key_columns: List[str] = field(default_factory=lambda: ["channel", "scenic_id", "comment_id"])
    required_input_fields: List[str] = field(default_factory=lambda: [
        "channel", "work_id", "scenic_id", "scenic_name", "comment_id", "content",
    ])
    columns: Dict[str, str] = field(default_factory=dict)
    review_column: str = ""
    #: 复核标记各档取值。语义见建表注释：
    #: 0未标注 1人工复核正确 2人工复核错误 3复核成功 4AI标注成功 5AI标注错误 6未人工复核
    #: 1/2/3 由人工填，引擎只写 4/5/6。
    review_flags: Dict[str, int] = field(default_factory=lambda: {
        "ai_success": 4,          # AI 标注成功且置信度达标
        "ai_failed": 5,           # AI 标注失败（模型调用或解析失败）
        "ai_low_confidence": 6,   # AI 标注成功但置信度低，待人工复核
    })
    #: 标注失败时是否只把复核标记置为 ai_failed（不动五个标注字段）。
    #: 打开后失败的行在 SQL 里也能查到（label_review_flag=5），
    #: 与 Redis 失败池互为补充：Redis 管重标，这个字段管盘点。
    write_flag_on_failure: bool = True
    #: 哪些复核标记算"待标注"。默认只有 0（从没标过）。
    #: 想连 AI 标过的一起重标就写 [0, 4]——但注意引擎自己会把成功的写成 4，
    #: 这个集合一旦包含引擎写的值，取数条件就不会随处理收缩，
    #: 跑批脚本会自动改用游标翻页，否则同一批会被无限重标。
    pending_flags: List[int] = field(default_factory=lambda: [0])
    json_ensure_ascii: bool = False

    # 允许出现在 SQL 里的标识符：字母数字下划线，防 SQL 注入
    _IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def engine_written_flags(self) -> List[int]:
        """引擎自己会写进去的标记值（4/6）。

        判断"取数条件会不会随处理收缩"要用它：如果待标注集合里包含这些值，
        标完之后这一行还是匹配条件，抽干循环就永远转不完。
        """
        return [int(self.review_flags[k])
                for k in ("ai_success", "ai_low_confidence")
                if k in self.review_flags]

    def validate(self) -> None:
        if not self._IDENT.match(self.table):
            raise ConfigError(f"storage.table 非法标识符：{self.table!r}")
        if not self.key_columns:
            raise ConfigError("storage.key_columns 不能为空，否则无法定位待更新行")
        for col in list(self.key_columns) + list(self.columns.values()):
            if not self._IDENT.match(col):
                raise ConfigError(f"storage 列名非法标识符：{col!r}")
        if self.review_column and not self._IDENT.match(self.review_column):
            raise ConfigError(f"storage.review_column 非法标识符：{self.review_column!r}")
        # 写回主键必须是调用方约定要传的字段，否则等于配了个永远取不到值的键
        orphan = sorted(set(self.key_columns) - set(self.required_input_fields))
        if orphan:
            raise ConfigError(
                f"storage.key_columns 中的 {orphan} 未出现在 required_input_fields 里，"
                f"调用方不会传这些字段，写回将永远匹配不到行"
            )
        required = {"sentiment_label", "sentiment_score", "dimension_tags", "entity_tags", "keyword_tags"}
        missing = required - set(self.columns)
        if missing:
            raise ConfigError(f"storage.columns 缺少映射：{sorted(missing)}")

        need_flags = {"ai_success", "ai_failed", "ai_low_confidence"}
        missing_flags = need_flags - set(self.review_flags)
        if missing_flags:
            raise ConfigError(f"storage.review_flags 缺少档位：{sorted(missing_flags)}")
        for name, value in self.review_flags.items():
            if not isinstance(value, int):
                raise ConfigError(f"storage.review_flags.{name} 必须是整数")
        if not self.pending_flags:
            raise ConfigError("storage.pending_flags 不能为空，否则永远取不到待标注数据")
        for value in self.pending_flags:
            if not isinstance(value, int):
                raise ConfigError(f"storage.pending_flags 必须都是整数，发现 {value!r}")
        if self.write_flag_on_failure and not self.review_column:
            raise ConfigError(
                "storage.write_flag_on_failure=true 需要同时配置 storage.review_column")


@dataclass(frozen=True)
class AppConfig:
    name: str = "opinion_labeling_engine"
    env: str = "dev"
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    labeling: LabelingConfig = field(default_factory=LabelingConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    failure_store: FailureStoreConfig = field(default_factory=FailureStoreConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    mysql: MySQLConfig = field(default_factory=MySQLConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    taxonomy_path: Path = DEFAULT_TAXONOMY_PATH
    config_path: Path = DEFAULT_CONFIG_PATH

    def validate(self, *, require_db: bool = True) -> None:
        """启动期整体校验。

        Args:
            require_db: 离线模式（只出 CSV / 只跑清洗）时置 False，跳过库连接校验。
        """
        self.llm.validate()
        self.labeling.validate()
        self.queue.validate()
        self.failure_store.validate()
        self.worker.validate()
        self.storage.validate()
        if require_db:
            self.mysql.validate()
        if self.worker.concurrency > self.mysql.pool_size:
            # 不是致命错误，但会造成 worker 抢连接，明确提示
            raise ConfigError(
                f"mysql.pool_size({self.mysql.pool_size}) 小于 worker.concurrency"
                f"({self.worker.concurrency})，会造成写库排队，请调大连接池"
            )


def _build(cls, data: Dict[str, Any], **extra):
    """按 dataclass 字段名过滤未知键，避免 YAML 里多写一行就崩。"""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    payload = {k: v for k, v in data.items() if k in known}
    payload.update(extra)
    return cls(**payload)


def load_config(
    path: Optional[os.PathLike | str] = None,
    *,
    taxonomy_path: Optional[os.PathLike | str] = None,
    validate: bool = True,
    require_db: bool = True,
) -> AppConfig:
    """读取并校验配置文件。

    Args:
        path: config.yaml 路径，默认 ``<project>/config/config.yaml``。
        taxonomy_path: taxonomy.yaml 路径，默认 ``<project>/config/taxonomy.yaml``。
        validate: 是否执行启动期校验。
        require_db: 是否要求数据库配置完整。

    Raises:
        ConfigError: 文件缺失或配置非法。
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"配置文件不存在：{cfg_path}")

    with cfg_path.open("r", encoding="utf-8-sig") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是字典：{cfg_path}")
    raw = _interpolate(raw)

    queue_raw = _section(raw, "queue")
    redis_cfg = _build(RedisConfig, _section(queue_raw, "redis"))
    queue_cfg = _build(QueueConfig, queue_raw, redis=redis_cfg)

    fs_raw = _section(raw, "failure_store")
    # 失败池的 redis 配置缺省沿用队列那份，省得同一套连接信息写两遍
    fs_redis_raw = _section(fs_raw, "redis")
    fs_redis = _build(RedisConfig, {**_section(queue_raw, "redis"), **fs_redis_raw})
    failure_cfg = _build(FailureStoreConfig, fs_raw, redis=fs_redis)

    app_raw = _section(raw, "app")
    cfg = AppConfig(
        name=app_raw.get("name", "opinion_labeling_engine"),
        env=str(app_raw.get("env", "dev")),
        logging=_build(LoggingConfig, _section(raw, "logging")),
        llm=_build(LLMConfig, _section(raw, "llm")),
        cleaning=_build(CleaningConfig, _section(raw, "cleaning")),
        labeling=_build(LabelingConfig, _section(raw, "labeling")),
        queue=queue_cfg,
        failure_store=failure_cfg,
        worker=_build(WorkerConfig, _section(raw, "worker")),
        mysql=_build(MySQLConfig, _section(raw, "mysql")),
        storage=_build(StorageConfig, _section(raw, "storage")),
        taxonomy_path=Path(taxonomy_path) if taxonomy_path else DEFAULT_TAXONOMY_PATH,
        config_path=cfg_path,
    )
    if validate:
        cfg.validate(require_db=require_db)
    return cfg
