<#
.SYNOPSIS
    Creates the local virtual environments and installs dependencies.

.DESCRIPTION
        Three venvs are required, by design:

      .\.venv                     Python 3.11 - developer tooling and the offline
                                  test suite. Agent runtime SDKs are NOT installed
                                  here (see the comment beside the install call).

            .\backend\api\.venv         Python 3.11 - FastAPI runtime dependencies.

            .\backend\agents\triage-agent\.venv
                                                                    Python 3.13 - matches the code-deploy runtime
                                                                    (python_3_13). `azd ai agent run` resolves the
                                                                    venv relative to the service directory.

        Frontend dependencies are installed from frontend\package-lock.json with
        `npm ci` so local setup and CI resolve the same dependency graph.

    See docs/implementation-plan.md section 2 for why the versions differ.

.EXAMPLE
    .\scripts\bootstrap.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-BasePython {
    param(
        [Parameter(Mandatory)][string] $Version,
        [Parameter(Mandatory)][string[]] $ArgumentList
    )

    $overrideName = "HOSTED_AGENTS_PYTHON_$($Version.Replace('.', ''))"
    $override = [Environment]::GetEnvironmentVariable($overrideName)
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        & $override @ArgumentList
        return
    }

    & py "-$Version" @ArgumentList
}

function Assert-PythonVersion {
    param([Parameter(Mandatory)][string] $Version)

    Invoke-BasePython -Version $Version -ArgumentList @('--version') *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Python $Version was not found. Install it, check 'py -0p', or set HOSTED_AGENTS_PYTHON_$($Version.Replace('.', ''))."
    }
}

function New-Venv {
    param(
        [Parameter(Mandatory)][string] $Version,
        [Parameter(Mandatory)][string] $Path
    )

    if (Test-Path -Path (Join-Path $Path 'pyvenv.cfg')) {
        Write-Host "[skip]  venv already exists: $Path"
        return
    }

    Write-Host "[create] Python $Version venv at $Path"
    Invoke-BasePython -Version $Version -ArgumentList @('-m', 'venv', $Path)
    if ($LASTEXITCODE -ne 0) { throw "Failed to create venv at $Path" }
}

function Install-Requirements {
    param(
        [Parameter(Mandatory)][string] $VenvPath,
        [Parameter(Mandatory)][string[]] $RequirementsFiles
    )

    $python = Join-Path $VenvPath 'Scripts\python.exe'
    $constraints = Join-Path $repoRoot 'constraints-local.txt'

    # `uv` is installed because `azd ai agent run` uses it (when present on the
    # activated venv) for a much faster dependency install. Installation of this
    # repo's own requirements goes through pip, which is the more reliable path
    # on restricted networks.
    & $python -m pip install --disable-pip-version-check --upgrade pip uv | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in $VenvPath" }

    foreach ($file in $RequirementsFiles) {
        Write-Host "[install] $file -> $VenvPath"
        # -c applies local-host-only constraints; see constraints-local.txt.
        & $python -m pip install --disable-pip-version-check -c $constraints -r $file
        if ($LASTEXITCODE -ne 0) { throw "Failed to install $file" }
    }
}

Assert-PythonVersion -Version '3.11'
Assert-PythonVersion -Version '3.13'

# --- Root dev venv (Python 3.11) -------------------------------------------
# Dev tooling plus the API's runtime dependencies, because the API test suite
# imports `app.*` (and therefore pymongo, pydantic and azure-identity).
#
# The AGENTS' SDKs are deliberately still excluded: their tests cover only pure
# config/tool modules, the two agents pin conflicting preview adapter versions,
# and `research-agent` runs inside its container rather than in a host venv.
$rootVenv = Join-Path $repoRoot '.venv'
New-Venv -Version '3.11' -Path $rootVenv
Install-Requirements -VenvPath $rootVenv -RequirementsFiles @(
    (Join-Path $repoRoot 'requirements-dev.txt')
    (Join-Path $repoRoot 'backend\api\requirements.txt')
)

# --- Code-agent venv (Python 3.13, must live in the service directory) ------
$triageVenv = Join-Path $repoRoot 'backend\agents\triage-agent\.venv'
New-Venv -Version '3.13' -Path $triageVenv
Install-Requirements -VenvPath $triageVenv -RequirementsFiles @(
    (Join-Path $repoRoot 'backend\agents\triage-agent\requirements.txt')
)

# --- FastAPI service venv (Python 3.11) -------------------------------------
# Separate from the root tooling venv so the service's runtime dependencies
# never leak into lint/type/test runs, and so it can be rebuilt independently.
$apiVenv = Join-Path $repoRoot 'backend\api\.venv'
New-Venv -Version '3.11' -Path $apiVenv
Install-Requirements -VenvPath $apiVenv -RequirementsFiles @(
    (Join-Path $repoRoot 'backend\api\requirements.txt')
)

# --- Frontend dependencies --------------------------------------------------
$frontendPath = Join-Path $repoRoot 'frontend'
$packageLock = Join-Path $frontendPath 'package-lock.json'
if (-not (Test-Path $packageLock)) {
    throw "Frontend lock file not found: $packageLock"
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
}
if ($null -eq $npmCommand) {
    throw 'npm was not found. Install a supported Node.js version and retry.'
}

Write-Host '[install] frontend dependencies (npm ci)'
Push-Location $frontendPath
try {
    & $npmCommand.Source ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host 'Bootstrap complete.' -ForegroundColor Green
Write-Host '  Dev tooling / tests  : .\.venv\Scripts\Activate.ps1                            (Python 3.11)'
Write-Host '  FastAPI service      : .\backend\api\.venv\Scripts\Activate.ps1                (Python 3.11)'
Write-Host '  Code agent (azd run) : .\backend\agents\triage-agent\.venv\Scripts\Activate.ps1 (Python 3.13)'
Write-Host '  Container agent runs in Docker via `azd ai agent run research-agent`.'
