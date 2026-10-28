# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""本机在集群里的身份：编号、角色、别的机器怎么访问到我。

都来自 config 的 `cluster` 段（每台机器自己的 config.yaml / 环境变量），
不走页面设置——页面设置存在共享库里，所有机器读到的是同一份，没法区分"我是谁"。
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass

ROLES = ("all", "master", "worker")
ROLE_LABELS = {"all": "主 + 从", "master": "主", "worker": "从"}


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    node_name: str
    role: str
    advertise_url: str
    host: str
    pid: int

    @property
    def is_master_role(self) -> bool:
        """能当主机（派任务、跑全局只该有一份的后台任务）。"""
        return self.role in ("all", "master")

    @property
    def is_worker_role(self) -> bool:
        """能跑采集任务。"""
        return self.role in ("all", "worker")


def resolve_identity(config) -> NodeIdentity:
    role = str(config.get("cluster.role", "all") or "all").strip().lower()
    if role not in ROLES:
        raise ValueError(f"cluster.role 只能是 all / master / worker，现在是：{role}")
    host = socket.gethostname() or "localhost"
    node_id = str(config.get("cluster.node_id", "") or "").strip() or host
    node_name = str(config.get("cluster.node_name", "") or "").strip() or node_id
    advertise = str(config.get("cluster.advertise_url", "") or "").strip().rstrip("/")
    if not advertise:
        port = int(config.get("server.port", 8000) or 8000)
        advertise = f"http://{local_ip()}:{port}"
    return NodeIdentity(node_id=node_id[:100], node_name=node_name[:100], role=role,
                        advertise_url=advertise, host=host[:200], pid=os.getpid())


def local_ip() -> str:
    """本机的内网 IP。UDP connect 不发包，只是让系统选一块出网的网卡。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
