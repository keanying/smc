"""请求体模型。响应统一走 {"code":0,"data":...,"message":""} 结构。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from ..core.constants import ALL_CHANNELS


class ScenicIn(BaseModel):
    scenic_id: str = Field(..., min_length=1, max_length=100, description="景区ID")
    scenic_name: str = Field(..., min_length=1, max_length=200, description="景区名称")
    province: Optional[str] = None
    city: Optional[str] = None
    remark: Optional[str] = None
    enabled: int = 1


class ScenicBulkIn(BaseModel):
    items: List[ScenicIn]


class KeywordsIn(BaseModel):
    keywords: List[str] = Field(..., description="关键字列表，重复的会自动跳过")

    @field_validator("keywords")
    @classmethod
    def _not_empty(cls, value: List[str]) -> List[str]:
        cleaned = [k.strip() for k in value if k and k.strip()]
        if not cleaned:
            raise ValueError("关键字列表不能为空")
        return cleaned


class FilterWordsIn(BaseModel):
    """景区的附关键字 / 过滤关键字。

    kind：aux = 附关键字（命中就留存）；exclude = 过滤关键字（命中就丢弃）。
    """
    kind: str = Field(..., description="aux（附关键字）或 exclude（过滤关键字）")
    words: List[str] = Field(..., description="词列表，支持逗号/换行粘贴，重复的自动跳过")

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value not in ("aux", "exclude"):
            raise ValueError("kind 只能是 aux（附关键字）或 exclude（过滤关键字）")
        return value

    @field_validator("words")
    @classmethod
    def _not_empty(cls, value: List[str]) -> List[str]:
        cleaned = [w.strip() for w in value if w and w.strip()]
        if not cleaned:
            raise ValueError("词列表不能为空")
        return cleaned


class TargetIn(BaseModel):
    channel: str
    target_type: str = "poi"
    target_id: str = Field(..., description="携程 POI_ID / 同程 sid / 作者主页ID")
    target_name: Optional[str] = None
    target_url: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)
    enabled: int = 1

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, value: str) -> str:
        if value not in ALL_CHANNELS:
            raise ValueError(f"未知平台：{value}，可选：{'、'.join(ALL_CHANNELS)}")
        return value

    @field_validator("target_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ("poi", "creator"):
            raise ValueError("target_type 只能是 poi 或 creator")
        return value


class AccountIn(BaseModel):
    channel: str
    account_name: str = Field(..., min_length=1, max_length=120)
    nickname: Optional[str] = None
    login_type: str = "qrcode"
    account_group: str = Field("default", max_length=100)
    enabled: int = 1
    #: 轮换锁时长（小时）。0 = 跟随系统设置里的全局值（默认 12 小时）。
    #: 上限 14 天：再大基本是填错了，等于把这个号永久雪藏。
    rotate_lock_hours: int = Field(0, ge=0, le=24 * 14)

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, value: str) -> str:
        if value not in ALL_CHANNELS:
            raise ValueError(f"未知平台：{value}")
        return value


class CookieImportIn(BaseModel):
    """手动导入 Cookie。

    raw 可以是 Cookie-Editor 导出的 JSON 数组，也可以是
    「name=value; name2=value2」这种请求头串，后端自动识别。
    """

    raw: str = Field(..., min_length=1, description="粘贴的 Cookie 内容")

    @field_validator("raw")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Cookie 内容不能为空")
        return value


def _validate_single_channel(value: List[str]) -> List[str]:
    """一个任务只能有一个平台。

    为什么收紧成单选（原来允许多选）：
      · 采集方式（接口/混合/拟人）、账号占用、浏览器排队都是按平台算的，
        混在一条任务里就没法单独设置，也说不清"这条任务失败了"是哪个平台失败；
      · 拟人模式下一个账号同时只能跑一个景区，多平台任务会在一条任务内部
        互相排队，进度条和日志都变得没法读；
      · 某个平台失败想重跑，只能把整条任务连着其它平台一起重跑。
    改成一平台一任务之后，这些都变成"再建一条任务"就解决了。

    ⚠️ 字段仍然是数组：库里已有的多平台老任务照常能读能跑，
    只是保存时会被收敛成一个。
    """
    if not value:
        raise ValueError("请选择采集平台")
    unknown = [c for c in value if c not in ALL_CHANNELS]
    if unknown:
        raise ValueError(f"未知平台：{'、'.join(unknown)}")
    if len(value) > 1:
        raise ValueError(
            "一个任务只能选一个采集平台；要采多个平台请分别建任务"
            f"（这次收到了 {len(value)} 个：{'、'.join(value)}）"
        )
    return value


class TaskIn(BaseModel):
    task_name: str = Field(..., min_length=1, max_length=200)
    scenic_id: Optional[str] = None
    channels: List[str] = Field(default_factory=list)
    collect_type: str = "keyword"
    keywords: List[str] = Field(default_factory=list)
    targets: List[Dict[str, Any]] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)

    schedule_type: str = "once"
    schedule_at: Optional[str] = None
    schedule_interval_seconds: Optional[int] = None
    cron_expression: Optional[str] = None
    timezone: str = "Asia/Shanghai"
    created_by: Optional[str] = None

    @field_validator("channels")
    @classmethod
    def _channels_valid(cls, value: List[str]) -> List[str]:
        return _validate_single_channel(value)

    @field_validator("schedule_type")
    @classmethod
    def _schedule_valid(cls, value: str) -> str:
        if value not in ("once", "at", "interval", "cron"):
            raise ValueError("schedule_type 只能是 once / at / interval / cron")
        return value

    @field_validator("collect_type")
    @classmethod
    def _collect_valid(cls, value: str) -> str:
        if value not in ("keyword", "creator", "poi", "detail"):
            raise ValueError("collect_type 只能是 keyword / creator / poi / detail")
        return value


class TaskPatchIn(BaseModel):
    """编辑已存在的任务：所有字段都可选，只改传上来的那些。

    需求 5：已经在跑的任务要能改运行模式和采集数量，而不是重新建一条。
    用 PATCH 而不是复用 PUT，是因为 PUT 要求带全量字段，
    前端只想改个 cron 却得把 keywords/targets 全传一遍，很容易把别的字段改坏。
    """

    task_name: Optional[str] = Field(None, min_length=1, max_length=200)
    channels: Optional[List[str]] = None
    collect_type: Optional[str] = None
    keywords: Optional[List[str]] = None
    params: Optional[Dict[str, Any]] = None

    schedule_type: Optional[str] = None
    schedule_at: Optional[str] = None
    schedule_interval_seconds: Optional[int] = None
    cron_expression: Optional[str] = None
    schedule_enabled: Optional[bool] = None
    timezone: Optional[str] = None

    @field_validator("channels")
    @classmethod
    def _channels_valid(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return value
        return _validate_single_channel(value)

    @field_validator("schedule_type")
    @classmethod
    def _schedule_valid(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ("once", "at", "interval", "cron"):
            raise ValueError("schedule_type 只能是 once / at / interval / cron")
        return value

    @field_validator("collect_type")
    @classmethod
    def _collect_valid(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ("keyword", "creator", "poi", "detail"):
            raise ValueError("collect_type 只能是 keyword / creator / poi / detail")
        return value


class SettingIn(BaseModel):
    section: str
    values: Dict[str, Any]


class ProxyTestIn(BaseModel):
    secret_id: Optional[str] = None
    secret_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    auth_mode: str = "token"
