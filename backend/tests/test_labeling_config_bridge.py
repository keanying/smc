"""生成给引擎的运行时配置：必须以**引擎自带的 config.yaml** 为准。

实跑打出来的那行警告就是这个问题：

    输出被 max_output_tokens=1024 截断（已用 1025 tokens），提到 2048 后重试；
    长期方案是调大 llm.max_output_tokens

引擎自己配的是 9096（注释写明推理模型的思考过程也吃这份额度），
而 smc 这边在 config_bridge 里手抄了一份、写成 1024——等于把引擎调小了
4 倍，每条还要多花一次重试的钱。同时被抄丢的还有 8 个键。

所以这里锁住两件事：
  1. 引擎配置文件里的值必须原样透传（不是 smc 复述一遍）
  2. smc 只能覆盖白名单里那几项——尤其不能被 sys_setting 表里的陈旧值污染
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Config, load_config          # noqa: E402
from app.labeling import config_bridge                    # noqa: E402


@pytest.fixture(scope="module")
def engine_yaml():
    with config_bridge.ENGINE_CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _build(**labeling_override):
    cfg = load_config()
    if labeling_override:
        lab = dict(cfg.get("labeling") or {})
        lab.update(labeling_override)
        cfg.set("labeling", lab)
    return config_bridge.build_engine_config(cfg)


def test_engine_tuning_is_passed_through(engine_yaml):
    """引擎配的输出预算必须原样到达引擎，不能被 smc 改小。"""
    built = _build()
    assert built["llm"]["max_output_tokens"] == engine_yaml["llm"]["max_output_tokens"]
    assert built["llm"]["max_output_tokens"] == 9096, "1024 那个老毛病回来了"
    assert built["llm"]["max_output_tokens_cap"] == engine_yaml["llm"]["max_output_tokens_cap"]


@pytest.mark.parametrize("section,key", [
    ("llm", "max_output_tokens"),
    ("llm", "max_output_tokens_cap"),
    ("llm", "extra_body"),
    ("llm", "fewshot_style"),
    ("llm", "system_role"),
    ("llm", "temperature"),
    ("llm", "top_p"),
    ("labeling", "write_on_model_failure"),
    ("labeling", "keyword_max_count"),
    ("cleaning", "max_content_length"),
    ("failure_store", "max_records"),
    ("failure_store", "ttl_days"),
    ("failure_store", "retry_min_age_seconds"),
    ("failure_store", "retry_batch_size"),
])
def test_no_engine_key_is_silently_dropped(engine_yaml, section, key):
    """引擎配置里的每一项都要出现在生成结果里，值也要一致。

    以前这 14 项里有 9 项直接消失了——不是报错，是安静地用了别的值。
    """
    built = _build()
    assert key in built.get(section, {}), f"{section}.{key} 被抄丢了"
    assert built[section][key] == engine_yaml[section][key], \
        f"{section}.{key} 被 smc 改成了 {built[section][key]!r}，" \
        f"引擎配的是 {engine_yaml[section][key]!r}"


def test_stale_db_setting_cannot_override_engine():
    """⚠️ 核心用例：sys_setting 表里的陈旧值不能压过引擎。

    「AI 标注」页点保存时，前端会把整个 labeling 段（含 ark 下所有键）
    发回后端存进 sys_setting，启动时再合并回配置。所以只要有人在旧版本上
    点过一次保存，库里就永远躺着一份 ark.max_output_tokens=1024，
    而 config.yaml 和引擎配置文件里都**找不到 1024 这个数**——
    这种问题查起来能耗掉一整天。

    白名单机制保证：不在 SMC_OWNED_LLM_KEYS 里的键，无论从哪来都到不了引擎。
    """
    cfg = load_config()
    lab = dict(cfg.get("labeling") or {})
    ark = dict(lab.get("ark") or {})
    # 模拟库里那份陈旧覆盖
    ark.update({"max_output_tokens": 1024, "temperature": 0.9,
                "max_retries": 99, "read_timeout": 1})
    lab["ark"] = ark
    cfg.set("labeling", lab)

    built = config_bridge.build_engine_config(cfg)

    assert built["llm"]["max_output_tokens"] == 9096, "库里的 1024 又把引擎压下去了"
    assert built["llm"]["temperature"] == 0.01
    assert built["llm"]["max_retries"] == 3
    assert built["llm"]["read_timeout"] == 60


def test_smc_owned_keys_still_win():
    """白名单里的项（页面上能改的）必须能覆盖引擎。"""
    cfg = load_config()
    lab = dict(cfg.get("labeling") or {})
    lab["ark"] = {**(lab.get("ark") or {}), "qps": 3, "with_fewshot": False,
                  "api_key": "k" * 36, "model": "my-model"}
    lab["worker_concurrency"] = 7
    lab["low_confidence_threshold"] = 0.42
    cfg.set("labeling", lab)

    built = config_bridge.build_engine_config(cfg)

    assert built["llm"]["rate_limit_qps"] == 3
    assert built["llm"]["with_fewshot"] is False
    assert built["llm"]["model"] == "my-model"
    assert built["worker"]["concurrency"] == 7
    assert built["labeling"]["low_confidence_threshold"] == 0.42


def test_credentials_always_written_even_when_empty():
    """没配 Key 时必须写空串，不能让引擎的占位默认值蒙混过关。

    引擎配置里是 ${ARK_API_KEY:ark-30981b8f-27f5-43ba-882d}——
    有个像模像样的默认值。不显式覆盖的话，没配 Key 也会解析出一个非空
    字符串，自检"配置校验"直接绿灯，真跑起来才 401。
    """
    cfg = load_config()
    lab = dict(cfg.get("labeling") or {})
    lab["ark"] = {**(lab.get("ark") or {}), "api_key": "", "model": ""}
    cfg.set("labeling", lab)

    built = config_bridge.build_engine_config(cfg)

    assert built["llm"]["api_key"] == ""
    assert built["llm"]["model"] == ""
    assert "${" not in str(built["llm"]["api_key"])


def test_storage_and_db_are_still_owned_by_smc():
    """smc 必须接管的那几项不能被"继承引擎配置"带跑偏。"""
    built = _build()
    cfg = load_config()
    assert built["mysql"]["database"] == cfg.get("mysql.database")
    assert built["storage"]["review_column"] == "label_review_flag"
    assert built["storage"]["review_flags"] == {"ai_success": 4, "ai_failed": 5,
                                                "ai_low_confidence": 6}
    assert built["storage"]["pending_flags"] == [0]
    assert "${" not in str(built["mysql"]["host"])


def test_failure_store_follows_queue_backend():
    """队列降级成 memory 时失败池要跟着降。

    这两个是**不同**的组件：队列切 memory 不代表失败池也切了，
    而引擎对 redis 失败池是**启动期直接炸**。之前就出现过
    自检 10 项全绿、ensure_started() 却抛「失败池连不上 Redis」。
    """
    built = _build(queue_backend="memory")
    assert built["queue"]["backend"] == "memory"
    assert built["failure_store"]["backend"] == "memory"
