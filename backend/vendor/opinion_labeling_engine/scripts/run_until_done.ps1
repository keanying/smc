# =============================================================================
# 持续标注，直到全部标完为止（Windows PowerShell）
# -----------------------------------------------------------------------------
# 用法（在项目根目录执行）：
#     .\scripts\run_until_done.ps1
#     .\scripts\run_until_done.ps1 -PendingFlags "0","5"
#     .\scripts\run_until_done.ps1 -ScenicId PFTSCA01009835
#
# 它靠退出码判断要不要继续：
#     0 = 符合条件的数据全部标完了  → 结束
#     2 = 还有剩余（卡住/被中断）    → 等一会儿重跑，接着标
#     其它 = 配置或连接错误          → 停下来让人看，不要闷头重试
#
# 想让它在你合上电脑之后还能跑，先把系统睡眠关掉：
#     powercfg /change standby-timeout-ac 0
# =============================================================================

[CmdletBinding()]
param(
    # 传给 label_from_table.py 的参数
    [string[]] $PendingFlags = @(),
    [string]   $Mode         = "pending",
    [string]   $ScenicId     = "",
    [string]   $Channel      = "",
    [int]      $BatchSize    = 0,

    # 两轮之间歇多久（秒）。给模型/数据库一点喘息时间
    [int] $RestSeconds = 60,
    # 最多重跑多少轮，防止配置有问题时无限循环。0 = 不限
    [int] $MaxRounds = 200,
    # Python 解释器
    [string] $Python = "python"
)

$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "label_from_table.py"

$argv = @($script, "--mode", $Mode)
if ($PendingFlags.Count -gt 0) { $argv += @("--pending-flags") + $PendingFlags }
if ($ScenicId)  { $argv += @("--scenic-id", $ScenicId) }
if ($Channel)   { $argv += @("--channel", $Channel) }
if ($BatchSize -gt 0) { $argv += @("--batch-size", $BatchSize) }

Write-Host "命令：$Python $($argv -join ' ')" -ForegroundColor Cyan
Write-Host "退出码 0=全部标完，2=还有剩余会自动重跑，其它=出错停下" -ForegroundColor Cyan

$round = 0
$startedAt = Get-Date

while ($true) {
    $round++
    if ($MaxRounds -gt 0 -and $round -gt $MaxRounds) {
        Write-Warning "已经跑了 $MaxRounds 轮还没标完，先停下来看看是不是哪里不对"
        exit 2
    }

    Write-Host ""
    Write-Host "===== 第 $round 轮  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" -ForegroundColor Yellow
    & $Python @argv
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        $elapsed = (Get-Date) - $startedAt
        Write-Host ""
        Write-Host "全部标完了。共 $round 轮，耗时 $([int]$elapsed.TotalHours) 小时 $($elapsed.Minutes) 分" -ForegroundColor Green
        exit 0
    }

    if ($code -ne 2) {
        # 配置错、连不上库、连不上 Redis —— 重试解决不了，闷头重跑只会刷屏
        Write-Warning "退出码 $code，不是『还有剩余』，停止重跑。看上面的报错"
        exit $code
    }

    Write-Host "还有剩余，$RestSeconds 秒后接着跑……" -ForegroundColor Yellow
    Start-Sleep -Seconds $RestSeconds
}
