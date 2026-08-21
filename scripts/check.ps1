<#
.SYNOPSIS
    Runs the full local quality gate for Python and the frontend.

.DESCRIPTION
    Requires the root venv and frontend dependencies created by
    scripts\bootstrap.ps1. Runs Python lint, format, type, test and dependency
    audits, followed by frontend tests (when configured), type checking, lint,
    build and production/full dependency audits. Exits on the first failure.

.EXAMPLE
    .\scripts\check.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$triagePath = Join-Path $repoRoot 'backend\agents\triage-agent'
$triagePython = Join-Path $triagePath '.venv\Scripts\python.exe'
$frontendPath = Join-Path $repoRoot 'frontend'

if (-not (Test-Path $python)) {
    throw "Root venv not found. Run .\scripts\bootstrap.ps1 first."
}
if (-not (Test-Path $triagePython)) {
    throw "Triage-agent Python 3.13 venv not found. Run .\scripts\bootstrap.ps1 first."
}
if (-not (Test-Path (Join-Path $frontendPath 'node_modules'))) {
    throw "Frontend dependencies not found. Run .\scripts\bootstrap.ps1 first."
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
}
if ($null -eq $npmCommand) {
    throw 'npm was not found. Install a supported Node.js version and retry.'
}

Push-Location $repoRoot
try {
    # Each agent is type-checked separately: both have a top-level `main.py`, so a
    # single mypy run over `backend` collides on the duplicate module name.
    #
    # pip-audit is likewise run once per requirements file. The agents and the API
    # are independent deployment units and their dependency sets legitimately
    # differ (the agents pin different protocol-adapter versions), so resolving
    # them into one environment would fail with a false conflict.
    $steps = [ordered]@{
        'ruff check'              = @('-m', 'ruff', 'check', '.')
        'ruff format'             = @('-m', 'ruff', 'format', '--check', '.')
        'mypy (research-agent)'   = @('-m', 'mypy', 'backend\agents\research-agent')
        'mypy (triage-agent)'     = @('-m', 'mypy', 'backend\agents\triage-agent')
        'mypy (api)'              = @('-m', 'mypy', 'backend\api\app')
        'mypy (billing-sync)'     = @('-m', 'mypy', 'backend\functions\billing-sync\billing_sync')
        'mypy (tests)'            = @('-m', 'mypy', 'backend\tests')
        'pytest'                  = @('-m', 'pytest')
        'pip-audit (research)'    = @('-m', 'pip_audit', '--strict', '-r', 'backend\agents\research-agent\requirements.txt')
        'pip-audit (triage)'      = @('-m', 'pip_audit', '--strict', '-r', 'backend\agents\triage-agent\requirements.txt')
        'pip-audit (api)'         = @('-m', 'pip_audit', '--strict', '-r', 'backend\api\requirements.txt')
        'pip-audit (billing)'     = @('-m', 'pip_audit', '--strict', '-r', 'backend\functions\billing-sync\requirements.txt')
        'pip-audit (dev tooling)' = @('-m', 'pip_audit', '--strict', '-r', 'requirements-dev.txt')
    }

    foreach ($name in $steps.Keys) {
        Write-Host ''
        Write-Host "==> $name" -ForegroundColor Cyan
        & $python @($steps[$name])
        if ($LASTEXITCODE -ne 0) {
            throw "$name failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host ''
    Write-Host '==> triage runtime contracts (Python 3.13)' -ForegroundColor Cyan
    Push-Location $triagePath
    try {
        & $triagePython -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) {
            throw "triage runtime contracts failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    Push-Location $frontendPath
    try {
        $frontendSteps = [ordered]@{
            'frontend test'             = @('run', 'test', '--if-present')
            'frontend typecheck'        = @('run', 'typecheck')
            'frontend lint'             = @('run', 'lint')
            'frontend build'            = @('run', 'build')
            'npm audit (production)'    = @('audit', '--omit=dev', '--audit-level=high')
            'npm audit (all packages)'  = @('audit', '--audit-level=high')
        }

        foreach ($name in $frontendSteps.Keys) {
            Write-Host ''
            Write-Host "==> $name" -ForegroundColor Cyan
            [string[]] $arguments = $frontendSteps[$name]
            & $npmCommand.Source @arguments
            if ($LASTEXITCODE -ne 0) {
                throw "$name failed with exit code $LASTEXITCODE"
            }
        }
    }
    finally {
        Pop-Location
    }

    Write-Host ''
    Write-Host 'All checks passed.' -ForegroundColor Green
}
finally {
    Pop-Location
}
