# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""这一轮该启动哪些任务、按什么顺序：**谁先排队谁先跑，后来的排在后面**。

候选有三种：
  1. 新建后立即执行的一次性任务
  2. 到点的定时任务，以及到了预定恢复时间的「排队等账号」任务
  3. 还没到预定恢复时间、但**现在已经有号能用**的「排队等账号」任务
     ——以前要干等到预定的那个点（按冷却最早什么时候结束算的），中间号空出来了也不动，
       反倒是后来到点的新任务先把号用上了

排序统一按「什么时候开始等的」：排队的看入队时间（queued_at），定时的看计划时间，
新建的看创建时间。单机调度（scheduler._poll）和集群派发（cluster.dispatch_due）共用这一份。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from ..core.logging import get_logger

logger = get_logger(__name__)

_FAR_FUTURE = datetime(9999, 1, 1)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("T", " ")[:19])
        except ValueError:
            pass
    return _FAR_FUTURE


def waited_since(task: Dict[str, Any]) -> datetime:
    """这个任务从什么时候开始等：入队时间 → 计划时间 → 创建时间。"""
    for key in ("queued_at", "next_run_time", "create_time"):
        value = task.get(key)
        if value:
            return _as_datetime(value)
    return _FAR_FUTURE


def fifo(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去重后按「开始等的时间」排：先来先跑。时间一样按 id。"""
    seen, unique = set(), []
    for task in tasks:
        if task["task_id"] not in seen:
            seen.add(task["task_id"])
            unique.append(task)
    return sorted(unique, key=lambda t: (waited_since(t), int(t.get("id") or 0)))


async def accounts_free_for(accounts: Any, task: Dict[str, Any]) -> bool:
    """这个任务要用的号（平台 / 点名的账号 / 分组）现在有没有不在冷却的。"""
    from ..cluster.dispatcher import needs_of      # 放这儿：cluster 那边也会引用本模块
    needs = needs_of(task)
    if not needs or accounts is None or not hasattr(accounts, "cooling_summary"):
        return False                     # 不靠账号排队的（或者查不了），照预定时间来
    for need in needs:
        summary = await accounts.cooling_summary(
            need.channel, preferred=need.preferred, group=need.group, local=False)
        if not int(summary.get("available") or 0):
            return False
    return True


async def ready_tasks(tasks_repo: Any, accounts: Any, *, limit: int) -> List[Dict[str, Any]]:
    """这一轮可以启动的任务，先来先跑。"""
    candidates = await tasks_repo.pending_once_tasks(limit=limit)
    candidates += await tasks_repo.due_tasks(limit=limit)
    try:
        for task in await tasks_repo.waiting_not_due(limit=limit):
            if await accounts_free_for(accounts, task):
                candidates.append(task)
    except Exception as exc:  # noqa: BLE001 —— 查不了就按预定时间来，不影响别的任务
        logger.warning("检查排队任务能否提前开始失败：%s", exc)
    return fifo(candidates)


def holds_browser_all_run(config: Any, task: Dict[str, Any], channel: str) -> bool:
    """这个任务在这个平台上是不是要**从头到尾占着一个浏览器**。

    与 runner 里的判断一致：要登录的平台，且选了拟人模式；快手接口模式也要常驻页面算签名。
    """
    from ..browser.specs import needs_login
    from ..core import collect_params
    from ..core.constants import CHANNEL_KUAISHOU

    if not needs_login(channel):
        return False
    if channel == CHANNEL_KUAISHOU:
        return True
    try:
        engine = str(collect_params.resolve(config, task.get("params") or {}, channel)
                     .get("collect_engine") or "hybrid").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return {"browser": "human"}.get(engine, engine) == "human"


async def browser_blocked(task: Dict[str, Any], *, config: Any, accounts: Any, slots: Any,
                          capacity: Any = None) -> bool:
    """要常驻浏览器、但它要用的号的浏览器现在全被占着（或者浏览器总名额满了）。

    这种任务现在启动只会占着一个运行名额去排队等浏览器，别的平台有空号的任务反而排不上——
    先不启动它（位置不变），把名额让给现在就能跑的；浏览器一空，下一轮按先后启动它。
    """
    from ..cluster.dispatcher import needs_of

    if accounts is None or slots is None:
        return False
    for need in needs_of(task):
        if not holds_browser_all_run(config, task, need.channel):
            continue
        if capacity is not None:
            try:
                stats = capacity.stats()
                if int(stats.get("active") or 0) >= int(stats.get("limit") or 0) > 0:
                    return True
            except Exception:  # noqa: BLE001
                pass
        try:
            names = await accounts.active_names(
                need.channel, preferred=need.preferred, group=need.group)
        except Exception:  # noqa: BLE001
            continue
        if names and not slots.free_accounts(need.channel, names):
            return True
    return False
