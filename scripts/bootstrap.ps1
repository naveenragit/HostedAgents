<#
.SYNOPSIS
    Creates the local virtual environments and installs dependencies.

.DESCRIPTION
    Two venvs are required, by design:

      .\.venv                     Python 3.11 - developer tooling and the offline
                                  test suite. Agent runtime SDKs are NOT installed
                                  here (see the comment beside the install call).

      .\src\triage-agent\.venv    Python 3.13 - matches the code-deploy runtime
                                  (python_3_13). `azd ai agent run` resolves the
                                  venv relative to the SERVICE directory, so this
                                  one must live there, not at the repo root.

    See docs/implementation-plan.md section 2 for why the versions differ.

.EXAMPLE
    .\scripts\bootstrap.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

function Assert-PythonVersion {
    param([Parameter(Mandatory)][string] $Version)

    & py "-$Version" --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Python $Version was not found. Install it, or check 'py -0p'."
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
    & py "-$Version" -m venv $Path
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
# Tooling only. The agents' runtime SDKs are deliberately NOT installed here:
#   * the test suite is offline and imports only the pure config/tools modules;
#   * `research-agent` runs locally inside its container, not in this venv;
#   * on Windows/ARM64 there is no prebuilt `cryptography` wheel for cp311, so
#     installing azure-identity here would trigger a Rust + OpenSSL source build.
$rootVenv = Join-Path $repoRoot '.venv'
New-Venv -Version '3.11' -Path $rootVenv
Install-Requirements -VenvPath $rootVenv -RequirementsFiles @(
    (Join-Path $repoRoot 'requirements-dev.txt')
)

# --- Code-agent venv (Python 3.13, must live in the service directory) ------
$triageVenv = Join-Path $repoRoot 'src\triage-agent\.venv'
New-Venv -Version '3.13' -Path $triageVenv
Install-Requirements -VenvPath $triageVenv -RequirementsFiles @(
    (Join-Path $repoRoot 'src\triage-agent\requirements.txt')
)

Write-Host ''
Write-Host 'Bootstrap complete.' -ForegroundColor Green
Write-Host '  Dev tooling / tests  : .\.venv\Scripts\Activate.ps1            (Python 3.11)'
Write-Host '  Code agent (azd run) : .\src\triage-agent\.venv\Scripts\Activate.ps1  (Python 3.13)'
Write-Host '  Container agent runs in Docker via `azd ai agent run research-agent`.'
