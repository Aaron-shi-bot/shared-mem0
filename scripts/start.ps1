param([switch]$Automatic, [string]$AppRoot = (Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot 'watchdog-common.ps1')
$context = Get-Mem0Context $AppRoot
if ($Automatic -and [IO.File]::Exists($context.Pause)) { Write-Output 'Mem0 remains paused for maintenance. Run start.ps1 manually to resume.'; exit 0 }
if (-not $Automatic -and [IO.File]::Exists($context.Pause)) { [IO.File]::Delete($context.Pause) }
$probe = $null
try { $probe=[IO.File]::Open($context.Lock,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None) }
catch [IO.IOException] { Write-Output 'Mem0 watchdog is already running. See automation/watchdog/status.json.'; exit 0 }
finally { if ($probe) { $probe.Dispose() } }
$watchdog = Join-Path $PSScriptRoot 'watchdog.ps1'
if (-not [IO.File]::Exists($watchdog)) { throw 'Watchdog program is missing.' }
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') + '-' + $PID
$shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$process = Start-Process -FilePath $shell -ArgumentList ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $watchdog + '" -AppRoot "' + $context.App + '"') -WorkingDirectory $context.App -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $context.Logs ('launcher-' + $stamp + '.out.log')) -RedirectStandardError (Join-Path $context.Logs ('launcher-' + $stamp + '.err.log'))
@{watchdog_requested=$true;launcher_pid=$process.Id;status_file=$context.Status;dashboard=('http://127.0.0.1:' + $context.Port)} | ConvertTo-Json -Compress
