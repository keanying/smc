# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""盒子这边：调主的接口。

RemoteDataRepository 顶替采集用的 DataRepository：写作品 / 评论 / 作者、查「今天采过哪些」
「人工删过哪些」，全都打到主上，盒子本地库里不存采到的数据。

主连不上：重试几次（网络抖一下不至于丢数据），还不行就抛 MasterUnavailable——
这批数据没存上，任务按失败收尾，日志里写清楚是连不上主。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Sequence, Tuple
from urllib.parse import quote

from ..core.logging import get_logger
from .codec import decode, encode

logger = get_logger(__name__)

TOKEN_HEADER = "X-SMC-Box-Token"
USER_HEADER = "X-SMC-Box-User"
#: 连不上 / 主返回 5xx 时的重试间隔（秒）。总共等半分钟左右
RETRY_DELAYS = (2, 5, 10, 15)
TIMEOUT_SECONDS = 60


class MasterUnavailable(RuntimeError):
    """连不上主，或者主拒绝了（令牌不对、景区没分给这个盒子……）。"""


class MasterClient:
    def __init__(self, master_url: str, token: str, *, retry_delays: Sequence[float] = RETRY_DELAYS,
                 transport=None):
        self.master_url = (master_url or "").rstrip("/")
        self.token = token or ""
        self.retry_delays = tuple(retry_delays)
        self.transport = transport          # 测试里换成假的，不真的连网

    @property
    def configured(self) -> bool:
        return bool(self.master_url and self.token)

    def headers(self, user: str = "") -> Dict[str, str]:
        out = {TOKEN_HEADER: self.token}
        if user:
            out[USER_HEADER] = quote(user)
        return out

    async def request(self, method: str, path: str, *, json: Any = None, retry: bool = True) -> Any:
        """调主，返回 data。业务错误（4xx）不重试，直接报；网络错误 / 5xx 重试。"""
        import httpx

        if not self.configured:
            raise MasterUnavailable("还没配主的地址和盒子令牌（config.yaml 的 edition.master_url / edition.box_token）")
        url = self.master_url + path
        delays = self.retry_delays if retry else ()
        last_error = ""
        for attempt in range(len(delays) + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5),
                                             transport=self.transport) as client:
                    resp = await client.request(method, url, json=json, headers=self.headers())
                body = _json_or_none(resp)
                if resp.status_code < 500:
                    if resp.status_code >= 400 or (body or {}).get("code", 0) != 0:
                        message = (body or {}).get("message") or resp.text[:200]
                        raise MasterUnavailable(f"主拒绝了（{resp.status_code}）：{message}")
                    return (body or {}).get("data")
                last_error = f"主返回 {resp.status_code}：{((body or {}).get('message') or resp.text)[:200]}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < len(delays):
                logger.warning("连主（%s）失败，%s 秒后重试：%s", self.master_url, delays[attempt], last_error)
                await asyncio.sleep(delays[attempt])
        raise MasterUnavailable(f"连不上主（{self.master_url}）：{last_error}")


def _json_or_none(resp) -> Optional[Dict[str, Any]]:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class RemoteDataRepository:
    """采集写数据的那几个方法，打到主上（主那边见 api/box_api.py）。"""

    #: runner 看到它就不在本地推标注——标注在主那边做
    remote = True

    def __init__(self, client: MasterClient, db):
        self.client = client
        #: 账号配额等**本地**的表还在盒子库里（runner 拿 data.db 建 AccountQuota）
        self.db = db

    async def _call(self, method: str, **payload) -> Any:
        return decode(await self.client.request(
            "POST", f"/api/box/data/{method}", json=encode(payload)))

    async def save_works(self, items) -> Tuple[int, int]:
        if not items:
            return 0, 0
        new, updated = await self._call("save_works", items=list(items))
        return int(new), int(updated)

    async def merge_work_hits(self, items) -> int:
        return int(await self._call("merge_work_hits", items=list(items))) if items else 0

    async def save_comments(self, items, *, skip_existing: bool = True) -> Tuple[int, int]:
        if not items:
            return 0, 0
        new, updated = await self._call("save_comments", items=list(items), skip_existing=skip_existing)
        return int(new), int(updated)

    async def save_authors(self, items) -> int:
        return int(await self._call("save_authors", items=list(items))) if items else 0

    async def work_ids_collected_since(self, scenic_id: str, channel: str, since: datetime) -> set:
        return set(await self._call("work_ids_collected_since", scenic_id=scenic_id,
                                    channel=channel, since=since))

    async def deleted_work_keys(self, scenic_id: str, channel: str) -> set:
        return set(await self._call("deleted_work_keys", scenic_id=scenic_id, channel=channel))

    def invalidate_overview_cache(self) -> None:
        """概览缓存在主上，主收到上报时自己会处理。"""
