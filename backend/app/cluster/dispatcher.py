# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主机派任务：一个到点的任务该交给哪台从机。

规则（从上往下，前面的是硬条件）：
1. 只派给在线、能跑任务（角色 all / worker）、页面上没关「接新任务」的机器；
2. 任务指定了机器（params.node_id）就只派那台；
3. 任务点名了账号，就只派给那个账号所在的机器——号的登录态在那台的硬盘上；
4. 其余需要登录的平台，挑「有能用的号」覆盖的平台最多的机器。
   覆盖最多的那几台都满了，就**等**它们空出来，不派给号不全的空闲机器
   （派过去也是排队等号，还占着那台的位子）；
5. 同样合适的几台里，挑最闲的（已派任务数 / 上限 最小）。

一个任务整条派给一台机器，不拆平台：日志、进度、统计、取消都是按任务算的。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..browser.specs import needs_login
from ..core import collect_params
from ..core.constants import CHANNEL_LABELS


@dataclass
class NodeSlot:
    node_id: str
    node_name: str
    max_running: int
    load: int            # 已派出（排队 + 在跑）的任务数

    @property
    def free(self) -> int:
        return self.max_running - self.load

    @property
    def ratio(self) -> float:
        return self.load / self.max_running if self.max_running > 0 else 1.0


@dataclass(frozen=True)
class Need:
    """任务在某个要登录的平台上要什么号。"""
    channel: str
    preferred: str = ""     # 点名的账号
    group: str = ""         # 账号分组


def needs_of(task: Dict[str, Any]) -> List[Need]:
    params = task.get("params") or {}
    out = []
    for channel in task.get("channels") or []:
        if not needs_login(channel):
            continue
        own = collect_params.channel_overrides(params, channel)
        out.append(Need(
            channel=channel,
            preferred=str(own.get("account_name") or params.get("account_name") or ""),
            group=str(own.get("account_group") or params.get("account_group") or ""),
        ))
    return out


class AccountPool:
    """每台机器上能用的号（node_id='' 的号还没分配机器，哪台都算）。"""

    def __init__(self, usable_rows: Iterable[Dict[str, Any]],
                 owners: Optional[Dict[Tuple[str, str], Optional[str]]] = None):
        self._count: Dict[Tuple[str, str, str], int] = {}
        for row in usable_rows:
            key = (str(row.get("node_id") or ""), str(row["channel"]),
                   str(row.get("account_group") or "default"))
            self._count[key] = self._count.get(key, 0) + int(row.get("c") or 0)
        #: (channel, account_name) -> 账号所在机器；None = 账号不存在
        self.owners = owners or {}

    def usable(self, node_id: str, need: Need) -> int:
        total = 0
        for (node, channel, group), count in self._count.items():
            if channel != need.channel or node not in (node_id, ""):
                continue
            if need.group and group != need.group:
                continue
            total += count
        return total

    def owner_ok(self, node_id: str, need: Need) -> bool:
        owner = self.owners.get((need.channel, need.preferred))
        # 账号不存在 / 没分配机器：不拦，让任务照常跑（runner 会报出具体问题）
        return owner is None or owner in ("", node_id)


def plan(task: Dict[str, Any], nodes: List[NodeSlot],
         pool: AccountPool) -> Tuple[Optional[NodeSlot], str]:
    """返回 (派给哪台, 没派的原因)。"""
    if not nodes:
        return None, "没有在线、能接任务的从机"
    params = task.get("params") or {}
    candidates = list(nodes)

    pinned = str(params.get("node_id") or "")
    if pinned:
        candidates = [n for n in candidates if n.node_id == pinned]
        if not candidates:
            return None, f"任务指定的机器 {pinned} 不在线或不接新任务"

    needs = needs_of(task)
    for need in needs:
        if not need.preferred:
            continue
        candidates = [n for n in candidates if pool.owner_ok(n.node_id, need)]
        if not candidates:
            owner = pool.owners.get((need.channel, need.preferred)) or "?"
            label = CHANNEL_LABELS.get(need.channel, need.channel)
            return None, f"{label}账号 [{need.preferred}] 在机器 {owner} 上，那台不在线或不接新任务"

    open_needs = [n for n in needs if not n.preferred]

    def coverage(node: NodeSlot) -> int:
        return sum(1 for need in open_needs if pool.usable(node.node_id, need) > 0)

    best = max(coverage(n) for n in candidates)
    fitting = [n for n in candidates if coverage(n) == best]
    free = [n for n in fitting if n.free > 0]
    if not free:
        names = "、".join(n.node_name for n in fitting)
        return None, f"合适的机器（{names}）都在满负荷跑，等空位"
    return min(free, key=lambda n: (n.ratio, n.load, n.node_id)), ""


def missing_note(task: Dict[str, Any], node: NodeSlot, pool: AccountPool) -> str:
    """派出去了，但这台机器上有的平台没有能用的号：提前在日志里说一声。"""
    lacking = [CHANNEL_LABELS.get(n.channel, n.channel) for n in needs_of(task)
               if not n.preferred and pool.usable(node.node_id, n) == 0]
    if not lacking:
        return ""
    return f"能派的机器上都没有能用的{'、'.join(lacking)}账号，这几个平台会排队等号或提示登录"
