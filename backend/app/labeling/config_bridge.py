"""把 smc 的配置翻译成标注引擎认识的 config.yaml。

为什么要生成而不是直接改引擎那份
--------------------------------
引擎自带的 `config/config.yaml` 全是 `${ENV}` 插值，是它自己的出厂配置。
我们不去动它（vendor 里的东西不改），而是**另生成一份**运行时配置，
用 `LabelingEngine.from_config(路径)` 显式指定。好处：

- 引擎随时可以整包替换，我们这边不用重新打补丁
- MySQL / Redis 直接复用 smc 已经配好的那一套，不用在两个地方各填一遍
- 生成的文件落在 data/ 下，**不进包**（里面有明文口令）

⚠️ 生成的文件含 API Key 和数据库口令，权限设成 0600，且不随包分发。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

import yaml

from ..core.config import Config
from ..core.logging import get_logger
from ..db import tables

logger = get_logger(__name__)

#: 引擎本体所在目录（vendor 里那一份）
ENGINE_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "opinion_labeling_engine"
#: 它自带的标签体系。**不改它**，直接用——二期要做标签维护再说
TAXONOMY_PATH = ENGINE_ROOT / "config" / "taxonomy.yaml"


def engine_importable() -> bool:
    """引擎在不在。不在就整个功能优雅降级，而不是让服务起不来。"""
    return (ENGINE_ROOT / "opinion_labeling_engine" / "__init__.py").exists()


def ensure_on_path() -> None:
    """把引擎目录加进 sys.path。

    放在 vendor/ 而不是 pip 安装，是因为它没发到 PyPI；
    放进 sys.path 而不是拷进 app/，是为了保持"原样引入、随时可换"。
    """
    import sys

    root = str(ENGINE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _ark(config: Config) -> Dict[str, Any]:
    return (config.get("labeling.ark") or {})


#: 引擎自带的配置文件。**它才是各项参数的权威来源**，smc 只覆盖自己必须管的那几项。
ENGINE_CONFIG_PATH = ENGINE_ROOT / "config" / "config.yaml"

#: smc 真正"拥有"的模型参数——也就是系统设置页面上能改的那几个。
#: ⚠️ 这是个**白名单**，不是黑名单，这点很重要：
#:    「AI 标注」页保存时会把整个 labeling 段（含 ark 下所有键）写进
#:    sys_setting 表，之后每次启动都会合并回配置。如果这里按黑名单过滤，
#:    库里一条陈旧的 max_output_tokens=1024 就能一直把引擎的 4096 压下去，
#:    而 config.yaml 和引擎配置文件里都找不到 1024 这个数——查起来极其难受。
#:    白名单保证：不在这张表里的键，无论从哪来，都到不了引擎。
SMC_OWNED_LLM_KEYS = {
    "base_url": "base_url",
    "endpoint": "endpoint",
    "qps": "rate_limit_qps",
    "with_fewshot": "with_fewshot",
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 递归合并进 base 的副本。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def engine_defaults() -> Dict[str, Any]:
    """读引擎自带的 config/config.yaml，原样当底板。

    ⚠️ 为什么必须以它为底，而不是在这边照抄一份：
    照抄的那份会**悄悄落后**。引擎里 llm.max_output_tokens 是 4096
    （注释写明推理模型的思考过程也吃这份额度），我这边手抄时写了 1024，
    结果实跑一直在打「输出被 max_output_tokens=1024 截断，提到 2048 重试」——
    等于把引擎调小了 4 倍，还多花一次重试的钱。
    同样被抄丢的还有 max_output_tokens_cap、extra_body（关思考链能省一半
    输出 token）、fewshot_style、system_url、write_on_model_failure、
    failure_store 的四个参数，一共 9 个键。

    这里不做 ${ENV} 展开——引擎自己的 load_config 会展开，
    原样透传反而让 ARK_API_KEY 这类环境变量在新机器上照常生效。
    """
    try:
        with ENGINE_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("引擎配置根节点不是映射")
        return data
    except Exception as exc:  # noqa: BLE001
        # 读不到就退回空底板：引擎的 dataclass 默认值兜着，不至于起不来
        logger.warning("[标注] 读不到引擎自带配置 %s（%s），"
                       "本次只用 smc 的覆盖项，引擎参数走 dataclass 默认值",
                       ENGINE_CONFIG_PATH, exc)
        return {}


def build_engine_config(config: Config) -> Dict[str, Any]:
    """在引擎自带配置的基础上，覆盖 smc 必须接管的部分。

    smc 接管的只有四类：
      1. 连哪个库、写哪张表（mysql / storage）
      2. 用哪个 Redis（queue.redis / failure_store.redis）
      3. 模型凭据和页面上能改的那几个参数（llm 的白名单）
      4. 日志落在哪

    其余一律**继承引擎的配置文件**——采样温度、输出预算、关键字规则、
    维度白名单这些是标注效果的调参，属于引擎的事，不该由 smc 复述一遍。
    """
    mysql = config.mysql
    redis = config.get("redis") or {}
    ark = _ark(config)
    lab = config.get("labeling") or {}

    backend = str(lab.get("queue_backend") or "memory").strip().lower()
    if backend == "redis" and not redis.get("enabled", True):
        # Redis 关着还配 redis 队列，起来就会连不上。降级并说清楚。
        logger.warning("[标注] 队列配的是 redis 但 redis.enabled=false，降级为 memory")
        backend = "memory"

    # ---- 模型：凭据必写，其余只覆盖白名单里**确实配了**的 ----
    # api_key / model 无论空不空都要显式写出去：引擎配置里的默认值是
    # ${ARK_API_KEY:ark-30981b8f-27f5-43ba-882d}，不覆盖的话没配 Key 也会
    # 解析出一个像模像样的字符串，自检直接绿灯放行，跑起来才 401。
    llm: Dict[str, Any] = {
        "api_key": str(ark.get("api_key") or ""),
        "model": str(ark.get("model") or ""),
    }
    for smc_key, engine_key in SMC_OWNED_LLM_KEYS.items():
        value = ark.get(smc_key)
        if value is None or value == "":
            continue
        llm[engine_key] = value

    redis_block = {
        "host": redis.get("host", "127.0.0.1"),
        "port": int(redis.get("port", 6379)),
        "db": int(redis.get("db", 0)),
        "password": redis.get("password") or "",
        "key_prefix": (redis.get("key_prefix") or "smc") + ":ole",
    }

    overrides: Dict[str, Any] = {
        "app": {"name": "opinion_labeling_engine", "env": "smc"},
        "logging": {
            "level": str(lab.get("log_level") or "INFO"),
            # 日志跟着 smc 的 data/logs 走，别在 vendor 目录里拉屎
            "dir": str(Path(config.get("export.dir") or "./data/exports").parent / "logs"),
            "file": "labeling.log",
            "log_content": bool(lab.get("log_content", False)),
        },
        "llm": llm,
        "labeling": {
            # 页面上能改的只有这一个，其余标注规则继承引擎配置
            "low_confidence_threshold": float(lab.get("low_confidence_threshold", 0.6)),
        },
        "queue": {
            "backend": backend,
            "max_size": int(lab.get("queue_max_size", 100000)),
            "redis": dict(redis_block),
        },
        "failure_store": {
            # 失败池跟队列用同一个 Redis；队列降级成 memory 时它也跟着降，
            # 否则会出现"队列不用 Redis、失败池却在启动时因为连不上而炸"
            "backend": "redis" if backend == "redis" else "memory",
            "redis": dict(redis_block),
        },
        "worker": {
            "concurrency": int(lab.get("worker_concurrency", 4)),
            "flush_batch_size": int(lab.get("flush_batch_size", 50)),
        },
        "mysql": {
            "host": mysql["host"],
            "port": int(mysql["port"]),
            "user": mysql["user"],
            "password": mysql["password"],
            "database": mysql["database"],
            "charset": "utf8mb4",
            # 引擎的配置校验要求 pool_size >= worker.concurrency
            "pool_size": max(int(lab.get("worker_concurrency", 4)),
                             int(lab.get("mysql_pool_size", 8))),
        },
        "storage": {
            # ⚠️ 表名从 smc 的 tables 常量取，不写死：
            # 这个项目做过一次表改名，写死的话改名之后标注会静默写不进去
            "table": tables.COMMENTS,
            "key_columns": ["channel", "scenic_id", "comment_id"],
            "required_input_fields": [
                "channel", "work_id", "scenic_id", "scenic_name",
                "comment_id", "content",
            ],
            "columns": {
                "sentiment_label": "sentiment_label",
                "sentiment_score": "sentiment_score",
                "dimension_tags": "dimension_tags",
                "entity_tags": "entity_tags",
                "keyword_tags": "keyword_tags",
            },
            # ⚠️ 这一列**必须**接上：新版引擎自己维护 AI 标注状态
            #   0未标注 1人工复核正确 2人工复核错误 3复核成功
            #   4AI标注成功 5AI标注错误 6未人工复核
            # 1/2/3 由人工（审核页面）写，4/5/6 由引擎写。
            # 留空的话引擎会跳过整套标记逻辑，审核页面就没法按状态筛。
            "review_column": str(lab.get("review_column") or "label_review_flag"),
            "review_flags": {
                "ai_success": 4,
                "ai_failed": 5,
                "ai_low_confidence": 6,
            },
            # 失败也写标记（5），这样失败的行在 SQL 里能盘点，
            # 和引擎自己的 Redis 失败池互为补充
            "write_flag_on_failure": True,
            # 只标从没标过的（0）。把 4 放进来会导致跑批无限重标——
            # 引擎标成功后写的就是 4，取数条件不会随处理收缩。
            "pending_flags": list(lab.get("pending_flags") or [0]),
            "json_ensure_ascii": False,
        },
    }

    return _deep_merge(engine_defaults(), overrides)


def runtime_config_path(config: Config) -> Path:
    """运行时配置落盘的位置。data/labeling/ 下的其它运行时文件（补标断点）也放这儿。"""
    target = Path(config.get("labeling.runtime_config_path")
                  or "./data/labeling/engine.runtime.yaml")
    if not target.is_absolute():
        # 相对路径按**配置文件所在项目根**算，不是按当前工作目录——
        # 服务可能从任何目录起（run.py 就允许），按 cwd 算会到处拉文件
        src = getattr(config, "source_path", None)
        root = Path(src).resolve().parents[1] if src else Path.cwd()
        target = root / target
    return target


def write_runtime_config(config: Config) -> Path:
    """把生成的配置落盘，返回路径。

    ⚠️ 里面有明文 API Key 和数据库口令，所以：
      - 落在 data/labeling/ 下（打包时 data/ 整个排除）
      - 权限 0600
    """
    target = runtime_config_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_engine_config(config)
    with target.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(target, 0o600)
    except OSError:          # Windows 上 chmod 基本没用，失败也不该拦住启动
        pass
    return target
