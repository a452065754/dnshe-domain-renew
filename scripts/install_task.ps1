<#
.SYNOPSIS
  注册 Windows 计划任务：每天定时执行 DNSHE 域名自动续期。

.DESCRIPTION
  默认用 pythonw.exe 静默运行（无黑窗口），输出全部落到 logs\auto-YYYYMMDD.log。
  想看到控制台窗口调试时加 -ShowConsole。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -Time 08:30
  powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -ShowConsole
#>
param(
    [string]$Time = "09:00",
    [string]$TaskName = "DNSHE域名自动续期",
    [switch]$ShowConsole
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "dnshe.py"
if (-not (Test-Path $target)) { throw "找不到入口脚本：$target" }

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { throw "PATH 里找不到 python，请先安装 Python 或把它的目录加入 PATH" }
$pyDir = Split-Path -Parent $pyCmd.Source
$pyw = Join-Path $pyDir "pythonw.exe"

if ($ShowConsole -or -not (Test-Path $pyw)) {
    $exe = $pyCmd.Source
} else {
    $exe = $pyw
}

$action = New-ScheduledTaskAction -Execute $exe `
    -Argument "`"$target`" auto" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$desc = "每天 $Time 检查 DNSHE 免费域名到期时间，进入 180 天续期窗口且剩余天数达到阈值时自动免费续期；日志写在 logs 目录"

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description $desc -Force | Out-Null

Write-Host "已注册计划任务：$TaskName" -ForegroundColor Green
Write-Host "  执行程序 ：$exe"
Write-Host "  参数     ：`"$target`" auto"
Write-Host "  触发时间 ：每天 $Time"
Write-Host "  工作目录 ：$root"
Write-Host "  日志位置 ：$root\logs"
Write-Host ""
Write-Host "查看：Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "手动跑一次：Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "删除：.\scripts\uninstall_task.ps1"
