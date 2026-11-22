param(
    [string]$OutputDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'backups'),
    [switch]$SkipTemporal,
    [switch]$RequireTemporal,
    [switch]$Quiesced
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$script:composeEnv = @()
if (Test-Path (Join-Path $root '.env')) { $script:composeEnv = @('--env-file', '.env') }
Push-Location $root
try {
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

    function Get-ComposeContainer([string]$Service, [switch]$Temporal) {
        $composeArgs = @('compose') + $script:composeEnv + @('-f', 'infra/compose.yml')
        if ($Temporal) { $composeArgs += @('-f', 'infra/temporal.compose.yml') }
        $composeArgs += @('ps', '-q', $Service)
        $result = & docker @composeArgs 2>$null
        $code = $LASTEXITCODE
        $container = $result | Select-Object -First 1
        if ($code -ne 0 -or -not $container) { return '' }
        return $container.Trim()
    }

    function Save-DatabaseDump(
        [string]$Container,
        [string]$User,
        [string]$Password,
        [string]$Database,
        [string]$Path
    ) {
        $remote = "/tmp/actionflow-$([guid]::NewGuid().ToString('N')).dump"
        & docker exec -e "PGPASSWORD=$Password" $Container pg_dump -U $User -d $Database -Fc -f $remote
        if ($LASTEXITCODE -ne 0) { throw "pg_dump failed for database $Database" }
        & docker cp "${Container}:$remote" $Path
        if ($LASTEXITCODE -ne 0) { throw "docker cp failed for database $Database" }
        & docker exec $Container rm -f $remote | Out-Null
        $item = Get-Item $Path
        return @{
            database = $Database
            file = $item.Name
            bytes = $item.Length
            sha256 = (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant()
        }
    }

    $script:dotenv = Get-DotEnv
    if (-not (Test-Path '.env') -and -not $env:POSTGRES_PASSWORD -and -not $env:ACTIONFLOW_DB_PASSWORD) {
        throw 'Missing .env and neither POSTGRES_PASSWORD nor ACTIONFLOW_DB_PASSWORD is set.'
    }

    $user = Get-Setting 'POSTGRES_USER' 'postgres'
    $database = Get-Setting 'POSTGRES_DB' 'actionflow'
    $password = Get-Setting 'POSTGRES_PASSWORD' (Get-Setting 'ACTIONFLOW_DB_PASSWORD' '')

    $appContainer = Get-ComposeContainer 'postgres'
    if (-not $appContainer) {
        throw 'Application PostgreSQL container is not running. Start it with docker compose up -d postgres.'
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = Join-Path $OutputDir $stamp
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

    Write-Host "Backup directory: $backupDir"
    $app = Save-DatabaseDump $appContainer $user $password $database (Join-Path $backupDir 'app.dump')

    $temporal = @()
    if (-not $SkipTemporal) {
        $temporalContainer = Get-ComposeContainer 'temporal-postgres' -Temporal
        if ($temporalContainer) {
            $temporalPassword = Get-Setting 'TEMPORAL_DB_PASSWORD' ''
            if (-not $temporalPassword) {
                throw 'TEMPORAL_DB_PASSWORD is not set; cannot dump Temporal.'
            }
            foreach ($temporalDatabase in @('temporal', 'temporal_visibility')) {
                $temporal += Save-DatabaseDump $temporalContainer 'temporal' $temporalPassword $temporalDatabase (Join-Path $backupDir "$temporalDatabase.dump")
            }
        } elseif ($RequireTemporal) {
            throw 'temporal-postgres is not running, but -RequireTemporal was supplied.'
        } else {
            Write-Warning 'temporal-postgres is not running; Temporal execution-state dumps were not created.'
        }
    }

    $manifest = @{
        version = 1
        created_at = (Get-Date).ToUniversalTime().ToString('o')
        git_commit = (git rev-parse HEAD 2>$null)
        quiesced = $Quiesced.IsPresent
        note = 'For a consistent copy, stop API, worker, dispatcher and Temporal server before running backup.ps1.'
        app = $app
        temporal = $temporal
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $backupDir 'manifest.json')
    Write-Host 'Backup complete.'
}
finally {
    Pop-Location
}
