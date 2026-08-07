# Public Release Checklist

Complete this checklist for the exact commit and artifact proposed for publication. Store approval
evidence in the organization's release system, not in source when it contains live identifiers or
sensitive details.

## Legal and Governance

- [ ] Owner/legal has selected and approved the repository license.
- [ ] The approved `LICENSE` file is present, tracked, and matches the approval exactly.
- [ ] Legal has decided whether a `NOTICE` file or third-party attribution bundle is required.
- [ ] Private vulnerability, client-support, and conduct-reporting destinations are configured and
      all owner-configuration placeholders are removed.
- [ ] Supported versions and response expectations in `SECURITY.md` are accurate.
- [ ] Contribution and Code of Conduct governance have named responsible teams in the private
      release record.

## Source and History Sanitization

- [ ] Full Git history, all tags, and submodules (if any) pass Gitleaks.
- [ ] GitHub secret scanning alerts are resolved by revoking/rotating affected credentials, not only
      by deleting text.
- [ ] Current files and history have been searched for tenant/subscription IDs, ARM IDs, live
      endpoints, resource/project/model/registry/cluster names, people names, emails, and internal
      URLs.
- [ ] Public identifiers are distinguished from secrets, and live values are retained only when an
      owner has approved their publication as necessary.
- [ ] `.env`, `.azure/`, credentials, tokens, local logs, chat exports, virtual environments,
      `node_modules`, and generated build output are untracked and absent from the artifact.
- [ ] History is rewritten only if secrets or identifiers in prior commits must be removed by an
      explicit owner/security decision. Rotation, branch freeze, fork coordination, cache/release
      invalidation, and reclone instructions are complete before any force-push.

## Clean Build and Artifact

- [ ] A new clean clone of the candidate commit is used; no configured working directory is copied.
- [ ] `.\scripts\bootstrap.ps1` completes with the documented Python and Node versions.
- [ ] `.\scripts\check.ps1` passes without skipped required checks.
- [ ] PowerShell scripts parse under the supported PowerShell version.
- [ ] Documentation links and repository paths resolve.
- [ ] `scripts/public-release.ps1` builds the artifact from `git archive`/tracked files only.
- [ ] The artifact manifest contains no ignored or forbidden local paths and matches the candidate
      commit.
- [ ] The artifact is independently rescanned for secrets and malware, then hashed and stored with
      release evidence.

## GitHub Repository Controls

- [ ] GitHub secret scanning is enabled for the repository and organization.
- [ ] Push protection is enabled and bypass permissions/alerts have an owner.
- [ ] Branch protection or rulesets require pull requests, review, current required CI checks, and
      restrict force pushes/deletions.
- [ ] Workflow token permissions use least privilege; pull-request CI has no Azure credentials,
      federated identity, login, deployment, or environment approval bypass.
- [ ] CI installs the pinned open-source Gitleaks CLI only after verifying its published checksum;
      no workflow secret or organization-scoped action license is required for the scan.
- [ ] Dependabot, dependency graph, and security updates are enabled and routed to an owning team.
- [ ] Private vulnerability reporting is enabled and linked from `SECURITY.md`.

## Azure Ownership and Operations

- [ ] The environment owner confirms this repository does not own Foundry, model, MongoDB, registry,
      network, monitoring, or teardown lifecycle.
- [ ] Deployer, application, Foundry account/project identities, and agent identities have only the
      required RBAC; unused assignments are removed.
- [ ] Registry push/pull permissions, including `AcrPull` for required Foundry identities, are
      verified at the narrowest practical scope.
- [ ] Public network access, firewall/IP rules, private endpoints, DNS, egress, and trusted services
      are reviewed and tested from each deployment boundary.
- [ ] MongoDB backup, point-in-time restore, soft-delete/tombstone, and data-retention policies are
      approved and consistent with privacy commitments.
- [ ] Application Insights collection, sampling, message-content capture, PII handling, access,
      export, and retention are reviewed. Content capture is explicitly disabled unless approved.
- [ ] Cost ownership, quotas, alerts, regional capacity, incident response, and disaster recovery are
      documented outside this sample.

## Responsible AI and Evaluations

- [ ] A private evidence JSON conforming to `docs/release-evidence.schema.json` is stored outside the
      repository and supplied to `scripts/public-release.ps1 -ReleaseEvidencePath <private-path>`.
- [ ] An approved Responsible AI/content-filter policy is bound to every model deployment used by
      the agents; policy name/version and approval are recorded privately.
- [ ] Safety tests cover harmful content, prompt injection, data exfiltration, tool misuse,
      over-refusal, and expected escalation behavior.
- [ ] Versioned evaluation data is licensed, sanitized, representative, and separated into
      development and held-out gates.
- [ ] Quality, safety, latency, reliability, and cost thresholds are defined before evaluation.
- [ ] Both agent deployment paths pass the same applicable release suite, and regressions have an
      explicit approver and expiry rather than a silent waiver.
- [ ] Evaluation results correlate to the exact source commit, dependency graph, prompt/instruction
      version, model deployment, RAI policy, and immutable agent version.

## Entra Authentication and Authorization

- [ ] API and browser client app registrations, tenant policy, owners, redirect/logout URIs, and
      supported account type are reviewed.
- [ ] The API exposes an approved delegated scope and/or application role; consent is documented.
- [ ] The API validates signature, issuer, audience, lifetime, tenant, stable `oid`, and required
      `scp` or `roles` authorization claims.
- [ ] The frontend acquires and sends bearer tokens without storing long-lived credentials.
- [ ] Dev-header authentication fails closed on every deployed compute target.
- [ ] Positive, expired-token, wrong-audience, wrong-tenant, missing-scope/role, and cross-user access
      tests pass.

## Dependencies and Supply Chain

- [ ] Python runtime and tooling dependency files are intentionally pinned/ bounded and all four
      dependency graphs pass `pip-audit --strict`.
- [ ] `frontend/package-lock.json` is generated from an approved public or organization-owned
      registry and `npm ci` reproduces it in a clean environment.
- [ ] Production npm audit passes at the approved severity; development-tool advisories are reviewed,
      time-bounded, and assigned.
- [ ] Container base image and build dependencies are pinned to approved versions/digests and the
      final `linux/amd64` image passes vulnerability/malware scanning.
- [ ] An SBOM covers Python, npm, container OS packages, and produced artifacts; it is attached to
      release evidence with provenance/signatures where required.
- [ ] Dependabot updates, preview SDK upgrades, and transitive dependency exceptions have owners.

## Persistence, Deletion, and Privacy

- [ ] Transcript ownership is explicit and tested: research uses Foundry Conversations while
      triage uses the custom MongoDB transcript with `store=false` and bounded role-preserving
      context.
- [ ] MongoDB stores only the user-scoped `foundryConversationId`, counters, and turn/item
      references for research; research message content is not duplicated in the `messages`
      collection.
- [ ] Turn idempotency handles concurrent retries and failures before/after model invocation without
      duplicate messages, duplicate model calls, or counter drift.
- [ ] Cursor pagination is stable under concurrent writes and is integrated through API, UI, and
      smoke tests.
- [ ] Every read/write/delete remains user-scoped and cross-user existence is not disclosed.
- [ ] Deletion uses an idempotent, resumable tombstone workflow that blocks new turns and has an
      approved minimal retention period.
- [ ] Privacy verification covers primary records, Foundry Conversations, operation records,
      traces/logs, exports, caches, backups/retention, and confirms both the remote research
      transcript and local mapping are removed.
- [ ] Data-subject request, legal hold, backup expiry, and deletion-failure escalation processes have
      accountable owners.

## Approval

- [ ] Release commit: recorded in the release system.
- [ ] Artifact hash and SBOM/provenance: recorded in the release system.
- [ ] Engineering, security, privacy, operations, Responsible AI, and legal approvals: complete.
- [ ] Remaining exceptions: documented with owner, risk acceptance, expiry, and compensating control.