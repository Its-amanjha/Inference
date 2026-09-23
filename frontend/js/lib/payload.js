// Readable summaries of API payloads. Every sentence here is derived from response fields;
// anything not understood stays visible in the raw disclosure instead of being guessed.
import {el,jsonView,button} from './html.js';
import {icon,registerIcons} from './icons.js';
import {state} from '../state.js';
import {api} from '../api.js';
import {parseTimestamp,relativeLabel} from './triage.js';

// A single loading treatment for initial loads; keep existing content during refreshes.
export function loadingState(label,{rows=3,compactLabel=label,kind='rows'}={}){
  const lines=()=>Array.from({length:rows},()=>el('span',{class:'loading-row'}));
  return el('div',{class:`loading-state${kind==='metrics'?' loading-metrics':''}`,role:'status','aria-live':'polite'},
    el('p',{class:'loading-label'},el('span',{class:'loading-spinner','aria-hidden':'true'}),
      el('span',{'aria-hidden':'true'},compactLabel),el('span',{class:'sr-only'},label)),
    el('div',{class:'loading-rows','aria-hidden':'true'},kind==='metrics'
      ? Array.from({length:4},()=>el('div',{class:'loading-metric'},lines())) : lines()));
}

export function asOf(date=new Date()){
  return el('p',{class:'load-as-of'},'As of ',el('time',{datetime:date.toISOString(),title:date.toLocaleString()},
    date.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',hour12:false})));
}

const TRANSIENT_CODES=new Set(['rate_limited','upstream_rate_limited','upstream_timeout','upstream_unavailable','probe_timeout']);
export function isTransient(error){
  if(!error) return false;
  const status = error.status !== undefined && error.status !== null ? Number(error.status) : undefined;
  return error instanceof TypeError || error.name === 'TimeoutError' || error.name === 'NetworkError'
    || TRANSIENT_CODES.has(typeof error === 'string' ? error : error.code)
    || status === 0 || [408,429,502,503,504].includes(status);
}

export function loadFailure(error,retry,label='Could not refresh data'){
  const transient=isTransient(error);
  return el('div',{class:transient?'coverage-partial load-notice':'error load-notice',role:transient?'status':'alert'},
    icon(transient?'info':'blocked'),el('div',{},el('p',{},el('strong',{},label)),
      el('p',{},error?.message||'The request did not complete.'),
      button('Retry',retry,'ghost')));
}

// Only connection-check metadata is cached, per signed-in user and browser tab.
// The backend's persisted probe has no read endpoint; never infer health from a saved URL.
function probeCache(){
  const user=state.user?.username;
  if(!user)return {key:null,rows:{}};
  const key=`inference:probe-cache:${user}`;
  try{
    const rows=JSON.parse(sessionStorage.getItem(key)||'{}');
    return {key,rows:rows&&typeof rows==='object'&&!Array.isArray(rows)?rows:{}};
  }
  catch{return {key,rows:{}};}
}
export function cachedProbe(instance){
  const entry=probeCache().rows[instance.id];
  return entry?.url===instance.url && typeof entry.result?.reachable==='boolean'
    && parseTimestamp(entry.result.checked_at)?entry.result:null;
}
export function rememberProbe(instance,result){
  const {key,rows}=probeCache();if(!key)return;
  rows[instance.id]={url:instance.url,result:{instance_id:instance.id,reachable:result.reachable,
    checked_at:result.checked_at,latency_ms:result.latency_ms,
    capabilities:result.capabilities,diagnostics:result.diagnostics}};
  const bounded=Object.fromEntries(Object.entries(rows).sort((a,b)=>String(b[1]?.result?.checked_at).localeCompare(String(a[1]?.result?.checked_at))).slice(0,200));
  try{sessionStorage.setItem(key,JSON.stringify(bounded));}catch{/* Storage may be disabled. */}
}
// The fleet scan is rate-limited (4 a minute) and three pages show its numbers. They share one result:
// a scan newer than a minute is reused, and a rate-limited or failed scan falls back to the last one.
// A scan of one instance is cached the same way under its own key.
const FLEET_TTL=60000;
function fleetKey(instance){const user=state.user?.username;return user?`inference:fleet-metrics:${user}${instance?`:${instance}`:''}`:null;}
// A cached whole-fleet scan is only reused for the same set of instances: adding or removing one invalidates it.
const fleetIds=instance=>instance||state.instances.map(item=>item.id).sort().join(',');
function readFleet(instance){const key=fleetKey(instance);if(!key)return null;try{const entry=JSON.parse(sessionStorage.getItem(key)||'null');
  return entry&&Array.isArray(entry.metrics?.data)&&entry.ids===fleetIds(instance)?entry:null;}catch{return null;}}
export function rememberFleetMetrics(metrics,instance=null){const key=fleetKey(instance);if(!key||!Array.isArray(metrics?.data))return;
  try{sessionStorage.setItem(key,JSON.stringify({at:Date.now(),ids:fleetIds(instance),metrics}));}catch{/* Storage may be disabled. */}}
// `fresh` always asks the server (the overview's Refresh must refresh) but still falls back when limited.
// The result's window_end is when the scan ran, so a reused scan can be labelled with its own time.
export async function fleetMetrics({signal,fresh=false,instance=null}={}){
  const cached=readFleet(instance);
  if(!fresh&&cached&&Date.now()-cached.at<FLEET_TTL)return cached.metrics;
  try{const metrics=await api('/api/n8n_proxy/dashboard'+(instance?`?instance_id=${encodeURIComponent(instance)}`:''),{signal});rememberFleetMetrics(metrics,instance);return metrics;}
  catch(error){if(error?.name==='AbortError'||!cached)throw error;return cached.metrics;}
}
// Availability, throughput and error rate for one instance, from the dashboard scan. A value the scan
// could not count is shown as a dash, never as zero.
export function instanceStats(evidence){
  const known=(complete,value)=>complete&&Number.isFinite(value);
  const workflows=known(evidence?.workflows_complete,evidence?.observed_workflows);
  const runs=known(evidence?.executions_complete,evidence?.observed_executions_24h);
  const terminal=runs&&Number.isFinite(evidence.terminal)?evidence.terminal:null;
  const successes=Number.isFinite(evidence?.successes)?evidence.successes:null;
  const rate=terminal?`${Math.round(successes/terminal*100)}%`:null;
  const items=[
    ['Workflows',workflows?String(evidence.observed_workflows):'—',workflows&&Number.isFinite(evidence.active_workflows)?`${evidence.active_workflows} active`:null],
    ['Runs · 24h',runs?String(evidence.observed_executions_24h):'—',null],
    ['Success rate',rate??'—',terminal?`of ${terminal} finished`:runs?'No finished runs':null],
    ['Failed · 24h',terminal!==null&&successes!==null?String(terminal-successes):'—',null],
  ];
  return el('dl',{class:'instance-stats'},items.map(([label,value,note])=>el('div',{class:'instance-stat'},
    el('dt',{},label),el('dd',{},el('span',{class:'instance-stat-value'},value),note?el('span',{class:'instance-stat-note'},note):null))));
}
export function forgetProbe(instanceId){
  const {key,rows}=probeCache();if(!key)return;delete rows[instanceId];
  try{sessionStorage.setItem(key,JSON.stringify(rows));}catch{/* Storage may be disabled. */}
}

// Both reads are viewer-authorized, limited to one record, and independently abortable.
// Unlike the aggregate /health route, a slow instance cannot hold back other cards.
export async function checkConnection(instance,{signal}={}){
  const controller=new AbortController(),started=performance.now(),user=state.user;
  const cancel=()=>controller.abort(signal?.reason);
  if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
  let timedOut=false;
  const timer=setTimeout(()=>{timedOut=true;controller.abort();},10000);
  try{
    const resources=[['workflows','workflow_read'],['executions','execution_read']];
    const results=await Promise.allSettled(resources.map(([resource])=>api(`/api/n8n_proxy/${resource}?`+
      new URLSearchParams({instance_id:instance.id,limit:'1'}),{signal:controller.signal})));
    if(signal?.aborted)throw new DOMException('Check cancelled','AbortError');
    if(timedOut)throw Object.assign(new Error('No response after 10s - the instance may be unreachable'),{code:'probe_timeout'});
    const capabilities={},diagnostics=[];
    let reachable=false;
    results.forEach((result,index)=>{
      const capability=resources[index][1];
      if(result.status==='fulfilled'){capabilities[capability]='supported';reachable=true;return;}
      const error=result.reason,code=error.code||'upstream_unavailable';
      if(['authentication_required','session_revoked','session_expired','permission_denied'].includes(code))throw error;
      const denied=['upstream_auth_failed','upstream_forbidden'].includes(code);
      capabilities[capability]=denied?'forbidden':'unavailable';
      reachable ||= denied || ['upstream_not_found','upstream_rate_limited','upstream_invalid_response'].includes(code);
      diagnostics.push({capability,code});
    });
    if(results.every(result=>result.status==='rejected'&&isTransient(result.reason)))throw results[0].reason;
    const result={instance_id:instance.id,reachable,capabilities,diagnostics,checked_at:new Date().toISOString(),latency_ms:Math.round(performance.now()-started)};
    if(user===state.user)rememberProbe(instance,result);
    return result;
  }finally{clearTimeout(timer);signal?.removeEventListener('abort',cancel);}
}

registerIcons({
  blocked:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M5.6 5.6l12.8 12.8',
  unknown:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M9.5 9.2a2.6 2.6 0 1 1 3.6 2.4c-.7.3-1.1.9-1.1 1.6v.4 M12 17v.1',
  clock:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2',
});

const TONE_ICON={success:'check',error:'blocked',warning:'errors',info:'info',neutral:'unknown'};

export function rawDisclosure(value,label='Raw payload'){
  return el('details',{className:'raw-payload'},el('summary',{},label),jsonView(value));
}

export function instanceName(id){
  if(!id)return 'an unspecified instance';
  return state.instances.find(item=>item.id===id)?.name||`instance ${id}`;
}

export const plural=(count,singular,many=`${singular}s`)=>`${count} ${count===1?singular:many}`;

export function toneBadge(text,tone='neutral',glyph=TONE_ICON[tone]){
  return el('span',{className:`badge decision-badge${tone==='neutral'?'':` ${tone}`}`},glyph?icon(glyph):null,text);
}

export function formatTime(value){
  if(value===null||value===undefined||value==='')return '';
  const date=typeof value==='number'?new Date(value*1000):new Date(/^\d{4}-\d{2}-\d{2} \d/.test(value)?value.replace(' ','T')+'Z':value);
  return Number.isNaN(date.getTime())?String(value):date.toLocaleString();
}

const CODES={
  upstream_auth_failed:'n8n rejected the API key',
  upstream_forbidden:'n8n denied this operation',
  upstream_not_found:'n8n could not find the resource',
  upstream_rate_limited:'n8n is rate limiting requests',
  upstream_rejected:'n8n rejected the operation',
  upstream_error:'n8n returned an unexpected response',
  upstream_invalid_response:'n8n returned an invalid response',
  upstream_timeout:'n8n did not respond in time',
  upstream_unavailable:'n8n is unavailable',
  upstream_outcome_unknown:'A remote write may have completed',
  rate_limited:'Too many checks; wait before trying again',
  instance_changed:'Connection settings changed during the check',
  stale_preview:'Source, target or mappings changed after the preview',
  preview_not_deployable:'The preview expired or has unresolved blockers',
  credential_binding_invalid:'A target credential binding is missing or has the wrong type',
  mapping_changed:'A mapping changed during the run',
  interrupted:'The run was interrupted',
  credential_discovery_unavailable:'Credential discovery is unavailable',
  credential_verification_unavailable:'Target credential access is not verified',
};
// Known codes get a sentence; unknown codes are shown verbatim rather than interpreted.
export const describeCode=code=>CODES[code]||`Error code ${code}`;

const CAPABILITIES=[['workflow_read','Workflow read'],['execution_read','Execution read']];
const CAPABILITY_STATE={supported:['Available','success'],forbidden:['Not permitted','error'],unavailable:['Could not check','warning']};

// Probe/health result: {reachable, capabilities:{workflow_read,execution_read}, diagnostics:[{capability?,code}], checked_at, latency_ms}
export function probeSummary(result){
  const capabilities=result?.capabilities&&typeof result.capabilities==='object'?result.capabilities:{};
  const diagnostics=Array.isArray(result?.diagnostics)?result.diagnostics:[];
  const known=new Set(CAPABILITIES.map(([key])=>key));
  const rows=[...CAPABILITIES,...Object.keys(capabilities).filter(key=>!known.has(key)).map(key=>[key,key])];
  const list=el('ul',{className:'decision-list capability-list'},rows.map(([key,label])=>{
    const [text,tone]=CAPABILITY_STATE[capabilities[key]]||['Unknown','neutral'];
    return el('li',{},el('span',{},label),toneBadge(text,tone));
  }));
  const labelFor=key=>CAPABILITIES.find(([name])=>name===key)?.[1]||key;
  const notes=diagnostics.length?el('ul',{className:'decision-list'},diagnostics.map(item=>el('li',{},
    el('span',{},`${item?.capability?`${labelFor(item.capability)}: `:''}${describeCode(item?.code)}.`)))):null;
  return {list,notes,diagnostics};
}

export function reachabilityBadge(result){
  if(typeof result?.reachable!=='boolean')return toneBadge('Not checked');
  if(result.reachable)return toneBadge('Reachable','success');
  const transient=result.diagnostics?.some(item=>TRANSIENT_CODES.has(item.code));
  return transient?toneBadge('No response','warning'):toneBadge('Unreachable','error');
}

export function checkedLine(result){
  const date=parseTimestamp(result?.checked_at);
  if(!date)return 'Last checked: never';
  return `Last checked ${relativeLabel(date).toLowerCase()}${Number.isFinite(result.latency_ms)?` · ${result.latency_ms} ms`:''}`;
}

const SECTION_ICON={blocking:'blocked',warning:'errors',mapped:'check'};
export function section(title,count,tone,...children){
  return el('section',{className:`decision-section${tone?` decision-${tone}`:''}`},
    el('h3',{className:'decision-section-title'},SECTION_ICON[tone]?icon(SECTION_ICON[tone]):null,el('span',{},title),count===null||count===undefined?null:el('span',{className:'decision-count'},String(count))),...children);
}
