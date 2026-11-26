"""Single deployment worker. Only this service has Docker control access.

User code runs in a separate, resource-limited container with no Docker socket,
no control-plane mounts, no secrets at build time, and a project-only network.
"""
import io
import json
import os
import re
import shutil
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

import docker
from docker.types import IPAMConfig, IPAMPool, LogConfig

from .core import *

HOST_DATA=os.getenv('HOST_DATA_DIR','/opt/ton-artifacts/data')
NGINX=os.getenv('NGINX_IMAGE','nginx:stable-alpine')
NODE=os.getenv('NODE_IMAGE','node:24-alpine')
LABEL='tech.piyaphan.artifacts'
LOGCONFIG=LogConfig(type='json-file',config={'max-size':'1m','max-file':'2'})
client=None

def safe_member(name):
    path=PurePosixPath(name)
    if not name or '\\' in name or path.is_absolute() or '..' in path.parts or any(ord(x)<32 for x in name): raise ValueError('Archive contains an unsafe path')
    return path

def unpack_zip(source,dest):
    """Never follow ZIP symlinks or extract hidden credential/build directories."""
    with zipfile.ZipFile(source) as z:
        entries=z.infolist()
        if len(entries)>4000 or sum(x.file_size for x in entries)>MAX_EXPANDED: raise ValueError('Archive exceeds 4,000 files or 150 MB extracted')
        for item in entries:
            p=safe_member(item.filename)
            if any(x in ('.git','node_modules','.ssh') or x.startswith('.env') for x in p.parts): raise ValueError('Remove .git, node_modules, .ssh and .env files before uploading')
            mode=item.external_attr>>16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)): raise ValueError('Links and special files are not permitted')
            target=dest.joinpath(*p.parts)
            if item.is_dir(): target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with z.open(item) as src,target.open('xb') as dst: shutil.copyfileobj(src,dst,1024*1024)
                target.chmod(0o644)

def log(did,line):
    with (DATA/'logs'/f'{did}.log').open('a') as f: f.write(line.rstrip()+'\n')

def update(did,status,**extra):
    with db() as c:
        fields={'status':status,'updated':now()}|extra
        c.execute('UPDATE deployments SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),did))

def heartbeat():
    while True:
        try:
            with db() as c: c.execute("INSERT INTO worker VALUES('main',?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",(now(),))
        except Exception: pass
        time.sleep(5)

def network(pid):
    name='artifact-'+pid
    try: return client.networks.get(name)
    except docker.errors.NotFound:
        with db() as c:
            index=c.execute('SELECT rowid FROM projects WHERE id=?',(pid,)).fetchone()[0]
        return client.networks.create(name,driver='bridge',ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=f'172.29.{index}.0/24')]),options={'com.docker.network.bridge.name':'br-art-'+digest(pid)[:7]},labels={LABEL:'network','project':pid})

def restricted(**kwargs):
    return dict(detach=True,user='10001:10001',cap_drop=['ALL'],security_opt=['no-new-privileges:true'],pids_limit=128,nano_cpus=750_000_000,mem_limit='384m',memswap_limit='384m',read_only=True,log_config=LOGCONFIG,init=True,**kwargs)

def build_node(did,p,source,net):
    package=source/'package.json'
    if not package.exists(): raise ValueError('Node archive must contain package.json at the ZIP root')
    pkg=json.loads(package.read_text())
    if not pkg.get('scripts',{}).get('start'): raise ValueError('package.json needs a start script that listens on 0.0.0.0:$PORT')
    build=None
    try:
        log(did,'Installing dependencies and building in an isolated temporary container (timeout 300s).')
        opts=restricted(name='artifact-build-'+did,network=net.name,working_dir='/workspace',environment={'HOME':'/tmp','npm_config_cache':'/tmp/npm','CI':'true'},volumes={str(Path(HOST_DATA)/'releases'/did/'app'):{'bind':'/source','mode':'ro'}},tmpfs={'/workspace':'rw,nosuid,nodev,size=512m,uid=10001,gid=10001','/tmp':'rw,nosuid,nodev,size=128m,uid=10001,gid=10001'},labels={LABEL:'build','project':p['id']})
        opts.update(mem_limit='768m',memswap_limit='768m',nano_cpus=1_000_000_000)
        build=client.containers.run(NODE,['sh','-c','(cp -R /source/. /workspace/ && if [ -f package-lock.json ]; then npm ci --ignore-scripts --no-audit --no-fund; else npm install --ignore-scripts --no-audit --no-fund; fi && npm run build --if-present); echo $? > /tmp/build-result; sleep 360'],**opts)
        deadline=time.time()+300
        exit_code=None
        while time.time()<deadline:
            build.reload()
            if build.status!='running': raise ValueError('Build container stopped unexpectedly')
            result=build.exec_run(['cat','/tmp/build-result'])
            if result.exit_code==0:
                exit_code=int(result.output.strip()); break
            time.sleep(2)
        else: raise ValueError('Build exceeded 300 seconds')
        output=build.logs(tail=400).decode(errors='replace')
        log(did,output[-64000:])
        if exit_code!=0: raise ValueError('Build failed; see deployment logs')
        # Export from tmpfs with a strict disk limit. tar paths and links validated.
        archive=DATA/'releases'/did/'build.tar'; size=0
        # Docker's archive API may omit tmpfs contents; tar inside the running
        # container so the exported tree is the same one npm just built.
        stream=build.exec_run(['tar','-C','/','-cf','-','workspace'],stream=True).output
        with archive.open('wb') as f:
            for chunk in stream:
                size+=len(chunk)
                if size>MAX_EXPANDED: raise ValueError('Built app exceeds 150 MB')
                f.write(chunk)
        output_dir=source.parent/'built'; output_dir.mkdir()
        with tarfile.open(archive) as t:
            members=t.getmembers()
            if len(members)>50000 or sum(x.size for x in members)>MAX_EXPANDED: raise ValueError('Build output too large')
            for m in members:
                safe_member(m.name)
                if m.isdev() or m.isfifo(): raise ValueError('Build contains special files')
                if m.issym() or m.islnk():
                    target=(output_dir/m.name).parent/m.linkname if m.issym() else output_dir/m.linkname
                    if not target.resolve().is_relative_to((output_dir/'workspace').resolve()): raise ValueError('Build contains an escaping link')
            t.extractall(output_dir,filter='data')
        if not (output_dir/'workspace'/'package.json').is_file():
            raise ValueError('Build export is missing package.json')
        shutil.rmtree(source)
        (output_dir/'workspace').rename(source)
        output_dir.rmdir(); archive.unlink()
        for root,dirs,files in os.walk(source):
            os.chmod(root,0o755)
            for name in files:
                q=Path(root)/name
                if not q.is_symlink(): q.chmod(0o755 if q.stat().st_mode&0o111 else 0o644)
    finally:
        if build:
            try: build.remove(force=True)
            except docker.errors.NotFound: pass

STATIC_CONF='''pid /tmp/nginx.pid;
events { worker_connections 256; }
http {
 include /etc/nginx/mime.types;
 default_type application/octet-stream;
 access_log off; error_log /dev/stderr warn;
 client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
 server { listen 8080; root /site; index index.html;
   location = /health { return 200 'ok'; }
   location ~ /\\. { deny all; }
   location / { try_files $uri $uri/ /index.html; }
 }
}
'''

def gateway_config(upstream,port):
    return '''pid /tmp/nginx.pid;
events { worker_connections 256; }
http {
 log_format artifact escape=json '{"kind":"artifact-traffic","id":"$request_id","time":"$msec","status":"$status","bytes":"$body_bytes_sent","duration":"$request_time","actor":"$http_x_artifact_actor"}';
 map $http_x_artifact_actor $log_external { '' 0; default 1; }
 access_log /dev/stdout artifact if=$log_external; error_log /dev/stderr warn;
 client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
 map $http_upgrade $connection_upgrade { default upgrade; '' close; }
 server { listen 8080; client_max_body_size 10m;
 location / { proxy_pass http://'''+upstream+':'+str(port)+''';
 proxy_http_version 1.1; proxy_set_header Host $host;
 proxy_set_header X-Artifact-Actor "";
 proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade;
 proxy_set_header X-Forwarded-Proto https; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
 proxy_read_timeout 300s; proxy_buffering off;
 } }
}
'''

def route(pid,container,port):
    folder=DATA/'routes'/pid; folder.mkdir(exist_ok=True)
    tmp=folder/'nginx.conf.new'; tmp.write_text(gateway_config(container,port)); tmp.chmod(0o644)
    os.replace(tmp,folder/'nginx.conf')

def gateway(pid,net):
    name='artifact-gateway-'+pid
    try:
        g=client.containers.get(name)
        g.reload()
        if g.status!='running': g.start()
        result=g.exec_run(['nginx','-t'])
        if result.exit_code: raise ValueError('Gateway configuration validation failed')
        if g.exec_run(['nginx','-s','reload']).exit_code: raise ValueError('Gateway reload failed')
        return g
    except docker.errors.NotFound:
        r='artifact-'+pid
        labels={LABEL:'gateway','project':pid,'traefik.enable':'true',f'traefik.http.routers.{r}.rule':f'Host(`{pid}.{APP_DOMAIN}`)',f'traefik.http.routers.{r}.entrypoints':'websecure',f'traefik.http.routers.{r}.tls.certresolver':'letsencrypt',f'traefik.http.routers.{r}.middlewares':'artifact-auth@docker',f'traefik.http.services.{r}.loadbalancer.server.port':'8080','traefik.docker.network':net.name}
        opts=restricted(name=name,network=net.name,labels=labels,volumes={str(Path(HOST_DATA)/'routes'/pid):{'bind':'/etc/nginx','mode':'ro'}},tmpfs={'/tmp':'rw,nosuid,nodev,size=16m,uid=10001,gid=10001'},restart_policy={'Name':'unless-stopped'})
        opts.update(mem_limit='32m',memswap_limit='32m',nano_cpus=250_000_000)
        return client.containers.run(NGINX,['nginx','-g','daemon off;'],**opts)

def wait_healthy(container,runtime,timeout=60):
    deadline=time.time()+timeout
    while time.time()<deadline:
        container.reload()
        if container.status!='running': return False
        command=['wget','-q','-O','/dev/null','http://127.0.0.1:8080/health'] if runtime=='static' else ['node','-e',"fetch('http://127.0.0.1:3000/',{signal:AbortSignal.timeout(3000)}).then(r=>process.exit(r.status<400?0:1)).catch(()=>process.exit(1))"]
        try:
            if container.exec_run(command).exit_code==0: return True
        except docker.errors.APIError: pass
        time.sleep(2)
    return False

def execute(d):
    did=d['id']; pid=d['project']; runtime_container=None; switched=False; previous=None
    try:
        p=project_access(d['email'],pid,write=True)
        if not re.fullmatch('[a-f0-9]{24}',did) or not re.fullmatch('[a-z][a-z0-9-]{2,39}',pid): raise ValueError('Invalid deployment identity')
        if shutil.disk_usage(DATA).free<2*1024**3: raise ValueError('Less than 2 GB disk space available')
        folder=DATA/'releases'/did; source=folder/'app'; source.mkdir(parents=True)
        with db() as c: u=c.execute('SELECT * FROM uploads WHERE id=?',(d['upload'],)).fetchone()
        zip_path=DATA/'uploads'/f'{u["id"]}.zip'
        if hashlib.sha256(zip_path.read_bytes()).hexdigest()!=u['sha256']: raise ValueError('Stored artifact checksum mismatch')
        log(did,'Validating ZIP and extracting release '+did)
        unpack_zip(zip_path,source); net=network(pid)
        if p['runtime']=='node': build_node(did,p,source,net)
        elif not (source/'index.html').exists(): raise ValueError('Static ZIP must contain index.html at its root')
        # Recheck access immediately before publishing, not only when enqueued.
        p=project_access(d['email'],pid,write=True)
        volumes={str(Path(HOST_DATA)/'releases'/did/'app'):{'bind':'/site' if p['runtime']=='static' else '/app','mode':'ro'}}
        opts=restricted(name='artifact-release-'+did,network=net.name,labels={LABEL:'release','project':pid,'release':did},volumes=volumes,tmpfs={'/tmp':'rw,nosuid,nodev,size=64m,uid=10001,gid=10001'},restart_policy={'Name':'unless-stopped'})
        if p['runtime']=='static':
            config=folder/'nginx.conf'; config.write_text(STATIC_CONF); config.chmod(0o644)
            opts['volumes'][str(Path(HOST_DATA)/'releases'/did/'nginx.conf')]={'bind':'/etc/nginx/nginx.conf','mode':'ro'}
            opts.update(mem_limit='64m',memswap_limit='64m')
            runtime_container=client.containers.run(NGINX,['nginx','-g','daemon off;'],**opts); port=8080
        else:
            opts.update(environment=decrypt_env(d.get('env_snapshot'))|{'PORT':'3000','HOST':'0.0.0.0','NODE_ENV':'production','HOME':'/tmp','npm_config_cache':'/tmp/npm'},working_dir='/app')
            runtime_container=client.containers.run(NODE,['npm','start'],**opts); port=3000
        log(did,'Starting release and checking HTTP readiness.')
        update(did,'building',container=runtime_container.name)
        if not wait_healthy(runtime_container,p['runtime']):
            log(did,redact_log(runtime_container.logs(tail=150).decode(errors='replace'),{'env':d.get('env_snapshot')}))
            raise ValueError('App failed readiness check. Node start script must listen on 0.0.0.0:3000 and return 2xx/3xx at /.')
        project_access(d['email'],pid,write=True)
        with db() as c: previous=c.execute('SELECT active_release FROM projects WHERE id=?',(pid,)).fetchone()['active_release']
        route(pid,runtime_container.name,port); switched=True
        g=gateway(pid,net)
        if not wait_healthy_gateway(g): raise ValueError('Gateway did not become healthy')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE projects SET active_release=? WHERE id=?',(did,pid))
            c.execute("UPDATE deployments SET status='healthy',updated=? WHERE id=?",(now(),did))
        log(did,'Healthy. Traffic now points to this release. Previous artifact retained for rollback.')
        audit(d['email'],'deployment.healthy',did)
        if previous and previous!=did:
            try: client.containers.get('artifact-release-'+previous).remove(force=True)
            except docker.errors.NotFound: pass
    except Exception as exc:
        message=exc.detail if isinstance(exc,HTTPException) else str(exc)
        message=message[:1500]
        if switched and previous:
            try:
                route(pid,'artifact-release-'+previous,8080 if p['runtime']=='static' else 3000)
                gateway(pid,network(pid))
            except Exception: message+='; gateway recovery needs owner inspection'
        if runtime_container:
            try: runtime_container.remove(force=True)
            except Exception: pass
        log(did,'FAILED: '+message)
        update(did,'failed',error=message)
        audit(d['email'],'deployment.failed',did)

def wait_healthy_gateway(g):
    for _ in range(15):
        try:
            if g.exec_run(['wget','-q','-O','/dev/null','http://127.0.0.1:8080/']).exit_code==0: return True
        except docker.errors.APIError: pass
        time.sleep(1)
    return False

def main():
    global client
    init(); client=docker.from_env(timeout=30)
    threading.Thread(target=heartbeat,daemon=True).start()
    # Recover a deployment interrupted by worker restart; keep last committed route.
    with db() as c:
        interrupted=[dict(r) for r in c.execute("SELECT * FROM deployments WHERE status='building'")]
        c.execute("UPDATE deployments SET status='failed',error='Worker restarted during deployment',updated=? WHERE status='building'",(now(),))
        projects=[dict(r) for r in c.execute('SELECT * FROM projects WHERE active_release IS NOT NULL')]
    for p in projects:
        try:
            route(p['id'],'artifact-release-'+p['active_release'],8080 if p['runtime']=='static' else 3000)
            gateway(p['id'],network(p['id']))
        except Exception as e: print('Route recovery requires inspection:',p['id'],type(e).__name__,flush=True)
    for d in interrupted:
        for prefix in ('artifact-build-','artifact-release-'):
            try: client.containers.get(prefix+d['id']).remove(force=True)
            except docker.errors.NotFound: pass
    print('Artifact worker ready',flush=True)
    def analytics_loop():
        from .telemetry import collect
        while True:
            try: collect(client)
            except Exception as exc: print('Telemetry retry:',type(exc).__name__,flush=True)
            time.sleep(15)
    threading.Thread(target=analytics_loop,daemon=True).start()
    last_logs=0
    while True:
        if time.time()-last_logs>10:
            with db() as c: active=[dict(r) for r in c.execute('SELECT d.* FROM deployments d JOIN projects p ON p.active_release=d.id')]
            for r in active:
                try:
                    text=client.containers.get('artifact-release-'+r['id']).logs(tail=200).decode(errors='replace')[-32000:]
                    (DATA/'logs'/f'{r["id"]}.runtime.log').write_text(redact_log(text,{'env':r.get('env_snapshot')}))
                except docker.errors.NotFound: pass
            last_logs=time.time()
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            d=c.execute("SELECT * FROM deployments WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if d: c.execute("UPDATE deployments SET status='building',updated=? WHERE id=?",(now(),d['id']))
        if d: execute(dict(d))
        else: time.sleep(2)

if __name__=='__main__': main()
