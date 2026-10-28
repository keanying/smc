# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""集群 API：看各台机器的状态，开关某台接不接新任务，清理已下线的机器。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..cluster import get_cluster
from .common import bad_request, not_found, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/cluster", tags=["集群"])


def _cluster():
    cluster = get_cluster()
    if cluster is None:
        raise bad_request("集群还没启动")
    return cluster


@router.get("")
async def overview():
    """所有机器：角色、在线、心跳、在跑几个任务、各平台有几个号。"""
    return ok(await _cluster().overview())


@router.put("/nodes/{node_id}/accept")
async def set_accept(node_id: str, on: bool = Query(...)):
    """开关这台机器接不接新任务。关掉后已经在跑的照常跑完，主机不再往这台派新的——维护机器前用。"""
    if not await _cluster().nodes.set_accept(node_id, on):
        raise not_found(f"机器不存在：{node_id}")
    return ok(message="已恢复接收新任务" if on else "已暂停接收新任务，在跑的会跑完")


@router.delete("/nodes/{node_id}")
async def delete_node(node_id: str, state: AppState = Depends(get_state)):
    """从列表里删掉一台已经下线的机器。它名下的账号要先迁到别的机器上，不然没人能用。"""
    cluster = _cluster()
    row = await cluster.nodes.get(node_id)
    if not row:
        raise not_found(f"机器不存在：{node_id}")
    idle = row.get("idle_seconds")
    if idle is not None and int(idle) <= cluster.offline_seconds:
        raise bad_request("这台机器还在线，不能删除；先把它的服务停掉")
    listing = await state.accounts.list(node=node_id, page_size=1)
    if listing["total"]:
        raise bad_request(f"这台机器上还有 {listing['total']} 个账号，先在账号管理里把它们迁到别的机器")
    await cluster.nodes.delete(node_id)
    return ok(message="已删除")
