import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import HTTPException

DATA = Path(os.getenv('DATA_DIR', '/data'))
PORTAL = os.getenv('PORTAL_URL', 'https://artifacts.piyaphan-p.tech')
ISSUER = os.getenv('ISSUER_URL', 'https://auth.piyaphan-p.tech')
RESOURCE = os.getenv('MCP_URL', 'https://mcp.piyaphan-p.tech/mcp')
APP_DOMAIN = os.getenv('APP_DOMAIN', 'apps.piyaphan-p.tech')
SCOPES = ['artifacts:read', 'artifacts:write', 'deployments:write']
MAX_UPLOAD = 50 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024

def now(): return int(time.time())
def ident(): return secrets.token_hex(12)
def digest(s): return hashlib.sha256(s.encode()).hexdigest()

@contextmanager
def db():
    c = sqlite3.connect(DATA / 'platform.db', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()

def init():
    DATA.mkdir(parents=True, exist_ok=True)
    for sub in ('uploads', 'releases', 'routes', 'logs'): (DATA / sub).mkdir(exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, sub TEXT UNIQUE, role TEXT NOT NULL DEFAULT 'member', enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, runtime TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'private', active_release TEXT, env TEXT, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS members(project TEXT REFERENCES projects(id), email TEXT REFERENCES users(email), role TEXT NOT NULL, PRIMARY KEY(project,email));
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), email TEXT NOT NULL, token_hash TEXT NOT NULL, status TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, expires INTEGER NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), upload TEXT REFERENCES uploads(id), email TEXT NOT NULL, status TEXT NOT NULL, idem TEXT NOT NULL, container TEXT, error TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL, UNIQUE(project,email,idem));
        CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL, client TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '', resource TEXT NOT NULL DEFAULT '', project TEXT, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS flows(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, n INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS worker(id TEXT PRIMARY KEY, heartbeat INTEGER NOT NULL);
        ''')
        if 'env_snapshot' not in [r[1] for r in c.execute('PRAGMA table_info(deployments)')]:
            c.execute('ALTER TABLE deployments ADD COLUMN env_snapshot TEXT')
        if 'owner_email' not in [r[1] for r in c.execute('PRAGMA table_info(projects)')]:
            c.execute('ALTER TABLE projects ADD COLUMN owner_email TEXT')
        if 'permissions' not in [r[1] for r in c.execute('PRAGMA table_info(users)')]:
            c.execute('ALTER TABLE users ADD COLUMN permissions TEXT')
        c.execute('UPDATE users SET permissions=? WHERE permissions IS NULL',(json.dumps(SCOPES),))
        from .telemetry import setup
        setup(c)
        # Never guess ownership when historical deployments have mixed authors.
        c.execute('''UPDATE projects SET owner_email=(
            SELECT lower(min(email)) FROM deployments WHERE project=projects.id
            HAVING count(DISTINCT lower(email))=1)
            WHERE owner_email IS NULL AND EXISTS(SELECT 1 FROM deployments WHERE project=projects.id)''')
        c.execute('''UPDATE projects SET owner_email=(SELECT lower(email) FROM audit
            WHERE action='project.create' AND target=projects.id ORDER BY id LIMIT 1)
            WHERE owner_email IS NULL AND NOT EXISTS(SELECT 1 FROM deployments WHERE project=projects.id)''')
        for email in os.getenv('OWNER_EMAILS', '').lower().split(','):
            if email.strip():
                c.execute("INSERT INTO users(email,role,created) VALUES(?,'owner',?) ON CONFLICT(email) DO UPDATE SET role='owner',enabled=1", (email.strip(), now()))

def user(email):
    with db() as c: r = c.execute('SELECT * FROM users WHERE email=? AND enabled=1', (email,)).fetchone()
    if not r: raise HTTPException(403, 'Account is not on the active whitelist')
    return dict(r)

def owner(email):
    u = user(email)
    if u['role'] != 'owner': raise HTTPException(403, 'Owner access required')
    return u

def permissions(email):
    raw=user(email).get('permissions')
    return json.loads(raw) if raw is not None else list(SCOPES)

def require_permission(email,permission):
    if permission not in permissions(email): raise HTTPException(403,permission+' permission disabled by admin')

def project_access(email, pid, write=False, manage=False):
    u = user(email)
    if write: require_permission(email,'deployments:write')
    elif manage: require_permission(email,'artifacts:write')
    else: require_permission(email,'artifacts:read')
    with db() as c:
        p = c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
        m = c.execute('SELECT role FROM members WHERE project=? AND email=?', (pid,email)).fetchone()
    if not p: raise HTTPException(404, 'Project not found')
    owns=p['owner_email']==email
    if (write or manage) and not owns:
        raise HTTPException(403, 'Only the project owner email may deploy or modify this project')
    if not owns and u['role'] != 'owner' and not m:
        raise HTTPException(403, 'No permission for this project')
    return dict(p)

def audit(email, action, target):
    with db() as c: c.execute('INSERT INTO audit(email,action,target,created) VALUES(?,?,?,?)',(email,action,target,now()))

def issue(kind, email, ttl=3600, client='', scope='', resource='', project=None):
    value = secrets.token_urlsafe(36)
    with db() as c:
        c.execute('INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?)',(digest(value),kind,email,client,scope,resource,project,now()+ttl))
    return value

def token(value, kind):
    with db() as c: r=c.execute('SELECT * FROM tokens WHERE hash=? AND kind=? AND expires>?',(digest(value or ''),kind,now())).fetchone()
    if not r: raise HTTPException(401,'Session expired or invalid')
    user(r['email'])
    return dict(r)

def flow(kind, payload, ttl=600):
    fid=secrets.token_urlsafe(32)
    with db() as c: c.execute('INSERT INTO flows VALUES(?,?,?,?)',(digest(fid),kind,json.dumps(payload),now()+ttl))
    return fid

def read_flow(fid, kind, consume=False):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT payload FROM flows WHERE id=? AND kind=? AND expires>?',(digest(fid),kind,now())).fetchone()
        if not r: raise HTTPException(400,'Request expired or already used')
        if consume: c.execute('DELETE FROM flows WHERE id=?',(digest(fid),))
    return json.loads(r['payload'])

def rate(key, count=30, window=60):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM limits WHERE expires<?',(now(),))
        r=c.execute('SELECT n FROM limits WHERE key=?',(key,)).fetchone()
        if r and r['n']>=count: raise HTTPException(429,'Too many requests; try again later')
        c.execute('INSERT INTO limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET n=n+1',(key,now()+window))

def public_project(p):
    return {k:v for k,v in dict(p).items() if k!='env'} | {'url':f'https://{p["id"]}.{APP_DOMAIN}'}

def list_projects(email,include_drafts=False):
    require_permission(email,'artifacts:read')
    with db() as c:
        rows=c.execute('''SELECT p.* FROM projects p WHERE p.owner_email=? AND
            (? OR EXISTS(SELECT 1 FROM deployments d WHERE d.project=p.id AND d.email=?))
            ORDER BY p.created DESC''',(email,include_drafts,email)).fetchall()
    return [public_project(r) for r in rows]

def create_project(email,pid,name,runtime):
    require_permission(email,'artifacts:write')
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,39}',pid) or pid in ('www','auth','mcp','artifacts','admin','api'):
        raise HTTPException(400,'Invalid or reserved project name')
    if runtime not in ('static','node') or not 1<=len(name)<=80: raise HTTPException(400,'Invalid project settings')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]>=8: raise HTTPException(409,'This VPS is limited to 8 projects')
        if c.execute('SELECT 1 FROM projects WHERE id=?',(pid,)).fetchone(): raise HTTPException(409,'Project name is already used; choose a new name')
        c.execute('INSERT INTO projects(id,name,runtime,visibility,created,owner_email) VALUES(?,?,?,?,?,?)',(pid,name,runtime,'private',now(),email))
    audit(email,'project.create',pid)
    return public_project(project_access(email,pid))

def create_upload(email,pid):
    project_access(email,pid,write=True)
    rate('upload:'+email,20,3600)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT COUNT(*) FROM uploads WHERE project=?',(pid,)).fetchone()[0]>=30: raise HTTPException(409,'Project has reached 30 artifacts; ask the owner to archive this project')
        if c.execute('SELECT COUNT(*) FROM uploads').fetchone()[0]>=200: raise HTTPException(409,'Platform artifact quota reached')
        uid=ident(); key=secrets.token_urlsafe(32)
        c.execute('INSERT INTO uploads(id,project,email,token_hash,status,expires,created) VALUES(?,?,?,?,?,?,?)',(uid,pid,email,digest(key),'pending',now()+900,now()))
    audit(email,'upload.create',uid)
    return {'artifact_id':uid,'upload_url':PORTAL+'/api/uploads/'+uid,'method':'PUT','headers':{'Authorization':'Upload '+key,'Content-Type':'application/zip'},'max_bytes':MAX_UPLOAD,'expires_in':900}

def finalize_upload(email,uid,sha256):
    with db() as c: u=c.execute('SELECT * FROM uploads WHERE id=?',(uid,)).fetchone()
    if not u: raise HTTPException(404,'Artifact not found')
    project_access(email,u['project'],write=True)
    if u['email']!=email: raise HTTPException(403,'Artifact belongs to another user')
    if u['status'] not in ('uploaded','ready'): raise HTTPException(409,'Upload has not completed')
    if not secrets.compare_digest(u['sha256'] or '',sha256.lower()): raise HTTPException(400,'SHA-256 does not match')
    with db() as c: c.execute("UPDATE uploads SET status='ready' WHERE id=?",(uid,))
    return {'artifact_id':uid,'status':'ready','sha256':sha256}

def create_deployment(email,pid,uid,key):
    p=project_access(email,pid,write=True)
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,100}',key): raise HTTPException(400,'Use an idempotency key of 8–100 safe characters')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT owner_email FROM projects WHERE id=?',(pid,)).fetchone()
        if not current or current['owner_email']!=email: raise HTTPException(403,'Project belongs to another user')
        prev=c.execute('SELECT * FROM deployments WHERE project=? AND email=? AND idem=?',(pid,email,key)).fetchone()
        if prev:
            if prev['upload']!=uid: raise HTTPException(409,'Idempotency key belongs to a different artifact')
            return {k:v for k,v in dict(prev).items() if k!='env_snapshot'}
        u=c.execute("SELECT * FROM uploads WHERE id=? AND project=? AND status='ready'",(uid,pid)).fetchone()
        if not u: raise HTTPException(400,'Artifact is not ready for this project')
        if u['email']!=email: raise HTTPException(403,'Artifact belongs to another user')
        if c.execute("SELECT COUNT(*) FROM deployments WHERE status IN ('queued','building')").fetchone()[0]>=10: raise HTTPException(409,'Deployment queue full')
        did=ident()
        c.execute('INSERT INTO deployments(id,project,upload,email,status,idem,created,updated,env_snapshot) VALUES(?,?,?,?,?,?,?,?,?)',(did,pid,uid,email,'queued',key,now(),now(),p['env']))
    audit(email,'deployment.create',did)
    return deployment(email,did)

def deployment(email,did):
    with db() as c: d=c.execute('SELECT * FROM deployments WHERE id=?',(did,)).fetchone()
    if not d: raise HTTPException(404,'Deployment not found')
    if d['email']!=email: raise HTTPException(403,'Deployment belongs to another user')
    project_access(email,d['project'])
    return {k:v for k,v in dict(d).items() if k!='env_snapshot'}|{'url':f'https://{d["project"]}.{APP_DOMAIN}'}

def encrypt_env(values):
    return Fernet(os.environ['ENCRYPTION_KEY'].encode()).encrypt(json.dumps(values).encode()).decode()

def decrypt_env(text):
    return json.loads(Fernet(os.environ['ENCRYPTION_KEY'].encode()).decrypt(text.encode())) if text else {}

def redact_log(text,p):
    for value in decrypt_env(p.get('env')).values():
        if value: text=text.replace(value,'[REDACTED]')
    return re.sub(r'(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s]+',r'\1[REDACTED]',text)

def get_logs(email,did,admin=False):
    if admin:
        owner(email)
        with db() as c:
            row=c.execute('SELECT * FROM deployments WHERE id=?',(did,)).fetchone()
            if not row: raise HTTPException(404,'Deployment not found')
            d=dict(row);p=dict(c.execute('SELECT * FROM projects WHERE id=?',(d['project'],)).fetchone())
    else:
        d=deployment(email,did); p=project_access(email,d['project'])
    f=DATA/'logs'/f'{did}.log'
    if not f.exists(): return {'text':'Waiting for worker…'}
    chunks=[]
    for file in (f,DATA/'logs'/f'{did}.runtime.log'):
        if file.exists():
            with file.open('rb') as h:
                h.seek(max(0,file.stat().st_size-16000)); chunks.append(h.read().decode(errors='replace'))
    with db() as c: snapshot=c.execute('SELECT env_snapshot FROM deployments WHERE id=?',(did,)).fetchone()[0]
    return {'text':redact_log(redact_log('\n'.join(chunks),p),{'env':snapshot})}
