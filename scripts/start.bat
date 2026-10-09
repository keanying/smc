@echo off
REM 裸机启动（Windows）
REM 前提：本机已有 Python 3.10+、MySQL 8.0+、Redis 6+（Redis 可选）
setlocal enabledelayedexpansion
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10 或更高版本并加入 PATH
    exit /b 1
)

if not exist "config\config.yaml" (
    copy "config\config.example.yaml" "config\config.yaml" >nul
    echo [提示] 已生成 config\config.yaml，请填写 MySQL 密码和快代理密钥后重新运行
    echo [提示] 配置文件位置：%cd%\config\config.yaml
    exit /b 0
)

if not exist ".venv" (
    echo [启动] 创建虚拟环境 .venv
    python -m venv .venv
)
call .venv\Scripts\activate.bat

if not exist ".venv\.deps_installed" (
    echo [启动] 安装 Python 依赖...
    python -m pip install -q --upgrade pip
    pip install -q -r backend\requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        exit /b 1
    )
    echo done > ".venv\.deps_installed"
)

REM 检查浏览器引擎配置
findstr /C:"engine: obscura" config\config.yaml >nul 2>nul
if not errorlevel 1 (
    echo [启动] 检测到 Obscura 引擎，检查 Obscura...
    if not exist "obscura\obscura.exe" (
        echo [启动] Obscura 未找到，正在下载...
        call scripts\start_obscura.bat --download-only
        if errorlevel 1 (
            echo [错误] Obscura 下载失败，请手动运行 scripts\start_obscura.bat
            exit /b 1
        )
    )
    echo [提示] Obscura 模式：请单独运行 scripts\start_obscura.bat 启动 CDP 服务
) else (
    python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.launch(headless=True).close(); p.stop()" >nul 2>nul
    if errorlevel 1 (
        echo [启动] 安装 Playwright Chromium（首次运行需要几分钟）...
        python -m playwright install chromium
    )
)

if exist "frontend" if not exist "frontend\dist" (
    where npm >nul 2>nul
    if not errorlevel 1 (
        echo [启动] 构建前端...
        pushd frontend
        call npm install --silent
        call npm run build
        popd
    ) else (
        echo [提示] 未找到 npm，跳过前端构建；后端 API 仍可通过 /docs 访问
    )
)

echo [启动] 检查 MySQL / Redis 连通性...
python scripts\selfcheck.py --quick
if errorlevel 1 (
    echo [错误] 依赖检查未通过，请按上面的提示处理后重试
    exit /b 1
)

echo [启动] 服务启动中，浏览器访问 http://127.0.0.1:8000
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
