$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    uv run --project backend --frozen pytest -c backend/pyproject.toml backend/tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    uv run --project backend --frozen ruff check backend
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    npm.cmd --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    uv run --project backend --frozen python scripts/validate_fixtures.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
