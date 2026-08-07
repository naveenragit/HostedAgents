# Support

This repository is a sample and reference implementation. It is provided without a production
support commitment, uptime objective, response-time SLA, or operational incident service.

## Community Questions and Bugs

Use the repository's public issue tracker for reproducible sample bugs, documentation corrections,
and feature discussions. Before posting:

- reproduce from a clean clone when possible;
- include the component, commit, operating system, and sanitized error text;
- replace tenant/subscription IDs, ARM IDs, endpoints, resource names, user IDs, and message content
  with placeholders;
- never include tokens, secrets, `.env` files, `.azure/` state, environment dumps, or customer data.

Public issues are not a suitable channel for production incidents or vulnerabilities. Report
security concerns privately through [SECURITY.md](SECURITY.md).

## Client and Production Support

The repository owner must configure the supported client contact before public release:

`OWNER-CONFIGURE: CLIENT_SUPPORT_URL`

Until that destination, scope, hours, and escalation policy are configured, there is no client
support channel for this sample. Deployers remain responsible for their application, data,
identity, networking, monitoring, retention, cost, and Azure resource lifecycle.

For Azure service incidents or account/subscription support, use the support plan and channels
associated with the affected Azure subscription. This repository cannot inspect or operate a
consumer's Azure environment.