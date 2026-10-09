"""「试搜一下」：拿真实的采集链路跑一个关键字，把每一步的结果摊开给用户看。

为什么要有这个东西：
    平台的失败大多是**静默的**——接口回 200、没有错误码、data 是空的。
    任务日志里只剩「关键字 [x] 采集到 0 条作品」，用户完全无法判断是
    这个词真没内容、还是被风控了、还是登录态不完整、还是参数少了。

    这个诊断跑的就是生产代码本身（同一个采集器、同一套代理与签名），
    只是把中间状态都记下来：Cookie 有几个、页面指纹在不在、
    发了什么请求、HTTP 状态码是多少、响应体前几百字符长什么样。

它不写库，max_works=1，只取一条就停，对平台的打扰接近于零。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..core.constants import CHANNEL_LABELS, CHANNELS_POI_BASED
from ..core.logging import get_logger
from .base import CollectContext, CollectorRegistry, CollectTarget, LoginRequired

logger = get_logger(__name__)


class _Recorder:
    """把 ctx.log 的输出收集起来，一并回给前端。"""

    def __init__(self) -> None:
        self.lines: List[Dict[str, str]] = []

    def log(self, message: str, level: str = "info") -> None:
        self.lines.append({"level": level, "message": str(message)[:600]})

    # CollectContext 期望的接口
    def info(self, message: str) -> None:
        self.log(message, "info")

    def warn(self, message: str) -> None:
        self.log(message, "warn")

    def error(self, message: str) -> None:
        self.log(message, "error")


async def probe_keyword(
    *,
    config,
    proxy_manager,
    browser_manager,
    channel: str,
    keyword: str,
    account_name: str = "",
    target_id: str = "",
) -> Dict[str, Any]:
    """跑一次最小规模的采集，返回诊断报告。"""
    label = CHANNEL_LABELS.get(channel, channel)
    started = time.time()
    recorder = _Recorder()

    report: Dict[str, Any] = {
        "channel": channel,
        "channel_label": label,
        "keyword": keyword,
        "account_name": account_name,
        "ok": False,
        "stage": "创建采集器",
        "found": 0,
        "sample": None,
        "error": "",
        "logs": recorder.lines,
        "proxy": {},
        "http": {},
        "elapsed_seconds": 0.0,
    }

    try:
        collector = CollectorRegistry.create(channel, config, proxy_manager)
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"创建采集器失败：{exc}"
        return report

    if not collector.integrated:
        report["error"] = f"{label} 采集器尚未接入"
        return report

    ctx = CollectContext(
        scenic_id="__probe__",
        scenic_name="连通性自检",
        task_id="probe",
        max_works=1,
        max_comments_per_work=0,
        collect_comments=False,
        enable_sub_comments=False,
        account_name=account_name,
        params={"browser_manager": browser_manager},
        logger=recorder,
    )

    try:
        report["stage"] = "准备登录态与客户端"
        await collector.prepare(ctx)

        client = getattr(collector, "_client", None)
        if client is not None:
            report["proxy"] = {
                "enabled": proxy_manager.enabled_for(channel),
                "proxy_url": _mask_proxy(getattr(client, "proxy_url", None)),
                "fingerprint": getattr(client, "profile_name", ""),
            }

        report["stage"] = "发起搜索请求"
        async for work in _first_items(collector, ctx, channel, keyword, target_id):
            report["found"] += 1
            if report["sample"] is None:
                report["sample"] = {
                    "work_id": work.work_id,
                    "title": (work.title or work.description or "")[:120],
                    "author": work.author_name or work.author_id,
                    "publish_time": str(work.publish_time or ""),
                    "url": work.work_url,
                }
            break

        report["ok"] = report["found"] > 0
        report["stage"] = "完成"
        if not report["ok"]:
            report["error"] = (
                f"请求发出去了，但一条结果都没拿到。"
                f"看下面的日志和响应摘要判断是被风控了还是参数不对。"
            )
    except LoginRequired as exc:
        report["error"] = str(exc)
        report["stage"] = "登录态检查"
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("[试搜] %s / %s 失败：%s", channel, keyword, exc)
    finally:
        client = getattr(collector, "_client", None)
        if client is not None and getattr(client, "last_response", None):
            report["http"] = client.last_response
        try:
            await collector.cleanup()
        except Exception:  # noqa: BLE001
            pass
        report["elapsed_seconds"] = round(time.time() - started, 1)

    return report


async def _first_items(collector, ctx, channel: str, keyword: str, target_id: str):
    """点评型平台没有关键字搜索，走 POI；其余走关键字。"""
    if channel in CHANNELS_POI_BASED:
        if not target_id:
            raise ValueError(
                f"{CHANNEL_LABELS.get(channel, channel)} 是按景区点评采集的，"
                f"请提供 POI_ID / sid 而不是关键字"
            )
        target = CollectTarget(target_type="poi", value=target_id, name=keyword)
        # 点评型平台产出的是评论，包一层让上面的循环能统一处理
        async for comment in collector.collect_by_poi(ctx, target):
            yield _CommentAsWork(comment)
        return

    async for work in collector.collect_by_keyword(ctx, keyword):
        yield work


class _CommentAsWork:
    """让点评型平台的返回也能套进"作品"的展示字段里。"""

    def __init__(self, comment):
        self.work_id = comment.comment_id
        self.title = comment.content
        self.description = comment.content
        self.author_name = comment.commenter_name
        self.author_id = comment.commenter_id
        self.publish_time = comment.publish_time
        self.work_url = ""


def _mask_proxy(url: Optional[str]) -> str:
    """代理地址里带用户名密码，回给前端前要打码。"""
    if not url:
        return ""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        _, _, host = rest.rpartition("@")
        return f"{scheme}://***:***@{host}"
    return url


async def probe_all_channels(
    *,
    config,
    proxy_manager,
    browser_manager,
    keyword: str,
    targets: Optional[Dict[str, str]] = None,
    channels: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """一键体检：把六个平台挨个试搜一遍，返回每个平台的诊断报告。

    串行而不是并发：抖音/快手要开浏览器页面，六个一起开会把机器拖住，
    而且并发请求同一批出口 IP 也容易触发风控。体检本来就不赶时间。
    """
    from ..core.constants import ALL_CHANNELS

    targets = targets or {}
    selected = channels or list(ALL_CHANNELS)
    reports: List[Dict[str, Any]] = []
    for channel in selected:
        if channel not in ALL_CHANNELS:
            continue
        try:
            report = await probe_keyword(
                config=config,
                proxy_manager=proxy_manager,
                browser_manager=browser_manager,
                channel=channel,
                keyword=keyword,
                target_id=targets.get(channel, ""),
            )
        except Exception as exc:  # noqa: BLE001
            report = {
                "channel": channel,
                "channel_label": CHANNEL_LABELS.get(channel, channel),
                "keyword": keyword,
                "ok": False,
                "stage": "体检调度",
                "found": 0,
                "sample": None,
                "error": f"{type(exc).__name__}: {exc}",
                "logs": [],
                "proxy": {},
                "http": {},
                "elapsed_seconds": 0.0,
            }
        reports.append(report)
    return reports
