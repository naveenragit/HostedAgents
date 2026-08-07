# Contributing

This repository is an unreleased sample that targets existing Microsoft Foundry and Azure resources.
Contributions should remain usable without Azure access unless they explicitly change deployment or
live integration behavior.

## Clean Setup

Use a fresh clone. Do not copy a configured working directory because ignored `.env`, `.azure/`,
virtual-environment, or build files can silently follow it.

Prerequisites:

- PowerShell 7;
- Git;
- Python 3.11 and 3.13 available through the Windows Python launcher;
- Node.js 22 or another version satisfying `frontend/package.json`;
- Azure CLI, Azure Developer CLI, and Docker only for explicitly approved live-agent work.

From the repository root:

```powershell
.\scripts\bootstrap.ps1
.\scripts\check.ps1
```

Bootstrap creates the separate Python environments required by the API and code-deployed agent,
then runs `npm ci` from the committed frontend lock file. The check script runs Python linting,
formatting, type checks, tests and audits, followed by frontend tests when configured, type checking,
linting, build, and npm audits.

Offline checks do not require `az login`, `azd auth login`, a database, or a model invocation. Copy
the `.env.example` files only when a task needs the local running application. Never commit the
resulting `.env` files or `.azure/` state.

## Change Guidelines

- Preserve both conversation strategies: research history is Foundry-managed with only a
  user-scoped link and turn references in Cosmos; triage history is Cosmos-managed and sent with
  `store=false`. The API coordinates both, and hosted agents never receive database credentials.
- Scope every MongoDB operation to the authenticated `userId` through `_scope()`.
- Preserve the existing-project boundary. Do not add automatic Azure provisioning or teardown.
- Do not add a shared source package under `backend/agents/`; package shared code as a versioned
  wheel when that complexity is justified.
- Keep first-party Python syntax compatible with Python 3.11. The code deployment runtime remains
  `python_3_13` because that is a platform constraint.
- Use placeholders in examples. Public Azure identifiers are not credentials, but live values still
  map an environment and should not be committed.
- Do not introduce legal text or create a `LICENSE` without owner/legal approval.
- Add focused tests for behavior changes. Changes to turn handling must cover retries, role/order
  preservation, user isolation, and failure recovery as applicable.

## Pull Requests

Before opening a pull request:

1. Rebase or merge the current default branch and resolve conflicts deliberately.
2. Run `.\scripts\check.ps1`.
3. Run relevant smoke or evaluation checks only against an approved non-production environment.
4. Review the diff and staged file list for secrets, live identifiers, generated files, and local
   configuration.
5. Update documentation and the release checklist when behavior, security controls, dependencies,
   or operational assumptions change.

Describe the user-visible behavior, validation performed, unresolved risks, and any owner action
needed. Pull-request CI performs no Azure login, deployment, provisioning, or live model invocation.

Report security concerns through [SECURITY.md](SECURITY.md), not through a pull request or public
issue. Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).