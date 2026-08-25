# Observability — per-user cost reporting

Two Azure Monitor workbooks live in this folder. They answer the same question —
*what is each user costing us?* — from two independent data sources, and they can
run side by side.

| Workbook | Template | Source | Covers |
| --- | --- | --- | --- |
| **Foundry hosted agents — per-user cost** | `foundry-cost-workbook.json` | Foundry tracing + Azure billing | Every agent in the project |
| Hosted agent model consumption and cost | `hosted-agents-workbook.json` | API Management AI gateway | Only traffic routed through the gateway |

**This guide covers the Foundry one.** It reads no API Management data, so it works
whether or not a gateway is provisioned.

---

## 1. How it works

Three sources, none of which is sufficient alone:

| Source | Provides | Missing |
| --- | --- | --- |
| `AppDependencies` gen_ai spans | tokens, model, agent, latency, outcome | no user, no price |
| `AppTraces` `user.id` | the user identity | no tokens, no price |
| Cost Management usage details | the money actually billed | **no user dimension, ever** |

So the report performs two joins:

1. **Tokens → user**, on `OperationId` (the trace id). Foundry stamps `user.id` onto
   log records written inside a request scope but *not* onto spans. That asymmetry
   is the only reason this join exists.
2. **Tokens → money**, via an effective-dated rate card, then reconciled against
   what Azure actually billed.

The invariant the design enforces: **quantities never come from billing, and money
never comes from telemetry.**

```
Foundry agents ──► AppDependencies (gen_ai spans) ──┐
               └─► AppTraces (user.id) ─────────────┤
                                                    ├──► FoundryAgentUsage
Cost Management ──► billing-sync job ──► DCE ──► DCR ──► FoundryBilledCost_CL
                                                    │         │
                                                    └─────────┴──► FoundryAgentCost
                                                                        │
                                                                   Workbook
```

### The saved functions

| Function | Returns |
| --- | --- |
| `FoundryAgentUsage` | one row per model call: tokens, user, agent, model, cost |
| `FoundryBilledCost` | what Azure billed, per meter per day, de-duplicated |
| `FoundryAgentCost` | allocated vs billed per model per day, with a status verdict |

---

## 2. What you need before starting

| Requirement | Why | How to check |
| --- | --- | --- |
| Foundry project with hosted agents | emits the spans | — |
| **Application Insights connected to the project, tracing enabled** | without this there is no data at all | Foundry portal → Agents → Traces |
| Log Analytics workspace behind that App Insights | hosts the functions and custom table | see command below |
| Azure Container Registry | hosts the billing job image | `az acr list -o table` |
| Azure CLI, logged in | everything below | `az account show` |

Tracing is **off by default** in Foundry. If it was never enabled, no amount of
deployment here will produce data.

Find the workspace behind your App Insights:

```powershell
az resource show -g <rg> -n <appinsights> `
  --resource-type "Microsoft.Insights/components" `
  --query "properties.WorkspaceResourceId" -o tsv
```

### Permissions

| Principal | Role | Scope |
| --- | --- | --- |
| You (deploying) | Contributor | resource group |
| You (deploying) | Owner or User Access Administrator | subscription — needed to create role assignments |
| Job identity | `Cost Management Reader` | **subscription** (usage details are only readable there) |
| Job identity | `Monitoring Metrics Publisher` | the data collection rule |
| Job identity | `AcrPull` | the container registry |
| Report viewers | `Log Analytics Reader` | the App Insights resource |

The three job roles are created for you by the deploy script. Nothing uses a key,
connection string or password.

---

## 3. Step-by-step

### Step 1 — Deploy the report

Creates the three saved functions and the workbook. Free, additive, and safe to
re-run; the existing API Management workbook is untouched.

```powershell
.\scripts\deploy-workbook.ps1 `
    -ResourceGroup       <rg-with-appinsights> `
    -WorkspaceName       <log-analytics-workspace> `
    -AppInsightsName     <appinsights> `
    -Workbook            foundry
```

Open **Application Insights → Workbooks → Foundry hosted agents — per-user cost**.

At this point token and per-user panels work, but every cost figure is zero:
there is no billing data yet.

> Pass `-Workbook all` to deploy both workbooks, or omit `-Workbook` (defaults to
> `all`). `-FunctionsOnly` updates the KQL without touching either workbook.

### Step 2 — Deploy the billing import

This is the part that creates **billable Azure resources**. Run it deliberately.

```powershell
.\scripts\deploy-billing-sync.ps1 `
    -ResourceGroup          <rg-for-the-job> `
    -WorkspaceName          <log-analytics-workspace> `
    -Name                   billing-sync-<unique-suffix> `
    -RegistryName           <container-registry> `
    -RegistryResourceGroup  <rg-with-the-registry> `
    -AccountName            <foundry-account>
```

`-AccountName` is optional and restricts billing rows to one Foundry account.
Omit it to ingest every model meter in the subscription.

It creates, in order:

1. `FoundryBilledCost_CL` custom table in the workspace
2. A data collection endpoint and rule
3. A user-assigned managed identity, with its three roles assigned **before**
   anything consumes it
4. The container image, built in ACR
5. A Container Apps environment and the scheduled job

Re-running is safe — every step is idempotent and updates in place.

### Step 3 — Trigger the first run

The job runs daily on a schedule, but does not run on creation. Trigger it once so
the report has data immediately:

```powershell
az containerapp job start -n job-<name> -g <rg>
```

Wait about a minute, then confirm it succeeded:

```powershell
az containerapp job execution list -n job-<name> -g <rg> `
  --query "[0].{name:name, status:properties.status}" -o table
```

### Step 4 — Verify

Run these in **Log Analytics → Logs**.

```kusto
// Billing landed. First ingestion into a new table can take ~10 minutes —
// an empty result immediately after the first run is normal, not a failure.
FoundryBilledCost_CL | summarize Rows = count(), Cost = sum(BilledCostUsd), Latest = max(TimeGenerated)
```

```kusto
// Telemetry is attributed. Attributed should equal Requests.
FoundryAgentUsage
| summarize Requests = count(), Attributed = countif(UserId != 'unattributed') by AgentName
```

```kusto
// The trust gate. Look for 'reconciled' with Factor near 1.0.
FoundryAgentCost
```

---

## 4. The container that imports billing

Billing is imported by a **Container Apps job** — not a Function App, and not a
manual script.

| Property | Value |
| --- | --- |
| Job | `job-<name>` |
| Environment | `cae-<name>` |
| Identity | `id-<name>` (user-assigned) |
| Image | `<registry>.azurecr.io/billing-sync:<timestamp>` |
| Source | [`backend/functions/billing-sync`](../backend/functions/billing-sync) |
| Schedule | `20 6 * * *` — 06:20 UTC daily |
| Resources | 0.5 vCPU, 1 GiB, 30-minute timeout, 1 retry |

### Why a job and not a Function App

Every storage account in the target subscription is governed to
`publicNetworkAccess=Disabled` with `allowSharedKeyAccess=false`. ARM accepts a
request to re-enable public access and silently keeps it disabled. A Function App
cannot start without reachable storage, so it would need VNet integration and
private endpoints. **A Container Apps job needs no storage account at all**, which
removes the constraint rather than working around it.

### What it does on each run

1. Reads Cost Management usage details for the subscription, **following
   pagination**. A single-page read silently truncates older days — it returned
   1000 records where the pager returns over 2000, and correspondingly less cost.
2. Keeps `meterCategory == 'Foundry Models'` rows inside the lookback window.
3. Maps each meter to a model deployment using
   [`model-meter-map.json`](model-meter-map.json).
4. Aggregates to one row per meter, day and resource.
5. Uploads to `FoundryBilledCost_CL` through the data collection rule.

### Configuration

Set as environment variables on the job:

| Variable | Purpose |
| --- | --- |
| `BILLING_SUBSCRIPTION_ID` | subscription to read usage from |
| `BILLING_DCE_ENDPOINT` | data collection endpoint URL |
| `BILLING_DCR_IMMUTABLE_ID` | data collection rule immutable id |
| `BILLING_STREAM_NAME` | `Custom-FoundryBilledCost_CL` |
| `BILLING_ACCOUNT_NAME` | optional Foundry account filter |
| `BILLING_LOOKBACK_DAYS` | default 14, range 1–90 |
| `AZURE_CLIENT_ID` | client id of the user-assigned identity |

The lookback is deliberately wider than one day: **billing restates recent days as
charges settle**, so each run re-reads a window and `FoundryBilledCost`
de-duplicates on the latest ingestion per meter-day. Summing the raw table instead
of calling the function multiplies your cost by the number of runs.

### Operating it

```powershell
# Run now
az containerapp job start -n job-<name> -g <rg>

# Execution history
az containerapp job execution list -n job-<name> -g <rg> -o table

# Console output
# (ContainerAppConsoleLogs_CL in Log Analytics)
```

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1d)
| where Log_s has 'ingested' or Log_s has 'unmapped'
| project TimeGenerated, Log_s
```

A healthy run logs `scanned N usage record(s), produced M billed row(s)` followed
by `ingested M row(s) covering X USD across Y model(s)`.

---

## 5. Keeping it accurate

### When a new model is deployed

Two files need an entry, or that model's spend is misreported:

1. **[`model-meter-map.json`](model-meter-map.json)** — maps billed meter names to
   model deployments. A meter with no entry is ingested as `ModelName = 'unmapped'`
   rather than dropped, so billed cost stays complete and the gap is visible. The
   job logs a warning naming the meter. After adding it, re-run
   `deploy-billing-sync.ps1` to rebuild the image.

2. **The rate card inside [`FoundryAgentUsage.kql`](functions/FoundryAgentUsage.kql)** —
   regenerate rows with:

   ```powershell
   .\scripts\sync-model-prices.ps1 -AccountName <foundry-account>
   ```

   **Append rows, never edit existing ones.** Rates are effective-dated; editing a
   row silently re-prices history, which is the failure this design exists to
   prevent. Then redeploy with `deploy-workbook.ps1 -FunctionsOnly`.

### Reading the reconciliation status

| Status | Meaning | Action |
| --- | --- | --- |
| `reconciled` | allocated and billed agree within 1% | trust the figures |
| `pending billing` | traced in the last two days, not yet billed | normal, wait |
| `unbilled` | traced, inside the window, but no meter billed | check the meter map |
| `outside billed window` | older than the ingested billing data | widen `BILLING_LOOKBACK_DAYS` |
| `untraced` | **Azure billed, but no trace exists** | that spend cannot be attributed to anyone |
| `unpriced` | no rate applied, counted as zero | re-run `sync-model-prices.ps1` |
| `divergent` | disagree beyond tolerance | the rate table or meter map is wrong |

`Factor` is the multiplier that makes allocation tie out to the invoice. The
chargeback table applies it, so `ChargebackUsd` sums to the bill on reconciled days
while `AllocatedUsd` shows raw behaviour.

---

## 6. Troubleshooting

| Symptom | Cause |
| --- | --- |
| Every panel empty | Tracing not enabled on the Foundry project, or no agent traffic in the range |
| All requests `unattributed` | The agent writes no log record inside the request scope. Portal playground sessions and some built-in agents behave this way |
| An agent shows zero cost | It emits no `gen_ai` spans. Agent Framework agents emit them automatically; agents on the raw responses adapter must record their own |
| Cost is zero but tokens are present | Billing not ingested yet, or the model has no rate — check `RateSource` and the `unpriced` count |
| `FoundryBilledCost_CL` empty after a successful run | First ingestion into a new custom table takes several minutes |
| Large `untraced` cost | Model usage from a workload that emits no `gen_ai` spans. Enabling the `AzureOpenAIRequestUsage` diagnostic category on the Foundry account logs token usage for **every** caller, though it identifies the calling identity rather than the end user |
| Job fails, no logs | The Container Apps environment was created without a logs destination |

### Known environment quirk

On Windows consoles that are not UTF-8, `az acr build` crashes while streaming the
build log (`UnicodeEncodeError`, cp1252) **even when the build succeeds**. Neither
`chcp 65001` nor `PYTHONIOENCODING` fixes it. The deploy script works around this by
ignoring the exit code and polling the registry for the published tag. To read a
build log, fetch it through the REST API rather than `az acr task logs`, which
crashes the same way.

---

## 7. What the report deliberately does not do

- **It does not read prompts or completions.** No query in the workbook touches
  `gen_ai.input.messages`, `gen_ai.output.messages` or `gen_ai.system_instructions`.
- **It does not invent numbers.** Unpriced requests are flagged, not defaulted to a
  guess. Unmapped meters are kept as `unmapped`, not dropped. Untraced spend is
  reported, not hidden.
- **It does not claim to be an invoice.** `BilledUsd` is the only real money in the
  report; everything else is allocation, and the reconciliation panel says how far
  the two diverge.
