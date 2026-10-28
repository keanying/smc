"""指标计算的配置：页面上维护的那一整份（景区、参数、定时、推送、推送表与字段）。

存在系统设置表里，键是 `metrics.config`（一份 JSON）。总开关不在这里，
在系统设置的 `metrics.enabled`——设置页打开它，菜单里才出现「指标计算」。

这里只做三件事：给默认值、校验、把推送令牌打码。不 import 引擎（它要 pandas），
引擎只在子进程里跑（见 worker.py）。
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_DIR = Path(__file__).resolve().parents[2] / "vendor" / "opinion_metric_engine"
SQL_DIR = ENGINE_DIR / "sql"

#: 设置表里存这份配置的键
SETTING_KEY = "metrics.config"

#: 引擎产出的六张表：简写 → (表名, 中文名)。顺序就是页面上的顺序
TABLES: List[Dict[str, str]] = [
    {"key": "core", "table": "ads_trf_social_opinion_comment_core_di", "label": "核心指标"},
    {"key": "platform", "table": "ads_trf_social_opinion_comment_platform_di", "label": "平台分布"},
    {"key": "dimension", "table": "ads_trf_social_opinion_comment_dimension_score_di",
     "label": "维度得分"},
    {"key": "content", "table": "ads_trf_social_opinion_comment_content_di", "label": "关键词云"},
    {"key": "drill", "table": "ads_trf_social_opinion_drill_analysis_di", "label": "下钻明细"},
    {"key": "macro", "table": "ads_trf_social_opinion_macro_gran_metric_di", "label": "大盘 KPI"},
]
TABLE_NAMES = [t["table"] for t in TABLES]

#: 推送时各表的默认主键（pkId）——对齐下游数据网关（quickBiData/dataSync/crossDomainSync）
#: 实际收的口径，来自上游线上配置和客户给的报文样例。和引擎自己的 DEFAULT_PUSH_PK 不一样
#: （引擎的下钻表默认只用 detail_uk），以这里为准；页面上每张表还能再改。
DEFAULT_PUSH_PK: Dict[str, List[str]] = {
    "core": ["scenic_spot_code", "publish_time", "travel_date"],
    "platform": ["scenic_spot_code", "platform_code", "publish_time", "travel_date"],
    "dimension": ["scenic_spot_code", "dimension_level1", "dimension_level2", "dimension_level3",
                  "publish_time", "travel_date"],
    "content": ["scenic_spot_code", "emotion_type", "emotion_word", "publish_time", "travel_date"],
    "drill": ["scenic_id", "emotion_word", "emotion_type", "channel", "work_id", "comment_id",
              "publish_time", "travel_date"],
    "macro": ["scenic_id", "channel", "time_granularity", "publish_time", "travel_date"],
}

#: 建表语句文件（引擎自带）。ads_word_detail_ddl.sql 是下钻表的旧版本，不用
DDL_FILES = ["ads_ddl_reference.sql", "ads_drill_analysis_ddl.sql", "ads_macro_gran_metric_ddl.sql"]

DEFAULT_CONFIG: Dict[str, Any] = {
    # 要算的景区（景区 ID）。空 = 取数区间里有评论的全部景区
    "scenics": [],
    # 计算参数：和引擎的 EtlSettings 同名，含义见 vendor/opinion_metric_engine/README.md
    "params": {
        "lookback_days": 180,          # 往前读多少天评论参与计算
        "rerun_days": 3,               # 顺带重跑目标日期之前几天（评论会晚到）
        "backfill_mode": "fill",       # 某渠道某天没评论：fill 复制最近一天的明细 / off 记 0
        "backfill_lookback": 5,        # 补明细时最多往前找几天
        "enable_drill_analysis": True,  # 产出下钻明细表
        "drill_keyword_filter": False,  # 下钻表只收原文含本景区采集关键字的评论
        "drill_mask_content": True,    # 下钻表评论摘要只露命中的关键词
        "enable_macro_metric": True,   # 产出大盘 KPI 表
        "content_top_words": 0,        # 关键词表每天每情感只留热度前 N 个词，0 = 全量
        "score_weights": {"positive": 1.0, "neutral": 0.9, "negative": 0.5},
        "write_db": True,              # 写 ADS 表；关掉 = 只算 + 推送
        "ads_database": "",            # ADS 表所在的库，空 = 和采集库同一个
    },
    # 定时：cron（5 段：分 时 日 月 周），每次算「昨天」
    "schedule": {"enabled": False, "cron": "30 2 * * *"},
    # 推送：算完、落库之后以 HTTP 推给下游
    "push": {
        "enabled": False,
        "url": "",
        # 下游网关的鉴权是 `xpftkey: pfk_xxx`——自定义请求头、不带前缀。
        # 换成 `Authorization: Bearer xxx` 这类就改这两项
        "token_header": "xpftkey",
        "token_prefix": "",
        "token": "",                    # 密钥，不回显
        "body_fields": {"table": "tableName", "pk": "pkId", "data": "data"},
        "extra_fields": {},
        "batch_size": 500,
        "timeout": 30,
        "retries": 2,
        "success_field": "status",      # 响应体里表示成败的字段；空 = 只看 HTTP 状态码
        "dry_run": False,               # 只组报文不真发
    },
    # 推送表：每张表推不推、推给下游叫什么、主键、推哪些字段（及改名）
    #   fields 空 = 全部字段原名推送；否则只推列出来的，按列出的顺序，name 是下游字段名
    "tables": {t["key"]: {"push": True, "push_name": "", "pk": [], "fields": []}
               for t in TABLES},
}

_CRON_FIELDS = 5
_URL_RE = re.compile(r"^https?://[^\s/]+", re.I)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SECRET_MASK = "***"


def default_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def normalize(raw: Optional[Dict[str, Any]], *, previous: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
    """页面提交的配置 → 校验过、补齐默认值的完整配置。不合法就抛 ValueError（说清楚哪里错）。

    previous：库里已有的配置。推送令牌留空或是打码值时沿用它的令牌——
    页面上看不到令牌原文，不能因为"没重新填"就把它清掉。
    """
    raw = raw or {}
    cfg = default_config()

    scenics = raw.get("scenics") or []
    if not isinstance(scenics, list):
        raise ValueError("scenics 必须是景区 ID 列表")
    cfg["scenics"] = list(dict.fromkeys(str(s).strip() for s in scenics if str(s).strip()))

    params = {**cfg["params"], **(raw.get("params") or {})}
    p = cfg["params"]
    p["lookback_days"] = _int(params, "lookback_days", 7, 1100, "取数回溯天数")
    p["rerun_days"] = _int(params, "rerun_days", 0, 60, "往前重跑天数")
    mode = str(params.get("backfill_mode") or "fill")
    if mode not in ("fill", "off"):
        raise ValueError("缺数回补只能是 fill（复制最近一天的明细）或 off（记 0）")
    p["backfill_mode"] = mode
    p["backfill_lookback"] = _int(params, "backfill_lookback", 0, 365, "回补最多往前找几天")
    for key in ("enable_drill_analysis", "drill_keyword_filter", "drill_mask_content",
                "enable_macro_metric", "write_db"):
        p[key] = bool(params.get(key))
    p["content_top_words"] = _int(params, "content_top_words", 0, 100000, "关键词保留个数")
    weights = {**cfg["params"]["score_weights"], **(params.get("score_weights") or {})}
    for k in ("positive", "neutral", "negative"):
        try:
            v = float(weights[k])
        except (TypeError, ValueError):
            raise ValueError(f"得分权重 {k} 必须是数字")
        if not 0 <= v <= 1:
            raise ValueError(f"得分权重 {k} 必须在 0~1 之间（当前 {v}）")
        weights[k] = v
    p["score_weights"] = {k: weights[k] for k in ("positive", "neutral", "negative")}
    ads_db = str(params.get("ads_database") or "").strip()
    if ads_db and not _IDENT_RE.match(ads_db):
        raise ValueError(f"ADS 库名只能是字母、数字、下划线：{ads_db}")
    p["ads_database"] = ads_db

    sched = {**cfg["schedule"], **(raw.get("schedule") or {})}
    cron = str(sched.get("cron") or "").strip()
    _check_cron(cron)
    cfg["schedule"] = {"enabled": bool(sched.get("enabled")), "cron": cron}

    push = {**cfg["push"], **(raw.get("push") or {})}
    out = cfg["push"]
    out["enabled"] = bool(push.get("enabled"))
    out["url"] = str(push.get("url") or "").strip()
    if out["url"] and not _URL_RE.match(out["url"]):
        raise ValueError(f"推送地址要以 http:// 或 https:// 开头：{out['url']}")
    if out["enabled"] and not out["url"]:
        raise ValueError("打开了推送，但没填推送地址")
    out["token_header"] = str(push.get("token_header") or "").strip() \
        or cfg["push"]["token_header"]
    if not re.match(r"^[A-Za-z0-9_-]+$", out["token_header"]):
        raise ValueError(f"鉴权请求头名不合法：{out['token_header']}")
    # 前缀可以是空串（= 不加前缀），所以不能用 `or` 兜默认值
    prefix = push.get("token_prefix")
    out["token_prefix"] = cfg["push"]["token_prefix"] if prefix is None else str(prefix)
    token = str(push.get("token") or "")
    if token in ("", _SECRET_MASK) and previous:
        token = str(((previous.get("push") or {}).get("token")) or "")
    out["token"] = token
    body = {**cfg["push"]["body_fields"], **(push.get("body_fields") or {})}
    for k in ("table", "pk", "data"):
        name = str(body.get(k) or "").strip()
        if not name:
            raise ValueError(f"报文字段名 {k} 不能为空")
        body[k] = name
    out["body_fields"] = {k: body[k] for k in ("table", "pk", "data")}
    extra = push.get("extra_fields") or {}
    if not isinstance(extra, dict):
        raise ValueError("额外报文字段必须是键值对")
    out["extra_fields"] = {str(k).strip(): v for k, v in extra.items() if str(k).strip()}
    out["batch_size"] = _int(push, "batch_size", 1, 10000, "每批行数")
    out["timeout"] = _int(push, "timeout", 1, 600, "超时秒数")
    out["retries"] = _int(push, "retries", 0, 10, "重试次数")
    out["success_field"] = str(push.get("success_field") or "").strip()
    out["dry_run"] = bool(push.get("dry_run"))

    tables = raw.get("tables") or {}
    for t in TABLES:
        given = tables.get(t["key"]) or {}
        tc = cfg["tables"][t["key"]]
        tc["push"] = bool(given.get("push", True))
        name = str(given.get("push_name") or "").strip()
        if name and not _IDENT_RE.match(name.replace(".", "_")):
            raise ValueError(f"{t['label']}：下游表名不合法：{name}")
        tc["push_name"] = name
        tc["pk"] = [c for c in (str(x).strip() for x in (given.get("pk") or [])) if c]
        fields, seen_names = [], set()
        for f in given.get("fields") or []:
            column = str((f or {}).get("column") or "").strip()
            if not column:
                continue
            target = str(f.get("name") or "").strip() or column
            if not _IDENT_RE.match(target):
                raise ValueError(f"{t['label']}：下游字段名不合法：{target}")
            if target in seen_names:
                raise ValueError(f"{t['label']}：下游字段名重复：{target}")
            seen_names.add(target)
            fields.append({"column": column, "name": target})
        if fields and tc["pk"]:
            picked = {f["column"] for f in fields}
            missing = [c for c in tc["pk"] if c not in picked]
            if missing:
                raise ValueError(f"{t['label']}：主键字段没有勾选推送：{'、'.join(missing)}")
        tc["fields"] = fields
    return cfg


def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给页面看的版本：令牌不回显，只告诉它"填过了没有"。"""
    out = copy.deepcopy(cfg)
    push = out.get("push") or {}
    push["has_token"] = bool(push.get("token"))
    push["token"] = ""
    return out


def _int(src: Dict[str, Any], key: str, lo: int, hi: int, label: str) -> int:
    try:
        value = int(src.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数")
    if not lo <= value <= hi:
        raise ValueError(f"{label}要在 {lo}~{hi} 之间（当前 {value}）")
    return value


def _check_cron(cron: str) -> None:
    from croniter import croniter

    if len(cron.split()) != _CRON_FIELDS or not croniter.is_valid(cron):
        raise ValueError(f"定时要写 5 段 cron（分 时 日 月 周），例如 30 2 * * * 是每天 2:30：{cron}")


# ---------------------------------------------------------------- 建表语句
def ddl_statements() -> Dict[str, str]:
    """引擎自带的建表语句 → {表名: CREATE TABLE IF NOT EXISTS ...}。"""
    out: Dict[str, str] = {}
    for name in DDL_FILES:
        path = SQL_DIR / name
        if not path.exists():
            continue
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if not ln.lstrip().startswith("--")]
        for stmt in "\n".join(lines).split(";"):
            m = re.match(r"\s*create\s+table\s+(?:if\s+not\s+exists\s+)?`?(\w+)`?",
                         stmt, re.I)
            if m and m.group(1) in TABLE_NAMES:
                body = stmt[m.end():]
                out[m.group(1)] = f"CREATE TABLE IF NOT EXISTS `{m.group(1)}`{body}"
    return out


def ddl_comments() -> Dict[str, Dict[str, Any]]:
    """建表语句里的中文注释：{表名: {"comment": 表注释, "columns": {列: 注释}}}。页面上当字段说明。"""
    out: Dict[str, Dict[str, Any]] = {}
    col_re = re.compile(r"^\s*`?(\w+)`?\s+(?:bigint|varchar|int|decimal|datetime|text|"
                        r"longtext|timestamp|char|date|tinyint|double|float)\b.*?"
                        r"comment\s+'([^']*)'", re.I)
    for table, stmt in ddl_statements().items():
        cols = {}
        for line in stmt.splitlines():
            m = col_re.match(line)
            if m:
                cols[m.group(1)] = m.group(2)
        tm = re.search(r"\)\s*comment\s*=?\s*'([^']*)'", stmt, re.I)
        out[table] = {"comment": tm.group(1) if tm else "", "columns": cols}
    return out
