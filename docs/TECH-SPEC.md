# Artifacts technical specification

Status: implemented baseline, 2026-11-26. This is a single-VPS deployment platform for a trusted Google-account whitelist, not a public hostile-code hosting service. Requirements are governed by code and tests; future features are explicitly listed as limitations.

## Product and trust model

Users authenticate with Google, obtain explicit OAuth consent for their harness, upload a ZIP and deploy it to a stable project hostname. Project ownership is the normalized authenticated email, never an email supplied in a prompt. Admins manage enabled accounts and permissions and inspect all deployments/usage. An admin cannot overwrite another user's project. Regular project listings are owner-filtered; MCP history excludes undeployed drafts. Legacy project membership allows viewing, not deployment or configuration changes.

## Components

| Component | Implementation | Responsibility |
|---|---|---|
| Edge | Existing Traefik, host network | TLS certificates, hostname routing, forward authentication |
| API | Python 3.13 / FastAPI / MCP SDK | Dashboard/static assets, Google login, OAuth issuer/resource server, project and admin APIs |
| Worker | Same platform image, separate process | Serial deployment queue, temporary builds, routing, runtime log snapshots, telemetry |
| Storage | SQLite WAL + host filesystem | Identity/metadata/state and uploads/releases/logs/routes |
| Per-project gateway | Nginx, dedicated project bridge | Stable upstream switching, traffic records, WebSocket proxy |
| Per-project runtime | Nginx static or Node | Tenant code/files, read-only bind mount |
| Temporary builder | Node | Dependency install and optional build without runtime secrets |

Steady state: 2 platform containers + 2 per deployed project, plus existing Traefik. During deployment a candidate runtime and (for Node) temporary builder may exist. No separate database container. Only the worker mounts Docker's socket; this gives it effective host-level privileges despite its container settings.

## Request and deployment paths

Browser/harness → HTTPS Traefik → API for dashboard/MCP. For an app: Traefik → API forwardAuth → project gateway → active runtime. Private apps use a separate host-only view cookie obtained through a one-use portal grant. Public requests receive an anonymous signed telemetry marker. The gateway strips that marker before tenant code receives the request.

1. Create a globally unique project ID (`[a-z][a-z0-9-]{2,39}`), static or node, private by default.
2. Request a 15-minute upload ticket; token hash is stored. PUT ZIP once; receiver streams to disk while hashing and caps bytes.
3. Finalize with SHA-256. Worker verifies stored ZIP hash again before extraction.
4. Create deployment with 8–100-character idempotency key. The database snapshots encrypted runtime environment. Duplicate key with same artifact returns the existing deployment; another artifact conflicts.
5. Worker atomically claims oldest queued deployment. Status: queued → building → healthy or failed.
6. Validate archive paths, counts, sizes and file types. Reject traversal, symlinks, devices, `.env*`, `.git`, `.ssh`, `node_modules`.
7. Node: copy into tmpfs; `npm ci` when lockfile exists, otherwise `npm install`, both with `--ignore-scripts`; run optional `npm run build`. Export validated tar back to host. Runtime secrets are not present during build.
8. Start candidate with non-root UID/GID 10001, dropped capabilities, no-new-privileges, resource caps, read-only filesystem and project-only mounts. Check readiness, recheck deployment permission, update gateway and validate response.
9. Commit active release and healthy status; remove previous runtime container, retain its artifact/release files. Failure attempts to restore the previous route and removes candidate runtime.
10. Rollback creates another deployment from a previous uploaded ZIP with current project environment; external database state is not rolled back.

Worker startup marks interrupted builds failed, restores routes for committed releases and removes interrupted build/candidate containers. It does not implement distributed workers or high availability.

## Authentication and authorization

Google ID token verification checks signing key, issuer, audience, expiry, nonce and verified email; Google subject is bound to the whitelist account after first login. Browser-bound state prevents login flow substitution. Dashboard sessions last 12 hours and use Secure, HttpOnly, host-only cookies. Browser writes require exact portal Origin. Admin APIs require dashboard session, rejecting Authorization headers even if an admin cookie is also present.

Harness uses dynamic client registration with exact allowlisted HTTPS callbacks or localhost HTTP callbacks with a port. Authorization-code flow requires S256 PKCE and exact resource audience. Access tokens last one hour; refresh tokens last 30 days and rotate on use. Raw random tokens are returned once; only hashes are stored. Consent intersects requested scopes with current account permissions. Disabling accounts and revoking permissions take effect during request authorization. Adding permissions does not enlarge old tokens.

| Scope | Capability |
|---|---|
| artifacts:read | Read permitted projects, deployment status and logs |
| artifacts:write | Create own projects, configure environment/visibility |
| deployments:write | Upload, deploy and rollback own projects |

MCP requires read scope as its baseline and corresponding write scope per operation. REST bearer requests also require read scope. Admin account operations are outside harness scopes. Project environment values are encrypted with Fernet, returned only as key names, and decrypted for runtime injection. Docker host administrators can still inspect runtime environment.

## Resources and network boundaries

| Item | Limit |
|---|---|
| Projects | 8 global |
| Upload records | 30/project, 200 global |
| ZIP / expanded or built output | 50 MiB / 150 MiB |
| ZIP entries / exported tar entries | 4,000 / 50,000 |
| Concurrent worker builds / pending deployment queue | 1 / 10 queued-or-building |
| Node build | 300 seconds, 768 MiB, 1 CPU |
| Node runtime | 384 MiB, 0.75 CPU |
| Static runtime | 64 MiB, 0.75 CPU |
| Gateway | 32 MiB, 0.25 CPU |
| API and worker, each | 384 MiB, 0.75 CPU |
| Process cap | 128 per configured container |
| App gateway request body | 10 MiB |
| Required free disk before deployment | 2 GiB |

Project bridges occupy 172.29.1.0/24 through 172.29.8.0/24. Scoped iptables rules permit same-project traffic/public egress and block host/private network destinations; Docker isolation and host firewall are part of the security boundary. These rules must be applied and persisted by the operator. Shared-kernel isolation is not equivalent to separate VMs.

## Observability

Worker collects gateway records every 15 seconds and UI refreshes every 30 seconds. Records include request ID, timestamp, status, response-body bytes, duration, signed project-bound identity. No URL/path/query/IP/cookie is deliberately included in structured traffic records. Aggregates group by hour/project/email, retain 30 days; request IDs deduplicate across reads, retained 24 hours. Requests without valid marker are excluded; public users without app sessions are anonymous. Requests include assets and completed WebSockets, not unique people/page views. Errors rejected before the gateway are excluded. Docker log rotation during outages can lose records; this is not billing-grade measurement.

CPU/RAM are latest runtime samples (not gateway/build totals); CPU 100% means one core. Runtime logs are snapshots of recent output, not complete historical streams. Deployer attribution comes from deployment records; audit history is separate. Raw application/error logs may contain user-generated data; redaction is best effort.

## Limitations and future work

No project deletion, automatic release/ZIP pruning, persistent tenant volumes, recurring backups, per-user billing, request tracing, dependency scan of uploaded apps, or HA. Deployment history can grow when redeploying an existing ZIP. Single VPS/storage failure affects all services; off-host encrypted backup and restore drills remain operator responsibilities. See the security review for remaining availability and host isolation risks.
