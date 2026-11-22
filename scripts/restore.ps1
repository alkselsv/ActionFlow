param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,
    [string]$TargetDatabase = 'actionflow_restore',
    [switch]$DropAfter,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$script:composeEnv = @()
if (Test-Path (Join-Path $root '.env')) { $script:composeEnv = @('--env-file', '.env') }
Push-Location $root
try {
    if ($TargetDatabase -notmatch '^[A-Za-z0-9_]+$') {
        throw 'TargetDatabase may contain only ASCII letters, digits and underscore.'
    }
    if (-not (Test-Path (Join-Path $BackupDir 'manifest.json'))) {
        throw "No manifest.json in $BackupDir."
    }
    $manifest = Get-Content -Encoding UTF8 (Join-Path $BackupDir 'manifest.json') | ConvertFrom-Json
    $appFile = Join-Path $BackupDir $manifest.app.file
    if (-not (Test-Path $appFile)) {
        throw "Application dump not found: $appFile"
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

    function Get-PostgresContainer {
        $composeArgs = @('compose') + $script:composeEnv + @('-f', 'infra/compose.yml', 'ps', '-q', 'postgres')
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

    $script:dotenv = Get-DotEnv
    $user = Get-Setting 'POSTGRES_USER' 'postgres'
    $sourceDatabase = $manifest.app.database
    $password = Get-Setting 'POSTGRES_PASSWORD' (Get-Setting 'ACTIONFLOW_DB_PASSWORD' '')
    $container = Get-PostgresContainer
    if (-not $container) { throw 'Application PostgreSQL container is not running.' }
    if ($TargetDatabase -eq $sourceDatabase -and -not $Force) {
        throw 'Restoring over the live database requires -Force and a stopped API.'
    }

    if (-not (Test-DatabaseExists $container $user $password $TargetDatabase)) {
        & docker exec -e "PGPASSWORD=$Password" $container createdb -U $user $TargetDatabase
        if ($LASTEXITCODE -ne 0) { throw "Failed to create database $TargetDatabase" }
    }

    $remote = "/tmp/actionflow-restore-$([guid]::NewGuid().ToString('N')).dump"
    try {
        & docker cp $appFile "${container}:$remote"
        if ($LASTEXITCODE -ne 0) { throw 'Failed to copy the dump into the container.' }
        & docker exec -e "PGPASSWORD=$password" $container pg_restore -U $user -d $TargetDatabase --clean --if-exists --no-owner --no-privileges --exit-on-error $remote
        if ($LASTEXITCODE -ne 0) { throw 'pg_restore failed.' }

        $hasVersion = (& docker exec -e "PGPASSWORD=$password" $container psql -U $user -d $TargetDatabase -tAc "SELECT to_regclass('public.alembic_version') IS NOT NULL").Trim()
        $version = if ($hasVersion -eq 't') { (& docker exec -e "PGPASSWORD=$password" $container psql -U $user -d $TargetDatabase -tAc 'SELECT version_num FROM alembic_version').Trim() } else { 'not_migrated' }
        $hasFindings = (& docker exec -e "PGPASSWORD=$password" $container psql -U $user -d $TargetDatabase -tAc "SELECT to_regclass('public.finding') IS NOT NULL").Trim()
        $findings = if ($hasFindings -eq 't') { (& docker exec -e "PGPASSWORD=$password" $container psql -U $user -d $TargetDatabase -tAc 'SELECT count(*) FROM finding').Trim() } else { '0' }
        $hasActions = (& docker exec -e "PGPASSWORD=$password" $container psql -U $user -d $TargetDatabase -tAc "SELECT to_regclass('public.manager_action') IS NOT NULL").Trim()
        $actions = if ($hasActions -eq 't') { (& docker exec -e "PGPASSWORD=$password" $container psql -U $user -d $TargetDatabase -tAc 'SELECT count(*) FROM manager_action').Trim() } else { '0' }
        Write-Host "Restored app database: $TargetDatabase"
        Write-Host "Schema version: $version; findings: $findings; manager actions: $actions"
    }
    finally {
        & docker exec $container rm -f $remote 2>$null | Out-Null
        if ($DropAfter -and $TargetDatabase -ne $sourceDatabase) {
            & docker exec -e "PGPASSWORD=$password" $container dropdb -U $user --if-exists $TargetDatabase
        }
    }
}
finally {
    Pop-Location
}
