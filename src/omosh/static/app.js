// SPDX-FileCopyrightText: 2026 CommanderTurtle
// SPDX-License-Identifier: AGPL-3.0-only
import {mountEditor,freshValue} from './structured.js';
const $ = (selector, root=document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const main = $('#main');
let state, csrf='', view='overview', config, catalog, schema, cliCommands, terminal, eventSource;
let cliSelection='status', cliValues={};
let selectedSession='', selectedDoc='about/overview', configMode='fields', configFilter='', optionFilter='', docFilter='';
let toastTimer;
let renderGeneration=0, configDirty=false;
const titles = {overview:'Overview',environments:'Environments',sessions:'Sessions',configuration:'Configuration',bridge:'Agent bridge',manual:'Reference',updates:'Versions',activity:'Activity',cli:'OpenShell CLI'};
const vendor = () => import('/assets/vendor/vendor.js');

async function api(url, {method='GET',body,...rest}={}) {
  const headers = {'X-Omosh-CSRF':csrf,...rest.headers};
  if (body !== undefined && typeof body !== 'string' && !(body instanceof Blob)) {headers['Content-Type']='application/json';body=JSON.stringify(body);}
  const result = await fetch(url,{method,headers,body,credentials:'same-origin'});
  if (!result.ok) {
    const content = await result.text();
    let message=content;try{message=JSON.parse(content).detail ?? content;}catch{}
    throw new Error(message);
  }
  return result.headers.get('content-type')?.includes('application/json') ? result.json() : result.text();
}
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6500);}
async function refreshState(){state=await api('/api/state');csrf=state.csrf;if(catalog&&catalog.version!==state.manual.version){catalog=null;schema=null;cliCommands=null;}updateChrome();}
function updateChrome(){
  $('#env-count').textContent=state.environments.length;$('#session-count').textContent=state.sessions.length;
  $('#instance-version').textContent=`OpenShell ${state.pin.version}`;
  const badge=$('#gateway-badge');badge.textContent=state.gateway.state;badge.className=`badge ${state.gateway.state==='reachable'?'ok':''}`;
  $('#footer-state').textContent=state.state_dir;
}
async function action(name){const job=await api(`/api/actions/${name}`,{method:'POST',body:{}});state.jobs.push(job);toast(`${name} started. Output is in Activity.`);if(view==='activity')render();}
async function preference(values){state.preferences=await api('/api/preferences',{method:'PUT',body:values});}
function heading(title,description,buttons=''){return `<div class="page-heading"><div><span class="eyebrow">LOCAL WORKSPACE</span><h1>${title}</h1><p>${description}</p></div><div class="actions">${buttons}</div></div>`;}
function empty(glyph,title,text,button=''){return `<div class="empty"><div class="glyph">${glyph}</div><h3>${title}</h3><p>${text}</p>${button}</div>`;}
function card(title,subtitle,content,extra=''){return `<section class="card"><div class="card-head"><div><h2>${title}</h2>${subtitle?`<p>${subtitle}</p>`:''}</div>${extra}</div>${content}</section>`;}
function countCard(label,value,foot){return `<div class="stat"><span>${label}</span><strong>${value}</strong><small>${foot}</small></div>`;}
function checks(){return state.doctor.length ? state.doctor.map(c=>`<div class="check-row"><span class="${c.ok===null?'check-unknown':c.ok?'check-ok':'check-fail'}">${c.ok===null?'·':c.ok?'✓':'×'}</span><div><b>${esc(c.name)}</b><small>${esc(c.detail)}</small></div></div>`).join('') : '<p class="muted">Run Check to inspect this host. Checks run on request.</p>';}
function overview(){
  const ready=state.setup||{};
  const steps=[['Host prerequisites','Docker Engine 28+, Compose, and supported Linux isolation.','Check','check',ready.host,true],['Pinned OpenShell CLI','Checksum-verified binary. Existing system installations stay separate.','Install CLI','install-cli',ready.cli,ready.host],['Workload image','A non-root image with bash, Python, and tmux included.','Build image','build-workload',ready.workload,ready.host],['Gateway & credentials','Initialize mTLS and start the owned Compose project.','Start gateway','start',ready.gateway,ready.cli&&ready.host]];
  return heading('Local gateway','Configuration, environments, and persistent shell sessions.',`<button data-action="stop">Stop gateway</button><button class="primary" data-action="start">▶ Start gateway</button>`)+
    `<div class="stats">${countCard('Environments',state.environments.length,'Named OpenShell sandboxes')}${countCard('Shell sessions',state.sessions.length,'Persistent through disconnects')}${countCard('OpenShell',esc(state.pin.version),'CLI + gateway + runtime pinned')}${countCard('Reference',state.manual.pages,`${state.manual.options} indexed option entries`)}</div>`+
    `<div class="grid">${card('Setup',`${steps.filter(s=>s[4]).length} of 4 ready · saved per instance`,steps.map((s,i)=>`<div class="step ${s[4]?'step-ready':''}"><span class="step-number">${s[4]?'✓':i+1}</span><div><h3>${s[0]}</h3><p>${s[1]}</p></div><button data-action="${s[3]}" ${s[5]?'':'disabled title="Complete the host check and CLI setup first"'}>${s[4]&&s[3]!=='check'?'Run again':s[2]} ↗</button></div>`).join('')+`<div class="card-body"><div class="note">The gateway controls the host Docker daemon. Review the setup acknowledgement before starting. Workloads enter through OpenShell’s isolation boundary.</div><div class="actions space"><button data-command="new-environment" ${ready.gateway?'':'disabled'}>Create environment →</button><button data-command="new-session" ${state.environments.length?'':'disabled'}>Create session →</button></div></div>`)}
    <div>${card('Runtime boundary','One control plane. Separate sandbox sessions.',`<div class="card-body boundary"><div class="boundary-node"><span>Browser / agent</span><small>localhost</small></div><div class="boundary-arrow">↓ authenticated API</div><div class="boundary-node"><span>omosh → OpenShell</span><small>mTLS</small></div><div class="boundary-arrow">↓ supervised exec</div><div class="boundary-node"><span>Sandbox → tmux</span><small>isolated workload</small></div></div>`)}
    <div class="section-gap">${card('Host checks','Refresh explicitly when the host changes.',`<div class="card-body">${checks()}</div>`,`<button class="quiet small" data-action="check">Check ↗</button>`)}</div></div></div>`+
    `<div class="section-gap">${card('Reference desk','Versioned alongside the runtime.',`<div class="card-body thumbnail-grid"><button class="thumbnail" data-doc="about/architecture"><img alt="OpenShell architecture" src="/reference/images/openshell-system-architecture.svg"><span>Architecture & trust boundaries ↗</span></button><button class="thumbnail" data-doc="how-it-works/policies/overview"><img alt="Sandbox enforcement" src="/reference/images/openshell-sandbox-enforcement.svg"><span>Policies & enforcement ↗</span></button></div>`)}</div>`;
}
function environments(){return heading('Environments','Independent sandboxes with their own image, policy, and resources.','<button class="primary" data-command="new-environment">+ New environment</button>')+
  card('Sandboxes',`${state.environments.length} registered in this instance`,state.environments.length?state.environments.map(e=>`<div class="list-item"><div><strong>${esc(e.name)}</strong><small>${esc(e.workspace)} / ${esc(e.image)}</small></div><div class="actions"><button class="small" data-env="${e.id}" data-env-action="get">Inspect</button><button class="small" data-env="${e.id}" data-env-action="start">Start</button><button class="small" data-env="${e.id}" data-env-action="stop">Stop</button><button class="small" data-new-session="${e.id}">+ Session</button></div></div>`).join(''):empty('▦','Create an environment','Build the workload image in Setup, start the gateway, then create a named sandbox.','<button data-command="new-environment">+ New environment</button>'))+
  '<p class="note section-gap">Workspaces group sandboxes and provider access. Create additional workspaces through the OpenShell CLI panel. Custom images must include bash, tmux, and Python 3 for the command bridge.</p>';}
function sessionView(){
  if(!state.sessions.some(s=>s.id===selectedSession))selectedSession=state.sessions[0]?.id??'';
  const session=state.sessions.find(s=>s.id===selectedSession);
  return heading('Shell sessions','Each named session retains its shell state inside its sandbox.','<button class="primary" data-command="new-session">+ New session</button>')+
  (session?`<div class="terminal-tabs">${state.sessions.map(s=>`<button class="${s.id===selectedSession?'active':''}" data-session="${s.id}">${esc(s.name)} <small> / ${esc(state.environments.find(e=>e.id===s.environment)?.name)}</small></button>`).join('')}</div><div class="terminal-panel"><div class="terminal-head"><span class="status-dot"></span><code>${esc(session.name)}</code><span class="muted" id="terminal-state">Detached</span><div class="actions"><button class="quiet small" data-command="copy-terminal">Copy selection</button><button class="quiet small" data-command="connect-terminal">Connect</button><button class="quiet small" data-command="detach-terminal">Detach</button><button class="quiet small danger" data-close-session="${session.id}">Close session</button></div></div><div class="terminal-stage" id="terminal"><div class="terminal-placeholder">Connect to attach the live tmux view.</div></div></div><div class="session-command"><label>Send a command<textarea id="shell-command" spellcheck="false" placeholder="pwd&#10;export PROJECT=/sandbox"></textarea></label><div class="row"><small>Plain shell text or a complete Markdown code block. One command at a time per session.</small><button class="primary" data-command="send-command">PUT command →</button></div></div>`:
  card('Sessions','Persistent shell state, detachable terminal views.',empty('›_','Choose a sandbox first','Sessions run inside an OpenShell environment. Closing the browser detaches the view; closing a session ends its shell.','<button data-command="new-session">+ New session</button>')));
}
function configView(){
  const options=schema.filter(o=>(o.path+' '+o.help).toLowerCase().includes(optionFilter.toLowerCase()));
  const active=config.fields.filter(f=>f.path.toLowerCase().includes(configFilter.toLowerCase()));
  return heading('Gateway configuration','Draft locally, preflight with OpenShell, then restart to apply.',`<button data-command="reload-config">Reload</button><button data-command="validate-config">Validate draft</button><button data-command="save-config">Save draft</button><button class="primary" data-command="preflight-config">Native preflight</button>`)+
  `<div class="config-layout"><section class="card"><div class="card-head"><h2>Settings index</h2><span class="chip">${schema.length}</span></div><div class="search"><input id="option-search" placeholder="Find a setting…" value="${esc(optionFilter)}" aria-label="Find a setting"></div><div class="catalog-list">${options.map(o=>`<button class="catalog-option" data-setting="${esc(o.path)}"><code>${esc(o.path.replace('openshell.',''))}</code><small>${esc(o.help.slice(0,95)||o.type)}</small></button>`).join('')}</div><div class="card-body"><button class="small" data-command="add-setting">+ Add setting</button></div></section>
  <section class="card"><div class="subnav"><button data-config-mode="fields" class="${configMode==='fields'?'active':''}">Fields</button><button data-config-mode="toml" class="${configMode==='toml'?'active':''}">Full TOML</button><button data-config-mode="deployment" class="${configMode==='deployment'?'active':''}">Deployment</button></div>
  ${configMode==='fields'?`<div class="search"><input id="field-search" placeholder="Filter configured fields…" value="${esc(configFilter)}" aria-label="Filter configured fields"></div>${active.map(f=>`<div class="field"><label><code>${esc(f.path.replace(/^openshell\./,''))}</code><small>${esc(f.type)} · <button class="quiet small" data-edit-field="${esc(f.path)}">Edit</button></small></label><code>${esc(JSON.stringify(f.value))}</code></div>`).join('')}`:
  configMode==='toml'?`<textarea class="config-editor" id="toml" aria-label="Gateway TOML" spellcheck="false">${esc(config.text)}</textarea>`:
  `<div class="card-body"><h3>Owned Docker deployment</h3><p>Gateway and init containers use one Compose project. State is bind-mounted at the same absolute host/container path. The host socket remains outside workload containers.</p><label class="inline"><input type="checkbox" id="telemetry" ${state.telemetry?'checked':''}>Enable upstream anonymous telemetry (applies at the next gateway start)</label><div class="note">Gateway, supervisor, and sandbox images are pinned by digest. The managed local deployment requires Docker and mTLS; the complete upstream manual also covers Podman, Kubernetes, VM, OIDC, and external extensions.</div><pre>${esc(state.state_dir)}
├─ gateway.toml
├─ compose.yaml
├─ gateway/       gateway database & PKI
├─ client/        isolated CLI registration
├─ bin/           pinned CLI versions
└─ omosh.sqlite3  sessions, jobs & token hashes</pre></div>`}</section></div><div id="validation" class="section-gap"></div>`;
}
function bridgeView(){const sample=selectedSession||state.sessions[0]?.id||'SESSION_ID';return heading('Agent bridge','Stateful shell commands over an authenticated localhost connection.','<button class="primary" data-command="new-token">+ Session token</button>')+
 `<div class="bridge-grid">${card('Command protocol','PUT submits. DONE returns Markdown.',`<div class="card-body"><div class="endpoint"><b>GET</b><code>/v1/sessions</code></div><div class="endpoint"><b>PUT</b><code>/v1/sessions/{id}/commands</code></div><div class="endpoint"><b>DONE</b><code>/v1/jobs/{id}?wait=30</code></div><div class="endpoint"><b>DELETE</b><code>/v1/jobs/{id}</code></div><p class="space">GET is also accepted for DONE. A pending result returns its current status; completion includes exit code, stdout, and stderr in separate code blocks.</p><div class="note">A bridge token grants access only to the sessions selected when it is created. It cannot change gateway settings or read operator state.</div></div>`)}
 ${card('Pipe a command','Use a session token in the Authorization header.',`<div class="card-body"><pre id="bridge-example">${esc(`curl -sS -X PUT '${location.origin}/v1/sessions/${sample}/commands' \
  -H "Authorization: Bearer $OMOSH_TOKEN" \
  -H 'Content-Type: text/plain' \
  --data-binary @command.md

curl -sS -X DONE '${location.origin}/v1/jobs/JOB_ID?wait=30' \
  -H "Authorization: Bearer $OMOSH_TOKEN"`)}</pre><button class="small" data-copy="bridge-example">Copy example</button></div>`)}</div>
 <div class="section-gap">${card('Session tokens','Raw tokens are displayed once. Stored credentials are hashed.',state.tokens.length?state.tokens.map(t=>`<div class="list-item"><div><strong>${esc(t.label)}</strong><small>${t.sessions.length} session${t.sessions.length===1?'':'s'} · ${new Date(t.created*1000).toLocaleDateString()}</small></div><button class="small danger" data-revoke="${esc(t.id)}">Revoke</button></div>`).join(''):empty('⇄','Create a scoped token','Create a named session, then issue a token for that session.'))}</div>`;}
async function updatesView(){
  const candidate=state.updates?.candidate,staged=state.updates?.staged,report=state.updates?.report;
  const {marked,DOMPurify}=await vendor();
  const matching=Boolean(report&&candidate&&report.candidate===candidate.commit);
  const status=matching?({ready:'Ready to apply',current:'Current release',reviewing:'Reviewing contracts',blocked:'Compatibility changes',error:'Staging interrupted',interrupted:'Staging cancelled'}[report.status]||'Compatibility report'):'';
  const findings=matching?`<div class="${report.status==='ready'||report.status==='current'?'note':'alert'}"><h3>${status}</h3>${[...report.breaking,...report.notes,...(report.error?[report.error]:[])].map(x=>`<p>${esc(x)}</p>`).join('')}</div>`:'';
  return heading('Pinned versions','Updates are checked on request. Existing settings stay in place.')+
    `<div class="grid">${card('OpenShell release','The installed contract is recorded alongside verified artifacts.',`<div class="card-body"><span class="version-label">${esc(state.pin.version)}</span><p class="mono">${esc(state.pin.commit.slice(0,12))}</p><div class="actions"><button data-action="check-update">Check latest stable</button><button data-action="stage-update" ${candidate?'':'disabled'}>Stage candidate</button><button class="primary" data-command="apply-update" ${staged?'':'disabled'}>Apply staged update</button></div><div class="note space">Staging verifies checksums, required CLI flags, and native gateway configuration. The report stays with the exact release and draft.</div><p class="space">Applying stops owned environments and changes all three runtime pins together. Existing sandboxes keep their original runtime; recreate them to move it forward. Extensions retain their independent service contracts.</p></div>`)}
    ${card('Artifact lock','Immutable image references.',`<div class="card-body">${Object.entries(state.pin.images).map(([k,v])=>`<label>${esc(k)}<pre>${esc(v)}</pre></label>`).join('')}</div>`)}</div>`+
    `<div class="section-gap">${card('Candidate',candidate?`${candidate.version} · ${candidate.commit.slice(0,12)}${staged?.commit===candidate.commit?' · staged':''}`:'Check latest stable to load a candidate.',candidate?`<div class="card-body"><p><a href="${esc(candidate.release_url)}" target="_blank" rel="noopener noreferrer">Release source ↗</a> · Published ${esc(candidate.published)}</p>${findings}<details class="space"><summary>Release notes</summary><article class="manual-content">${DOMPurify.sanitize(marked.parse(candidate.notes||'No release notes provided.'))}</article></details></div>`:'<div class="card-body"><p>Candidate metadata and compatibility findings appear here.</p></div>')}</div>`;
}
function activityView(){return heading('Activity','Action results and bridge commands. Nothing runs while this view is idle.')+
 card('Job journal','Persisted in the local state directory.',state.jobs.length?[...state.jobs].reverse().map(j=>`<details class="job" data-job-id="${j.id}"><summary class="list-item"><span><span class="job-title">${esc(j.title)}</span><small class="job-time">${esc(j.id.slice(0,10))} · ${new Date(j.created*1000).toLocaleTimeString()}</small></span><span class="badge ${j.status==='succeeded'?'ok':j.status==='failed'?'error':''}">${esc(j.status)}</span></summary><div class="job-output">${['queued','running'].includes(j.status)?`<button class="small danger" data-cancel="${j.id}">Cancel</button>`:''}${j.error?`<pre class="error">${esc(j.error)}</pre>`:''}${j.stdout!==undefined?`<h3>stdout</h3><pre>${esc(j.stdout)}</pre>`:''}${j.stderr?`<h3>stderr</h3><pre>${esc(j.stderr)}</pre>`:''}${j.exit_code!==undefined?`<small>Exit code ${j.exit_code}${j.truncated?' · display output bounded':''}</small>`:''}</div></details>`).join(''):empty('◷','No actions yet','Run a setup check or submit a command to see its result here.'));}
function cliView(){
  const command=cliCommands.find(c=>c.command.join(' ')===cliSelection)||cliCommands[0];
  const controls=command.fields.map(f=>{
    const value=cliValues[f.key]??'';const label=esc(f.flag||f.key);let control;
    if(f.type==='bool')return `<label class="inline"><input type="checkbox" data-cli-field="${f.key}" ${value?'checked':''}><span>${label}<small style="display:block">${esc(f.help)}</small></span></label>`;
    if(f.choices.length)control=`<select data-cli-field="${f.key}"><option value="">Upstream default${f.default?' · '+esc(f.default):''}</option>${f.choices.map(v=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
    else if(f.repeat)control=`<textarea data-cli-field="${f.key}" placeholder="${f.last?'One argument per line':'One value per line'}" spellcheck="false">${esc(value)}</textarea>`;
    else control=`<input data-cli-field="${f.key}" type="${f.type==='number'?'number':/secret|token|password/.test(f.key)?'password':'text'}" autocomplete="off" value="${esc(value)}" placeholder="${esc(f.default??(f.required?'Required':'Optional'))}">`;
    return `<label>${label}${f.required?' <span class="muted">· required</span>':''}${control}<small style="display:block;margin-top:6px">${esc(f.help)}</small></label>`;
  }).join('');
  return heading('OpenShell CLI',`${cliCommands.length} source-derived command forms. Native CLI validation remains authoritative.`)+
  card('Command builder','Explicit argument values, help, and conflict checks.',`<div class="card-body"><div class="form-grid"><label>Command<select id="cli-command">${cliCommands.map(c=>`<option value="${esc(c.command.join(' '))}" ${c===command?'selected':''}>${esc(c.command.join(' '))}</option>`).join('')}</select></label><label>Workspace<input id="cli-workspace" value="default"></label></div><p>${esc(command.help)}</p><div class="form-grid">${controls}</div><div class="actions"><button data-command="build-cli">Build argument array</button><button data-command="cli-help">Show native help</button></div><div class="note space">Fields are read from the pinned clap declarations. Template conflicts, repeated flags, positional values, and trailing command arguments stay explicit. The native CLI resolves remaining groups and constraints.</div><label class="space">Arguments (JSON array)<textarea id="cli-argv" spellcheck="false">${esc(JSON.stringify(command.command))}</textarea></label><button class="primary" data-command="cli-run">Review & run</button></div>`);
}
function buildCli(){
  const command=cliCommands.find(c=>c.command.join(' ')===cliSelection)||cliCommands[0];
  const active=new Map();document.querySelectorAll('[data-cli-field]').forEach(input=>{const value=input.type==='checkbox'?input.checked:input.value;cliValues[input.dataset.cliField]=value;if(value!==''&&value!==false)active.set(input.dataset.cliField,value);});
  const conflicts=[];for(const field of command.fields){if(!active.has(field.key))continue;for(const k of field.conflicts)if(active.has(k))conflicts.push(`${field.flag||field.key} conflicts with ${k}`);for(const k of field.requires)if(!active.has(k))conflicts.push(`${field.flag||field.key} requires ${k}`);}
  if(conflicts.length)throw new Error([...new Set(conflicts)].join('; '));
  const args=[...command.command],last=[];
  for(const field of command.fields){if(!active.has(field.key)){if(field.required)throw new Error(`${field.flag||field.key} is required`);continue;}
    const value=active.get(field.key),values=field.repeat?String(value).split('\n').filter(s=>s.length):[String(value)];
    if(field.last){last.push(...values);continue;}
    if(field.type==='bool'){args.push(field.flag);continue;}
    for(const item of values){if(field.flag)args.push(field.flag);args.push(item);}
  }
  if(last.length)args.push('--',...last);$('#cli-argv').value=JSON.stringify(args,null,2);return args;
}
async function manualView(){
  const results=catalog.pages.filter(p=>(p.title+' '+p.slug+' '+p.text).toLowerCase().includes(docFilter.toLowerCase()));
  const page=catalog.pages.find(p=>p.slug===selectedDoc)||catalog.pages[0];
  const {marked,DOMPurify}=await vendor();
  let text=page.text.replace(/<\/?(?:Note|Warning|Tip|Info|Accordion|AccordionGroup|Steps|Step|Tabs|Tab|Card|CardGroup|ParamField|ResponseField|CodeGroup|Frame|Expandable|Update)[^>]*>/g,'\n');
  text=text.replace(/\]\((?:\.{1,2}\/)*\/?images\//g,'](/reference/images/').replace(/src="(?:\.{1,2}\/)*\/?images\//g,'src="/reference/images/');
  text=text.replaceAll('github.com/NVIDIA/OpenShell/blob/main/',`github.com/NVIDIA/OpenShell/blob/${catalog.commit}/`).replaceAll('raw.githubusercontent.com/NVIDIA/OpenShell/main/',`raw.githubusercontent.com/NVIDIA/OpenShell/${catalog.commit}/`);
  const html=DOMPurify.sanitize(marked.parse(text),{FORBID_TAGS:['iframe','style','form','input','button'],FORBID_ATTR:['style']});
  return heading('Reference desk',`${catalog.pages.length} upstream pages · ${catalog.version} · ${catalog.options.length} option entries`)+
  `<div class="manual-layout"><section class="card"><div class="search"><input id="doc-search" placeholder="Search the complete manual…" value="${esc(docFilter)}" aria-label="Search the complete manual"></div><div class="doc-search-meta">${results.length} pages</div><div class="manual-nav">${results.map(p=>`<button class="${p.slug===page.slug?'active':''}" data-doc="${esc(p.slug)}">${esc(p.title)}<small style="display:block;font-size:9px">${esc(p.slug.split('/')[0])}</small></button>`).join('')}</div></section><article class="card manual-content"><div class="manual-source"><a target="_blank" rel="noopener noreferrer" href="${esc(page.source)}">Upstream source ↗</a> · ${catalog.version} · <button class="quiet small" data-command="copy-doc">Copy source</button></div><h1>${esc(page.title)}</h1>${html}</article></div>`;
}
async function render(){
  const generation=++renderGeneration;
  if($('#toml')&&config){configDirty=configDirty||$('#toml').value!==config.text;config.text=$('#toml').value;}
  teardownTerminal();
  if(!state)return;
  document.querySelectorAll('[data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
  $('#breadcrumb').textContent=titles[view];
  if(view==='configuration'&&!config)config=await api('/api/config');
  if((view==='configuration'||view==='manual')&&!catalog)catalog=await (await fetch('/reference/catalog.json')).json();
  if(view==='configuration'&&!schema)schema=await (await fetch('/reference/config-fields.json')).json();
  if(view==='cli'&&!cliCommands)cliCommands=await (await fetch('/reference/commands.json')).json();
  const renderers={overview,environments,sessions:sessionView,configuration:configView,bridge:bridgeView,manual:manualView,updates:updatesView,activity:activityView,cli:cliView};
  const html=await renderers[view]();if(generation!==renderGeneration)return;main.innerHTML=html;
  const toolbar=$('.terminal-head .actions',main);
  if(toolbar){const button=document.createElement('button');button.className='quiet small';button.textContent='Fresh shell';button.dataset.reviveSession=selectedSession;toolbar.insertBefore(button,toolbar.lastElementChild);const options=document.createElement('button');options.className='quiet small';options.textContent='Display';options.dataset.command='terminal-options';toolbar.insertBefore(options,toolbar.lastElementChild);}
  if(view==='environments')main.querySelectorAll('[data-env-action="get"]').forEach(inspect=>{const button=document.createElement('button');button.className='quiet small danger';button.textContent='Remove';button.dataset.removeEnv=inspect.dataset.env;inspect.parentElement.append(button);});
}
function modal(title,body,submit,handler){
  const dialog=$('#form-dialog');dialog.innerHTML=`<button class="dialog-close" type="button" aria-label="Close">×</button><form><span class="eyebrow">OMOSH / OPERATOR</span><h2>${title}</h2>${body}<p class="error" role="alert"></p><div class="actions"><button type="button" class="cancel">Cancel</button><button class="primary">${submit}</button></div></form>`;
  $('.dialog-close',dialog).onclick=()=>dialog.close();$('.cancel',dialog).onclick=()=>dialog.close();
  $('form',dialog).onsubmit=async event=>{event.preventDefault();if(event.target.dataset.uploading)return;const button=$('button.primary',dialog);button.disabled=true;try{await handler(new FormData(event.target),dialog);dialog.close();}catch(error){$('.error',dialog).textContent=error.message;}finally{button.disabled=false;}};
  dialog.showModal();
}
function newEnvironment(){modal('New environment',`<p>Create a supervised Docker sandbox. The default policy denies outbound network access until you attach a provider or grant a rule.</p><div class="form-grid"><label>Name<input name="name" placeholder="workbench" pattern="[a-z0-9-]+" required></label><label>Workspace<input name="workspace" value="default" required></label><label class="span2">Image<input name="image" value="omosh-workload:local" required></label><label>CPU (optional)<input name="cpu" placeholder="2"></label><label>Memory (optional)<input name="memory" placeholder="4Gi"></label></div><details><summary>Advanced creation options</summary><label>Template (owns image and resources)<input name="template" placeholder="my-template"></label><label>GPU count<input name="gpu" type="number" min="1" max="128" placeholder="0"></label><label>Providers, one name per line<textarea name="providers" placeholder="openrouter"></textarea></label><label>Non-secret environment entries<textarea name="env" placeholder="PROJECT=/sandbox"></textarea></label><label>Local port forwards, one per line<textarea name="forward" placeholder="127.0.0.1:3000"></textarea></label><label>Policy YAML (optional)<textarea name="policy" placeholder="version: 1"></textarea></label></details>`, 'Create environment',async data=>{
  const body=Object.fromEntries(data);for(const k of ['env','forward','providers'])body[k]=body[k].split('\n').map(s=>s.trim()).filter(Boolean);
  await api('/api/environments',{method:'POST',body});toast('Environment creation started. See Activity for the result.');});}
function newSession(environment=''){if(!state.environments.length){toast('Create an environment first.');location.hash='environments';return;}modal('New shell session',`<label>Environment<select name="environment">${state.environments.map(e=>`<option value="${e.id}" ${e.id===environment?'selected':''}>${esc(e.workspace)} / ${esc(e.name)}</option>`).join('')}</select></label><label>Session name<input name="name" value="shell" required pattern="[a-z0-9-]+"></label><p>The image must include tmux, bash, and Python 3. A browser disconnect leaves the session alive.</p>`,'Create session',async data=>{await api('/api/sessions',{method:'POST',body:Object.fromEntries(data)});toast('Session creation started. See Activity.');});}
function editSetting(path){
  if($('#toml')?.value!==undefined&&$('#toml').value!==config.text){config.text=$('#toml').value;configDirty=true;}
  if(configDirty){toast('Save or reload the TOML draft before editing individual fields.');return;}
  const definition=schema?.find(f=>f.path===path)||{path,type:'json',help:''};
  const stored=path.split('.').reduce((value,key)=>value?.[key],config.values);
  const current=config.fields.find(f=>f.path===path)??(stored!==undefined?{path,value:stored}:undefined);
  const listenerNote=/^openshell\.gateway\.(bind_address|health_bind_address|metrics_bind_address)$/.test(path)?' Container listeners use 0.0.0.0:PORT. Compose exposes them only on host 127.0.0.1.':'';
  const initial=current?.value??definition.example??(definition.type==='json'?freshValue(definition.structure):definition.type==='bool'?false:definition.type==='number'?0:'');
  let input;
  if(definition.type==='bool')input=`<select name="value"><option value="true" ${initial===true?'selected':''}>true</option><option value="false" ${initial===false?'selected':''}>false</option></select>`;
  else if(definition.choices?.length)input=`<select name="value">${[...new Set([initial,...definition.choices])].map(v=>`<option value="${esc(v)}" ${v===initial?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
  else if(definition.type==='json')input=`<div id="structured-value"></div><details><summary>JSON source</summary><textarea name="value" required spellcheck="false">${esc(JSON.stringify(initial,null,2))}</textarea><button type="button" id="apply-json-value">Apply JSON to fields</button></details>`;
  else input=`<input name="value" type="${definition.type==='number'?'number':'text'}" value="${esc(initial)}" required>`;
  modal(current?'Edit setting':'Configure setting',`<p class="mono">${esc(path)}</p><p>${esc((definition.help||'Refer to the upstream reference for this setting’s constraints.')+listenerNote)}</p><label>Value${input}</label>${definition.file?'<label>Upload an operator file (optional)<input type="file" data-operator-file accept=".pem,.crt,.key,.yaml,.yml,.json,.toml,.txt"></label><p>Files stay in the private state directory. Native preflight checks file contents and any ownership requirements.</p>':''}${definition.example!==undefined?`<p>Example: <code>${esc(JSON.stringify(definition.example))}</code></p>`:''}${current?'<label class="inline"><input type="checkbox" name="remove">Remove this override and use the upstream default</label>':''}${definition.source?`<p><a href="${esc(definition.source)}" target="_blank" rel="noopener noreferrer">Pinned source ↗</a></p>`:''}`,'Save draft',async data=>{
    let value=data.get('value');if(definition.type==='json')value=JSON.parse(value);if(definition.type==='bool')value=value==='true';if(definition.type==='number')value=Number(value);
    await api('/api/config',{method:'PUT',body:{path,value,remove:data.has('remove'),revision:config.revision}});config=await api('/api/config');await render();
  });
  if(definition.type==='json'){
    const target=$('#structured-value'),source=$('#form-dialog [name="value"]');
    const editor=mountEditor(target,initial,definition.structure,value=>{source.value=JSON.stringify(value,null,2);});
    $('#apply-json-value').onclick=()=>{try{editor.set(JSON.parse(source.value));}catch(error){toast(error.message);}};
  }
  if(definition.manual){const help=document.createElement('button');help.type='button';help.className='small quiet';help.textContent='Open reference chapter ↗';help.onclick=async()=>{$('#form-dialog').close();selectedDoc=definition.manual;await preference({document:selectedDoc});view='manual';location.hash='manual';await render();};$('#form-dialog form').append(help);}
}
function teardownTerminal(){if(!terminal)return;terminal.destroyed=true;terminal.observer?.disconnect();terminal.socket?.close();terminal.disposables?.forEach(d=>d.dispose());terminal.term?.dispose();terminal=null;}
async function connectTerminal(){
  teardownTerminal();const marker={};terminal=marker;const mount=$('#terminal');if(!mount)return;
  const {Terminal,FitAddon}=await vendor();if(terminal!==marker||!mount.isConnected)return;
  mount.replaceChildren();const term=new Terminal({fontSize:state.preferences?.terminal_font_size||13,fontFamily:'Cascadia Code, Consolas, monospace',cursorBlink:true,scrollback:state.preferences?.terminal_scrollback||5000,allowProposedApi:false,theme:{background:'#0c0f0a',foreground:'#d6e0cd',cursor:'#b9df79',selectionBackground:'#536b3466'}});const fit=new FitAddon();term.loadAddon(fit);term.open(mount);fit.fit();
  const socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/terminal/${selectedSession}`);socket.binaryType='arraybuffer';Object.assign(marker,{term,fit,socket,disposables:[]});
  let resizeFrame=0;
  function send(message){if(socket.readyState===WebSocket.OPEN)socket.send(JSON.stringify(message));}
  const resize=()=>{if(resizeFrame)return;resizeFrame=requestAnimationFrame(()=>{resizeFrame=0;if(marker.destroyed)return;fit.fit();send({type:'resize',cols:Math.min(500,term.cols),rows:Math.min(300,term.rows)});});};
  marker.observer=new ResizeObserver(resize);marker.observer.observe(mount);
  marker.disposables.push(term.onData(data=>{for(const part of data.match(/[\s\S]{1,4000}/gu)||[])send({type:'input',data:part});}));
  socket.onopen=()=>{$('#terminal-state').textContent='Connected';resize();term.focus();};
  socket.onmessage=event=>{if(marker.destroyed)return;if(typeof event.data==='string'){const notice=JSON.parse(event.data);if(notice.type==='busy')toast(notice.detail);return;}term.write(new Uint8Array(event.data),()=>send({type:'ack'}));};
  socket.onclose=event=>{if(marker.destroyed)return;$('#terminal-state').textContent=`Detached (${event.code})`;term.writeln('\r\n[detached]');};
}
const commands={
 'build-cli':()=>buildCli(),
 'new-environment':newEnvironment,'new-session':()=>newSession(),
 'connect-terminal':connectTerminal,'detach-terminal':()=>{teardownTerminal();$('#terminal-state').textContent='Detached';},
 'copy-terminal':async()=>{if(terminal?.term.hasSelection())await navigator.clipboard.writeText(terminal.term.getSelection());},
 'send-command':async()=>{const text=$('#shell-command').value;const job=await api(`/v1/sessions/${selectedSession}/commands`,{method:'PUT',body:{command:text}});toast(`Command submitted: ${job.id.slice(0,10)}. See Activity.`);},
 'reload-config':async()=>{if($('#toml'))$('#toml').remove();configDirty=false;config=await api('/api/config');await render();},
 'save-config':async()=>{const text=$('#toml')?.value??config.text;await api('/api/config',{method:'PUT',body:{text,revision:config.revision}});config=await api('/api/config');configDirty=false;toast('Draft saved. Restart the gateway to apply.');},
 'terminal-options':()=>modal('Terminal display','<label>Font size<input name="font" type="number" min="10" max="24" value="'+(state.preferences?.terminal_font_size||13)+'" required></label><label>Scrollback lines<input name="lines" type="number" min="500" max="20000" value="'+(state.preferences?.terminal_scrollback||5000)+'" required></label>','Save',async data=>{await preference({terminal_font_size:Number(data.get('font')),terminal_scrollback:Number(data.get('lines'))});if(terminal)await connectTerminal();}),
 'validate-config':async()=>{const result=await api('/api/config/validate',{method:'POST',body:{text:$('#toml')?.value??config.text}});$('#validation').innerHTML=`<div class="${result.errors.length?'alert':'note'}">${[...result.errors,...result.warnings,'Native preflight validates the complete upstream contract before activation.'].map(esc).join('<br>')}<div class="actions space">${[...new Set((result.issues||[]).map(i=>i.path))].map(path=>`<button class="small" data-setting="${esc(path)}">Edit ${esc(path.replace('openshell.',''))} →</button>`).join('')}</div></div>`;},
 'preflight-config':async()=>{await api('/api/config/preflight',{method:'POST',body:{}});toast('Native preflight started. See Activity.');},
 'add-setting':()=>modal('Add a setting','<p>Use the option’s exact dotted TOML path from the versioned reference.</p><label>Field path<input name="path" placeholder="openshell.gateway.ssh_session_ttl_secs" required></label><label>Value (JSON)<textarea name="value" placeholder="3600" required></textarea></label>','Add to draft',async data=>{await api('/api/config',{method:'PUT',body:{path:data.get('path'),value:JSON.parse(data.get('value')),revision:config.revision}});config=await api('/api/config');await render();}),
 'new-token':()=>{if(!state.sessions.length){toast('Create a session before issuing a token.');return;}modal('Session token',`<label>Label<input name="label" value="agent" required></label><p>Select the shell sessions this token can access.</p>${state.sessions.map(s=>`<label class="inline"><input type="checkbox" name="session" value="${s.id}">${esc(s.name)} / ${esc(s.environment)}</label>`).join('')}`,'Create token',async(data,dialog)=>{const result=await api('/api/tokens',{method:'POST',body:{label:data.get('label'),sessions:data.getAll('session')}});await refreshState();setTimeout(()=>modal('Copy your token',`<p>Shown once. Store it as <code>OMOSH_TOKEN</code>.</p><textarea id="new-token-value" readonly>${esc(result.token)}</textarea>`,'Done',async()=>{}),0);});},
 'apply-update':()=>modal('Apply staged update','<p>This stops the owned sandboxes and gateway, then switches the pinned components. Review the candidate release notes in Activity first. Existing sandbox runtime images stay attached to those sandboxes.</p><label class="inline"><input type="checkbox" required>I have reviewed compatibility and can stop these environments.</label>','Apply update',()=>action('apply-update')),
 'cli-help':async()=>{const argv=(cliCommands.find(c=>c.command.join(' ')===cliSelection)||cliCommands[0]).command;await api('/api/cli',{method:'POST',body:{argv:[...argv,'--help'],workspace:$('#cli-workspace').value}});toast('Command help requested. Open Activity for the output.');},
 'cli-run':()=>{const argv=JSON.parse($('#cli-argv').value),workspace=$('#cli-workspace').value;modal('Run OpenShell command',`<pre>${esc(JSON.stringify(argv,null,2))}</pre><p>Runs against workspace <code>${esc(workspace)}</code>. Review paths and flags before continuing.</p>`,'Run',()=>api('/api/cli',{method:'POST',body:{argv,workspace,confirm:true}}));},
 'copy-doc':async()=>{await navigator.clipboard.writeText(catalog.pages.find(p=>p.slug===selectedDoc).text);toast('Reference source copied.');},
};
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');const link=event.target.closest('.manual-content a');
  if(link){const url=new URL(link.href);if(url.origin===location.origin&&!url.pathname.startsWith('/reference/')){const slug=url.pathname.replace(/^\//,'').replace(/\/$/,'');if(catalog.pages.some(p=>p.slug===slug)){event.preventDefault();selectedDoc=slug;await render();return;}}}
  if(!button)return;
  try{
    if(button.dataset.action){if(button.dataset.action==='start'&&!sessionStorage.getItem('omosh-socket-ack')){modal('Start the local gateway','<p>The gateway receives access to the host Docker socket and can create sibling containers. omosh keeps that socket outside sandbox workloads and publishes the operator service on localhost.</p><label class="inline"><input type="checkbox" required>I trust this gateway with the local Docker daemon.</label>','Start gateway',async()=>{sessionStorage.setItem('omosh-socket-ack','1');await action('start');});return;}button.disabled=true;await action(button.dataset.action);button.disabled=false;}
    if(button.dataset.command)await commands[button.dataset.command]?.();
    if(button.dataset.doc){selectedDoc=button.dataset.doc;await preference({document:selectedDoc});if(view!=='manual')location.hash='manual';else await render();}
    if(button.dataset.session){selectedSession=button.dataset.session;await preference({session:selectedSession});await render();await connectTerminal();}
    if(button.dataset.newSession) newSession(button.dataset.newSession);
    if(button.dataset.envAction){await api(`/api/environments/${button.dataset.env}/${button.dataset.envAction}`,{method:'POST',body:{}});toast('Action queued. See Activity.');}
    if(button.dataset.closeSession)modal('Close shell session','<p>This ends the tmux session and its shell processes. The environment remains available.</p>','Close session',async()=>{await api(`/api/sessions/${button.dataset.closeSession}`,{method:'DELETE'});toast('Session close queued.');});
    if(button.dataset.reviveSession)modal('Start a fresh shell','<p>Use this after a shell exits or its sandbox restarts. It creates a fresh shell under the same session name; previous process state is gone. A live shell is kept intact.</p>','Start fresh shell',async()=>{await api(`/api/sessions/${button.dataset.reviveSession}/revive`,{method:'POST',body:{}});toast('Fresh shell queued.');});
    if(button.dataset.removeEnv){const env=state.environments.find(e=>e.id===button.dataset.removeEnv);modal('Remove environment',`<p>Deleting removes the owned OpenShell sandbox and its sessions. Unregistering removes only omosh’s records and leaves any workload running; use it to reconcile resources managed elsewhere.</p><label>Action<select name="mode"><option value="delete">Delete owned sandbox</option><option value="unregister">Unregister only</option></select></label><label>Type ${esc(env.name)} to confirm<input name="confirm_name" required autocomplete="off"></label>`,'Remove environment',async data=>{if(data.get('confirm_name')!==env.name)throw new Error('Environment name does not match.');await api(`/api/environments/${env.id}`,{method:'DELETE',body:Object.fromEntries(data)});toast('Removal queued.');});}
    if(button.dataset.cancel){await api(`/v1/jobs/${button.dataset.cancel}`,{method:'DELETE'});toast('Cancellation requested.');}
    if(button.dataset.revoke){await api(`/api/tokens/${button.dataset.revoke}`,{method:'DELETE'});await refreshState();await render();}
    if(button.dataset.copy){await navigator.clipboard.writeText($(`#${button.dataset.copy}`).textContent);toast('Copied.');}
    if(button.dataset.configMode){configMode=button.dataset.configMode;await preference({config_mode:configMode});await render();}
    if(button.dataset.editField)editSetting(button.dataset.editField);
    if(button.dataset.setting)editSetting(button.dataset.setting);
    if(button.dataset.cliPreset)$('#cli-argv').value=JSON.stringify([button.dataset.cliPreset,'--help']);
  }catch(error){button.disabled=false;toast(error.message);}
});
let searchTimer;
document.addEventListener('input',event=>{
  const mapping={'option-search':v=>optionFilter=v,'field-search':v=>configFilter=v,'doc-search':v=>docFilter=v};const setter=mapping[event.target.id];if(!setter)return;
  setter(event.target.value);clearTimeout(searchTimer);const id=event.target.id,pos=event.target.selectionStart;
  searchTimer=setTimeout(async()=>{await render();const input=$(`#${id}`);input?.focus();input?.setSelectionRange(pos,pos);},180);
});
document.addEventListener('change',async event=>{
  if(event.target.matches('[data-operator-file]')){
    const file=event.target.files[0],form=event.target.closest('form');
    if(file&&form){
      const button=$('button.primary',form);button.disabled=true;form.dataset.uploading='true';
      try{
        if(file.size>1048576)throw new Error('Operator files are limited to 1 MiB.');
        const result=await api('/api/files',{method:'POST',body:file,headers:{'Content-Type':'application/octet-stream','X-Omosh-Filename':encodeURIComponent(file.name)}});
        if(form.isConnected)$('[name="value"]',form).value=result.path;
        toast('File saved privately. Save the draft to use its path.');
      }catch(error){toast(error.message);}
      finally{button.disabled=false;delete form.dataset.uploading;}
    }
  }
  if(event.target.id==='cli-command'){cliSelection=event.target.value;cliValues={};await render();}
  if(event.target.id==='telemetry'){try{await api('/api/telemetry',{method:'PUT',body:{enabled:event.target.checked}});state.telemetry=event.target.checked;toast('Saved. Applies at the next gateway start.');}catch(error){toast(error.message);}}
});
$('#refresh').onclick=()=>action('refresh').catch(error=>toast(error.message));
window.addEventListener('hashchange',()=>{const target=location.hash.slice(1);if(titles[target]){view=target;render().catch(error=>toast(error.message));}});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&terminal){teardownTerminal();if($('#terminal-state'))$('#terminal-state').textContent='Detached · tab hidden';}});
window.addEventListener('pagehide',()=>{teardownTerminal();eventSource?.close();});
async function initialize(){
  await refreshState();selectedSession=state.preferences.session||'';selectedDoc=state.preferences.document||selectedDoc;configMode=state.preferences.config_mode||configMode;view=titles[location.hash.slice(1)]?location.hash.slice(1):'overview';await render();
  eventSource?.close();eventSource=new EventSource('/api/events');
  eventSource.addEventListener('gateway',event=>{state.gateway=JSON.parse(event.data);updateChrome();});
  eventSource.addEventListener('job',async event=>{
    const job=JSON.parse(event.data);const index=state.jobs.findIndex(j=>j.id===job.id);if(index<0)state.jobs.push(job);else state.jobs[index]=job;
    if(job.session===selectedSession&&terminal&&!terminal.destroyed){$('#terminal-state').textContent=['queued','running'].includes(job.status)?'Agent command active · Ctrl-C to cancel':'Connected';}
    if(!['queued','running'].includes(job.status)){await refreshState();if(view==='sessions'&&['Create session','Close session','Start fresh shell'].includes(job.title))await render();else if(view!=='sessions'&&view!=='configuration'&&view!=='manual'&&view!=='cli')await render();toast(`${job.title}: ${job.status}`);}
    else if(view==='activity')await render();
  });
}
$('#auth-form').onsubmit=async event=>{event.preventDefault();try{await api('/api/auth',{method:'POST',body:{key:new FormData(event.target).get('key')}});$('#auth-dialog').close();await initialize();}catch(error){$('#auth-error').textContent=error.message;}};
async function boot(){const key=new URLSearchParams(location.hash.slice(1)).get('key');if(key){history.replaceState(null,'','#overview');await api('/api/auth',{method:'POST',body:{key}});}try{await initialize();}catch(error){if(error.message.includes('operator')||error.message.includes('Operator'))$('#auth-dialog').showModal();else throw error;}}
boot().catch(error=>{toast(error.message);$('#auth-dialog').showModal();});
