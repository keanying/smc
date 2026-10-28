# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

from __future__ import annotations

from dataclasses import asdict, dataclass


CITY_COLUMNS = ["city_id", "city_name", "city_url"]

REGION_COLUMNS = [
    "province_code",
    "province_name",
    "city_code",
    "city_name",
    "district_code",
    "district_name",
    "qunar_city_id",
    "qunar_city_name",
    "qunar_city_url",
    "source_url",
]

POI_COLUMNS = [
    "city_id",
    "city_name",
    "poi_id",
    "poi_name",
    "address",
    "open_time",
    "tel",
    "scenic_intro",
    "discount_policy",
    "amenity",
    "scenic_level",
]

COMMENT_COLUMNS = [
    "scenic_id",
    "scenic_name",
    "channel",
    "work_id",
    "comment_level",
    "comment_parent_id",
    "comment_id",
    "commenter_id",
    "image_list",
    "video_list",
    "location",
    "content",
    "likes",
    "extra_content",
    "sentiment_label",
    "sentiment_score",
    "dimension_tags",
    "entity_tags",
    "keyword_tags",
    "label_review_flag",
    "publish_time",
    "crawl_time",
    "commenter_name",
    "root_comment_id",
    "sub_comment_count",
]


@dataclass(frozen=True)
class City:
    city_id: str
    city_name: str
    city_url: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PoiSummary:
    poi_id: str
    poi_name: str
    address: str = ""
    scenic_level: str = ""


@dataclass(frozen=True)
class PoiRecord:
    city_id: str
    city_name: str
    poi_id: str
    poi_name: str
    address: str
    open_time: str
    tel: str
    scenic_intro: str
    discount_policy: str
    amenity: str
    scenic_level: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
