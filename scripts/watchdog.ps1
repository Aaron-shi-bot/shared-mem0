param(
    [string]$AppRoot = (Split-Path -Parent $PSScriptRoot),
    [ValidateRange(1,300)][int]$PollSeconds = 10,
    [ValidateRange(1,300)][int]$BackoffBaseSeconds = 5,
    [ValidateRange(1,3600)][int]$BackoffMaximumSeconds = 300,
    [ValidateRange(1,3600)][int]$StableSeconds = 60,
    [ValidateRange(1,3600)][int]$StartupGraceSeconds = 180
)
. (Join-Path $PSScriptRoot 'watchdog-common.ps1')
$context = Get-Mem0Context $AppRoot
$lock = $null
try { $lock = [IO.File]::Open($context.Lock, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
catch [IO.IOException] { Write-Output 'Mem0 watchdog already owns this installation.'; exit 0 }
$failures = 0
$retryAt = [DateTime]::MinValue
$lastPid = 0
$trackedProcess = $null
$healthySince = $null
$observedSince = Get-Date
$currentPrefix = ''
$lastPhase = ''
$lastExit = $null
$lastError = $null
$previous = Read-Mem0Json $context.Status
if ($previous -and $previous.app_root -eq $context.App) {
    $failures = [int]$previous.consecutive_failures
    if ($previous.next_retry_at) { try { $retryAt = [DateTime]::Parse($previous.next_retry_at).ToUniversalTime() } catch {} }
}
try {
    Write-Mem0Event $context 'watchdog_started' @{pid=$PID;app_root=$context.App}
    while ($true) {
        $phase = 'checking'
        try {
            if ([IO.File]::Exists($context.Pause)) {
                foreach ($owned in @(Get-Mem0ServiceProcesses $context)) { $null = Stop-Mem0OwnedProcess $owned.ProcessId $context }
                Write-Mem0Json $context.Status @{format='mem0-watchdog-v1';app_root=$context.App;phase='paused';watchdog_pid=$null;service_pid=$null;updated_at=(Get-Date).ToUniversalTime().ToString('o');consecutive_failures=$failures;next_retry_at=$null;last_error=$null}
                Write-Mem0Event $context 'paused' @{reason='manual_stop'}
                break
            }
            $owned = @(Get-Mem0ServiceProcesses $context)
            if ($owned.Count -gt 1) { $phase='conflict'; $lastError='multiple_owned_processes' }
            elseif ($owned.Count -eq 1) {
                $servicePid = [int]$owned[0].ProcessId
                if ($lastPid -ne $servicePid) {
                    $lastPid = $servicePid; $observedSince=Get-Date; $healthySince=$null
                    if ($trackedProcess) { $trackedProcess.Dispose() }
                    $trackedProcess = [Diagnostics.Process]::GetProcessById($servicePid)
                    $null = $trackedProcess.Handle
                    Write-Mem0Event $context 'service_observed' @{pid=$servicePid}
                }
                $health = Get-Mem0Health $context
                if ($health.Ready) {
                    $phase='ready'; $lastError=$null
                    if (-not $healthySince) { $healthySince=Get-Date }
                    if (((Get-Date) - $healthySince).TotalSeconds -ge $StableSeconds) { $failures=0; $retryAt=[DateTime]::MinValue }
                } else {
                    $healthySince=$null
                    if (((Get-Date) - $observedSince).TotalSeconds -lt $StartupGraceSeconds) { $phase='starting'; $lastError='health_not_ready' }
                    else { $phase='degraded'; $lastError='process_alive_but_health_not_ready' }
                    # A living process owns the DB. Never launch another process based on health alone.
                }
            } else {
                if ($lastPid) {
                    $lastExit=$null
                    try { if ($trackedProcess -and $trackedProcess.HasExited) { $lastExit=$trackedProcess.ExitCode } } catch {}
                    $failures++; $retryAt=(Get-Date).ToUniversalTime().AddSeconds((Get-Mem0Backoff $failures $BackoffBaseSeconds $BackoffMaximumSeconds))
                    Write-Mem0Event $context 'service_exited' @{pid=$lastPid;exit_code=$lastExit;failures=$failures;retry_at=$retryAt.ToString('o')}
                    $lastPid=0; $healthySince=$null; $lastError='service_exited'
                }
                if ((Get-Date).ToUniversalTime() -lt $retryAt) { $phase='backoff' }
                elseif (Test-Mem0Port $context) {
                    $failures++; $retryAt=(Get-Date).ToUniversalTime().AddSeconds((Get-Mem0Backoff $failures $BackoffBaseSeconds $BackoffMaximumSeconds)); $phase='blocked'; $lastError='port_in_use_by_unverified_process'
                } else {
                    # Recheck both the pause request and the process inventory immediately before launch.
                    if (-not [IO.File]::Exists($context.Pause) -and @(Get-Mem0ServiceProcesses $context).Count -eq 0) {
                        try {
                            $started = Start-Mem0Child $context
                            if ($started) {
                                if ($trackedProcess) { $trackedProcess.Dispose() }
                                $trackedProcess=$started.Process; $lastPid=$trackedProcess.Id; $currentPrefix=$started.Prefix; $observedSince=Get-Date
                                $phase='starting'; $lastError=$null
                                Write-Mem0Event $context 'service_started' @{pid=$lastPid;log_prefix=$currentPrefix}
                                Remove-Mem0ExpiredLogs $context $currentPrefix
                            }
                        } catch {
                            $failures++; $retryAt=(Get-Date).ToUniversalTime().AddSeconds((Get-Mem0Backoff $failures $BackoffBaseSeconds $BackoffMaximumSeconds))
                            $lastError='launch_failed:' + $_.Exception.GetType().Name; $phase='backoff'
                            Write-Mem0Event $context 'launch_failed' @{error=$lastError;failures=$failures;retry_at=$retryAt.ToString('o')}
                        }
                    }
                }
            }
            if ($phase -ne $lastPhase) { Write-Mem0Event $context 'phase_changed' @{phase=$phase;error=$lastError}; $lastPhase=$phase }
            Write-Mem0Json $context.Status @{format='mem0-watchdog-v1';app_root=$context.App;data_root=$context.Data;phase=$phase;watchdog_pid=$PID;service_pid=$lastPid;updated_at=(Get-Date).ToUniversalTime().ToString('o');consecutive_failures=$failures;next_retry_at=$(if ($retryAt -gt [DateTime]::MinValue) {$retryAt.ToString('o')} else {$null});last_exit_code=$lastExit;last_error=$lastError;log_prefix=$currentPrefix}
        } catch {
            try { Write-Mem0Event $context 'watchdog_iteration_failed' @{error=$_.Exception.GetType().Name;message=$_.Exception.Message} } catch {}
            Start-Sleep -Seconds ([Math]::Max(5,$PollSeconds))
        }
        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    if ($trackedProcess) { $trackedProcess.Dispose() }
    if ($lock) { $lock.Dispose() }
}
