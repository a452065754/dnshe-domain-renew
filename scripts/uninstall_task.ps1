<#
.SYNOPSIS
  删除 DNSHE 域名自动续期计划任务。
#>
param([string]$TaskName = "DNSHE域名自动续期")

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "没有找到计划任务：$TaskName" -ForegroundColor Yellow
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
