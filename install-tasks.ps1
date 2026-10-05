# ============================================================
#  WorkBuddy 多账号自动签到 - 计划任务注册（通用版）
#
#  用法：在解压后的目录里运行
#      powershell -ExecutionPolicy Bypass -File .\install-tasks.ps1
#
#  注册两个静默任务（不开面板，后台直接签到）：
#     WorkBuddySigninMulti      每天 00:05        签到 + 成长中心
#     WorkBuddySigninMultiPoll  每天 01/05/09/13/17/21 点  补签 + 成长中心
#
#  卸载：
#     Unregister-ScheduledTask -TaskName "WorkBuddySigninMulti" -Confirm:$false
#     Unregister-ScheduledTask -TaskName "WorkBuddySigninMultiPoll" -Confirm:$false
# ============================================================
$ErrorActionPreference = "Stop"

# ---- 定位脚本目录（%PSScriptRoot% 在直接粘贴时为空，做兜底）----
$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$engine = Join-Path $here "run_all.py"

# ---- 定位 pythonw.exe（需要能被计划任务无窗口调用）----
function Find-Pythonw {
    # 1) 便携布局：脚本目录里直接放了 pythonw.exe
    $local = Join-Path $here "pythonw.exe"
    if (Test-Path $local) { return $local }

    # 2) py 启动器（python.org 安装默认自带；pyw.exe 是无控制台版）
    $pyw = Join-Path $env:SystemRoot "pyw.exe"
    if (Test-Path $pyw) { return $pyw }

    # 3) PATH 里的 pythonw.exe（跳过 Microsoft Store 占位符）
    $hits = @(where.exe pythonw 2>$null)
    foreach ($h in $hits) {
        if ($h -and $h -notmatch "WindowsApps" -and (Test-Path $h)) { return $h }
    }

    # 4) 常见安装位置（新版本优先）
    $cands = @()
    foreach ($v in @("314", "313", "312", "311", "310")) {
        $cands += "$env:LOCALAPPDATA\Programs\Python\Python$v\pythonw.exe"
        $cands += "C:\Python$v\pythonw.exe"
        $cands += "$env:ProgramFiles\Python$v\pythonw.exe"
    }
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }

    return $null
}

# ---- 校验 ----
if (-not (Test-Path $engine)) {
    Write-Host "[错误] 没找到 run_all.py，请确认脚本和它在同一目录：$here" -ForegroundColor Red
    exit 1
}
$pythonw = Find-Pythonw
if (-not $pythonw) {
    Write-Host "[错误] 没找到 pythonw.exe。" -ForegroundColor Red
    Write-Host "       请先安装 Python 3：https://www.python.org/downloads/" -ForegroundColor Yellow
    exit 1
}
Write-Host "引擎    : $engine"
Write-Host "解释器  : $pythonw"

# ---- 注册任务 ----
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$settings  = { param($min)
    New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $min)
}

# 任务 1：每天 00:05 全量
$act1 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$engine`"" -WorkingDirectory $here
$tri1 = New-ScheduledTaskTrigger -Daily -At "00:05"
$set1 = & $settings 30
Register-ScheduledTask -TaskName "WorkBuddySigninMulti" `
    -Action $act1 -Trigger $tri1 -Settings $set1 -Principal $principal `
    -Description "WorkBuddy multi-account auto signin (silent)" -Force | Out-Null

# 任务 2：每天 6 次轮询补签
$tri2 = @()
foreach ($hh in @("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")) {
    $tri2 += New-ScheduledTaskTrigger -Daily -At $hh
}
$act2 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$engine`"" -WorkingDirectory $here
$set2 = & $settings 30
Register-ScheduledTask -TaskName "WorkBuddySigninMultiPoll" `
    -Action $act2 -Trigger $tri2 -Settings $set2 -Principal $principal `
    -Description "WorkBuddy poll (catch-up signin)" -Force | Out-Null

# ---- 复核（不只信返回值）----
Write-Host ""
Write-Host "已注册：" -ForegroundColor Green
foreach ($n in @("WorkBuddySigninMulti", "WorkBuddySigninMultiPoll")) {
    $t = Get-ScheduledTask -TaskName $n
    $i = Get-ScheduledTaskInfo -TaskName $n
    Write-Host ("  {0,-26} {1,-6} next={2}" -f $t.TaskName, $t.State, $i.NextRunTime)
}
