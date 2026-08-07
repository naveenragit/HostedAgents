# Hosted Agents

A three-tier chat application over **Microsoft Foundry hosted agents** that demonstrates two
conversation-management strategies behind one API and UI.

```
frontend/                    React 19 + TypeScript (Vite) chat UI
  │ same-origin /api
  ▼
backend/api/                 FastAPI orchestration and user-scoped catalog
  ├─ research-agent ────── Responses + conversation ID ──► Foundry Conversations
  │                                                        owns research history
  ├─ triage-agent ──────── Responses + store=false ───────► hosted agent
  │
  └─ MONGODB-OIDC ────────────────────────────────────────► Cosmos DB for MongoDB
                               user/Foundry links, turn metadata,
                               and triage history
```

| Component | Deployment | Runtime | Framework |
|-----------|-----------|---------|-----------|
| [frontend](frontend) | local (`npm run dev`) | Node 22 / React 19 | Vite |
| [backend/api](backend/api) | local (`uvicorn`) | Python 3.11 | FastAPI |
| [backend/agents/research-agent](backend/agents/research-agent) | **Container** → ACR → Foundry | Python 3.11 | raw `azure-ai-agentserver-responses` adapter |
| [backend/agents/triage-agent](backend/agents/triage-agent) | **Code** (ZIP) → Foundry builds it | `python_3_13` | Microsoft Agent Framework |

Design rationale, decisions and risks: **[docs/implementation-plan.md](docs/implementation-plan.md)**.
Rules for anyone (or any AI agent) changing this code: **[AGENTS.md](AGENTS.md)**.

---

## Current status

This is a local proof of concept for an **existing** Foundry project, not a production deployment.
The API intentionally has no application-user authentication. It accepts only direct loopback
clients, refuses startup on recognized Azure compute, and uses one server-owned `poc-user` identity
for database scoping. Vite and FastAPI must both remain bound to `127.0.0.1`.

The POC retains the controls that matter for sharing code: keyless Azure service authentication,
agent allow-listing, explicit transcript ownership, bounded role-preserving input for the custom
path, durable idempotency fences, cursor pagination, generic errors, and content-free deletion
tombstones. Add real caller authentication and authorization before any shared-network or deployed
use.

No `LICENSE` is currently present. Obtain owner/legal approval before publishing this repository;
the code can be shared privately only under terms supplied by the repository owner.

---

## Three things worth knowing before you start

**1. Conversation ownership is deliberately different for the two agents.**

| Agent | Transcript owner | What Cosmos stores | Invocation |
|---|---|---|---|
| `research-agent` | **Foundry Conversations** | The user-scoped application conversation, its server-only `foundryConversationId`, counters, and idempotent turn/item references; no duplicate message content | The new user item plus `conversation=<foundryConversationId>`; the hosted handler reads Foundry history for inference |
| `triage-agent` | **Cosmos DB for MongoDB** | The complete custom transcript and turn state | Bounded chronological history with `store=false` |

The FastAPI layer chooses the strategy, enforces the `userId` scope, and coordinates deletion. The
hosted agents never receive database credentials. This also avoids a real trap: a hosted agent's
Entra identity does not exist until *after* its first deploy, so agent-side database access would
force a deploy → assign-role → redeploy cycle.

**2. Cosmos DB for MongoDB (vCore) is not Cosmos DB for NoSQL.**
The cluster host is `*.mongocluster.cosmos.azure.com` and speaks the MongoDB wire protocol, so the
persistence layer uses **PyMongo** — `azure-cosmos` physically cannot connect to it. Auth is keyless
via `MONGODB-OIDC` with a `DefaultAzureCredential` token callback, and deliberately **without**
`ALLOWED_HOSTS` (PyMongo accepts that only for the interactive browser flow).

**3. Python 3.11 everywhere except `triage-agent`.**
Foundry's code-deploy path accepts only
[`python_3_13` / `python_3_14`](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code#supported-runtimes).
The container agent can use 3.11 because its Dockerfile owns the runtime. The whole repo is pinned
to the **3.11 language level** in ruff and mypy so nothing drifts onto newer syntax.

---

## Prerequisites

### Local tooling

| Tool | Version | Notes |
|------|---------|-------|
| Python | **3.11** *and* **3.13** | Verify with `py -0p`. Both are needed — see the venv table below. |
| Node.js | **22 recommended** (`20.19+` supported) | For the frontend. Verify with `node --version`. |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | 2.80+ | Resource discovery and the local `DefaultAzureCredential`. |
| [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) | 1.28+ | Deploys the agents. It has a separate login and needs the `azure.ai.agents` extension. |
| Docker Desktop | any | **Only** to run `research-agent` locally. Not needed to deploy — images build remotely in ACR. |

### Azure resources

You need access to an existing Foundry project and a Mongo vCore cluster. This repo does **not**
create them (there is deliberately no `azd provision`).

| Resource | Why | Required access |
|----------|-----|-----------------|
| Foundry project | Hosts the agents, serves the model | **Foundry Project Manager** at project scope |
| Model deployment | The agents' LLM | Must already exist in the project |
| Cosmos DB for MongoDB (vCore) cluster | Conversation catalog, turn metadata, user/Foundry links, and triage history | Your Entra identity registered on the cluster (see below) |
| Azure Container Registry | Only for the **container** agent | `AcrPush` for you, `AcrPull` for the Foundry identities |

### Finding your values

```powershell
# Subscription
az account show --query id -o tsv

# Tenant
az account show --query tenantId -o tsv

# Foundry account + resource group
az cognitiveservices account list --query "[].{name:name, rg:resourceGroup, location:location}" -o table

# Project ARM resource ID — needed by azd. Do NOT hand-construct this from the endpoint.
az resource show `
  --ids "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>" `
  --api-version 2025-06-01 --query id -o tsv

# Model deployments available in the project
az cognitiveservices account deployment list -n <account> -g <rg> `
  --query "[].{name:name, model:properties.model.name}" -o table
```

The project **endpoint** has the shape
`https://<account>.services.ai.azure.com/api/projects/<project>`.

### Registering your identity on the Mongo cluster

This is the single most common blocker. `az login` alone is **not** enough — your Entra identity
must also be granted access on the cluster itself, or every connection fails authentication.

- **Administrative identity:** Azure portal → your Mongo vCore / DocumentDB cluster →
  **Authentication** → add your Entra ID user → **Save**.
- **Non-administrative identity:** connect as an admin and run
  `db.runCommand({ createUser: "<entra-object-id>", roles: [...], customData: { IdentityProvider: { type: "MicrosoftEntraID", ... } } })`.

Full procedure: [Configure role-based access control for Azure DocumentDB](https://learn.microsoft.com/azure/documentdb/how-to-connect-role-based-access-control).

> **`mongosh` and MongoDB Compass cannot do Entra OIDC against a Cosmos vCore endpoint.** That is a
> tool limitation, not a misconfiguration. Verify connectivity with `scripts/init-database.ps1`
> instead.

---

## Get started and deploy

Follow these steps in order. The repository deploys the two hosted agents only; the unauthenticated
FastAPI service and React UI intentionally remain local-only. Steps 1–3 are normally one-time setup
for an Azure environment. Steps 4–7 configure and run the local application.

### 1. Authenticate the CLIs

Azure CLI and Azure Developer CLI keep separate login state. Sign in to both with the identity that
has access to the existing Foundry project, Mongo cluster, and container registry:

```powershell
az login
az account set --subscription "<subscription-guid>"
az account show --query "{subscription:id, tenant:tenantId, user:user.name}" -o table

azd auth login
azd auth login --check-status
azd extension install azure.ai.agents
```

The `az login` identity is also what the local API uses through `DefaultAzureCredential`. Do not
replace either login with an API key, database password, or client secret.

### 2. Bootstrap the environments

```powershell
.\scripts\bootstrap.ps1
```

Creates three virtual environments. They are separate on purpose:

| Venv | Python | Contents | Why separate |
|------|--------|----------|--------------|
| `.venv` | 3.11 | dev tooling + API deps | Runs ruff / mypy / pytest; the tests import `app.*` |
| `backend/api/.venv` | 3.11 | `backend/api/requirements.txt` | The service's own runtime, rebuildable independently |
| `backend/agents/triage-agent/.venv` | 3.13 | that agent's requirements | Must match the code-deploy runtime, and `azd ai agent run` resolves the venv from the **service** directory |

`constraints-local.txt` is applied during bootstrap only. It pins `cryptography` to the newest
release publishing a `win_arm64` wheel so this repo bootstraps on Windows/ARM64 without a Rust +
OpenSSL source build. **It never reaches Azure.**

### 3. Deploy the hosted agents

Configure an azd environment for the **existing** Foundry project. Use the values found in
[Finding your values](#finding-your-values):

```powershell
azd env new dev --subscription "<subscription-guid>" --location "<region>"

azd env set AZURE_AI_PROJECT_ENDPOINT      "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set FOUNDRY_PROJECT_ENDPOINT       "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set AZURE_AI_PROJECT_ID            "<project-arm-resource-id>"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "<existing-model-deployment-name>"
azd env set AZURE_TENANT_ID                "<tenant-guid>"
azd env set AZURE_RESOURCE_GROUP           "<foundry-resource-group>"
```

Deploy the direct-code agent first. It needs no registry:

```powershell
azd deploy triage-agent --no-prompt
```

The container agent needs an existing ACR plus the RBAC described in
[Container agent](#container-agent-needs-an-owner-managed-registry). After that one-time setup:

```powershell
azd env set AZURE_CONTAINER_REGISTRY_ENDPOINT "<acrname>.azurecr.io"
azd env set AZURE_CONTAINER_REGISTRY_NAME     "<acrname>"
azd deploy research-agent --no-prompt
```

Confirm that both agents report an active/deployed status before starting the local application:

```powershell
azd ai agent show triage-agent --output json
azd ai agent show research-agent --output json
```

There is deliberately no `azd provision` or `azd up`: this repository consumes existing Azure
resources and must not take ownership of their lifecycle. Each successful deploy creates a new,
immutable agent version and makes the latest version active.

### 4. Configure the local application

```powershell
Copy-Item backend\api\.env.example backend\api\.env
Copy-Item frontend\.env.example    frontend\.env.local
```

Edit `backend\api\.env`:

| Variable | Example | Notes |
|----------|---------|-------|
| `FOUNDRY_PROJECT_ENDPOINT` | `https://<account>.services.ai.azure.com/api/projects/<project>` | Required |
| `AGENT_NAMES` | `research-agent,triage-agent` | Allow-list. A client can never reach an agent that is not listed. |
| `FOUNDRY_CONVERSATION_AGENTS` | `research-agent` | Agents whose durable transcript is owned by Foundry. Must be a subset of `AGENT_NAMES`; omitted agents use Cosmos history. |
| `MONGO_CONNECTION_STRING` | `mongodb+srv://<cluster>.mongocluster.cosmos.azure.com/?tls=true&authMechanism=MONGODB-OIDC&retrywrites=false&maxIdleTimeMS=120000` | Required. Contains **no password**. |
| `MONGO_DATABASE` | `agentchat` | Created for you in step 3 |
| `API_POC_MODE` | `true` | Explicit opt-in. Refused on recognized Azure compute. |
| `API_POC_USER_ID` | `poc-user` | Fixed server-owned persistence scope; clients cannot override it. |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173` | Explicit allow-list; never becomes `*` |
| `API_LOG_LEVEL` / `API_MAX_INPUT_CHARS` | `INFO` / `8000` | Optional |

Both files are git-ignored. There are **no passwords in either**. Azure service access uses
`DefaultAzureCredential`; API callers are restricted to loopback rather than authenticated.
Confirm nothing sensitive is tracked:

```powershell
git check-ignore -q backend\api\.env; $LASTEXITCODE    # 0 = ignored, good
```

### 5. Create the database

```powershell
.\scripts\init-database.ps1
```

Idempotent. Creates the database, all three collections (`conversations`, `messages`, and `turns`)
and their indexes by calling the same
`ensure_schema()` the API runs at start-up, so provisioning cannot drift from the application.
The `messages` collection contains only Cosmos-managed transcripts; research message content stays
in Foundry. The `turns` collection keeps idempotency state and Foundry item references. The schema
also includes a TTL index for content-free deletion tombstones. This is the fastest way to prove
your Entra identity works against the cluster.

Expected output:

```
Connecting with your az login identity (MONGODB-OIDC)...
INFO     api.init connected
INFO     api.init database ready
Database 'agentchat' is ready. Collections: conversations, messages, turns
```

### 6. Run it

```powershell
# Terminal 1 — API on http://127.0.0.1:8000
cd backend\api
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
# Terminal 2 — UI on http://localhost:5173
cd frontend
npm run dev
```

Open <http://localhost:5173>. Interactive API docs: <http://127.0.0.1:8000/docs>.

The Vite dev server proxies `/api` and `/health` to the API, so the browser stays on one origin and
CORS behaves the same as it would behind a reverse proxy.

### 7. Verify end to end

```powershell
.\scripts\smoke-test.ps1                            # triage-agent
.\scripts\smoke-test.ps1 -AgentName research-agent  # container agent
```

Checks health, lists agents, creates a conversation, verifies the expected transcript owner, sends a
real turn to the deployed agent, reads history back from that owner, verifies pagination and replay
idempotency, and deletes its test conversation in a `finally` block. For research, deletion removes
the Foundry conversation before purging its Cosmos link. The API and UI must already be running, and
both named agents must already be deployed. The script refuses non-loopback API URLs. This calls the
live model, so it costs tokens.

---

## Daily development

### Quality gate

```powershell
.\scripts\check.ps1
```

Runs, in order:

| Step | Note |
|------|------|
| `ruff check` / `ruff format --check` | Security (`S`), blind-except (`BLE`), bugbear, async, annotations |
| `mypy --strict` × 4 | Once per package — several have a top-level `main.py`, so one run over `backend` collides on duplicate module names |
| `pytest` | Offline tests. No Azure login, deployment, or live model call. |
| `pip-audit --strict` × 4 | Once per requirements file — the agents pin different preview adapter versions and would false-conflict if resolved together |
| `npm run test --if-present` | Runs the Vitest frontend suite |
| `npm run typecheck`, `lint`, `build` | TypeScript and production-bundle checks |
| `npm audit --omit=dev --audit-level=high` | Fails high/critical production dependency advisories |
| `npm audit --audit-level=high` | Fails high/critical advisories in the complete frontend dependency graph |

### Repository layout

```
azure.yaml                          azd project — both agent services, existing Foundry project
pyproject.toml                      repo-wide tooling config (ruff, mypy, pytest)
requirements-dev.txt                root venv tooling
constraints-local.txt               local-host-only pip pins (Windows/ARM64 wheel gap)

scripts/bootstrap.ps1               create the three venvs, install dependencies
scripts/check.ps1                   lint + types + tests + dependency audit
scripts/init-database.ps1           create database, collections, indexes
scripts/smoke-test.ps1              loopback-only end-to-end agent/persistence test
scripts/verify-release-evidence.ps1 validate private RAI/evaluation evidence for a commit
scripts/verify-release-artifact.ps1 build and inspect the tracked-file ZIP
scripts/public-release.ps1          fail-closed public artifact gate

backend/api/
  .env.example                      template — copy to .env
  requirements.txt
  app/main.py                       app factory, lifespan, domain-error → HTTP mapping
  app/config.py                     env parsing, eager validation, POC deployment guard
  app/dependencies.py               DI wiring for shared clients
  app/domain/models.py              API contract + document shape in one place
  app/security/auth.py              loopback boundary → fixed POC Principal
  app/repositories/mongo.py         PyMongo + OIDC; every filter is user-scoped
  app/services/agent_client.py      agent allow-list + Foundry conversation operations
  app/services/conversation_service.py   selects and coordinates transcript ownership
  app/api/routes/                   conversations, agents, health
  app/cli/init_database.py          schema provisioning entry point

backend/agents/research-agent/      container agent (Dockerfile, .dockerignore)
backend/agents/triage-agent/        code agent (.agentignore)
backend/tests/                      offline unit tests

frontend/src/api/client.ts          typed fetch wrapper
frontend/src/hooks/useChat.ts       chat state, request cancellation
frontend/src/components/            ConversationList, MessageList, MessageComposer, ErrorBanner
```

> ⚠️ **Do not add a shared package under `backend/agents/`.** Code deploy zips **only** the service
> directory, and the Docker build context is **only** the service directory. A sibling package
> imports fine locally and then fails with `ModuleNotFoundError` in the sandbox. Publish a versioned
> wheel and add it to each agent's `requirements.txt` instead.

---

## API reference

Every route uses the same server-owned POC principal. **No route accepts a user id from the client.**
This preserves repository scoping but does not provide multi-user isolation or authentication.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health/live` | Liveness. Touches no dependencies. |
| `GET` | `/health/ready` | Readiness. Pings the database. |
| `GET` | `/api/capabilities` | Browser-relevant server limits |
| `GET` | `/api/agents` | Agents this API is allowed to address |
| `POST` | `/api/conversations` | Start a conversation — body `{"agentName": "..."}` |
| `GET` | `/api/conversations` | List conversations; optional `limit` and `cursor` |
| `GET` | `/api/conversations/{id}` | Fetch one |
| `DELETE` | `/api/conversations/{id}` | Fence and purge it, retaining only an expiring content-free tombstone |
| `GET` | `/api/conversations/{id}/messages` | One chronological page; optional `limit` and `cursor` |
| `POST` | `/api/conversations/{id}/messages` | Send a turn; requires `Idempotency-Key` |

List responses have the shape `{"items": [...], "nextCursor": "..."}`. Pass `nextCursor` back as
`cursor` until it is `null`. An idempotency key is scoped to the caller and conversation: replaying
the same key and body returns the persisted turn, while reusing it for different content returns
`409`. A turn still in flight returns `409` with `Retry-After`; a post-dispatch timeout also returns
`409`, but is terminal because retrying could duplicate a non-idempotent model request.

Conversation responses include `conversationManager` (`foundry` or `cosmos`). The user-scoped
`foundryConversationId` link remains server-side in Cosmos and is not exposed to the browser.
Foundry conversation items do not expose item timestamps, so their message `createdAt` is `null`;
Cosmos-managed messages retain timestamps.

Example from the same workstation:

```powershell
$h = @{
  'Content-Type' = 'application/json'
  'Idempotency-Key' = [guid]::NewGuid().ToString()
}
$c = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/conversations' -Method POST -Headers $h `
       -Body '{"agentName":"triage-agent"}'
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/conversations/$($c.id)/messages" -Method POST `
       -Headers $h -Body '{"content":"The export button does nothing in Safari."}'
```

### POC access boundary

The browser sends no bearer token or user identity header. FastAPI rejects non-loopback peers and
scopes all records to `API_POC_USER_ID`. Vite also binds to `127.0.0.1`, preventing its `/api` proxy
from turning the local API into a network service. Do not use `--host 0.0.0.0`, expose either port,
or place a network-accessible reverse proxy in front of this POC. A deployed version needs real
authentication, authorization, tenant-aware scoping, rate limits, and an owner-reviewed threat model.

---

## Deploying the agents

The API and UI are local-only. The agents deploy into the **existing** Foundry project.

### One-time azd setup

```powershell
azd env new dev --subscription "<subscription-guid>" --location "<region>"

azd env set AZURE_AI_PROJECT_ENDPOINT      "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set FOUNDRY_PROJECT_ENDPOINT       "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set AZURE_AI_PROJECT_ID            "<project-arm-resource-id>"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "<existing-model-deployment-name>"
azd env set AZURE_TENANT_ID                "<tenant-guid>"
azd env set AZURE_RESOURCE_GROUP           "<foundry-resource-group>"
```

There is **no `azd provision`** — the project already exists and this repo does not own it.

Verify only the required names, without printing their values:

```powershell
$required = @(
  'AZURE_AI_PROJECT_ENDPOINT',
  'AZURE_AI_PROJECT_ID',
  'AZURE_AI_MODEL_DEPLOYMENT_NAME',
  'AZURE_TENANT_ID',
  'AZURE_RESOURCE_GROUP'
)

foreach ($name in $required) {
  $value = azd env get-value $name 2>$null
  $state = if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($value)) {
    'set (redacted)'
  } else {
    'MISSING'
  }
  "$name=$state"
}
```

Do not paste an environment-wide dump into an issue, pull request, build log, or chat. Public Azure
identifiers are not credentials, but they map a live environment and should be shared only when the
troubleshooting context requires them.

### Code agent (no extra infrastructure)

```powershell
azd deploy triage-agent --no-prompt
```

### Container agent (needs an owner-managed registry)

`azd` only wires registry RBAC for registries it provisions itself. This repository does not own
the registry or its role assignments. Before deploying, the environment owner must provide an
existing registry, grant the deployer push access, and grant the applicable Foundry account/project
identities `AcrPull` at the narrowest practical scope.

```powershell
# Use only values supplied and approved by the environment owner.
azd env set AZURE_CONTAINER_REGISTRY_ENDPOINT "<acrname>.azurecr.io"
azd env set AZURE_CONTAINER_REGISTRY_NAME     "<acrname>"
azd deploy research-agent --no-prompt
```

### Verify

```powershell
azd ai agent show triage-agent --output json     # expect "status": "active"
azd ai agent show research-agent --output json   # expect "status": "active"
.\scripts\smoke-test.ps1 -AgentName research-agent
```

Each successful deploy creates a **new immutable version**; the latest is active.

### Constraints to respect

- **Container images must be `linux/amd64`.** `docker.remoteBuild: true` in `azure.yaml` makes ACR
  build them, which is what keeps this working from an ARM64 workstation. A local build needs
  `docker build --platform linux/amd64 .`
- **`FOUNDRY_*` and `AGENT_*` are both reserved env prefixes.** Never declare either in
  `azure.yaml`. This repo's agent settings use `APP_*` for exactly that reason.
- **`azure.yaml` is hand-maintained.** Do not re-run `azd ai agent init` against it — init is not
  idempotent and silently creates `<service>-2`.

---

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not open a public
issue with vulnerability details, tokens, environment dumps, message content, or proof-of-concept
data.

| Control | Implementation |
|---------|----------------|
| No secrets in source | Only `*.env.example` is committed; `.env*` is git-ignored at any depth |
| Keyless service auth | `DefaultAzureCredential` for Foundry **and** the database (`MONGODB-OIDC`) |
| Local-only caller boundary | FastAPI rejects non-loopback peers; Vite binds to `127.0.0.1` |
| Identity never client-supplied | A fixed server-owned `API_POC_USER_ID` scopes every repository operation |
| Scoped persistence | Every repository filter is built by `_scope(user_id, …)`; no query can omit `userId` |
| Agent allow-list | Clients can only reach agents named in `AGENT_NAMES` |
| Deployment fails closed | `API_POC_MODE=true` is refused when recognized Azure compute markers are present |
| Explicit CORS | Origin allow-list only; an empty list never becomes `*` |
| Input bounds | Capped in the Pydantic model, again in the service, hard ceiling 32k chars |
| Retry-safe turns | Durable per-user idempotency state; no transport retry after the dispatch fence |
| Explicit transcript ownership | Research uses a linked Foundry Conversation; triage uses bounded role-preserving input with `store=false` and a Cosmos transcript |
| No transcript duplication | Research content stays in Foundry; Cosmos stores only its user-scoped link, counters, and turn/item references |
| Privacy-preserving delete | User-scoped tombstone fences sends; Foundry state is removed before its link is purged; a short-lived content-free tombstone remains |
| Prompt-injection posture | System prompts treat user content as untrusted data, not instructions |
| PII-safe logging | Lengths, ids and outcomes only; Azure SDK HTTP body logging stays off |
| Error bodies leak nothing | Internal failures are logged; clients get a generic message |
| Non-root container | `appuser` (UID 10001), `nologin` shell, multi-stage build |
| Bounded dependencies | Lower + upper bounds everywhere; `pip-audit --strict` in the gate |
| Static analysis | `ruff` (`S`, `BLE`, `B`, `ASYNC`, `ANN`) + `mypy --strict` with the pydantic plugin |

Public identifiers and secrets are different classes:

| Public configuration or identifier | Secret or sensitive data |
|---|---|
| Tenant/subscription IDs, resource names, ARM IDs, service endpoints, Entra client ID | Tokens, API keys, client secrets, passwords, private keys |
| Agent/model/database/collection names | Credential-bearing connection strings, signed URLs, unredacted chat/log exports |

Examples use placeholders for both classes so the repository does not publish a live environment
map. If a secret is committed, rotate or revoke it before removing it from Git history. Rewrite
history only when secrets or identifiers in prior commits must be removed by owner decision; no
history rewrite is performed by this repository's release tooling.

If a real secret is ever needed, use a Foundry project connection and reference it from
`azure.yaml` as `${{connections.<name>.credentials.<field>}}` — resolved at sandbox start and never
echoed back by the management API. Do not put it in an image or in `azure.yaml`.

---

## Troubleshooting

### Database

| Symptom | Cause / fix |
|---------|-------------|
| `ALLOWED_HOSTS is only valid with OIDC_HUMAN_CALLBACK` | Remove `ALLOWED_HOSTS` from `authMechanismProperties`. It applies only to the interactive browser flow; docs and Node samples showing `['*.azure.com']` describe that flow, not the Python machine callback. |
| Auth fails despite a valid `az login` | Your Entra identity is not registered on the cluster. See [Registering your identity](#registering-your-identity-on-the-mongo-cluster). |
| `mongosh` / Compass won't connect with Entra ID | Expected — neither supports Cosmos vCore's OIDC flow. Use `scripts/init-database.ps1`. |
| `ServerSelectionTimeoutError` | Cluster firewall. Add your client IP to the cluster's networking rules. |

### API

| Symptom | Cause / fix |
|---------|-------------|
| `Configuration error: Required environment variable ... is not set` | Copy `backend\api\.env.example` → `.env` and fill it in. Fail-fast is intentional. |
| `ModuleNotFoundError: No module named 'app'` | You launched uvicorn from the wrong directory or the wrong venv. `cd backend\api` and activate `.\.venv`, or pass `--app-dir backend\api`. |
| `API_POC_MODE is refused on Azure compute` | Working as designed. This unauthenticated POC is local-only; add caller authentication before deployment. |
| `502 The agent is currently unavailable` | The agent isn't deployed, or is named differently. Check the API log for the underlying error and run `azd ai agent show <agent-name> --output json`. |
| Port 8000 already in use | `Get-NetTCPConnection -LocalPort 8000 -State Listen` then stop the owning process. |

### Agent deployment

| Symptom | Cause / fix |
|---------|-------------|
| `ValidationError: Environment variable 'AGENT_*' is reserved for platform use` | `FOUNDRY_*` **and** `AGENT_*` are reserved. Rename to `APP_*`. Note the container path rejects this while the code path does not. |
| `[ImageError] Container registry authentication failed` | The Foundry managed identities lack `AcrPull` on your registry. See [Container agent](#container-agent-needs-an-owner-managed-registry). |
| Agent deploys but never becomes healthy | The image is not `linux/amd64`. Keep `docker.remoteBuild: true`. |
| `session_not_ready` (424) on first invoke | Cold start. Retry after 15–30s. |
| `ModuleNotFoundError` in the sandbox but not locally | Something outside the service directory was imported. See the warning in [Repository layout](#repository-layout). |
| `azd ai agent show` seems to hang | It is waiting on an interactive agent picker. Pass the agent name: `azd ai agent show triage-agent`. |

### Local environment

| Symptom | Cause / fix |
|---------|-------------|
| `bootstrap.ps1` fails building `cryptography` (Rust / OpenSSL) | Windows/ARM64 wheel gap. Check the pin in `constraints-local.txt` still points at a release with a `win_arm64` wheel: `pip download cryptography --only-binary=:all: --no-deps -d $env:TEMP\c` |
| `uv` fails with a TLS `HandshakeFailure` | Known on this network; `bootstrap.ps1` uses `pip` for that reason. |
| mypy reports `Duplicate module named "main"` | Type-check one package at a time, as `scripts/check.ps1` does. |

---

## Project policies

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Release checklist](docs/release-checklist.md)
- [Private release evidence schema](docs/release-evidence.schema.json)

## License and resource lifecycle

No `LICENSE` is present because the owner/legal terms have not been selected. That is an explicit
public-release blocker; do not infer a license from dependencies, examples, or repository visibility.
The release script intentionally fails until an approved `LICENSE` is added.
It also requires a private, out-of-repository evidence file for the exact commit via
`-ReleaseEvidencePath`; that evidence proves model-policy binding, approval, thresholds, and
passing evaluations without publishing live environment identifiers.

This repository does not own the existing Foundry project, model deployment, MongoDB cluster,
registry, network, monitoring resources, or their retention policies. It therefore provides no
automated resource teardown or purge procedure. Environment owners must review dependencies,
retention and shared-resource impact before changing or deleting any Azure resource.
