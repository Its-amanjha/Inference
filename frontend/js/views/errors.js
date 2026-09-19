import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,field,heading,table,notice,empty} from '../lib/html.js';
import {severityOf,severityPill,relativeTime,instanceName,explainHref,repairHref,TRIAGE_FILTERS} from '../lib/triage.js';

const COLUMNS=[['Severity'],['When'],['Workflow / node'],['Instance'],['Count','triage-num'],['Execution'],['Actions','triage-actions-head']];
const LONG_MESSAGE=100;

// Message text stays a text node; long messages keep the full text in a disclosure.
function message(text) {
  const value=text===null||text===undefined?'':String(text);
  if(!value)return el('p',{class:'triage-message triage-missing'},'Message unavailable');
  const clamped=el('p',{class:'triage-message'},value);
  if(value.length<=LONG_MESSAGE&&!value.includes('\n'))return clamped;
  return [clamped,el('details',{class:'triage-full'},el('summary',{},'Full message'),el('p',{},value))];
}

export async function render(container){
  const controller=new AbortController(),signal=controller.signal,instance=state.instanceId;
  const status=el('select',{},el('option',{value:'open'},'Open'),el('option',{value:'resolved'},'Resolved'));
  let rows=[],cursor=null,busy=false,queued=false,filter='all',loadedStatus=status.value;
  const feedback=el('div',{class:'triage-feedback'}),content=el('div',{class:'triage-results',tabindex:-1});
  const count=el('p',{class:'triage-count','aria-live':'polite'}),announcer=el('p',{class:'triage-announce',role:'status'});
  const chips=TRIAGE_FILTERS.map(([key,label])=>el('button',{type:'button',class:'button ghost triage-chip','aria-pressed':String(key===filter),onclick:()=>setFilter(key)},label));
  const next=button('Older errors',()=>load(cursor),'button ghost');next.disabled=true;
  status.addEventListener('change',()=>load(),{signal});
  container.append(heading('Error triage','Confirmed errors and evidence-based findings, newest first.'),
    el('div',{class:'toolbar'},field('Status',status),button('Refresh',()=>load(),'button ghost')),
    el('div',{class:'triage-filterbar'},el('div',{class:'triage-filters',role:'group','aria-label':'Filter loaded errors by type'},chips),
      el('div',{class:'triage-summary'},count,el('p',{},'Filters apply to loaded errors only.'))),
    announcer,feedback,content,next);

  const visibleRows=()=>filter==='all'?rows:rows.filter(row=>severityOf(row.error_type).kind===filter);
  function updateCount(){count.textContent=rows.length?`Showing ${visibleRows().length} of ${rows.length} loaded`:'No errors loaded';}
  function setFilter(key){
    filter=key;
    chips.forEach((chip,index)=>chip.setAttribute('aria-pressed',String(TRIAGE_FILTERS[index][0]===key)));
    draw();
  }
  function draw(){
    updateCount();
    const shown=visibleRows();
    if(!rows.length){content.replaceChildren(empty('Nothing to triage',loadedStatus==='open'?'No open errors for this instance selection. An empty list does not prove every execution succeeded; telemetry coverage may be incomplete.':'No resolved errors for this instance selection. Errors marked resolved from the Open list appear here.',loadedStatus==='resolved'?[button('Check again',()=>load(),'ghost'),button('Back to Open',()=>{status.value='open';load();},'ghost')]:[button('Check again',()=>load(),'ghost')]));return;}
    if(!shown.length){content.replaceChildren(empty('No loaded errors of this type',cursor?'Choose All, or load older errors to look further back.':'Choose All to see the other loaded errors.',[button('Show all types',()=>setFilter('all'),'ghost')]));return;}
    content.replaceChildren(table(
      ['Severity','When','Workflow / node','Instance','Count','Execution','Actions'],
      shown.map(renderRowCells),
      {
        className:'triage-table',
        wrapClass:'triage-table-wrap',
        ariaLabel:'Errors table',
        numeric:[4],
        sortable:{0:'text',1:'date',2:'text',3:'text',4:'number',5:'text'},
        initialSort:{column:1,direction:'desc'}
      }
    ));
  }
  function renderRowCells(row){
    const workflow=row.workflow_name||'Workflow name unavailable',node=row.node_name||'Workflow',name=`${workflow} · ${node}`;
    const cell=(label,className,...children)=>el('td',{class:className,'data-label':label},children);
    const actions=el('div',{class:'triage-action-group'},el('a',{class:'button ghost',href:explainHref(row)},'Explain with AI'));
    // Repair pins this error's workflow as the AI target; applying any change still needs an operator's approval.
    if(can('operator'))actions.append(el('a',{class:'button ghost',href:repairHref(row)},'Repair with AI'));
    if(loadedStatus==='open'&&can('operator')){
      const resolve=button('Mark resolved',()=>resolveRow(row,resolve,name),'button ghost');
      resolve.dataset.action='resolve';actions.append(resolve);
    }
    return [
      {sortValue:severityOf(row.error_type).label,content:cell('Severity','triage-severity',severityPill(row.error_type))},
      {sortValue:row.occurred_at?new Date(row.occurred_at).getTime():0,content:cell('When','triage-when',relativeTime(row.occurred_at))},
      {sortValue:`${workflow} · ${node}`,content:cell('Workflow / node','triage-subject',el('div',{class:'triage-subject-body'},el('div',{class:'triage-title'},el('strong',{},workflow),el('span',{class:'triage-node'},node)),message(row.error_message)))},
      {sortValue:instanceName(row.instance_id),content:cell('Instance','triage-instance',instanceName(row.instance_id))},
      {sortValue:row.count??0,content:cell('Count','triage-num',row.count===null||row.count===undefined?el('span',{class:'triage-missing'},'Unavailable'):String(row.count))},
      {sortValue:row.execution_id?String(row.execution_id):'',content:cell('Execution','triage-execution',row.execution_id?String(row.execution_id):el('span',{class:'triage-missing'},'Unavailable'))},
      cell('Actions','triage-actions',actions)
    ];
  }
  async function resolveRow(row,control,name){
    control.disabled=true;feedback.replaceChildren();announcer.textContent='';
    try{await api(`/api/errors/${encodeURIComponent(row.id)}/resolve`,{method:'POST'});}
    catch(error){
      if(signal.aborted)return;
      control.disabled=false;
      notice(feedback,Object.assign(new Error(`Could not mark ${name} resolved. ${error.message}`),{status:error.status,code:error.code}));
      control.focus();return;
    }
    if(signal.aborted)return;
    // Remove only this row; the rest of the loaded page, filters and cursor stay as they are.
    rows=rows.filter(item=>item!==row);
    draw();
    announcer.textContent=`${name} marked resolved.`;
  }
  async function load(before=null){
    if(busy){if(!before)queued=true;return;}
    busy=true;next.disabled=true;content.setAttribute('aria-busy','true');
    const requested=status.value;
    try{
      const params=new URLSearchParams({status:requested,limit:String(pageSize())});
      if(instance)params.set('instance_id',instance);
      if(before)params.set('before',before);
      const result=await api('/api/errors?'+params,{signal});
      if(!before){feedback.replaceChildren();announcer.textContent='';}
      loadedStatus=requested;rows=before?rows.concat(result.data):result.data;cursor=result.next_cursor;
      draw();
    }catch(error){if(!signal.aborted)notice(feedback,error);}
    finally{
      busy=false;content.removeAttribute('aria-busy');next.disabled=!cursor;
      if(queued&&!signal.aborted){queued=false;load();}
    }
  }
  await load();return()=>controller.abort();
}
