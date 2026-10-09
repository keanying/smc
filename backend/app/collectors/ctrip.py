"""携程点评采集器。

直接沿用用户已验证可跑通的 ctrip_collection.py 的请求流程，改动只有三处：
  1. 同步 requests -> 异步 ProxiedClient（自动换 IP + 指纹绑定）
  2. 输出从 CSV 行改成统一的 WorkItem / CommentItem
  3. 每个 POI 生成一条"合成作品"，让数据中心的
     景区 -> 平台 -> 作品 -> 评论 这棵树对携程也成立
     （extra_content.synthetic=true 标记，需要时可一键过滤掉）

携程没有登录要求，只需要一个 cid（客户端标识），换 IP 时同步换 cid。
"""
from __future__ import annotations

import copy
import json
import random
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.logging import get_logger
from ..utils import normalize as nz
from .base import (
    BaseCollector,
    CollectContext,
    CollectorRegistry,
    CollectTarget,
    CommentItem,
    WorkItem,
)

logger = get_logger(__name__)

CLIENT_ID_URL = (
    "https://m.ctrip.com/restapi/soa2/10290/createclientid"
    "?systemcode=09&createtype=3&contentType=json"
)
COMMENT_URL = "https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList"

BASE_PAYLOAD: Dict[str, Any] = {
    "arg": {
        "channelType": 2,
        "collapseType": 0,
        "commentTagId": 0,
        "pageIndex": 1,
        "pageSize": 10,
        "poiId": 0,
        "sourceType": 1,
        "sortType": 1,
        "starType": 0,
    },
    "head": {
        "cid": "",
        "ctok": "",
        "cver": "1.0",
        "lang": "01",
        "sid": "8888",
        "syscode": "09",
        "auth": "",
        "xsid": "",
        "extension": [],
    },
}

SITE_HEADERS = {
    "Origin": "https://m.ctrip.com",
    "Referer": "https://m.ctrip.com/",
}


@CollectorRegistry.register
class CtripCollector(BaseCollector):
    channel = "ctrip"
    supports_works = False       # 携程没有"作品"，只有景区点评
    needs_login = False
    supported_targets = ("poi",)

    # 做成类属性，测试时可指向本地假服务器
    client_id_url = CLIENT_ID_URL
    comment_url = COMMENT_URL

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._cid: str = ""
        self._client = None

    # ---------------- 生命周期 ----------------
    async def prepare(self, ctx: CollectContext) -> None:
        self._client = self.make_client(base_headers=dict(SITE_HEADERS))
        await self._client.__aenter__()
        ctx.log(f"[携程] 采集客户端就绪，指纹 {self._client.profile_name}")

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------- cid ----------------
    async def _get_cid(self) -> str:
        if self._cid:
            return self._cid
        data = await self._client.get_json(self.client_id_url)
        cid = (data or {}).get("ClientID")
        if not cid:
            raise RuntimeError(f"获取 cid 的响应中没有 ClientID：{str(data)[:200]}")
        self._cid = str(cid)
        return self._cid

    def _reset_cid(self) -> None:
        """换 IP 后 cid 必须一起换，否则新 IP 带着老 cid 更容易被识别。"""
        self._cid = ""

    @staticmethod
    def _trace_id(cid: str) -> str:
        return f"{cid}-{int(time.time() * 1000)}-{random.randint(0, 9999999)}"

    # ---------------- 合成作品 ----------------
    def poi_work(self, ctx: CollectContext, target: CollectTarget) -> WorkItem:
        """为一个 POI 生成一条合成作品记录，作为其下所有点评的挂载点。"""
        poi_id = str(target.value)
        return self.new_work(
            ctx,
            work_id=poi_id,
            work_url=f"https://you.ctrip.com/sight/{poi_id}.html",
            author_id="ctrip",
            author_name="携程",
            title=f"{target.name or ctx.scenic_name} · 携程点评",
            description=f"携程 POI {poi_id} 的点评集合",
            location=ctx.scenic_name,
            publish_time=None,
            crawl_time=datetime.now(),
            extra_content=json.dumps(
                {"synthetic": True, "poi_id": poi_id, "source": "ctrip"},
                ensure_ascii=False,
            ),
        ).finalize()

    # ---------------- 采集 ----------------
    async def collect_by_poi(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[CommentItem]:
        poi_id = str(target.value).strip()
        if not poi_id.isdigit():
            raise ValueError(f"携程 POI_ID 必须是数字：{poi_id}")

        page_size = int(self.platform_config.get("page_size", 10))
        # 0 表示"一直翻到接口没有数据为止"，所以不能用 or 兜底——
        # 那样用户显式填的 0 会被当成"没填"退回默认值
        raw_pages = ctx.params.get("max_pages")
        if raw_pages in (None, ""):
            raw_pages = self.platform_config.get("max_pages", 300)
        max_pages = max(0, int(raw_pages))
        sort_type = int(self.platform_config.get("sort_type", 1))

        page_index = 1
        pages_fetched = 0
        total_count: Optional[int] = None
        emitted = 0
        seen_ids: set[str] = set()

        while True:
            ctx.raise_if_cancelled()
            if max_pages > 0 and pages_fetched >= max_pages:
                ctx.log(f"[携程] POI {poi_id} 已达页数上限 {max_pages}")
                break
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                ctx.log(f"[携程] POI {poi_id} 已达评论条数上限 {ctx.max_comments_per_work}")
                break

            result = await self._fetch_page(poi_id, page_index, page_size, sort_type)
            items: List[Dict] = result.get("items") or []

            if total_count is None and isinstance(result.get("totalCount"), int):
                total_count = result["totalCount"]
                ctx.log(f"[携程] POI {poi_id} 接口报告点评总数 {total_count}")

            if not items:
                ctx.log(f"[携程] POI {poi_id} 第 {page_index} 页为空，采集结束")
                break

            new_in_page = 0
            for raw in items:
                comment_id = str(raw.get("commentId") or "")
                if not comment_id or comment_id in seen_ids:
                    continue
                seen_ids.add(comment_id)
                new_in_page += 1

                comment = self.safe_map(ctx, self._to_comment, ctx, raw, poi_id,
                                        what="点评")
                yield comment
                emitted += 1

                # 商家/其他用户的回复作为二级评论
                if ctx.enable_sub_comments and ctx.max_comment_level >= 2:
                    for reply in self._extract_replies(raw):
                        sub = self._to_reply(ctx, reply, poi_id, comment.comment_id)
                        if sub is not None:
                            yield sub
                            emitted += 1

                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            if new_in_page == 0:
                ctx.log(f"[携程] POI {poi_id} 第 {page_index} 页无新增（重复页），采集结束")
                break

            pages_fetched += 1
            ctx.log(
                f"[携程] POI {poi_id} 第 {page_index} 页：本页 {len(items)} 条，累计 {emitted} 条"
            )

            if total_count is not None and len(seen_ids) >= total_count:
                ctx.log(f"[携程] POI {poi_id} 已达接口总数，采集结束")
                break
            if len(items) < page_size:
                ctx.log(f"[携程] POI {poi_id} 当前页不足一页，采集结束")
                break

            page_index += 1
            await self._client.sleep_interval()

    async def _fetch_page(
        self, poi_id: str, page_index: int, page_size: int, sort_type: int
    ) -> Dict[str, Any]:
        """请求单页点评；代理异常已由 ProxiedClient 处理，这里只管业务错误重试。"""
        max_retries = int(self.config.get("crawl.max_retries", 3))
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            cid = await self._get_cid()
            params = {"_fxpcqlniredt": cid, "x-traceID": self._trace_id(cid)}

            payload = copy.deepcopy(BASE_PAYLOAD)
            payload["arg"]["poiId"] = int(poi_id)
            payload["arg"]["pageIndex"] = page_index
            payload["arg"]["pageSize"] = page_size
            payload["arg"]["sortType"] = sort_type
            payload["head"]["cid"] = cid

            try:
                data = await self._client.post_json(
                    self.comment_url, params=params, json_body=payload
                )
                if data.get("code") == 200:
                    return data.get("result") or {}
                last_error = RuntimeError(
                    f"业务错误：code={data.get('code')}，msg={data.get('msg', '')}"
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                # 代理层已经换过 IP，这里把 cid 一并作废
                self._reset_cid()

            # 第二次失败后强制换 cid 再试
            if attempt == 1:
                self._reset_cid()

        raise RuntimeError(f"请求 POI={poi_id} 第 {page_index} 页失败：{last_error}")

    # ---------------- 字段映射 ----------------
    def _to_comment(self, ctx: CollectContext, raw: Dict, poi_id: str) -> CommentItem:
        user_info = raw.get("userInfo") or {}
        score_map = {
            entry.get("name"): entry.get("score")
            for entry in (raw.get("scores") or [])
            if entry.get("name")
        }
        # images[].imageSrcUrl 才是原图，imageThumbUrl 是 180x180 缩略图。
        # collect_urls 的规则是「一个 dict 只产出一个资源」，所以这里一张图只会
        # 落一条原图地址，不会把缩略图当成第二张图。
        images = nz.collect_urls(raw.get("images"), raw.get("imageList"), raw.get("pictures"))
        videos = nz.collect_urls(raw.get("videos"), raw.get("videoList"))

        replies = self._extract_replies(raw)

        # 携程特有字段全部塞进 extra_content，不丢信息
        extra = {
            "score": raw.get("score"),
            "landscape_score": score_map.get("景色"),
            "fun_score": score_map.get("趣味"),
            "price_quality_score": score_map.get("性价比"),
            "tourist_type": raw.get("touristTypeDisplay"),
            "travel_type": raw.get("travelTypeDisplay"),
            "useful_count": raw.get("usefulCount"),
            "collect_count": raw.get("collectCnt"),
            "from_type": raw.get("fromTypeText"),
            "user_member": user_info.get("userMember"),
            "user_avatar": user_info.get("userImage"),
            "detail_url": raw.get("jumpH5Url"),
            "poi_id": poi_id,
        }

        return self.new_comment(
            ctx,
            comment_id=str(raw.get("commentId") or ""),
            work_id=poi_id,
            depth=1,
            comment_parent_id="",
            root_comment_id=str(raw.get("commentId") or ""),
            commenter_id=nz.to_text(user_info.get("userId")),
            commenter_name=nz.to_text(user_info.get("nickName") or user_info.get("userNick")),
            content=nz.to_text(raw.get("content")),
            likes=nz.to_int(raw.get("usefulCount")),
            # replyCount 是接口给的权威值；replyInfo 可能只带回第一页回复
            sub_comment_count=nz.to_int(raw.get("replyCount"), default=len(replies)) or len(replies),
            location=nz.to_text(raw.get("ipLocatedName")),
            image_list=nz.to_json_list(images),
            video_list=nz.to_json_list(videos),
            publish_time=nz.to_datetime(raw.get("publishTime")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json({k: v for k, v in extra.items() if v not in (None, "")}),
        ).finalize()

    @staticmethod
    def _extract_replies(raw: Dict) -> List[Dict]:
        """取出一条点评下的回复。

        真实响应里回复列表的键是 replyInfo（之前猜的 replyList 等一个都没命中，
        携程的图和回复因此被静默丢掉）。另外还有一种只回一条时的扁平写法：
        回复内容直接放在点评对象的 replyContent/replyTime 上，replyInfo 为空。
        两种都要认。
        """
        for key in ("replyInfo", "replyList", "replies", "shopReply", "replyInfoList"):
            value = raw.get(key)
            if isinstance(value, list) and value:
                return value
            if isinstance(value, dict) and value:
                return [value]

        # 扁平写法：顶层 replyContent 有值就当成一条商家回复
        if raw.get("replyContent"):
            return [{
                "replyId": f"{raw.get('commentId')}_reply",
                "content": raw.get("replyContent"),
                "publishTime": raw.get("replyTime"),
                "ipLocatedName": raw.get("replyIpLocatedName"),
                "replyUserName": raw.get("replyTag") or "商家回复",
                "_flattened": True,
            }]
        return []

    def _to_reply(
        self, ctx: CollectContext, raw: Dict, poi_id: str, parent_id: str
    ) -> Optional[CommentItem]:
        reply_id = str(
            raw.get("replyId") or raw.get("commentId") or raw.get("id") or ""
        )
        if not reply_id:
            return None
        user_info = raw.get("userInfo") or {}
        images = nz.collect_urls(raw.get("images"), raw.get("imageList"))
        videos = nz.collect_urls(raw.get("videos"), raw.get("videoList"))
        return self.new_comment(
            ctx,
            comment_id=reply_id,
            work_id=poi_id,
            depth=2,
            comment_parent_id=parent_id,
            root_comment_id=parent_id,
            commenter_id=nz.to_text(user_info.get("userId") or raw.get("replyUserId")),
            commenter_name=nz.to_text(
                user_info.get("nickName")
                or user_info.get("userNick")
                or raw.get("replyUserName")
                or "携程商家"
            ),
            content=nz.to_text(raw.get("content") or raw.get("replyContent")),
            likes=nz.to_int(raw.get("usefulCount")),
            location=nz.to_text(raw.get("ipLocatedName")),
            image_list=nz.to_json_list(images),
            video_list=nz.to_json_list(videos),
            publish_time=nz.to_datetime(raw.get("publishTime") or raw.get("replyTime")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json({
                "poi_id": poi_id,
                "is_reply": True,
                "flattened": bool(raw.get("_flattened")),
            }),
        ).finalize()
