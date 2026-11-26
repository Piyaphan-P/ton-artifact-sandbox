import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .core import *
from .auth import router, actor, admin_actor, cookie_session, request_ip
from .admin import router as admin_router

class Verifier(TokenVerifier):
    async def verify_token(self,value):
        try:
            t=token(value,'access')
            if t['resource']!=RESOURCE: return None
            return AccessToken(token=value,client_id=t['client'],scopes=t['scope'].split(),expires_at=t['expires'],resource=t['resource'],subject=t['email'])
        except HTTPException: return None

mcp=FastMCP('Piyaphan Artifacts',instructions='Deploy ZIP artifacts to assigned projects. Obtain an upload ticket, PUT a ZIP with the returned Authorization header, finalize with SHA-256, then create a deployment with an idempotency key. Projects are private by default. Deployment is asynchronous: poll deployments_get. Never upload credentials or .env files.',token_verifier=Verifier(),auth=AuthSettings(issuer_url=ISSUER,resource_server_url=RESOURCE,validate_token_resource=True,required_scopes=['artifacts:read']),stateless_http=True,json_response=True,transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=[urlsplit(RESOURCE).netloc,'localhost:*','127.0.0.1:*'],allowed_origins=[PORTAL,ISSUER]))

def mcp_actor(write=False,permission='deployments:write'):
    t=get_access_token()
    if not t or not t.subject: raise ValueError('Authentication required')
    user(t.subject)
    needed=permission if write else 'artifacts:read'
    if needed not in t.scopes: raise ValueError(needed+' scope required; reconnect and consent')
    require_permission(t.subject,needed)
    from .telemetry import usage
    usage(t.subject,'mcp')
    return t.subject

@mcp.tool()
def projects_list() -> list[dict]:
    """List only this authenticated email's projects with deployment history. Other users' projects and undeployed drafts are excluded."""
    return list_projects(mcp_actor())

@mcp.tool()
def projects_create(project_id:str,name:str,runtime:str='static') -> dict:
    """Create a private project owned by the authenticated email. Choose a new unique project_id; existing projects cannot be taken over. runtime: static or node. Then request an upload ticket."""
    return create_project(mcp_actor(True,'artifacts:write'),project_id,name,runtime)

@mcp.tool()
def artifacts_create_upload(project_id:str) -> dict:
    """Request a 15-minute upload ticket. PUT a ZIP of built static files or Node source with the returned header. Never include .env, node_modules or .git."""
    return create_upload(mcp_actor(True),project_id)

@mcp.tool()
def artifacts_finalize(artifact_id:str,sha256:str) -> dict:
    """Verify the uploaded ZIP's SHA-256 checksum before deployment."""
    return finalize_upload(mcp_actor(True),artifact_id,sha256)

@mcp.tool()
def deployments_create(project_id:str,artifact_id:str,idempotency_key:str) -> dict:
    """Queue an immutable release. Reuse the same idempotency key only for retrying the same artifact."""
    return create_deployment(mcp_actor(True),project_id,artifact_id,idempotency_key)

@mcp.tool()
def deployments_get(deployment_id:str) -> dict:
    """Get deployment status and its project URL. Poll until healthy or failed."""
    return deployment(mcp_actor(),deployment_id)

@mcp.tool()
def deployments_logs(deployment_id:str) -> dict:
    """Get the bounded tail of build/runtime logs with configured secret values redacted."""
    return get_logs(mcp_actor(),deployment_id)

@mcp.tool()
def deployments_rollback(deployment_id:str,idempotency_key:str) -> dict:
    """Redeploy a previously healthy artifact. Does not roll back database changes. Uses current project environment."""
    email=mcp_actor(True); d=deployment(email,deployment_id)
    if d['status']!='healthy': raise ValueError('Select a previously healthy release')
    return create_deployment(email,d['project'],d['upload'],idempotency_key)

@asynccontextmanager
async def lifespan(app):
    init()
    async with mcp.session_manager.run(): yield

app=FastAPI(title='Piyaphan Artifacts',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

@app.middleware('http')
async def boundaries(request,call_next):
    host=request.url.hostname or ''
    canonical={urlsplit(PORTAL).hostname,urlsplit(ISSUER).hostname,urlsplit(RESOURCE).hostname,'testserver','localhost','127.0.0.1'}
    path=request.url.path
    if host not in canonical and not (host.endswith('.'+APP_DOMAIN) and path=='/_artifact/callback'):
        return JSONResponse({'detail':'Unknown host'},400)
    if path.startswith('/api/') or path.startswith('/oauth/'):
        size=request.headers.get('content-length','0')
        try:
            if int(size)>MAX_UPLOAD and not path.startswith('/api/uploads/'): return JSONResponse({'detail':'Request too large'},413)
        except ValueError: return JSONResponse({'detail':'Invalid length'},400)
    response=await call_next(request)
    if path=='/mcp' and response.status_code==401:
        # Advertise the full consent scope to clients that derive their OAuth
        # request from the challenge instead of authorization-server metadata.
        response.headers['WWW-Authenticate']=(
            'Bearer resource_metadata="'+RESOURCE.rsplit('/mcp',1)[0]+
            '/.well-known/oauth-protected-resource/mcp", scope="'+' '.join(SCOPES)+'"'
        )
    response.headers['X-Content-Type-Options']='nosniff'
    # Native OAuth consent form POSTs need their same-origin Origin header.
    # no-referrer can make browsers send Origin: null on navigation requests.
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin' if path=='/oauth/consent' else 'no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Cache-Control']='no-store'
    response.headers.setdefault('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' "+PORTAL+"; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return response

@app.exception_handler(HTTPException)
async def denied(request,exc):
    return JSONResponse({'detail':exc.detail},exc.status_code,headers=exc.headers)

app.include_router(router)
app.include_router(admin_router)

@app.get('/.well-known/oauth-protected-resource/mcp')
async def protected_resource_metadata():
    # FastMCP derives scopes_supported from required_scopes, which describes
    # the minimum for reads, not every scope supported by deployment tools.
    return {'resource':RESOURCE,'authorization_servers':[ISSUER],
            'scopes_supported':SCOPES,'bearer_methods_supported':['header']}

@app.get('/health')
async def health():
    with db() as c: r=c.execute("SELECT heartbeat FROM worker WHERE id='main'").fetchone()
    return {'status':'ok','worker_ready':bool(r and now()-r['heartbeat']<30),'google_configured':bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))}

@app.get('/api/me')
async def me(request:Request):
    email=actor(request); u=user(email)
    return {'email':email,'role':u['role'],'permissions':permissions(email),'mcp_url':RESOURCE}

@app.get('/api/projects')
async def projects(request:Request,include_drafts:bool=False): return list_projects(actor(request),include_drafts)

class ProjectIn(BaseModel):
    id:str=Field(pattern=r'^[a-z][a-z0-9-]{2,39}$')
    name:str=Field(min_length=1,max_length=80)
    runtime:str=Field(pattern=r'^(static|node)$')
    visibility:str=Field(default='private',pattern=r'^(private|public)$')

@app.post('/api/projects')
async def project_create(request:Request,data:ProjectIn):
    email=actor(request,True,'artifacts:write')
    p=create_project(email,data.id,data.name,data.runtime)
    if data.visibility=='public':
        with db() as c: c.execute("UPDATE projects SET visibility='public' WHERE id=?",(data.id,))
        p['visibility']='public'
    return p

class VisibilityIn(BaseModel):
    visibility:str=Field(pattern=r'^(private|public)$')

@app.put('/api/projects/{pid}/visibility')
async def visibility(request:Request,pid:str,data:VisibilityIn):
    email=actor(request,True,'artifacts:write'); project_access(email,pid,manage=True)
    with db() as c: c.execute('UPDATE projects SET visibility=? WHERE id=?',(data.visibility,pid))
    audit(email,'project.visibility.'+data.visibility,pid)
    return {'ok':True}

@app.get('/api/projects/{pid}/environment')
async def environment_keys(request:Request,pid:str):
    p=project_access(actor(request),pid,manage=True)
    return {'keys':sorted(decrypt_env(p['env']))}

class EnvIn(BaseModel):
    values:dict[str,str]

@app.put('/api/projects/{pid}/environment')
async def environment_set(request:Request,pid:str,data:EnvIn):
    email=actor(request,True,'artifacts:write'); project_access(email,pid,manage=True)
    if len(data.values)>40 or len(json.dumps(data.values))>32000: raise HTTPException(400,'Environment too large')
    for k,v in data.values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',k) or k in ('PATH','HOME','NODE_OPTIONS','LD_PRELOAD','PORT','HOST','NODE_ENV') or '\x00' in v: raise HTTPException(400,'Invalid or reserved environment key')
    with db() as c: c.execute('UPDATE projects SET env=? WHERE id=?',(encrypt_env(data.values),pid))
    audit(email,'project.environment',pid)
    return {'keys':sorted(data.values),'note':'Applies to the next deployment. Existing release keeps its environment.'}

@app.get('/api/users')
async def users(request:Request):
    admin_actor(request)
    with db() as c: rows=[dict(r) for r in c.execute('SELECT email,role,enabled,created,permissions FROM users ORDER BY created')]
    return [r|{'permissions':json.loads(r['permissions']) if r['permissions'] is not None else list(SCOPES)} for r in rows]

class PermissionsIn(BaseModel):
    permissions:list[str]

@app.put('/api/users/{target}/permissions')
async def set_permissions(request:Request,target:str,data:PermissionsIn):
    email=admin_actor(request,True); target=target.strip().lower()
    if set(data.permissions)-set(SCOPES): raise HTTPException(400,'Unknown permission')
    values=[s for s in SCOPES if s in data.permissions]
    with db() as c:
        if not c.execute('SELECT 1 FROM users WHERE email=?',(target,)).fetchone(): raise HTTPException(404,'User not found')
        c.execute('UPDATE users SET permissions=? WHERE email=?',(json.dumps(values),target))
    audit(email,'user.permissions',target+':'+','.join(values))
    return {'email':target,'permissions':values,'note':'Revocations apply immediately; added OAuth scopes require new consent.'}

class UserIn(BaseModel):
    email:str=Field(min_length=3,max_length=200)
    enabled:bool=True

@app.post('/api/users')
async def invite(request:Request,data:UserIn):
    email=admin_actor(request,True); invited=data.email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',invited): raise HTTPException(400,'Invalid email')
    if invited==email and not data.enabled: raise HTTPException(400,'Cannot disable your own owner account')
    with db() as c:
        c.execute("INSERT INTO users(email,created,enabled) VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET enabled=excluded.enabled",(invited,now(),int(data.enabled)))
        if not data.enabled: c.execute('DELETE FROM tokens WHERE email=?',(invited,))
    audit(email,'user.enable' if data.enabled else 'user.disable',invited)
    return {'ok':True}

class MemberIn(BaseModel):
    email:str
    role:str=Field(pattern=r'^(viewer|deployer|maintainer|remove)$')

@app.get('/api/projects/{pid}/members')
async def members(request:Request,pid:str):
    project_access(actor(request),pid,manage=True)
    with db() as c: return [dict(r) for r in c.execute('SELECT email,role FROM members WHERE project=?',(pid,))]

@app.post('/api/projects/{pid}/members')
async def membership(request:Request,pid:str,data:MemberIn):
    email=admin_actor(request,True); project_access(email,pid); member=data.email.strip().lower(); user(member)
    with db() as c:
        if data.role=='remove': c.execute('DELETE FROM members WHERE project=? AND email=?',(pid,member))
        else: c.execute('INSERT INTO members VALUES(?,?,?) ON CONFLICT(project,email) DO UPDATE SET role=excluded.role',(pid,member,data.role))
    audit(email,'membership.'+data.role,pid+':'+member)
    return {'ok':True}

@app.get('/api/audit')
async def audit_list(request:Request):
    admin_actor(request)
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')]

@app.post('/api/projects/{pid}/uploads')
async def upload_ticket(request:Request,pid:str): return create_upload(actor(request,True),pid)

@app.put('/api/uploads/{uid}')
async def upload(request:Request,uid:str):
    raw=request.headers.get('authorization','')
    if not raw.startswith('Upload '): raise HTTPException(401,'Upload authorization required')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u=c.execute("SELECT * FROM uploads WHERE id=? AND token_hash=? AND status='pending' AND expires>?",(uid,digest(raw[7:]),now())).fetchone()
        if not u: raise HTTPException(403,'Invalid, expired or already-used upload ticket')
        project_access(u['email'],u['project'],write=True)
        c.execute("UPDATE uploads SET status='receiving' WHERE id=?",(uid,))
    p=DATA/'uploads'/f'{uid}.zip'; sha=hashlib.sha256(); size=0
    try:
        with p.open('xb') as h:
            async for chunk in request.stream():
                size+=len(chunk)
                if size>MAX_UPLOAD: raise HTTPException(413,'ZIP exceeds 50 MB')
                h.write(chunk); sha.update(chunk)
        with db() as c: c.execute("UPDATE uploads SET status='uploaded',size=?,sha256=? WHERE id=?",(size,sha.hexdigest(),uid))
    except BaseException:
        p.unlink(missing_ok=True)
        with db() as c: c.execute("UPDATE uploads SET status='failed' WHERE id=?",(uid,))
        raise
    return {'artifact_id':uid,'sha256':sha.hexdigest(),'bytes':size}

class FinalizeIn(BaseModel): sha256:str
@app.post('/api/uploads/{uid}/finalize')
async def finalize(request:Request,uid:str,data:FinalizeIn): return finalize_upload(actor(request,True),uid,data.sha256)

class DeployIn(BaseModel):
    artifact_id:str
    idempotency_key:str

@app.post('/api/projects/{pid}/deployments')
async def deploy(request:Request,pid:str,data:DeployIn): return create_deployment(actor(request,True),pid,data.artifact_id,data.idempotency_key)

@app.get('/api/projects/{pid}/deployments')
async def release_list(request:Request,pid:str):
    email=actor(request); project_access(email,pid)
    with db() as c: return [{k:v for k,v in dict(r).items() if k!='env_snapshot'} for r in c.execute('SELECT * FROM deployments WHERE project=? AND email=? ORDER BY created DESC,rowid DESC LIMIT 30',(pid,email))]

@app.get('/api/deployments/{did}')
async def release(request:Request,did:str): return deployment(actor(request),did)

@app.get('/api/deployments/{did}/logs')
async def logs(request:Request,did:str): return get_logs(actor(request),did)

class RollbackIn(BaseModel): idempotency_key:str
@app.post('/api/deployments/{did}/rollback')
async def rollback(request:Request,did:str,data:RollbackIn):
    email=actor(request,True); d=deployment(email,did)
    if d['status']!='healthy': raise HTTPException(400,'Release has not passed health checks')
    return create_deployment(email,d['project'],d['upload'],data.idempotency_key)

@app.get('/')
async def index(): return FileResponse(Path(__file__).parent/'static'/'index.html')

app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
app.mount('/',mcp.streamable_http_app())
