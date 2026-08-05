# Implementation Plan — Two Foundry Hosted Agents (Container + Code)

**Status:** Scaffold complete, not yet provisioned to Azure.
**Owner:** naveenra
**Last reviewed against docs:** 2026-08-05

---

## 1. Objective

Stand up **two Microsoft Foundry hosted agents** in one `azd` project, each exercising a different
deployment path, written in Python against the Foundry SDK / agent-server protocol adapters:

| # | Agent | Deployment path | Runtime | Protocol | Framework |
|---|-------|-----------------|---------|----------|-----------|
| 1 | `research-agent` | **Container** (Dockerfile → ACR → agent version) | Python **3.11** (our base image) | `responses` | none — direct `azure-ai-agentserver-responses` adapter + `azure-ai-projects` |
| 2 | `triage-agent` | **Code** (ZIP upload, platform builds) | `python_3_13` (platform-fixed) | `responses` | Microsoft Agent Framework |

---

## 2. Key constraint discovered during doc review — Python 3.11

> **The Python 3.11 request can only be honoured for the container agent.**

Per [Deploy a hosted agent from source code → Supported runtimes](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code#supported-runtimes),
the `code_configuration.runtime` field accepts **only** `python_3_13` and `python_3_14` for Python.
`azd ai agent init --runtime` enforces the same allow-list and hard-fails on anything else:

```
--runtime must be one of: python_3_13, python_3_14, dotnet_10
```

Container deployments have no such restriction — the runtime is whatever the Dockerfile declares.

### Decision

| Component | Python version | Rationale |
|-----------|----------------|-----------|
| Root dev venv `.venv` | **3.11** | Honours the stated requirement. Runs linting, typing and tests. Tooling only — no agent SDKs (see AD-6). |
| `research-agent` container image | **3.11** (`python:3.11-slim`) | Requirement honoured end-to-end; dev == prod. |
| `triage-agent` deploy runtime | **3.13** | Platform mandate — non-negotiable. |
| `triage-agent` local venv | **3.13** | Must match the deploy runtime, and `azd ai agent run` resolves the venv from the service directory. |

**Mitigation for the split:** all first-party code targets the Python 3.11 language level
(`requires-python = ">=3.11"`, `ruff target-version = py311`, `mypy python_version = 3.11`), so
nothing in `triage-agent` can drift onto 3.12+ syntax. The two agents share no importable code
(see §4), so the version split is contained.

---

## 3. Verified platform contract

Sourced from [Deploy a hosted agent](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent)
and [Hosted agent runtime contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract).

| Item | Value | Consequence for this repo |
|------|-------|---------------------------|
| Container architecture | **must be `linux/amd64`** | This dev box is **ARM64**. `remoteBuild: true` is set in `azure.yaml` so ACR builds the image. Any local build **must** pass `--platform linux/amd64`. |
| Port | `8088` | `EXPOSE 8088`; adapters bind it automatically. |
| Health probe | `GET /readiness` | Provided by the adapter — **do not hand-roll**. |
| Responses endpoint | `POST /responses` | Provided by `ResponsesAgentServerHost` / `ResponsesHostServer`. |
| Injected env vars | `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_PROJECT_ARM_ID`, `FOUNDRY_AGENT_NAME`, `FOUNDRY_AGENT_VERSION`, `FOUNDRY_AGENT_SESSION_ID`, `APPLICATIONINSIGHTS_CONNECTION_STRING` | Read them; **never redeclare** them in `azure.yaml`. `FOUNDRY_*` is a reserved prefix. |
| Agent identity | Platform creates a dedicated Entra service principal per agent at deploy time | Code uses `DefaultAzureCredential` only. **No keys, no connection strings, no user-assigned MI wiring.** |
| Secrets | `${{connections.<name>.credentials.<field>}}` resolved at sandbox start | Any future secret goes through a Foundry project connection, never into the image or `azure.yaml`. |
| Required RBAC | **Foundry Project Manager** at project scope | Deployer prerequisite; documented in the README. |
| Tracing | App Insights connection string injected automatically → OpenTelemetry on by default | Our logging config must not emit PII. |

---

## 4. Architecture decisions

### AD-1 — Each agent is fully self-contained; no shared library package

**Why:** the packaging boundary is the service directory.

- Code deploy zips **only** `services.<name>.project` (`src/triage-agent/`). A sibling
  `src/shared/` package would silently not be in the ZIP → `ModuleNotFoundError` at runtime.
- Container deploy uses the service directory as the Docker build context, with the same effect.

**Consequence:** a small amount of structural repetition (`config.py`, `instructions.py`) is
accepted deliberately. It is not duplication to be refactored away. If shared logic ever becomes
substantial, publish it as a versioned wheel and add it to each agent's `requirements.txt` — that
is the only supported sharing mechanism.

### AD-2 — Layered module layout inside each agent

```
main.py                # composition root ONLY: read config → build → run. No business logic.
<pkg>/config.py        # env parsing + validation, fail-fast, frozen dataclass
<pkg>/instructions.py  # system prompt as data, not string-concat at call sites
<pkg>/handler.py       # protocol handler (container agent)
<pkg>/tools.py         # pure functions exposed as tools (code agent)
<pkg>/telemetry.py     # logging setup, PII-safe
```

Config is parsed **once at import of the composition root** and fails fast with a clear message,
so a misconfigured agent dies at container start rather than on first user request.

### AD-3 — Different framework per agent, deliberately

`research-agent` uses the raw `azure-ai-agentserver-responses` adapter to show the low-level,
framework-agnostic contract. `triage-agent` uses Microsoft Agent Framework
(`agent_framework` + `agent_framework_foundry_hosting`), matching the featured Foundry sample.
This makes the repo a working reference for both styles.

### AD-4 — Remote container build

`docker.remoteBuild: true`. Removes the local-Docker dependency from CI, and sidesteps the
ARM64-host → amd64-target cross-build trap on this machine.

### AD-5 — One `azure.yaml`, one `ai-project`, two agent services

Both agents `uses: [ai-project]` and share a single model deployment. `services.ai-project.deployments[]`
is the **single source of truth** for model deployments — never `azd env set AI_PROJECT_DEPLOYMENTS`
(single-escaped JSON breaks the Bicep `json()` parse) and never
`az cognitiveservices account deployment create` against an azd-managed account.

`azure.yaml` is hand-maintained rather than generated, because `azd ai agent init` is not
idempotent — re-running it against an existing project silently creates `<service>-2` under
`--no-prompt`. It was validated with `azd ai agent doctor`, which reports
*azure.yaml present and parseable*, *agent service in azure.yaml*, and
*agent definition valid (per service)*.

### AD-6 — The root venv holds tooling only

No agent SDK is installed into `.venv`. Three reasons:

- the test suite is offline by design and imports only the pure `config` / `tools` modules;
- `research-agent` runs locally inside its own container, not in a host venv;
- `azure-identity` → `msal` → `cryptography`, which has an intermittent `win_arm64` wheel gap
  on this host (see AD-7).

This also keeps the audit honest: each agent's dependency set is audited on its own.

### AD-7 — Local-only pip constraints, separate from deployed requirements

`constraints-local.txt` pins `cryptography` to the newest release publishing a `win_arm64`
wheel. Newer releases are sdist-only for that platform, so pip falls back to a Rust + OpenSSL
source build that fails without a full toolchain.

It is applied by `scripts/bootstrap.ps1` via `pip install -c` and **never** ships: the container
agent installs its `requirements.txt` inside a `linux/amd64` image, and the code agent's
dependencies are resolved server-side by `remote_build`. Putting the pin in the agents'
`requirements.txt` would have leaked a workstation quirk into production.

---

## 5. Repository layout

```
HostedAgents/
├─ .editorconfig
├─ .gitattributes
├─ .gitignore
├─ AGENTS.md                     # skill marker
├─ README.md
├─ azure.yaml                    # azd project: ai-project + 2 agent services
├─ pyproject.toml                # tooling config (ruff, mypy, pytest) — not a distributable
├─ requirements-dev.txt
├─ constraints-local.txt         # local-host-only pip constraints (AD-7)
├─ docs/
│  └─ implementation-plan.md     # this file
├─ scripts/
│  ├─ bootstrap.ps1              # create both venvs, install deps
│  └─ check.ps1                  # lint + type + test + dependency audit
├─ src/
│  ├─ research-agent/            # ── CONTAINER deploy, Python 3.11 ──
│  │  ├─ .dockerignore
│  │  ├─ .env.example
│  │  ├─ Dockerfile
│  │  ├─ main.py
│  │  ├─ requirements.txt
│  │  └─ research_agent/
│  │     ├─ __init__.py
│  │     ├─ config.py
│  │     ├─ handler.py
│  │     ├─ instructions.py
│  │     └─ telemetry.py
│  └─ triage-agent/              # ── CODE deploy, python_3_13 ──
│     ├─ .agentignore
│     ├─ .env.example
│     ├─ main.py
│     ├─ requirements.txt
│     └─ triage_agent/
│        ├─ __init__.py
│        ├─ config.py
│        ├─ instructions.py
│        └─ tools.py
└─ tests/
   ├─ conftest.py
   ├─ test_research_agent_config.py
   └─ test_triage_agent_tools.py
```

---

## 6. Security controls applied

| # | Control | Where |
|---|---------|-------|
| S-1 | **No secrets in source or image.** Only `.env.example` is committed; `.env` and `.azure/` are git-ignored. | `.gitignore`, `.env.example` |
| S-2 | **Keyless auth.** `DefaultAzureCredential` against the platform-issued agent identity. No API keys, no connection strings. | `config.py`, `main.py` |
| S-3 | **Secrets, if ever needed, via Foundry connections** — `${{connections.<n>.credentials.<f>}}`, resolved at sandbox start, never echoed by the management API. | documented in README |
| S-4 | **Non-root container.** Dedicated `appuser` (UID 10001), no shell, no home writes. | `Dockerfile` |
| S-5 | **Minimal attack surface.** `python:3.11-slim`, `--no-cache-dir`, `PIP_NO_CACHE_DIR`, no build toolchain in the final image. | `Dockerfile` |
| S-6 | **Reproducible dependencies.** Version floors + upper bounds on every direct dependency; `pip-audit` wired into `scripts/check.ps1`. | `requirements*.txt` |
| S-7 | **Fail-fast config validation.** Missing/blank required env vars raise at start-up, not mid-request. Prevents silently running against the wrong project. | `config.py` |
| S-8 | **Input bounds.** User input is length-capped before it reaches the model — blunts prompt-stuffing and runaway token cost. | `handler.py`, `main.py` |
| S-9 | **PII-safe logging.** Structured logs record lengths, IDs and outcomes — never message bodies. Azure SDK HTTP-body logging left off. | `telemetry.py` |
| S-10 | **No `store` of conversation content** by our code (`store: False`); history is managed by the platform. | `handler.py`, `main.py` |
| S-11 | **Static analysis gate.** `ruff` (incl. `S` = flake8-bandit, `ASYNC`, `B`) + `mypy --strict`. | `pyproject.toml` |
| S-12 | **Least privilege by default.** Container CPU/memory capped in `azure.yaml`; agent identity gets only the platform defaults (model inference + session storage). Any external resource requires an explicit, reviewed role assignment. | `azure.yaml` |
| S-13 | **Docker build context minimised** so local `.env`/`.venv` can never enter a layer. | `.dockerignore`, `.agentignore` |

---

## 7. Execution plan

### Phase 0 — Local scaffold *(DONE — no Azure calls, no cost)*

1. Repo structure, `.gitignore`, tooling config.
2. Both agents' source, `Dockerfile`, `requirements.txt`, `.env.example`.
3. `azure.yaml` with `ai-project` + both agent services.
4. venvs: `.venv` (3.11, tooling) and `src/triage-agent/.venv` (3.13, runtime).
5. Quality gate green.

**Verified:**

| Check | Result |
|-------|--------|
| `ruff check` / `ruff format --check` | clean, 14 files |
| `mypy --strict` × 3 targets | no issues (6 + 5 + 3 source files) |
| `pytest` | 29 passed |
| `pip-audit --strict` × 3 requirement sets | no known vulnerabilities |
| `azd ai agent doctor` | *azure.yaml present and parseable*, *agent service in azure.yaml*, *agent definition valid (per service)* — only the azd-environment check fails, which is the Phase 1 gate below |
| Responses adapter API | `CreateResponse`, `ResponseContext`, `ResponsesAgentServerHost`, `ResponsesServerOptions`, `TextResponse`, `@response_handler`, `get_input_text()` confirmed present in the pinned package |
| `git status` | 33 files staged; no `.env`, `.venv`, `.azure/`, `*.log` or `__pycache__` |

> **Note — pytest CVE caught by the gate.** `pip-audit` flagged `PYSEC-2026-1845` in
> pytest 8.4.2; the pin moved to 9.0.3. `pytest-asyncio` was removed rather than held back —
> it capped pytest below 9 and no async tests exist.

### Phase 1 — Azure prerequisites *(BLOCKED — needs a human decision)*

> **Gate:** it was not specified whether to create a **new** Foundry project or target an **existing**
> one. Provisioning creates billable resources, so this scaffold stops here.

```powershell
az login                     # run manually — never automated
azd auth login               # run manually — never automated
az account show --query id -o tsv
```

Confirm **Foundry Project Manager** on the target project scope.

### Phase 2 — Bind the azd project

**Option A — new Foundry project**

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID "<sub-guid>"
azd env set AZURE_LOCATION        "northcentralus"
azd provision --no-prompt
```

**Option B — existing Foundry project**

```powershell
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID       "<sub-guid>"
azd env set AZURE_LOCATION              "<region>"
azd env set AZURE_AI_PROJECT_ENDPOINT   "https://<account>.services.ai.azure.com/api/projects/<project>"
azd env set AZURE_AI_PROJECT_ID         "<project-arm-resource-id>"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "<existing-deployment-name>"
# skip azd provision
```

Post-provision, verify the expected fingerprint:

```powershell
azd env get-values
```

Expect `ENABLE_HOSTED_AGENTS=true`, `ENABLE_CAPABILITY_HOST=false`,
`FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_PROJECT_ID`, `AZURE_AI_MODEL_DEPLOYMENT_NAME`,
and an **empty** `AI_AGENT_PENDING_PROVISION`.

### Phase 3 — Local inner loop

```powershell
# code agent (venv must be the one inside the service dir)
.\src\triage-agent\.venv\Scripts\Activate.ps1
azd ai agent run triage-agent --no-client      # wait for the "Running on http://0.0.0.0:8088" line
azd ai agent invoke --local "Customer says the export button does nothing on Safari."
```

```powershell
# container agent — requires Docker Desktop running locally
azd ai agent run research-agent --no-client
azd ai agent invoke --local "Summarise the tradeoffs of remote vs local container builds."
```

Stop the server before moving on — a lingering process holds file locks.

### Phase 4 — Deploy

```powershell
azd deploy research-agent --no-prompt
azd deploy triage-agent   --no-prompt
azd ai agent show --output json      # expect "status": "active" per agent
```

Each successful deploy mints a **new immutable version**; the latest is active.

### Phase 5 — Smoke test *(billable — one invocation each)*

```powershell
azd ai agent invoke --agent research-agent "hello, are you up?"
azd ai agent invoke --agent triage-agent   "hello, are you up?"
```

### Phase 6 — Evaluation suites

```powershell
azd ai agent eval generate --gen-instruction "<the service description from azure.yaml>" --no-wait --no-prompt
azd ai agent eval run
```

### Phase 7 — Teardown

```powershell
azd down --purge
```

---

## 8. Verification checklist

Phase 0 (done):

- [x] `scripts/check.ps1` green (ruff, format, mypy ×3, pytest, pip-audit ×3)
- [x] `azd ai agent doctor` — azure.yaml parseable, both agent definitions valid
- [x] `git status` shows no `.env`, `.venv`, `.azure/` or `*.log` staged

Phases 1–5 (pending the project decision):

- [ ] `azd env get-values` matches the §7 fingerprint
- [ ] Both agents respond to a local `/responses` invoke before any remote deploy
- [ ] `azd ai agent show --output json` reports `active` for both agents
- [ ] `research-agent` image is `linux/amd64` (`docker manifest inspect` or ACR portal)
- [ ] Eval suites generated and run for both agents

---

## 9. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| ARM64 dev box produces an ARM image | Agent version provisions then never becomes healthy | `remoteBuild: true`; local builds documented with `--platform linux/amd64` |
| 3.11 ↔ 3.13 drift between agents | Code that works locally fails in the `triage-agent` sandbox | Language level pinned to `py311` in ruff + mypy for the whole repo |
| Someone adds `src/shared/` and imports it | `ModuleNotFoundError` only at deploy time, not locally | AD-1 documented here and in `README.md`; publish a wheel instead |
| Re-running `azd ai agent init` | Non-idempotent — silently creates `<service>-2` under `--no-prompt` | `azure.yaml` is hand-maintained; edit it directly and re-run `azd provision` |
| Model deployment name drift between `azure.yaml` and azd env | Agent 404s on first invoke | Single source of truth is `services.ai-project.deployments[]`; agents reference `${AZURE_AI_MODEL_DEPLOYMENT_NAME}` |
| Idle cold start | First invoke after idle returns `session_not_ready` (424) | Retry after 15–30s; CPU/memory floor of `1` / `2Gi` kept for the container agent |
| `cryptography` `win_arm64` wheel gap breaks `bootstrap.ps1` | New contributors on ARM64 hit a Rust + OpenSSL source build | `constraints-local.txt` pins to the newest release with a wheel; re-verify the pin periodically (AD-7) |
| Preview packages (`azure-ai-agentserver-responses`, `agent-framework-foundry-hosting`) change API | Agent fails at start-up after an unplanned upgrade | Both are pinned exactly; upgrades are a deliberate, reviewed change |

---

## 10. References

- [What are hosted agents?](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
- [Hosted agent runtime contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract)
- [Deploy a hosted agent (container)](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent)
- [Deploy a hosted agent from source code](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-code)
- [Hosted agent permissions reference](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Debug a hosted agent](https://learn.microsoft.com/azure/foundry/agents/how-to/debug-hosted-agent)
- [Add a protocol adapter](https://learn.microsoft.com/azure/foundry/agents/how-to/add-protocol-adapter)
- [Host Agent Framework agents as Foundry hosted agents](https://learn.microsoft.com/agent-framework/hosting/foundry-hosted-agent?pivots=programming-language-python)
