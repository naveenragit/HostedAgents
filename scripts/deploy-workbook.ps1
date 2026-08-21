<#
.SYNOPSIS
    Deploys the versioned Azure Monitor workbook and its saved KQL functions.

.DESCRIPTION
    The workbook and the two functions it depends on live only in Azure once
    deployed, which makes them easy to lose and impossible to review. This script
    restores them from the definitions under `observability\`.

    The committed workbook is a template: workspace and Application Insights
    resource IDs are replaced with placeholders so no environment identifiers are
    stored in source control. They are resolved from the names passed in here.

    The workbook is useless without its functions, so both are deployed together.
    Deploying is idempotent: the same workbook ID is updated in place rather than
    creating a duplicate.

.PARAMETER ResourceGroup
    Resource group holding the Application Insights component and the Log
    Analytics workspace.

.PARAMETER WorkspaceName
    Log Analytics workspace that stores the gateway logs and metrics.

.PARAMETER AppInsightsName
    Application Insights component the workbook is listed under.

.PARAMETER SubscriptionId
    Target subscription. Defaults to the current Azure CLI subscription.

.PARAMETER WorkbookId
    Workbook resource name, which must be a GUID. Only valid when -Workbook
    selects a single workbook. Keep the default to update the existing workbook;
    supply a new GUID to deploy an independent copy.

.PARAMETER Workbook
    Which workbook to deploy. `apim` is the API Management gateway report;
    `foundry` is the per-user cost report built from Foundry tracing and
    billing. Defaults to both. The saved functions for both are always deployed
    together, because a workbook without its functions is useless.

.PARAMETER FunctionsOnly
    Deploy only the saved functions and leave the workbooks untouched.

.EXAMPLE
    .\scripts\deploy-workbook.ps1 -ResourceGroup rg-observability -WorkspaceName my-workspace -AppInsightsName my-appinsights
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    [Parameter(Mandatory)]
    [string]$WorkspaceName,

    [Parameter(Mandatory)]
    [string]$AppInsightsName,

    [string]$SubscriptionId,

    [ValidateSet('all', 'apim', 'foundry')]
    [string]$Workbook = 'all',

    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
    [string]$WorkbookId,

    [switch]$FunctionsOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($WorkbookId -and $Workbook -eq 'all') {
    throw 'Pass -Workbook apim or -Workbook foundry when overriding -WorkbookId, otherwise both workbooks would be written to the same resource.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$observability = Join-Path $repoRoot 'observability'
$functionsPath = Join-Path $observability 'functions'

$catalog = @(
    [pscustomobject]@{
        Key         = 'apim'
        Template    = Join-Path $observability 'hosted-agents-workbook.json'
        Id          = 'a7f3c1d2-9b64-4e8a-8c17-6f2d5b0e91aa'
        DisplayName = 'Hosted agent model consumption and cost'
    }
    [pscustomobject]@{
        Key         = 'foundry'
        Template    = Join-Path $observability 'foundry-cost-workbook.json'
        Id          = 'b4e91d67-2a38-4c05-9f1e-7d3a86b2c410'
        DisplayName = 'Foundry hosted agents - per-user cost'
    }
)

$selected = @($catalog | Where-Object { $Workbook -eq 'all' -or $_.Key -eq $Workbook })
if ($WorkbookId) {
    $selected[0].Id = $WorkbookId
}

foreach ($entry in $selected) {
    if (-not (Test-Path $entry.Template)) {
        throw "Workbook template not found at $($entry.Template)."
    }
}
if (-not (Test-Path $functionsPath)) {
    throw "Function definitions not found at $functionsPath."
}

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
    $args = @('rest', '--method', $Method, '--url', $Url, '--url-parameters', "api-version=$ApiVersion", '-o', 'json')
    if ($BodyPath) {
        $args += @('--headers', 'Content-Type=application/json', '--body', "@$BodyPath")
    }
    $result = & az @args 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "ARM $Method failed for $Url"
    }
    return $result
}

$appInsightsId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Insights/components/$AppInsightsName"
$workspaceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$WorkspaceName"

# Fail before writing anything if either target is missing, so a typo cannot
# produce a workbook whose queries silently return nothing.
Write-Host '==> Verifying targets' -ForegroundColor Cyan
$component = Invoke-Arm -Method get -Url "https://management.azure.com$appInsightsId" -ApiVersion '2020-02-02' | ConvertFrom-Json
$null = Invoke-Arm -Method get -Url "https://management.azure.com$workspaceId" -ApiVersion '2023-09-01'
$location = $component.location
Write-Host "    Application Insights: $AppInsightsName ($location)" -ForegroundColor DarkGray
Write-Host "    Workspace           : $WorkspaceName" -ForegroundColor DarkGray

Write-Host '==> Deploying saved functions' -ForegroundColor Cyan
foreach ($file in Get-ChildItem -Path $functionsPath -Filter '*.kql' | Sort-Object Name) {
    $alias = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    $query = Get-Content -Raw -Path $file.FullName

    $body = @{
        properties = @{
            category      = 'Hosted agents'
            displayName   = $alias
            query         = $query
            functionAlias = $alias
            version       = 2
        }
    } | ConvertTo-Json -Depth 5 -Compress

    $bodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "savedsearch-$alias.json"
    $body | Set-Content -Path $bodyPath -Encoding utf8 -NoNewline

    if ($PSCmdlet.ShouldProcess($alias, 'Deploy saved function')) {
        $null = Invoke-Arm -Method put -Url "https://management.azure.com$workspaceId/savedSearches/$alias" -ApiVersion '2020-08-01' -BodyPath $bodyPath
        Write-Host "    $alias" -ForegroundColor DarkGray
    }
    Remove-Item -Path $bodyPath -ErrorAction SilentlyContinue
}

if ($FunctionsOnly) {
    Write-Host 'Functions deployed. Workbooks skipped (-FunctionsOnly).' -ForegroundColor Green
    return
}

foreach ($entry in $selected) {
    Write-Host "==> Deploying workbook: $($entry.DisplayName)" -ForegroundColor Cyan
    $serialized = (Get-Content -Raw -Path $entry.Template).
        Replace('__LOG_ANALYTICS_WORKSPACE_ID__', $workspaceId).
        Replace('__APP_INSIGHTS_RESOURCE_ID__', $appInsightsId)

    if ($serialized -match '__[A-Z_]+__') {
        throw "Unsubstituted placeholder remains in $($entry.Template): $($Matches[0])"
    }

    # Parse before sending: ARM accepts malformed content and the failure only
    # shows up as an unreadable workbook in the portal.
    try {
        $null = $serialized | ConvertFrom-Json
    }
    catch {
        throw "$($entry.Template) is not valid JSON after substitution: $($_.Exception.Message)"
    }

    $workbookBody = @{
        location   = $location
        kind       = 'shared'
        properties = @{
            displayName    = $entry.DisplayName
            serializedData = $serialized
            version        = 'Notebook/1.0'
            category       = 'workbook'
            # Lowercase: the portal matches the gallery on an exact string
            # compare, and a mixed-case ID hides the workbook from the
            # resource's list.
            sourceId       = $appInsightsId.ToLowerInvariant()
        }
    } | ConvertTo-Json -Depth 8 -Compress

    $workbookPath = Join-Path ([System.IO.Path]::GetTempPath()) "workbook-deploy-$($entry.Key).json"
    $workbookBody | Set-Content -Path $workbookPath -Encoding utf8 -NoNewline

    if ($PSCmdlet.ShouldProcess($entry.Id, 'Deploy workbook')) {
        $response = Invoke-Arm -Method put `
            -Url "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Insights/workbooks/$($entry.Id)" `
            -ApiVersion '2023-06-01' -BodyPath $workbookPath | ConvertFrom-Json
        Write-Host "    revision $($response.properties.revision)" -ForegroundColor DarkGray
    }
    Remove-Item -Path $workbookPath -ErrorAction SilentlyContinue
}

Write-Host 'Done. Open Application Insights > Workbooks to view them.' -ForegroundColor Green
