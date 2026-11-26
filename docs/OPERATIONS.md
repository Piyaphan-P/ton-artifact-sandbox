# Operations runbook

## Current installation

VPS: 72.60.43.77. Source directory: `/root/orca/projects/ton-agent-vps-hostinger/artifact-platform`. Persistent data: `/opt/ton-artifacts/data`; backups: `/opt/ton-artifacts/backups`. These addresses are operational metadata, not credentials. The repository excludes deployed tenant ZIPs, databases, production `.env`, tokens, private keys and Google client JSON.

## Bootstrap on a compatible Linux Docker host

1. Install Docker Engine with Compose and configure existing Traefik with Docker provider, `websecure`, and `letsencrypt`. Current Compose assumes Traefik uses the host network so its forwardAuth can reach `127.0.0.1:18080`. Do not expose this API port publicly.
2. Point portal/auth/MCP and app DNS names at the VPS. Review Compose's literal hostname rules, callback regex and firewall host IP if changing domains/host. `.env` URL settings alone do not rewrite those literals.
3. Copy `.env.example` to `.env`, restrict mode to 600, set the Google web OAuth client ID/secret, admin email, and a new Fernet key. Generate the key locally using `Fernet.generate_key()` from cryptography and save it directly to the private configuration; never put it in shell history, CI logs or Git. Google redirect URI is `PORTAL_URL/auth/google/callback`.
4. Resolve and review Nginx/Node image digests and set `NGINX_IMAGE` / `NODE_IMAGE`. Template tags are examples, not immutable production pins. Review/pin the Python base image for reproducible releases too.
5. Create `/opt/ton-artifacts/data` owned by UID/GID 10001 with restrictive directory permissions; API user needs write access. Keep this path equal to the Compose mount and `HOST_DATA_DIR`.
6. Review `scripts/firewall.sh`, apply it as root after Docker starts and arrange persistence (the existing installation uses `ton-artifacts-firewall`). Do not blindly replace unrelated host rules.
7. Run `docker compose build` and `docker compose up -d`; inspect `docker compose ps`, `/health`, worker log, and an actual private app flow. Verify firewall isolation from a disposable app container before inviting users.

The only settings holding secrets are private `.env` values and encrypted project settings in SQLite. `OWNER_EMAILS` bootstraps/promotes admins on startup; editing it does not automatically demote existing DB users. Never rotate `ENCRYPTION_KEY` without migrating existing ciphertext and retaining a recoverable old key.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q tests
.venv/bin/pip-audit -r requirements.txt
.venv/bin/bandit -r app scripts -f json -o /tmp/artifacts-bandit.json
gitleaks dir . --redact
node --check app/static/app.js
node --check app/static/admin.js
```

Tests use a temporary database and generated test secrets; they do not require production credentials. CI runs tests, dependency audit, high-severity Bandit gate and JavaScript syntax checks. Medium/low Bandit findings still need human triage. Gitleaks should be run before each push; CI is not a replacement for a pre-publication scan.

## Upgrade

Check source drift and queued/building deployments first. Back up SQLite/config and source before changing files. Run `python3 scripts/backup.py` from the current VPS source path; the script has installation-specific paths. Build the candidate platform image, then `docker compose up -d`. API and worker must run the same version. Verify `/health` reports worker ready, private/public routes, MCP read/write authorization, admin session access, and rejected harness admin requests. Upgrading platform containers does not rebuild existing tenant runtimes.

For a failed source upgrade restore the backed-up source/config and rebuild/recreate platform services. Database schema migration is additive at startup; do not restore an old DB casually because it discards newer user activity. Current security fixes require no schema rollback.

## Backups and disaster recovery

`backup.py` sets umask 077 before creating an archive, copies a consistent SQLite snapshot via SQLite's backup API and archives uploads, routes, `.env` and Compose. Archive is mode 600 and contains real secrets. It excludes built release directories, logs, images and platform source. Copy encrypted backups off-host; there is no installed recurring schedule or guaranteed RPO/RTO.

Recovery: on an isolated replacement host restore matching source revision and configuration, preserve the encryption key, restore snapshot/uploads/routes with correct permissions, install/pin images and reapply host firewall/Traefik configuration. Old active-release pointers refer to runtime containers that do not exist on a new host; recreate releases by redeploying retained artifacts using the correct owner identity. Keep DNS off the replacement until private/public routes and auth pass. Do not claim an archive alone is a complete live-container restore. A full restore drill is not part of the repository security scan.

## Troubleshooting

- Missing write scope: reconnect harness and consent to the needed scopes; changing account permissions does not upgrade existing grants.
- Missing project: compare authenticated email, owner_email and whether MCP project history has any deployment by that email.
- Admin 403 with bearer: expected; use Google dashboard session for user/permission/admin analytics operations.
- Stuck deployment: inspect worker heartbeat and deployment log; worker restart marks interrupted builds failed while preserving committed routes.
- App 502: inspect gateway upstream, runtime readiness and project bridge; avoid deleting a healthy previous release during diagnosis.
- Metrics stale: check worker telemetry errors/log rotation and collector timestamp, not just API health.
- Storage full: retained uploads/releases are not automatically pruned. Inventory and back up before any manual deletion; reconcile database references.

`remote_exec.py` is an optional Orca terminal transport for explicitly authorized maintenance scripts. Set `ORCA_REMOTE_ENV` and `ORCA_REMOTE_TERMINAL` from a verified existing shell. It is not an HTTP endpoint and must never execute untrusted uploaded scripts. Inspect the terminal after a transport failure before retrying a mutation.
