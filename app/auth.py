import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .core import *
from .telemetry import principal

router=APIRouter()
COOKIE='__Host-artifact-platform'
GOOGLE_CALLBACK=PORTAL+'/auth/google/callback'
GOOGLE_KEYS=jwt.PyJWKClient('https://www.googleapis.com/oauth2/v3/certs')

def cookie_session(request):
    return token(request.cookies.get(COOKIE),'session')

def csrf(request):
    if request.headers.get('origin') != PORTAL:
        raise HTTPException(403,'Request must originate from the dashboard')

def actor(request,write=False,permission='deployments:write'):
    auth=request.headers.get('authorization','')
    if auth.startswith('Bearer '):
        t=token(auth[7:],'access')
        if t['resource']!=RESOURCE: raise HTTPException(401,'Wrong token audience')
        if 'artifacts:read' not in t['scope'].split(): raise HTTPException(403,'artifacts:read scope required')
        if write and permission:
            if permission not in t['scope'].split(): raise HTTPException(403,permission+' scope required; reconnect and consent')
            require_permission(t['email'],permission)
        return t['email']
    t=cookie_session(request)
    if write: csrf(request)
    if write and permission: require_permission(t['email'],permission)
    return t['email']

def admin_actor(request,write=False):
    # Harness consent grants artifact operations, never account administration.
    if request.headers.get('authorization'):
        raise HTTPException(403,'Admin actions require dashboard login')
    email=cookie_session(request)['email']
    if write: csrf(request)
    owner(email)
    return email

def safe_path(path):
    return path if path.startswith('/') and not path.startswith('//') and '\\' not in path and not any(ord(x)<32 for x in path) else '/'

def request_ip(request):
    # Traefik sets X-Real-IP; only loopback publishes this application port.
    return request.headers.get('x-real-ip', request.client.host if request.client else 'unknown')

def page(title,body):
    return HTMLResponse('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><link rel="stylesheet" href="'+PORTAL+'/static/style.css"></head><body class="auth-page"><main class="auth-card"><div class="brand-mark">a.</div><p class="eyebrow">PIYAPHAN / ARTIFACTS</p><h1>'+html.escape(title)+'</h1>'+body+'</main></body></html>')

@router.get('/auth/login')
async def google_login(request:Request,next:str='/'):
    rate('login:'+request_ip(request),20,60)
    client=os.getenv('GOOGLE_CLIENT_ID')
    if not client or not os.getenv('GOOGLE_CLIENT_SECRET'):
        return page('Google login is not configured','<p>The owner needs to configure Google OAuth. Access remains closed until setup is complete.</p>')
    nonce=secrets.token_urlsafe(32); binding=secrets.token_urlsafe(32)
    state=flow('google',{'next':safe_path(next),'nonce':nonce,'binding':digest(binding)})
    params={'client_id':client,'redirect_uri':GOOGLE_CALLBACK,'response_type':'code','scope':'openid email profile','state':state,'nonce':nonce,'prompt':'select_account'}
    r=RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params),302)
    r.set_cookie('__Host-google-flow',binding,secure=True,httponly=True,samesite='lax',max_age=600)
    return r

def verify_google(raw,nonce):
    key=GOOGLE_KEYS.get_signing_key_from_jwt(raw)
    claims=jwt.decode(raw,key.key,algorithms=['RS256'],audience=os.environ['GOOGLE_CLIENT_ID'],issuer=['https://accounts.google.com','accounts.google.com'],options={'require':['exp','iat','sub','iss','aud','nonce']})
    if not secrets.compare_digest(claims['nonce'],nonce) or claims.get('email_verified') is not True:
        raise ValueError('Invalid Google identity')
    return claims

@router.get('/auth/google/callback')
async def google_callback(request:Request,state:str='',code:str='',error:str=''):
    f=read_flow(state,'google')
    if not secrets.compare_digest(f['binding'],digest(request.cookies.get('__Host-google-flow',''))): raise HTTPException(400,'Login browser binding failed')
    read_flow(state,'google',True)
    if error or not code: return page('Login cancelled','<p>No access was granted.</p><a class="button" href="/">Return to dashboard</a>')
    async with httpx.AsyncClient(timeout=20) as h:
        result=await h.post('https://oauth2.googleapis.com/token',data={'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'code':code,'grant_type':'authorization_code','redirect_uri':GOOGLE_CALLBACK})
    if result.status_code!=200: raise HTTPException(400,'Google could not complete the login')
    try: claims=await asyncio.to_thread(verify_google,result.json()['id_token'],f['nonce'])
    except Exception: raise HTTPException(400,'Google identity verification failed')
    email=claims['email'].lower(); u=user(email)
    if u['sub'] and u['sub']!=claims['sub']: raise HTTPException(403,'Google account does not match the invited identity')
    with db() as c: c.execute('UPDATE users SET sub=? WHERE email=?',(claims['sub'],email))
    session=issue('session',email,ttl=12*3600)
    audit(email,'login.google',email)
    r=RedirectResponse(PORTAL+f['next'],302)
    r.set_cookie(COOKIE,session,max_age=12*3600,secure=True,httponly=True,samesite='lax')
    r.delete_cookie('__Host-google-flow',secure=True,httponly=True,samesite='lax')
    return r

@router.post('/auth/logout')
async def logout(request:Request):
    csrf(request)
    with db() as c: c.execute('DELETE FROM tokens WHERE hash=?',(digest(request.cookies.get(COOKIE,'')),))
    r=JSONResponse({'ok':True}); r.delete_cookie(COOKIE,secure=True,httponly=True,samesite='lax'); return r

@router.get('/.well-known/oauth-authorization-server')
async def metadata():
    return {'issuer':ISSUER,'authorization_endpoint':ISSUER+'/oauth/authorize','token_endpoint':ISSUER+'/oauth/token','registration_endpoint':ISSUER+'/oauth/register','revocation_endpoint':ISSUER+'/oauth/revoke','response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],'code_challenge_methods_supported':['S256'],'token_endpoint_auth_methods_supported':['none'],'scopes_supported':SCOPES,'authorization_response_iss_parameter_supported':True}

def validate_redirect(uri):
    if len(uri)>2048: return False
    try:
        u=urlsplit(uri)
        if u.fragment or u.username or u.password: return False
        if u.scheme=='http' and u.hostname in ('localhost','127.0.0.1','::1') and u.port:
            return True
        allowed=[x.strip() for x in os.getenv('OAUTH_ALLOWED_REDIRECTS','https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback').split(',') if x.strip()]
        return uri in allowed
    except ValueError: return False

@router.post('/oauth/register')
async def register(request:Request):
    rate('register:'+request_ip(request),10,3600)
    d=await request.json(); redirects=d.get('redirect_uris',[])
    if not isinstance(redirects,list) or not 1<=len(redirects)<=5 or not all(isinstance(x,str) and validate_redirect(x) for x in redirects): raise HTTPException(400,'Only loopback redirects and owner-approved HTTPS callbacks are allowed')
    if d.get('token_endpoint_auth_method','none')!='none': raise HTTPException(400,'Public PKCE clients only')
    cid='client_'+ident(); name=str(d.get('client_name','AI Harness'))[:80]
    with db() as c:
        c.execute('DELETE FROM clients WHERE created<? AND id NOT IN (SELECT client FROM tokens)',(now()-30*86400,))
        if c.execute('SELECT COUNT(*) FROM clients').fetchone()[0]>=1000: raise HTTPException(429,'Client registration capacity reached')
        c.execute('INSERT INTO clients VALUES(?,?,?,?)',(cid,name,json.dumps(redirects),now()))
    return JSONResponse({'client_id':cid,'client_name':name,'redirect_uris':redirects,'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','client_id_issued_at':now()},201)

@router.get('/oauth/authorize')
async def authorize(request:Request,client_id:str='',redirect_uri:str='',response_type:str='',code_challenge:str='',code_challenge_method:str='',state:str='',scope:str='artifacts:read artifacts:write deployments:write',resource:str=''):
    rate('authorize:'+request_ip(request),30,60)
    with db() as c: client=c.execute('SELECT * FROM clients WHERE id=?',(client_id,)).fetchone()
    if not client or redirect_uri not in json.loads(client['redirects']): raise HTTPException(400,'Unregistered client or redirect URI')
    if response_type!='code' or code_challenge_method!='S256' or not re.fullmatch('[A-Za-z0-9_-]{43}',code_challenge): raise HTTPException(400,'Authorization Code with S256 PKCE is required')
    if resource!=RESOURCE: raise HTTPException(400,'The resource parameter must be '+RESOURCE)
    scopes=scope.split()
    if not scopes or set(scopes)-set(SCOPES): raise HTTPException(400,'Invalid scope')
    if len(state)>1024: raise HTTPException(400,'State too long')
    fid=flow('consent',{'client':client_id,'name':client['name'],'redirect':redirect_uri,'challenge':code_challenge,'state':state,'scope':' '.join(scopes),'resource':resource})
    return RedirectResponse(PORTAL+'/oauth/consent?'+urlencode({'id':fid}),302)

@router.get('/oauth/consent')
async def consent(request:Request,id:str):
    f=read_flow(id,'consent')
    try: t=cookie_session(request)
    except HTTPException: return RedirectResponse('/auth/login?'+urlencode({'next':'/oauth/consent?'+urlencode({'id':id})}),302)
    granted=[s for s in f['scope'].split() if s in permissions(t['email'])]
    if not granted: raise HTTPException(403,'Admin has not granted any requested permissions')
    nonce=flow('consent-csrf',{'email':t['email'],'id':id,'session':digest(request.cookies[COOKIE]),'scope':' '.join(granted)})
    f['scope']=' '.join(granted)
    response=page('Connect your harness', '<p><strong>'+html.escape(f['name'])+'</strong> wants access to your Artifact projects.</p><p class="muted">Signed in as '+html.escape(t['email'])+'</p><p>Permissions: '+html.escape(f['scope'])+'</p><p>Only projects assigned to your account can be accessed.</p><form method="post" action="/oauth/consent"><input type="hidden" name="id" value="'+html.escape(id)+'"><input type="hidden" name="nonce" value="'+nonce+'"><div class="actions"><button name="decision" value="allow">Allow access</button><button class="secondary" name="decision" value="deny">Cancel</button></div></form>')
    # Browsers enforce form-action on the 303 callback destination as well.
    # Only allow the already-validated, registered client's callback origin.
    callback=urlsplit(f['redirect'])
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' "+PORTAL+"; frame-ancestors 'none'; base-uri 'none'; form-action 'self' "+callback.scheme+'://'+callback.netloc
    return response

@router.post('/oauth/consent')
async def consent_submit(request:Request):
    csrf(request); t=cookie_session(request); form=await request.form(); fid=str(form.get('id',''))
    n=read_flow(str(form.get('nonce','')),'consent-csrf',True)
    if any(n.get(k)!=v for k,v in {'email':t['email'],'id':fid,'session':digest(request.cookies[COOKIE])}.items()): raise HTTPException(400,'Consent verification failed')
    f=read_flow(fid,'consent',True)
    params={'state':f['state'],'iss':ISSUER}
    if form.get('decision')!='allow': params['error']='access_denied'
    else:
        f['scope']=' '.join(s for s in n['scope'].split() if s in permissions(t['email']))
        if not f['scope']: raise HTTPException(403,'Permissions were revoked')
        code=flow('code',f|{'email':t['email']},ttl=120); params['code']=code
        audit(t['email'],'oauth.consent',f['client'])
    return RedirectResponse(f['redirect']+('&' if '?' in f['redirect'] else '?')+urlencode(params),303)

def oauth_error(message):
    return JSONResponse({'error':'invalid_grant','error_description':message},400,headers={'Cache-Control':'no-store'})

@router.post('/oauth/token')
async def exchange(request:Request):
    rate('token:'+request_ip(request),60,60)
    f=await request.form(); client=str(f.get('client_id','')); resource=str(f.get('resource',''))
    if resource and resource!=RESOURCE: return oauth_error('Wrong resource')
    try:
        if f.get('grant_type')=='authorization_code':
            a=read_flow(str(f.get('code','')),'code')
            verifier=str(f.get('code_verifier',''))
            if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}',verifier): return oauth_error('Invalid PKCE verifier')
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            if client!=a['client'] or str(f.get('redirect_uri',''))!=a['redirect'] or not secrets.compare_digest(challenge,a['challenge']): return oauth_error('Code binding failed')
            user(a['email']); read_flow(str(f['code']),'code',True)
        elif f.get('grant_type')=='refresh_token':
            a=token(str(f.get('refresh_token','')),'refresh')
            if a['client']!=client or a['resource']!=RESOURCE: return oauth_error('Refresh binding failed')
            requested=str(f.get('scope',a['scope']))
            if set(requested.split())-set(a['scope'].split()): return oauth_error('Scope escalation denied')
            with db() as c:
                if c.execute('DELETE FROM tokens WHERE hash=? AND kind=?',(a['hash'],'refresh')).rowcount!=1: return oauth_error('Refresh token already used')
            a['scope']=requested
        else: return JSONResponse({'error':'unsupported_grant_type'},400)
    except HTTPException: return oauth_error('Grant expired, revoked, or invalid')
    a['scope']=' '.join(s for s in a['scope'].split() if s in permissions(a['email']))
    if not a['scope']: return oauth_error('Permissions were revoked')
    access=issue('access',a['email'],client=client,scope=a['scope'],resource=RESOURCE)
    refresh=issue('refresh',a['email'],ttl=30*86400,client=client,scope=a['scope'],resource=RESOURCE)
    return JSONResponse({'access_token':access,'refresh_token':refresh,'token_type':'Bearer','expires_in':3600,'scope':a['scope']},headers={'Cache-Control':'no-store','Pragma':'no-cache'})

@router.post('/oauth/revoke')
async def revoke(request:Request):
    f=await request.form()
    with db() as c: c.execute('DELETE FROM tokens WHERE hash=? AND client=?',(digest(str(f.get('token',''))),str(f.get('client_id',''))))
    return JSONResponse({})

@router.get('/internal/access')
async def access_check(request:Request):
    host=request.headers.get('x-forwarded-host','').split(':')[0]
    if not host.endswith('.'+APP_DOMAIN): raise HTTPException(403,'Unknown artifact host')
    pid=host[:-(len(APP_DOMAIN)+1)]
    with db() as c: p=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
    if not p or not p['active_release']: raise HTTPException(404,'Project not available')
    try:
        t=token(request.cookies.get('__Host-artifact-view'),'view')
        if t['project']!=pid: raise HTTPException(403,'Wrong project session')
        project_access(t['email'],pid)
        return JSONResponse({'ok':True},headers={'X-Artifact-Actor':principal(pid,t['email'])})
    except HTTPException:
        if p['visibility']=='public':
            return JSONResponse({'ok':True},headers={'X-Artifact-Actor':principal(pid,'anonymous')})
        path=safe_path(request.headers.get('x-forwarded-uri','/'))
        return RedirectResponse(PORTAL+'/access?'+urlencode({'project':pid,'path':path}),302)

@router.get('/access')
async def app_access(request:Request,project:str,path:str='/'):
    try: t=cookie_session(request)
    except HTTPException: return RedirectResponse('/auth/login?'+urlencode({'next':'/access?'+urlencode({'project':project,'path':safe_path(path)})}),302)
    project_access(t['email'],project)
    grant=flow('view-grant',{'email':t['email'],'project':project,'path':safe_path(path)},ttl=60)
    return RedirectResponse(f'https://{project}.{APP_DOMAIN}/_artifact/callback?'+urlencode({'code':grant}),302)

@router.get('/_artifact/callback')
async def view_callback(request:Request,code:str):
    f=read_flow(code,'view-grant')
    if request.url.hostname!=f['project']+'.'+APP_DOMAIN: raise HTTPException(400,'Wrong callback host')
    project_access(f['email'],f['project']); read_flow(code,'view-grant',True)
    value=issue('view',f['email'],ttl=3600,project=f['project'])
    r=RedirectResponse(f['path'],302); r.set_cookie('__Host-artifact-view',value,max_age=3600,secure=True,httponly=True,samesite='lax')
    return r
