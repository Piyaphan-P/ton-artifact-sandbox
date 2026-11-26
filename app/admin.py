"""Read-only cross-project observability for platform admins."""
from fastapi import APIRouter,Request,Query
from .core import db,owner,now,APP_DOMAIN,get_logs,audit
from .auth import admin_actor

router=APIRouter(prefix='/api/admin')

def admin(request):
    return admin_actor(request)

@router.get('/overview')
async def overview(request:Request,hours:int=Query(24,ge=1,le=720),project:str=''):
    admin(request);since=(now()-hours*3600)//3600*3600
    with db() as c:
        projects=[dict(r) for r in c.execute('''SELECT p.id,p.name,p.owner_email,p.runtime,p.visibility,p.active_release,
            d.email deployed_by,d.status,d.created deployed_at,
            (SELECT count(*) FROM deployments WHERE project=p.id) deployment_count,
            (SELECT coalesce(sum(size),0) FROM uploads WHERE project=p.id) artifact_bytes
            FROM projects p LEFT JOIN deployments d ON d.id=p.active_release ORDER BY p.created DESC''')]
        traffic={r['project']:dict(r) for r in c.execute('''SELECT project,sum(requests) requests,sum(bytes) bytes,
            sum(errors4) errors4,sum(errors5) errors5,sum(duration_ms) duration_ms,
            count(DISTINCT nullif(email,'')) authenticated_users FROM traffic WHERE hour>=? GROUP BY project''',(since,))}
        resources={r['project']:dict(r) for r in c.execute('SELECT * FROM app_resources')}
        series=[dict(r) for r in c.execute('''SELECT (hour / ?) * ? bucket,sum(requests) requests,sum(bytes) bytes,
            sum(errors4+errors5) errors FROM traffic WHERE hour>=? AND (?='' OR project=?) GROUP BY bucket ORDER BY bucket''',
            (3600 if hours<=48 else 86400,3600 if hours<=48 else 86400,since,project,project))]
        users={r['email']:dict(r)|{'logins':0,'deployments':0,'mcp_calls':0,'requests':0,'bytes':0,'last_seen':None} for r in c.execute('SELECT email,role,enabled FROM users')}
        for r in c.execute("SELECT email,sum(action='login.google') logins,max(created) seen FROM audit WHERE created>=? GROUP BY email",(since,)):
            if r['email'] in users:users[r['email']].update(logins=r['logins'],last_seen=r['seen'])
        for r in c.execute('SELECT email,count(*) n FROM deployments WHERE created>=? GROUP BY email',(since,)):
            if r['email'] in users:users[r['email']]['deployments']=r['n']
        for r in c.execute("SELECT email,sum(calls) n,max(hour) seen FROM platform_usage WHERE hour>=? AND channel='mcp' GROUP BY email",(since,)):
            if r['email'] in users:
                users[r['email']]['mcp_calls']=r['n'];users[r['email']]['last_seen']=max(users[r['email']]['last_seen'] or 0,r['seen'])
        for r in c.execute("SELECT email,sum(requests) n,sum(bytes) b,max(hour) seen FROM traffic WHERE hour>=? AND email!='' GROUP BY email",(since,)):
            if r['email'] in users:
                users[r['email']].update(requests=r['n'],bytes=r['b']);users[r['email']]['last_seen']=max(users[r['email']]['last_seen'] or 0,r['seen'])
        meta={r['key']:r['value'] for r in c.execute('SELECT * FROM telemetry_meta')}
        anon=c.execute("SELECT coalesce(sum(requests),0) FROM traffic WHERE hour>=? AND email=''",(since,)).fetchone()[0]
    for p in projects:
        t=traffic.get(p['id'],{'requests':0,'bytes':0,'errors4':0,'errors5':0,'duration_ms':0,'authenticated_users':0})
        t['avg_ms']=round(t['duration_ms']/t['requests'],2) if t['requests'] else None
        p.update(traffic=t,resources=resources.get(p['id']),url='https://'+p['id']+'.'+APP_DOMAIN)
    selected=[p for p in projects if not project or p['id']==project]
    totals={k:sum(p['traffic'][k] for p in selected) for k in ('requests','bytes','errors4','errors5','duration_ms')}
    totals['avg_ms']=round(totals['duration_ms']/totals['requests'],2) if totals['requests'] else None
    return {'hours':hours,'since':since,'project':project,'projects':projects,'totals':totals,'series':series,
            'users':list(users.values()),'anonymous_requests':anon,'telemetry_started':meta.get('started'),
            'last_collected':meta.get('collected'),'retention_days':30,'server_time':now()}

@router.get('/deployments')
async def deployments(request:Request,project:str='',email:str='',limit:int=Query(30,ge=1,le=100),offset:int=Query(0,ge=0,le=100000)):
    admin(request)
    where="WHERE (?='' OR d.project=?) AND (?='' OR d.email=?)";args=(project,project,email,email)
    with db() as c:
        total=c.execute('SELECT count(*) FROM deployments d '+where,args).fetchone()[0]
        rows=[dict(r) for r in c.execute('''SELECT d.id,d.project,d.email,d.status,d.created,d.updated,
            p.name,p.owner_email,(p.active_release=d.id) active
            FROM deployments d JOIN projects p ON p.id=d.project '''+where+' ORDER BY d.created DESC,d.rowid DESC LIMIT ? OFFSET ?',(*args,limit,offset))]
    return {'total':total,'rows':rows,'offset':offset,'limit':limit}

@router.get('/deployments/{did}/logs')
async def logs(request:Request,did:str):
    email=admin(request);result=get_logs(email,did,admin=True)
    audit(email,'admin.logs.read',did)
    return result
