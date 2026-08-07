# Security Policy

## Supported Versions

No public production release is currently supported. Security reports are accepted for the default
branch while the project is being prepared for release, but the sample carries no production SLA.

| Version | Supported |
|---|---|
| Default branch (unreleased) | Reports accepted; fixes are best effort |
| Any pre-release tag or copied deployment | No |

The supported-version table must be revised before the first approved public release and after each
release line reaches end of support.

## Report a Vulnerability Privately

Do not disclose a suspected vulnerability in a public issue, discussion, pull request, commit
message, build log, or chat transcript. Do not include access tokens, message content, environment
dumps, live resource identifiers, or working exploit data in public channels.

The repository owner must configure a private reporting destination before public release:

`OWNER-CONFIGURE: PRIVATE_SECURITY_REPORTING_URL`

The preferred destination is GitHub Private Vulnerability Reporting or an organization-managed
security intake portal. Until that destination is configured, public release remains blocked. An
internal reporter should use their organization's existing private security channel rather than a
repository issue.

Include only what is needed to reproduce and assess the issue:

- affected commit, tag, component, and deployment mode;
- impact and prerequisites;
- minimal reproduction steps or proof of concept;
- whether a credential, customer record, or live Azure resource may be involved;
- suggested mitigation, if known;
- a safe way to coordinate follow-up.

Do not test against an environment or data set you do not own or have explicit permission to use.
Acknowledgement and remediation targets must be configured by the owner with the private reporting
channel; none are promised by this unreleased sample.

## Secrets and Public Identifiers

Tokens, API keys, passwords, client secrets, private keys, credential-bearing connection strings,
signed URLs, and unredacted message/log exports are secrets or sensitive data. Tenant and
subscription IDs, ARM IDs, endpoints, and resource names are generally public identifiers, not
credentials, but they still map a live environment and are sanitized from examples.

Never post either class when it is unnecessary to resolve the report. See the configuration
classification in [docs/implementation-plan.md](docs/implementation-plan.md).

## Secret or Identifier Incident Procedure

1. Contain access and stop publishing affected artifacts. Do not wait for source cleanup before
   disabling a compromised credential.
2. Revoke or rotate exposed credentials first, including derived credentials, certificates,
   deployment tokens, and service connections. Review Azure and identity-provider audit logs.
3. Preserve minimal forensic evidence in the approved private incident system. Do not duplicate
   secrets into tickets or chat.
4. Scan the complete Git history, tags, release artifacts, CI logs, caches, package registries, and
   deployment outputs to determine exposure.
5. Remove the value from the current tree and replace it with keyless authentication or an approved
   secret reference. Add a regression check where practical.
6. Rewrite Git history only if a secret or public identifier must be removed from prior commits by
   explicit owner/security decision. Freeze merges, coordinate protected branches and forks, use an
   approved history-rewrite tool, invalidate old artifacts/caches, force-push only the reviewed
   refs, and require affected clones to be replaced. History rewriting does not revoke a secret.
7. Re-run full-history secret scanning and artifact inspection, verify the replacement credential,
   and monitor for use of the revoked value.
8. Document root cause, affected systems, retention implications, and prevention actions privately.

This procedure is guidance, not authorization to rewrite repository history or change Azure
resources. Those actions require the applicable repository and environment owners.