# Hosted Agents

A three-tier application over Microsoft Foundry hosted agents:

```
frontend/            React + TypeScript (Vite) chat UI
   │  same-origin /api  (dev-server proxy)
   ▼
backend/api/         FastAPI service layer — invokes agents, owns chat history
   │                        │
   │ Responses protocol     │ MONGODB-OIDC (keyless)
   ▼                        ▼
backend/agents/      Azure Cosmos DB for MongoDB (vCore)
   research-agent  (container, Python 3.11)
   triage-agent    (code, python_3_13)
```

Design, decisions and execution plan: **[docs/implementation-plan.md](docs/implementation-plan.md)**.

| Component | Deployment | Runtime |
|-----------|-----------|---------|
| [frontend](frontend) | local dev (`npm run dev`) | Node 22 / React 19 |
| [backend/api](backend/api) | local dev (`uvicorn`) | Python 3.11 |
| [backend/agents/research-agent](backend/agents/research-agent) | **Container** → ACR → Foundry | Python 3.11 |
| [backend/agents/triage-agent](backend/agents/triage-agent) | **Code** (ZIP) → Foundry builds | `python_3_13` |

---

## Two things worth knowing up front

**1. The backend writes to the database, not the agents.** Agents stay stateless. Persistence lives
in one place, so per-user scoping has a single audit point — and the agents don't need database RBAC
on their per-agent Entra identities, which only exist *after* the first deploy and would otherwise
force a deploy → assign-role → redeploy cycle.

**2. Cosmos DB for MongoDB (vCore) is not Cosmos DB for NoSQL.** The cluster
(`*.mongocluster.cosmos.azure.com`) speaks the MongoDB wire protocol, so the persistence layer uses
**PyMongo**, not `azure-cosmos`. Authentication is keyless via `MONGODB-OIDC` with a
`DefaultAzureCredential` token callback — and **without** `ALLOWED_HOSTS`, which PyMongo accepts
only for the interactive `OIDC_HUMAN_CALLBACK` flow.

Python 3.11 is used everywhere except `triage-agent`: Foundry's code-deploy path accepts only
[`python_3_13` / `python_3_14`](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code#supported-runtimes).
The whole repo is pinned to the **3.11 language level** in ruff and mypy so nothing drifts.

---

## Getting started

### Prerequisites

- Python **3.11** and **3.13** (`py -0p`), Node **22+**
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) 2.80+, signed in (`az login`)
- Your Entra identity registered on the Mongo vCore cluster with read/write access
- Docker Desktop — only to run `research-agent` locally

### 1. Bootstrap

```powershell
.\scripts\bootstrap.ps1
```

| Venv | Python | Purpose |
|------|--------|---------|
| `.venv` | 3.11 | Dev tooling + tests (imports the API under test) |
| `backend/api/.venv` | 3.11 | The FastAPI service |
| `backend/agents/triage-agent/.venv` | 3.13 | Matches the code-deploy runtime; `azd ai agent run` resolves the venv from the **service** directory |

### 2. Configure

```powershell
Copy-Item backend\api\.env.example backend\api\.env
Copy-Item frontend\.env.example    frontend\.env.local
```

Both are git-ignored. Fill in the Foundry endpoint and the Mongo connection string.
There are **no passwords** in either file — auth is `DefaultAzureCredential` throughout.

Confirm nothing sensitive is tracked:

```powershell
git check-ignore -q backend\api\.env; $LASTEXITCODE   # 0 = ignored
```

### 3. Create the database

```powershell
.\scripts\init-database.ps1
```

Idempotent. Creates the database, both collections and their indexes by calling the same
`ensure_schema()` the API runs at start-up, so provisioning cannot drift from the app.

### 4. Run

```powershell
# terminal 1 — API on http://127.0.0.1:8000
.\backend\api\.venv\Scripts\Activate.ps1
cd backend\api
uvicorn app.main:app --reload --port 8000
```

```powershell
# terminal 2 — UI on http://localhost:5173
cd frontend
npm run dev
```

Interactive API docs: <http://127.0.0.1:8000/docs>.

### 5. Quality gate

```powershell
.\scripts\check.ps1
```

`ruff check`, `ruff format --check`, `mypy --strict` (once per package — several have a top-level
`main.py`), `pytest`, and `pip-audit --strict` once per requirements file.

```powershell
cd frontend; npm run typecheck; npm run lint
```

---

## API

All routes derive the user from the authenticated principal. **No route accepts a user id from the
client** — that is what makes cross-user access impossible rather than merely discouraged.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health/live` | Liveness (no dependencies) |
| `GET` | `/health/ready` | Readiness (pings the database) |
| `GET` | `/api/agents` | Agents this API may address |
| `POST` | `/api/conversations` | Start a conversation |
| `GET` | `/api/conversations` | List the caller's conversations |
| `GET` | `/api/conversations/{id}` | Fetch one |
| `DELETE` | `/api/conversations/{id}` | Delete it and its messages |
| `GET` | `/api/conversations/{id}/messages` | Full transcript |
| `POST` | `/api/conversations/{id}/messages` | Send a turn, get the reply |

### Authentication

| `AUTH_MODE` | Behaviour |
|-------------|-----------|
| `dev` | Trusts the `x-dev-user-id` header. **Local workstation only** — the app refuses to start in this mode when Azure compute markers (`CONTAINER_APP_NAME`, `WEBSITE_INSTANCE_ID`, …) are present. |
| `entra` | Validates an Entra ID bearer token against the tenant JWKS and uses its `oid` claim. |

`oid` is the partition key because it is immutable and non-reassignable — unlike `sub`
(unique per application) or `email` / `preferred_username` (mutable).

---

## Repository layout

```
azure.yaml                       azd project — both agent services, existing Foundry project
pyproject.toml                   repo-wide tooling (ruff, mypy, pytest)
constraints-local.txt            local-host-only pip pins (Windows/ARM64 wheel gap)
scripts/bootstrap.ps1            create venvs, install dependencies
scripts/check.ps1                lint + types + tests + dependency audit
scripts/init-database.ps1        create database, collections, indexes
backend/api/app/
  main.py                        app factory, lifespan, domain-error → HTTP mapping
  config.py                      env parsing, eager validation, dev-auth guard
  domain/models.py               API contract + document shape in one place
  security/auth.py               Entra token validation → Principal
  repositories/mongo.py          PyMongo + OIDC; every filter is user-scoped
  services/agent_client.py       agent allow-list + Responses invocation
  services/conversation_service.py  the single writer for chat history
  api/routes/                    conversations, agents, health
  cli/init_database.py           schema provisioning entry point
backend/agents/                  the two hosted agents (self-contained)
backend/tests/                   offline unit tests — no Azure, no network
frontend/src/
  api/client.ts                  typed fetch wrapper
  hooks/useChat.ts               chat state, request cancellation
  components/                    ConversationList, MessageList, MessageComposer, ErrorBanner
```

> ⚠️ **Do not add a shared package under `backend/agents/`.** Code deploy zips **only** the service
> directory, and the Docker build context is **only** the service directory. A sibling package
> imports fine locally then fails with `ModuleNotFoundError` in the sandbox. Publish a versioned
> wheel instead.

---

## Security

| Control | Implementation |
|---------|----------------|
| No secrets in source | Only `*.env.example` committed; `.env*` git-ignored at any depth |
| Keyless auth everywhere | `DefaultAzureCredential` for Foundry **and** the database (`MONGODB-OIDC`) |
| Identity is never client-supplied | User comes from a validated token's `oid` claim, never a body or query field |
| Structural per-user isolation | Every repository filter is built by `_scope(user_id, …)`; no query can omit `userId` |
| Indistinguishable 404s | A conversation owned by someone else returns the same 404 as one that doesn't exist |
| Agent allow-list | Clients can only reach agents named in `AGENT_NAMES` |
| Dev-auth fail-closed | `AUTH_MODE=dev` refuses to start when Azure compute markers are present |
| Explicit CORS | Origin allow-list only; an empty list never becomes `*` |
| Input bounds | Length-capped in the model, again in the service, hard ceiling 32k chars |
| Prompt-injection posture | System prompts treat user content as untrusted data, not instructions |
| PII-safe logging | Lengths, ids and outcomes only; Azure SDK HTTP body logging stays off |
| Error bodies leak nothing | Internal failures logged; clients get a generic message |
| Non-root container | `appuser` (UID 10001), `nologin` shell, multi-stage build |
| Bounded dependencies | Lower + upper bounds everywhere; `pip-audit --strict` in the gate |
| Static analysis | `ruff` (`S` bandit, `BLE`, `B`, `ASYNC`, `ANN`) + `mypy --strict` with the pydantic plugin |

---

## Deploying the agents

The API and UI run locally for now. The agents deploy into the **existing** Foundry project:

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID          "<subscription-guid>"
azd env set AZURE_LOCATION                 "<region>"
azd env set AZURE_AI_PROJECT_ENDPOINT      "https://foundry-pocs.services.ai.azure.com/api/projects/foundry-proj-pocs"
azd env set AZURE_AI_PROJECT_ID            "<project-arm-resource-id>"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "<existing-model-deployment-name>"

azd deploy research-agent --no-prompt
azd deploy triage-agent   --no-prompt
azd ai agent show --output json      # expect "status": "active"
```

There is **no `azd provision`** — the project already exists and this repo does not own it.
The deploying identity needs **Foundry Project Manager** at project scope.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `ALLOWED_HOSTS is only valid with OIDC_HUMAN_CALLBACK` | Remove `ALLOWED_HOSTS` from `authMechanismProperties`; it applies only to the interactive flow |
| Mongo auth fails with a valid `az login` | Your Entra identity isn't registered on the cluster. See [Entra ID auth for vCore](https://learn.microsoft.com/azure/documentdb/how-to-connect-role-based-access-control) |
| `mongosh` / Compass can't connect with Entra ID | Expected — neither supports Cosmos vCore's OIDC flow. Use application code |
| API exits with `Configuration error: ...` | A required env var is missing. Copy `.env.example` → `.env` |
| `AUTH_MODE=dev is refused on Azure compute` | Working as intended. Use `AUTH_MODE=entra` |
| Agent deploys but never becomes healthy | Image is not `linux/amd64`. Keep `remoteBuild: true` |
| `session_not_ready` (424) on first invoke | Cold start. Retry after 15–30s |
| `bootstrap.ps1` fails compiling `cryptography` | Windows/ARM64 wheel gap — see `constraints-local.txt` |
