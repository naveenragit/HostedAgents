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
    Workbook resource name, which must be a GUID. Keep the default to update the
    existing workbook; supply a new GUID to deploy an independent copy.

.PARAMETER FunctionsOnly
    Deploy only the saved functions and leave the workbook untouched.

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

    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
    [string]$WorkbookId = 'a7f3c1d2-9b64-4e8a-8c17-6f2d5b0e91aa',

    [switch]$FunctionsOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$observability = Join-Path $repoRoot 'observability'
$templatePath = Join-Path $observability 'hosted-agents-workbook.json'
$functionsPath = Join-Path $observability 'functions'

if (-not (Test-Path $templatePath)) {
    throw "Workbook template not found at $templatePath."
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
    Write-Host 'Functions deployed. Workbook skipped (-FunctionsOnly).' -ForegroundColor Green
    return
}

Write-Host '==> Deploying workbook' -ForegroundColor Cyan
$serialized = (Get-Content -Raw -Path $templatePath).
    Replace('__LOG_ANALYTICS_WORKSPACE_ID__', $workspaceId).
    Replace('__APP_INSIGHTS_RESOURCE_ID__', $appInsightsId)

if ($serialized -match '__[A-Z_]+__') {
    throw "Unsubstituted placeholder remains in the workbook template: $($Matches[0])"
}

# Parse before sending: ARM accepts malformed content and the failure only shows
# up as an unreadable workbook in the portal.
try {
    $null = $serialized | ConvertFrom-Json
}
catch {
    throw "Workbook template is not valid JSON after substitution: $($_.Exception.Message)"
}

$workbookBody = @{
    location   = $location
    kind       = 'shared'
    properties = @{
        displayName    = 'Hosted agent model consumption and cost'
        serializedData = $serialized
        version        = 'Notebook/1.0'
        category       = 'workbook'
        # Lowercase: the portal matches the gallery on an exact string compare,
        # and a mixed-case ID hides the workbook from the resource's list.
        sourceId       = $appInsightsId.ToLowerInvariant()
    }
} | ConvertTo-Json -Depth 8 -Compress

$workbookPath = Join-Path ([System.IO.Path]::GetTempPath()) 'workbook-deploy.json'
$workbookBody | Set-Content -Path $workbookPath -Encoding utf8 -NoNewline

if ($PSCmdlet.ShouldProcess($WorkbookId, 'Deploy workbook')) {
    $response = Invoke-Arm -Method put `
        -Url "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Insights/workbooks/$WorkbookId" `
        -ApiVersion '2023-06-01' -BodyPath $workbookPath | ConvertFrom-Json
    Write-Host "    revision $($response.properties.revision)" -ForegroundColor DarkGray
}
Remove-Item -Path $workbookPath -ErrorAction SilentlyContinue

Write-Host 'Done. Open Application Insights > Workbooks to view it.' -ForegroundColor Green
