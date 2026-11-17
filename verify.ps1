<#
NovelCraft 一键验收
================================================================
用法（在项目根目录，双击 verify.cmd 或命令行运行）：

    verify.cmd            快速验收 = git 空白检查 + 后端 pytest + 前端 tsc
    verify.cmd -Full      全量验收 = 上面三项 + 前端 vite build

退出码：0 = 全部通过；非 0 = 有失败（数值 = 失败项数）

怎么看结果：
    屏幕最后一行出现 [VERIFY OK]   -> 这次改动没弄坏东西，可以继续
    出现 [VERIFY FAIL]            -> 上面红色 [FAIL] 那一步出了问题
================================================================
#>
param([switch]$Full)

$ErrorActionPreference = 'Continue'

$root = $PSScriptRoot
$py   = Join-Path $root 'backend\.venv\Scripts\python.exe'
$tsc  = Join-Path $root 'frontend\node_modules\.bin\tsc.cmd'

$script:rows = @()
$script:fail = 0

function Rule([string]$t) {
    Write-Host ''
    Write-Host ('-' * 64) -ForegroundColor DarkGray
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ('-' * 64) -ForegroundColor DarkGray
}

function Record([string]$name, [int]$code) {
    if ($code -eq 0) {
        $script:rows += [pscustomobject]@{ 检查项 = $name; 结果 = 'PASS'; 退出码 = $code }
        Write-Host "[PASS] $name" -ForegroundColor Green
    } else {
        $script:rows += [pscustomobject]@{ 检查项 = $name; 结果 = 'FAIL'; 退出码 = $code }
        $script:fail++
        Write-Host "[FAIL] $name  (exit=$code)" -ForegroundColor Red
    }
}

# ---------------------------------------------------------------
# 0. 环境自检：缺东西就别往下跑，先告诉人怎么补
# ---------------------------------------------------------------
Rule '0/4 环境自检'
$envOk = $true
if (-not (Test-Path $py)) {
    Write-Host "[FAIL] 找不到后端虚拟环境解释器：" -ForegroundColor Red
    Write-Host "       $py" -ForegroundColor Red
    $envOk = $false
}
if (-not (Test-Path $tsc)) {
    Write-Host "[FAIL] 找不到前端 TypeScript 编译器（node_modules 没装）：" -ForegroundColor Red
    Write-Host "       $tsc" -ForegroundColor Red
    $envOk = $false
}
if (-not $envOk) {
    Write-Host ''
    Write-Host '先补环境，再重新验收：' -ForegroundColor Yellow
    Write-Host '  后端：cd backend' -ForegroundColor Yellow
    Write-Host '        python -m venv .venv' -ForegroundColor Yellow
    Write-Host '        .venv\Scripts\python.exe -m pip install -r requirements.lock.txt' -ForegroundColor Yellow
    Write-Host '  前端：cd frontend' -ForegroundColor Yellow
    Write-Host '        npm install' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '[VERIFY FAIL] 环境不全，无法验收' -ForegroundColor Red
    exit 2
}
Write-Host '[ok] 后端 venv 与前端 node_modules 都在' -ForegroundColor Green

# 顺带提醒：有未提交改动时，改坏了回不去
Push-Location $root
$dirty = @(& git status --porcelain 2>$null)
Pop-Location
if ($dirty.Count -gt 0) {
    Write-Host "[注意] 当前有 $($dirty.Count) 个文件改动未提交。改坏了代码回不到上一个能跑的版本，建议先 git commit。" -ForegroundColor Yellow
}

# ---------------------------------------------------------------
# 1. git 空白错误 / 冲突标记
# ---------------------------------------------------------------
Rule '1/4 git diff --check（行尾空白、冲突标记残留）'
Push-Location $root
$gd = & git diff --check 2>&1
$gdCode = $LASTEXITCODE
Pop-Location
if ($gdCode -ne 0) {
    $gd | Select-Object -First 20 | ForEach-Object { Write-Host $_ -ForegroundColor Red }
} else {
    Write-Host '没有空白错误、没有残留冲突标记' -ForegroundColor Gray
}
Record 'git diff --check' $gdCode

# ---------------------------------------------------------------
# 2. 后端 pytest
# ---------------------------------------------------------------
Rule '2/4 后端 pytest（离线回归）'
Push-Location (Join-Path $root 'backend')
$sw = [Diagnostics.Stopwatch]::StartNew()
# 2026-11-15 已知坑：venv 的 Marvis 运行时 Python 写不了系统 TEMP（AppData\Local\Temp 被拒），
# tempfile 回退到 cwd → pytest tmp_path 落进仓库内 → 快照类测试的「非 git 仓库」假设失效（37 项失败）。
# 修复：验收时把 TEMP/TMP/TMPDIR 指向仓库外的可写目录（default-workspace\.pytest-tmp）。
$ncTestTemp = Join-Path $root '..\.pytest-tmp'
New-Item -ItemType Directory -Force -Path $ncTestTemp | Out-Null
$env:TEMP = $ncTestTemp
$env:TMP = $ncTestTemp
$env:TMPDIR = $ncTestTemp
$po = & $py -m pytest -m "not formal_db" 2>&1
$pc = $LASTEXITCODE
$sw.Stop()
Pop-Location

$show = if ($pc -eq 0) { 3 } else { 45 }
$po | Select-Object -Last $show | ForEach-Object {
    if ($pc -eq 0) { Write-Host $_ -ForegroundColor Gray }
    else { Write-Host $_ -ForegroundColor Red }
}
Write-Host ("耗时 " + [math]::Round($sw.Elapsed.TotalSeconds, 1) + " 秒") -ForegroundColor DarkGray
Record 'backend pytest' $pc

# ---------------------------------------------------------------
# 3. 前端 TypeScript 类型检查
# ---------------------------------------------------------------
Rule '3/4 前端 tsc --noEmit（类型检查）'
Push-Location (Join-Path $root 'frontend')
$sw2 = [Diagnostics.Stopwatch]::StartNew()
$to = & $tsc --noEmit 2>&1
$tc = $LASTEXITCODE
$sw2.Stop()
Pop-Location

if ($tc -ne 0) {
    $to | Select-Object -First 40 | ForEach-Object { Write-Host $_ -ForegroundColor Red }
} else {
    Write-Host '类型全部对上' -ForegroundColor Gray
}
Write-Host ("耗时 " + [math]::Round($sw2.Elapsed.TotalSeconds, 1) + " 秒") -ForegroundColor DarkGray
Record 'frontend tsc' $tc

# ---------------------------------------------------------------
# 4. 可选：前端真实构建
# ---------------------------------------------------------------
if ($Full) {
    Rule '4/4 前端 vite build（真实打包）'
    $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if (-not $npm) { $npm = 'C:\Program Files\nodejs\npm.cmd' }
    Push-Location (Join-Path $root 'frontend')
    $sw3 = [Diagnostics.Stopwatch]::StartNew()
    $bo = & $npm 'run' 'build' 2>&1
    $bc = $LASTEXITCODE
    $sw3.Stop()
    Pop-Location

    $show3 = if ($bc -eq 0) { 6 } else { 45 }
    $bo | Select-Object -Last $show3 | ForEach-Object {
        if ($bc -eq 0) { Write-Host $_ -ForegroundColor Gray }
        else { Write-Host $_ -ForegroundColor Red }
    }
    Write-Host ("耗时 " + [math]::Round($sw3.Elapsed.TotalSeconds, 1) + " 秒") -ForegroundColor DarkGray
    Record 'frontend build' $bc
} else {
    Rule '4/4 前端 vite build（本次跳过，加 -Full 才跑）'
    Write-Host '快速验收不打包，省时间；准备交付/上线前再跑 verify.cmd -Full' -ForegroundColor Gray
}

# ---------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------
Rule '验收汇总'
$script:rows | Format-Table -AutoSize | Out-String | Write-Host

if ($script:fail -eq 0) {
    Write-Host "[VERIFY OK] 全部通过（$($script:rows.Count) 项）" -ForegroundColor Green
    exit 0
} else {
    Write-Host "[VERIFY FAIL] $($script:fail) 项没过，看上面红色输出" -ForegroundColor Red
    exit $script:fail
}
