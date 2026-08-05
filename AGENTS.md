# Agent instructions for this repository

This project was built with the **microsoft-foundry** skill. Before working on or answering
questions about Foundry agents in this repository, read the microsoft-foundry skill first.

## What this repository is

Two Microsoft Foundry hosted agents in one `azd` project:

- `src/research-agent` — **container** deployment, Python **3.11**, `responses` protocol,
  built directly on the `azure-ai-agentserver-responses` adapter.
- `src/triage-agent` — **code** deployment, runtime **`python_3_13`**, `responses` protocol,
  built on Microsoft Agent Framework.

Design rationale, decisions and the execution plan live in `docs/implementation-plan.md`.
Read it before making structural changes.

## Rules to respect when changing this code

1. **Do not add a shared package under `src/`.** Code deploy zips only the service directory,
   and the Docker build context is only the service directory. Cross-agent imports fail at
   runtime while working locally. Ship a versioned wheel instead.
2. **`python_3_11` is not a valid code-deploy runtime.** Only `python_3_13` and `python_3_14`
   are accepted. Do not "fix" `triage-agent` to 3.11.
3. **`azure.yaml` is hand-maintained.** Do not re-run `azd ai agent init` against it — init is
   not idempotent and silently creates `<service>-2` under `--no-prompt`.
4. **`services.ai-project.deployments[]` is the single source of truth** for model deployments.
   Never run `azd env set AI_PROJECT_DEPLOYMENTS`, and never
   `az cognitiveservices account deployment create` against this azd-managed account.
5. **Never declare `FOUNDRY_*` environment variables** in `azure.yaml` — the platform injects them.
6. **No secrets in source, `azure.yaml`, or images.** Use a Foundry project connection and the
   `${{connections.<name>.credentials.<field>}}` placeholder.
7. **Container images must be `linux/amd64`.** Keep `docker.remoteBuild: true`. Local builds on
   this ARM64 host require `--platform linux/amd64`.
8. **Never run `az login` or `azd auth login` on the user's behalf.** Stop and ask.
9. **Keep the 3.11 language level** across the whole repo (`ruff`/`mypy` are pinned to `py311`).
10. **Run `scripts/check.ps1`** before declaring any change complete.
