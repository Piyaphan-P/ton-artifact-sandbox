'use strict';
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
let me=null, projects=[], selected=null, logId=null, busy=false;
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,6500)}
async function api(path,options={}){
  const headers={...options.headers};
  if(options.body && !(options.body instanceof Blob)){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body)}
  const r=await fetch(path,{...options,headers});
  const d=await r.json().catch(()=>({detail:'Unexpected server response'}));
  if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail||d));
  return d;
}
function elem(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e}
function action(label,fn,cls='secondary'){const b=elem('button',label,cls);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>toast(e.message)));return b}
function when(ts){return new Date(ts*1000).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'})}
function ownerUI(){ $$('.owner-only').forEach(e=>e.hidden=me?.role!=='owner') }
async function tab(name){
  ['projects','detail','connect','people','activity','admin'].forEach(t=>$('#'+t+'-tab').hidden=t!==name);
  $$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  $('#breadcrumb').textContent='Workspace / '+({projects:'Projects',detail:selected?.name||'Project',connect:'Connect MCP',people:'People & access',activity:'Activity',admin:'Admin overview'}[name]);
  if(name==='projects')await refreshProjects();
  if(name==='people')await refreshPeople();
  if(name==='activity')await refreshActivity();
  if(name==='admin')await window.AdminOverview.load();
}
async function refreshProjects(){
  projects=await api('/api/projects?include_drafts=true');$('#count-projects').textContent=projects.length;$('#count-live').textContent=projects.filter(p=>p.active_release).length;
  const grid=$('#project-grid');grid.replaceChildren();
  if(!projects.length){const box=elem('div',undefined,'empty');box.append(elem('h2','A place for your next idea.'),elem('p','Create your own project, then upload your first artifact.', 'muted'));grid.append(box)}
  for(const p of projects){
    const card=elem('article',undefined,'project-card');card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','Open '+p.name);
    const top=elem('div',undefined,'card-top');top.append(elem('span',p.runtime==='node'?'{ }':'</>','project-icon'),elem('span',p.active_release?'Live':'No release','badge '+(p.active_release?'healthy':'')));
    const bottom=elem('div',undefined,'card-bottom');bottom.append(elem('span',p.runtime==='node'?'Node.js':'Static website'),elem('span',p.visibility==='private'?'Private':'Public'));
    card.append(top,elem('h2',p.name),elem('div',new URL(p.url).host,'url'),bottom);
    const open=()=>openProject(p).catch(e=>toast(e.message));card.onclick=open;card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open()}};grid.append(card)
  }
}
async function openProject(p){selected=p;$('#project-title').textContent=p.name;$('#project-runtime').textContent=p.runtime==='node'?'NODE.JS APPLICATION':'STATIC WEBSITE';$('#project-link').textContent=new URL(p.url).host;$('#project-link').href=p.url;$('#log-panel').hidden=true;logId=null;await tab('detail');await refreshReleases()}
async function refreshReleases(){
  if(!selected)return;
  const rows=await api('/api/projects/'+selected.id+'/deployments');const body=$('#releases');body.replaceChildren();
  if(!rows.length){const tr=elem('tr');const td=elem('td','No releases yet. Upload a ZIP to deploy your first version.','muted');td.colSpan=4;tr.append(td);body.append(tr)}
  for(const d of rows){
    const tr=elem('tr');const status=elem('td');status.append(elem('span',d.status,'badge '+d.status));const acts=elem('td');
    acts.append(action('Logs',async()=>{logId=d.id;await refreshLogs();$('#log-panel').hidden=false;$('#log-panel').scrollIntoView({behavior:'smooth',block:'nearest'})}));
    if(d.status==='healthy')acts.append(action('Rollback',async()=>{if(!confirm('Deploy this artifact again using the current environment?'))return;await api('/api/deployments/'+d.id+'/rollback',{method:'POST',body:{idempotency_key:crypto.randomUUID()}});toast('Rollback queued');await refreshReleases()}));
    tr.append(elem('td',d.id.slice(0,10),'mono'),status,elem('td',when(d.created)),acts);body.append(tr)
  }
}
async function refreshLogs(){if(logId)$('#logs').textContent=(await api('/api/deployments/'+logId+'/logs')).text}
async function refreshPeople(){
  const users=await api('/api/users');$('#people').replaceChildren();
  for(const u of users){
    const tr=elem('tr'),td=elem('td'),perms=elem('td'),choices=[];
    for(const [value,label] of [['artifacts:read','Read'],['artifacts:write','Create / edit'],['deployments:write','Deploy / rollback']]){
      const wrap=elem('label',undefined,'permission-choice'),input=elem('input');input.type='checkbox';input.value=value;input.checked=u.permissions.includes(value);input.setAttribute('aria-label',u.email+' '+label);wrap.append(input,elem('span',label));perms.append(wrap);choices.push(input)
    }
    perms.append(action('Save permissions',async()=>{await api('/api/users/'+encodeURIComponent(u.email)+'/permissions',{method:'PUT',body:{permissions:choices.filter(x=>x.checked).map(x=>x.value)}});toast('Permissions saved. Added OAuth scopes need reconnect; revocations apply immediately.')}));
    if(u.email!==me.email)td.append(action(u.enabled?'Disable':'Enable',async()=>{await api('/api/users',{method:'POST',body:{email:u.email,enabled:!u.enabled}});await refreshPeople();toast('Access updated')}));
    tr.append(elem('td',u.email),elem('td',u.role==='owner'?'Admin':u.role),elem('td',u.enabled?'Enabled':'Disabled'),perms,td);$('#people').append(tr)
  }
}
async function refreshActivity(){const rows=await api('/api/audit');$('#activity').replaceChildren();for(const r of rows){const tr=elem('tr');tr.append(elem('td',when(r.created)),elem('td',r.email),elem('td',r.action),elem('td',r.target));$('#activity').append(tr)}}
async function settings(){
  const base='/api/projects/'+selected.id;
  const [env,members]=await Promise.all([api(base+'/environment'),api(base+'/members')]);
  $('#visibility-form select').value=selected.visibility;$('#env-keys').textContent='Configured keys: '+(env.keys.join(', ')||'none');$('#env-form textarea').value='';$('#members-list').textContent=members.map(m=>m.email+' · '+m.role).join(' | ')||'No additional members';$('#settings-dialog').showModal()
}
function onForm(id,fn){$(id).addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;if(b)b.disabled=true;try{await fn(new FormData(e.target),e.target)}catch(err){toast(err.message)}finally{if(b)b.disabled=false}})}
$$('.dismiss').forEach(b=>b.onclick=()=>b.closest('dialog').close());
$$('.nav-item').forEach(b=>b.onclick=()=>tab(b.dataset.tab).catch(e=>toast(e.message)));
$('#new-project').onclick=()=>$('#project-dialog').showModal();
$('#back-projects').onclick=()=>tab('projects').catch(e=>toast(e.message));
$('#deploy-open').onclick=()=>{$('#upload-status').textContent='';$('#deploy-dialog').showModal()};
$('#project-settings').onclick=()=>settings().catch(e=>toast(e.message));
$('#close-logs').onclick=()=>{$('#log-panel').hidden=true;logId=null};
$('#copy-mcp').onclick=()=>navigator.clipboard.writeText($('#mcp-url').textContent).then(()=>toast('MCP URL copied')).catch(()=>toast('Copy the endpoint displayed above'));
$('#logout').onclick=()=>api('/auth/logout',{method:'POST'}).then(()=>location.reload()).catch(e=>toast(e.message));
onForm('#project-form',async(f,form)=>{const p=await api('/api/projects',{method:'POST',body:{name:f.get('name'),id:f.get('id'),runtime:f.get('runtime')}});$('#project-dialog').close();form.reset();await openProject(p);toast('Project created')});
onForm('#invite-form',async(f,form)=>{await api('/api/users',{method:'POST',body:{email:f.get('email'),enabled:true}});form.reset();await refreshPeople();toast('Account whitelisted. They can now create and deploy their own projects.')});
onForm('#visibility-form',async f=>{const visibility=f.get('visibility');if(visibility==='public'&&!confirm('Anyone with the URL will be able to view this app. Continue?'))return;await api('/api/projects/'+selected.id+'/visibility',{method:'PUT',body:{visibility}});selected.visibility=visibility;toast('Visibility updated')});
onForm('#member-form',async f=>{await api('/api/projects/'+selected.id+'/members',{method:'POST',body:{email:f.get('email'),role:f.get('role')}});toast('Membership updated');$('#settings-dialog').close()});
onForm('#env-form',async f=>{const values=JSON.parse(f.get('values'));if(!values||Array.isArray(values)||typeof values!=='object')throw new Error('Enter a JSON object');await api('/api/projects/'+selected.id+'/environment',{method:'PUT',body:{values}});$('#env-form textarea').value='';toast('Environment saved. Deploy a new release to apply.');$('#settings-dialog').close()});
onForm('#deploy-form',async(f,form)=>{
  const file=f.get('file');if(!file?.size||file.size>50*1024*1024)throw new Error('Choose a ZIP file up to 50 MB');busy=true;
  try{
    $('#upload-status').textContent='Calculating checksum…';const bytes=await file.arrayBuffer();const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(b=>b.toString(16).padStart(2,'0')).join('');
    const t=await api('/api/projects/'+selected.id+'/uploads',{method:'POST'});
    $('#upload-status').textContent='Uploading artifact…';const upload=await fetch(t.upload_url,{method:'PUT',headers:t.headers,body:file});if(!upload.ok)throw new Error((await upload.json()).detail||'Upload failed');
    await api('/api/uploads/'+t.artifact_id+'/finalize',{method:'POST',body:{sha256:hash}});
    await api('/api/projects/'+selected.id+'/deployments',{method:'POST',body:{artifact_id:t.artifact_id,idempotency_key:crypto.randomUUID()}});
    $('#deploy-dialog').close();form.reset();toast('Release queued. Watch its status below.');await refreshReleases();
  }finally{busy=false}
});
async function boot(){
  try{me=await api('/api/me')}catch{ $('#login').hidden=false;return }
  $('#dashboard').hidden=false;$('#account').textContent=me.email;$('#account').title=me.email;$('#avatar').textContent=me.email[0].toUpperCase();$('#role').textContent=me.role==='owner'?'Admin':me.role;$('#mcp-url').textContent=me.mcp_url;ownerUI();await tab('projects');
  const h=await api('/health');$('#worker-status').textContent=h.worker_ready?'Worker ready':'Worker unavailable';
  setInterval(async()=>{if(document.hidden||busy)return;try{if(!$('#detail-tab').hidden){await refreshReleases();await refreshLogs()}const h=await api('/health');$('#worker-status').textContent=h.worker_ready?'Worker ready':'Worker unavailable'}catch{}},5000)
}
boot().catch(e=>toast(e.message));
