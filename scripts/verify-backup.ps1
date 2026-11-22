param(
    [switch]$RequireTemporal
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $verifyRoot = Join-Path $root "backups\verify-$stamp"
    $backupArgs = @{ OutputDir = $verifyRoot }
    if ($RequireTemporal) { $backupArgs.RequireTemporal = $true }
    & (Join-Path $PSScriptRoot 'backup.ps1') @backupArgs
    if ($LASTEXITCODE -ne 0) { throw 'backup.ps1 failed.' }

    $backupDir = Get-ChildItem -Directory $verifyRoot |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $backupDir) { throw 'Backup directory was not created.' }

    $target = "actionflow_verify_$stamp"
    & (Join-Path $PSScriptRoot 'restore.ps1') -BackupDir $backupDir.FullName -TargetDatabase $target -DropAfter
    if ($LASTEXITCODE -ne 0) { throw 'restore.ps1 failed.' }

    $manifest = Get-Content -Encoding UTF8 (Join-Path $backupDir.FullName 'manifest.json') | ConvertFrom-Json
    $temporalDumps = @($manifest.temporal | Where-Object { $_ -and $_.database })
    if ($temporalDumps.Count -gt 0) {
        & (Join-Path $PSScriptRoot 'restore-temporal.ps1') -BackupDir $backupDir.FullName
        if ($LASTEXITCODE -ne 0) { throw 'restore-temporal.ps1 failed.' }
    } elseif ($RequireTemporal) {
        throw 'Temporal dumps are required but missing.'
    } else {
        Write-Warning 'Temporal is not running; only application database restore was verified.'
    }

    Write-Host "Backup verification completed: $($backupDir.FullName)"
    Write-Host 'The verification databases were dropped after successful restore.'
}
finally {
    Pop-Location
}
