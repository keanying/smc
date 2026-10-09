"""同程点评采集器（www.ly.com DianPingAjax 接口）。

接口形如：
  https://www.ly.com/scenery/AjaxHelper/DianPingAjax.aspx
      ?action=GetDianPingList&sid=32289&page=1&pageSize=10&labId=6&sort=0&iid=<随机数>

字段映射依据 2026-08 从线上抓到的真实响应（backend/tests/fixtures/tongcheng_page.json）：

  顶层：  isSuccess errorMsg dpList totalNum pageInfo dpTagList dianpingInfo
          goodNum midNum badNum starNum degreeLevel hasImgNum serviceScoreAvgList ...
  单条：  dpId dpGuid dpContent dpDate dpUserName homeId DPLocation zanCount
          dpImgUrl csReplyList subList dpImpressionList lineAccess commentType
          servicePoint DPUserLevel isElite dpTripMode dpTripPurpose
          DPItemId DPItemName DPSite memberHeaderImgUrl ...

仍保留"多候选键名"机制，是为了接口小改版时不至于直接采空——
命中不了会在任务日志里打出实际键名，一眼能看出要改哪里。

两种回复要分开处理，语义不同：
  csReplyList  客服/商家回复    -> level_2，extra_content.reply_type=cs
  subList      用户追评/子评论  -> level_2，extra_content.reply_type=sub
"""
from __future__ import annotations

import hashlib
import json
import random
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

DIANPING_URL = "https://www.ly.com/scenery/AjaxHelper/DianPingAjax.aspx"

SITE_HEADERS = {
    "Origin": "https://www.ly.com",
    "Referer": "https://www.ly.com/scenery/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# 语义字段 -> 键名候选（第一个是线上实际使用的，其余为容错备选）
FIELD_MAP: Dict[str, List[str]] = {
    # --- 顶层 ---
    "list": ["dpList", "dianPingList", "commentList", "list", "items"],
    "total": ["totalNum", "total", "totalCount", "dpCount", "recordCount"],
    "page_count": ["pageCount", "totalPage", "pageTotal", "PageCount"],
    "success": ["isSuccess"],
    "error": ["errorMsg"],
    # --- 单条评论 ---
    "comment_id": ["dpId", "dpGuid", "id", "commentId"],
    "comment_guid": ["dpGuid"],
    "content": ["dpContent", "content", "dpText", "comment"],
    "publish_time": ["dpDate", "dpTime", "createDate", "createTime", "addTime"],
    # 同程没有独立的 memberId 字段；homeId 是会员主页 ID，最接近"评论用户ID"
    "user_id": ["homeId", "memberId", "userId", "uid", "customerId"],
    "user_name": ["dpUserName", "userName", "memberName", "nickName"],
    "avatar": ["memberHeaderImgUrl", "userPhoto", "headImgUrl"],
    "user_level": ["DPUserLevel", "userLevel", "memberLevel"],
    "likes": ["zanCount", "usefulCount", "praiseCount", "likeCount", "goodCount"],
    "images": ["dpImgUrl", "dpPicList", "picList", "images", "imgList", "photoList"],
    "location": ["DPLocation", "ipLocation", "ipLocated", "userLocation"],
    # 评级：线上用 lineAccess（好评/中评/差评）+ commentType（数值分类）；
    # score 一列是给可能存在的 dpGrade 之类数值评分留的兜底
    "rating_text": ["lineAccess"],
    "comment_type": ["commentType"],
    "service_point": ["servicePoint"],
    "score": ["dpGrade", "score", "grade", "Score"],
    "is_elite": ["isElite"],
    "impressions": ["dpImpressionList", "impressionList", "tagList"],
    "trip_mode": ["dpTripMode"],
    "trip_purpose": ["dpTripPurpose"],
    "item_id": ["DPItemId"],
    "item_name": ["DPItemName"],
    "site": ["DPSite"],
    "price_desc": ["productPriceDesc"],
    # --- 两种回复 ---
    # csReplyList 是线上实际字段；replyList / replys 作为接口改版时的兜底
    "cs_replies": ["csReplyList", "csReply", "serviceReplyList", "replyList", "replys"],
    "sub_comments": ["subList", "subDpList", "childList"],
    # --- 回复内部字段 ---
    "reply_id": ["dpId", "replyId", "id", "dpGuid"],
    "reply_content": ["dpContent", "replyContent", "content"],
    "reply_time": ["dpDate", "replyDate", "replyTime", "createTime"],
    "reply_user": ["dpUserName", "replyUserName", "userName", "memberName"],
}


def pick(source: Dict, semantic: str, default: Any = None) -> Any:
    """按 FIELD_MAP 的候选键名依次取值，返回第一个非空值。"""
    if not isinstance(source, dict):
        return default
    for key in FIELD_MAP.get(semantic, []):
        if key in source and source[key] not in (None, "", [], {}):
            return source[key]
    return default


@CollectorRegistry.register
class TongchengCollector(BaseCollector):
    channel = "tongcheng"
    supports_works = False       # 同程同样只有景区点评，没有作品
    needs_login = False
    supported_targets = ("poi",)

    dianping_url = DIANPING_URL  # 测试可覆盖

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._client = None
        self._shape_logged = False

    async def prepare(self, ctx: CollectContext) -> None:
        self._client = self.make_client(base_headers=dict(SITE_HEADERS))
        await self._client.__aenter__()
        self._shape_logged = False
        ctx.log(f"[同程] 采集客户端就绪，指纹 {self._client.profile_name}")

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------- 合成作品 ----------------
    def poi_work(self, ctx: CollectContext, target: CollectTarget) -> WorkItem:
        sid = str(target.value)
        return self.new_work(
            ctx,
            work_id=sid,
            work_url=f"https://www.ly.com/scenery/BookSceneryTicket_{sid}.html",
            author_id="tongcheng",
            author_name="同程",
            title=f"{target.name or ctx.scenic_name} · 同程点评",
            description=f"同程 sid {sid} 的点评集合",
            location=ctx.scenic_name,
            crawl_time=datetime.now(),
            extra_content=json.dumps(
                {"synthetic": True, "sid": sid, "source": "tongcheng"},
                ensure_ascii=False,
            ),
        ).finalize()

    # ---------------- 采集 ----------------
    async def collect_by_poi(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[CommentItem]:
        sid = str(target.value).strip()
        if not sid:
            raise ValueError("同程 sid 不能为空")

        page_size = int(self.platform_config.get("page_size", 10))
        # 0 表示"一直翻到接口没有数据为止"，所以不能用 or 兜底——
        # 那样用户显式填的 0 会被当成"没填"退回默认值
        raw_pages = ctx.params.get("max_pages")
        if raw_pages in (None, ""):
            raw_pages = self.platform_config.get("max_pages", 300)
        max_pages = max(0, int(raw_pages))
        lab_id = int(target.extra.get("lab_id", self.platform_config.get("lab_id", 6)))
        sort = int(target.extra.get("sort", self.platform_config.get("sort", 0)))

        page = 1
        pages_fetched = 0
        emitted = 0
        seen: set[str] = set()
        total: Optional[int] = None

        while True:
            ctx.raise_if_cancelled()
            if max_pages > 0 and pages_fetched >= max_pages:
                ctx.log(f"[同程] sid {sid} 已达页数上限 {max_pages}")
                break
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                ctx.log(f"[同程] sid {sid} 已达评论条数上限 {ctx.max_comments_per_work}")
                break

            payload = await self._fetch_page(sid, page, page_size, lab_id, sort)
            self._log_shape_once(ctx, payload)

            # 接口自己报错时要说清楚，不能当成"没数据"。
            # 注意：isSuccess 是整数 1/0 而不是布尔，所以必须判真假而不是 `is False`
            # （Python 里 `0 is False` 为 False，用 is 判断会把失败响应当成正常）。
            success = payload.get("isSuccess")
            if success is not None and not success:
                message = nz.to_text(payload.get("errorMsg")) or "接口返回 isSuccess=0"
                raise RuntimeError(f"同程接口报错（sid={sid}，第 {page} 页）：{message}")

            items = self._extract_list(payload)
            if total is None:
                total = self._extract_total(payload)
                if total is not None:
                    page_total = self._extract_page_count(payload)
                    ctx.log(
                        f"[同程] sid {sid} 接口报告点评总数 {total}"
                        + (f"，共 {page_total} 页" if page_total else "")
                    )

            if not items:
                ctx.log(f"[同程] sid {sid} 第 {page} 页为空，采集结束")
                break

            new_in_page = 0
            for raw in items:
                comment_id = nz.to_text(pick(raw, "comment_id"))
                if not comment_id or comment_id in seen:
                    continue
                seen.add(comment_id)
                new_in_page += 1

                comment = self.safe_map(ctx, self._to_comment, ctx, raw, sid,
                                        what="点评")
                yield comment
                emitted += 1

                if ctx.enable_sub_comments and ctx.max_comment_level >= 2:
                    for sub in self._iter_replies(ctx, raw, sid, comment.comment_id):
                        yield sub
                        emitted += 1

                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            if new_in_page == 0:
                ctx.log(f"[同程] sid {sid} 第 {page} 页无新增（重复页），采集结束")
                break

            pages_fetched += 1
            ctx.log(f"[同程] sid {sid} 第 {page} 页：本页 {len(items)} 条，累计 {emitted} 条")

            if total is not None and total > 0 and len(seen) >= total:
                ctx.log(f"[同程] sid {sid} 已达接口总数，采集结束")
                break
            if len(items) < page_size:
                ctx.log(f"[同程] sid {sid} 当前页不足一页，采集结束")
                break

            page += 1
            await self._client.sleep_interval()

    async def _fetch_page(
        self, sid: str, page: int, page_size: int, lab_id: int, sort: int
    ) -> Dict[str, Any]:
        params = {
            "action": "GetDianPingList",
            "sid": sid,
            "page": page,
            "pageSize": page_size,
            "labId": lab_id,
            "sort": sort,
            # iid 是接口用来打散缓存的随机数，照抄浏览器行为
            "iid": f"0.{random.randint(10**16, 10**17 - 1)}",
        }
        max_retries = int(self.config.get("crawl.max_retries", 3))
        last_error: Optional[Exception] = None

        for _ in range(max_retries):
            try:
                text = await self._client.get_text(self.dianping_url, params=params)
                return self._parse_body(text)
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        raise RuntimeError(f"请求同程 sid={sid} 第 {page} 页失败：{last_error}")

    @staticmethod
    def _parse_body(text: str) -> Dict[str, Any]:
        """响应可能是纯 JSON，也可能被 JSONP 包一层，两种都兼容。"""
        body = (text or "").strip()
        if not body:
            return {}
        if not body.startswith("{") and not body.startswith("["):
            start = body.find("{")
            end = body.rfind("}")
            if start >= 0 and end > start:
                body = body[start:end + 1]
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise RuntimeError(
                f"同程响应无法解析为 JSON，前 200 字符：{body[:200]}"
            ) from exc
        return data if isinstance(data, dict) else {"dpList": data}

    def _extract_list(self, payload: Dict[str, Any]) -> List[Dict]:
        """列表通常直接在根上（dpList），也兼容包在 data / result 里的情况。"""
        found = pick(payload, "list")
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]
        for wrapper_key in ("data", "Data", "result", "Result", "response"):
            wrapper = payload.get(wrapper_key)
            if isinstance(wrapper, dict):
                nested = pick(wrapper, "list")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            if isinstance(wrapper, list):
                return [item for item in wrapper if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_total(payload: Dict[str, Any]) -> Optional[int]:
        """总数在顶层 totalNum，也在 pageInfo.totalCount 里，两处都认。"""
        value = pick(payload, "total")
        if value in (None, ""):
            page_info = payload.get("pageInfo")
            if isinstance(page_info, dict):
                value = page_info.get("totalCount") or page_info.get("totalNum")
        return nz.to_int(value) if value not in (None, "") else None

    @staticmethod
    def _extract_page_count(payload: Dict[str, Any]) -> Optional[int]:
        """总页数在 pageInfo.totalPage，不在顶层。"""
        page_info = payload.get("pageInfo")
        if isinstance(page_info, dict):
            value = pick(page_info, "page_count") or page_info.get("totalPage")
            if value not in (None, ""):
                return nz.to_int(value)
        value = pick(payload, "page_count")
        return nz.to_int(value) if value not in (None, "") else None

    @staticmethod
    def _as_list(value: Any) -> List[Dict]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    def _log_shape_once(self, ctx: CollectContext, payload: Dict[str, Any]) -> None:
        """第一页打一次实际键名。接口改版时这行日志能直接定位问题。"""
        if self._shape_logged or not payload:
            return
        self._shape_logged = True
        sample = self._extract_list(payload)
        if not sample:
            ctx.log(
                f"[同程] 未从响应里解析出评论列表；顶层键={sorted(payload.keys())[:20]}。"
                f"接口结构可能已变，请把这行日志发回以便对齐 FIELD_MAP。"
            )
            return
        item = sample[0]
        missing = [
            name for name in ("comment_id", "content", "publish_time", "user_name")
            if pick(item, name) in (None, "")
        ]
        if missing:
            ctx.log(
                f"[同程] 以下关键字段没取到值：{missing}；"
                f"单条评论实际键名={sorted(item.keys())}。请把这行日志发回。"
            )

    # ---------------- 字段映射 ----------------
    @staticmethod
    def _commenter_identity(raw: Dict) -> tuple[str, str]:
        """返回 (评论用户ID, ID 来源)。

        实测同程的 homeId 恒为空字符串，接口不暴露任何数字/GUID 形式的用户ID；
        唯一稳定的用户标识是 dpUserName（形如 同程会员_C5813641E07 或
        BD564A346DAED8CD），同一个人的多条评论会重复出现同一个值。

        取值顺序：先按 FIELD_MAP 的 user_id 候选找真正的用户ID
        （homeId / memberId / userId …，接口改版给出真 ID 时能自动用上），
        都取不到才退回用户名，并在 extra_content 里标明 ID 的来源，
        避免下游把用户名当成平台真实 UID。
        """
        for key in FIELD_MAP["user_id"]:
            value = nz.to_text(raw.get(key)).strip()
            if value:
                return value, key
        user_name = nz.to_text(pick(raw, "user_name")).strip()
        if user_name:
            return user_name, "dpUserName"
        return "", ""

    def _to_comment(self, ctx: CollectContext, raw: Dict, sid: str) -> CommentItem:
        images = nz.collect_urls(pick(raw, "images"))
        cs_replies = self._as_list(pick(raw, "cs_replies"))
        sub_comments = self._as_list(pick(raw, "sub_comments"))
        commenter_id, id_source = self._commenter_identity(raw)

        # 同程特有信息全部进 extra_content，一条都不丢
        extra = {
            "sid": sid,
            "dp_guid": pick(raw, "comment_guid"),
            # 说明 commenter_id 是哪来的：homeId 恒空时会退回用户名
            "commenter_id_source": id_source,
            "rating_text": pick(raw, "rating_text"),        # 好评 / 中评 / 差评
            "comment_type": pick(raw, "comment_type"),
            "service_point": pick(raw, "service_point"),
            "score": pick(raw, "score"),
            "user_level": pick(raw, "user_level"),
            "is_elite": raw.get("isElite"),
            "avatar": pick(raw, "avatar"),
            "trip_mode": pick(raw, "trip_mode"),            # 出行方式
            "trip_purpose": pick(raw, "trip_purpose"),      # 出行目的
            "impressions": pick(raw, "impressions"),        # 印象标签
            "item_id": pick(raw, "item_id"),                # 购买的产品
            "item_name": pick(raw, "item_name"),
            "site": pick(raw, "site"),
            "price_desc": pick(raw, "price_desc"),
        }

        return self.new_comment(
            ctx,
            comment_id=nz.to_text(pick(raw, "comment_id")),
            work_id=sid,
            depth=1,
            comment_parent_id="",
            root_comment_id=nz.to_text(pick(raw, "comment_id")),
            commenter_id=commenter_id,
            commenter_name=nz.to_text(pick(raw, "user_name")),
            content=nz.to_text(pick(raw, "content")),
            likes=nz.to_int(pick(raw, "likes")),
            sub_comment_count=len(cs_replies) + len(sub_comments),
            location=nz.to_text(pick(raw, "location")),
            image_list=nz.to_json_list(images),
            publish_time=nz.to_datetime(pick(raw, "publish_time")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json(
                {k: v for k, v in extra.items() if v not in (None, "", [], {})}
            ),
        ).finalize()

    def _iter_replies(
        self, ctx: CollectContext, raw: Dict, sid: str, parent_id: str
    ):
        """把两种回复都转成二级评论，用 reply_type 区分来源。"""
        for reply in self._as_list(pick(raw, "cs_replies")):
            item = self._to_reply(ctx, reply, sid, parent_id, "cs", "同程客服")
            if item is not None:
                yield item
        for reply in self._as_list(pick(raw, "sub_comments")):
            item = self._to_reply(ctx, reply, sid, parent_id, "sub", "")
            if item is not None:
                yield item

    def _to_reply(
        self, ctx: CollectContext, raw: Dict, sid: str, parent_id: str,
        reply_type: str, default_name: str,
    ) -> Optional[CommentItem]:
        content = nz.to_text(pick(raw, "reply_content"))
        reply_id = nz.to_text(pick(raw, "reply_id"))
        if not reply_id:
            if not content:
                return None
            # 回复没有独立 ID 时派生一个。必须用 md5 而不是内置 hash()：
            # Python 对 str 的 hash 每个进程随机加盐，重启后同一条回复会得到
            # 不同 ID，导致重复入库、破坏幂等。
            digest = hashlib.md5(
                f"{parent_id}|{reply_type}|{content}".encode("utf-8")
            ).hexdigest()
            reply_id = f"{parent_id}_r{digest[:12]}"

        return self.new_comment(
            ctx,
            comment_id=reply_id,
            work_id=sid,
            depth=2,
            comment_parent_id=parent_id,
            root_comment_id=parent_id,
            commenter_id=nz.to_text(pick(raw, "user_id")),
            commenter_name=nz.to_text(pick(raw, "reply_user")) or default_name,
            content=content,
            likes=nz.to_int(pick(raw, "likes")),
            location=nz.to_text(pick(raw, "location")),
            image_list=nz.to_json_list(nz.collect_urls(pick(raw, "images"))),
            publish_time=nz.to_datetime(pick(raw, "reply_time")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json({
                "sid": sid,
                "is_reply": True,
                "reply_type": reply_type,   # cs=客服回复，sub=用户追评
            }),
        ).finalize()
