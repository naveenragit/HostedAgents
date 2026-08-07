<#
.SYNOPSIS
    Validates and builds a public release artifact from committed files only.

.DESCRIPTION
    Fails closed unless the worktree is clean, an approved tracked LICENSE exists,
    owner placeholders are resolved, Gitleaks is installed, full validation passes,
    and both Git history and the extracted artifact pass secret scanning. The final
    ZIP is produced by `git archive` through verify-release-artifact.ps1.

    This repository intentionally has no LICENSE yet, so this command must fail
    until the owner/legal approver adds one without changing this script.

.PARAMETER OutputDirectory
    Destination directory for the verified ZIP. Relative paths are resolved from
    the repository root.

.PARAMETER ReleaseEvidencePath
    Path to the private, out-of-repository Responsible AI and evaluation evidence
    JSON for the exact candidate commit.

.EXAMPLE
    .\scripts\public-release.ps1
#>
[CmdletBinding()]
param(
    [string] $OutputDirectory = 'artifacts',
    [string] $ReleaseEvidencePath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$checkScript = Join-Path $PSScriptRoot 'check.ps1'
$archiveVerifier = Join-Path $PSScriptRoot 'verify-release-artifact.ps1'
$evidenceVerifier = Join-Path $PSScriptRoot 'verify-release-evidence.ps1'

function Invoke-GitText {
    param([Parameter(Mandatory)][string[]] $ArgumentList)

    $output = @(& git @ArgumentList)
    if ($LASTEXITCODE -ne 0) {
        throw "git $($ArgumentList -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output
}

function Assert-NoReleaseMarkers {
    $markers = @(
        ('OWNER' + '-CONFIGURE:'),
        ('RELEASE' + '-BLOCKER:'),
        ('TODO' + '-RELEASE:'),
        ('TBD' + '-RELEASE:'),
        ('CHANGE' + 'ME'),
        ('REPLACE' + '_ME')
    )

    $matches = [System.Collections.Generic.List[string]]::new()
    foreach ($marker in $markers) {
        $result = @(& git grep -n -I -F $marker HEAD --)
        if ($LASTEXITCODE -notin @(0, 1)) {
            throw "git grep failed while checking release markers."
        }
        foreach ($line in $result) {
            $matches.Add($line)
        }
    }

    if ($matches.Count -gt 0) {
        throw "Unresolved release placeholders remain:`n$($matches -join "`n")"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git was not found.'
}

Push-Location $repoRoot
try {
    Invoke-GitText -ArgumentList @('rev-parse', '--is-inside-work-tree') | Out-Null

    $worktreeState = @(git status --porcelain=v1 --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to inspect the Git worktree.'
    }
    if ($worktreeState.Count -gt 0) {
        throw "Release requires a clean worktree. Commit or remove all changes first:`n$($worktreeState -join "`n")"
    }

    $licensePath = Join-Path $repoRoot 'LICENSE'
    if (-not (Test-Path -Path $licensePath -PathType Leaf)) {
        throw 'Public release blocked: owner/legal must add an approved root LICENSE file.'
    }
    & git ls-files --error-unmatch -- LICENSE *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Public release blocked: LICENSE exists but is not tracked.'
    }

    Assert-NoReleaseMarkers

    if ([string]::IsNullOrWhiteSpace($ReleaseEvidencePath)) {
        throw 'Public release blocked: provide -ReleaseEvidencePath for private RAI and evaluation evidence.'
    }
    $candidateCommit = @(Invoke-GitText -ArgumentList @('rev-parse', 'HEAD'))[0]
    Write-Host '==> Private release evidence' -ForegroundColor Cyan
    & $evidenceVerifier -EvidencePath $ReleaseEvidencePath -ExpectedCommit $candidateCommit
    if ($LASTEXITCODE -ne 0) {
        throw "Release evidence validation failed with exit code $LASTEXITCODE"
    }

    $gitleaks = Get-Command gitleaks -ErrorAction SilentlyContinue
    if ($null -eq $gitleaks) {
        throw 'Gitleaks is required for full-history and artifact scans but was not found.'
    }

    Write-Host '==> Gitleaks full history' -ForegroundColor Cyan
    & $gitleaks.Source git --redact --no-banner '--log-opts=--all' $repoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Gitleaks history scan failed with exit code $LASTEXITCODE"
    }

    Write-Host '==> PowerShell syntax' -ForegroundColor Cyan
    $parseFailures = [System.Collections.Generic.List[string]]::new()
    foreach ($script in Get-ChildItem -Path $PSScriptRoot -Filter '*.ps1' -File) {
        $tokens = $null
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $script.FullName,
            [ref] $tokens,
            [ref] $errors
        ) | Out-Null
        foreach ($errorRecord in $errors) {
            $parseFailures.Add("$($script.Name): $($errorRecord.Message)")
        }
    }
    if ($parseFailures.Count -gt 0) {
        throw "PowerShell syntax validation failed:`n$($parseFailures -join "`n")"
    }

    Write-Host '==> Repository quality gate' -ForegroundColor Cyan
    & $checkScript
    if ($LASTEXITCODE -ne 0) {
        throw "Repository quality gate failed with exit code $LASTEXITCODE"
    }

    $commit = @(Invoke-GitText -ArgumentList @('rev-parse', '--short=12', 'HEAD'))[0]
    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) "hosted-agents-release-$PID"
    $temporaryArchive = Join-Path $temporaryRoot "hosted-agents-$commit.zip"
    $extractPath = Join-Path $temporaryRoot 'extracted'
    Remove-Item -Path $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

    try {
        Write-Host '==> Tracked-file artifact' -ForegroundColor Cyan
        & $archiveVerifier -OutputPath $temporaryArchive -Treeish HEAD
        if ($LASTEXITCODE -ne 0) {
            throw "Artifact verification failed with exit code $LASTEXITCODE"
        }

        Expand-Archive -Path $temporaryArchive -DestinationPath $extractPath

        Write-Host '==> Gitleaks extracted artifact' -ForegroundColor Cyan
        & $gitleaks.Source dir --redact --no-banner $extractPath
        if ($LASTEXITCODE -ne 0) {
            throw "Gitleaks artifact scan failed with exit code $LASTEXITCODE"
        }

        $resolvedOutputDirectory = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
            [System.IO.Path]::GetFullPath($OutputDirectory)
        }
        else {
            [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
        }
        New-Item -ItemType Directory -Path $resolvedOutputDirectory -Force | Out-Null
        $finalArchive = Join-Path $resolvedOutputDirectory "hosted-agents-$commit.zip"
        Move-Item -Path $temporaryArchive -Destination $finalArchive -Force

        $hash = (Get-FileHash -Path $finalArchive -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Host ''
        Write-Host "Public release artifact: $finalArchive" -ForegroundColor Green
        Write-Host "SHA256: $hash"
    }
    finally {
        Remove-Item -Path $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
finally {
    Pop-Location
}