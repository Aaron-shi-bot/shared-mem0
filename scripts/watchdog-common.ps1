$ErrorActionPreference = 'Stop'

function Write-Mem0Json([string]$Path, $Value) {
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path)) | Out-Null
    $temporary = $Path + '.tmp-' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Read-Mem0Json([string]$Path) {
    try { return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json } catch { return $null }
}

function Get-Mem0Context([string]$AppRoot, [string]$DataRoot = '', [int]$Port = 0) {
    $app = [IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
    $settings = Read-Mem0Json (Join-Path $app 'config/settings.json')
    if (-not $settings) { throw 'Mem0 settings.json is missing or invalid.' }
    $data = [IO.Path]::GetFullPath([string]$settings.data_root).TrimEnd('\')
    if ($DataRoot -and [IO.Path]::GetFullPath($DataRoot).TrimEnd('\') -ne $data) { throw 'Data directory does not match this installation.' }
    if ($Port -and $Port -ne [int]$settings.port) { throw 'Port does not match this installation.' }
    if ([int]$settings.port -lt 1024 -or [int]$settings.port -gt 65535) { throw 'Invalid local service port.' }
    $interpreter = Join-Path $app 'runtime/pythonw.exe'
    $launcher = $null
    if (-not [IO.File]::Exists($interpreter)) {
        $venvFile = Join-Path $app 'venv/pyvenv.cfg'
        $line = [IO.File]::ReadAllLines($venvFile) | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
        if (-not $line) { throw 'Cannot locate the windowless Python interpreter.' }
        $pythonHome = ($line -replace '^home\s*=\s*', '').Trim()
        $interpreter = Join-Path $pythonHome 'pythonw.exe'
        $launcher = Join-Path $app 'venv/Scripts/pythonw.exe'
    }
    if (-not [IO.File]::Exists($interpreter)) { throw 'Windowless Python interpreter is missing.' }
    $state = Join-Path $data 'automation/watchdog'
    $logs = Join-Path $data 'logs/watchdog'
    $serviceLogs = Join-Path $data 'logs/service'
    foreach ($folder in @($state, $logs, $serviceLogs)) { [IO.Directory]::CreateDirectory($folder) | Out-Null }
    return @{ App=$app; Data=$data; Port=[int]$settings.port; Interpreter=[IO.Path]::GetFullPath($interpreter);
              Launcher=$launcher; Server=(Join-Path $app 'app/server.py'); State=$state; Logs=$logs; ServiceLogs=$serviceLogs;
              Pause=(Join-Path $state 'pause.json'); Status=(Join-Path $state 'status.json'); Lock=(Join-Path $state 'instance.lock') }
}

function Get-Mem0ScriptArgument([string]$CommandLine) {
    $arguments = @([regex]::Matches($CommandLine, '(?:^|\s)(?:"([^"\r\n]*)"|([^\s"]+))') | ForEach-Object {
        if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value }
    })
    for ($i = 1; $i -lt $arguments.Count; $i++) {
        $argument = $arguments[$i]
        if ($argument.StartsWith('-c') -or $argument.StartsWith('-m')) { return $null }
        if ($argument -in @('-X', '-W')) { $i++; continue }
        if ($argument -eq '--') { if ($i + 1 -lt $arguments.Count) { return $arguments[$i + 1] }; return $null }
        if ($argument -in @('-B','-u','-E','-I','-s','-S','-O','-OO','-q','-v','-vv','-P','-b','-bb','-d')) { continue }
        if ($argument.StartsWith('-X') -or $argument.StartsWith('-W')) { continue }
        if ($argument.StartsWith('-')) { return $null }
        return $argument
    }
    return $null
}

function Test-Mem0ServiceProcess($Process, $Context) {
    if (-not $Process -or -not $Process.ExecutablePath -or -not $Process.CommandLine) { return $false }
    if ($Process.Name -notin @('python.exe', 'pythonw.exe')) { return $false }
    $allowed = @($Context.Interpreter, (Join-Path ([IO.Path]::GetDirectoryName($Context.Interpreter)) 'python.exe'))
    if ($Context.Launcher) { $allowed += $Context.Launcher; $allowed += Join-Path $Context.App 'venv/Scripts/python.exe' }
    try {
        $executable = [IO.Path]::GetFullPath($Process.ExecutablePath)
        if (-not ($allowed | Where-Object { [IO.Path]::GetFullPath($_) -eq $executable })) { return $false }
        $script = Get-Mem0ScriptArgument $Process.CommandLine
        return ($script -and [IO.Path]::GetFullPath($script) -eq [IO.Path]::GetFullPath($Context.Server))
    } catch { return $false }
}

function Get-Mem0ServiceProcesses($Context) {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object { Test-Mem0ServiceProcess $_ $Context })
}

function Stop-Mem0OwnedProcess([int]$ProcessId, $Context) {
    $snapshot = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcessId)
    if (-not (Test-Mem0ServiceProcess $snapshot $Context)) { return $false }
    $handle = $null
    try {
        $handle = [Diagnostics.Process]::GetProcessById($ProcessId)
        $null = $handle.Handle
        if ([Math]::Abs(($handle.StartTime.ToUniversalTime() - $snapshot.CreationDate.ToUniversalTime()).TotalSeconds) -gt 1) { return $false }
        $handle.Kill()
        return $true
    } catch [ArgumentException] { return $false }
    catch [InvalidOperationException] { return $false }
    finally { if ($handle) { $handle.Dispose() } }
}

function Get-Mem0Health($Context) {
    $response = $null
    $reader = $null
    try {
        $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:' + $Context.Port + '/health')
        $request.Proxy = $null
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $response = $request.GetResponse()
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        $health = $reader.ReadToEnd() | ConvertFrom-Json
        return @{ Responding=$true; Service=($health.service -eq 'mem0-local'); Ready=($health.service -eq 'mem0-local' -and $health.ready -eq $true) }
    } catch { return @{Responding=$false; Service=$false; Ready=$false} }
    finally { if ($reader) { $reader.Dispose() }; if ($response) { $response.Dispose() } }
}

function Test-Mem0Port($Context) {
    $client = [Net.Sockets.TcpClient]::new()
    try { $task = $client.ConnectAsync('127.0.0.1', $Context.Port); return ($task.Wait(500) -and $client.Connected) }
    catch { return $false }
    finally { $client.Dispose() }
}

function Get-Mem0Backoff([int]$Failures, [double]$Base = 5, [double]$Maximum = 300) {
    return [Math]::Min($Maximum, $Base * [Math]::Pow(2, [Math]::Min(20, [Math]::Max(0, $Failures - 1))))
}

function Write-Mem0Event($Context, [string]$Event, $Details = @{}) {
    $file = Join-Path $Context.Logs ((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd') + '.jsonl')
    $record = @{at=(Get-Date).ToUniversalTime().ToString('o');event=$Event;details=$Details}
    [IO.File]::AppendAllText($file, (($record | ConvertTo-Json -Compress -Depth 6) + "`n"), [Text.UTF8Encoding]::new($false))
}

function Remove-Mem0ExpiredLogs($Context, [string]$CurrentPrefix = '', [int]$Days = 30, [int]$KeepRuns = 100) {
    # Only this watchdog's regular log files are eligible; legacy logs remain intact.
    $cutoff = (Get-Date).ToUniversalTime().AddDays(-$Days)
    $files = @(Get-ChildItem -LiteralPath $Context.ServiceLogs -File | Where-Object { $_.Name -match '^server-[0-9]{8}T[0-9]{9}Z-[a-f0-9]{8}\.(out|err)\.log$' })
    $prefixes = @($files | ForEach-Object { $_.Name -replace '\.(out|err)\.log$', '' } | Sort-Object -Unique -Descending)
    $keep = @($prefixes | Select-Object -First $KeepRuns)
    $candidates = @($files | Where-Object { $prefix = $_.Name -replace '\.(out|err)\.log$', ''; $prefix -ne $CurrentPrefix -and ($_.LastWriteTimeUtc -lt $cutoff -or $prefix -notin $keep) })
    $candidates += @(Get-ChildItem -LiteralPath $Context.Logs -File | Where-Object { $_.Name -match '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl$' -and $_.LastWriteTimeUtc -lt $cutoff })
    $launcherLogs = @(Get-ChildItem -LiteralPath $Context.Logs -File | Where-Object { $_.Name -match '^launcher-[0-9]{8}T[0-9]{9}Z-[0-9]+\.(out|err)\.log$' } | Sort-Object LastWriteTimeUtc -Descending)
    $candidates += @($launcherLogs | Select-Object -Skip ($KeepRuns * 2))
    $candidates += @($launcherLogs | Where-Object { $_.LastWriteTimeUtc -lt $cutoff })
    foreach ($file in $candidates) {
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $resolved = [IO.Path]::GetFullPath($file.FullName)
        if ($resolved.StartsWith([IO.Path]::GetFullPath($Context.Data) + '\logs\', [StringComparison]::OrdinalIgnoreCase)) {
            try { [IO.File]::Delete($resolved) } catch { Write-Mem0Event $Context 'log_cleanup_failed' @{file=$file.Name;error=$_.Exception.GetType().Name} }
        }
    }
}

function Start-Mem0Child($Context) {
    if ([IO.File]::Exists($Context.Pause)) { return $null }
    $prefix = 'server-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
    $prior = [Environment]::GetEnvironmentVariable('__PYVENV_LAUNCHER__', 'Process')
    try {
        if ($Context.Launcher) { [Environment]::SetEnvironmentVariable('__PYVENV_LAUNCHER__', $Context.Launcher, 'Process') }
        else { [Environment]::SetEnvironmentVariable('__PYVENV_LAUNCHER__', $null, 'Process') }
        if ([IO.File]::Exists($Context.Pause)) { return $null }
        $process = Start-Process -FilePath $Context.Interpreter -ArgumentList ('-B -X utf8 -u "' + $Context.Server + '"') -WorkingDirectory $Context.App -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Context.ServiceLogs ($prefix + '.out.log')) -RedirectStandardError (Join-Path $Context.ServiceLogs ($prefix + '.err.log'))
        return @{Process=$process;Prefix=$prefix}
    } finally { [Environment]::SetEnvironmentVariable('__PYVENV_LAUNCHER__', $prior, 'Process') }
}
