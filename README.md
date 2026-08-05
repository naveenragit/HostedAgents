# Hosted Agents

Two Microsoft Foundry **hosted agents** in a single `azd` project, each demonstrating a
different deployment path.

| Agent | Deployment | Runtime | Protocol | Framework |
|-------|-----------|---------|----------|-----------|
| [`research-agent`](src/research-agent) | **Container** — Dockerfile → ACR → agent version | Python **3.11** | `responses` | none — `azure-ai-agentserver-responses` adapter |
| [`triage-agent`](src/triage-agent) | **Code** — source ZIP, Foundry builds it | `python_3_13` | `responses` | Microsoft Agent Framework |

Full design, rationale and step-by-step execution plan: **[docs/implementation-plan.md](docs/implementation-plan.md)**.

---

## Why two Python versions?

Foundry's code-deploy path accepts only `python_3_13` / `python_3_14`
([docs](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code#supported-runtimes)).
Python 3.11 is available only on the container path, where the Dockerfile owns the runtime.

The whole repository is written to the **3.11 language level** (`ruff target-version = py311`,
`mypy python_version = 3.11`) so no code can drift onto newer syntax.

---

## Getting started

### Prerequisites

- Python **3.11** and **3.13** (`py -0p` to verify)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) 2.80+
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) with the `azure.ai.agents` extension
- **Foundry Project Manager** role on the target Foundry project
- Docker Desktop — only for running `research-agent` locally. Deployment does not need it:
  `remoteBuild: true` builds the image in ACR.

### 1. Bootstrap

```powershell
.\scripts\bootstrap.ps1
```

Creates two virtual environments:

| Path | Python | Contents | Purpose |
|------|--------|----------|---------|
| `.venv` | 3.11 | tooling only | ruff, mypy, pytest, pip-audit |
| `src/triage-agent/.venv` | 3.13 | `src/triage-agent/requirements.txt` | Matches the code-deploy runtime; `azd ai agent run` resolves the venv from the **service** directory |

The agents' runtime SDKs are deliberately absent from `.venv`: the test suite is offline and
imports only the pure config/tools modules, and `research-agent` runs inside its container.

`constraints-local.txt` is applied during bootstrap only. It pins `cryptography` to the newest
release that ships a `win_arm64` wheel, so this repo bootstraps on Windows/ARM64 without a Rust +
OpenSSL source build. It never reaches Foundry.

### 2. Quality gate

```powershell
.\scripts\check.ps1
```

Runs `ruff check`, `ruff format --check`, `mypy --strict` (once per agent — both have a top-level
`main.py`), `pytest`, and `pip-audit --strict` (once per requirements file — the agents are
independent deployment units with intentionally different adapter pins).

### 3. Sign in

```powershell
az login
azd auth login
```

> Run these yourself — they are interactive browser flows and are never automated by tooling here.

### 4. Bind to a Foundry project

**New project:**

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID "<subscription-guid>"
azd env set AZURE_LOCATION "northcentralus"
azd provision --no-prompt
```

**Existing project:**

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID "<subscription-guid>"
azd env set AZURE_LOCATION "<region>"
azd env set AZURE_AI_PROJECT_ENDPOINT "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set AZURE_AI_PROJECT_ID "<project-arm-resource-id>"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "<existing-deployment-name>"
# skip azd provision
```

### 5. Run locally

```powershell
# code agent — activate the venv inside its service directory first
.\src\triage-agent\.venv\Scripts\Activate.ps1
azd ai agent run triage-agent --no-client
# wait for "Running on http://0.0.0.0:8088", then in another shell:
azd ai agent invoke --local "The export button does nothing in Safari for several customers."
```

```powershell
# container agent — needs Docker Desktop running
azd ai agent run research-agent --no-client
azd ai agent invoke --local "What are the tradeoffs of remote vs local container builds?"
```

Both agents serve `POST /responses` and `GET /readiness` on **port 8088**.

### 6. Deploy

```powershell
azd deploy research-agent --no-prompt
azd deploy triage-agent   --no-prompt
azd ai agent show --output json     # expect "status": "active"
```

Each deploy creates a new **immutable version**; the latest is active.

### 7. Tear down

```powershell
azd down --purge
```

---

## Repository layout

```
azure.yaml                     azd project: ai-project + both agent services
pyproject.toml                 repo-wide tooling config (ruff, mypy, pytest)
requirements-dev.txt           root venv tooling
constraints-local.txt          local-host-only pip constraints (Windows/ARM64 wheel gap)
docs/implementation-plan.md    design, decisions, execution plan, risks
scripts/bootstrap.ps1          create venvs, install dependencies
scripts/check.ps1              lint + types + tests + dependency audit
src/research-agent/            container agent (Dockerfile, .dockerignore)
src/triage-agent/              code agent (.agentignore)
tests/                         offline unit tests — no Azure calls
```

Each agent follows the same internal shape:

```
main.py                composition root only — read config, build, run
<pkg>/config.py        env parsing + eager validation, frozen dataclass
<pkg>/instructions.py  system prompt as reviewable data
<pkg>/handler.py       protocol handler          (research-agent)
<pkg>/tools.py         pure functions as tools   (triage-agent)
<pkg>/telemetry.py     PII-safe logging setup
```

> ⚠️ **Do not add a shared `src/common/` package.**
> Code deploy zips **only** the service directory, and the Docker build context is
> **only** the service directory. A sibling package would import fine locally and then
> fail with `ModuleNotFoundError` in the sandbox. If shared logic becomes substantial,
> publish it as a versioned wheel and add it to each agent's `requirements.txt`.
> See [AD-1](docs/implementation-plan.md#ad-1--each-agent-is-fully-self-contained-no-shared-library-package).

---

## Security

| Control | Implementation |
|---------|----------------|
| No secrets in source or images | Only `.env.example` is committed; `.env`, `.azure/` are git-ignored and excluded from both the Docker context and the deploy ZIP |
| Keyless authentication | `DefaultAzureCredential` against the per-agent Entra identity Foundry provisions at deploy time — no API keys, no connection strings |
| Secrets, when unavoidable | Foundry project connections, referenced as `${{connections.<name>.credentials.<field>}}` and resolved at sandbox start |
| Non-root container | Dedicated `appuser` (UID 10001), `nologin` shell |
| Minimal image | `python:3.11-slim`, multi-stage build, no compiler toolchain or pip cache in the final layer |
| Bounded dependencies | Lower **and** upper bounds on every direct dependency; `pip-audit --strict` in the quality gate |
| Fail-fast configuration | Missing or malformed env vars raise at start-up, not mid-request |
| Input bounds | User input length-capped before reaching the model (`AGENT_MAX_INPUT_CHARS`, hard ceiling 32k chars) |
| Prompt-injection posture | System instructions explicitly treat user-supplied content as untrusted data, not commands |
| PII-safe logging | Logs record lengths, IDs and outcomes only; Azure SDK HTTP body logging stays off |
| No conversation persistence by us | `store: False` — history is platform-managed |
| Static analysis | `ruff` with `S` (bandit), `B`, `ASYNC`, `ANN`, plus `mypy --strict` |

Never commit a `.env` file. Verify with `git status` before every push.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Agent deploys but never becomes healthy | Image is not `linux/amd64`. Keep `remoteBuild: true`; if building locally, use `docker build --platform linux/amd64 .` |
| `session_not_ready` (424) on first invoke | Cold start. Retry after 15–30s. |
| `could not connect` on `invoke --local` | The local server was not ready. Wait for the `Running on http://0.0.0.0:8088` line — never start it with `&`/`nohup`. |
| `ModuleNotFoundError` in the sandbox only | Something outside the service directory was imported. See AD-1 above. |
| Model 404 on first invoke | `AZURE_AI_MODEL_DEPLOYMENT_NAME` in the azd env disagrees with `azure.yaml`. The `deployments[]` block is the source of truth. |
| `invalid character 'n' after object key:value pair` on provision | Someone ran `azd env set AI_PROJECT_DEPLOYMENTS`. Clear it and declare deployments in `azure.yaml` instead. |
| `bootstrap.ps1` fails compiling `cryptography` (Rust/OpenSSL) | Windows/ARM64 wheel gap. Verify the pin in `constraints-local.txt` still points at a release with a `win_arm64` wheel: `pip download cryptography --only-binary=:all: --no-deps -d %TEMP%\cryptotest` |

More: [Debug a hosted agent](https://learn.microsoft.com/azure/foundry/agents/how-to/debug-hosted-agent).
