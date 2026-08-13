# Cost reporting baseline — 2026-08-12

Frozen snapshot of the reporting figures **before** switching from an embedded
retail rate card to billed-cost allocation (option C). Every number below must be
re-checked after that change; the "Validation criteria" section defines what is
allowed to move and what is not.

- **Captured (UTC):** 2026-08-12T21:02:28Z
- **Source:** `HostedAgentModelUsage` saved function, Log Analytics workspace behind
  the Application Insights resource used by the AI gateway.
- **Rollback commit:** `32771f9` (local only, not pushed). Previous state: `dc4077b`.

Identifiers are truncated deliberately. `d6bec9d8…` is the interactive operator
account used to drive the agents; the `*-pseudo-*`, `gateway-smoke-test` and
`policy-verify` keys are synthetic values from gateway testing.

## How the baseline cost was calculated

`ListCostUsd` applies a **flat, non-effective-dated retail rate card** embedded in
the KQL function:

$$
\text{ListCost} = \frac{T_{\text{in}} \cdot R_{\text{in}} + T_{\text{out}} \cdot R_{\text{out}}}{1{,}000{,}000}
$$

Two known weaknesses motivated the change:

1. The card is flat, so editing a rate silently re-prices all history.
2. Retail rates are not necessarily the billed rates. Observed in the subscription's
   own usage records: a GPT-5 input meter billed at `unitPrice = 1.25` while the
   retail card used `2.50`.

Cached input is **not** discounted in this figure, so it is an upper bound.

### Rate card in effect at capture (USD per 1M tokens)

| Model | Input | Output |
| --- | ---: | ---: |
| gpt-4o | 2.50 | 10.00 |
| gpt-5 | 2.50 | 20.00 |
| gpt-5-mini | 0.45 | 3.60 |
| gpt-5.1 | 2.50 | 20.00 |
| gpt-5.2 | 3.50 | 28.00 |
| gpt-5.4 | 5.00 | 30.00 |
| gpt-5.6-sol | 5.00 | 30.00 |
| gpt-5.6-luna | 1.00 | 6.00 |
| gpt-5-codex | 1.25 | 10.00 |
| gpt-chat-latest | 5.00 | 30.00 |
| grok-4 | 3.00 | 15.00 |
| text-embedding-3-small | 0.02 | 0.00 |
| text-embedding-3-large | 0.13 | 0.00 |

## Totals

| Metric | Value |
| --- | ---: |
| Requests | 18 |
| Distinct users | 7 |
| Input tokens | 1,009 |
| Output tokens | 499 |
| Total tokens | 1,508 |
| List cost (USD) | 0.01082890 |

## Per user

| User | Requests | Input | Output | Total | List cost (USD) |
| --- | ---: | ---: | ---: | ---: | ---: |
| d6bec9d8… | 4 | 850 | 125 | 975 | 0.00800000 |
| unattributed | 6 | 70 | 180 | 250 | 0.00111070 |
| carol-pseudo-03 | 2 | 23 | 30 | 53 | 0.00052750 |
| bob-pseudo-02 | 2 | 23 | 77 | 100 | 0.00039535 |
| alice-pseudo-01 | 2 | 23 | 77 | 100 | 0.00039535 |
| gateway-smoke-test | 1 | 10 | 5 | 15 | 0.00020000 |
| policy-verify | 1 | 10 | 5 | 15 | 0.00020000 |

## Per agent and model

| Agent | Model | Requests | Input | Output | List cost (USD) |
| --- | --- | ---: | ---: | ---: | ---: |
| research-agent | gpt-5.4 | 4 | 342 | 84 | 0.00423000 |
| triage-agent | gpt-5.4 | 2 | 528 | 51 | 0.00417000 |
| unknown | gpt-4o | 4 | 48 | 52 | 0.00064000 |
| unknown | gpt-5-mini | 2 | 22 | 128 | 0.00047070 |
| research-agent | gpt-5.1 | 1 | 11 | 17 | 0.00036750 |
| triage-agent | gpt-4o | 2 | 24 | 26 | 0.00032000 |
| research-agent | gpt-5-mini | 1 | 11 | 64 | 0.00023535 |
| triage-agent | gpt-5-mini | 1 | 11 | 64 | 0.00023535 |
| research-agent | gpt-4o | 1 | 12 | 13 | 0.00016000 |

## Daily by model

| Day | Model | Requests | Input | Output | List cost (USD) |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-08-12 | gpt-4o | 7 | 84 | 91 | 0.00112000 |
| 2026-08-12 | gpt-5-mini | 4 | 44 | 256 | 0.00094140 |
| 2026-08-12 | gpt-5.1 | 1 | 11 | 17 | 0.00036750 |
| 2026-08-12 | gpt-5.4 | 6 | 870 | 135 | 0.00840000 |

## Coverage

| Metric | Value |
| --- | ---: |
| Requests | 18 |
| Unattributed | 6 |
| Unpriced | 0 |
| Failed | 0 |

All 18 requests occurred on 2026-08-12. The 6 unattributed requests predate
gateway header capture and are retained deliberately rather than dropped.

## Validation criteria after implementing option C

**Must not change** — these come from telemetry, not pricing, so any movement
means the change broke attribution rather than repricing:

1. Request count per user, per agent and per model.
2. Input, output and total token counts at every level.
3. The unattributed count (6) and failed count (0).
4. Distinct user count (7).

**Expected to change:**

5. Cost per user, agent, model and day, because the rate source changes from
   retail to billed.
6. A new reconciliation figure appears; the sum of allocated cost for a given
   meter and day must equal the billed cost for that meter and day.

**Expected new failure modes to check for:**

7. Requests whose meter is not yet billed appear as *pending*, not as zero cost.
   Billing latency is typically 8–24 hours, so same-day traffic will not have
   billed cost immediately.
8. Requests whose model has no meter mapping appear as *unmapped*, not as zero.
9. Total allocated cost must never exceed billed cost for the same scope and period.

**Sanity check on magnitude:** billed cost may legitimately differ from the
0.01082890 baseline. It should remain the same order of magnitude. A difference
greater than roughly 2x warrants investigation of the meter mapping before the
figures are trusted.

## Validation result — 2026-08-12, after switching to billed rates

The rate card was replaced with an effective-dated table sourced from this
subscription's own Cost Management usage records, tagged `RateSource = billed`
where a real billed rate was observed and `retail` where it was not.

### Invariants — all held

| Metric | Baseline | After | Result |
| --- | ---: | ---: | --- |
| Requests | 18 | 18 | unchanged |
| Distinct users | 7 | 7 | unchanged |
| Input tokens | 1,009 | 1,009 | unchanged |
| Output tokens | 499 | 499 | unchanged |
| Total tokens | 1,508 | 1,508 | unchanged |
| Unattributed | 6 | 6 | unchanged |
| Unpriced | 0 | 0 | unchanged |
| Failed | 0 | 0 | unchanged |

No token or request figure moved, which is the required outcome: the change was
a repricing, not a change to attribution.

### Cost — changed as expected

| Scope | Baseline | After | Change |
| --- | ---: | ---: | --- |
| **Total** | 0.01082890 | **0.00662890** | −38.8% |
| gpt-5.4 | 0.00840000 | 0.00420000 | −50% (billed) |
| gpt-4o | 0.00112000 | 0.00112000 | unchanged (retail) |
| gpt-5-mini | 0.00094140 | 0.00094140 | unchanged (retail) |
| gpt-5.1 | 0.00036750 | 0.00036750 | unchanged (retail) |

The entire delta of 0.00420000 is the gpt-5.4 correction. Nothing else moved,
confirming the change was surgical.

### Root cause of the correction

The baseline priced gpt-5.4 from the retail `5.4 pp inp Gl` / `5.4 pp opt Gl`
meters at 5.00 / 30.00 USD per 1M tokens. The subscription is actually billed on
`5.4 inp Gl` / `5.4 opt Gl` at **2.50 / 15.00** — exactly half. The same pattern
held for GPT-5 (billed 1.25 / 10.00 against retail 2.50 / 20.00), while
`text-embedding-3-large` matched exactly at 0.13.

**The original report overstated gpt-5.4 spend by 2x.**

### Observed billed rates (USD per 1M tokens)

| Model | Input | Output | First seen |
| --- | ---: | ---: | --- |
| gpt-5.4 | 2.50 | 15.00 | 2026-08-04 |
| gpt-5 | 1.25 | 10.00 | 2026-08-04 |
| gpt-4.1 | 2.00 | 8.00 | 2026-08-04 |
| text-embedding-3-large | 0.13 | — | 2026-08-05 |

Regenerate with `scripts\sync-model-prices.ps1 -AccountName <account>`. Append new
effective-dated rows rather than editing existing ones; editing rewrites history,
which is the failure this change was made to prevent.

### Coverage after the change

63.4% of reported cost now rests on observed billed rates; the remaining 36.6%
(gpt-4o, gpt-5-mini, gpt-5.1) is still retail because those models were only
exercised against a different Foundry account and have no billed meter here yet.
The workbook's rate provenance panel shows this split.

### Still outstanding

Full reconciliation of allocated cost against billed cost could **not** be
completed, because there is no overlapping day:

- Gateway telemetry begins 2026-08-12.
- Billed model meters stop at 2026-08-07.

Model usage bills with a lag, so this resolves on its own once 2026-08-12 usage
is invoiced. Until then, cached-input rates for models with no billed cached
meter are inferred at 10% of the billed input rate — a ratio that holds across
every GPT-5-family meter on the public price list, but which remains an
inference rather than an observation.

## Second validation — 2026-08-13, after billing caught up

2026-08-12 usage was invoiced overnight, which both closed the reconciliation gap
and revealed billed rates for the remaining models.

### Reconciliation now possible, and it matches

For gpt-5.4 on `foundry-pocs`, 2026-08-12:

| Measure | Input tokens | Output tokens |
| --- | ---: | ---: |
| Billed (Cost Management) | 880 | 140 |
| Observed at the gateway | 870 | 135 |
| Difference | 10 | 5 |

The difference is exactly the single direct-to-Foundry call made while validating
the backend URL, which bypassed the gateway by design. **The gateway accounts for
100% of the traffic that passed through it**, and the residual is explained rather
than unexplained.

This also demonstrates the limit of the approach: traffic that does not traverse
the gateway is invisible to per-user attribution, though it still bills.

### Rates corrected again

Newly observed billed rates, all effective 2026-08-12:

| Model | Direction | Was | Now | Source |
| --- | --- | ---: | ---: | --- |
| gpt-4o | input / output | 2.50 / 10.00 | 2.50 / 10.00 | billed, retail was already correct |
| gpt-5.1 | input | 2.50 | **1.25** | billed |
| gpt-5-mini | output | 3.60 | **2.00** | billed |

`gpt-5.1` output and `gpt-5-mini` input have no billed meter yet, so those models
are marked `mixed` — one direction authoritative, one still retail.

### Invariants — held again

Requests 18, distinct users 7, tokens 1,009 / 499 / 1,508, unattributed 6,
unpriced 0, failed 0. Unchanged from the baseline for the second time.

### Cost

| Scope | Baseline | After round 1 | After round 2 |
| --- | ---: | ---: | ---: |
| **Total** | 0.01082890 | 0.00662890 | **0.00620555** |
| gpt-5.4 | 0.00840000 | 0.00420000 | 0.00420000 |
| gpt-4o | 0.00112000 | 0.00112000 | 0.00112000 |
| gpt-5-mini | 0.00094140 | 0.00094140 | 0.00053180 |
| gpt-5.1 | 0.00036750 | 0.00036750 | 0.00035375 |

Total correction from the original retail-based figure: **−42.7%**.

### Rate provenance now

| Confidence | Share of cost |
| --- | ---: |
| Authoritative (billed both directions) | 85.7% |
| Partly authoritative (`mixed`) | 14.3% |
| Retail estimate | 0% |

No figure in the report now rests purely on the public price list.

## Cached tokens are not available in the gateway log

Confirmed against the table schema rather than by sampling.
`ApiManagementGatewayLlmLog` exposes only `PromptTokens`, `CompletionTokens` and
`TotalTokens`. Model responses do return `usage.input_tokens_details.cached_tokens`,
but API Management does not persist that breakdown to the log.

Cached and reasoning counts are therefore taken from the `llm-emit-token-metric`
policy output in `AppMetrics`, which does carry them. The workbook states this
where the figures are used, so the two sources are not confused.
