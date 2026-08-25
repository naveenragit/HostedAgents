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

    Output is a KQL datatable fragment for the `ModelRates` saved function in
    Log Analytics, which is the single rate card every pricing function reads. It
    intentionally does not write to Azure: review the mapping before applying it,
    because meter names do not match model deployment names and new models need a
    mapping entry.

    Requires an authenticated Azure CLI session with billing read access.

.PARAMETER SubscriptionId
    Subscription to read usage from. Defaults to the current CLI subscription.

.PARAMETER AccountName
    Optional Foundry/Cognitive Services account name to restrict results to.

.PARAMETER Days
    How many days of usage to keep. Defaults to 7.

.PARAMETER MaxPages
    Safety stop for `nextLink` paging. A subscription with a long history can
    page for a very long time; raise this if the script reports it stopped early.

.NOTES
    The Consumption API ignores a `properties/usageStart` filter on this
    subscription and returns the same unfiltered result regardless, so the window
    is applied client-side. Paging is not optional: one 1000-record page does not
    cover a busy day, and reading only the first page silently truncates older
    days, which pushes every emitted `ValidFrom` forward to a date the rate did
    not actually start on.

.EXAMPLE
    .\scripts\sync-model-prices.ps1 -AccountName <foundry-account>

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
    [int]$Days = 7,
    [ValidateRange(1, 500)]
    [int]$MaxPages = 50
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Shared with the billing-sync function so the two cannot drift apart and
# silently disagree about which meter belongs to which model.
$mapPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'observability\model-meter-map.json'
if (-not (Test-Path $mapPath)) {
    throw "Meter map not found at $mapPath."
}
# Only the standard tier derives a deployment's rate. A long-context meter
# prices the same deployment differently, so including it here would emit two
# conflicting input rates for one model.
$allMeters = @((Get-Content -Raw -Path $mapPath | ConvertFrom-Json).meters)
$meterMap = @($allMeters | Where-Object { $_.tier -eq 'standard' })

# Meter patterns are case-sensitive, as documented in the map and as applied by
# billing_sync.meters, the resolver that actually ingests billed cost. PowerShell's
# -match is case-insensitive, so using it here made this script claim mappings the
# ingestion path rejects, and the two reports disagreed about the same meter.
function Resolve-Meter {
    param([string]$MeterName)
    foreach ($entry in $meterMap) {
        if ($MeterName -cmatch $entry.pattern) {
            return [pscustomobject]@{ Model = $entry.model; Direction = $entry.direction }
        }
    }
    return $null
}

function Test-KnownMeter {
    param([string]$MeterName)
    foreach ($entry in $allMeters) {
        if ($MeterName -cmatch $entry.pattern) { return $true }
    }
    return $false
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

# Follow nextLink. A single 1000-record page does not cover a busy subscription,
# and the records it drops are the older ones -- exactly the ones that establish
# when a rate first took effect.
$url = "https://management.azure.com/subscriptions/$SubscriptionId/providers/Microsoft.Consumption/usageDetails?api-version=2024-08-01&metric=ActualCost&`$top=1000"
$all = [System.Collections.Generic.List[object]]::new()
$page = 0
$truncated = $false
while ($url) {
    if ($page -ge $MaxPages) {
        $truncated = $true
        break
    }
    $page++
    $raw = az rest --method get --url $url -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        if ($page -eq 1) {
            throw 'Failed to read usage details. Confirm the signed-in identity has billing read access.'
        }
        # A later page failing is survivable, but the window is now partial and
        # any ValidFrom derived from it may be later than the real start date.
        Write-Warning "Usage detail paging failed at page $page. Results are incomplete."
        $truncated = $true
        break
    }
    $body = $raw | ConvertFrom-Json
    foreach ($record in $body.value) { $all.Add($record) }
    $url = if ($body.PSObject.Properties.Name -contains 'nextLink') { $body.nextLink } else { $null }
}
Write-Host "    Read $($all.Count) usage record(s) across $page page(s)" -ForegroundColor DarkGray
if ($truncated) {
    Write-Warning "Stopped before the end of the data. Re-run with a larger -MaxPages before trusting the ValidFrom dates."
}

$records = @($all | Where-Object { [datetime]$_.properties.date -ge $since })
if ($records.Count -eq 0) {
    throw "No usage records in the last $Days day(s). Model meters lag usage, so try a wider -Days value."
}
Write-Host "    $($records.Count) record(s) inside the $Days-day window" -ForegroundColor DarkGray

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

$unresolved = @($observed | Where-Object { -not $_.Model } | Select-Object -ExpandProperty Meter -Unique)
$nonStandard = @($unresolved | Where-Object { Test-KnownMeter -MeterName $_ })
$unmapped = @($unresolved | Where-Object { -not (Test-KnownMeter -MeterName $_) })

# A unit this script cannot convert to a per-million basis has no usable price.
# Keeping it would compare NaN against NaN when detecting rate changes, which is
# never equal, and emit a spurious new row for every single day.
$unconvertible = @($observed | Where-Object { $_.Model -and [double]::IsNaN($_.PerMTok) } | Select-Object -ExpandProperty Meter -Unique)
if ($unconvertible.Count -gt 0) {
    Write-Warning 'Meters billed in a unit this script cannot convert to per-1M tokens (excluded):'
    $unconvertible | ForEach-Object { Write-Warning "  $_" }
    $observed = @($observed | Where-Object { -not ($_.Model -and [double]::IsNaN($_.PerMTok)) })
}

if ($nonStandard.Count -gt 0) {
    # Already in the map under a non-standard tier. Adding a second entry would
    # produce two conflicting rates for one deployment.
    Write-Host "`n    Skipped (non-standard rate tier, priced separately):" -ForegroundColor DarkGray
    $nonStandard | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
}
if ($unmapped.Count -gt 0) {
    Write-Warning 'Unmapped meters (these stay on retail rates until added to observability\model-meter-map.json):'
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

Write-Host "`n==> KQL rows for the ModelRates datatable" -ForegroundColor Cyan
Write-Host "    Paste into observability\functions\ModelRates.kql, the single rate" -ForegroundColor DarkGray
Write-Host "    card every pricing function reads." -ForegroundColor DarkGray
Write-Host "    Append new rows rather than editing existing ones. Editing a rate" -ForegroundColor DarkGray
Write-Host "    rewrites history; appending an effective-dated row does not." -ForegroundColor DarkGray

$byModel = $observed | Where-Object { $_.Model } | Group-Object Model
if (-not $byModel) {
    Write-Warning 'No mapped model meters in this window, so there are no rows to emit.'
    return
}
foreach ($group in $byModel) {
    $model = $group.Name
    # The 1/10th cached discount holds across the GPT-5 family on the public
    # price list. It is not a general rule, so it is never applied to anything
    # else: inventing a cached rate for a model that does not discount caching
    # understates cost on every cached token it serves.
    $assumesCacheDiscount = $model -cmatch '^gpt-5'

    $dates = @($group.Group | Select-Object -ExpandProperty Date -Unique | Sort-Object)
    $lastInput = $null
    $lastCached = $null
    $lastOutput = $null
    $emittedAny = $false

    # Walk the window in order and emit a row only where a rate actually moved.
    # Collapsing the whole window into one first-seen row dated at the earliest
    # observation reports a rate as having been in effect on days it was not,
    # which is the exact history rewrite effective dating exists to prevent.
    foreach ($date in $dates) {
        $day = @($group.Group | Where-Object { $_.Date -eq $date })
        $inputRow = @($day | Where-Object { $_.Direction -eq 'input' }) | Select-Object -First 1
        $cachedRow = @($day | Where-Object { $_.Direction -eq 'cachedInput' }) | Select-Object -First 1
        $outputRow = @($day | Where-Object { $_.Direction -eq 'output' }) | Select-Object -First 1

        # A direction with no meter on this day did not change price, it simply
        # was not used, so the last known value carries forward.
        $inputPrice = if ($inputRow) { $inputRow.PerMTok } else { $lastInput }
        $cachedPrice = if ($cachedRow) { $cachedRow.PerMTok } else { $lastCached }
        $outputPrice = if ($outputRow) { $outputRow.PerMTok } else { $lastOutput }

        if ($null -eq $inputPrice) { continue }

        $changed = ($inputPrice -ne $lastInput) -or ($cachedPrice -ne $lastCached) -or ($outputPrice -ne $lastOutput)
        $lastInput = $inputPrice
        $lastCached = $cachedPrice
        $lastOutput = $outputPrice
        if (-not $changed) { continue }

        $inferred = @()

        if ($null -ne $cachedPrice) {
            $cached = '{0}' -f $cachedPrice
        }
        elseif ($assumesCacheDiscount) {
            $cached = '{0}' -f [math]::Round($inputPrice * 0.1, 6)
            $inferred += 'cached rate inferred at 1/10th of input'
        }
        else {
            $cached = '<KEEP-EXISTING-CACHED-RATE>'
            $inferred += 'no cached meter billed and no documented discount for this family'
        }

        # A direction with no billed meter must not be emitted as zero, which
        # would silently understate cost.
        if ($null -ne $outputPrice) {
            $output = '{0}' -f $outputPrice
        }
        else {
            $output = '<KEEP-EXISTING-OUTPUT-RATE>'
            $inferred += 'no output meter billed'
        }

        $source = if ($inferred.Count -eq 0) { 'billed' } else { 'mixed' }
        foreach ($note in $inferred) {
            Write-Warning "${model} @ ${date}: $note. Keep the current value and leave the row as 'mixed'."
        }

        "'{0}', datetime({1}), {2}, {3}, {4}, '{5}'," -f $model, $date, $inputPrice, $cached, $output, $source
        $emittedAny = $true
    }

    if (-not $emittedAny) {
        Write-Warning "Skipping ${model}: no input meter observed, so there is nothing to anchor a rate on."
    }
}
