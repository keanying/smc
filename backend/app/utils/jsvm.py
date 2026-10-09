"""调用平台签名 JS 的 Node 子进程桥。

为什么不用 PyExecJS：它已停止维护，在 Python 3.11+ 上装不上（构建 wheel 失败）。
直接起 Node 子进程更稳，也不用担心 execjs 的运行时选择逻辑。

为什么不是每次请求起一个进程：抖音每个请求都要算一次 a_bogus，
起进程的开销（~50ms）比签名本身大得多。这里维持一个常驻 Node 进程，
用「一行 JSON 请求 / 一行 JSON 响应」的方式通信，进程挂了自动重启。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.logging import get_logger

logger = get_logger(__name__)

# backend/app/utils/jsvm.py -> utils -> app -> backend
BACKEND_ROOT = Path(__file__).resolve().parents[2]
LIBS_DIR = BACKEND_ROOT / "libs"

# 常驻在 Node 里的桥接脚本：按行读 JSON，调用指定模块的指定函数，按行回 JSON
_BRIDGE_JS = r"""
const readline = require('readline');
const path = require('path');

const modules = new Map();

function loadModule(file) {
  if (!modules.has(file)) {
    modules.set(file, require(file));
  }
  return modules.get(file);
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on('line', (line) => {
  if (!line.trim()) return;
  let req;
  try {
    req = JSON.parse(line);
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: null, error: 'bad request json: ' + e.message }) + '\n');
    return;
  }
  try {
    const mod = loadModule(req.file);
    const fn = mod[req.fn];
    if (typeof fn !== 'function') {
      throw new Error('function not found: ' + req.fn + ' in ' + req.file);
    }
    const result = fn.apply(null, req.args || []);
    process.stdout.write(JSON.stringify({ id: req.id, result }) + '\n');
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: req.id, error: String(e && e.message || e) }) + '\n');
  }
});

process.stdout.write(JSON.stringify({ id: 'ready', result: 'ok' }) + '\n');
"""


class JsRuntimeError(RuntimeError):
    """Node 侧执行失败。"""


class NodeJsRuntime:
    """常驻 Node 进程，串行执行 JS 函数调用。

    签名计算本身是纯 CPU 的毫秒级操作，串行足够；
    加锁也顺便避免了多协程同时写 stdin 造成的消息交错。
    """

    def __init__(self, node_bin: str = "node"):
        self.node_bin = node_bin
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._counter = 0
        self._bridge_path: Optional[Path] = None

    async def _ensure_started(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return

        if self._bridge_path is None:
            bridge = LIBS_DIR / "_bridge.cjs"
            bridge.parent.mkdir(parents=True, exist_ok=True)
            bridge.write_text(_BRIDGE_JS, encoding="utf-8")
            self._bridge_path = bridge

        try:
            self._process = await asyncio.create_subprocess_exec(
                self.node_bin, str(self._bridge_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise JsRuntimeError(
                f"找不到 Node 运行时（{self.node_bin}）。"
                f"抖音的 a_bogus 签名需要 Node 执行 libs/douyin.cjs，"
                f"请安装 Node.js 18+，或在配置里把抖音平台关掉。"
            ) from exc

        # 等就绪行
        line = await asyncio.wait_for(self._process.stdout.readline(), timeout=15)
        payload = json.loads(line.decode("utf-8"))
        if payload.get("id") != "ready":
            raise JsRuntimeError(f"Node 桥接进程启动异常：{payload}")
        logger.info("Node 签名运行时已启动（pid=%s）", self._process.pid)

    async def call(self, js_file: str | Path, function: str, args: List[Any],
                   timeout: float = 20.0) -> Any:
        """对外入口：把实际执行提交到子进程专用循环。

        Node 桥接进程和它的 stdin/stdout 管道都绑定在创建它们的循环上，
        所以从启动到调用到关闭必须全程同一个循环——不能只把启动挪过去。
        直接在服务器循环上跑的话，Windows + uvicorn --reload 会抛
        NotImplementedError（见 core/subprocess_loop.py）。
        """
        from ..core.subprocess_loop import SUBPROCESS_LOOP

        return await SUBPROCESS_LOOP.run(self._call(js_file, function, args, timeout))

    async def _call(self, js_file: str | Path, function: str, args: List[Any],
                    timeout: float) -> Any:
        path = Path(js_file)
        if not path.is_absolute():
            path = LIBS_DIR / path
        if not path.exists():
            raise JsRuntimeError(
                f"签名脚本不存在：{path}。"
                f"请把 douyin.cjs 放到 backend/libs/ 下（见 README 的平台接入说明）。"
            )

        async with self._lock:
            await self._ensure_started()
            self._counter += 1
            request_id = self._counter
            message = json.dumps(
                {"id": request_id, "file": str(path), "fn": function, "args": args},
                ensure_ascii=False,
            )
            assert self._process is not None and self._process.stdin is not None
            self._process.stdin.write((message + "\n").encode("utf-8"))
            await self._process.stdin.drain()

            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=timeout
                )
            except asyncio.TimeoutError as exc:
                await self._shutdown()
                raise JsRuntimeError(f"调用 {function} 超时（{timeout}s），已重启 Node 运行时") from exc

            if not line:
                stderr = b""
                if self._process.stderr is not None:
                    stderr = await self._process.stderr.read(2000)
                await self._shutdown()
                raise JsRuntimeError(
                    f"Node 运行时意外退出：{stderr.decode('utf-8', 'ignore')[:500]}"
                )

            payload = json.loads(line.decode("utf-8"))
            if payload.get("error"):
                raise JsRuntimeError(f"{function} 执行失败：{payload['error']}")
            return payload.get("result")

    async def shutdown(self) -> None:
        """对外入口：同样要在子进程专用循环上执行，进程是在那儿创建的。"""
        from ..core.subprocess_loop import SUBPROCESS_LOOP

        if not SUBPROCESS_LOOP.is_running():
            return
        await SUBPROCESS_LOOP.run(self._shutdown())

    async def _shutdown(self) -> None:
        if self._process is None:
            return
        try:
            self._process.kill()
            await self._process.wait()
        except ProcessLookupError:
            pass
        finally:
            self._process = None


def resolve_node_bin() -> str:
    """找一个可用的 Node。

    优先级：
      1. 环境变量 SMC_NODE_BIN 显式指定
      2. PATH 里的 node
      3. Playwright 自带的 node（装了 playwright 就一定有，
         这样用户不用为了抖音签名单独装 Node.js）
    """
    explicit = os.getenv("SMC_NODE_BIN")
    if explicit:
        return explicit

    from shutil import which

    found = which("node")
    if found:
        return found

    try:
        import playwright

        bundled = Path(playwright.__file__).parent / "driver" / (
            "node.exe" if os.name == "nt" else "node"
        )
        if bundled.exists():
            logger.info("PATH 里没有 node，改用 Playwright 自带的：%s", bundled)
            return str(bundled)
    except Exception:  # noqa: BLE001
        pass

    return "node"


_runtime: Optional[NodeJsRuntime] = None


def get_js_runtime() -> NodeJsRuntime:
    global _runtime
    if _runtime is None:
        _runtime = NodeJsRuntime(resolve_node_bin())
    return _runtime


async def shutdown_js_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.shutdown()
        _runtime = None
