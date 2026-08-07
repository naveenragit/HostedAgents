# Implementation Plan

**Architecture:** three-tier chat application using an existing Microsoft Foundry project
**Last reviewed:** 2026-08-07

## Purpose

This repository demonstrates two Foundry hosted-agent deployment paths behind one application-owned
API and one browser client. It does not provision or own the Foundry project, model deployment,
MongoDB cluster, container registry, network, monitoring workspace, or Entra tenant configuration.

The intended system is:

```text
React + TypeScript UI
        |
   | loopback-only /api, no caller credentials
        v
FastAPI service
   |-- loopback boundary and fixed POC scope
   |-- user-scoped conversation catalog
   |-- idempotent turn orchestration and deletion
        |
   +---- conversation=<linked id> ------------> research-agent (container, Python 3.11)
   |                                             `-- Foundry owns transcript
   |
   +---- bounded history, store=false --------> triage-agent (code, python_3_13)
        |
   `---- MONGODB-OIDC -----------------------> Existing Cosmos DB for MongoDB vCore cluster
                        |-- user/Foundry conversation links
                        |-- idempotency and deletion metadata
                        `-- triage transcript
```

## Components

| Component | Responsibility | Deployment ownership |
|---|---|---|
| `frontend/` | React 19 chat UI; calls only the API | Not deployed by this repository |
| `backend/api/` | Local access boundary, user scoping, strategy selection, orchestration, and deletion | Not deployed by this repository |
| `backend/agents/research-agent/` | Low-level Responses adapter example | Container version deployed to an existing project |
| `backend/agents/triage-agent/` | Microsoft Agent Framework example | Source/code version deployed to an existing project |
| Existing Mongo vCore cluster | Conversation catalog, user/Foundry links, turn metadata, and custom triage transcript | External prerequisite |
| Existing Foundry project and model | Hosts and executes agent versions; owns research transcripts | External prerequisite |
| Existing Azure Container Registry | Builds/stores the container-agent image | External prerequisite |

`azure.yaml` is hand-maintained and binds only the two agent services to existing resources. There
is deliberately no project-provisioning service. Resource creation, mutation, and teardown remain
with the Azure environment owner.

## Architecture Decisions

### Conversation ownership is strategy-specific

The contrast between platform-managed and application-managed conversation state is the purpose of
this POC. `FOUNDRY_CONVERSATION_AGENTS` selects the Foundry-managed agents and must be a subset of
`AGENT_NAMES`; the committed example selects only `research-agent`.

For `research-agent`:

1. The API creates a durable Foundry Conversation and a user-scoped application conversation.
2. Cosmos stores the `foundryConversationId` link, manager type, counters, and non-content turn
   metadata. It does not duplicate research message content in the `messages` collection.
3. Each invocation supplies only the new user item and `conversation=<foundryConversationId>`.
   Foundry maintains prior conversation items. The low-level hosted adapter exposes that history
   separately from current input, so the research handler reads `get_history()` and
   `get_input_items()`, then forwards the newest complete pairs under one deterministic character
   budget to its downstream model.
4. After a completed response, the API records the Foundry user and assistant item IDs in its local
   idempotency record. History and completed-turn replay read those items from Foundry.

For `triage-agent`:

1. Cosmos is the durable transcript owner.
2. The API loads a bounded chronological window without splitting a user/assistant pair, preserves
   roles, and appends the new user input once.
3. The hosted-agent request uses `store=false`; after success the API transactionally persists both
   messages in Cosmos.

Both paths expose the same application API. The conversation response identifies its manager but
does not expose the server-side Foundry conversation ID. Foundry-projected messages use
`createdAt: null` because Foundry conversation items do not expose a per-item timestamp.
Input/output limits and history selection remain deterministic and covered by tests.

### The API owns orchestration and user scoping

Hosted agents never receive MongoDB credentials and never select the conversation manager. The API
is the only component that maps an application user and conversation to Foundry or Cosmos, so user
scoping and deletion have one auditable enforcement point. This also avoids post-deployment
database role assignments to per-agent identities. Every MongoDB operation must include `userId`
through the repository `_scope()` helper.

The cluster speaks the MongoDB wire protocol. Use PyMongo with `MONGODB-OIDC` and
`DefaultAzureCredential`, requesting
`https://ossrdbms-aad.database.windows.net/.default`. Do not use `azure-cosmos`, account keys, or
passwords. Do not configure `ALLOWED_HOSTS` with PyMongo's machine `OIDC_CALLBACK`; that option is
valid only for the interactive `OIDC_HUMAN_CALLBACK` flow.

### Turns are idempotent

The send-turn workflow accepts an opaque client idempotency key. The unique scope is
`(userId, conversationId, idempotencyKey)`.

1. Validate caller ownership, agent allow-list membership, input bounds, and authorization scope.
2. Atomically reserve the key with state `pending` and a request fingerprint.
3. If the same key and fingerprint already completed, return the stored user/assistant pair.
4. If the key exists with another fingerprint, return a conflict without invoking the model.
5. Resolve the conversation manager. For research, invoke once using its Foundry conversation ID;
   for triage, load bounded role-preserving Cosmos history and invoke once with `store=false`.
6. Atomically mark the operation complete and update counters. Triage also stores both messages;
   research stores only the durable Foundry user/assistant item references.
8. Before dispatch, persist state `invoking` and disable SDK retries. A pre-dispatch failure may be
   retried safely; a post-dispatch ambiguous outcome becomes terminal `indeterminate` and returns
   `409` rather than risking a second non-idempotent model invocation.

The repository implementation uses MongoDB transactions, a unique compound turn index, leases, and
explicit recovery. Replays of completed turns return one logical result; concurrent pending turns
return `409` plus `Retry-After`; same-key/different-body and indeterminate outcomes return `409`.

### Deletion uses a tombstone workflow

Deletion is fenced, transactional, and auditable rather than an uncoordinated pair of hard deletes.

1. A user-scoped delete marks the conversation `deleting`, records a deletion request ID and time,
   and immediately hides it from normal reads.
2. New turns and retries are rejected once deletion begins.
3. For a Foundry-managed conversation, the retained local tombstone keeps the scoped
   `foundryConversationId` available while the API deletes the remote conversation. A `404` from
   Foundry is treated as successful idempotent cleanup.
4. Only after remote cleanup succeeds does the transactional purge delete user-scoped messages and
   operation records. Cosmos-managed conversations go directly from the same fence to local purge.
5. A minimal terminal tombstone records only non-content proof needed for retry/audit, with an
   explicit short retention period.
6. Repeated delete requests are idempotent. If remote cleanup fails, the retained link and deleting
   state allow a later DELETE to resume without reopening the conversation to sends.
7. Privacy verification covers both stores: research content is absent from Foundry and its local
   link is purged; triage content is absent from Cosmos; backups/retention follow approved policy.

The terminal document contains only user/conversation identifiers, deletion request metadata, and
`expiresAt`; a TTL index removes it after the configured retention period (one day by default,
seven-day hard maximum). Tests cover cross-user 404 behavior, blocked live turns, repeated deletion
without retention extension, content removal, and index configuration.

### Local POC access boundary

The API has no application-user authentication. It rejects non-loopback peers and assigns every
request one server-owned `API_POC_USER_ID`; request headers, bodies, and query parameters cannot
select a persistence identity. Startup requires `API_POC_MODE=true` and refuses recognized Azure
compute environments. The Vite proxy and documented Uvicorn command both bind to `127.0.0.1`.

These controls make the sample suitable for a single-workstation POC, not for shared-network or
deployed use. Before deployment, replace this boundary with owner-approved caller authentication,
authorization, stable tenant/user scoping, abuse controls, and a reviewed hosting design. Azure
service authentication for Foundry and MongoDB remains keyless and is independent of caller auth.

### Responsible AI policy is a deployment prerequisite

Each model deployment used by the hosted agents must have an environment-owner-approved Responsible
AI/content-filter policy. Before release, record the policy name/version outside source control if it
contains environment identifiers, verify the deployment is actually bound to it, and test expected
handling for harmful content, prompt injection, data exfiltration attempts, and over-refusal. An
evaluation gate must cover quality and safety regressions before promoting an agent version. The
public release gate accepts only private, out-of-tree evidence conforming to
`docs/release-evidence.schema.json`; it verifies the exact source commit, both immutable agent
versions, deployment/policy binding records, predeclared thresholds, safety coverage, and passing
results. This source contract is not evidence that any live environment has passed.

### Agent packages are isolated

Foundry code deployment packages only the service directory, and the container build context is also
the service directory. Do not add a sibling shared package under `backend/agents/`. Publish shared
logic as a reviewed, versioned wheel and pin it in each agent's requirements.

The container image must be `linux/amd64`; `docker.remoteBuild: true` is retained for ARM64 developer
hosts. The code-deploy runtime remains `python_3_13`, because `python_3_11` is not accepted by that
platform path. First-party source remains compatible with the Python 3.11 language level.

## Deployment Boundaries

This repository may:

- validate and package source;
- deploy new immutable versions of the two named hosted-agent services when a human explicitly
  chooses the target environment;
- invoke agents for smoke tests and evaluations.

This repository must not automatically:

- create, replace, or delete the Foundry account/project or model deployment;
- create or delete the MongoDB cluster/database outside the explicit database initialization path;
- create or delete shared registries, monitoring resources, private endpoints, or role assignments;
- sign in a user, select an Azure subscription, or infer a production target;
- run resource teardown or purge commands.

The environment owner supplies the existing project endpoint/ARM ID, model deployment name, tenant,
subscription, resource group, and registry coordinates. Deployment requires reviewed RBAC: the
deployer's Foundry permissions, registry push rights, and `AcrPull` for the applicable Foundry managed
identities. Network rules and private connectivity are environment-owned controls.

## Configuration Classification

Public identifiers are not credentials, but publishing live values unnecessarily maps an
environment. Documentation and examples use placeholders.

| Public configuration/identifier | Secret or sensitive value |
|---|---|
| Tenant, subscription, resource-group, account, project, deployment, registry, cluster and agent names | Access/refresh tokens, API keys, client secrets, passwords |
| ARM resource IDs and service endpoints | Private keys, certificates with private material |
| Entra application/client ID and API audience | Connection strings containing credentials or signed URLs |
| Database and collection names | Unredacted logs, message content, exported conversations |
| RAI policy name when approved for publication | Security-report details and vulnerability evidence |

Both classes must stay out of examples when they identify a live environment. Secrets must never be
committed. Local `.env` and `.azure/` state are ignored and must never be copied into release
artifacts. If a secret enters Git, rotate/revoke it first and then purge Git history. Rewrite history
only when prior commits contain material that the owner has decided must be removed; do not rewrite
history merely because current files use sanitized placeholders.

## Local and CI Validation

`scripts/bootstrap.ps1` creates the Python environments and runs `npm ci` from the frontend lock
file. `scripts/check.ps1` is the shared local gate:

- Ruff lint and format checks;
- strict mypy checks per independently packaged Python component;
- offline pytest suite;
- strict `pip-audit` for every requirements file;
- frontend test command when present, typecheck, lint, and production build;
- production and full dependency-graph npm audits at `high`.

CI adds full-history Gitleaks scanning and verifies that a `git archive` artifact contains only
tracked, non-sensitive paths. Pull-request CI performs no Azure login, provisioning, deployment, or
live model invocation.

## Current Status

Status observed from source on 2026-08-07; this section does not claim commands were run as part of
this document rewrite.

| Area | Source state | Release consequence |
|---|---|---|
| Three-tier layout | Present | Architecture paths are current |
| Existing-project agent binding | Present in hand-maintained `azure.yaml` | Never use project provisioning/teardown workflows |
| Mongo user scoping and keyless OIDC | Present | Retain and test `_scope()` on every operation |
| Split conversation management | Research uses a linked Foundry Conversation; triage uses custom Cosmos history with `store=false` | Source contract implemented and offline-tested |
| Research transcript duplication | Cosmos stores only the Foundry link, counters, and item references | Source contract implemented and offline-tested |
| Idempotent turns | Durable pending/invoking/completed/failed/indeterminate states and unique key | Ambiguous remote outcomes fail terminally rather than redispatch |
| Pagination | Stable keyset cursors in repository, API, UI, and smoke contract | Source contract implemented and offline-tested |
| Deletion | Transactional fence; remote research cleanup; local purge; expiring content-free tombstone | Environment retention/backups still need owner approval |
| Local POC boundary | Fixed identity, loopback peer checks, Azure-compute startup refusal | Must be replaced before shared-network or deployed use |
| Frontend access | No credentials or identity headers; Vite binds to loopback | Not a production authentication design |
| RAI policy/evaluation evidence | Fail-closed private evidence schema and verifier | Actual deployment binding and eval evidence remain release blockers |
| Validation CI | Read-only workflow; no Azure auth/deploy; shared gate plus history/artifact scans | Branch/ruleset ownership remains external |
| API and frontend hosting | No deployment definition in this repository | Local/sample scope only |
| License | Intentionally absent pending owner/legal approval | Public release blocker |

## Delivery Sequence

1. Obtain environment-owner evidence for the approved RAI/content-filter binding and run the same
   versioned quality/safety gate against both immutable agent versions.
2. Resolve remaining dependency advisories, produce the approved SBOM/provenance, and complete the
   operational/privacy portions of the release checklist.
3. Obtain an approved `LICENSE` and replace owner-contact placeholders.
4. Configure repository secret scanning, push protection, rulesets, and update ownership outside
   this source tree.
5. Run validation in a clean clone, then build the public artifact with
   `scripts/public-release.ps1 -ReleaseEvidencePath <private-path>`.

## References

- [Hosted agents overview](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
- [Hosted agent runtime contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract)
- [Deploy a container hosted agent](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent)
- [Deploy a hosted agent from source](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code)
- [Hosted-agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Microsoft Entra protected web API](https://learn.microsoft.com/entra/identity-platform/scenario-protected-web-api-overview)
- [Azure AI content filtering](https://learn.microsoft.com/azure/ai-services/openai/concepts/content-filter)
