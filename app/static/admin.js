'use strict';
window.AdminOverview=(()=>{
 let offset=0,loading=false;
 const number=n=>Number(n||0).toLocaleString();
 const bytes=n=>{n=Number(n||0);const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?1:0)+' '+u[i]};
 const cell=(text,caption)=>{const td=elem('td');td.append(elem('div',text));if(caption)td.append(elem('div',caption,'caption'));return td};
 function empty(body,columns,text){const tr=elem('tr'),td=elem('td',text,'muted');td.colSpan=columns;tr.append(td);body.append(tr)}
 async function releases(){
  const q=new URLSearchParams({project:$('#admin-project').value,email:$('#admin-email').value.trim(),offset,limit:20});
  const data=await api('/api/admin/deployments?'+q);const body=$('#admin-deployments');body.replaceChildren();
  if(!data.rows.length)empty(body,6,'No deployments match this filter.');
  for(const d of data.rows){const tr=elem('tr'),status=elem('td'),ops=elem('td');status.append(elem('span',d.status,'badge '+d.status));ops.append(action('View logs',async()=>{
   const log=await api('/api/admin/deployments/'+d.id+'/logs');$('#admin-log-title').textContent=d.name+' · '+d.id.slice(0,10);$('#admin-log-text').textContent=log.text;$('#admin-log-panel').hidden=false;$('#admin-log-panel').scrollIntoView({behavior:'smooth',block:'nearest'});
  }));tr.append(cell(d.name,d.project),cell(d.email),cell(d.id.slice(0,10),d.active?'Current release':''),status,cell(when(d.created)),ops);body.append(tr)}
  $('#admin-page').textContent=data.total?`${offset+1}–${offset+data.rows.length} of ${number(data.total)}`:'0 deployments';$('#admin-prev').disabled=offset===0;$('#admin-next').disabled=offset+data.rows.length>=data.total;
 }
 async function load(){
  if(loading)return;loading=true;$('#admin-refresh').disabled=true;
  try{
   const hours=Number($('#admin-hours').value),project=$('#admin-project').value;
   const data=await api('/api/admin/overview?'+new URLSearchParams({hours,project}));
   const select=$('#admin-project');select.replaceChildren(new Option('All apps',''));for(const p of data.projects)select.add(new Option(p.name+' · '+p.id,p.id));select.value=project;
   const stats=$('#admin-stats');stats.replaceChildren();
   for(const [label,value] of [['REQUESTS',number(data.totals.requests)],['RESPONSE DATA',bytes(data.totals.bytes)],['4XX + 5XX',number(data.totals.errors4+data.totals.errors5)],['AVG RESPONSE',data.totals.avg_ms===null?'—':number(data.totals.avg_ms)+' ms']]){const item=elem('div');item.append(elem('span',label,'caption'),elem('strong',value));stats.append(item)}
   const fresh=data.last_collected&&data.server_time-data.last_collected<90;
   $('#admin-freshness').textContent=(fresh?'Collecting · updated ':data.last_collected?'Collector delayed · last update ':'Waiting for first collection')+(data.last_collected?when(data.last_collected):'');
   const body=$('#admin-apps');body.replaceChildren();
   for(const p of data.projects.filter(p=>!project||p.id===project)){const tr=elem('tr'),name=elem('td'),link=elem('a',p.name);link.href=p.url;link.target='_blank';link.rel='noopener noreferrer';name.append(link,elem('div',p.active_release?p.active_release.slice(0,10)+' · '+(p.status||'unknown'):'No release','caption'));
    const t=p.traffic,r=p.resources,recent=r&&data.server_time-r.updated<90;
    tr.append(name,cell(p.owner_email||'Unassigned',p.deployed_by?'Deployed: '+p.deployed_by:'Not deployed'),cell(number(t.requests)),cell(bytes(t.bytes)),cell(number(t.errors4)+' / '+number(t.errors5)),cell(t.avg_ms===null?'—':number(t.avg_ms)+' ms'),cell(r?number(r.cpu)+'% · '+bytes(r.memory):'No sample',r?(recent?r.state:'Stale sample')+' / limit '+bytes(r.memory_limit):''),cell(bytes(p.artifact_bytes)));body.append(tr)
   }
   if(!body.children.length)empty(body,8,'No apps yet.');
   const users=$('#admin-usage');users.replaceChildren();for(const u of data.users){const tr=elem('tr');tr.append(cell(u.email,(u.role==='owner'?'Admin':'Member')+(u.enabled?'':' · Disabled')),cell(number(u.logins)),cell(number(u.mcp_calls)),cell(number(u.deployments)),cell(number(u.requests)),cell(bytes(u.bytes)),cell(u.last_seen?when(u.last_seen):'—'));users.append(tr)}
   $('#admin-anonymous').textContent='Anonymous app requests: '+number(data.anonymous_requests)+'. Named users are not a count of all unique visitors.';
   const chart=$('#admin-chart');chart.replaceChildren();const step=hours<=48?3600:86400;const start=Math.floor(data.since/step)*step;const end=Math.floor(data.server_time/step)*step;const index=new Map(data.series.map(x=>[x.bucket,x]));const max=Math.max(1,...data.series.map(x=>x.requests));
   for(let at=start;at<=end;at+=step){const item=index.get(at)||{requests:0,bytes:0,errors:0};const bar=elem('div',undefined,'traffic-bar');bar.style.height=Math.max(2,item.requests/max*100)+'%';bar.title=when(at)+' · '+number(item.requests)+' requests · '+bytes(item.bytes)+' · '+item.errors+' errors';chart.append(bar)}
   chart.setAttribute('aria-label',number(data.totals.requests)+' requests from '+when(data.since)+' to '+when(data.server_time));
   $('#admin-chart-range').textContent=hours<=48?'Hourly':'Daily';$('#admin-chart-note').textContent='Collection began '+when(data.telemetry_started)+'. Hover over a bar for its count; empty periods have no collected data.';
   await releases();
  }finally{loading=false;$('#admin-refresh').disabled=false}
 }
 const run=()=>load().catch(e=>toast(e.message));
 $('#admin-refresh').onclick=run;$('#admin-hours').onchange=run;$('#admin-project').onchange=()=>{offset=0;run()};
 $('#admin-email').onchange=()=>{offset=0;releases().catch(e=>toast(e.message))};
 $('#admin-prev').onclick=()=>{offset=Math.max(0,offset-20);releases().catch(e=>toast(e.message))};$('#admin-next').onclick=()=>{offset+=20;releases().catch(e=>toast(e.message))};
 $('#admin-log-close').onclick=()=>$('#admin-log-panel').hidden=true;
 setInterval(()=>{if(!document.hidden&&!$('#admin-tab').hidden)run()},30000);
 return {load};
})();
