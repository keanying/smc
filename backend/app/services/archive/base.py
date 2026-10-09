"""景区档案采集源的公共约定。

**档案不是点评。** `collectors/` 下那些采的是用户发的内容（作品、评论），
要账号、要标注、要进内容表；这里采的是**平台自己的景区资料**——
名字、等级、地址、开放时间、优待政策。两者共用代理池，别的都不共用。

三个渠道的能力差得很远，不要按"都一样"来用：

    渠道        区域编号          列表字段          详情页
    ---------  ---------------  ---------------  --------------------
    携程        districtId       名称/等级/地址     有：电话/开放时间/
    (ctrip)                                       优待政策/服务设施/介绍
    同程        pid（省）         名称/等级/地址/    没有（只到列表这一层）
    (tongcheng)                  省/市
    去哪儿      城市 slug         名称/等级/地址     有：电话/开放时间/
    (qunar)                                       优待政策/介绍

所以 `supports_detail = False` 的渠道（同程），档案里那几列**永远是空的**。
这不是采集失败，是平台不提供——detail_status 会写成 `unsupported`
而不是 `error`，免得有人对着一屏"失败"去反复重采。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: 档案支持的渠道。顺序就是页面上标签页的顺序。
ARCHIVE_CHANNELS = ("tongcheng", "ctrip", "qunar")


@dataclass
class Region:
    """一个可采集区域。region_id 是**平台自己的编号**，不是行政区划代码。"""
    region_id: str
    region_name: str
    level: str = "province"


@dataclass
class ArchiveRow:
    """一条景区档案。字段名和 src_opinion_scenic_poi_info 的列一一对应。"""
    poi_id: str
    poi_name: str
    province: str = ""
    city_id: str = ""
    city_name: str = ""
    address: str = ""
    scenic_level: str = ""
    open_time: str = ""
    tel: str = ""
    scenic_intro: str = ""
    discount_policy: str = ""
    amenity: str = ""
    source_url: str = ""
    region_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "poi_id": self.poi_id, "poi_name": self.poi_name,
            "province": self.province, "city_id": self.city_id,
            "city_name": self.city_name, "address": self.address,
            "scenic_level": self.scenic_level, "open_time": self.open_time,
            "tel": self.tel, "scenic_intro": self.scenic_intro,
            "discount_policy": self.discount_policy, "amenity": self.amenity,
            "source_url": self.source_url, "region_id": self.region_id,
        }


#: 采集过程中往外报进度用。参数是(已采条数, 一句人话)。
ProgressFn = Callable[[int, str], None]


class ArchiveSource:
    """一个渠道的档案采集源。

    子类只要实现 `regions()` 和 `collect_region()` 两个方法；
    支持详情的再实现 `fetch_detail()`。HTTP、代理、重试都在基类里。
    """

    channel: str = ""
    label: str = ""
    #: 平台有没有景区详情页。False 的渠道，详情那几列永远为空。
    supports_detail: bool = False
    base_headers: Dict[str, str] = field(default_factory=dict)  # type: ignore[assignment]

    def __init__(self, config, proxy_manager, db=None):
        self.config = config
        self.proxy_manager = proxy_manager
        self.db = db

    # ---------------- HTTP（统一走代理池） ----------------
    def _client(self):
        from ...proxy.manager import ProxiedClient
        return ProxiedClient(self.config, self.proxy_manager, self.channel,
                             base_headers=dict(self.base_headers or {}))

    async def get_text(self, url: str, params: Optional[Dict] = None) -> str:
        async with self._client() as client:
            return await client.get_text(url, params=params or {})

    async def post_json(self, url: str, payload: Dict[str, Any]) -> Any:
        async with self._client() as client:
            return await client.post_json(url, json_body=payload)

    # ---------------- 子类实现 ----------------
    async def regions(self) -> List[Region]:
        raise NotImplementedError

    async def collect_region(self, region: Region, *,
                             progress: Optional[ProgressFn] = None,
                             limit: int = 0) -> List[ArchiveRow]:
        raise NotImplementedError

    async def fetch_detail(self, poi_id: str) -> Dict[str, str]:
        """返回 open_time/tel/scenic_intro/discount_policy/amenity 五个键。

        不支持详情的渠道不实现，采集作业会跳过——注意是**跳过**，
        不是当成失败重试。
        """
        raise NotImplementedError(f"{self.channel} 没有景区详情页")


_REGISTRY: Dict[str, type] = {}


def register(cls):
    _REGISTRY[cls.channel] = cls
    return cls


def get_source(channel: str, config, proxy_manager, db=None) -> ArchiveSource:
    cls = _REGISTRY.get(channel)
    if cls is None:
        raise ValueError(f"没有 {channel} 的档案采集源，可用的是：{sorted(_REGISTRY)}")
    return cls(config, proxy_manager, db)


def available() -> List[Dict[str, Any]]:
    """页面上列渠道标签页用。按 ARCHIVE_CHANNELS 的顺序。"""
    out = []
    for ch in ARCHIVE_CHANNELS:
        cls = _REGISTRY.get(ch)
        if cls is None:
            continue
        out.append({"channel": ch, "label": cls.label,
                    "supports_detail": cls.supports_detail})
    return out
