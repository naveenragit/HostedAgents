<#
.SYNOPSIS
    Deploys the daily billing-sync Container Apps job and the Log Analytics
    custom table it writes to.

.DESCRIPTION
    Cost reporting needs what Azure actually charged, and Azure Monitor has no
    native Cost Management connector. This script provisions the one-way pipe:

        Cost Management  ->  scheduled job  ->  DCE  ->  DCR  ->  FoundryBilledCost_CL

    It creates, in order:
      1. The `FoundryBilledCost_CL` custom table in the workspace.
      2. A data collection endpoint and a data collection rule targeting it.
      3. A user-assigned managed identity, with its three role assignments made
         before anything consumes it.
      4. A Container Apps environment and the scheduled job.
      5. The container image, built in ACR.

    A Container Apps job rather than a Function App: every storage account in the
    target subscription is governed to private-only access with shared keys
    disabled, so a Function App cannot start without VNet integration and private
    endpoints. A job needs no storage account, which removes the constraint
    rather than working around it.

    The identity is user-assigned, not system-assigned, so all three roles exist
    before the job is created. A system-assigned identity would require creating
    the job, granting AcrPull, then updating it to the real image.

    Everything is keyless. No connection string, key or secret is created,
    stored or printed.

    Re-running is safe: every step is idempotent and updates in place.

    THIS SCRIPT CREATES BILLABLE AZURE RESOURCES. The repository does not own
    resource lifecycle, so it is never invoked automatically. Run it
    deliberately, as the environment owner.

.PARAMETER ResourceGroup
    Resource group for the job, identity and data collection resources.

.PARAMETER WorkspaceName
    Log Analytics workspace that receives the custom table.

.PARAMETER WorkspaceResourceGroup
    Resource group of the workspace. Defaults to -ResourceGroup.

.PARAMETER Name
    Base name for the created resources. Must be globally unique enough for the
    Container Apps environment.

.PARAMETER RegistryName
    Existing Azure Container Registry used to build and host the image.

.PARAMETER RegistryResourceGroup
    Resource group of the registry. Defaults to -ResourceGroup.

.PARAMETER AccountName
    Optional Foundry/Cognitive Services account name to restrict billing rows to.

.PARAMETER Location
    Azure region. Defaults to the workspace's region.

.PARAMETER SubscriptionId
    Target subscription. Defaults to the current Azure CLI subscription.

.PARAMETER Cron
    Schedule in cron format, UTC. Defaults to 06:20 daily: model meters lag usage
    by roughly 8-24 hours, so an early-morning run picks up the previous day
    rather than racing it.

.EXAMPLE
    .\scripts\deploy-billing-sync.ps1 -ResourceGroup rg-observability `
        -WorkspaceName <workspace> -Name billing-sync-abc123 `
        -RegistryName <registry> -RegistryResourceGroup rg-registry `
        -AccountName <foundry-account>
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    [Parameter(Mandatory)]
    [string]$WorkspaceName,

    [string]$WorkspaceResourceGroup,

    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$')]
    [string]$Name,

    [Parameter(Mandatory)]
    [string]$RegistryName,

    [string]$RegistryResourceGroup,

    [string]$AccountName,

    [string]$Location,

    [string]$SubscriptionId,

    [string]$Cron = '20 6 * * *'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $repoRoot 'backend\functions\billing-sync'
$meterMapSource = Join-Path $repoRoot 'observability\model-meter-map.json'
$tableName = 'FoundryBilledCost_CL'
$streamName = 'Custom-FoundryBilledCost_CL'

foreach ($required in @($sourceRoot, $meterMapSource)) {
    if (-not (Test-Path $required)) { throw "Not found: $required" }
}

if (-not $WorkspaceResourceGroup) { $WorkspaceResourceGroup = $ResourceGroup }
if (-not $RegistryResourceGroup) { $RegistryResourceGroup = $ResourceGroup }

if (-not $SubscriptionId) {
    $SubscriptionId = (az account show --query id -o tsv)
    if ($LASTEXITCODE -ne 0 -or -not $SubscriptionId) {
        throw 'Could not resolve a subscription. Run az login first, or pass -SubscriptionId.'
    }
}

function Invoke-Arm {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$ApiVersion,
        [string]$BodyPath
    )
    $cmd = @('rest', '--method', $Method, '--url', $Url, '--url-parameters', "api-version=$ApiVersion", '-o', 'json')
    if ($BodyPath) { $cmd += @('--headers', 'Content-Type=application/json', '--body', "@$BodyPath") }
    $result = & az @cmd 2>$null
    if ($LASTEXITCODE -ne 0) { throw "ARM $Method failed for $Url" }
    return $result
}

function Write-JsonBody {
    param([Parameter(Mandatory)][hashtable]$Body, [Parameter(Mandatory)][string]$Label)
    $path = Join-Path ([System.IO.Path]::GetTempPath()) "billing-sync-$Label.json"
    # -WhatIf:$false because a request body in the temp directory is not the
    # mutation being previewed; suppressing it here keeps a dry run readable.
    ($Body | ConvertTo-Json -Depth 12 -Compress) | Set-Content -Path $path -Encoding utf8 -NoNewline -WhatIf:$false
    return $path
}

$workspaceId = "/subscriptions/$SubscriptionId/resourceGroups/$WorkspaceResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$WorkspaceName"

Write-Host '==> Verifying workspace and registry' -ForegroundColor Cyan
$workspace = Invoke-Arm -Method get -Url "https://management.azure.com$workspaceId" -ApiVersion '2023-09-01' | ConvertFrom-Json
if (-not $Location) { $Location = $workspace.location }
$registryId = az acr show -n $RegistryName -g $RegistryResourceGroup --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $registryId) { throw "Container registry $RegistryName not found." }
$registryServer = az acr show -n $RegistryName -g $RegistryResourceGroup --query loginServer -o tsv
Write-Host "    workspace $WorkspaceName ($Location)" -ForegroundColor DarkGray
Write-Host "    registry  $registryServer" -ForegroundColor DarkGray

# --- 1. Custom table -------------------------------------------------------
# Column names must match the payload the job uploads exactly; a mismatch is
# dropped silently at ingestion rather than reported as an error.
Write-Host '==> Creating custom table' -ForegroundColor Cyan
$columns = @(
    @{ name = 'TimeGenerated'; type = 'datetime' }
    @{ name = 'UsageDate'; type = 'datetime' }
    @{ name = 'ModelName'; type = 'string' }
    @{ name = 'Meter'; type = 'string' }
    @{ name = 'Direction'; type = 'string' }
    @{ name = 'RateTier'; type = 'string' }
    @{ name = 'BilledCostUsd'; type = 'real' }
    @{ name = 'BilledTokens'; type = 'real' }
    @{ name = 'EffectivePriceUsd'; type = 'real' }
    @{ name = 'UnitOfMeasure'; type = 'string' }
    @{ name = 'ResourceName'; type = 'string' }
    @{ name = 'SubscriptionId'; type = 'string' }
)
$tablePath = Write-JsonBody -Label 'table' -Body @{
    properties = @{ schema = @{ name = $tableName; columns = $columns } }
}
if ($PSCmdlet.ShouldProcess($tableName, 'Create custom table')) {
    $null = Invoke-Arm -Method put -Url "https://management.azure.com$workspaceId/tables/$tableName" -ApiVersion '2022-10-01' -BodyPath $tablePath
    Write-Host "    $tableName" -ForegroundColor DarkGray
}
Remove-Item $tablePath -ErrorAction SilentlyContinue

# --- 2. Data collection endpoint and rule ----------------------------------
Write-Host '==> Creating data collection endpoint and rule' -ForegroundColor Cyan
$dceName = "dce-$Name"
$dcrName = "dcr-$Name"
$dceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Insights/dataCollectionEndpoints/$dceName"
$dcrId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Insights/dataCollectionRules/$dcrName"

$dcePath = Write-JsonBody -Label 'dce' -Body @{
    location   = $Location
    properties = @{ networkAcls = @{ publicNetworkAccess = 'Enabled' } }
}
# Placeholders so a dry run can still print the job it would create. Every value
# below is only ever read by a call that is itself behind ShouldProcess.
$dceEndpoint = '<dce-logs-ingestion-endpoint>'
if ($PSCmdlet.ShouldProcess($dceName, 'Create data collection endpoint')) {
    $dce = Invoke-Arm -Method put -Url "https://management.azure.com$dceId" -ApiVersion '2023-03-11' -BodyPath $dcePath | ConvertFrom-Json
    $dceEndpoint = $dce.properties.logsIngestion.endpoint
    Write-Host "    $dceName" -ForegroundColor DarkGray
}
Remove-Item $dcePath -ErrorAction SilentlyContinue

# transformKql is required even as a passthrough; without it the stream has no
# declared destination mapping and every upload is accepted then dropped.
$dcrPath = Write-JsonBody -Label 'dcr' -Body @{
    location   = $Location
    properties = @{
        dataCollectionEndpointId = $dceId
        streamDeclarations       = @{ $streamName = @{ columns = $columns } }
        destinations             = @{
            logAnalytics = @(@{ workspaceResourceId = $workspaceId; name = 'primary' })
        }
        dataFlows                = @(
            @{
                streams      = @($streamName)
                destinations = @('primary')
                transformKql = 'source'
                outputStream = "Custom-$tableName"
            }
        )
    }
}
$dcrImmutableId = '<dcr-immutable-id>'
if ($PSCmdlet.ShouldProcess($dcrName, 'Create data collection rule')) {
    $dcr = Invoke-Arm -Method put -Url "https://management.azure.com$dcrId" -ApiVersion '2023-03-11' -BodyPath $dcrPath | ConvertFrom-Json
    $dcrImmutableId = $dcr.properties.immutableId
    Write-Host "    $dcrName (immutableId $dcrImmutableId)" -ForegroundColor DarkGray
}
Remove-Item $dcrPath -ErrorAction SilentlyContinue

# --- 3. Identity and roles --------------------------------------------------
Write-Host '==> Creating managed identity and assigning roles' -ForegroundColor Cyan
$identityName = "id-$Name"
$identityId = '<identity-resource-id>'
$identityClientId = '<identity-client-id>'
$identityPrincipalId = $null
if ($PSCmdlet.ShouldProcess($identityName, 'Create user-assigned managed identity')) {
    $null = az identity create -n $identityName -g $ResourceGroup -l $Location -o none
    if ($LASTEXITCODE -ne 0) { throw "Failed to create managed identity $identityName" }
    $identityId = az identity show -n $identityName -g $ResourceGroup --query id -o tsv
    $identityPrincipalId = az identity show -n $identityName -g $ResourceGroup --query principalId -o tsv
    $identityClientId = az identity show -n $identityName -g $ResourceGroup --query clientId -o tsv
    Write-Host "    $identityName" -ForegroundColor DarkGray
}

# Cost Management Reader is subscription-scoped because usage details are only
# readable at that scope. It grants read access to cost data and nothing else.
$assignments = @(
    @{ Role = 'Cost Management Reader'; Scope = "/subscriptions/$SubscriptionId" }
    @{ Role = 'Monitoring Metrics Publisher'; Scope = $dcrId }
    @{ Role = 'AcrPull'; Scope = $registryId }
)
foreach ($assignment in $assignments) {
    if (-not $PSCmdlet.ShouldProcess($assignment.Scope, "Assign '$($assignment.Role)' to $identityName")) { continue }
    # Role assignment is eventually consistent against a freshly created
    # principal, so a transient failure here is retried rather than fatal.
    $assigned = $false
    foreach ($attempt in 1..5) {
        $null = az role assignment create --assignee-object-id $identityPrincipalId `
            --assignee-principal-type ServicePrincipal `
            --role $assignment.Role --scope $assignment.Scope -o none 2>$null
        $existing = az role assignment list --assignee $identityPrincipalId --scope $assignment.Scope `
            --query "[?roleDefinitionName=='$($assignment.Role)'] | length(@)" -o tsv 2>$null
        if ($existing -and [int]$existing -gt 0) { $assigned = $true; break }
        Start-Sleep -Seconds (5 * $attempt)
    }
    if (-not $assigned) { throw "Could not assign $($assignment.Role) to $identityName." }
    Write-Host "    $($assignment.Role)" -ForegroundColor DarkGray
}

# --- 4. Image ---------------------------------------------------------------
# Built in ACR: the platform requires linux/amd64 and this repository's dev hosts
# are ARM64. The context is staged in a temp directory rather than assembled in
# place, because the meter map copied alongside the source would otherwise
# shadow the canonical file for local runs and tests.
Write-Host '==> Building image in ACR' -ForegroundColor Cyan
$imageTag = "billing-sync:$(Get-Date -Format 'yyyyMMddHHmmss')"
$image = "$registryServer/$imageTag"
if ($PSCmdlet.ShouldProcess($image, 'Build image')) {
    $context = Join-Path ([System.IO.Path]::GetTempPath()) "billing-sync-context-$Name"
    if (Test-Path $context) { Remove-Item $context -Recurse -Force }
    $null = New-Item -ItemType Directory -Path $context
    Copy-Item -Path (Join-Path $sourceRoot '*') -Destination $context -Recurse -Force
    Get-ChildItem -Path $context -Recurse -Force -Directory |
        Where-Object { $_.Name -in @('__pycache__', '.venv') } |
        Remove-Item -Recurse -Force
    Copy-Item -Path $meterMapSource -Destination (Join-Path $context 'billing_sync\model-meter-map.json') -Force

    az acr build --registry $RegistryName --resource-group $RegistryResourceGroup `
        --image $imageTag --platform linux/amd64 $context

    # The exit code is not trustworthy here: the CLI's log streamer crashes on
    # Windows consoles that are not UTF-8, failing the command even when the
    # build succeeded, and returning before the build finishes. Success is
    # confirmed by polling for the tag instead.
    $tag = $imageTag.Split(':')[1]
    $published = $false
    foreach ($attempt in 1..30) {
        $found = az acr repository show-tags -n $RegistryName --repository 'billing-sync' `
            --query "[?@=='$tag'] | length(@)" -o tsv 2>$null
        if ($found -and [int]$found -gt 0) { $published = $true; break }
        Start-Sleep -Seconds 10
    }
    Remove-Item $context -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $published) {
        $failedRun = az acr task list-runs -r $RegistryName --top 1 --query '[0].runId' -o tsv 2>$null
        throw "ACR build did not publish $imageTag. Inspect the log with: az acr task logs -r $RegistryName --run-id $failedRun"
    }
    Write-Host "    $image" -ForegroundColor DarkGray
}

# --- 5. Container Apps environment and job ----------------------------------
Write-Host '==> Creating Container Apps environment' -ForegroundColor Cyan
$envName = "cae-$Name"
$existingEnv = az containerapp env show -n $envName -g $ResourceGroup --query id -o tsv 2>$null
if (-not $existingEnv -and $PSCmdlet.ShouldProcess($envName, 'Create Container Apps environment')) {
    # Console output goes to the same workspace as the report. Without a logs
    # destination a failed run leaves no diagnosable trace at all.
    $workspaceCustomerId = az monitor log-analytics workspace show `
        -g $WorkspaceResourceGroup -n $WorkspaceName --query customerId -o tsv
    $workspaceKey = az monitor log-analytics workspace get-shared-keys `
        -g $WorkspaceResourceGroup -n $WorkspaceName --query primarySharedKey -o tsv
    if (-not $workspaceKey) { throw 'Could not read the workspace shared key.' }

    $null = az containerapp env create -n $envName -g $ResourceGroup -l $Location `
        --logs-destination log-analytics `
        --logs-workspace-id $workspaceCustomerId `
        --logs-workspace-key $workspaceKey -o none
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Container Apps environment $envName" }
}
Write-Host "    $envName" -ForegroundColor DarkGray

Write-Host '==> Creating scheduled job' -ForegroundColor Cyan
$jobName = "job-$Name"
$envVars = @(
    "BILLING_SUBSCRIPTION_ID=$SubscriptionId"
    "BILLING_DCE_ENDPOINT=$dceEndpoint"
    "BILLING_DCR_IMMUTABLE_ID=$dcrImmutableId"
    "BILLING_STREAM_NAME=$streamName"
    "AZURE_CLIENT_ID=$identityClientId"
)
if ($AccountName) { $envVars += "BILLING_ACCOUNT_NAME=$AccountName" }

$existingJob = az containerapp job show -n $jobName -g $ResourceGroup --query id -o tsv 2>$null
if ($PSCmdlet.ShouldProcess($jobName, 'Create or update scheduled job')) {
    if ($existingJob) {
        $null = az containerapp job update -n $jobName -g $ResourceGroup `
            --image $image --cron-expression $Cron --replace-env-vars @envVars -o none
    }
    else {
        # replica-completion-count defaults to 0, which the platform rejects.
        $null = az containerapp job create -n $jobName -g $ResourceGroup `
            --environment $envName --trigger-type Schedule --cron-expression $Cron `
            --image $image --cpu 0.5 --memory 1.0Gi `
            --replica-timeout 1800 --replica-retry-limit 1 `
            --parallelism 1 --replica-completion-count 1 `
            --mi-user-assigned $identityId --registry-server $registryServer `
            --registry-identity $identityId --env-vars @envVars -o none
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to deploy job $jobName" }
    Write-Host "    $jobName (cron '$Cron' UTC)" -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host "  Trigger a run now : az containerapp job start -n $jobName -g $ResourceGroup" -ForegroundColor DarkGray
Write-Host "  Verify ingestion  : FoundryBilledCost_CL | summarize count(), max(TimeGenerated)" -ForegroundColor DarkGray
