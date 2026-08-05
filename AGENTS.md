# Agent instructions for this repository

This project was built with the **microsoft-foundry** skill. Before working on or answering
questions about Foundry agents in this repository, read the microsoft-foundry skill first.

## What this repository is

A three-part application:

- `backend/agents/` — two Microsoft Foundry **hosted agents**, deployed into an **existing**
  Foundry project (`foundry-proj-pocs`):
  - `research-agent` — **container** deployment, Python **3.11**, `responses` protocol,
    built on the `azure-ai-agentserver-responses` adapter.
  - `triage-agent` — **code** deployment, runtime **`python_3_13`**, `responses` protocol,
    built on Microsoft Agent Framework.
- `backend/api/` — a **FastAPI** service layer. It invokes the deployed agents and is the
  **only** component that reads or writes chat history.
- `frontend/` — a **React + TypeScript (Vite)** chat UI that talks only to the API.

Design rationale and the execution plan live in `docs/implementation-plan.md`.
Read it before making structural changes.

## Rules to respect when changing this code

### Persistence

1. **The backend owns persistence. Agents never touch the database.** Agents stay stateless.
   This keeps one auditable place for per-user scoping, and avoids needing database RBAC on the
   per-agent Entra identities (which only exist *after* the first deploy).
2. **The database is Azure Cosmos DB for MongoDB (vCore)** — `*.mongocluster.cosmos.azure.com`.
   It speaks the MongoDB wire protocol. Use **PyMongo**, never `azure-cosmos` (that SDK only
   talks to Cosmos DB for NoSQL and cannot connect to this cluster).
3. **Every query must be user-scoped.** Build filters with `_scope(user_id, ...)` in
   `backend/api/app/repositories/mongo.py`. Never issue a `find`/`delete` without `userId`.
4. **Do not pass `ALLOWED_HOSTS` in the OIDC auth properties.** PyMongo permits it only
   alongside `OIDC_HUMAN_CALLBACK`; with the machine `OIDC_CALLBACK` this repo uses, it raises
   `ConfigurationError: ALLOWED_HOSTS is only valid with OIDC_HUMAN_CALLBACK`. Docs and samples
   showing `ALLOWED_HOSTS: ['*.azure.com']` describe the Node/Compass browser flows.
5. **Keyless only.** `DefaultAzureCredential` + `MONGODB-OIDC`, token scope
   `https://ossrdbms-aad.database.windows.net/.default`. Never add an account key or password.

### Foundry agents

6. **Do not add a shared package under `backend/agents/`.** Code deploy zips only the service
   directory, and the Docker build context is only the service directory. A cross-agent import
   works locally and fails in the sandbox. Ship a versioned wheel instead.
7. **`python_3_11` is not a valid code-deploy runtime.** Only `python_3_13` and `python_3_14`
   are accepted. Do not "fix" `triage-agent` to 3.11.
8. **`azure.yaml` is hand-maintained** and targets an **existing** Foundry project — there is
   deliberately no `ai-project` service and no `azd provision`. Do not re-run
   `azd ai agent init` against it; init is not idempotent and silently creates `<service>-2`.
9. **`FOUNDRY_*` and `AGENT_*` are both reserved environment-variable prefixes.** Never declare
   either in `azure.yaml`. The container path rejects the whole deployment with
   `ValidationError: ... is reserved for platform use ... per container-image-spec`.
   This repo's agent settings use the `APP_*` prefix for exactly that reason.
10. **Container images must be `linux/amd64`.** Keep `docker.remoteBuild: true`. Local builds on
    this ARM64 host require `--platform linux/amd64`.

### General

11. **No secrets in source.** `.env` files are git-ignored; only `*.env.example` is committed.
    Verify with `git check-ignore -q <path>` before committing.
12. **Never run `az login` or `azd auth login` on the user's behalf.** Stop and ask.
13. **Keep the 3.11 language level** across the whole repo (`ruff` and `mypy` are pinned to `py311`).
14. **Run `scripts/check.ps1`** before declaring any change complete.
