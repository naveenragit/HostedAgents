<#
.SYNOPSIS
    Runs the full local quality gate: lint, format check, type check, tests and
    a dependency vulnerability audit.

.DESCRIPTION
    Requires the root venv created by scripts\bootstrap.ps1. Exits non-zero on
    the first failure so it can be wired straight into CI.

.EXAMPLE
    .\scripts\check.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "Root venv not found. Run .\scripts\bootstrap.ps1 first."
}

Push-Location $repoRoot
try {
    # Each agent is type-checked separately: both have a top-level `main.py`, so a
    # single mypy run over `src` collides on the duplicate module name.
    #
    # pip-audit is likewise run once per requirements file. The agents are
    # independent deployment units and their dependency sets legitimately differ
    # (they pin different protocol-adapter versions), so resolving them into one
    # environment would fail with a false conflict.
    $steps = [ordered]@{
        'ruff check'              = @('-m', 'ruff', 'check', '.')
        'ruff format'             = @('-m', 'ruff', 'format', '--check', '.')
        'mypy (research-agent)'   = @('-m', 'mypy', 'src\research-agent')
        'mypy (triage-agent)'     = @('-m', 'mypy', 'src\triage-agent')
        'mypy (tests)'            = @('-m', 'mypy', 'tests')
        'pytest'                  = @('-m', 'pytest')
        'pip-audit (research)'    = @('-m', 'pip_audit', '--strict', '-r', 'src\research-agent\requirements.txt')
        'pip-audit (triage)'      = @('-m', 'pip_audit', '--strict', '-r', 'src\triage-agent\requirements.txt')
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
    Write-Host 'All checks passed.' -ForegroundColor Green
}
finally {
    Pop-Location
}
