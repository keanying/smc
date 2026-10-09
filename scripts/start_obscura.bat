@echo off
REM Obscura 浏览器启动脚本（Windows）
REM 用于 Scenic Media Collector 的 Obscura 实验版本

setlocal enabledelayedexpansion
cd /d "%~dp0.."

set OBSCURA_DIR=obscura
set OBSCURA_BIN=%OBSCURA_DIR%\obscura.exe
set DOWNLOAD_ONLY=0

REM 解析参数
if "%1"=="--download-only" set DOWNLOAD_ONLY=1

REM 检查 Obscura 是否已下载
if not exist "%OBSCURA_BIN%" (
    echo [启动] Obscura 未找到，正在下载...
    if not exist "%OBSCURA_DIR%" mkdir "%OBSCURA_DIR%"
    cd "%OBSCURA_DIR%"
    
    REM Windows 只有 x86_64 版本
    set DOWNLOAD_URL=https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-x86_64-windows.zip
    
    echo [下载] %DOWNLOAD_URL%
    curl -LO "%DOWNLOAD_URL%"
    if errorlevel 1 (
        echo [错误] 下载失败，请手动下载：
        echo   https://github.com/h4ckf0r0day/obscura/releases
        exit /b 1
    )
    
    REM 解压（需要 PowerShell 或 tar）
    where tar >nul 2>nul
    if not errorlevel 1 (
        tar -xzf obscura-x86_64-windows.zip
    ) else (
        echo [提示] 使用 PowerShell 解压...
        powershell -Command "Expand-Archive -Path 'obscura-x86_64-windows.zip' -DestinationPath '.' -Force"
    )
    
    del obscura-x86_64-windows.zip
    cd /d "%~dp0.."
    echo [完成] Obscura 下载完成
)

REM 只下载不启动
if "%DOWNLOAD_ONLY%"=="1" exit /b 0

REM 验证 Obscura 可用
"%OBSCURA_BIN%" --version >nul 2>nul
if errorlevel 1 (
    echo [错误] Obscura 无法运行，请检查二进制文件
    exit /b 1
)

for /f "tokens=*" %%i in ('"%OBSCURA_BIN%" --version 2^>^&1') do set OBSCURA_VERSION=%%i
echo [信息] Obscura 版本: %OBSCURA_VERSION%

REM 默认配置
if not defined OBSCURA_PORT set OBSCURA_PORT=9223
if not defined OBSCURA_STEALTH set OBSCURA_STEALTH=true
if not defined OBSCURA_STORAGE_ROOT set OBSCURA_STORAGE_ROOT=data\obscura_profiles

if not exist "%OBSCURA_STORAGE_ROOT%" mkdir "%OBSCURA_STORAGE_ROOT%"

echo [启动] Obscura CDP 服务...
echo   端口: %OBSCURA_PORT%
echo   Stealth: %OBSCURA_STEALTH%
echo   存储目录: %OBSCURA_STORAGE_ROOT%

REM 构建参数
set ARGS=serve --port %OBSCURA_PORT% --storage-dir "%OBSCURA_STORAGE_ROOT%"
if "%OBSCURA_STEALTH%"=="true" set ARGS=%ARGS% --stealth

REM 启动
"%OBSCURA_BIN%" %ARGS%
