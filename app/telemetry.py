"""Bounded hourly analytics. No request paths, IPs, cookies or bearer tokens."""
import base64
import hashlib
import hmac
import json
import math
from .core import db,now,os

RETENTION=30*86400

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS traffic(hour INTEGER,project TEXT,email TEXT,requests INTEGER DEFAULT 0,bytes INTEGER DEFAULT 0,errors4 INTEGER DEFAULT 0,errors5 INTEGER DEFAULT 0,duration_ms REAL DEFAULT 0,PRIMARY KEY(hour,project,email));
    CREATE TABLE IF NOT EXISTS traffic_seen(id TEXT PRIMARY KEY,created INTEGER);
    CREATE TABLE IF NOT EXISTS traffic_cursor(container TEXT PRIMARY KEY,updated INTEGER);
    CREATE TABLE IF NOT EXISTS platform_usage(hour INTEGER,email TEXT,channel TEXT,calls INTEGER DEFAULT 0,PRIMARY KEY(hour,email,channel));
    CREATE TABLE IF NOT EXISTS app_resources(project TEXT PRIMARY KEY,updated INTEGER,cpu REAL,memory INTEGER,memory_limit INTEGER,state TEXT);
    CREATE TABLE IF NOT EXISTS telemetry_meta(key TEXT PRIMARY KEY,value INTEGER);
    ''')
    c.execute("INSERT OR IGNORE INTO telemetry_meta VALUES('started',?)",(now(),))

def principal(project,email):
    stamp=str(now()); encoded=base64.urlsafe_b64encode(email.encode()).decode().rstrip('=')
    body=project+'|'+encoded+'|'+stamp
    signature=hmac.new(os.environ['ENCRYPTION_KEY'].encode(),('traffic-v1|'+body).encode(),hashlib.sha256).hexdigest()
    return encoded+'.'+stamp+'.'+signature

def verify_principal(project,value,at):
    if not isinstance(value,str):return ''
    try:
        encoded,stamp,signature=value.split('.')
        body=project+'|'+encoded+'|'+stamp
        expected=hmac.new(os.environ['ENCRYPTION_KEY'].encode(),('traffic-v1|'+body).encode(),hashlib.sha256).hexdigest()
        # Requests can be long-lived; validate signature and avoid future claims.
        if not hmac.compare_digest(signature,expected) or not 0<=at-int(stamp)<=86400:return ''
        email=base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)).decode()
        return email if len(email)<=200 else ''
    except (ValueError,UnicodeError):return ''

def usage(email,channel):
    with db() as c:
        c.execute('INSERT INTO platform_usage VALUES(?,?,?,1) ON CONFLICT(hour,email,channel) DO UPDATE SET calls=calls+1',(now()//3600*3600,email,channel))

def ingest(project,container,lines,until):
    accepted=0
    with db() as c:
        for line in lines:
            try:
                x=json.loads(line)
                if x.get('kind')!='artifact-traffic':continue
                rid=x['id'];at=float(x['time']);status=int(x['status']);size=int(x['bytes']);duration=float(x['duration'])*1000
                if not isinstance(rid,str) or len(rid)!=32 or not all(z in '0123456789abcdef' for z in rid):continue
                if not math.isfinite(at) or not math.isfinite(duration) or not now()-RETENTION<=at<=now()+60 or not 100<=status<=599 or not 0<=size<=10**12 or not 0<=duration<=86400000:continue
                actor=x.get('actor','');email=verify_principal(project,actor,int(at))
                # A missing/forged marker is internal traffic, not an authenticated visitor.
                if not email:continue
                if email=='anonymous':email=''
                key=container+':'+rid
                if c.execute('INSERT OR IGNORE INTO traffic_seen VALUES(?,?)',(key,int(at))).rowcount!=1:continue
                c.execute('''INSERT INTO traffic VALUES(?,?,?,1,?,?,?,?) ON CONFLICT(hour,project,email) DO UPDATE SET
                    requests=requests+1,bytes=bytes+excluded.bytes,errors4=errors4+excluded.errors4,
                    errors5=errors5+excluded.errors5,duration_ms=duration_ms+excluded.duration_ms''',
                    (int(at)//3600*3600,project,email,size,int(400<=status<500),int(status>=500),duration))
                accepted+=1
            except (ValueError,TypeError,KeyError):continue
        c.execute('INSERT INTO traffic_cursor VALUES(?,?) ON CONFLICT(container) DO UPDATE SET updated=excluded.updated',(container,until))
        c.execute("INSERT INTO telemetry_meta VALUES('collected',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now(),))
    return accepted

def cleanup():
    with db() as c:
        c.execute('DELETE FROM traffic WHERE hour<?',(now()-RETENTION,))
        c.execute('DELETE FROM platform_usage WHERE hour<?',(now()-RETENTION,))
        c.execute('DELETE FROM traffic_seen WHERE created<?',(now()-86400,))

def collect(client):
    import datetime
    with db() as c:
        projects=[dict(r) for r in c.execute('SELECT id,active_release FROM projects WHERE active_release IS NOT NULL')]
        started=c.execute("SELECT value FROM telemetry_meta WHERE key='started'").fetchone()[0]
    for p in projects:
        try:
            gateway=client.containers.get('artifact-gateway-'+p['id'])
            with db() as c:cursor=c.execute('SELECT updated FROM traffic_cursor WHERE container=?',(gateway.id,)).fetchone()
            since=max(started,(cursor[0]-5 if cursor else now()-86400))
            until=now()
            lines=gateway.logs(since=datetime.datetime.fromtimestamp(since,datetime.timezone.utc),stdout=True,stderr=False,tail=10000).decode(errors='replace').splitlines()
            ingest(p['id'],gateway.id,lines,until)
            runtime=client.containers.get('artifact-release-'+p['active_release'])
            stats=runtime.stats(stream=False)
            current=stats.get('cpu_stats',{});previous=stats.get('precpu_stats',{})
            cpu_delta=current.get('cpu_usage',{}).get('total_usage',0)-previous.get('cpu_usage',{}).get('total_usage',0)
            system_delta=current.get('system_cpu_usage',0)-previous.get('system_cpu_usage',0)
            cores=current.get('online_cpus',1)
            cpu=max(0,cpu_delta/system_delta*cores*100) if system_delta>0 else 0
            mem=stats.get('memory_stats',{});cache=mem.get('stats',{}).get('inactive_file',mem.get('stats',{}).get('total_inactive_file',0))
            used=max(0,mem.get('usage',0)-cache)
            with db() as c:c.execute('INSERT OR REPLACE INTO app_resources VALUES(?,?,?,?,?,?)',(p['id'],now(),round(cpu,2),used,mem.get('limit',0),runtime.status))
        except Exception as exc:
            print('Telemetry collection retry:',p['id'],type(exc).__name__,flush=True)
    cleanup()
