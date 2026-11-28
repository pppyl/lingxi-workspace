param(
    [string]$ClientDirectory = $PSScriptRoot,
    [string]$ReportPath = (Join-Path $env:TEMP ('LingxiWorkspace-startup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt'))
)

# Read-only diagnostics. Never start the client or open an account database.
$ErrorActionPreference = 'Stop'
$report = [System.Collections.Generic.List[string]]::new()
$report.Add('LingxiWorkspace Windows startup diagnostics')
$report.Add('Time: ' + (Get-Date -Format o))
$report.Add('Windows: ' + [Environment]::OSVersion.VersionString)
$report.Add('64-bit OS: ' + [Environment]::Is64BitOperatingSystem)
$report.Add('Client directory: ' + $ClientDirectory)
$report.Add('')

$dataFolders = @((Join-Path $env:LOCALAPPDATA 'LingxiWorkspace'), (Join-Path $env:LOCALAPPDATA 'LingxiWorkspace\data'))
foreach ($scope in 'Process', 'User', 'Machine') {
    foreach ($name in 'LINGXI_DATA_DIR', 'LINGXI_HOST', 'LINGXI_PORT') {
        $value = [Environment]::GetEnvironmentVariable($name, $scope)
        $report.Add($scope + ' ' + $name + ': ' + $value)
        if ($name -eq 'LINGXI_DATA_DIR' -and $value) { $dataFolders += $value }
    }
}

$report.Add('')
$report.Add('Client files:')
if (Test-Path -LiteralPath $ClientDirectory -PathType Container) {
    $executables = @(Get-ChildItem -LiteralPath $ClientDirectory -Filter 'LingxiWorkspace*.exe' -File)
    foreach ($file in $executables) {
        $report.Add($file.Name + ' | bytes=' + $file.Length + ' | SHA256=' + (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash)
    }
    $report.Add('_internal present: ' + (Test-Path -LiteralPath (Join-Path $ClientDirectory '_internal') -PathType Container))
    if ($executables.Count -eq 0) { $report.Add('No LingxiWorkspace executable found in this directory.') }
} else { $report.Add('Client directory does not exist.') }

foreach ($folder in ($dataFolders | Select-Object -Unique)) {
    $report.Add('')
    $report.Add('Data directory: ' + $folder)
    $report.Add('Exists: ' + (Test-Path -LiteralPath $folder -PathType Container))
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) { continue }
    try {
        foreach ($name in 'accounts.sqlite3', 'accounts.sqlite3-wal', 'accounts.sqlite3-shm', 'desktop.lock', 'desktop-instance.json', 'desktop.log', 'recharge', 'migration-backups') {
            $path = Join-Path $folder $name
            if (Test-Path -LiteralPath $path) {
                $file = Get-Item -LiteralPath $path
                $report.Add($name + ' | bytes=' + $file.Length + ' | modified=' + $file.LastWriteTime.ToString('o'))
            }
        }
        $log = Join-Path $folder 'desktop.log'
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            $lines = @(Get-Content -LiteralPath $log -Encoding UTF8 -Tail 200)
            $startup = @($lines | Where-Object { $_ -match 'Desktop startup: version=' } | Select-Object -Last 1)
            if ($startup.Count -gt 0) { $report.Add('Last startup identity: ' + $startup[0]) }
            $start = -1
            for ($index = 0; $index -lt $lines.Count; $index++) {
                if ($lines[$index] -match 'Desktop startup or shutdown failed') { $start = $index }
            }
            if ($start -ge 0) {
                $report.Add('Last desktop failure:')
                for ($index = $start; $index -lt $lines.Count; $index++) {
                    if ($index -gt $start -and $lines[$index] -match '^(DEBUG|INFO|WARNING|ERROR|CRITICAL):') { break }
                    $report.Add(($lines[$index] -replace '1[3-9]\d{9}', '[phone]'))
                }
            } else {
                $report.Add('No desktop failure traceback in the last 200 log lines.')
                $report.Add('Waitress shutdown warnings: ' + @($lines | Where-Object { $_ -match '^WARNING:waitress:.*thread\(s\) still running' }).Count)
            }
        }
    } catch { $report.Add('Unable to inspect this directory: ' + $_.Exception.Message) }
}

$report.Add('')
$report.Add('Running clients:')
Get-CimInstance Win32_Process -Filter "Name='LingxiWorkspace.exe'" | ForEach-Object {
    $report.Add('PID=' + $_.ProcessId + ' | path=' + $_.ExecutablePath)
}
$report.Add('Port 5500 listeners:')
Get-NetTCPConnection -LocalPort 5500 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $report.Add($_.LocalAddress + ':5500 | PID=' + $_.OwningProcess)
}

$report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
Write-Output ('Report saved: ' + $ReportPath)
