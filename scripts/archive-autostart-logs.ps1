# archive-autostart-logs.ps1
# Keep the newest N segmented autostart logs; move older ones OUTSIDE the repo.
# Segmented names only: <prefix>-YYYYMMDD-HHmmss.log (optional -2/-3 suffix).
# Never deletes, never overwrites, never reads log content.
# Non-fatal by design: the caller (autostart_*.bat) must start even if this fails.

param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('backend', 'frontend')]
  [string]$Prefix,

  [int]$Keep = 20,

  [Parameter(Mandatory = $true)]
  [string]$LogDir,

  [string]$ArchiveRoot = (Join-Path $env:USERPROFILE 'NovelCraft-logs\archive'),

  [string]$ActiveLog = '',

  [int]$MinAgeMinutes = 10,

  # Test hook only: pin the batch folder name so conflict handling can be exercised.
  # Empty means "use the current timestamp".
  [string]$BatchName = '',

  [switch]$WhatIf
)

$ErrorActionPreference = 'Continue'

function Write-Line([string]$text) {
  Write-Output $text
}

if ($Keep -lt 1) {
  Write-Line "RESULT=INVALID_PARAMS reason=Keep<1"
  exit 2
}
if (-not (Test-Path -LiteralPath $LogDir -PathType Container)) {
  Write-Line "RESULT=SKIP reason=log_dir_missing path=$LogDir"
  exit 0
}

$namePattern = '^' + [regex]::Escape($Prefix) + '-.*\.log$'
$tsPattern = '^' + [regex]::Escape($Prefix) + '-(\d{8}-\d{6})(?:-(\d+))?\.log$'
$now = Get-Date

$items = @()
foreach ($f in @(Get-ChildItem -LiteralPath $LogDir -File -ErrorAction SilentlyContinue)) {
  if ($f.Name -notmatch $namePattern) { continue }
  if (($f.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
  $m = [regex]::Match($f.Name, $tsPattern)
  $parsed = $null
  if ($m.Success) {
    try {
      $parsed = [datetime]::ParseExact($m.Groups[1].Value, 'yyyyMMdd-HHmmss', [Globalization.CultureInfo]::InvariantCulture)
    } catch {
      $parsed = $null
    }
  }
  $dup = 0
  if ($m.Success -and $m.Groups[2].Success) { [void][int]::TryParse($m.Groups[2].Value, [ref]$dup) }
  $sortTime = $f.LastWriteTime
  if ($null -ne $parsed) { $sortTime = $parsed }
  $items += [pscustomobject]@{
    File           = $f
    Name           = $f.Name
    SortTime       = $sortTime
    TimestampOk    = ($null -ne $parsed)
    DuplicateIndex = $dup
    AgeMinutes     = [math]::Round(($now - $f.LastWriteTime).TotalMinutes, 1)
  }
}

if ($items.Count -eq 0) {
  Write-Line "RESULT=OK prefix=$Prefix total=0 kept=0 moved=0"
  exit 0
}

$sorted = @($items | Sort-Object -Property @{ Expression = 'SortTime'; Descending = $true },
                                            @{ Expression = 'DuplicateIndex'; Descending = $true },
                                            @{ Expression = 'Name'; Descending = $true })

$kept = @($sorted | Select-Object -First $Keep)
$rest = @($sorted | Select-Object -Skip $Keep)

$activeFull = ''
if ($ActiveLog -ne '') {
  try { $activeFull = [IO.Path]::GetFullPath($ActiveLog) } catch { $activeFull = '' }
}

$protected = @{}
foreach ($k in $kept) { $protected[$k.File.FullName] = $true }
if ($activeFull -ne '') { $protected[$activeFull] = $true }

$unparsed = @($items | Where-Object { -not $_.TimestampOk }).Count

$candidates = @()
$skipped = @()
foreach ($r in $rest) {
  $full = $r.File.FullName
  if ($activeFull -ne '' -and $full -eq $activeFull) {
    $skipped += [pscustomobject]@{ Name = $r.Name; Reason = 'active_log' }
    continue
  }
  if ($protected.ContainsKey($full)) {
    $skipped += [pscustomobject]@{ Name = $r.Name; Reason = 'in_keep_window' }
    continue
  }
  if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
    $skipped += [pscustomobject]@{ Name = $r.Name; Reason = 'missing_before_move' }
    continue
  }
  if ($r.AgeMinutes -lt $MinAgeMinutes) {
    $skipped += [pscustomobject]@{ Name = $r.Name; Reason = 'modified_within_min_age' }
    continue
  }
  $candidates += $r
}

if ($candidates.Count -eq 0) {
  Write-Line "RESULT=OK prefix=$Prefix total=$($items.Count) kept=$($kept.Count) moved=0 skipped=$($skipped.Count) unparsed=$unparsed"
  exit 0
}

if ($BatchName -ne '') {
  $batchName = $BatchName
} else {
  $batchName = Get-Date -Format 'yyyyMMdd-HHmmss'
}
$batchDir = Join-Path (Join-Path $ArchiveRoot $batchName) 'autostart_logs'

if (-not $WhatIf) {
  if ($BatchName -eq '') {
    $suffix = 1
    while (Test-Path -LiteralPath $batchDir) {
      $suffix = $suffix + 1
      $batchName = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $suffix
      $batchDir = Join-Path (Join-Path $ArchiveRoot $batchName) 'autostart_logs'
    }
  }
  try {
    New-Item -ItemType Directory -Path $batchDir -Force -ErrorAction Stop | Out-Null
  } catch {
    Write-Line "RESULT=NONFATAL reason=batch_dir_create_failed path=$batchDir error=$($_.Exception.Message)"
    exit 0
  }
}

$moved = 0
$conflict = 0
$failed = 0
foreach ($c in $candidates) {
  $src = $c.File.FullName
  $dst = Join-Path $batchDir $c.Name
  if (Test-Path -LiteralPath $dst) {
    $conflict = $conflict + 1
    Write-Line "SKIP reason=target_exists name=$($c.Name)"
    continue
  }
  if ($WhatIf) {
    Write-Line "PLAN move name=$($c.Name) to=$dst"
    $moved = $moved + 1
    continue
  }
  $ok = $false
  $lastError = ''
  for ($attempt = 1; $attempt -le 2 -and -not $ok; $attempt++) {
    try {
      [IO.File]::Move($src, $dst)
      $ok = $true
    } catch {
      $lastError = $_.Exception.Message
      Start-Sleep -Milliseconds 200
    }
  }
  if ($ok) {
    $moved = $moved + 1
    Write-Line "MOVED name=$($c.Name)"
  } else {
    $failed = $failed + 1
    Write-Line "FAILED name=$($c.Name) error=$lastError"
  }
}

$mode = 'APPLY'
if ($WhatIf) { $mode = 'WHATIF' }
Write-Line "RESULT=DONE mode=$mode prefix=$Prefix total=$($items.Count) kept=$($kept.Count) active=$([bool]($activeFull -ne '')) moved=$moved skipped=$($skipped.Count) conflict=$conflict failed=$failed unparsed=$unparsed batch=$batchDir"
exit 0
