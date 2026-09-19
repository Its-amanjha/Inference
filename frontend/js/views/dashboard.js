import {api} from '../api.js';
import {state,pageSize,can} from '../state.js';
import {el,button,heading,table,empty} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {severityPill,relativeTime,instanceName,explainHref,repairHref} from '../lib/triage.js';
import {loadingState,loadFailure,asOf} from '../lib/payload.js';

export async function render(container) {
  const controller=new AbortController(),instance=state.instanceId;
  let busy=false;
  container.classList.add('dashboard');
  const stats=el('div',{class:'metric-strip',role:'group','aria-label':'Execution metrics'});
  const coverage=el('div',{class:'coverage'}),telemetry=el('section',{class:'telemetry-strip','aria-label':'Retained telemetry'});
  const findings=el('div',{class:'panel-body'}),fleet=el('div',{class:'panel-body'}),feed=el('div',{});
  const panel=(title,body,label,href)=>el('section',{class:'panel'},el('header',{class:'panel-head'},el('h2',{},icon(href.slice(1)),title),label?el('a',{href},label):null),body);
  const scope=state.instances.find(row=>row.id===instance)?.name||'All instances';
  const sections=[
    {content:telemetry,label:`retained telemetry for ${scope}`},
    {content:feed,label:`recent executions for ${scope}`},
    {content:stats,label:`execution metrics for ${scope}`},
    {content:findings,label:`investigation queue for ${scope}`}
  ].map(section=>({...section,feedback:el('div'),stamp:el('div'),loaded:false}));
  const wrap=index=>el('div',{},sections[index].feedback,sections[index].stamp,sections[index].content);
  const refreshButton=button([icon('refresh'),'Refresh'],()=>refresh(),'ghost');
  const actions=el('div',{class:'actions'},refreshButton,el('a',{class:'button',href:can('admin')?'#instances':'#errors'},can('admin')?'Manage instances':'Investigate errors',icon('arrow')));
  container.append(el('div',{class:'page-heading-row'},heading('Fleet overview',`${scope} · Execution health and findings that need attention.`),actions),
    sections[2].feedback,sections[2].stamp,stats,coverage,el('div',{class:'dashboard-columns'},panel('Investigation queue',wrap(3),'View all errors','#errors'),panel('Connected instances',fleet,'Fleet health','#fleet-health')),
    panel('Recent executions',wrap(1),'View traces','#observe'),wrap(0));
  const scoped=instance?state.instances.filter(row=>row.id===instance):state.instances;
  function renderFleet(metrics) {
    fleet.replaceChildren();
    if(!scoped.length){fleet.append(empty('Connect your first instance','Connect your automation environment to see workflows and execution history.'),el('a',{class:'button ghost',href:'#instances'},icon('add'),'Open instances'));return;}
    for(const row of scoped){
      const evidence=metrics?.data?.find(item=>item.instance_id===row.id||item.id===row.id);
      const complete=evidence?.workflows_complete&&evidence?.executions_complete;
      fleet.append(el('article',{class:'instance-row'},el('a',{href:'#instances'},icon('instances'),row.name),el('p',{},row.url),el('span',{class:`badge ${evidence?(complete?'info':'warning'):''}`},evidence?(complete?'History retrieved':'Partial coverage'):'Connection saved')));
    }
  }
  renderFleet();
  async function refresh() {
    if(busy||controller.signal.aborted)return;busy=true;refreshButton.disabled=true;
    for(const section of sections){
      section.feedback.replaceChildren();
      if(!section.loaded)section.content.replaceChildren(loadingState(`Loading ${section.label}…`));
      else section.feedback.append(el('p',{class:'load-progress',role:'status'},`Refreshing ${section.label}…`));
    }
    try {
      const query=instance?`?instance_id=${encodeURIComponent(instance)}`:'';
      const errorQuery=new URLSearchParams({status:'open',limit:'4'});if(instance)errorQuery.set('instance_id',instance);
      const results=await Promise.allSettled([
        api('/api/otel/summary'+query,{signal:controller.signal}),
        instance?api('/api/n8n_proxy/executions'+query+'&limit='+pageSize(),{signal:controller.signal}):Promise.resolve(null),
        api('/api/n8n_proxy/dashboard'+query,{signal:controller.signal}),
        api('/api/errors?'+errorQuery,{signal:controller.signal})
      ]);
      if(controller.signal.aborted)return;
      // Each response owns one section. A failed refresh never discards its last good content.
      for(const [index,result] of results.entries()){
        const section=sections[index];section.feedback.replaceChildren();
        if(result.status==='fulfilled'){
          section.content.replaceChildren();section.stamp.replaceChildren(asOf());section.loaded=true;
        }else{
          if(!section.loaded)section.content.replaceChildren();
          section.feedback.append(loadFailure(result.reason,refresh,`Could not refresh ${section.label}`));
        }
      }
      if(results[2].status==='fulfilled'){
        coverage.replaceChildren();
        const metrics=results[2].value;
        // Null means the data could not be counted; it is shown as missing, never as zero or failure.
        const rate=metrics.success_rate_percent===null?(metrics.terminal_executions_24h===0?'No completed runs':null):`${metrics.success_rate_percent}%`;
        const incomplete=metrics.data.filter(row=>!row.workflows_complete||!row.executions_complete);
        for(const [index,[label,value,glyph,hint]] of [['Confirmed silent-error spans · 24h',metrics.confirmed_silent_error_spans_24h,'errors','Silent failures confirmed from received telemetry'],['Saved executions · 24h',metrics.executions_24h,'play'],['Success rate · completed runs',rate,'check'],['Total workflows',metrics.workflows,'workflows']].entries()){
          const unknown=value===null,text=unknown?'Unavailable':String(value),labelId=`metric-label-${index}`;
          const detail=unknown?(incomplete.length?'Coverage incomplete':'Not reported'):hint;
          // The label names the metric; the value is plain text so heading navigation is not a list of bare numbers.
          stats.append(el('article',{class:`card metric-cell${unknown?' metric-unknown':''}`,'aria-labelledby':labelId},el('div',{class:'metric-label'},icon(glyph),el('p',{class:'muted',id:labelId},label)),el('p',{class:`metric-number${unknown||text.length>12?' metric-text':''}`},text),detail?el('p',{class:'metric-hint'},detail):null));
        }
        coverage.append(el('details',{class:'coverage-details'},el('summary',{},'How these metrics are counted'),el('p',{},`${metrics.scope} Success rate excludes running/waiting executions. Silent-error spans count only received telemetry. Window ends ${new Date(metrics.window_end).toLocaleString()}.`)));
        if(incomplete.length){
          const missing=row=>!row.workflows_complete&&!row.executions_complete?'workflow and execution history incomplete':!row.workflows_complete?'workflow list incomplete':'execution history incomplete';
          coverage.prepend(el('section',{class:'coverage-partial',role:'status'},icon('info'),el('div',{},
            el('h2',{},'Incomplete coverage'),
            el('p',{},'Totals that depend on these instances show as Unavailable. Data is missing; this does not mean anything failed.'),
            el('ul',{},incomplete.map(row=>el('li',{},el('strong',{},row.name),`: ${missing(row)}`))),
            el('p',{class:'muted'},'Possible causes: the instance was unreachable, returned data this version does not support, or has more than 2,500 records per resource.'))));
        }
        renderFleet(metrics);
      }
      if(results[0].status==='fulfilled'){
        for(const [label,key] of [['Retained traces','traces'],['Recorded spans','spans'],['Error spans','error_spans']])telemetry.append(el('div',{class:'telemetry-stat'},el('span',{},label),el('strong',{},String(results[0].value[key]))));
        telemetry.append(el('p',{class:'muted'},'Everything kept in retention, not only the last 24 hours. Error spans count every failed span; the silent-error metric above counts only confirmed silent failures from the last 24 hours. Missing telemetry does not prove failure.'));
      }
      if(results[3].status==='fulfilled'){
        const rows=results[3].value.data;
        if(!rows.length){const blank=empty('No open findings',scoped.length?'No recorded errors match this instance selection. Telemetry coverage may be incomplete.':'Connect an instance and configure telemetry to start investigating execution anomalies.');blank.prepend(icon('observe'));findings.append(blank);}
        for(const row of rows){
          const occurrences=row.count===null||row.count===undefined?'Occurrences unavailable':`${row.count} occurrence(s)`;
          // Time and execution are shown only when the record carries them.
          const parts=[el('span',{},`${instanceName(row.instance_id)} · ${row.node_name||'Workflow'} · ${occurrences}`),row.occurred_at?relativeTime(row.occurred_at):null,row.execution_id?el('span',{},`Execution ${row.execution_id}`):null].filter(Boolean);
          findings.append(el('article',{class:'finding-row'},el('div',{class:'finding-title'},el('strong',{},row.workflow_name),severityPill(row.error_type)),
            el('p',{},row.error_message),el('div',{class:'finding-meta'},parts.flatMap((part,position)=>position?[el('span',{class:'finding-sep','aria-hidden':'true'},'·'),part]:[part]),el('span',{class:'finding-actions'},el('a',{href:explainHref(row)},'Explain with AI'),can('operator')?el('a',{href:repairHref(row)},'Repair with AI'):null))));
        }
        if(rows.length)findings.append(el('p',{class:'muted'},'Latest open findings. Review all errors for earlier records.'));
      }
      if(results[1].status==='fulfilled'&&!results[1].value)feed.append(state.instances.length?empty('Choose an instance','Pick an instance in the switcher at the top of the page to see its recent executions.'):empty('No instances connected',can('admin')?'Connect an n8n instance in Instances, then pick it in the switcher at the top of the page to see its recent executions.':'An administrator needs to connect an n8n instance before recent executions can appear.'));
      else if(results[1].status==='fulfilled'){
        const rows=results[1].value.data;
        const badge=status=>el('span',{class:`badge ${['success','completed'].includes(status)?'success':['error','crashed'].includes(status)?'error':['running','waiting'].includes(status)?'info':''}`},status||'Unknown');
        feed.append(rows.length?table(['Execution','Workflow','Status','Started'],rows.map(row=>[String(row.id),row.workflowId??'—',badge(row.status),row.startedAt?new Date(row.startedAt).toLocaleString():'—'])):empty('No recent executions','No execution records were returned by this instance.'),el('p',{class:'muted feed-caption'},`Latest ${pageSize()} executions from the selected instance. Refreshes every 30 seconds.`));
      }
    }finally{busy=false;refreshButton.disabled=false;}
  }
  const run=()=>refresh().catch(error=>{if(!controller.signal.aborted)sections[2].feedback.replaceChildren(loadFailure(error,run,'Could not refresh the dashboard'));});
  run();
  const timer=setInterval(()=>{if(!document.hidden)run();},30000);
  return()=>{controller.abort();clearInterval(timer);};
}
