param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,
    [switch]$InPlace,
    [switch]$Confirm,
    [switch]$KeepDatabases
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$script:composeEnv = @()
if (Test-Path (Join-Path $root '.env')) { $script:composeEnv = @('--env-file', '.env') }
Push-Location $root
try {
    $manifestPath = Join-Path $BackupDir 'manifest.json'
    if (-not (Test-Path $manifestPath)) { throw "No manifest.json in $BackupDir." }
    $manifest = Get-Content -Encoding UTF8 $manifestPath | ConvertFrom-Json
    $temporalDumps = @($manifest.temporal | Where-Object { $_ -and $_.database })
    if ($temporalDumps.Count -eq 0) { throw 'The backup contains no Temporal dumps.' }
    if ($InPlace -and -not $Confirm) {
        throw 'Restoring Temporal over the live environment requires -InPlace -Confirm.'
    }

    function Get-DotEnv {
        $values = @{}
        $path = Join-Path $root '.env'
        if (Test-Path $path) {
            foreach ($raw in Get-Content -Encoding UTF8 $path) {
                $line = $raw.Trim()
                if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
                $key, $value = $line.Split('=', 2)
                $values[$key.Trim()] = $value.Trim().Trim('"').Trim("'")
            }
        }
        return $values
    }

    function Get-Setting([string]$Name, [string]$Default = '') {
        $processValue = [Environment]::GetEnvironmentVariable($Name)
        if ($processValue) { return $processValue }
        if ($script:dotenv.ContainsKey($Name) -and $script:dotenv[$Name]) {
            return $script:dotenv[$Name]
        }
        return $Default
    }

    function Get-TemporalContainer {
        $composeArgs = @('compose') + $script:composeEnv + @('-f', 'infra/compose.yml', '-f', 'infra/temporal.compose.yml', 'ps', '-q', 'temporal-postgres')
        $result = & docker @composeArgs 2>$null
        $code = $LASTEXITCODE
        $container = $result | Select-Object -First 1
        if ($code -ne 0 -or -not $container) { return '' }
        return $container.Trim()
    }

    function Test-DatabaseExists([string]$Container, [string]$User, [string]$Password, [string]$Database) {
        $result = & docker exec -e "PGPASSWORD=$Password" $Container psql -U $User -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$Database'"
        $code = $LASTEXITCODE
        return ($code -eq 0 -and (($result -join '').Trim() -eq '1'))
    }

    function Restore-Dump([string]$Container, [string]$User, [string]$Password, [string]$Database, [string]$Path, [switch]$DropAfter) {
        if (-not (Test-DatabaseExists $Container $User $Password $Database)) {
            & docker exec -e "PGPASSWORD=$Password" $Container createdb -U $User $Database
            if ($LASTEXITCODE -ne 0) { throw "Failed to create database $Database" }
        }
        $remote = "/tmp/temporal-restore-$([guid]::NewGuid().ToString('N')).dump"
        try {
            & docker cp $Path "${Container}:$remote"
            if ($LASTEXITCODE -ne 0) { throw 'Failed to copy the Temporal dump into the container.' }
            & docker exec -e "PGPASSWORD=$Password" $Container pg_restore -U $User -d $Database --clean --if-exists --no-owner --no-privileges --exit-on-error $remote
            if ($LASTEXITCODE -ne 0) { throw "pg_restore failed for $Database" }
            $version = (& docker exec -e "PGPASSWORD=$Password" $Container psql -U $User -d $Database -tAc 'SELECT count(*) FROM schema_version').Trim()
            Write-Host "Restored Temporal database: $Database; schema_version rows: $version"
        }
        finally {
            & docker exec $Container rm -f $remote 2>$null | Out-Null
            if ($DropAfter) {
                & docker exec -e "PGPASSWORD=$Password" $Container dropdb -U $User --if-exists $Database
            }
        }
    }

    $script:dotenv = Get-DotEnv
    $container = Get-TemporalContainer
    if (-not $container) { throw 'temporal-postgres container is not running.' }
    $password = Get-Setting 'TEMPORAL_DB_PASSWORD' ''
    if (-not $password) { throw 'TEMPORAL_DB_PASSWORD is not set.' }

    if ($InPlace) {
        Write-Host 'Stopping Temporal server before restoring execution state...'
        $composeArgs = @('compose') + $script:composeEnv + @('-f', 'infra/compose.yml', '-f', 'infra/temporal.compose.yml', 'stop', 'temporal')
        & docker @composeArgs
        if ($LASTEXITCODE -ne 0) { throw 'Failed to stop Temporal.' }
    }

    $suffix = (Get-Date -Format 'yyyyMMddHHmmss')
    foreach ($item in $temporalDumps) {
        $sourceDatabase = $item.database
        $targetDatabase = if ($InPlace) { $sourceDatabase } else { "${sourceDatabase}_verify_$suffix" }
        $path = Join-Path $BackupDir $item.file
        if (-not (Test-Path $path)) { throw "Dump not found: $path" }
        Restore-Dump $container 'temporal' $password $targetDatabase $path -DropAfter:(-not $KeepDatabases -and -not $InPlace)
    }

    if ($InPlace) {
        Write-Host 'Starting Temporal server...'
        $composeArgs = @('compose') + $script:composeEnv + @('-f', 'infra/compose.yml', '-f', 'infra/temporal.compose.yml', 'up', '-d', 'temporal')
        & docker @composeArgs
        if ($LASTEXITCODE -ne 0) { throw 'Failed to start Temporal.' }
    }
}
finally {
    Pop-Location
}
