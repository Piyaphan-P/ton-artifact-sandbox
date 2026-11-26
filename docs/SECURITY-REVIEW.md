# Security review — 2026-11-26

## Scope and assurance

Reviewed the platform application, dashboard JavaScript, Compose/Docker settings, archive handling, OAuth/authorization, backup script and repository publication contents. This was source review plus automated tests/scanners and targeted live authorization checks, not an exhaustive penetration test or a container/host CVE audit. No production credentials or tenant content are included in evidence. Findings below are manual severity assessments, not formal CVSS scores.

## Fixed findings

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| SEC-01 | High | A harness bearer token for an admin could reach account/permission/membership APIs, despite consent describing artifact scopes only. Read-only admin observability endpoints also accepted harness tokens. | New admin_actor requires dashboard cookie session, rejects Authorization headers, verifies admin and enforces exact Origin on writes. Applied to users, permissions, membership changes, audit and admin analytics/logs. |
| SEC-02 | Medium | REST bearer read endpoints checked audience but did not require consented artifacts:read scope consistently. | actor now requires artifacts:read for bearer REST requests, matching MCP baseline; write operations retain their own scope/permission checks. |
| SEC-03 | Low | Upload quota count/check/insert could interleave across requests. | Upload reservation uses BEGIN IMMEDIATE before count and insert, serializing quota checks. |
| SEC-04 | Low | Backup archive was chmod 600 only after completion; creation permissions depended on process umask and existing directory mode. | Set umask 077 before creating snapshots/archive so secret-containing backup files are private from creation. |

SEC-01/02 regression tests verify that even a full-scope admin harness token cannot list/manage accounts, change permissions, change membership or read admin overview/history; same cookie session still works and normal artifact reads remain permitted. A token without read scope is denied. Existing ownership, PKCE, refresh replay, consent, CSRF, upload checks, archive traversal/symlinks, telemetry signature and log redaction tests remain passing.

## Validation evidence

- pytest: **27 passed** on local Python 3.14; one third-party TestClient deprecation warning. Runtime uses Python 3.13; live smoke verifies deployed behavior rather than claiming a full production pytest run.
- pip-audit 2.10.1: **36 resolved runtime packages, 0 known vulnerabilities reported** by its advisory lookup at review time. Does not scan Docker OS packages, tenant npm dependencies or prove absence of unknown vulnerabilities.
- Gitleaks 8.30.1: source directory scan with redaction found no leaks; full Git history is scanned before push. `.env.example` has blank secret fields. No private Google JSON, .env, database, ZIP, backup, log or private key is tracked.
- Bandit 1.9.4: **0 high-severity findings**. Medium/low findings were reviewed; see triage below. The scan is not represented as finding-free.
- JavaScript syntax: app.js and admin.js pass node --check.
- VPS updated after source hash checks, no queued/running deployments, and a private backup. API health reports ok / worker_ready / google_configured. Live smoke checks session-admin success, bearer-admin denial and preserved artifact reads; temporary tokens are removed afterward.

## Static scanner triage

| Rule | Locations | Assessment |
|---|---|---|
| B608 | admin queries | SQL fragments are constant clauses; all user filters/limit/offset are bound parameters. No input concatenation into SQL. |
| B608 | worker.update | Dynamic column names originate only in internal fixed call sites; deployment/user values use bound parameters. Do not expose arbitrary extra keys to requests. |
| B105 | OAuth responses | `none` and `Bearer` are protocol constants, not passwords. |
| B108 | worker tmpfs paths | `/tmp` and `/workspace` are inside isolated non-root containers with bounded tmpfs; not world-writable host temp files. |
| B103 | release permissions | 0755 applies to published build output directories for non-root runtime access, not credential files. |
| B104 | Node host | 0.0.0.0 binding is within a project container; runtime ports are not published on the host. |
| B110 | heartbeat / cleanup | Best-effort failures may be swallowed; readiness uses heartbeat age, cleanup is operationally observable. Availability/diagnostic limitation, not authorization bypass. |
| B404/B603/B607 | remote_exec | Operator-only maintenance helper invokes the selected Orca CLI via argument list, no shell. Requires trusted executable PATH and trusted script, not reachable from tenant API. |

## Residual risks and follow-up

- Docker socket gives worker host-level control; shared kernel and public egress mean this platform remains restricted to trusted whitelist users. Host/kernel/Docker/Traefik and runtime-image vulnerability scanning is not included in the dependency scan.
- Request-rate limits are per application process/database keys, not comprehensive edge DDoS defense. Non-upload body checks currently rely on Content-Length and permit large bodies; add a streaming body cap, connection deadlines and edge rate/concurrency controls before opening to untrusted accounts/public scale.
- Repeated deployments of retained artifacts can accumulate release directories/logs; no automatic pruning or per-user disk budgets. Global upload and queue limits do not bound all historical release disk growth.
- Python base and template image tags can move; current production tenant runtime images are configured separately. Pin reviewed digests and scan complete images for reproducibility and OS advisories.
- Runtime environment is visible to VPS/Docker administrators. Backup includes .env/encryption key and requires encrypted off-host storage. No recurring off-host backup or restore drill was added by this review.
- Redaction is best effort; an app can encode or fragment a secret in output. Gateway structured metrics omit paths/IPs, but application/error logs can contain arbitrary user data.
- CI uses major-version action tags, not commit pins; dependency advisory results can change. Review updates and rerun scans regularly.

No claim of “zero vulnerabilities” is made. Fixed privilege/scope issues are verified; the remaining constraints above must be considered before increasing trust or deployment scale.
