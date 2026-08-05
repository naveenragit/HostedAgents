<#
.SYNOPSIS
    Creates the chat database, collections and indexes on the Cosmos DB for
    MongoDB (vCore) cluster, then verifies connectivity.

.DESCRIPTION
    Idempotent. Reads configuration from backend/api/.env (which is git-ignored),
    authenticates with your `az login` identity via MONGODB-OIDC, and calls the
    same `ensure_schema()` the API runs at start-up — so this script cannot drift
    from the application.

    MongoDB creates a database lazily on first write. Running this makes the
    database, collections and indexes exist up front, so the first request is not
    also a schema migration.

    Prerequisites:
      * `az login` completed.
      * Your Entra identity registered on the cluster with read/write access.
        See docs/implementation-plan.md section 6.

.EXAMPLE
    .\scripts\init-database.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot 'backend\api\.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "API venv not found. Run .\scripts\bootstrap.ps1 first."
}

$envFile = Join-Path $repoRoot 'backend\api\.env'
if (-not (Test-Path $envFile)) {
    throw "backend\api\.env not found. Copy backend\api\.env.example to .env and fill it in."
}

Write-Host 'Connecting with your az login identity (MONGODB-OIDC)...' -ForegroundColor Cyan

Push-Location (Join-Path $repoRoot 'backend\api')
try {
    & $python -m app.cli.init_database
    if ($LASTEXITCODE -ne 0) {
        throw "Database initialisation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
