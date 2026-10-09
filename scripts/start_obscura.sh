#!/bin/bash
# Obscura 浏览器启动脚本
# 用于 Scenic Media Collector 的 Obscura 实验版本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OBSCURA_DIR="$PROJECT_ROOT/obscura"
OBSCURA_BIN="$OBSCURA_DIR/obscura"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检查 Obscura 是否已下载
if [ ! -f "$OBSCURA_BIN" ]; then
    log_info "Obscura 未找到，正在下载..."
    mkdir -p "$OBSCURA_DIR"
    cd "$OBSCURA_DIR"
    
    # 检测架构
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        DOWNLOAD_URL="https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-x86_64-linux.tar.gz"
    elif [ "$ARCH" = "aarch64" ]; then
        DOWNLOAD_URL="https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-aarch64-linux.tar.gz"
    else
        log_error "不支持的架构: $ARCH"
        exit 1
    fi
    
    curl -LO "$DOWNLOAD_URL"
    tar xzf obscura-*.tar.gz
    rm obscura-*.tar.gz
    chmod +x obscura obscura-worker
    log_info "Obscura 下载完成"
fi

# 验证 Obscura 可用
if ! "$OBSCURA_BIN" --version &>/dev/null; then
    log_error "Obscura 无法运行，请检查二进制文件"
    exit 1
fi

OBSCURA_VERSION=$("$OBSCURA_BIN" --version 2>&1 | head -1)
log_info "Obscura 版本: $OBSCURA_VERSION"

# 默认配置
PORT="${OBSCURA_PORT:-9223}"
STEALTH="${OBSCURA_STEALTH:-true}"
STORAGE_ROOT="${OBSCURA_STORAGE_ROOT:-$PROJECT_ROOT/data/obscura_profiles}"

mkdir -p "$STORAGE_ROOT"

log_info "启动 Obscura CDP 服务..."
log_info "  端口: $PORT"
log_info "  Stealth: $STEALTH"
log_info "  存储目录: $STORAGE_ROOT"

# 构建参数
ARGS=("serve" "--port" "$PORT" "--storage-dir" "$STORAGE_ROOT")
if [ "$STEALTH" = "true" ]; then
    ARGS+=("--stealth")
fi

# 启动
exec "$OBSCURA_BIN" "${ARGS[@]}"
