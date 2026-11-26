# Piyaphan Artifacts

Self-hosted dashboard and authenticated MCP deployment service on Hostinger.

- Dashboard: https://artifacts.piyaphan-p.tech
- MCP: https://mcp.piyaphan-p.tech/mcp
- OAuth issuer: https://auth.piyaphan-p.tech
- Google redirect URI: https://artifacts.piyaphan-p.tech/auth/google/callback
- App URLs: https://PROJECT.apps.piyaphan-p.tech

## Architecture

Existing Traefik terminates HTTPS. API serves the dashboard, Google login, OAuth with S256 PKCE, whitelist and project authorization, and eight MCP tools. Worker processes a SQLite queue and is the only platform service with Docker socket access. API and worker share `ton-artifacts:1` and persistent `/opt/ton-artifacts/data` storage.

Each project has a dedicated bridge network, nginx gateway and active runtime container. Node builds use a temporary container; healthy releases replace the gateway upstream. A failed readiness check leaves the previous release serving. Rollback rebuilds a previous uploaded artifact using current environment values; it does not revert external databases.

Images: platform, pinned nginx, pinned Node. Added steady-state containers: 2 + 2 per project. Existing Traefik/Hermes are separate.

## Use

Sign in with an invited Google account. Admins invite users and set artifact permissions under People & access. Members create and manage projects owned by their authenticated email. Legacy membership grants viewing only; only the project owner can deploy or change visibility. Apps are private by default.

Upload a ZIP with `index.html` at the root for static sites. For Node, include `package.json` with a `start` script. Listen on `0.0.0.0:3000`; `/` must respond with 2xx or 3xx. Dependencies install with lifecycle install scripts disabled; an optional `npm run build` runs without project secrets. ZIPs must exclude `.env`, `.git`, `.ssh`, and `node_modules`.

Configure runtime environment variables in the dashboard, then deploy a new release. Values are encrypted at rest and never read back through the API. Connect external services such as Supabase through these variables. Runtime disk is read-only apart from temporary storage; persistent app data belongs in an external service.

## MCP / harness

Configure a remote Streamable HTTP MCP server at `https://mcp.piyaphan-p.tech/mcp`, with OAuth enabled. Discovery provides the issuer and dynamic registration endpoint. Authorize through Google and the explicit consent screen. Supported callbacks are loopback HTTP with a port and the configured approved HTTPS callback list (`OAUTH_ALLOWED_REDIRECTS`). Other hosted harness callbacks must be added by the owner; do not broaden to arbitrary redirects.

Tools: `projects_list`, `projects_create`, `artifacts_create_upload`, `artifacts_finalize`, `deployments_create`, `deployments_get`, `deployments_logs`, `deployments_rollback`.

Upload workflow: request upload ticket, PUT ZIP using the returned one-use header, finalize its SHA256, create deployment with an idempotency key, poll status. Do not log upload or OAuth tokens.

## Operations

VPS source: `/root/orca/projects/ton-agent-vps-hostinger/artifact-platform`.
Run Compose commands from there. `.env` is root-readable only and must never be committed.

```sh
docker compose ps
docker compose logs --tail 100 worker
docker compose --progress plain build
docker compose --progress plain up -d
curl -fsS https://artifacts.piyaphan-p.tech/health
python3 scripts/backup.py
```

Backup creates a root-only archive under `/opt/ton-artifacts/backups`, containing a consistent SQLite snapshot, uploads, routes, Compose and encryption/OAuth credentials. Store an encrypted copy off-host separately. It does not contain built release directories; restoring requires redeploying artifacts. Keep the original encryption key. No recurring backup schedule is installed.

Limits: 8 projects, 30 artifacts per project, 200 total uploads, 50 MB ZIP, 150 MB expanded/built output, one build at a time and 300-second build timeout. Node runtime 384 MB RAM; build 768 MB; static runtime 64 MB; gateway 32 MB. Build dependencies and packages must fit these limits.

Firewall service `ton-artifacts-firewall` restricts the reserved `172.29.0.0/16` app subnet from reaching private networks and the VPS host, while allowing each project network and public egress. Other Docker networks are not modified.

This is for a small trusted whitelist on a shared Linux kernel, not a hostile public code-execution service. Worker Docker access is privileged infrastructure. Backups, Docker updates and host maintenance remain owner responsibilities. Project deletion, scheduled artifact retention and persistent app volumes are not implemented.

## Validation

Local tests: `PYTHONPATH=. pytest -q tests`. They cover authorization/revocation, CSRF, secret encryption, upload integrity/idempotency, PKCE/token audience/replay, MCP scopes, private app sessions, ZIP traversal and symlinks. Production smoke tests must additionally exercise real Docker builds, routing, HTTPS, rollback, and Google sign-in.

## Email ownership policy

Projects are owned by the normalized email from the authenticated identity. Whitelisted members can create their own projects. MCP project history excludes other owners and undeployed drafts; the dashboard includes only the current user's drafts. Deployment, rollback, environment and visibility changes require ownership, including for platform owners. Membership alone only grants viewing access. Legacy projects are assigned from their single deployment author; mixed-author histories remain write-blocked for explicit reconciliation. OAuth discovery and the unauthenticated MCP challenge advertise all three artifact scopes; existing tokens require new consent to gain write scope.


Admin permissions: `artifacts:read` allows reading own projects/logs; `artifacts:write` allows creating and configuring own projects; `deployments:write` allows upload, deploy and rollback of own projects. Admins select these under People & access. Revocations apply immediately to existing tokens; new OAuth scopes require reconnection/consent. The admin account is piyaphan.po@gmail.com. The company account piyaphan.p@infinitaskt.com is a member; its artifact permissions remain configurable by the admin. OAuth identity cannot be changed by writing an email in a prompt; sign in with the intended Google account.

## Admin observability

Admin-only GET endpoints: `/api/admin/overview?hours=24&project=`, `/api/admin/deployments?project=&email=&offset=0&limit=30`, and `/api/admin/deployments/{id}/logs`. These do not bypass ownership for writes. Gateway JSON access records contain only request ID, timestamp, status, response bytes, duration and a signed app-bound principal; client identities cannot be supplied as plain email headers. The gateway removes the principal before forwarding to tenant code. Worker polls every 15 seconds, deduplicates request IDs, keeps hourly traffic/MCP aggregates 30 days and samples runtime CPU/RAM. No raw paths, IPs, cookies or authentication tokens are stored. Existing gateways reload logging config during worker startup. Counters begin at `telemetry_meta.started`, and admin UI reports collector freshness. This is best-effort operational telemetry, not billing-grade metering: log rotation during downtime can lose events, and completed WebSockets count at disconnect.

## Technical documentation

- [Technical specification](docs/TECH-SPEC.md): components, deployment lifecycle, security boundaries and limits.
- [Data model and ER](docs/DATA-MODEL.md): all tables, columns, keys and indexes.
- [API and MCP reference](docs/API.md): routes, request models, scopes and workflows.
- [Operations runbook](docs/OPERATIONS.md): setup, upgrades, backups and recovery.
- [Security review](docs/SECURITY-REVIEW.md): findings, fixes, evidence and residual risks.
- [User guide](USER-GUIDE.md): dashboard, Google login, MCP and deployment examples.

Admin APIs now require a dashboard session; harness bearer tokens cannot administer users,
permissions, membership or cross-project analytics. No production credentials or user artifacts
are included in this repository. `.env.example` contains blank secret fields only.
