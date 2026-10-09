#!/usr/bin/env bash
# 裸机启动（Linux / macOS）。
# 前提：本机已有 Python 3.10+、MySQL 8.0+、Redis 6+（Redis 可选）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[启动]${NC} $*"; }
warn()  { echo -e "${YELLOW}[提示]${NC} $*"; }
error() { echo -e "${RED}[错误]${NC} $*" >&2; }

# ---------- 1. 检查 Python ----------
if ! command -v python3 >/dev/null 2>&1; then
    error "未找到 python3，请先安装 Python 3.10 或更高版本"
    exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python 版本：$PY_VERSION"

# ---------- 2. 准备配置 ----------
if [ ! -f config/config.yaml ]; then
    cp config/config.example.yaml config/config.yaml
    warn "已生成 config/config.yaml，请填写 MySQL 密码和快代理密钥后重新运行"
    warn "配置文件位置：$ROOT/config/config.yaml"
    exit 0
fi

# ---------- 3. 虚拟环境与依赖 ----------
if [ ! -d .venv ]; then
    info "创建虚拟环境 .venv"
    python3 -m venv .venv
fi
source .venv/bin/activate

if [ ! -f .venv/.deps_installed ] || [ backend/requirements.txt -nt .venv/.deps_installed ]; then
    info "安装 Python 依赖…"
    pip install -q --upgrade pip
    pip install -q -r backend/requirements.txt
    touch .venv/.deps_installed
fi

# ---------- 4. 浏览器 ----------
if [ -z "${SMC_BROWSER_EXECUTABLE_PATH:-}" ]; then
    if ! python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch(headless=True).close()
" >/dev/null 2>&1; then
        info "安装 Playwright Chromium（首次运行需要几分钟）…"
        python3 -m playwright install chromium
        python3 -m playwright install-deps chromium 2>/dev/null || \
            warn "系统依赖安装失败，如遇浏览器启动报错请手动执行：playwright install-deps chromium"
    fi
fi

# ---------- 5. 前端 ----------
if [ -d frontend ] && [ ! -d frontend/dist ]; then
    if command -v npm >/dev/null 2>&1; then
        info "构建前端…"
        (cd frontend && npm install --silent && npm run build)
    else
        warn "未找到 npm，跳过前端构建；后端 API 仍可通过 /docs 访问"
    fi
fi

# ---------- 6. 依赖服务自检 ----------
info "检查 MySQL / Redis 连通性…"
python3 scripts/selfcheck.py --quick || {
    error "依赖检查未通过，请按上面的提示处理后重试"
    exit 1
}

# ---------- 7. 启动 ----------
PORT=$(python3 -c "
import sys; sys.path.insert(0,'backend')
from app.core.config import load_config
print(load_config().get('server.port', 8000))
")
info "启动服务：http://127.0.0.1:${PORT}   （接口文档 http://127.0.0.1:${PORT}/docs）"
cd backend
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
