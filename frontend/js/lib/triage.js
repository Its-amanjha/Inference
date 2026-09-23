// Shared severity, time and target helpers for the Errors view and the dashboard investigation queue.
import {el} from './html.js';
import {icon,registerIcons} from './icons.js';
import {state} from '../state.js';

registerIcons({
  'severity-error':'M8.2 3h7.6L21 8.2v7.6L15.8 21H8.2L3 15.8V8.2L8.2 3z M9 9l6 6 M15 9l-6 6',
  'severity-silent':'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7.5v5.5 M12 16.5v.1',
  'severity-warning':'M12 3 2 21h20L12 3z M12 9v5 M12 17v.1',
  'severity-neutral':'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M8 12h8',
});

// Recorded values: classifier span_error/continuation_error/zero_output, the error_record
// fallback ExecutionError and the errors table default 'Error'.
const EXECUTION_TYPES=new Set(['span_error','ExecutionError','execution_error','Error']);
const GLYPHS={silent:'severity-silent',zero_output:'severity-warning',execution:'severity-error',other:'severity-neutral'};
export const TRIAGE_FILTERS=[['all','All'],['execution','Execution errors'],['silent','Silent failures'],['zero_output','Zero-output warnings']];

function humanize(value) {
  const words=String(value).replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/[_\-.]+/g,' ').trim().toLowerCase();
  return words?words[0].toUpperCase()+words.slice(1):'';
}

export function severityOf(type) {
  if(type==='continuation_error')return {level:'error',label:'Silent failure',kind:'silent'};
  if(type==='zero_output')return {level:'warning',label:'Zero-output warning',kind:'zero_output'};
  if(EXECUTION_TYPES.has(type))return {level:'error',label:'Execution error',kind:'execution'};
  return {level:'neutral',label:(typeof type==='string'&&humanize(type))||'Type unavailable',kind:'other'};
}

// Icon plus text plus color class; color never carries the meaning alone.
export function severityPill(type) {
  const severity=severityOf(type);
  return el('span',{class:`badge severity-pill${severity.level==='neutral'?'':` ${severity.level}`}`},icon(GLYPHS[severity.kind]),severity.label);
}

const ISO_DATE=/^\d{4}-\d{2}-\d{2}/;
const SQLITE_UTC=/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;
export function parseTimestamp(value) {
  if(typeof value!=='string'||!ISO_DATE.test(value.trim()))return null;
  const text=value.trim();
  // SQLite datetime('now') is UTC without a zone designator.
  const date=new Date(SQLITE_UTC.test(text)?`${text.replace(' ','T')}Z`:text);
  return Number.isNaN(date.getTime())?null:date;
}

const absoluteFormat=new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short'});
export function relativeLabel(date,now=Date.now()) {
  const seconds=Math.round((now-date.getTime())/1000);
  if(seconds<-60)return absoluteFormat.format(date);
  if(seconds<60)return 'Just now';
  const minutes=Math.floor(seconds/60);if(minutes<60)return `${minutes} min ago`;
  const hours=Math.floor(minutes/60);if(hours<24)return `${hours} h ago`;
  const days=Math.floor(hours/24);if(days<7)return days===1?'1 day ago':`${days} days ago`;
  const sameYear=date.getFullYear()===new Date(now).getFullYear();
  return new Intl.DateTimeFormat(undefined,sameYear?{month:'short',day:'numeric'}:{year:'numeric',month:'short',day:'numeric'}).format(date);
}

// Relative text for scanning; the absolute local time is in title and in visually hidden text.
export function relativeTime(value,now=Date.now()) {
  if(value===null||value===undefined||value==='')return el('span',{class:'relative-time triage-missing'},'Unavailable');
  const date=parseTimestamp(value);
  if(!date)return el('span',{class:'relative-time'},String(value));
  const absolute=absoluteFormat.format(date),label=relativeLabel(date,now);
  return el('time',{class:'relative-time',datetime:date.toISOString(),title:absolute},label,label===absolute?null:el('span',{class:'sr-only'},`, ${absolute}`));
}

export const instanceName=id=>state.instances.find(item=>item.id===id)?.name||id||'Unavailable';
// An n8n execution status as a badge: icon, word and tone together.
const RUN_STATES={success:['success','Succeeded','check'],error:['error','Failed','errors'],crashed:['error','Crashed','errors'],
  running:['info','Running','refresh'],waiting:['info','Waiting','clock'],new:['info','Queued','clock'],canceled:['','Canceled','close']};
export function runBadge(status){
  const [tone,label,glyph]=RUN_STATES[status]||['',status?String(status):'Unknown','info'];
  return el('span',{class:`badge run-badge${tone?` ${tone}`:''}`},icon(glyph),label);
}
export function runDuration(started,stopped,status){
  const from=Date.parse(started),to=Date.parse(stopped);
  if(!Number.isFinite(from)||!Number.isFinite(to)||to<from)return ['running','waiting','new'].includes(status)?'In progress':'—';
  const ms=to-from;
  return ms<1000?`${ms} ms`:ms<60000?`${(ms/1000).toFixed(ms<10000?1:0)} s`:ms<3600000?`${Math.round(ms/60000)} min`:`${(ms/3600000).toFixed(1)} h`;
}
// A link into the n8n editor: the execution when there is one, otherwise the workflow. It uses the URL the
// instance was connected with, so it opens wherever that URL is reachable from this browser.
export function n8nUrl(instanceId,workflowId,executionId=null){
  const base=state.instances.find(item=>item.id===instanceId)?.url;
  if(!base||!workflowId)return null;
  let root;try{root=new URL(base);}catch{return null;}
  if(root.protocol!=='http:'&&root.protocol!=='https:')return null;
  const origin=root.origin+root.pathname.replace(/\/+$/,'').replace(/\/api\/v1$/,'');
  const workflow=`${origin}/workflow/${encodeURIComponent(workflowId)}`;
  return executionId?`${workflow}/executions/${encodeURIComponent(executionId)}`:workflow;
}
export const explainHref=row=>'#assistant?'+new URLSearchParams({error_id:row.id,instance_id:row.instance_id});
// Repair links pin a target for a person to review; error rows pass their id, workflow rows pass only instance_id and workflow_id.
export const repairHref=row=>'#assistant?'+new URLSearchParams([['mode','repair'],['instance_id',row?.instance_id],['workflow_id',row?.workflow_id],['error_id',row?.id]]
  .filter(([,value])=>value!==null&&value!==undefined&&value!=='').map(([key,value])=>[key,String(value)]));

// Per-user browser localStorage saved views for triage and operational filters
export function getSavedViews(scope = 'errors', user = state.user?.username || 'anonymous') {
  try {
    const raw = localStorage.getItem(`inf_saved_views_${user}_${scope}`);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function saveView(scope = 'errors', user = state.user?.username || 'anonymous', name, filters) {
  if (!name || typeof name !== 'string' || !name.trim()) return false;
  const trimmed = name.trim();
  const existing = getSavedViews(scope, user).filter(v => v.name !== trimmed);
  existing.push({ name: trimmed, filters, savedAt: Date.now() });
  try {
    localStorage.setItem(`inf_saved_views_${user}_${scope}`, JSON.stringify(existing));
    return true;
  } catch {
    return false;
  }
}

export function deleteSavedView(scope = 'errors', user = state.user?.username || 'anonymous', name) {
  const existing = getSavedViews(scope, user).filter(v => v.name !== name);
  try {
    localStorage.setItem(`inf_saved_views_${user}_${scope}`, JSON.stringify(existing));
    return true;
  } catch {
    return false;
  }
}

// Delegates to the platform shell shortcut registry
export function registerShortcuts(shortcuts, title = '') {
  if (typeof window.registerShortcuts === 'function') {
    return window.registerShortcuts(shortcuts, title);
  }
  return () => {};
}


