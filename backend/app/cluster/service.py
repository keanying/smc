# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""集群：心跳、选主、派任务、掉线收尾、设置同步。

每台机器都跑一个 Cluster，每隔 heartbeat_seconds 秒 tick 一次：
    1. 心跳：在机器表里刷新自己（在跑几个任务、上限、浏览器占用）；
    2. 设置同步：别的机器在页面上改了设置，本机合并进来；
    3. 能当主机的（all / master）抢租约，抢到的就是**当前主机**：
       - 把掉线机器上挂着的任务标成中断；
       - 把到点的任务派给从机（规则见 dispatcher.py）。
从机自己不挑任务，只跑派给自己的（scheduler.py 的 _poll）。
单机部署时角色是 all：自己当主机，派给自己。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import __version__
from ..browser.specs import needs_login
from ..core.constants import TaskStatus
from ..core.logging import get_logger
from .dispatcher import AccountPool, NodeSlot, missing_note, needs_of, plan
from .identity import ROLE_LABELS, NodeIdentity, resolve_identity
from .repo import NodeRepository

logger = get_logger(__name__)

#: 同一个任务、同一个"派不出去"的原因，多久才在任务日志里再提一次
NOTE_REPEAT_SECONDS = 600


class Cluster:
    def __init__(self, config, db, *, tasks, accounts, settings,
                 on_settings_changed: Optional[Callable[[List[str]], Awaitable[None]]] = None,
                 identity: Optional[NodeIdentity] = None):
        self.config = config
        self.db = db
        self.me = identity or resolve_identity(config)
        self.nodes = NodeRepository(db)
        self.tasks = tasks
        self.accounts = accounts
        self.settings = settings
        self.on_settings_changed = on_settings_changed
        #: 本机的任务调度器（只有能跑任务的机器才有），派给自己的任务要捅它一下立刻开跑
        self.scheduler = None
        self.started_at = datetime.now().replace(microsecond=0)
        self.is_leader = False
        self._loop_task: Optional[asyncio.Task] = None
        self._digest: Optional[Dict[str, str]] = None
        self._notes: Dict[str, Tuple[str, float]] = {}
        self._tick_lock = asyncio.Lock()
        #: 集群内部令牌（机器之间互相调用时带上），start() 时从库里取
        self.secret = ""

    # ------------------------------------------------------------------ 配置
    @property
    def heartbeat_seconds(self) -> float:
        return max(1.0, float(self.config.get("cluster.heartbeat_seconds", 5) or 5))

    @property
    def offline_seconds(self) -> int:
        return max(10, int(self.config.get("cluster.offline_seconds", 30) or 30))

    @property
    def max_running(self) -> int:
        """本机最多同时跑几个任务。只协调不跑任务的主机是 0。"""
        if not self.me.is_worker_role:
            return 0
        own = int(self.config.get("cluster.max_running_tasks", 0) or 0)
        return own if own > 0 else int(self.config.get("scheduler.max_running_tasks", 3) or 3)

    def auto_dispatch(self) -> bool:
        """「系统设置 → 调度」关掉时，到点的任务不自动派，只能手动点「立即执行」。"""
        return bool(self.config.get("scheduler.enabled", True))

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self.secret = await self.nodes.ensure_secret()
        await self._warn_duplicate()
        if self.me.is_worker_role:
            await self._claim_accounts()
        await self.heartbeat()
        if self.me.is_master_role:
            self.is_leader = await self.nodes.acquire_lease(self.me.node_id, self.offline_seconds)
        self._digest = await self.nodes.settings_digest()
        logger.info(
            "集群：本机 %s（%s），角色 %s%s，对外地址 %s",
            self.me.node_name, self.me.node_id, ROLE_LABELS[self.me.role],
            "，当前是主机" if self.is_leader else "", self.me.advertise_url)
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None
        try:
            if self.is_leader:
                await self.nodes.release_lease(self.me.node_id)
            await self.nodes.mark_stopped(self.me.node_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("集群收尾时出错（忽略）：%s", exc)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 一次失败不能把心跳停掉
                logger.warning("集群心跳 / 派发出错：%s", exc)

    async def tick(self) -> None:
        async with self._tick_lock:
            await self.heartbeat()
            await self._sync_settings()
            if self.me.is_master_role:
                was = self.is_leader
                self.is_leader = await self.nodes.acquire_lease(self.me.node_id, self.offline_seconds)
                if self.is_leader != was:
                    logger.warning("集群：本机%s主机", "成为" if self.is_leader else "不再是")
            if self.is_leader:
                await self.reap_offline()
                if self.auto_dispatch():
                    await self.dispatch_due()

    # ------------------------------------------------------------------ 心跳
    def running_task_ids(self) -> List[str]:
        return sorted(self.scheduler.running_task_ids()) if self.scheduler else []

    async def heartbeat(self) -> None:
        from ..browser.capacity import BROWSER_CAPACITY

        running = self.running_task_ids()
        try:
            browser = BROWSER_CAPACITY.stats()
        except Exception:  # noqa: BLE001
            browser = {}
        await self.nodes.heartbeat(
            self.me, version=__version__, max_running=self.max_running,
            running_count=len(running),
            stats={"running_tasks": running, "browser": browser, "leader": self.is_leader},
            started_at=self.started_at,
        )

    async def _warn_duplicate(self) -> None:
        """两台机器配了同一个编号：心跳互相覆盖、任务派得一团乱。只能提醒，没法替用户改。"""
        row = await self.nodes.get(self.me.node_id)
        if not row or row.get("idle_seconds") is None:
            return
        if int(row["idle_seconds"]) <= self.offline_seconds and row.get("host") != self.me.host:
            logger.error(
                "集群：机器编号 %s 已经被 %s 在用（%s 秒前还有心跳）。"
                "两台机器不能用同一个编号，请在其中一台的 config.yaml 里设 cluster.node_id",
                self.me.node_id, row.get("host"), row["idle_seconds"])

    async def _claim_accounts(self) -> None:
        """升级上来的老账号还没有机器归属：profile 在本机硬盘上的认领过来。

        集群里还没有别的从机时（单机部署 / 第一台升级的机器），全部认领。
        免登录平台的占位账号不认领，哪台都能用。
        """
        rows = await self.accounts.unassigned()
        if not rows:
            return
        others = [n for n in await self.nodes.list(self.offline_seconds)
                  if n["node_id"] != self.me.node_id and n.get("role") in ("all", "worker")]
        claimed = 0
        for row in rows:
            if not needs_login(row["channel"]):
                continue
            profile = str(row.get("profile_dir") or "")
            if others and not (profile and Path(profile).is_dir()):
                continue
            claimed += await self.accounts.set_node(row["channel"], row["account_name"], self.me.node_id)
        if claimed:
            logger.info("集群：%d 个还没分配机器的账号归到本机（%s）", claimed, self.me.node_id)

    # ------------------------------------------------------------------ 设置同步
    async def _sync_settings(self) -> None:
        digest = await self.nodes.settings_digest()
        if self._digest is None:
            self._digest = digest
            return
        changed = sorted(k for k in set(digest) | set(self._digest)
                         if digest.get(k) != self._digest.get(k))
        self._digest = digest
        if not changed:
            return
        await self.settings.apply_overrides(self.config)
        logger.info("集群：别的机器改了设置（%s），本机已同步", "、".join(changed))
        if self.on_settings_changed is not None:
            await self.on_settings_changed(changed)

    async def refresh_settings_digest(self) -> None:
        """本机页面上刚保存过设置：已经生效了，别在下一次心跳时当成"别人改的"再处理一遍。"""
        self._digest = await self.nodes.settings_digest()

    # ------------------------------------------------------------------ 派任务
    async def worker_slots(self) -> List[NodeSlot]:
        nodes = await self.nodes.list(self.offline_seconds)
        load = await self.tasks.load_by_node()
        return [
            NodeSlot(node_id=n["node_id"], node_name=n.get("node_name") or n["node_id"],
                     max_running=int(n.get("max_running") or 0),
                     load=load.get(n["node_id"], 0))
            for n in nodes
            if n["online"] and n.get("role") in ("all", "worker") and int(n.get("accept_tasks") or 0)
        ]

    async def _pool(self, tasks: Sequence[Dict[str, Any]]) -> AccountPool:
        owners: Dict[Tuple[str, str], Optional[str]] = {}
        for task in tasks:
            for need in needs_of(task):
                key = (need.channel, need.preferred)
                if need.preferred and key not in owners:
                    owners[key] = await self.accounts.node_of(*key)
        return AccountPool(await self.accounts.usable_by_node(), owners)

    async def dispatch_due(self) -> int:
        slots = await self.worker_slots()
        candidates = await self.tasks.pending_once_tasks(limit=100)
        candidates += await self.tasks.due_tasks(limit=100)
        seen, todo = set(), []
        for task in candidates:
            if task["task_id"] not in seen:
                seen.add(task["task_id"])
                todo.append(task)
        if not todo:
            return 0
        pool = await self._pool(todo)
        sent = 0
        for task in todo:
            node, reason = plan(task, slots, pool)
            if node is None:
                await self._note(task["task_id"], reason)
                continue
            if await self._send(task, node, pool):
                sent += 1
        return sent

    async def dispatch_now(self, task_id: str) -> Optional[NodeSlot]:
        """「立即执行」：不等主机下一轮，当场挑机器派出去。任务已经在排队 / 在跑返回 None。"""
        task = await self.tasks.get(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        if task.get("status") in (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value):
            return None
        pool = await self._pool([task])
        node, reason = plan(task, await self.worker_slots(), pool)
        if node is None:
            raise RuntimeError(f"现在没法执行：{reason}")
        return node if await self._send(task, node, pool) else None

    async def _send(self, task: Dict[str, Any], node: NodeSlot, pool: AccountPool) -> bool:
        task_id = task["task_id"]
        if not await self.tasks.claim_for_node(task_id, node.node_id):
            return False
        node.load += 1
        self._notes.pop(task_id, None)
        message = f"派给机器：{node.node_name}（{node.node_id}）"
        lacking = missing_note(task, node, pool)
        await self._task_log(task_id, "INFO", message + (f"。{lacking}" if lacking else ""))
        logger.info("集群：任务 %s（%s）派给 %s", task.get("task_name"), task_id, node.node_id)
        if node.node_id == self.me.node_id and self.scheduler is not None:
            self.scheduler.poke()
        return True

    async def _note(self, task_id: str, reason: str) -> None:
        last = self._notes.get(task_id)
        now = time.monotonic()
        if last and last[0] == reason and now - last[1] < NOTE_REPEAT_SECONDS:
            return
        self._notes[task_id] = (reason, now)
        await self._task_log(task_id, "WARN", f"等待派发：{reason}")

    async def _task_log(self, task_id: str, level: str, message: str) -> None:
        try:
            await self.tasks.add_logs([{"task_id": task_id, "level": level, "message": message}])
        except Exception as exc:  # noqa: BLE001
            logger.debug("写任务日志失败（忽略）：%s", exc)

    # ------------------------------------------------------------------ 掉线收尾
    async def reap_offline(self) -> int:
        nodes = await self.nodes.list(self.offline_seconds)
        online = [n["node_id"] for n in nodes if n["online"]]
        names = {n["node_id"]: n.get("node_name") or n["node_id"] for n in nodes}
        reaped = 0
        for task in await self.tasks.orphaned(online):
            node = task.get("node_id") or ""
            msg = (f"机器 {names.get(node, node)} 掉线（超过 {self.offline_seconds} 秒没有心跳），"
                   f"任务中断")
            await self.tasks.mark_finished(task["task_id"], TaskStatus.FAILED.value, error=msg)
            await self._task_log(task["task_id"], "ERROR", msg)
            try:
                await self.tasks.reschedule(task)
            except Exception as exc:  # noqa: BLE001
                logger.warning("掉线任务 %s 重新排期失败：%s", task["task_id"], exc)
            reaped += 1
        if reaped:
            logger.warning("集群：%d 个任务所在机器已掉线，已标为中断", reaped)
        return reaped

    # ------------------------------------------------------------------ 页面
    async def overview(self) -> Dict[str, Any]:
        nodes = await self.nodes.list(self.offline_seconds)
        load = await self.tasks.load_by_node()
        accounts: Dict[str, Dict[str, Dict[str, int]]] = {}
        for row in await self.accounts.count_by_node():
            per = accounts.setdefault(str(row["node_id"] or ""), {})
            per[row["channel"]] = {"total": int(row["total"] or 0), "usable": int(row["usable"] or 0)}
        leader = await self.nodes.lease_holder()
        items = []
        for n in nodes:
            items.append({
                "node_id": n["node_id"], "node_name": n.get("node_name") or n["node_id"],
                "role": n.get("role"), "role_label": ROLE_LABELS.get(n.get("role"), n.get("role")),
                "advertise_url": n.get("advertise_url"), "host": n.get("host"),
                "version": n.get("version"), "online": n["online"],
                "idle_seconds": n.get("idle_seconds"), "heartbeat_at": n.get("heartbeat_at"),
                "started_at": n.get("started_at"),
                "accept_tasks": bool(int(n.get("accept_tasks") or 0)),
                "max_running": int(n.get("max_running") or 0),
                "running_count": int(n.get("running_count") or 0),
                "load": load.get(n["node_id"], 0),
                "stats": n.get("stats") or {},
                "accounts": accounts.get(n["node_id"], {}),
                "is_self": n["node_id"] == self.me.node_id,
                "is_leader": n["node_id"] == leader,
            })
        return {
            "self": {"node_id": self.me.node_id, "node_name": self.me.node_name,
                     "role": self.me.role, "role_label": ROLE_LABELS[self.me.role],
                     "is_leader": self.is_leader, "version": __version__},
            "leader": leader,
            "offline_seconds": self.offline_seconds,
            "auto_dispatch": self.auto_dispatch(),
            "nodes": items,
            "unassigned_accounts": accounts.get("", {}),
        }

    async def node_url(self, node_id: str) -> Tuple[str, str]:
        """别的机器的访问地址。不在线返回 ('', 原因)。"""
        row = await self.nodes.get(node_id)
        if not row:
            return "", f"机器 {node_id} 不存在"
        idle = row.get("idle_seconds")
        if idle is None or int(idle) > self.offline_seconds:
            return "", f"机器 {row.get('node_name') or node_id} 不在线"
        if not row.get("advertise_url"):
            return "", f"机器 {node_id} 没有登记访问地址（cluster.advertise_url）"
        return str(row["advertise_url"]).rstrip("/"), ""

    async def online_workers(self) -> List[Dict[str, Any]]:
        return [n for n in await self.nodes.list(self.offline_seconds)
                if n["online"] and n.get("role") in ("all", "worker")]


_cluster: Optional[Cluster] = None


def set_cluster(cluster: Optional[Cluster]) -> None:
    global _cluster
    _cluster = cluster


def get_cluster() -> Optional[Cluster]:
    return _cluster
