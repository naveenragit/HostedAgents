<#
.SYNOPSIS
    Reports the model token rates this subscription is actually billed.

.DESCRIPTION
    Reads Cost Management usage details for AI model meters and reports the
    effective price Azure charged, per meter and per day.

    Cost reporting built on the public retail price list is an estimate, and it
    can be wrong: the retail "pp" meters for the GPT-5 family were observed at
    twice the rate this subscription was actually billed. Rates are also not
    static, so a single flat rate table silently re-prices history whenever it is
    edited. This script produces effective-dated rows sourced from billing so the
    reporting function can price each request at the rate that applied on the day
    it ran.

    Output is a KQL datatable fragment for the `HostedAgentModelUsage` saved
    function in Log Analytics. It intentionally does not write to Azure: review
    the mapping before applying it, because meter names do not match model
    deployment names and new models need a mapping entry.

    Requires an authenticated Azure CLI session with billing read access.

.PARAMETER SubscriptionId
    Subscription to read usage from. Defaults to the current CLI subscription.

.PARAMETER AccountName
    Optional Foundry/Cognitive Services account name to restrict results to.

.PARAMETER Days
    How many days of usage to keep from the returned page. Defaults to 7.

.NOTES
    The Consumption API ignores a `properties/usageStart` filter on this
    subscription and returns the same unfiltered page regardless, so the window
    is applied client-side. Paging via `nextLink` and the Cost Management query
    API were both unusable: the former gateway-times-out, the latter returns 429.

.EXAMPLE
    .\scripts\sync-model-prices.ps1 -AccountName foundry-pocs

.NOTES
    Billing lags usage. Model meters are typically a day or more behind, so the
    most recent day of traffic usually has no billed rate yet. Requests priced
    from the retail list are tagged `RateSource = retail` in the workbook.
#>
[CmdletBinding()]
param(
    [string]$SubscriptionId,
    [string]$AccountName,
    [ValidateRange(1, 31)]
    [int]$Days = 7
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Maps a billed meter name to the model deployment and the token direction it
# prices. Billing names do not match deployment names and vary in casing and
# spacing between model families, so this cannot be derived reliably.
# Add an entry whenever a new model is deployed.
$meterMap = @(
    @{ Pattern = '^5\.4 inp Gl';                 Model = 'gpt-5.4';                Direction = 'input' }
    @{ Pattern = '^5\.4 opt Gl';                 Model = 'gpt-5.4';                Direction = 'output' }
    @{ Pattern = '^5\.4 cd inp Gl';              Model = 'gpt-5.4';                Direction = 'cachedInput' }
    @{ Pattern = '^GPT 5 Inpt Glbl';             Model = 'gpt-5';                  Direction = 'input' }
    @{ Pattern = '^GPT 5 outpt Glbl';            Model = 'gpt-5';                  Direction = 'output' }
    @{ Pattern = '^GPT 5 Mini [Ii]npt Glbl';     Model = 'gpt-5-mini';             Direction = 'input' }
    @{ Pattern = '^GPT 5 Mini outpt Glbl';       Model = 'gpt-5-mini';             Direction = 'output' }
    @{ Pattern = '^GPT 5\.1 inp Gl';             Model = 'gpt-5.1';                Direction = 'input' }
    @{ Pattern = '^GPT 5\.1 opt Gl';             Model = 'gpt-5.1';                Direction = 'output' }
    @{ Pattern = '^gpt 4\.1 Inp glbl';           Model = 'gpt-4.1';                Direction = 'input' }
    @{ Pattern = '^gpt 4\.1 Outp glbl';          Model = 'gpt-4.1';                Direction = 'output' }
    @{ Pattern = '^gpt-4o-\d+-Inp-glbl';         Model = 'gpt-4o';                 Direction = 'input' }
    @{ Pattern = '^gpt-4o-\d+-Outp-glbl';        Model = 'gpt-4o';                 Direction = 'output' }
    @{ Pattern = '^text-embedding-3-large-glbl'; Model = 'text-embedding-3-large'; Direction = 'input' }
    @{ Pattern = '^text-embedding-3-small-glbl'; Model = 'text-embedding-3-small'; Direction = 'input' }
)

function Resolve-Meter {
    param([string]$MeterName)
    foreach ($entry in $meterMap) {
        if ($MeterName -match $entry.Pattern) {
            return [pscustomobject]@{ Model = $entry.Model; Direction = $entry.Direction }
        }
    }
    return $null
}

# Usage quantity is expressed in the meter's own pricing unit, so a price of
# 0.002 per "1K" and 2.5 per "1M" both have to reach the same per-million basis.
function ConvertTo-PerMillion {
    param([double]$Price, [string]$UnitOfMeasure)
    switch -Regex ($UnitOfMeasure) {
        '^1M' { return $Price }
        '^1K' { return $Price * 1000.0 }
        default { return [double]::NaN }
    }
}

if (-not $SubscriptionId) {
    $SubscriptionId = (az account show --query id -o tsv)
    if ($LASTEXITCODE -ne 0 -or -not $SubscriptionId) {
        throw 'Could not resolve a subscription. Run az login first, or pass -SubscriptionId.'
    }
}

$since = (Get-Date).ToUniversalTime().AddDays(-$Days).Date
Write-Host "==> Reading billed model meters since $($since.ToString('yyyy-MM-dd'))" -ForegroundColor Cyan

# One bounded request. Server-side date filtering is not honoured here and
# nextLink paging gateway-times-out, so take a single page and filter locally.
$raw = az rest --method get `
    --url "https://management.azure.com/subscriptions/$SubscriptionId/providers/Microsoft.Consumption/usageDetails" `
    --url-parameters api-version=2024-08-01 metric=ActualCost '$top=1000' `
    -o json 2>$null

if ($LASTEXITCODE -ne 0 -or -not $raw) {
    throw 'Failed to read usage details. Confirm the signed-in identity has billing read access.'
}

$records = @(($raw | ConvertFrom-Json).value | Where-Object { [datetime]$_.properties.date -ge $since })
if ($records.Count -eq 0) {
    throw "No usage records in the last $Days day(s). Model meters lag usage, so try a wider -Days value."
}
Write-Host "    Retrieved $($records.Count) usage record(s) in window" -ForegroundColor DarkGray

$models = $records | Where-Object { $_.properties.meterCategory -eq 'Foundry Models' }
if ($AccountName) {
    $models = $models | Where-Object { $_.properties.instanceName -match "/$([regex]::Escape($AccountName))$" }
}

if (-not $models) {
    Write-Warning "No billed model meters found. Model usage typically lags by a day or more."
    return
}

$observed = foreach ($record in $models) {
    $p = $record.properties
    $mapped = Resolve-Meter -MeterName $p.meterName
    [pscustomobject]@{
        Date      = ([datetime]$p.date).ToString('yyyy-MM-dd')
        Meter     = $p.meterName
        Model     = if ($mapped) { $mapped.Model } else { $null }
        Direction = if ($mapped) { $mapped.Direction } else { $null }
        PerMTok   = if ($mapped) { ConvertTo-PerMillion -Price ([double]$p.effectivePrice) -UnitOfMeasure $p.unitOfMeasure } else { [double]::NaN }
        Account   = ($p.instanceName -split '/')[-1]
    }
}

$unmapped = $observed | Where-Object { -not $_.Model } | Select-Object -ExpandProperty Meter -Unique
if ($unmapped) {
    Write-Warning "Unmapped meters (these will stay on retail rates until added to `$meterMap):"
    $unmapped | ForEach-Object { Write-Warning "  $_" }
}

Write-Host "`n==> Observed billed rates (USD per 1M tokens)" -ForegroundColor Cyan
$observed |
    Where-Object { $_.Model } |
    Group-Object Model, Direction, PerMTok |
    ForEach-Object {
        $first = $_.Group[0]
        [pscustomobject]@{
            Model     = $first.Model
            Direction = $first.Direction
            PerMTok   = $first.PerMTok
            ValidFrom = ($_.Group | Sort-Object Date | Select-Object -First 1).Date
            Days      = $_.Count
        }
    } |
    Sort-Object Model, Direction |
    Format-Table -AutoSize

Write-Host "==> KQL rows for the rates datatable in HostedAgentModelUsage" -ForegroundColor Cyan
Write-Host "    Append new rows rather than editing existing ones. Editing a rate" -ForegroundColor DarkGray
Write-Host "    rewrites history; appending an effective-dated row does not." -ForegroundColor DarkGray

$byModel = $observed | Where-Object { $_.Model } | Group-Object Model
if (-not $byModel) {
    Write-Warning 'No mapped model meters in this window, so there are no rows to emit.'
    return
}
foreach ($group in $byModel) {
    $validFrom = ($group.Group | Sort-Object Date | Select-Object -First 1).Date
    $inputRow = $group.Group | Where-Object { $_.Direction -eq 'input' } | Select-Object -First 1
    $outputRow = $group.Group | Where-Object { $_.Direction -eq 'output' } | Select-Object -First 1
    $cachedRow = $group.Group | Where-Object { $_.Direction -eq 'cachedInput' } | Select-Object -First 1

    if (-not $inputRow) {
        Write-Warning "Skipping $($group.Name): no input meter observed."
        continue
    }

    $input_ = $inputRow.PerMTok
    # Cached input is discounted to a tenth of the input rate across every
    # GPT-5-family meter on the public price list, so when no cached meter has
    # been billed yet that ratio is applied to the billed input rate. It is an
    # inference, not an observation: replace it as soon as a cached meter bills.
    $cached = if ($cachedRow) { $cachedRow.PerMTok } else { [math]::Round($input_ * 0.1, 6) }

    # A direction with no billed meter must not be emitted as zero, which would
    # silently understate cost. Emit a placeholder and mark the row 'mixed' so
    # the workbook reports it as only partly authoritative.
    if ($outputRow) {
        $output = '{0}' -f $outputRow.PerMTok
        $source = 'billed'
    }
    else {
        $output = '<KEEP-EXISTING-OUTPUT-RATE>'
        $source = 'mixed'
        Write-Warning "$($group.Name): no output meter billed yet. Keep the current output rate and leave the row as 'mixed'."
    }

    "'{0}', datetime({1}), {2}, {3}, {4}, '{5}'," -f $group.Name, $validFrom, $input_, $cached, $output, $source
}
