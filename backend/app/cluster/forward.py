# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""把只能在某台机器上做的事，转给那台机器。

账号的浏览器 profile 在它所在机器的硬盘上，扫码登录、采集 Cookie、导入 Cookie、
退出登录这些都只能在那台机器上做；任务的实时日志和实时画面也只在跑它的那台机器的内存里。
页面统一连主机（或者随便哪一台），这一层看请求是关于哪个账号 / 哪个任务的，
不在本机就原样转给那台机器，再把结果原样回给页面。页面不需要知道有几台机器。

转发的请求带 X-SMC-Forwarded 头，对方收到后不会再转，避免两台机器互相踢皮球。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

from ..core.logging import get_logger
from .service import Cluster, get_cluster

logger = get_logger(__name__)

FORWARD_HEADER = b"x-smc-forwarded"
HTTP_TIMEOUT_SECONDS = 300          # 打开登录浏览器、批量采集 Cookie 都可能要等一会
_HOP_HEADERS = {b"host", b"content-length", b"connection", b"transfer-encoding",
                b"keep-alive", b"upgrade", b"accept-encoding"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection"}

#: /api/accounts/{channel}/{account}/{动作}：这些动作要碰本机的浏览器 profile
_ACCOUNT_ACTION = re.compile(
    r"^/api/accounts/(?P<channel>[^/]+)/(?P<name>[^/]+)/"
    r"(?P<action>check|cookies|harvest|probe|logout|login-session)$")
_ACCOUNT = re.compile(r"^/api/accounts/(?P<channel>[^/]+)/(?P<name>[^/]+)$")
_LOGIN_SESSION = re.compile(r"^/api/accounts/login-session/(?P<sid>[^/]+)(?:/save)?$")
_WS_BROWSER = re.compile(r"^/ws/browser/(?P<sid>[^/]+)$")
_VIEW_FRAME = re.compile(r"^/api/live/views/(?P<vid>[^/]+)/frame\.jpg$")
_WS_LIVE = re.compile(r"^/ws/live/(?P<vid>[^/]+)$")
_WS_TASK_LOG = re.compile(r"^/ws/tasks/(?P<task>[^/]+)/logs$")


def node_of_session(session_id: str) -> str:
    """登录会话编号是「机器编号~随机串」（见 browser/live_session.py 的 ID_PREFIX）。"""
    return session_id.rsplit("~", 1)[0] if "~" in session_id else ""


async def _task_node(cluster: Cluster, task_id: str) -> str:
    """任务正在哪台机器上跑。没在跑就是本机（看历史日志、没有实时画面）。"""
    if not task_id or task_id == "adhoc":
        return ""
    task = await cluster.tasks.get(task_id)
    if not task or task.get("status") not in ("running", "queued"):
        return ""
    return str(task.get("node_id") or "")


async def _any_worker(cluster: Cluster) -> str:
    """本机只协调不跑任务时，「试搜」这类要开浏览器的事交给一台在线从机。"""
    if cluster.me.is_worker_role:
        return ""
    workers = await cluster.online_workers()
    return workers[0]["node_id"] if workers else ""


async def resolve_target(cluster: Cluster, scope_type: str, method: str, path: str,
                         query: str, body: Callable[[], Awaitable[bytes]]) -> str:
    """这个请求该由哪台机器处理。返回 '' 表示本机。"""
    if scope_type == "websocket":
        if m := _WS_BROWSER.match(path):
            return node_of_session(m["sid"])
        if m := _WS_LIVE.match(path):
            return await _task_node(cluster, m["vid"].split("::", 1)[0])
        if m := _WS_TASK_LOG.match(path):
            return await _task_node(cluster, m["task"])
        return ""

    if not (path.startswith("/api/accounts") or path.startswith("/api/live")):
        return ""
    if m := _LOGIN_SESSION.match(path):
        return node_of_session(m["sid"])
    if m := _VIEW_FRAME.match(path):
        return await _task_node(cluster, m["vid"].split("::", 1)[0])
    if path == "/api/live/views":
        task_id = (parse_qs(query).get("task_id") or [""])[0]
        return await _task_node(cluster, task_id)
    if path == "/api/accounts/probe-all":
        return await _any_worker(cluster)
    if path == "/api/accounts" and method == "POST":
        try:
            payload = json.loads(await body() or b"{}")
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        # 编辑已有账号只改库里的分组、昵称等，哪台都能做——账号所在机器离线时也得能改
        if await cluster.accounts.node_of(str(payload.get("channel") or ""),
                                          str(payload.get("account_name") or "")) is not None:
            return ""
        return str(payload.get("node_id") or "") or await _any_worker(cluster)
    if m := _ACCOUNT_ACTION.match(path):
        if m["action"] == "cookies" and method == "GET":
            return ""            # 只读库里存的 Cookie 名，哪台都一样
        if m["action"] == "probe" and m["name"] == "-":
            return await _any_worker(cluster)
        return await cluster.accounts.node_of(m["channel"], m["name"]) or ""
    if (m := _ACCOUNT.match(path)) and method == "DELETE":
        # 账号所在机器已经下线（比如机器报废了）：删库里的记录就行，不然这个号永远删不掉
        node = await cluster.accounts.node_of(m["channel"], m["name"]) or ""
        return node if node and (await cluster.node_url(node))[0] else ""
    return ""


class ClusterForwarder:
    """ASGI 中间件。没接集群（测试里）或者目标就是本机时什么都不做。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        cluster = get_cluster()
        if cluster is None or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if any(k == FORWARD_HEADER for k, _ in scope.get("headers") or []):
            return await self.app(scope, receive, send)

        buffered: Optional[bytes] = None

        async def read_body() -> bytes:
            nonlocal buffered
            if buffered is None:
                chunks = []
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        break
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        break
                buffered = b"".join(chunks)
            return buffered

        try:
            target = await resolve_target(
                cluster, scope["type"], scope.get("method", "GET"), scope["path"],
                (scope.get("query_string") or b"").decode("latin-1"), read_body)
        except Exception as exc:  # noqa: BLE001 —— 判断不了就本机处理，不能把请求弄丢
            logger.warning("集群转发：判断目标机器失败，按本机处理：%s", exc)
            target = ""

        if not target or target == cluster.me.node_id:
            return await self.app(scope, _replay(buffered, receive) if buffered is not None
                                  else receive, send)

        base, reason = await cluster.node_url(target)
        if scope["type"] == "websocket":
            return await _forward_ws(cluster, scope, receive, send, base, reason)
        if not base:
            return await JSONResponse(
                status_code=503, content={"code": 503, "data": None, "message": reason},
            )(scope, receive, send)
        return await _forward_http(cluster, scope, await read_body(), send, base, target)


def _replay(body: bytes, receive):
    sent = False

    async def again():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return again


def _target_url(scope, base: str, ws: bool = False) -> str:
    raw = scope.get("raw_path") or scope["path"].encode()
    url = base + raw.decode("latin-1")
    if scope.get("query_string"):
        url += "?" + scope["query_string"].decode("latin-1")
    if ws:
        url = "ws" + url[4:] if url.startswith("http") else url
    return url


def _forward_headers(scope, cluster: Cluster):
    headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers") or []
               if k.lower() not in _HOP_HEADERS and not k.lower().startswith(b"sec-websocket")]
    headers.append(("x-smc-forwarded", cluster.me.node_id))
    return headers


async def _forward_http(cluster: Cluster, scope, body: bytes, send, base: str, target: str):
    import httpx

    url = _target_url(scope, base)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=5)) as client:
            resp = await client.request(scope["method"], url, content=body,
                                        headers=_forward_headers(scope, cluster))
    except httpx.HTTPError as exc:
        logger.warning("集群转发 %s %s 到 %s 失败：%s", scope["method"], scope["path"], target, exc)
        payload = {"code": 502, "data": None,
                   "message": f"转到机器 {target}（{base}）失败：{exc}。检查那台机器是否开着、"
                              f"cluster.advertise_url 从这台机器能不能访问到"}
        content = json.dumps(payload, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 502,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(content)).encode())]})
        await send({"type": "http.response.body", "body": content})
        return
    content = resp.content
    headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers.multi_items()
               if k.lower() not in _DROP_RESPONSE]
    headers.append((b"content-length", str(len(content)).encode()))
    headers.append((b"x-smc-node", target.encode("latin-1", "replace")))
    await send({"type": "http.response.start", "status": resp.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": content})


async def _forward_ws(cluster: Cluster, scope, receive, send, base: str, reason: str):
    import websockets

    first = await receive()
    if first["type"] != "websocket.connect":
        return
    upstream = None
    if base:
        try:
            upstream = await websockets.connect(
                _target_url(scope, base, ws=True), max_size=None, open_timeout=10,
                additional_headers=[("x-smc-forwarded", cluster.me.node_id)])
        except Exception as exc:  # noqa: BLE001
            reason = f"连不上那台机器（{base}）：{exc}"
    await send({"type": "websocket.accept"})
    if upstream is None:
        msg = {"type": "status", "state": "error", "message": f"转发失败：{reason}"}
        await send({"type": "websocket.send", "text": json.dumps(msg, ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 1011})
        return

    async def client_to_upstream():
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_client():
        async for data in upstream:
            if isinstance(data, bytes):
                await send({"type": "websocket.send", "bytes": data})
            else:
                await send({"type": "websocket.send", "text": data})

    pumps = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, websockets.ConnectionClosed):
                logger.debug("集群转发 WebSocket 结束：%s", exc)
    finally:
        await upstream.close()
        try:
            await send({"type": "websocket.close", "code": 1000})
        except Exception:  # noqa: BLE001 —— 页面那头已经断了
            pass


__all__ = ["ClusterForwarder", "node_of_session", "resolve_target"]
