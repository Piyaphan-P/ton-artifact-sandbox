# HTTP API and MCP reference

## Authentication

Dashboard: Google session cookie; state-changing calls require exact portal Origin. Bearer access tokens require the MCP resource audience and `artifacts:read`, plus the operation write scope. Admin endpoints accept dashboard sessions only. Upload PUT uses a one-use `Authorization: Upload <ticket>` header. Never persist tokens in examples or logs.

## HTTP routes

| Method | Path | Handler |
|---|---|---|
| GET | `/.well-known/oauth-protected-resource/mcp` | `main.py:protected_resource_metadata` |
| GET | `/health` | `main.py:health` |
| GET | `/api/me` | `main.py:me` |
| GET | `/api/projects` | `main.py:projects` |
| POST | `/api/projects` | `main.py:project_create` |
| PUT | `/api/projects/{pid}/visibility` | `main.py:visibility` |
| GET | `/api/projects/{pid}/environment` | `main.py:environment_keys` |
| PUT | `/api/projects/{pid}/environment` | `main.py:environment_set` |
| GET | `/api/users` | `main.py:users` |
| PUT | `/api/users/{target}/permissions` | `main.py:set_permissions` |
| POST | `/api/users` | `main.py:invite` |
| GET | `/api/projects/{pid}/members` | `main.py:members` |
| POST | `/api/projects/{pid}/members` | `main.py:membership` |
| GET | `/api/audit` | `main.py:audit_list` |
| POST | `/api/projects/{pid}/uploads` | `main.py:upload_ticket` |
| PUT | `/api/uploads/{uid}` | `main.py:upload` |
| POST | `/api/uploads/{uid}/finalize` | `main.py:finalize` |
| POST | `/api/projects/{pid}/deployments` | `main.py:deploy` |
| GET | `/api/projects/{pid}/deployments` | `main.py:release_list` |
| GET | `/api/deployments/{did}` | `main.py:release` |
| GET | `/api/deployments/{did}/logs` | `main.py:logs` |
| POST | `/api/deployments/{did}/rollback` | `main.py:rollback` |
| GET | `/` | `main.py:index` |
| GET | `/auth/login` | `auth.py:google_login` |
| GET | `/auth/google/callback` | `auth.py:google_callback` |
| POST | `/auth/logout` | `auth.py:logout` |
| GET | `/.well-known/oauth-authorization-server` | `auth.py:metadata` |
| POST | `/oauth/register` | `auth.py:register` |
| GET | `/oauth/authorize` | `auth.py:authorize` |
| GET | `/oauth/consent` | `auth.py:consent` |
| POST | `/oauth/consent` | `auth.py:consent_submit` |
| POST | `/oauth/token` | `auth.py:exchange` |
| POST | `/oauth/revoke` | `auth.py:revoke` |
| GET | `/internal/access` | `auth.py:access_check` |
| GET | `/access` | `auth.py:app_access` |
| GET | `/_artifact/callback` | `auth.py:view_callback` |
| GET | `/api/admin/overview` | `admin.py:overview` |
| GET | `/api/admin/deployments` | `admin.py:deployments` |
| GET | `/api/admin/deployments/{did}/logs` | `admin.py:logs` |

MCP SDK additionally mounts `/mcp`; static UI assets are under `/static`. OpenAPI (`/openapi.json`) exposes the FastAPI routes/models, not MCP tool schemas. See source handler for response details and permission checks.

## Request models

### ProjectIn

```python
class ProjectIn(BaseModel):
    id: str = Field(pattern='^[a-z][a-z0-9-]{2,39}$')
    name: str = Field(min_length=1, max_length=80)
    runtime: str = Field(pattern='^(static|node)$')
    visibility: str = Field(default='private', pattern='^(private|public)$')
```

### VisibilityIn

```python
class VisibilityIn(BaseModel):
    visibility: str = Field(pattern='^(private|public)$')
```

### EnvIn

```python
class EnvIn(BaseModel):
    values: dict[str, str]
```

### PermissionsIn

```python
class PermissionsIn(BaseModel):
    permissions: list[str]
```

### UserIn

```python
class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    enabled: bool = True
```

### MemberIn

```python
class MemberIn(BaseModel):
    email: str
    role: str = Field(pattern='^(viewer|deployer|maintainer|remove)$')
```

### FinalizeIn

```python
class FinalizeIn(BaseModel):
    sha256: str
```

### DeployIn

```python
class DeployIn(BaseModel):
    artifact_id: str
    idempotency_key: str
```

### RollbackIn

```python
class RollbackIn(BaseModel):
    idempotency_key: str
```

## MCP tools

| Tool | Arguments | Result |
|---|---|---|
| projects_list | none | Own deployed project history |
| projects_create | project_id, name, runtime=static | Private owned project |
| artifacts_create_upload | project_id | artifact_id, upload_url, one-use headers, expiry, size cap |
| artifacts_finalize | artifact_id, sha256 | ready artifact |
| deployments_create | project_id, artifact_id, idempotency_key | Deployment record |
| deployments_get | deployment_id | Current status/metadata |
| deployments_logs | deployment_id | Redacted recent log text |
| deployments_rollback | deployment_id, idempotency_key | New deployment of retained artifact |

Sequence: projects_create (if needed) → artifacts_create_upload → HTTP PUT ZIP → artifacts_finalize → deployments_create → poll deployments_get → deployments_logs. Use the returned URL/header directly over HTTPS; tool input cannot override authenticated email. Retry create with the same idempotency key after an uncertain response; never blindly create duplicates.

## Responses and errors

Domain records exclude project env ciphertext and deployment env_snapshot. Environment endpoints return key names only. HTTP errors use `detail` except OAuth token errors (`error`, `error_description`). 400 invalid input; 401 invalid/expired authentication; 403 scope/permission/ownership denial; 404 missing resource; 409 quota/idempotency/queue conflict; 413 ZIP too large; 422 model validation; 429 rate limit. MCP SDK wraps tool failures in its protocol response. Deployment submission success is not deployment completion.

## Admin analytics

`overview`: hours 1–720, optional project filter. Returns project inventory, selected traffic totals/time series, all-user usage, anonymous count, sampling timestamps and retention. User usage remains platform-wide even when project traffic is filtered. CPU/RAM are latest snapshots, not averages over the selected window.

`deployments`: optional exact project/email, limit 1–100, offset 0–100000. Returns rows and total. `deployments/{did}/logs` audits admin access and applies current plus deployment-snapshot secret redaction.

## OAuth

Discover issuer via protected-resource metadata. Register public PKCE client with allowed redirect_uris. Authorize with response_type=code, S256 challenge, exact resource, state and requested scopes. Explicit Google dashboard consent grants the intersection with allowed permissions. Exchange code with verifier, client_id, same redirect_uri and resource; refresh rotates token. Revocation accepts token and client_id. Session/refresh tokens are never committed to this reference.
