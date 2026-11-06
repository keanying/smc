#!/usr/bin/env bash
# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
#
# 一键部署（Linux / macOS）：建虚拟环境 → 装依赖 → 装 Playwright Chromium 并验证 → 检查环境 → 启动
#
#   bash scripts/setup.sh                      # 装好并在后台启动（再跑一次 = 检查 / 补装 / 重启）
#   bash scripts/setup.sh --recreate           # 删掉 .venv 重建（换了 Python、依赖版本乱了时用）
#   bash scripts/setup.sh --mirror             # 国内镜像：pip 走清华源，Chromium 走 npmmirror
#   sudo bash scripts/setup.sh --user opinion --system-deps --xvfb
#                                              # 服务器：以 opinion 用户跑，装系统依赖 / 中文字体 / 虚拟屏幕
#   bash scripts/setup.sh --foreground         # 前台运行（Ctrl+C 停），排查问题时用
#   bash scripts/setup.sh --no-start           # 只安装不启动
#
#   bash scripts/setup.sh status | stop | restart   # 管理后台服务
#
# 选项：
#   --python PY      指定解释器（默认依次找 python3.12 / 3.11 / 3.10 / 3.13 / python3）
#   --recreate       删掉 .venv 重建
#   --user NAME      服务用哪个系统用户跑（root 执行时生效）：虚拟环境、Chromium 都装给这个用户。
#                    ⚠️ 不指定的话，用 root 装、换别的用户跑，会找不到浏览器（Chromium 装在 ~/.cache 里）
#   --mirror         用国内镜像下载 pip 包和 Chromium
#   --system-deps    装 Chromium 需要的系统库、中文字体（推流画面里中文不显示方块）、Xvfb；需要 root
#   --xvfb           没有图形界面时开一块虚拟屏幕 :99，服务带着它跑——交互式登录默认是有头窗口，
#                    服务器上没屏幕会报 Missing X server。不想装 Xvfb 的话，可以在
#                    「系统设置 → 调度与浏览器」打开「登录窗口用无头」
#   --no-browser     不装 Playwright 自带的 Chromium（配置里指定了系统 Chrome 时用）
#   --build-frontend 强制重新构建前端（需要 node / npm）
#   --foreground     前台运行
#   --no-start       只安装不启动
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
step()  { echo -e "\n${BLUE}==>${NC} $*"; }
info()  { echo -e "${GREEN}[完成]${NC} $*"; }
warn()  { echo -e "${YELLOW}[注意]${NC} $*"; }
error() { echo -e "${RED}[错误]${NC} $*" >&2; }
CURRENT_STEP=""
step_name() { CURRENT_STEP="$*"; step "$*"; }
# 任何一步出错都说清是哪一步，别让人翻一屏输出去找
trap 'rc=$?; [ $rc -ne 0 ] && [ -n "$CURRENT_STEP" ] && error "在「$CURRENT_STEP」这一步失败了（退出码 $rc），原因见上面的输出"' EXIT

# ---------------------------------------------------------------- 参数
ACTION="install"
PYTHON_BIN=""; RECREATE=0; RUN_USER=""; MIRROR=0; SYSTEM_DEPS=0; XVFB=0
NO_BROWSER=0; BUILD_FRONTEND=0; FOREGROUND=0; NO_START=0
while [ $# -gt 0 ]; do
    case "$1" in
        install|start|stop|restart|status) ACTION="$1" ;;
        --python) PYTHON_BIN="${2:?--python 后面要跟解释器，如 python3.12}"; shift ;;
        --recreate) RECREATE=1 ;;
        --user) RUN_USER="${2:?--user 后面要跟用户名}"; shift ;;
        --mirror) MIRROR=1 ;;
        --system-deps) SYSTEM_DEPS=1 ;;
        --xvfb) XVFB=1 ;;
        --no-browser) NO_BROWSER=1 ;;
        --build-frontend) BUILD_FRONTEND=1 ;;
        --foreground) FOREGROUND=1 ;;
        --no-start) NO_START=1 ;;
        -h|--help) awk 'NR>=8 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
        *) error "不认识的参数：$1（--help 看用法）"; exit 2 ;;
    esac
    shift
done

VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
DATA_DIR="$ROOT/data"
LOG_DIR="$DATA_DIR/logs"
PID_FILE="$DATA_DIR/smc.pid"
OUT_LOG="$LOG_DIR/server.out"
XVFB_DISPLAY=":99"
IS_ROOT=0; [ "$(id -u)" -eq 0 ] && IS_ROOT=1
CUR_USER="$(id -un)"
[ -z "$RUN_USER" ] && RUN_USER="$CUR_USER"
if [ "$RUN_USER" != "$CUR_USER" ] && [ "$IS_ROOT" -ne 1 ]; then
    error "--user $RUN_USER 需要用 root（sudo）执行"; exit 2
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    error "系统里没有用户 $RUN_USER"; exit 2
fi

# 以服务用户执行（Chromium 要装进他的 ~/.cache，文件归属也得是他）
as_user() {
    if [ "$RUN_USER" = "$CUR_USER" ]; then
        bash -c "$1"
    else
        su -s /bin/bash "$RUN_USER" -c "$1"
    fi
}

PIP_ARGS=()
if [ "$MIRROR" -eq 1 ]; then
    PIP_ARGS=(-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn)
    export PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"
fi

port_of() {
    as_user "cd '$ROOT' && '$VPY' -c \"import sys; sys.path.insert(0, 'backend'); from app.core.config import load_config; print(load_config().get('server.port', 8000))\"" 2>/dev/null || echo 8000
}

# ---------------------------------------------------------------- 后台服务管理
pid_alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

# 健康检查用虚拟环境里的 Python 发请求：精简系统上不一定有 curl
healthy() {
    "$VPY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$1/api/health', timeout=2)" >/dev/null 2>&1
}

wait_healthy() {
    local port="$1" i
    for i in $(seq 1 90); do
        if healthy "$port"; then return 0; fi
        if ! pid_alive && [ "$FOREGROUND" -eq 0 ]; then return 1; fi
        sleep 1
    done
    return 1
}

do_status() {
    local port; port="$(port_of)"
    if pid_alive; then
        if healthy "$port"; then
            info "服务运行中（PID $(cat "$PID_FILE")）：http://127.0.0.1:${port}"
        else
            warn "进程在（PID $(cat "$PID_FILE")），但健康检查没通过——可能还在启动，或看日志：$OUT_LOG"
        fi
    else
        warn "服务没有在运行"
    fi
}

do_stop() {
    if ! pid_alive; then warn "服务没有在运行"; rm -f "$PID_FILE"; return 0; fi
    local pid; pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then
        warn "15 秒内没退出，强制结束"; kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    info "服务已停止"
}

start_xvfb() {
    if [ -n "${DISPLAY:-}" ]; then return 0; fi
    if ! command -v Xvfb >/dev/null 2>&1; then
        warn "没有 Xvfb，开不了虚拟屏幕（加 --system-deps 用 root 装一下）"; return 1
    fi
    if ! pgrep -f "Xvfb ${XVFB_DISPLAY}" >/dev/null 2>&1; then
        as_user "nohup Xvfb ${XVFB_DISPLAY} -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &"
        sleep 1
    fi
    export DISPLAY="$XVFB_DISPLAY"
    info "虚拟屏幕 ${XVFB_DISPLAY} 已就绪"
}

do_start() {
    local port; port="$(port_of)"
    if pid_alive; then warn "服务已在运行（PID $(cat "$PID_FILE")），先停掉再启动"; do_stop; fi
    [ "$XVFB" -eq 1 ] && start_xvfb || true
    as_user "mkdir -p '$LOG_DIR'"
    local display_env=""
    [ -n "${DISPLAY:-}" ] && display_env="DISPLAY='${DISPLAY}'"
    if [ "$FOREGROUND" -eq 1 ]; then
        step "前台启动（Ctrl+C 停止）：http://127.0.0.1:${port}"
        if [ "$RUN_USER" = "$CUR_USER" ]; then
            cd "$ROOT" && exec env ${DISPLAY:+DISPLAY="$DISPLAY"} "$VPY" run.py
        fi
        exec su -s /bin/bash "$RUN_USER" -c "cd '$ROOT' && ${display_env} exec '$VPY' run.py"
    fi
    step "后台启动服务"
    # ⚠️ cd 和后台启动要分成两句：写成「cd && 程序 &」的话，后台跑的是一个包着程序的子 shell——
    #    $! 记成子 shell 的 PID（stop 只杀掉它，服务还在跑），子 shell 还占着终端输出，
    #    ssh 远程执行 / 接管道时命令永远不返回
    as_user "cd '$ROOT' || exit 1; ${display_env} nohup setsid '$VPY' run.py >> '$OUT_LOG' 2>&1 < /dev/null & echo \$! > '$PID_FILE'"
    if wait_healthy "$port"; then
        info "服务已启动（PID $(cat "$PID_FILE")）"
        echo -e "      页面：${GREEN}http://127.0.0.1:${port}${NC}    接口文档：http://127.0.0.1:${port}/docs"
        echo    "      日志：$OUT_LOG"
        echo    "      停止：bash scripts/setup.sh stop      状态：bash scripts/setup.sh status"
    else
        error "服务没起来，最后 30 行日志："
        tail -n 30 "$OUT_LOG" 2>/dev/null || true
        exit 1
    fi
}

case "$ACTION" in
    status) do_status; exit 0 ;;
    stop) do_stop; exit 0 ;;
    start) do_start; exit 0 ;;
    restart) do_stop; do_start; exit 0 ;;
esac

# ================================================================= 安装
# ---------------------------------------------------------------- 1. Python
step_name "1/8 选择 Python"
if [ -z "$PYTHON_BIN" ]; then
    for cand in python3.12 python3.11 python3.10 python3.13 python3; do
        if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; break; fi
    done
fi
if [ -z "$PYTHON_BIN" ] || ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    error "找不到 Python 3.10+，先装一个（Ubuntu: apt install python3.12 python3.12-venv）"; exit 1
fi
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    error "$PYTHON_BIN 是 $PY_VER，需要 3.10 或更高"; exit 1
fi
info "$PYTHON_BIN（Python $PY_VER）"

# ---------------------------------------------------------------- 2. 配置
step_name "2/8 配置文件"
if [ ! -f config/config.yaml ]; then
    cp config/config.example.yaml config/config.yaml
    [ "$RUN_USER" != "$CUR_USER" ] && chown "$RUN_USER" config/config.yaml
    warn "已生成 config/config.yaml——装完后请填 MySQL 地址和密码（mysql 段），再执行：bash scripts/setup.sh start"
    NEED_CONFIG=1
else
    info "config/config.yaml 已存在"
    NEED_CONFIG=0
fi

# ---------------------------------------------------------------- 3. 虚拟环境
step_name "3/8 虚拟环境 .venv"
if pid_alive; then warn "服务正在运行，先停掉再装"; do_stop; fi
if [ "$RECREATE" -eq 1 ] && [ -d "$VENV" ]; then
    rm -rf "$VENV"; info "已删除旧的 .venv"
fi
if [ -x "$VPY" ] && ! "$VPY" -c 'import sys' >/dev/null 2>&1; then
    warn ".venv 坏了（解释器跑不起来），重建"; rm -rf "$VENV"
fi
if [ -x "$VPY" ]; then
    VENV_VER="$("$VPY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    if [ "$VENV_VER" != "$PY_VER" ]; then
        warn ".venv 是 Python $VENV_VER，和选中的 $PY_VER 不一样——要换版本请加 --recreate"
    fi
    info "沿用现有 .venv（Python $VENV_VER）"
else
    if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
        error "$PYTHON_BIN 不带 venv 模块（Ubuntu: apt install python${PY_VER}-venv）"; exit 1
    fi
    "$PYTHON_BIN" -m venv "$VENV"
    info "已创建 .venv（Python $PY_VER）"
fi
if [ "$RUN_USER" != "$CUR_USER" ]; then
    chown -R "$RUN_USER" "$VENV"
    mkdir -p "$DATA_DIR" && chown -R "$RUN_USER" "$DATA_DIR"
fi

# ---------------------------------------------------------------- 4. 依赖
step_name "4/8 安装 Python 依赖（backend/requirements.txt）"
as_user "'$VPY' -m pip install -q --upgrade pip ${PIP_ARGS[*]:-}"
as_user "'$VPY' -m pip install -q -r '$ROOT/backend/requirements.txt' ${PIP_ARGS[*]:-}"
PW_WANT="$(grep -iE '^playwright\s*==' backend/requirements.txt | sed -E 's/.*==\s*//' || true)"
PW_HAVE="$("$VPY" -m pip show playwright 2>/dev/null | awk '/^Version:/{print $2}')"
info "依赖已装好；Playwright $PW_HAVE${PW_WANT:+（requirements 要求 $PW_WANT）}"
if [ -n "$PW_WANT" ] && [ "$PW_WANT" != "$PW_HAVE" ]; then
    warn "Playwright 版本和 requirements 不一致，建议加 --recreate 重建虚拟环境"
fi

# ---------------------------------------------------------------- 5. 系统依赖（可选，root）
if [ "$SYSTEM_DEPS" -eq 1 ]; then
    step "5/8 系统依赖：Chromium 运行库、中文字体、Xvfb"
    if [ "$IS_ROOT" -ne 1 ]; then
        warn "--system-deps 需要 root，跳过（sudo 再跑一次）"
    else
        "$VPY" -m playwright install-deps chromium || warn "Chromium 运行库没装全，看上面的报错"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get install -y -q fonts-noto-cjk xvfb >/dev/null || warn "中文字体 / Xvfb 没装上"
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y -q google-noto-sans-cjk-ttc-fonts xorg-x11-server-Xvfb >/dev/null || warn "中文字体 / Xvfb 没装上"
        elif command -v yum >/dev/null 2>&1; then
            yum install -y -q google-noto-sans-cjk-ttc-fonts xorg-x11-server-Xvfb >/dev/null || warn "中文字体 / Xvfb 没装上"
        else
            warn "认不出包管理器，中文字体和 Xvfb 请手动装"
        fi
        info "系统依赖已处理"
    fi
else
    step "5/8 系统依赖：跳过（需要时加 --system-deps 并用 root 执行）"
fi

# ---------------------------------------------------------------- 6. 浏览器
step_name "6/8 浏览器（Playwright Chromium）"
EXE_PATH="${SMC_BROWSER_EXECUTABLE_PATH:-}"
if [ -z "$EXE_PATH" ] && [ -f config/config.yaml ]; then
    EXE_PATH="$(as_user "cd '$ROOT' && '$VPY' -c \"import sys; sys.path.insert(0, 'backend'); from app.core.config import load_config; print(load_config().get('browser.executable_path') or '')\"" 2>/dev/null || true)"
fi
if [ -n "$EXE_PATH" ]; then
    info "配置里指定了浏览器：$EXE_PATH（不下载 Playwright 自带的）"
elif [ "$NO_BROWSER" -eq 1 ]; then
    warn "按 --no-browser 跳过下载"
else
    as_user "${PLAYWRIGHT_DOWNLOAD_HOST:+PLAYWRIGHT_DOWNLOAD_HOST='$PLAYWRIGHT_DOWNLOAD_HOST'} '$VPY' -m playwright install chromium" \
        || { error "Chromium 下载失败：国内服务器加 --mirror 再试"; exit 1; }
fi
# 真开一次无头浏览器：装了不等于能跑（缺系统库、装到了别的用户目录下都会在这里暴露）
if as_user "cd '$ROOT' && '$VPY' - <<'PYEOF'
import os
from playwright.sync_api import sync_playwright
exe = '''$EXE_PATH''' or None
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=exe, args=['--no-sandbox'])
    page = b.new_page(); page.set_content('<p>ok</p>'); assert page.inner_text('p') == 'ok'
    print(f'Chromium {b.version} 启动正常')
    b.close()
PYEOF"; then
    info "浏览器验证通过"
else
    error "浏览器启动失败：多半是缺系统库，用 root 加 --system-deps 再跑一次"
    exit 1
fi

# ---------------------------------------------------------------- 7. 运行环境检查
step_name "7/8 运行环境检查"
if [ "$(uname -s)" = "Linux" ]; then
    if [ -n "${DISPLAY:-}" ]; then
        info "有图形环境（DISPLAY=$DISPLAY），交互式登录可以用有头窗口"
    elif [ "$XVFB" -eq 1 ]; then
        info "没有图形环境：启动时开虚拟屏幕 ${XVFB_DISPLAY}"
    else
        warn "没有图形环境（没有 DISPLAY）。交互式登录默认是有头窗口，会报 Missing X server。二选一："
        warn "  ① 加 --xvfb 再跑（开一块虚拟屏幕，风控表现和有屏幕一样）"
        warn "  ② 页面「系统设置 → 调度与浏览器」打开「登录窗口用无头」"
    fi
    if command -v fc-list >/dev/null 2>&1; then
        if [ "$(fc-list :lang=zh 2>/dev/null | wc -l)" -gt 0 ]; then
            info "已有中文字体"
        else
            warn "没有中文字体：登录推流画面里中文会显示成方块（root 加 --system-deps 装上）"
        fi
    fi
fi
if [ -f frontend/dist/index.html ] && [ "$BUILD_FRONTEND" -eq 0 ]; then
    info "前端已构建（frontend/dist）"
elif command -v npm >/dev/null 2>&1; then
    step "构建前端"
    as_user "cd '$ROOT/frontend' && npm install --silent && npm run build" || { error "前端构建失败"; exit 1; }
    info "前端已构建"
else
    warn "没有 frontend/dist，也没有 npm：接口能用，页面是空白的（装 Node.js 后加 --build-frontend）"
fi

# ---------------------------------------------------------------- 8. 依赖服务 + 启动
step_name "8/8 检查 MySQL / Redis"
if [ "$NEED_CONFIG" -eq 1 ]; then
    warn "刚生成的 config/config.yaml 还没填数据库：填好后执行 bash scripts/setup.sh start"
    exit 0
fi
if ! as_user "cd '$ROOT' && '$VPY' scripts/selfcheck.py --quick"; then
    error "数据库 / Redis 检查没通过：按上面的提示改 config/config.yaml，再执行 bash scripts/setup.sh start"
    exit 1
fi

if [ "$NO_START" -eq 1 ]; then
    info "安装完成（--no-start，没有启动）。启动：bash scripts/setup.sh start"
    exit 0
fi
do_start
