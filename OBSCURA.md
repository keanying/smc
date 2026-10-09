# Obscura 实验版本

本版本将交互式登录浏览器从 Playwright + Chromium 替换为 [Obscura](https://github.com/h4ckf0r0day/obscura)，一个用 Rust 编写的轻量级无头浏览器。

## 为什么用 Obscura

| 维度 | Playwright + Chromium | Obscura |
|------|----------------------|---------|
| 内存占用 | 200+ MB | **30 MB** |
| 启动速度 | ~2 秒 | **<50ms** |
| 反检测 | 需手动注入 stealth.js | **内置 stealth 模式** |
| 指纹随机化 | 固定 | **GPU/屏幕/Canvas/音频/电池随机化** |
| `navigator.webdriver` | 需处理 | **`undefined`（原生）** |
| `event.isTrusted` | `false` | **`true`** |

## 快速开始

### 1. 下载 Obscura

**Linux / macOS**

```bash
# 自动下载（推荐）
./scripts/start_obscura.sh

# 或手动下载
mkdir -p obscura && cd obscura
curl -LO https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-x86_64-linux.tar.gz
tar xzf obscura-x86_64-linux.tar.gz
chmod +x obscura obscura-worker
```

**Windows**

```cmd
REM 自动下载（推荐）
scripts\start_obscura.bat

REM 或手动下载
mkdir obscura && cd obscura
curl -LO https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-x86_64-windows.zip
tar -xzf obscura-x86_64-windows.zip
REM 或用 PowerShell: Expand-Archive obscura-x86_64-windows.zip -DestinationPath .
```

### 2. 配置

编辑 `config/config.yaml`：

```yaml
browser:
  engine: obscura  # 改为 obscura
  obscura:
    port: 9223
    stealth: true
    executable: ""  # 留空自动从 PATH 找，或指定路径如 ./obscura/obscura
```

### 3. 启动

**Linux / macOS**

```bash
# 终端 1：启动 Obscura CDP 服务
./scripts/start_obscura.sh

# 终端 2：启动 Scenic 后端
cd backend && python -m app.main
```

**Windows**

```cmd
REM 终端 1：启动 Obscura CDP 服务
scripts\start_obscura.bat

REM 终端 2：启动 Scenic 后端
cd backend && python -m app.main
```

> **Windows 一键启动**：`scripts\start.bat` 会自动检测 `engine: obscura`，
> 如果 Obscura 未下载会自动下载，并提示你单独运行 `scripts\start_obscura.bat`。

## 架构变化

### 原架构（Playwright）

```
用户点击「打开浏览器」
  → LiveBrowserSession._open_browser()
  → playwright.chromium.launch_persistent_context(profile_dir)
  → 本地 Chromium 进程
```

### 新架构（Obscura）

```
用户点击「打开浏览器」
  → LiveBrowserSession._open_browser()
  → manager._ensure_obscura(channel, account)
  → 启动 obscura serve --port 9223 --storage-dir {profile_dir}/obscura_storage
  → playwright.chromium.connect_over_cdp(ws://127.0.0.1:9223)
  → Obscura Rust 进程
```

## 注意事项

1. **每个账号一个 Obscura 进程**：`--storage-dir` 隔离 Cookie，多账号并行登录互不干扰
2. **Cookie 持久化**：Obscura 自动把 Cookie 写到 `--storage-dir/cookies.json`，重启后仍登录
3. **采集链路不变**：抖音/快手/小红书的签名采集仍用原有方式，只有交互式登录换 Obscura
4. **回退**：把 `browser.engine` 改回 `playwright` 即可切回原方案

## 已知限制

- Obscura 的 V8 是独立实现，不是 Chromium 的 V8，某些平台特有 JS API 可能行为不一致
- Service Worker 支持不完整，抖音/快手某些页面可能异常
- 媒体播放不支持，视频页渲染可能有问题

## 测试

```bash
# 验证 Obscura 基本功能
./obscura/obscura fetch https://example.com --eval "document.title"

# 验证 CDP 连接
python3 -c "
import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('ws://127.0.0.1:9223')
        page = await browser.new_page()
        await page.goto('https://example.com')
        print(await page.title())
        await browser.close()

asyncio.run(test())
"
```
