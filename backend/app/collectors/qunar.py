"""去哪儿景区点评采集器。

和携程/同程一样是 **POI 型**平台：没有"作品"的概念，只有挂在景区下面的点评。
所以同样给每个 POI 造一条"合成作品"当挂载点，让数据中心的
景区 → 平台 → 作品 → 评论 这棵树对去哪儿也成立。

解析从哪来
----------
页面解析（HTML + 内嵌 JSON + JSON-LD 兜底）**原样引入**自用户提供的
`qunar_sight_crawler`，放在 `backend/vendor/qunar_crawler/`，一行没改。
理由见那边的 NOTICE.md：去哪儿改版时，页面结构知识是唯一要重写的东西，
保持原样意味着上游出新版直接替换文件，这里不用动。

这一层换掉了什么
----------------
上游是一个命令行工具：裸 `requests.Session` + 固定延时 + CSV 落盘。
接进来之后：

  1. HTTP 换成 `ProxiedClient` —— 和其它平台同一套代理池、指纹绑定、
     风控降级（被封自动换 IP）
  2. 节奏换成 `crawl.pace` 的平台配置，不再是固定 delay
  3. 输出换成统一的 WorkItem / CommentItem，直接进现有的评论表，
     **AI 标注、人工复核、去重水印全都自动适用**，不用为去哪儿单独做
  4. 加了取消检查、条数/页数上限、错误归因日志

⚠️ 这个数据源的两个限制，写在 extra_content 里也说在这儿：
   · **点赞数拿不到**，页面上不发布，一律是 0。别当成"没人点赞"。
   · **没有二级评论**，去哪儿点评页不展示回复。所以 enable_sub_comments
     对这个平台不起作用，不是没实现。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
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

#: vendor 目录进 sys.path。和标注引擎同一个做法：
#: 原样引入、不拷进 app/，上游出新版直接替换整个目录。
VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor"


def _ensure_vendor_on_path() -> None:
    root = str(VENDOR_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


BASE_URL = "https://sight.qunar.com"
SITE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    "Referer": f"{BASE_URL}/",
}


@CollectorRegistry.register
class QunarCollector(BaseCollector):
    channel = "qunar"
    supports_works = False        # 没有"作品"，只有景区点评
    needs_login = False
    supported_targets = ("poi",)

    #: 做成类属性，离线测试时指向本地假站点
    base_url = BASE_URL

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._client = None

    # ---------------- 生命周期 ----------------
    async def prepare(self, ctx: CollectContext) -> None:
        _ensure_vendor_on_path()
        self._client = self.make_client(base_headers=dict(SITE_HEADERS))
        await self._client.__aenter__()
        ctx.log(f"[去哪儿] 采集客户端就绪，指纹 {self._client.profile_name}")

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------- 合成作品 ----------------
    def poi_work(self, ctx: CollectContext, target: CollectTarget) -> WorkItem:
        """为一个 POI 生成一条合成作品，作为它下面所有点评的挂载点。"""
        poi_id = str(target.value)
        return self.new_work(
            ctx,
            work_id=poi_id,
            work_url=self.poi_homepage(target, f"{self.base_url}/{poi_id}"),
            author_id="qunar",
            author_name="去哪儿",
            title=f"{target.name or ctx.scenic_name} · 去哪儿点评",
            description=f"去哪儿景区 {poi_id} 的点评集合",
            location=ctx.scenic_name,
            publish_time=None,
            crawl_time=datetime.now(),
            extra_content=json.dumps(
                {"synthetic": True, "poi_id": poi_id, "source": "qunar"},
                ensure_ascii=False,
            ),
        ).finalize()

    # ---------------- 采集 ----------------
    async def collect_by_poi(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[CommentItem]:
        _ensure_vendor_on_path()
        from qunar_crawler.parsers import parse_comment_page

        poi_id = str(target.value)
        max_pages = int(ctx.params.get("max_pages") or 0)
        emitted = 0
        page = 1
        seen: set = set()
        scenic_name = ""
        total_pages: Optional[int] = None

        while True:
            ctx.raise_if_cancelled()
            if max_pages > 0 and page > max_pages:
                ctx.log(f"[去哪儿] 景区 {poi_id} 已达页数上限 {max_pages}")
                break
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                ctx.log(f"[去哪儿] 景区 {poi_id} 已达评论条数上限 "
                        f"{ctx.max_comments_per_work}")
                break

            html = await self._fetch_comment_page(ctx, poi_id, page)
            if html is None:
                break

            try:
                parsed = parse_comment_page(html, poi_id, scenic_name=scenic_name)
            except Exception as exc:      # noqa: BLE001
                ctx.log(f"[去哪儿] 景区 {poi_id} 第 {page} 页解析失败：{exc}。"
                        f"多半是页面改版了——去 vendor/qunar_crawler/parsers.py 看看",
                        "warn")
                break

            scenic_name = scenic_name or parsed.scenic_name
            if total_pages is None and parsed.total_pages:
                total_pages = parsed.total_pages
                ctx.log(f"[去哪儿] 景区 {poi_id}（{scenic_name or '未取到名字'}）"
                        f"共 {total_pages} 页点评")

            rows = parsed.comments or []
            if not rows:
                ctx.log(f"[去哪儿] 景区 {poi_id} 第 {page} 页没有点评，采集结束")
                break

            new_in_page = 0
            for raw in rows:
                comment_id = str(raw.get("comment_id") or "")
                if not comment_id or comment_id in seen:
                    continue
                seen.add(comment_id)
                new_in_page += 1
                item = self.safe_map(ctx, self._to_comment, ctx, raw, poi_id,
                                     what="点评")
                if item is not None:
                    yield item
                    emitted += 1
                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            ctx.log(f"[去哪儿] 景区 {poi_id} 第 {page} 页：本页 {len(rows)} 条，"
                    f"新增 {new_in_page} 条，累计 {emitted} 条")

            if new_in_page == 0:
                # ⚠️ 去哪儿的分页翻过头会**把最后一页原样再给一遍**，不是返回空。
                #    只判空的话会在最后一页上无限循环，日志看着还一直在"采集中"。
                ctx.log(f"[去哪儿] 景区 {poi_id} 第 {page} 页全是重复的，采集结束")
                break
            if total_pages and page >= total_pages:
                ctx.log(f"[去哪儿] 景区 {poi_id} 已翻到最后一页（{total_pages}）")
                break

            page += 1
            await self._client.sleep_interval()

    async def _fetch_comment_page(
        self, ctx: CollectContext, poi_id: str, page: int
    ) -> Optional[str]:
        """取一页点评的 HTML。代理异常由 ProxiedClient 处理，这里只管业务重试。"""
        url = f"{self.base_url}/{poi_id}/comment"
        max_retries = int(self.config.get("crawl.max_retries", 3))
        last: Optional[Exception] = None
        for attempt in range(max_retries):
            ctx.raise_if_cancelled()
            try:
                html = await self._client.get_text(url, params={"pageNum": page})
            except Exception as exc:      # noqa: BLE001
                last = exc
                ctx.log(f"[去哪儿] 取第 {page} 页失败（第 {attempt + 1} 次）：{exc}",
                        "warn")
                await self._client.sleep_interval()
                continue

            if self._looks_like_verification(html):
                # ⚠️ 撞到验证页就**停**，不绕。绕过验证是另一回事，
                #    而且继续硬打只会让这个 IP 更快被拉黑。
                #    换 IP 重试一次是合理的，换完还是验证页就收工。
                ctx.log(f"[去哪儿] 第 {page} 页返回的是验证页，换 IP 重试", "warn")
                await self._client.rotate("撞到验证页")
                try:
                    html = await self._client.get_text(url, params={"pageNum": page})
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"[去哪儿] 换 IP 后仍失败：{exc}", "warn")
                    return None
                if self._looks_like_verification(html):
                    ctx.log("[去哪儿] 换 IP 后还是验证页，这一轮先收了——"
                            "多半是采太快或者出口 IP 段被盯上了", "warn")
                    return None
            return html
        ctx.log(f"[去哪儿] 第 {page} 页重试 {max_retries} 次仍失败：{last}", "warn")
        return None

    @staticmethod
    def _looks_like_verification(html: str) -> bool:
        """撞没撞上验证页。

        只看前 2000 个字符：验证页是个极简页面，标记一定在开头；
        而正常点评页的正文里完全可能出现"验证码"三个字（用户在吐槽别的网站），
        全文搜会把正常页面误判成验证页，表现是"明明有数据却一条都不采"。
        """
        head = (html or "")[:2000]
        return any(mark in head for mark in ("访问验证", "安全验证", "请输入验证码"))

    # ---------------- 字段映射 ----------------
    def _to_comment(self, ctx: CollectContext, raw: Dict[str, Any],
                    poi_id: str) -> CommentItem:
        """vendor 解析出来的行 → 统一的 CommentItem。

        上游那份 row 的字段名和本系统的评论表**几乎完全一致**（连
        label_review_flag 都有），所以这里基本是平移，只做三件事：
        类型归一、把去哪儿特有的信息塞进 extra_content、补上 scenic 归属。
        """
        extra: Dict[str, Any] = {}
        try:
            extra = json.loads(raw.get("extra_content") or "{}")
        except (ValueError, TypeError):
            extra = {}
        extra.setdefault("source", "qunar")
        # 把数据源限制写进每一条记录：以后有人问"去哪儿的点赞怎么全是 0"，
        # 翻一条数据就能看到答案，不用去翻代码或者文档
        extra["likes_unavailable"] = True
        extra["replies_unavailable"] = True

        return self.new_comment(
            ctx,
            work_id=poi_id,
            comment_id=nz.to_text(raw.get("comment_id")),
            content=nz.to_text(raw.get("content")),
            commenter_id=nz.to_text(raw.get("commenter_id")),
            commenter_name=nz.to_text(raw.get("commenter_name")),
            likes=0,                       # 页面不发布，见上面的说明
            location=nz.to_text(raw.get("location")),
            image_list=nz.to_text(raw.get("image_list")) or "[]",
            video_list="[]",
            publish_time=nz.to_datetime(raw.get("publish_time")),
            crawl_time=datetime.now(),
            # ⚠️ 不传 comment_level：new_comment 自己按 depth 生成，
            #    再传一次是重复关键字参数，直接 TypeError。
            comment_parent_id="",
            root_comment_id=nz.to_text(raw.get("comment_id")),
            sub_comment_count=0,
            extra_content=json.dumps(extra, ensure_ascii=False),
        ).finalize()
