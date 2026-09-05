// Readable summaries of API payloads. Every sentence here is derived from response fields;
// anything not understood stays visible in the raw disclosure instead of being guessed.
import {el,jsonView} from './html.js';
import {icon,registerIcons} from './icons.js';
import {state} from '../state.js';

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
  return result?.reachable?toneBadge('Reachable','success'):toneBadge('Unreachable','error');
}

export function checkedLine(result){
  if(!result?.checked_at)return 'No completed check';
  return `Checked ${formatTime(result.checked_at)}${Number.isFinite(result.latency_ms)?` · ${result.latency_ms} ms`:''}`;
}

const SECTION_ICON={blocking:'blocked',warning:'errors',mapped:'check'};
export function section(title,count,tone,...children){
  return el('section',{className:`decision-section${tone?` decision-${tone}`:''}`},
    el('h3',{className:'decision-section-title'},SECTION_ICON[tone]?icon(SECTION_ICON[tone]):null,el('span',{},title),count===null||count===undefined?null:el('span',{className:'decision-count'},String(count))),...children);
}
