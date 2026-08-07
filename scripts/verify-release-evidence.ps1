<#
.SYNOPSIS
    Validates private Responsible AI and evaluation evidence for a release commit.

.DESCRIPTION
    Checks the evidence schema and semantic links between the exact source commit,
    both hosted-agent versions, model/content-filter bindings, predeclared gates,
    and passing evaluation results. The evidence file must remain outside the
    repository because its references can identify a live environment.

.PARAMETER EvidencePath
    Absolute or relative path to a private JSON evidence file outside this repository.

.PARAMETER ExpectedCommit
    Full 40-character source commit. Defaults to the current repository HEAD.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $EvidencePath,

    [string] $ExpectedCommit
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$schemaPath = Join-Path $repoRoot 'docs\release-evidence.schema.json'

function Convert-EvidenceTimestamp {
    param(
        [Parameter(Mandatory)][string] $Value,
        [Parameter(Mandatory)][string] $Field
    )

    $parsed = [DateTimeOffset]::MinValue
    $valid = [DateTimeOffset]::TryParse(
        $Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref] $parsed
    )
    if (-not $valid) {
        throw "Release evidence field '$Field' is not a valid date-time."
    }
    return $parsed
}

function Assert-PrivateReference {
    param(
        [Parameter(Mandatory)][string] $Value,
        [Parameter(Mandatory)][string] $Field
    )

    $placeholderPattern = '(?i)(<[^>]+>|placeholder|change.?me|replace.?me|owner-configure|tbd|todo|example\.(com|org|net))'
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -lt 8 -or $Value -match $placeholderPattern) {
        throw "Release evidence field '$Field' must contain a non-placeholder private record reference."
    }
}

if (-not (Test-Path -LiteralPath $schemaPath -PathType Leaf)) {
    throw "Release evidence schema was not found at '$schemaPath'."
}

$resolvedEvidencePath = (Resolve-Path -LiteralPath $EvidencePath -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedEvidencePath -PathType Leaf)) {
    throw "Release evidence path is not a file: '$resolvedEvidencePath'."
}

$normalizedRepoRoot = [IO.Path]::GetFullPath($repoRoot).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$normalizedEvidencePath = [IO.Path]::GetFullPath($resolvedEvidencePath)
if ($normalizedEvidencePath.StartsWith($normalizedRepoRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Release evidence must be supplied from outside the repository and must not be committed.'
}

$rawEvidence = Get-Content -LiteralPath $resolvedEvidencePath -Raw
if (-not ($rawEvidence | Test-Json -SchemaFile $schemaPath)) {
    throw 'Release evidence does not conform to docs/release-evidence.schema.json.'
}
$evidence = $rawEvidence | ConvertFrom-Json

if ([string]::IsNullOrWhiteSpace($ExpectedCommit)) {
    $ExpectedCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to resolve the repository HEAD commit.'
    }
}
if ($ExpectedCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw 'ExpectedCommit must be a full 40-character Git commit.'
}
$normalizedCommit = $ExpectedCommit.ToLowerInvariant()
if ($evidence.sourceCommit.ToLowerInvariant() -ne $normalizedCommit) {
    throw 'Release evidence sourceCommit does not match the candidate commit.'
}

$now = [DateTimeOffset]::UtcNow
$createdAt = Convert-EvidenceTimestamp -Value $evidence.evidenceCreatedAt -Field 'evidenceCreatedAt'
$expiresAt = Convert-EvidenceTimestamp -Value $evidence.expiresAt -Field 'expiresAt'
$approvedAt = Convert-EvidenceTimestamp -Value $evidence.releaseApproval.approvedAt -Field 'releaseApproval.approvedAt'
$thresholdsDefinedAt = Convert-EvidenceTimestamp -Value $evidence.evaluationGate.thresholdsDefinedAt -Field 'evaluationGate.thresholdsDefinedAt'

if ($createdAt -gt $now.AddMinutes(5)) {
    throw 'Release evidence creation time is in the future.'
}
if ($expiresAt -le $now -or $expiresAt -le $createdAt) {
    throw 'Release evidence is expired or has an invalid validity window.'
}
if ($approvedAt -gt $now.AddMinutes(5)) {
    throw 'Release approval time is in the future.'
}
if ($thresholdsDefinedAt -gt $now.AddMinutes(5)) {
    throw 'Evaluation thresholds were recorded in the future.'
}

Assert-PrivateReference -Value $evidence.releaseApproval.approvalReference -Field 'releaseApproval.approvalReference'
Assert-PrivateReference -Value $evidence.releaseApproval.approverReference -Field 'releaseApproval.approverReference'
Assert-PrivateReference -Value $evidence.evaluationGate.definitionReference -Field 'evaluationGate.definitionReference'
Assert-PrivateReference -Value $evidence.evaluationGate.thresholdApprovalReference -Field 'evaluationGate.thresholdApprovalReference'

$requiredServices = @('research-agent', 'triage-agent')
$actualServices = @($evidence.agents | ForEach-Object { $_.service })
if (@($actualServices | Sort-Object -Unique).Count -ne $requiredServices.Count) {
    throw 'Release evidence must contain each hosted-agent service exactly once.'
}
foreach ($service in $requiredServices) {
    if ($service -notin $actualServices) {
        throw "Release evidence is missing service '$service'."
    }
}

$requiredSafetyCoverage = @(
    'harmful-content',
    'prompt-injection',
    'data-exfiltration',
    'tool-misuse',
    'over-refusal',
    'escalation'
)
$latestEvaluationCompletion = [DateTimeOffset]::MinValue

foreach ($agent in $evidence.agents) {
    $prefix = "agents[$($agent.service)]"
    if ($agent.sourceCommit.ToLowerInvariant() -ne $normalizedCommit) {
        throw "$prefix.sourceCommit does not match the candidate commit."
    }
    if ($agent.evaluation.agentVersion -cne $agent.agentVersion) {
        throw "$prefix evaluation does not identify the deployed agent version."
    }

    Assert-PrivateReference -Value $agent.modelDeployment.deploymentReference -Field "$prefix.modelDeployment.deploymentReference"
    Assert-PrivateReference -Value $agent.modelDeployment.raiPolicy.policyReference -Field "$prefix.modelDeployment.raiPolicy.policyReference"
    Assert-PrivateReference -Value $agent.modelDeployment.raiPolicy.approvalReference -Field "$prefix.modelDeployment.raiPolicy.approvalReference"
    Assert-PrivateReference -Value $agent.modelDeployment.raiPolicy.bindingEvidenceReference -Field "$prefix.modelDeployment.raiPolicy.bindingEvidenceReference"
    Assert-PrivateReference -Value $agent.evaluation.suiteReference -Field "$prefix.evaluation.suiteReference"
    Assert-PrivateReference -Value $agent.evaluation.runReference -Field "$prefix.evaluation.runReference"

    $bindingVerifiedAt = Convert-EvidenceTimestamp -Value $agent.modelDeployment.raiPolicy.bindingVerifiedAt -Field "$prefix.modelDeployment.raiPolicy.bindingVerifiedAt"
    $evaluationStartedAt = Convert-EvidenceTimestamp -Value $agent.evaluation.startedAt -Field "$prefix.evaluation.startedAt"
    $evaluationCompletedAt = Convert-EvidenceTimestamp -Value $agent.evaluation.completedAt -Field "$prefix.evaluation.completedAt"

    if ($thresholdsDefinedAt -gt $evaluationStartedAt) {
        throw "$prefix evaluation began before its release thresholds were defined."
    }
    if ($bindingVerifiedAt -gt $evaluationStartedAt) {
        throw "$prefix RAI policy binding was not verified before evaluation began."
    }
    if ($evaluationCompletedAt -lt $evaluationStartedAt -or $evaluationCompletedAt -gt $now.AddMinutes(5)) {
        throw "$prefix evaluation timestamps are invalid."
    }
    if ($evaluationCompletedAt -gt $latestEvaluationCompletion) {
        $latestEvaluationCompletion = $evaluationCompletedAt
    }

    foreach ($coverage in $requiredSafetyCoverage) {
        if ($coverage -notin $agent.evaluation.safetyCoverage) {
            throw "$prefix evaluation does not cover '$coverage'."
        }
    }

    foreach ($metric in $agent.evaluation.metrics) {
        $meetsThreshold = if ($metric.direction -eq 'at-least') {
            $metric.observed -ge $metric.threshold
        }
        else {
            $metric.observed -le $metric.threshold
        }
        if (-not $meetsThreshold) {
            throw "$prefix metric '$($metric.name)' is marked passed but does not meet its threshold."
        }
    }
}

if ($approvedAt -lt $latestEvaluationCompletion) {
    throw 'Release approval predates the completed evaluation evidence.'
}

Write-Host "Verified private release evidence for commit $($normalizedCommit.Substring(0, 12)) and both hosted agents." -ForegroundColor Green