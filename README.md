# Hosted Agents

A three-tier chat application over **Microsoft Foundry hosted agents**, with per-user conversation
history in **Azure Cosmos DB for MongoDB (vCore)**.

```
frontend/                 React 19 + TypeScript (Vite) chat UI
    │  same-origin /api   (Vite dev-server proxy)
    ▼
backend/api/              FastAPI service layer
    │                     · invokes the agents
    │                     · the ONLY writer of chat history
    │                          │
    │ Responses protocol       │ MONGODB-OIDC (keyless)
    ▼                          ▼
backend/agents/           Azure Cosmos DB for MongoDB (vCore)
    research-agent   container deploy, Python 3.11
    triage-agent     code deploy,      python_3_13
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

Both agents are **deployed and live** in an existing Foundry project. The API and UI are **local
only** — there is no hosting for them yet.

| Thing | State |
|-------|-------|
| `research-agent`, `triage-agent` | Deployed to Foundry project `foundry-proj-pocs`, status `active` |
| Cosmos database `agentchat` | Created, with `conversations` + `messages` collections and indexes |
| FastAPI service | Runs locally on `http://127.0.0.1:8000` |
| React UI | Runs locally on `http://localhost:5173` |
| Quality gate | Green — ruff, `mypy --strict` ×4, 67 tests, `pip-audit` ×4 |

---

## Three things worth knowing before you start

**1. The backend writes to the database — the agents never do.**
Agents are stateless. Persistence lives in one place, so per-user scoping has a single audit point.
It also sidesteps a real trap: a hosted agent's Entra identity does not exist until *after* its
first deploy, so agent-side database access would force a deploy → assign-role → redeploy cycle.

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
| Node.js | **22+** | For the frontend. |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | 2.80+ | `az login` before anything else. |
| [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) | 1.28+ | Only needed to deploy agents. Requires the `azure.ai.agents` extension: `azd extension install azure.ai.agents` |
| Docker Desktop | any | **Only** to run `research-agent` locally. Not needed to deploy — images build remotely in ACR. |

### Azure resources

You need access to an existing Foundry project and a Mongo vCore cluster. This repo does **not**
create them (there is deliberately no `azd provision`).

| Resource | Why | Required access |
|----------|-----|-----------------|
| Foundry project | Hosts the agents, serves the model | **Foundry Project Manager** at project scope |
| Model deployment | The agents' LLM | Must already exist in the project |
| Cosmos DB for MongoDB (vCore) cluster | Chat history | Your Entra identity registered on the cluster (see below) |
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

## Setup

### 1. Bootstrap the environments

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

### 2. Configure

```powershell
Copy-Item backend\api\.env.example backend\api\.env
Copy-Item frontend\.env.example    frontend\.env.local
```

Edit `backend\api\.env`:

| Variable | Example | Notes |
|----------|---------|-------|
| `FOUNDRY_PROJECT_ENDPOINT` | `https://<account>.services.ai.azure.com/api/projects/<project>` | Required |
| `AGENT_NAMES` | `research-agent,triage-agent` | Allow-list. A client can never reach an agent that is not listed. |
| `MONGO_CONNECTION_STRING` | `mongodb+srv://<cluster>.mongocluster.cosmos.azure.com/?tls=true&authMechanism=MONGODB-OIDC&retrywrites=false&maxIdleTimeMS=120000` | Required. Contains **no password**. |
| `MONGO_DATABASE` | `agentchat` | Created for you in step 3 |
| `AUTH_MODE` | `dev` | `dev` trusts the `x-dev-user-id` header — local only. Use `entra` for real tokens. |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173` | Explicit allow-list; never becomes `*` |
| `API_LOG_LEVEL` / `API_MAX_INPUT_CHARS` | `INFO` / `8000` | Optional |

Both files are git-ignored. There are **no passwords in either** — authentication is
`DefaultAzureCredential` throughout. Confirm nothing sensitive is tracked:

```powershell
git check-ignore -q backend\api\.env; $LASTEXITCODE    # 0 = ignored, good
```

### 3. Create the database

```powershell
.\scripts\init-database.ps1
```

Idempotent. Creates the database, both collections and their indexes by calling the same
`ensure_schema()` the API runs at start-up, so provisioning cannot drift from the application.
This is also the fastest way to prove your Entra identity works against the cluster.

Expected output:

```
Connecting with your az login identity (MONGODB-OIDC)...
INFO     api.init connected
INFO     api.init database ready
Database 'agentchat' is ready. Collections: conversations, messages
```

### 4. Run it

```powershell
# Terminal 1 — API on http://127.0.0.1:8000
cd backend\api
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

```powershell
# Terminal 2 — UI on http://localhost:5173
cd frontend
npm run dev
```

Open <http://localhost:5173>. Interactive API docs: <http://127.0.0.1:8000/docs>.

The Vite dev server proxies `/api` and `/health` to the API, so the browser stays on one origin and
CORS behaves the same as it would behind a reverse proxy.

### 5. Verify end to end

```powershell
.\scripts\smoke-test.ps1                            # triage-agent
.\scripts\smoke-test.ps1 -AgentName research-agent  # container agent
```

Checks health, lists agents, creates a conversation, sends a real turn to the deployed agent, reads
the transcript back from Cosmos, and **asserts per-user isolation** (a second user must see nothing
and get a 404 on a direct fetch). This calls the live model, so it costs tokens.

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
| `pytest` | 67 offline tests. No Azure, no network. |
| `pip-audit --strict` × 4 | Once per requirements file — the agents pin different preview adapter versions and would false-conflict if resolved together |

Frontend:

```powershell
cd frontend
npm run typecheck
npm run lint
npm run build
```

### Repository layout

```
azure.yaml                          azd project — both agent services, existing Foundry project
pyproject.toml                      repo-wide tooling config (ruff, mypy, pytest)
requirements-dev.txt                root venv tooling
constraints-local.txt               local-host-only pip pins (Windows/ARM64 wheel gap)

scripts/bootstrap.ps1               create the three venvs, install dependencies
scripts/check.ps1                   lint + types + tests + dependency audit
scripts/init-database.ps1           create database, collections, indexes
scripts/smoke-test.ps1              end-to-end test incl. per-user isolation

backend/api/
  .env.example                      template — copy to .env
  requirements.txt
  app/main.py                       app factory, lifespan, domain-error → HTTP mapping
  app/config.py                     env parsing, eager validation, dev-auth guard
  app/dependencies.py               DI wiring for shared clients
  app/domain/models.py              API contract + document shape in one place
  app/security/auth.py              Entra token validation → Principal
  app/repositories/mongo.py         PyMongo + OIDC; every filter is user-scoped
  app/services/agent_client.py      agent allow-list + Responses invocation
  app/services/conversation_service.py   the single writer of chat history
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

Every route derives the user from the authenticated principal. **No route accepts a user id from the
client** — that is what makes cross-user access structurally impossible rather than merely
discouraged.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health/live` | Liveness. Touches no dependencies. |
| `GET` | `/health/ready` | Readiness. Pings the database. |
| `GET` | `/api/agents` | Agents this API is allowed to address |
| `POST` | `/api/conversations` | Start a conversation — body `{"agentName": "..."}` |
| `GET` | `/api/conversations` | List the caller's conversations |
| `GET` | `/api/conversations/{id}` | Fetch one |
| `DELETE` | `/api/conversations/{id}` | Delete it and all of its messages |
| `GET` | `/api/conversations/{id}/messages` | Full transcript, oldest first |
| `POST` | `/api/conversations/{id}/messages` | Send a turn — body `{"content": "..."}` |

Example with dev auth:

```powershell
$h = @{ 'x-dev-user-id' = 'alice'; 'Content-Type' = 'application/json' }
$c = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/conversations' -Method POST -Headers $h `
       -Body '{"agentName":"triage-agent"}'
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/conversations/$($c.id)/messages" -Method POST `
       -Headers $h -Body '{"content":"The export button does nothing in Safari."}'
```

### Authentication modes

| `AUTH_MODE` | Behaviour |
|-------------|-----------|
| `dev` | Trusts the `x-dev-user-id` header. **Local workstation only** — the app refuses to start when Azure compute markers (`CONTAINER_APP_NAME`, `WEBSITE_INSTANCE_ID`, `WEBSITE_SITE_NAME`, `KUBERNETES_SERVICE_HOST`) are present. |
| `entra` | Validates an Entra ID bearer token against the tenant JWKS and uses its `oid` claim. Requires `AZURE_TENANT_ID` and `API_AUDIENCE`. |

`oid` is the partition key because it is immutable and non-reassignable — unlike `sub` (unique only
per application) or `email` / `preferred_username` (both mutable).

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

### Code agent (no extra infrastructure)

```powershell
azd deploy triage-agent --no-prompt
```

### Container agent (needs a registry)

`azd` only wires registry RBAC for registries it provisions itself. Against an existing project you
must supply one and grant pull rights, or the deploy fails with
`[ImageError] Container registry authentication failed`.

```powershell
# 1. A registry (skip if you already have one)
az acr create --name <acrname> --resource-group <rg> --sku Basic --location <region> --admin-enabled false

# 2. Grant AcrPull to BOTH the Foundry account and project managed identities
$acrId = az acr show --name <acrname> --query id -o tsv
$account = az cognitiveservices account show -n <account> -g <rg> --query identity.principalId -o tsv
$project = az resource show --ids "<project-arm-resource-id>" --api-version 2025-06-01 --query identity.principalId -o tsv

foreach ($p in @($account, $project)) {
  az role assignment create --assignee-object-id $p --assignee-principal-type ServicePrincipal `
     --role AcrPull --scope $acrId
}

# 3. Point azd at it, then deploy
azd env set AZURE_CONTAINER_REGISTRY_ENDPOINT "<acrname>.azurecr.io"
azd env set AZURE_CONTAINER_REGISTRY_NAME     "<acrname>"
azd deploy research-agent --no-prompt
```

### Verify

```powershell
azd ai agent show --output json     # expect "status": "active"
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

| Control | Implementation |
|---------|----------------|
| No secrets in source | Only `*.env.example` is committed; `.env*` is git-ignored at any depth |
| Keyless auth everywhere | `DefaultAzureCredential` for Foundry **and** the database (`MONGODB-OIDC`) |
| Identity never client-supplied | Derived from a validated token's `oid` claim, never a body or query field |
| Structural per-user isolation | Every repository filter is built by `_scope(user_id, …)`; no query can omit `userId` |
| Indistinguishable 404s | Another user's conversation returns the same 404 as one that doesn't exist |
| Agent allow-list | Clients can only reach agents named in `AGENT_NAMES` |
| Dev-auth fails closed | `AUTH_MODE=dev` refuses to start when Azure compute markers are present |
| Explicit CORS | Origin allow-list only; an empty list never becomes `*` |
| Input bounds | Capped in the Pydantic model, again in the service, hard ceiling 32k chars |
| Prompt-injection posture | System prompts treat user content as untrusted data, not instructions |
| PII-safe logging | Lengths, ids and outcomes only; Azure SDK HTTP body logging stays off |
| Error bodies leak nothing | Internal failures are logged; clients get a generic message |
| Non-root container | `appuser` (UID 10001), `nologin` shell, multi-stage build |
| Bounded dependencies | Lower + upper bounds everywhere; `pip-audit --strict` in the gate |
| Static analysis | `ruff` (`S`, `BLE`, `B`, `ASYNC`, `ANN`) + `mypy --strict` with the pydantic plugin |

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
| `AUTH_MODE=dev is refused on Azure compute` | Working as designed. Use `AUTH_MODE=entra`. |
| `502 The agent is currently unavailable` | The agent isn't deployed, or is named differently. Check the API log for the underlying error and run `azd ai agent show`. |
| Port 8000 already in use | `Get-NetTCPConnection -LocalPort 8000 -State Listen` then stop the owning process. |

### Agent deployment

| Symptom | Cause / fix |
|---------|-------------|
| `ValidationError: Environment variable 'AGENT_*' is reserved for platform use` | `FOUNDRY_*` **and** `AGENT_*` are reserved. Rename to `APP_*`. Note the container path rejects this while the code path does not. |
| `[ImageError] Container registry authentication failed` | The Foundry managed identities lack `AcrPull` on your registry. See [Container agent](#container-agent-needs-a-registry). |
| Agent deploys but never becomes healthy | The image is not `linux/amd64`. Keep `docker.remoteBuild: true`. |
| `session_not_ready` (424) on first invoke | Cold start. Retry after 15–30s. |
| `ModuleNotFoundError` in the sandbox but not locally | Something outside the service directory was imported. See the warning in [Repository layout](#repository-layout). |

### Local environment

| Symptom | Cause / fix |
|---------|-------------|
| `bootstrap.ps1` fails building `cryptography` (Rust / OpenSSL) | Windows/ARM64 wheel gap. Check the pin in `constraints-local.txt` still points at a release with a `win_arm64` wheel: `pip download cryptography --only-binary=:all: --no-deps -d $env:TEMP\c` |
| `uv` fails with a TLS `HandshakeFailure` | Known on this network; `bootstrap.ps1` uses `pip` for that reason. |
| mypy reports `Duplicate module named "main"` | Type-check one package at a time, as `scripts/check.ps1` does. |

---

## Tearing down

The agents and any resources you created live in your Azure subscription:

```powershell
# Remove a deployed agent version (leaves the Foundry project intact)
azd ai agent show --output json          # inspect first

# Registry created solely for this project
az acr delete --name <acrname> --resource-group <rg>

# Chat history
# Drop the `agentchat` database from the Mongo cluster.
```

`azd down` is **not** appropriate here — this repo does not own the Foundry project and would
attempt to remove resources it did not create.
