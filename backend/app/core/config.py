"""统一配置：DEFAULT_CONFIG < config.yaml < 环境变量 三层覆盖。

环境变量命名：SMC_<段>_<键>，嵌套段用双下划线，例如
    SMC_MYSQL_PASSWORD=xxx
    SMC_PROXY_ENABLED=true
    SMC_PLATFORMS__CTRIP__MAX_PAGES=50
类型按 DEFAULT_CONFIG 中同位置的默认值推断，解析失败直接抛错而不是静默忽略。
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# backend/app/core/config.py -> core -> app -> backend -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"

DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "cors_origins": ["*"],
        "log_level": "INFO",
        "data_dir": "./data",
    },
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "scenic_media",
        "charset": "utf8mb4",
        "pool_min": 2,
        "pool_max": 20,
        # 连接空闲多久就回收重建（秒）。要明显小于 MySQL 的 wait_timeout
        # （默认 28800），否则被服务端掐掉的连接会在下次查询时报错重连。
        "pool_recycle_seconds": 3600,
        "auto_migrate": True,
    },
    "redis": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 6379,
        "db": 0,
        "username": "",
        "password": "",
        "ssl": False,
        "protocol": 2,
        "key_prefix": "smc",
        "watermark_ttl_days": 90,
        "dedupe_ttl_days": 30,
    },
    "proxy": {
        "enabled": False,
        "provider": "kuaidaili",
        "auth_mode": "token",
        "secret_id": "",
        "secret_key": "",
        "username": "",
        "password": "",
        "min_ttl_seconds": 20 * 60,
        "max_ttl_seconds": 30 * 60,
        "safety_buffer_seconds": 30,
        "fetch_retries": 5,
        "validate_on_fetch": True,
        "api_timeout_seconds": 10,
        "rotate_every_n_requests": 0,
        # channel=每个平台各持有一个 IP，互不牵连；global=全平台共用一个 IP，省提取次数
        "pool_scope": "channel",
        "per_platform": {},
    },
    "browser": {
        # 采集与「采集 Cookie」用不用无头
        "headless": True,
        # 交互式登录窗口用不用无头。默认有头：无头容易被风控拦、二维码可能渲染不出来。
        # 服务跑在自己电脑上、不想被弹窗打扰时可以打开——画面通过推流照样能看到。
        "headless_login": False,
        # 留空则用 Playwright 自带的 Chromium；
        # 指定路径可复用系统已装的 Chrome，或适配沙箱里预置的浏览器
        "executable_path": "",
        "profiles_dir": "./data/browser_profiles",
        "stealth_js": "./backend/libs/stealth.min.js",
        "default_timeout_ms": 60000,
        "slow_mo_ms": 0,
        "login_timeout_seconds": 300,
        "session_check_interval_seconds": 600,
        # 后台每隔多少分钟自动跑一遍"采集 Cookie"，把各账号的登录态刷新到最新。
        # 0 = 关闭，只在采集时按需刷新。
        "auto_refresh_cookie_minutes": 0,
        # 拟人模式下，一个账号的浏览器同时只能服务一个任务。
        # 账号全被占用时后来的任务会**排队等**（而不是被跳过），
        # 这是最多等多久（秒）。等超了这一轮报错，好过无限期挂着——
        # 真卡死通常是有人开着登录窗口忘了关，需要人来处理。
        "slot_wait_timeout_seconds": 1800,
    },
    "crawl": {
        "default_keyword_limit": 100,
        "default_max_works": 100,
        "default_max_comments_per_work": 500,
        # 携程/同程没有作品，只有景区点评，这是一个 POI 默认采多少条点评
        "default_max_comments_per_poi": 1000,
        "collect_comments": True,
        "enable_sub_comments": True,
        "max_comment_level": 3,
        # 新建任务默认的采集方式：api / hybrid / human。
        #   api    只走接口，失败直接报错（想确认接口坏没坏时用）
        #   hybrid 先走接口，被风控或拿不到数据时自动换成拟人
        #   human  直接拟人：浏览器模拟真人操作，慢但最不容易被拦
        # 只有抖音/快手/小红书写了拟人采集器，其它平台恒为 api。
        "collect_engine": "hybrid",
        # 当天已经采成功的作品，重跑时跳过它的评论（作品本身还是会更新）。
        # 关键字重试、或者同一天里任务被再跑一次时省下的就是这部分——
        # 拟人模式下每条作品要真实点开、翻评论，几百条就是几个小时。
        "skip_works_collected_today": True,
        "request_interval_seconds": 1.0,
        "page_interval_seconds": 1.0,
        "request_timeout_seconds": 25,
        "max_retries": 3,
        "concurrency": 2,
        # 任务看门狗：运行超过多少分钟就取消（0 = 不限制）。
        # 平台风控把连接挂住时任务会永远卡在一个请求上，占着并发槽位。
        "task_timeout_minutes": 360,
        # 单个关键字的时间预算（分钟），0 = 不限。到点换下一个关键字。
        "keyword_budget_minutes": 0,
        # ---------------- 采集水印（去重）----------------
        # 两级水印都落 Redis，解决两件不同的事：
        #   关键字水印 —— 任务被反复重启时，别把刚采完的关键字再跑一遍
        #   作品水印   —— 同一条作品被多个关键字命中是常态（实测重合率三成），
        #                 命中就跳过，连评论都不用翻
        # ⚠️ Redis 连不上时会退化成进程内缓存，水印只在本进程有效——
        #    这是有意的降级：宁可重复采一点，也不能因为 Redis 挂了就不采。
        "dedup": {
            "enabled": True,
            "keyword_enabled": True,
            "work_enabled": True,
            "keyword_ttl_seconds": 4 * 3600,        # 4 小时
            "work_ttl_seconds": 3 * 24 * 3600,      # 3 天
        },
        # 采集节奏（频率控制），按平台覆盖。详见 app/core/pacing.py。
        #   work_seconds   采完一条作品，停多久再看下一条
        #   scroll_seconds 每次滚动之间
        #   action_seconds 点击/输入这类小动作之间
        # 三个档位分开是因为量级差一个数量级：混成一个数，
        # 要么滚动慢得离谱，要么作品之间快得不像真人。
        # 这里留空 = 用 pacing.py 里的出厂默认（各平台不同）。
        "pace": {},
        # 单账号配额与冷却：别把一个账号用到被封。
        # 平台判"机器号"看的不只是请求间隔（那个归 crawl.pace 管），
        # 还看一个账号的**总量和连续在线时长**。0 = 该项不限制。
        # 留空的项走 core/account_quota.py 里的各平台出厂默认
        # （小红书最保守：150 条/天、45 分钟/次、冷却 180 分钟）。
        "account_quota": {
            "enabled": True,
            # 下面三项留空表示"按平台出厂默认"；填了就全平台统一用这个值
            # "daily_works": 300,
            # "session_minutes": 90,
            # "cooldown_minutes": 120,
            "per_channel": {},
        },
    },
    # ---------------- 通知（飞书机器人）----------------
    # 采集卡在**需要人**的地方时把人叫来：账号失效、二次验证、
    # 要重新登录、以及采集报错。这类问题机器解决不了。
    "notify": {
        "enabled": False,
        # 飞书群机器人的 webhook 地址。⚠️ 这个地址等于一张进群门票，
        # 别提交进仓库，用环境变量 SMC_NOTIFY_FEISHU_WEBHOOK。
        "feishu_webhook": "",
        # 通知正文里的远程登录信息（向日葵）
        "remote_account": "",
        "remote_password": "",
        # 同一个平台的同一类问题，多久内只发一次。
        # 不设的话一轮采集能刷出上百条一模一样的消息，真出事反而被淹掉。
        "cooldown_seconds": 600,
        # 需要登录/验证时，是否**暂停**该平台的采集等人处理。
        # 关掉的话会像以前一样跳过这个平台继续跑（但那样通知发了也没人处理）。
        "pause_on_login_required": True,
        # 暂停后最多等多久（分钟）。到点还没人处理就放弃这个平台。
        "pause_timeout_minutes": 30,
    },

    # ---------------- 舆情标注引擎（vendor/opinion_labeling_engine）----------
    # 引擎本体不改，这里只是把它要的东西用 smc 的口径配一遍。
    "labeling": {
        # 边采边标总开关。开之前会自检（模型配置、数据库联调、表结构），
        # **有任何一项不过就不标**，而不是偷偷跳过。
        "enabled": False,
        # 队列：memory = 进程内（重启丢任务，适合单机小量）；redis = 生产
        "queue_backend": "memory",
        "worker_concurrency": 4,
        "flush_batch_size": 50,
        "mysql_pool_size": 8,
        "queue_max_size": 100000,
        # 置信度低于它的会被标记为待复核，在标注审核页面能筛出来
        "low_confidence_threshold": 0.6,
        # AI标注/人工复核标记列。表里已经有这一列（见 schema.sql）：
        #   0未标注 1人工复核正确 2人工复核错误 3复核成功
        #   4AI标注成功 5AI标注错误 6未人工复核
        # 1/2/3 由审核页面写，4/5/6 由引擎写。留空 = 关掉整套标记逻辑。
        "review_column": "label_review_flag",
        # 哪些标记算"待标注"。默认 [0]（从没标过的）。
        # ⚠️ 别把 4 放进来：引擎标成功写的就是 4，跑批会无限重标。
        "pending_flags": [0],
        "log_level": "INFO",
        "log_content": False,
        # 生成给引擎用的运行时配置落在哪（含明文口令，不进包）
        "runtime_config_path": "./data/labeling/engine.runtime.yaml",
        "ark": {
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "endpoint": "/responses",
            # ⚠️ 别把 Key 写进 config.yaml 提交出去，用环境变量：
            #    SMC_LABELING__ARK__API_KEY
            "api_key": "",
            "model": "",
            # 全局限流：每秒最多几次模型调用
            "qps": 8,
            # ⚠️ 这里**只放系统设置页面上能改的那几项**。
            #    采样温度、输出预算（max_output_tokens）、超时、重试次数
            #    这些一律不在这儿写——它们归引擎的
            #    backend/vendor/opinion_labeling_engine/config/config.yaml 管，
            #    要调去改那个文件。
            #    以前这里抄了一份，把 max_output_tokens 写成 1024，
            #    而引擎自己是 4096，结果实跑一直在打
            #    「输出被 max_output_tokens=1024 截断，提到 2048 后重试」——
            #    等于把引擎调小了 4 倍，还每条多花一次重试的钱。
            #    config_bridge.SMC_OWNED_LLM_KEYS 是那份白名单。
            # few-shot 提高准确率，代价约 1.5k tokens/条
            "with_fewshot": True,
        },
    },
    "export": {
        "dir": "./data/exports",
        "encoding": "utf-8-sig",
        "chunk_size": 5000,
    },
    "scheduler": {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "max_running_tasks": 3,
        "misfire_grace_seconds": 300,
    },
    # 大模型调用（评论分析等）。
    # ⚠️ api_key / base_url 特意不写默认值，也不建议写进 config.yaml——
    # 这个文件是要提交进仓库的。用环境变量传：
    #     SMC_API_API_KEY=sk-xxx
    #     SMC_API_BASE_URL=https://...
    # 留空就在这里报错，比"配置里明文躺着一把密钥"安全。
    "api": {
        "model": "deepseek-v4-flash-260425",
        "api_key": "",
        "base_url": "",
        # 0 = 每次输出都一样（做结构化抽取用这个）；越高越发散
        "temperature": 0.3,
        # 单次回复的 token 上限
        "max_tokens": 2000,
        "timeout_seconds": 60,
    },
    # 批处理（把评论成批喂给模型时用）
    "process": {
        # 一批塞多少条。太大容易超 max_tokens，太小则请求次数翻倍
        "batch_size": 10,
        # 并发线程数。受限于上游的 QPS，调高之前先确认配额
        "max_workers": 7,
        "max_retries": 3,
        # 重试间隔（秒），配合退避
        "retry_delay": 2,
    },
    "platforms": {
        "douyin": {"enabled": True, "need_login": True},
        "kuaishou": {"enabled": True, "need_login": True},
        "weibo": {"enabled": True, "need_login": True},
        "xiaohongshu": {"enabled": True, "need_login": True},
        "ctrip": {"enabled": True, "need_login": False, "page_size": 10,
                  "max_pages": 300, "sort_type": 1},
        "tongcheng": {"enabled": True, "need_login": False, "page_size": 10,
                      "max_pages": 300, "lab_id": 6, "sort": 0},
    },
}

_ENV_PREFIX = "SMC_"
_lock = threading.Lock()
_cache: Optional["Config"] = None


def _cast_like(default: Any, raw: str) -> Any:
    """按默认值的类型解析环境变量字符串。"""
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"无法解析为布尔值：{raw}")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并；两边都是 dict 才递归，其余类型整体替换。"""
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _env_name_candidates(path_part: str) -> List[str]:
    """在真实配置树里找出这个变量**本来想写成**哪一个。

    为什么要查树而不是直接猜：拆分点是猜不出来的。
    `LABELING_ARK_API_KEY` 既可能是 labeling.ark.api_key，
    也可能是 labeling.ark_api_key 或 labeling.ark.api.key——
    只有把 DEFAULT_CONFIG 拉平、拿"段名用下划线拼起来"去比，
    才能给出**确实存在**的那一个，而不是一个同样错的建议。
    """
    target = path_part.lower()
    found: List[str] = []

    def walk(node: Any, segs: List[str]) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = segs + [str(key).lower()]
            if "_".join(path) == target:
                found.append(_ENV_PREFIX + "__".join(p.upper() for p in path))
            walk(value, path)

    walk(DEFAULT_CONFIG, [])
    return found


def _warn_env_ignored(env_name: str, segments: List[str]) -> None:
    """匹配不上任何配置项的 SMC_ 变量，必须喊一声。

    ⚠️ 这是**安静失效**最典型的一种：`SMC_LABELING_ARK_API_KEY` 看着完全合理，
    实际会被拆成 `labeling.ark_api_key`（真正的路径是 `labeling.ark.api_key`），
    然后被 continue 掉——变量设了、日志一声不吭、程序照跑，
    只是那个 Key 压根没进配置。查这种问题要花掉一整个下午。
    嵌套超过一层必须用双下划线：SMC_LABELING__ARK__API_KEY。
    """
    hint = ""
    candidates = _env_name_candidates(env_name[len(_ENV_PREFIX):])
    if candidates:
        hint = f"。你要找的应该是 {' 或 '.join(candidates)}"
    logger_ = logging.getLogger(__name__)
    logger_.warning(
        "环境变量 %s 没有对应的配置项（解析成 %s），已忽略%s",
        env_name, ".".join(segments), hint,
    )


def _apply_env(config: Dict[str, Any]) -> List[str]:
    """把 SMC_ 开头的环境变量覆盖进配置，返回生效的变量名列表。"""
    applied: List[str] = []
    for env_name, raw in sorted(os.environ.items()):
        if not env_name.startswith(_ENV_PREFIX):
            continue
        path_part = env_name[len(_ENV_PREFIX):]
        if path_part in ("CONFIG",):
            continue
        if "__" in path_part:
            segments = [seg.lower() for seg in path_part.split("__")]
        else:
            section, _, key = path_part.partition("_")
            if not key:
                continue
            segments = [section.lower(), key.lower()]

        cursor: Any = config
        default_cursor: Any = DEFAULT_CONFIG
        ok = True
        for seg in segments[:-1]:
            if not isinstance(cursor, dict) or seg not in cursor:
                ok = False
                break
            cursor = cursor[seg]
            default_cursor = default_cursor.get(seg) if isinstance(default_cursor, dict) else None
        if not ok or not isinstance(cursor, dict):
            _warn_env_ignored(env_name, segments)
            continue

        leaf = segments[-1]
        if leaf not in cursor:
            _warn_env_ignored(env_name, segments)
            continue
        default_value = default_cursor.get(leaf) if isinstance(default_cursor, dict) else None
        if default_value is None:
            default_value = cursor.get(leaf)
        try:
            cursor[leaf] = _cast_like(default_value, raw)
        except ValueError as exc:
            raise ValueError(f"环境变量 {env_name} 的值无法解析：{raw}（{exc}）") from exc
        applied.append(env_name)
    return applied


class Config:
    """点号取值的配置对象：config.get('mysql.host') 或 config.mysql['host']。"""

    def __init__(self, data: Dict[str, Any], source_path: Optional[Path] = None):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "source_path", source_path)
        self._resolve_paths()

    def get(self, dotted_key: str, default: Any = None) -> Any:
        cursor: Any = self._data
        for seg in dotted_key.split("."):
            if not isinstance(cursor, dict) or seg not in cursor:
                return default
            cursor = cursor[seg]
        return cursor

    def set(self, dotted_key: str, value: Any) -> None:
        section, _, leaf = dotted_key.rpartition(".")
        target = self.get(section) if section else self._data
        if not isinstance(target, dict):
            raise KeyError(f"配置路径不存在：{dotted_key}")
        target[leaf] = value

    def __getattr__(self, item: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if item in data:
            return data[item]
        raise AttributeError(f"配置中没有段：{item}")

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def redacted(self) -> Dict[str, Any]:
        """给前端/日志看的版本：抹掉所有密钥。"""
        # ⚠️ api_key 必须在这个集合里：标注引擎的方舟 Key 就叫这个名字，
        # 漏掉的话它会随 /api/settings 明文发到前端，再进浏览器缓存和截图
        secret_keys = {"password", "secret_key", "secret_id", "signature",
                       "token", "api_key", "apikey", "access_key",
                       # 飞书 webhook 等于一张进群门票，谁拿到都能往群里发
                       "feishu_webhook", "webhook", "remote_password"}

        def _walk(node: Any) -> Any:
            if isinstance(node, dict):
                return {
                    k: ("***" if k.lower() in secret_keys and v else _walk(v))
                    for k, v in node.items()
                }
            if isinstance(node, list):
                return [_walk(item) for item in node]
            return node

        return _walk(self.as_dict())

    def _resolve_paths(self) -> None:
        """把相对路径统一解析为基于项目根的绝对路径。"""
        for dotted in ("server.data_dir", "browser.profiles_dir",
                       "browser.stealth_js", "export.dir"):
            value = self.get(dotted)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = (PROJECT_ROOT / path).resolve()
            self.set(dotted, str(path))

    def ensure_dirs(self) -> None:
        for dotted in ("server.data_dir", "browser.profiles_dir", "export.dir"):
            Path(self.get(dotted)).mkdir(parents=True, exist_ok=True)

    def platform(self, channel: str) -> Dict[str, Any]:
        return self.get(f"platforms.{channel}", {}) or {}

    def proxy_for(self, channel: str) -> Dict[str, Any]:
        """平台级代理配置 = 全局 proxy 段 + per_platform[channel] 覆盖。"""
        merged = copy.deepcopy(self.get("proxy", {}) or {})
        override = (merged.pop("per_platform", {}) or {}).get(channel, {})
        return _deep_merge(merged, override or {})


def validate(config: Config) -> None:
    proxy = config.get("proxy", {}) or {}
    if proxy.get("enabled"):
        if proxy.get("auth_mode") not in ("token", "plain"):
            raise ValueError("proxy.auth_mode 只能是 token 或 plain")
        missing = [k for k in ("secret_id", "secret_key") if not proxy.get(k)]
        if missing:
            raise ValueError(
                "代理已启用但缺少配置：" + "、".join(f"proxy.{k}" for k in missing)
                + "（可用环境变量 SMC_PROXY_SECRET_ID / SMC_PROXY_SECRET_KEY 注入）"
            )
        if bool(proxy.get("username")) != bool(proxy.get("password")):
            raise ValueError("proxy.username 与 proxy.password 必须同时填写或同时留空（白名单模式）")
        if proxy["min_ttl_seconds"] > proxy["max_ttl_seconds"]:
            raise ValueError("proxy.min_ttl_seconds 不能大于 proxy.max_ttl_seconds")

    if not config.get("mysql.database"):
        raise ValueError("mysql.database 不能为空")
    if int(config.get("crawl.max_comment_level", 1)) < 1:
        raise ValueError("crawl.max_comment_level 必须 >= 1")
    if int(config.get("crawl.default_keyword_limit", 1)) < 1:
        raise ValueError("crawl.default_keyword_limit 必须 >= 1")


def load_config(path: str | Path | None = None, use_cache: bool = True) -> Config:
    global _cache
    with _lock:
        if use_cache and _cache is not None and path is None:
            return _cache

        data = copy.deepcopy(DEFAULT_CONFIG)
        config_path = Path(path or os.getenv("SMC_CONFIG") or CONFIG_DIR / "config.yaml")
        source: Optional[Path] = None
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as fh:
                file_data = yaml.safe_load(fh) or {}
            if not isinstance(file_data, dict):
                raise ValueError(f"配置文件根节点必须是映射：{config_path}")
            unknown = [k for k in file_data if k not in DEFAULT_CONFIG]
            if unknown:
                raise ValueError(f"配置文件包含未知配置段：{'、'.join(unknown)}")
            _deep_merge(data, file_data)
            source = config_path

        _apply_env(data)
        config = Config(data, source)
        validate(config)
        if path is None:
            _cache = config
        return config


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None
