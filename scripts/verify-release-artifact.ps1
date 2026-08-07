<#
.SYNOPSIS
    Builds and verifies a ZIP containing exactly the tracked files from a Git tree.

.DESCRIPTION
    Uses `git archive`, never the working directory, then compares every archived
    file with `git ls-tree`. Refuses local configuration, credential material,
    dependency folders, caches, and generated output even if one was accidentally
    tracked. This check deliberately does not enforce legal/contact release gates;
    scripts\public-release.ps1 owns those gates.

.PARAMETER OutputPath
    Destination ZIP path. Relative paths are resolved from the repository root.

.PARAMETER Treeish
    Git tree or commit to archive. Defaults to HEAD.

.EXAMPLE
    .\scripts\verify-release-artifact.ps1 -OutputPath "$env:TEMP\hosted-agents.zip"
#>
[CmdletBinding()]
param(
    [string] $OutputPath = 'artifacts\hosted-agents-public-check.zip',
    [string] $Treeish = 'HEAD'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-GitText {
    param([Parameter(Mandatory)][string[]] $ArgumentList)

    $output = @(& git @ArgumentList)
    if ($LASTEXITCODE -ne 0) {
        throw "git $($ArgumentList -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output
}

function Test-ForbiddenArchivePath {
    param([Parameter(Mandatory)][string] $Path)

    $normalized = $Path.Replace('\', '/')
    $segments = @($normalized -split '/')
    $leaf = $segments[-1]

    $forbiddenDirectories = @(
        '.azure',
        '.venv',
        'venv',
        'node_modules',
        '__pycache__',
        '.pytest_cache',
        '.mypy_cache',
        '.ruff_cache',
        'dist',
        'secrets'
    )
    if (@($segments | Where-Object { $_ -in $forbiddenDirectories }).Count -gt 0) {
        return $true
    }

    if ($normalized -match '(^|/)\.foundry/(results|datasets)(/|$)') {
        return $true
    }

    $isEnvExample = $leaf -eq '.env.example' -or $leaf.EndsWith('.env.example')
    $isLocalEnv =
        $leaf -eq '.env' -or
        $leaf.StartsWith('.env.') -or
        $leaf.EndsWith('.env') -or
        $leaf.EndsWith('.azd.env')
    if ($isLocalEnv -and -not $isEnvExample) {
        return $true
    }

    $forbiddenNames = @(
        'local.settings.json',
        'secrets.json',
        'appsettings.Development.json'
    )
    if ($leaf -in $forbiddenNames) {
        return $true
    }

    return $leaf -match '\.(pem|key|pfx|p12|pubxml|publishsettings|pyc|pyo|log)$'
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git was not found.'
}

$gitRoot = @(Invoke-GitText -ArgumentList @('rev-parse', '--show-toplevel'))[0]
$expectedRoot = [System.IO.Path]::GetFullPath($repoRoot).TrimEnd('\', '/')
$actualRoot = [System.IO.Path]::GetFullPath($gitRoot).TrimEnd('\', '/')
if (-not $expectedRoot.Equals($actualRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Repository root mismatch. Expected '$expectedRoot', got '$actualRoot'."
}

Invoke-GitText -ArgumentList @('rev-parse', '--verify', "$Treeish^{tree}") | Out-Null

$archivePath = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    [System.IO.Path]::GetFullPath($OutputPath)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputPath))
}
$archiveDirectory = Split-Path -Parent $archivePath
New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null

$temporaryArchive = "$archivePath.tmp-$PID"
Remove-Item -Path $temporaryArchive -Force -ErrorAction SilentlyContinue

try {
    Invoke-GitText -ArgumentList @(
        'archive',
        '--format=zip',
        "--output=$temporaryArchive",
        $Treeish
    ) | Out-Null

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($temporaryArchive)
    try {
        $archiveFiles = @(
            $zip.Entries |
                Where-Object { -not [string]::IsNullOrEmpty($_.Name) } |
                ForEach-Object { $_.FullName.Replace('\', '/') } |
                Sort-Object -Unique
        )
    }
    finally {
        $zip.Dispose()
    }

    $trackedFiles = @(
        Invoke-GitText -ArgumentList @('ls-tree', '-r', '--name-only', $Treeish) |
            ForEach-Object { $_.Replace('\', '/') } |
            Sort-Object -Unique
    )

    $differences = @(Compare-Object -ReferenceObject $trackedFiles -DifferenceObject $archiveFiles)
    if ($differences.Count -gt 0) {
        $summary = $differences | Select-Object -First 20 | Out-String
        throw "Archive does not exactly match tracked files:`n$summary"
    }

    $forbidden = @($archiveFiles | Where-Object { Test-ForbiddenArchivePath -Path $_ })
    if ($forbidden.Count -gt 0) {
        throw "Archive contains forbidden local or sensitive paths:`n$($forbidden -join "`n")"
    }

    Move-Item -Path $temporaryArchive -Destination $archivePath -Force
    Write-Host "Verified tracked-file archive: $archivePath" -ForegroundColor Green
    Write-Host "Files: $($archiveFiles.Count)"
}
finally {
    Remove-Item -Path $temporaryArchive -Force -ErrorAction SilentlyContinue
}