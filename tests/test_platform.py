import base64
import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='artifact-test-')
os.environ['OWNER_EMAILS']='owner@example.com'
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
from app import core,auth
from app.main import app
from app.worker import unpack_zip

@pytest.fixture(scope='session')
def shared_client():
    with TestClient(app,base_url=core.PORTAL) as c:
        yield c

@pytest.fixture
def client(shared_client):
    c=shared_client
    with core.db() as db:
        for t in ('traffic','traffic_seen','traffic_cursor','platform_usage','app_resources','members','deployments','uploads','projects','tokens','flows','clients','audit','limits','users'): db.execute('DELETE FROM '+t)
    core.init()
    c.cookies.clear()
    c.cookies.set(auth.COOKIE,core.issue('session','owner@example.com'))
    c.headers['Origin']=core.PORTAL
    return c

def project(c,pid='test-app',runtime='static'):
    r=c.post('/api/projects',json={'id':pid,'name':'Test project','runtime':runtime})
    assert r.status_code==200,r.text
    return r.json()

def zip_bytes(files={'index.html':'<h1>Hello</h1>'}):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for name,value in files.items(): z.writestr(name,value)
    return b.getvalue()

def upload(c,pid='test-app'):
    data=zip_bytes();t=c.post(f'/api/projects/{pid}/uploads').json()
    r=c.put(t['upload_url'],headers=t['headers'],content=data);assert r.status_code==200,r.text
    uid=t['artifact_id']
    assert c.post(f'/api/uploads/{uid}/finalize',json={'sha256':hashlib.sha256(data).hexdigest()}).status_code==200
    return uid,t

def test_upload_checksum_one_use_and_idempotency(client):
    project(client);uid,t=upload(client)
    assert client.put(t['upload_url'],headers=t['headers'],content=b'bad').status_code==403
    assert client.post(f'/api/uploads/{uid}/finalize',json={'sha256':'bad'}).status_code==400
    body={'artifact_id':uid,'idempotency_key':'same-request-123'}
    a=client.post('/api/projects/test-app/deployments',json=body)
    b=client.post('/api/projects/test-app/deployments',json=body)
    assert a.status_code==200,a.text
    assert a.json()['id']==b.json()['id']
    body['artifact_id']='another-artifact'
    assert client.post('/api/projects/test-app/deployments',json=body).status_code==409

def test_membership_revocation_and_csrf(client):
    project(client)
    assert client.post('/api/users',json={'email':'friend@example.com'}).status_code==200
    member=core.issue('session','friend@example.com')
    client.cookies.set(auth.COOKIE,member)
    assert client.get('/api/projects').json()==[]
    assert client.post('/api/projects/test-app/uploads').status_code==403
    with core.db() as db: db.execute('INSERT INTO members VALUES(?,?,?)',('test-app','friend@example.com','viewer'))
    assert client.get('/api/projects').json()==[]
    assert client.post('/api/projects/test-app/uploads').status_code==403
    with core.db() as db: db.execute("UPDATE members SET role='deployer'")
    assert client.post('/api/projects/test-app/uploads').status_code==403
    assert client.put('/api/projects/test-app/visibility',json={'visibility':'public'}).status_code==403
    assert client.post('/api/projects/test-app/uploads',headers={'Origin':'https://evil.apps.piyaphan-p.tech'}).status_code==403
    with core.db() as db: db.execute("UPDATE users SET enabled=0 WHERE email='friend@example.com'")
    assert client.get('/api/projects').status_code==403

def test_encrypted_environment_not_readable(client):
    project(client,'node-app','node')
    value='sensitive-test-value'
    assert client.put('/api/projects/node-app/environment',json={'values':{'SUPABASE_KEY':value}}).status_code==200
    assert value not in client.get('/api/projects').text
    assert client.get('/api/projects/node-app/environment').json()=={'keys':['SUPABASE_KEY']}
    with core.db() as db: raw=db.execute('SELECT env FROM projects').fetchone()[0]
    assert value not in raw
    assert core.decrypt_env(raw)=={'SUPABASE_KEY':value}
    assert client.put('/api/projects/node-app/environment',json={'values':{'LD_PRELOAD':'evil'}}).status_code==400

def test_private_app_access_and_cross_project_token(client):
    project(client);project(client,'other-app')
    with core.db() as db: db.execute("UPDATE projects SET active_release='example'")
    headers={'X-Forwarded-Host':'test-app.'+core.APP_DOMAIN,'X-Forwarded-Uri':'/hello'}
    assert client.get('/internal/access',headers=headers,follow_redirects=False).status_code==302
    view=core.issue('view','owner@example.com',project='other-app')
    client.cookies.set('__Host-artifact-view',view)
    assert client.get('/internal/access',headers=headers,follow_redirects=False).status_code==302
    client.cookies.set('__Host-artifact-view',core.issue('view','owner@example.com',project='test-app'))
    assert client.get('/internal/access',headers=headers).status_code==200
    assert client.get('/api/projects',headers={'Host':'evil.apps.piyaphan-p.tech'}).status_code==400

def test_oauth_pkce_audience_code_replay_refresh(client):
    redirect='http://localhost:45321/callback'
    d=client.post('/oauth/register',json={'client_name':'Test harness','redirect_uris':[redirect]})
    assert d.status_code==201,d.text
    cid=d.json()['client_id']; verifier='a'*64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    payload={'client':cid,'name':'Test harness','redirect':redirect,'challenge':challenge,'state':'x','scope':'artifacts:read deployments:write','resource':core.RESOURCE,'email':'owner@example.com'}
    code=core.flow('code',payload)
    body={'grant_type':'authorization_code','client_id':cid,'redirect_uri':redirect,'code':code,'code_verifier':'wrong','resource':core.RESOURCE}
    assert client.post('/oauth/token',data=body).status_code==400
    body['code_verifier']=verifier;body['resource']='https://wrong.example/mcp'
    assert client.post('/oauth/token',data=body).status_code==400
    body['resource']=core.RESOURCE
    r=client.post('/oauth/token',data=body);assert r.status_code==200,r.text
    assert client.post('/oauth/token',data=body).status_code==400
    tokens=r.json(); refresh={'grant_type':'refresh_token','client_id':cid,'refresh_token':tokens['refresh_token']}
    assert client.post('/oauth/token',data=refresh).status_code==200
    assert client.post('/oauth/token',data=refresh).status_code==400
    bearer={'Authorization':'Bearer '+tokens['access_token']}
    assert client.get('/api/projects',headers=bearer).status_code==200
    with core.db() as db: db.execute("UPDATE users SET enabled=0 WHERE email='owner@example.com'")
    assert client.get('/api/projects',headers=bearer).status_code==403

def test_oauth_unsafe_redirects_and_authorization(client):
    assert client.post('/oauth/register',json={'redirect_uris':['https://evil.example/callback']}).status_code==400
    assert not auth.validate_redirect('http://127.0.0.1.evil.com:42/cb')
    assert not auth.validate_redirect('http://localhost:42/cb#fragment')
    assert auth.safe_path('//evil.example')=='/'
    assert client.get('/oauth/authorize',params={'client_id':'bad','redirect_uri':'https://evil.example'},follow_redirects=False).status_code==400

def test_mcp_auth_tools_and_scope(client):
    project(client)
    uid,_=upload(client)
    core.create_deployment('owner@example.com','test-app',uid,'mcp-read-test')
    headers={'Host':'mcp.piyaphan-p.tech','Accept':'application/json, text/event-stream'}
    msg={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}
    assert client.post('/mcp',headers=headers,json=msg).status_code==401

    access=core.issue('access','owner@example.com',scope='artifacts:read',resource=core.RESOURCE)
    headers['Authorization']='Bearer '+access
    r=client.post('/mcp',headers=headers,json=msg);assert r.status_code==200,r.text
    assert len(r.json()['result']['tools'])==8
    msg={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'projects_list','arguments':{}}}
    r=client.post('/mcp',headers=headers,json=msg);assert r.status_code==200,r.text
    assert 'test-app' in r.text
    msg['params']={'name':'artifacts_create_upload','arguments':{'project_id':'test-app'}}
    r=client.post('/mcp',headers=headers,json=msg)
    assert r.json()['result']['isError'] is True
    bad=core.issue('access','owner@example.com',scope='artifacts:read',resource='wrong')
    headers['Authorization']='Bearer '+bad
    assert client.post('/mcp',headers=headers,json=msg).status_code==401

def test_mcp_discovery_requests_deployment_consent(client):
    metadata=client.get('/.well-known/oauth-protected-resource/mcp').json()
    assert metadata['resource']==core.RESOURCE
    assert metadata['scopes_supported']==['artifacts:read','artifacts:write','deployments:write']
    auth=client.get('/.well-known/oauth-authorization-server').json()
    assert auth['scopes_supported']==metadata['scopes_supported']
    r=client.post('/mcp',headers={'Host':'mcp.piyaphan-p.tech','Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
    assert r.status_code==401
    assert 'scope="artifacts:read artifacts:write deployments:write"' in r.headers['WWW-Authenticate']
    assert '/.well-known/oauth-protected-resource/mcp' in r.headers['WWW-Authenticate']

@pytest.mark.parametrize('name',['../escape','/absolute','test/../../escape','test\\escape','.env','.git/config','node_modules/lib/index.js'])
def test_zip_rejects_unsafe_paths(tmp_path,name):
    src=tmp_path/'bad.zip';src.write_bytes(zip_bytes({name:'bad'}));dest=tmp_path/'out';dest.mkdir()
    with pytest.raises(ValueError): unpack_zip(src,dest)

def test_zip_rejects_symlinks(tmp_path):
    src=tmp_path/'bad.zip'
    with zipfile.ZipFile(src,'w') as z:
        info=zipfile.ZipInfo('link');info.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(info,'/etc/passwd')
    with pytest.raises(ValueError): unpack_zip(src,tmp_path/'out')

def test_zip_works(tmp_path):
    src=tmp_path/'good.zip';src.write_bytes(zip_bytes());out=tmp_path/'out';out.mkdir();unpack_zip(src,out)
    assert (out/'index.html').read_text()=='<h1>Hello</h1>'

def test_consent_browser_policy_allows_registered_callback(client):
    project(client)
    fid=core.flow('consent',{'client':'test','name':'Harness','redirect':'http://127.0.0.1:45321/callback','challenge':'a'*43,'state':'state','scope':'artifacts:read deployments:write','resource':core.RESOURCE})
    response=client.get('/oauth/consent',params={'id':fid})
    assert response.status_code==200
    assert response.headers['Referrer-Policy']=='strict-origin-when-cross-origin'
    assert "form-action 'self' http://127.0.0.1:45321" in response.headers['Content-Security-Policy']
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    import re
    nonce=re.search(r'name="nonce" value="([^"]+)"',response.text).group(1)
    response=client.post('/oauth/consent',data={'id':fid,'nonce':nonce,'decision':'allow'},follow_redirects=False)
    assert response.status_code==303
    assert response.headers['location'].startswith('http://127.0.0.1:45321/callback?')
    assert client.post('/oauth/consent',data={'id':fid,'nonce':nonce,'decision':'allow'},follow_redirects=False).status_code==400

def test_email_owned_projects_and_cross_user_writes(client):
    project(client); uid,_=upload(client)
    own=core.create_deployment('owner@example.com','test-app',uid,'owner-deploy')
    client.post('/api/users',json={'email':'friend@example.com'})
    with core.db() as c:
        c.execute("INSERT INTO members VALUES('test-app','friend@example.com','maintainer')")
        c.execute("UPDATE deployments SET status='healthy' WHERE id=?",(own['id'],))
    client.cookies.set(auth.COOKIE,core.issue('session','friend@example.com'))
    assert client.get('/api/projects').json()==[]
    assert client.get('/api/projects?include_drafts=true').json()==[]
    assert client.post('/api/projects/test-app/uploads').status_code==403
    assert client.put('/api/projects/test-app/environment',json={'values':{'KEY':'hijack'}}).status_code==403
    assert client.post('/api/deployments/'+own['id']+'/rollback',json={'idempotency_key':'cross-rollback'}).status_code==403
    assert client.get('/api/deployments/'+own['id']+'/logs').status_code==403
    assert client.get('/api/projects/test-app/deployments').json()==[]
    r=project(client,'friend-app');assert r['owner_email']=='friend@example.com'
    assert client.get('/api/projects').json()==[]
    assert [p['id'] for p in client.get('/api/projects?include_drafts=true').json()]==['friend-app']
    friend_uid,_=upload(client,'friend-app')
    friend_deploy=core.create_deployment('friend@example.com','friend-app',friend_uid,'friend-deploy')
    assert [p['id'] for p in client.get('/api/projects').json()]==['friend-app']
    core.create_deployment('friend@example.com','friend-app',friend_uid,'friend-redeploy')
    client.cookies.set(auth.COOKIE,core.issue('session','owner@example.com'))
    assert [p['id'] for p in client.get('/api/projects').json()]==['test-app']
    assert client.post('/api/projects/friend-app/uploads').status_code==403
    assert client.post('/api/deployments/'+friend_deploy['id']+'/rollback',json={'idempotency_key':'admin-cannot-steal'}).status_code==403
    with core.db() as c:c.execute('UPDATE uploads SET email=? WHERE id=?',('friend@example.com',uid))
    with pytest.raises(core.HTTPException) as exc:core.create_deployment('owner@example.com','test-app',uid,'cross-artifact')
    assert exc.value.status_code==403

def test_owner_migration_preserves_single_author_and_blocks_ambiguity(client):
    project(client);uid,_=upload(client)
    core.create_deployment('owner@example.com','test-app',uid,'migration-original')
    with core.db() as c:c.execute('UPDATE projects SET owner_email=NULL')
    core.init()
    assert core.project_access('owner@example.com','test-app')['owner_email']=='owner@example.com'
    with core.db() as c:
        c.execute("INSERT INTO deployments(id,project,upload,email,status,idem,created,updated) VALUES('legacy-other','test-app',?,'friend@example.com','failed','legacy',1,1)",(uid,))
        c.execute('UPDATE projects SET owner_email=NULL')
    core.init()
    with pytest.raises(core.HTTPException) as exc:core.project_access('owner@example.com','test-app',write=True)
    assert exc.value.status_code==403
    assert core.list_projects('owner@example.com')==[]

def test_admin_permissions_limit_existing_tokens_and_new_consent(client):
    client.post('/api/users',json={'email':'friend@example.com'})
    token=core.issue('access','friend@example.com',scope=' '.join(core.SCOPES),resource=core.RESOURCE)
    headers={'Authorization':'Bearer '+token}
    assert client.put('/api/users/friend@example.com/permissions',json={'permissions':['artifacts:read']}).status_code==200
    assert client.get('/api/projects',headers=headers).status_code==200
    assert client.post('/api/projects',headers=headers,json={'id':'blocked-app','name':'Blocked','runtime':'static'}).status_code==403
    assert client.put('/api/users/friend@example.com/permissions',headers=headers,json={'permissions':core.SCOPES}).status_code==403
    assert client.put('/api/users/friend@example.com/permissions',json={'permissions':['artifacts:read','artifacts:write']}).status_code==200
    project_result=client.post('/api/projects',headers=headers,json={'id':'friend-app','name':'Friend','runtime':'static'})
    assert project_result.status_code==200,project_result.text
    assert client.post('/api/projects/friend-app/uploads',headers=headers).status_code==403
    assert client.put('/api/users/friend@example.com/permissions',json={'permissions':core.SCOPES}).status_code==200
    assert client.post('/api/projects/friend-app/uploads',headers=headers).status_code==200
    # Old read-only grants cannot silently gain deploy scope after an admin change.
    readonly=core.issue('access','friend@example.com',scope='artifacts:read',resource=core.RESOURCE)
    assert client.post('/api/projects/friend-app/uploads',headers={'Authorization':'Bearer '+readonly}).status_code==403
    # Revocation affects previously-issued full-scope tokens immediately.
    client.put('/api/users/friend@example.com/permissions',json={'permissions':[]})
    assert client.get('/api/projects',headers=headers).status_code==403
    assert client.post('/api/projects/friend-app/uploads',headers=headers).status_code==403
    assert client.put('/api/users/friend@example.com/permissions',json={'permissions':['madeup:admin']}).status_code==400


def test_consent_never_grants_permissions_beyond_admin_allowance(client):
    client.post('/api/users',json={'email':'friend@example.com'})
    client.put('/api/users/friend@example.com/permissions',json={'permissions':['artifacts:read']})
    client.cookies.set(auth.COOKIE,core.issue('session','friend@example.com'))
    fid=core.flow('consent',{'client':'test','name':'Harness','redirect':'http://localhost:45321/callback','challenge':'a'*43,'state':'state','scope':' '.join(core.SCOPES),'resource':core.RESOURCE})
    response=client.get('/oauth/consent',params={'id':fid})
    assert 'Permissions: artifacts:read</p>' in response.text
    assert 'deployments:write' not in response.text
    import re
    from urllib.parse import urlsplit,parse_qs
    nonce=re.search(r'name="nonce" value="([^"]+)"',response.text).group(1)
    response=client.post('/oauth/consent',data={'id':fid,'nonce':nonce,'decision':'allow'},follow_redirects=False)
    code=parse_qs(urlsplit(response.headers['location']).query)['code'][0]
    assert core.read_flow(code,'code')['scope']=='artifacts:read'

def test_admin_can_observe_other_owner_but_cannot_deploy(client):
    client.post('/api/users',json={'email':'friend@example.com'})
    client.cookies.set(auth.COOKIE,core.issue('session','friend@example.com'))
    project(client,'friend-app');uid,_=upload(client,'friend-app')
    d=core.create_deployment('friend@example.com','friend-app',uid,'admin-observe-test')
    value='super-private-runtime-value'
    with core.db() as c:
        c.execute('UPDATE projects SET env=? WHERE id=?',(core.encrypt_env({'KEY':value}),'friend-app'))
        c.execute('UPDATE deployments SET env_snapshot=? WHERE id=?',(core.encrypt_env({'KEY':value}),d['id']))
    (core.DATA/'logs'/f'{d["id"]}.log').write_text('App started '+value)
    for path in ('/api/admin/overview','/api/admin/deployments','/api/admin/deployments/'+d['id']+'/logs'):
        assert client.get(path).status_code==403
    client.cookies.set(auth.COOKIE,core.issue('session','owner@example.com'))
    assert client.get('/api/projects').json()==[]
    r=client.get('/api/admin/overview');assert r.status_code==200
    assert r.json()['projects'][0]['owner_email']=='friend@example.com'
    assert value not in r.text
    r=client.get('/api/admin/deployments');assert r.json()['rows'][0]['email']=='friend@example.com'
    r=client.get('/api/admin/deployments/'+d['id']+'/logs');assert r.status_code==200
    assert value not in r.text and '[REDACTED]' in r.text
    assert client.post('/api/projects/friend-app/uploads').status_code==403
    assert client.get('/api/admin/overview?hours=721').status_code==422

def test_traffic_deduplicates_signed_identity_and_aggregates(client):
    from app import telemetry
    project(client)
    at=core.now()
    def event(rid,who='owner@example.com',status=200):
        return json.dumps({'kind':'artifact-traffic','id':rid*32,'time':str(at),'status':str(status),'bytes':'1024','duration':'0.125','actor':telemetry.principal('test-app',who)})
    lines=[event('a'),event('b','anonymous',404),event('c','owner@example.com',500)]
    assert telemetry.ingest('test-app','container1',lines,at)==3
    assert telemetry.ingest('test-app','container1',lines,at)==0
    assert telemetry.ingest('other-app','container2',lines,at)==0,'Signed identity must be bound to app'
    fake=json.loads(event('d'));fake['actor']='forged@example.com'
    assert telemetry.ingest('test-app','container1',[json.dumps(fake),'bad json'],at)==0
    telemetry.usage('owner@example.com','mcp')
    d=client.get('/api/admin/overview').json()
    assert d['totals']['requests']==3 and d['totals']['bytes']==3072
    assert d['totals']['errors4']==1 and d['totals']['errors5']==1
    assert d['totals']['avg_ms']==125
    u=next(x for x in d['users'] if x['email']=='owner@example.com')
    assert u['requests']==2 and u['mcp_calls']==1
    assert d['anonymous_requests']==1
    with core.db() as c:
        c.execute('INSERT INTO traffic VALUES(?,?,?,?,?,?,?,?)',(at-40*86400,'test-app','',1,2,0,0,1))
    telemetry.cleanup()
    with core.db() as c:assert c.execute('SELECT count(*) FROM traffic WHERE hour<?',(at-30*86400,)).fetchone()[0]==0

def test_forwardauth_provides_verified_traffic_marker(client):
    from app import telemetry
    project(client)
    with core.db() as c:c.execute("UPDATE projects SET active_release='sample'")
    headers={'X-Forwarded-Host':'test-app.'+core.APP_DOMAIN,'X-Forwarded-Uri':'/'}
    client.cookies.set('__Host-artifact-view',core.issue('view','owner@example.com',project='test-app'))
    r=client.get('/internal/access',headers=headers)
    assert telemetry.verify_principal('test-app',r.headers['X-Artifact-Actor'],core.now())=='owner@example.com'
    client.cookies.clear()
    with core.db() as c:c.execute("UPDATE projects SET visibility='public'")
    r=client.get('/internal/access',headers=headers)
    assert telemetry.verify_principal('test-app',r.headers['X-Artifact-Actor'],core.now())=='anonymous'


def test_harness_tokens_cannot_administer_accounts(client):
    project(client)
    bearer=core.issue('access','owner@example.com',scope=' '.join(core.SCOPES),resource=core.RESOURCE)
    headers={'Authorization':'Bearer '+bearer}
    for path in ('/api/users','/api/audit','/api/admin/overview','/api/admin/deployments'):
        assert client.get(path,headers=headers).status_code==403
    assert client.post('/api/users',json={'email':'attacker@example.com'},headers=headers).status_code==403
    assert client.put('/api/users/owner@example.com/permissions',json={'permissions':[]},headers=headers).status_code==403
    assert client.post('/api/projects/test-app/members',json={'email':'owner@example.com','role':'viewer'},headers=headers).status_code==403
    assert client.get('/api/projects',headers=headers).status_code==200
    assert client.get('/api/users').status_code==200


def test_rest_reads_require_consented_read_scope(client):
    project(client)
    bearer=core.issue('access','owner@example.com',scope='deployments:write',resource=core.RESOURCE)
    assert client.get('/api/projects',headers={'Authorization':'Bearer '+bearer}).status_code==403
