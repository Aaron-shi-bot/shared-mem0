param([string]$AppRoot = (Split-Path -Parent $PSScriptRoot), [ValidateRange(1,60)][int]$WaitSeconds = 20)
. (Join-Path $PSScriptRoot 'watchdog-common.ps1')
$context = Get-Mem0Context $AppRoot
# Pause is persistent across watchdog and Windows restarts. Only a manual start clears it.
Write-Mem0Json $context.Pause @{format='mem0-watchdog-pause-v1';app_root=$context.App;requested_at=(Get-Date).ToUniversalTime().ToString('o');reason='manual_maintenance'}
$deadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    foreach ($owned in @(Get-Mem0ServiceProcesses $context)) { $null = Stop-Mem0OwnedProcess $owned.ProcessId $context }
    Start-Sleep -Milliseconds 250
    $remaining = @(Get-Mem0ServiceProcesses $context)
    $probe = $null
    try { $probe=[IO.File]::Open($context.Lock,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None) } catch [IO.IOException] {}
    if ($probe) { $probe.Dispose(); if ($remaining.Count -eq 0) { Write-Output 'Mem0 stopped and paused. Run start.ps1 manually to resume.'; exit 0 } }
} while ((Get-Date) -lt $deadline)
throw 'Pause is recorded, but shutdown has not completed. Inspect automation/watchdog/status.json and logs/watchdog.'
