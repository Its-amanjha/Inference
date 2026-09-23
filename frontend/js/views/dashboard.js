import {api} from '../api.js';
import {state,can} from '../state.js';
import {el,button,heading,table,empty} from '../lib/html.js';
import {icon,registerIcons} from '../lib/icons.js';
import {severityPill,relativeTime,instanceName,explainHref,repairHref,n8nUrl,runBadge,runDuration as duration} from '../lib/triage.js';
import {loadingState,loadFailure,asOf,cachedProbe,fleetMetrics} from '../lib/payload.js';
import {rowMenu} from '../lib/row-menu.js';


// Adapted Lucide SVG paths, kept local and rendered at the shared UI stroke width.
// Source and license: frontend/licenses/lucide-LICENSE.txt.
registerIcons({
  'overview-queue':'M9 2h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2 M12 11h4 M12 16h4 M8 11h.01 M8 16h.01',
  'overview-server':'M4 2h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z M4 14h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2z M6 6h.01 M6 18h.01',
  'overview-retrieved':'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z m-6-3-5.5 5.5L8 12',
  'overview-partial':'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z M12 8v4 M12 16h.01',
  'overview-empty':'M22 12h-6l-2 3h-4l-2-3H2 M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'
});

export async function render(container) {
  const controller=new AbortController(),instance=state.instanceId;
  let busy=false;
  container.classList.add('dashboard');
  const stats=el('div',{class:'metric-strip',role:'group','aria-label':'Execution metrics'});
  const coverage=el('div',{class:'coverage'});
  const findings=el('div',{class:'dashboard-findings'}),fleet=el('div',{class:'panel-body dashboard-fleet'}),feed=el('div',{});
  const panel=(title,body,label,href,glyph=href.slice(1))=>el('section',{class:'panel'},el('header',{class:'panel-head'},el('h2',{},icon(glyph),title),label?el('a',{href},label,icon('arrow')):null),body);
  const scope=state.instances.find(row=>row.id===instance)?.name||'All instances';
  const sections=[
    {content:feed,short:'execution history',label:`recent executions for ${scope}`},
    {content:stats,short:'metrics',kind:'metrics',label:`execution metrics for ${scope}`},
    {content:findings,short:'findings',label:`investigation queue for ${scope}`}
  ].map(section=>({...section,feedback:el('div'),stamp:el('div'),loaded:false}));
  const wrap=index=>el('div',{class:`dashboard-section${index===2?' panel-body dashboard-investigation':''}`},sections[index].feedback,sections[index].stamp,sections[index].content);
  const refreshButton=button([icon('refresh'),'Refresh'],()=>refresh(),'ghost');
  const actions=el('div',{class:'actions'},refreshButton,el('a',{class:'button',href:can('admin')?'#instances':'#errors'},can('admin')?'Manage instances':'Investigate errors',icon('arrow')));
  // With nothing connected, one card says how to start instead of three empty panels saying the same thing.
  const step=(title,body,next)=>el('li',{},el('div',{class:'onboarding-step'},el('h3',{},title),el('p',{},body),next));
  const onboarding=()=>el('section',{class:'panel onboarding'},
    el('header',{class:'panel-head'},el('h2',{},icon('instances'),'Get your fleet on screen')),
    el('ol',{class:'onboarding-steps'},
      step('Connect an n8n instance','Its address and an API key from n8n (Settings, then n8n API). Workflows, runs and errors appear here.',
        can('admin')?el('a',{class:'button',href:'#instances'},icon('add'),'Connect an instance'):el('p',{class:'muted'},'An administrator connects instances.')),
      step('Send telemetry (optional)','Point the instance’s OpenTelemetry exporter at Inference to see each run’s steps and AI cost in Observability.',
        can('admin')?el('a',{class:'onboarding-link',href:'#instances'},'Telemetry settings are on each instance',icon('arrow')):null),
      step('Connect a model (optional)','An AI model you choose explains failed runs and proposes fixes you approve.',
        el('a',{class:'onboarding-link',href:'#assistant'},'Open the AI workspace',icon('arrow')))));
  container.append(el('div',{class:'page-heading-row'},heading('Fleet overview',`${scope} · Execution health and findings that need attention.`),actions),
    sections[1].feedback,sections[1].stamp,stats,coverage,
    ...(state.instances.length?[el('div',{class:'dashboard-columns'},panel('Investigation queue',wrap(2),'View all errors','#errors','overview-queue'),panel('Connected instances',fleet,'All instances','#instances','overview-server')),
      panel('Recent executions',wrap(0),'View traces','#observe')]:[onboarding()]));
  const scoped=instance?state.instances.filter(row=>row.id===instance):state.instances;
  function renderFleet(metrics) {
    fleet.replaceChildren();
    if(!scoped.length){fleet.append(empty('Connect your first instance','Connect your automation environment to see workflows and execution history.',[el('a',{class:'button ghost',href:'#instances'},icon('add'),'Open instances')],'instances'));return;}
    // One row per instance: name with its last connection check, then the numbers an operator compares.
    // A value the scan could not count is a dash with a reason on hover, never a zero.
    const count=(done,value)=>done&&Number.isFinite(value)?value:null;
    const rows=scoped.map(row=>{
      const evidence=metrics?.data?.find(item=>item.instance_id===row.id||item.id===row.id);
      const probe=cachedProbe(row);
      const health=probe?(probe.reachable?'up':'down'):'unknown';
      const status=probe?(probe.reachable?(Number.isFinite(probe.latency_ms)?`${probe.latency_ms} ms`:'Reachable'):'Unreachable'):'Not checked';
      const workflows=count(evidence?.workflows_complete,evidence?.observed_workflows);
      const runs=count(evidence?.executions_complete,evidence?.observed_executions_24h);
      const finished=runs!==null&&Number.isFinite(evidence?.terminal)?evidence.terminal:null;
      const missing=el('span',{class:'fleet-mini-missing',title:evidence?'Could not be counted from this instance':'Not loaded yet'},'—');
      return [
        el('div',{class:'fleet-mini-name'},el('a',{href:'#instances'},row.name),
          el('span',{class:`fleet-mini-status is-${health}`},el('span',{class:'fleet-mini-dot','aria-hidden':'true'}),status)),
        workflows===null?missing:el('span',{},String(workflows),Number.isFinite(evidence?.active_workflows)?el('span',{class:'fleet-mini-sub'},`${evidence.active_workflows} active`):null),
        runs===null?missing.cloneNode(true):String(runs),
        finished?`${Math.round(evidence.successes/finished*100)}%`:runs===null?missing.cloneNode(true):'—',
      ];
    });
    fleet.append(table(['Instance','Workflows','Runs · 24h','Success'],rows,{numeric:[1,2,3],className:'fleet-mini'}));
  }
  const names=new Map(),namesLoaded=new Set();
  const nameKey=(instanceId,workflowId)=>`${instanceId}:${workflowId}`;
  async function workflowNames(rows){
    const wanted=[...new Set(rows.filter(row=>!names.has(nameKey(row.instanceId,row.workflowId))).map(row=>row.instanceId))].filter(id=>!namesLoaded.has(id));
    await Promise.all(wanted.map(async instanceId=>{
      namesLoaded.add(instanceId);
      try{
        const result=await api('/api/n8n_proxy/workflows?'+new URLSearchParams({instance_id:instanceId,limit:'250'}),{signal:controller.signal});
        for(const row of result.data||[])names.set(nameKey(instanceId,row.id),row.name||String(row.id));
      }catch{/* Names are a convenience: without them the row still shows its workflow ID. */}
    }));
  }
  // Recent runs: the selected instance's, or the newest across the fleet (up to five instances, three at a
  // time), each tagged with its instance.
  async function recentRuns(){
    const targets=instance?[instance]:state.instances.slice(0,5).map(row=>row.id);
    if(!targets.length)return null;
    const lists=[];let next=0,failed=0;
    await Promise.all(Array.from({length:Math.min(3,targets.length)},async()=>{
      while(next<targets.length&&!controller.signal.aborted){
        const id=targets[next++];
        try{const result=await api('/api/n8n_proxy/executions?'+new URLSearchParams({instance_id:id,limit:'10'}),{signal:controller.signal});
          lists.push(...(result.data||[]).map(row=>({...row,instanceId:id})));}
        catch(error){if(instance)throw error;failed++;}
      }
    }));
    if(failed===targets.length)throw new Error('No instance returned its recent executions.');
    return {data:lists.sort((a,b)=>String(b.startedAt||'').localeCompare(String(a.startedAt||''))).slice(0,10),partial:failed>0};
  }
  function emptyExecutions(){return state.instances.length?empty('Choose an instance','Pick one in the switcher at the top of the page to see its runs.',[],'play'):empty('No instances connected',can('admin')?'Connect an n8n instance in Instances, then pick it in the switcher at the top of the page to see its recent executions.':'An administrator needs to connect an n8n instance before recent executions can appear.',can('admin')?[el('a',{class:'button ghost',href:'#instances'},'Open instances')]:[],'instances');}
  renderFleet();
  async function refresh() {
    if(busy||controller.signal.aborted)return;busy=true;refreshButton.disabled=true;
    for(const section of sections){
      section.feedback.replaceChildren();
      if(!section.loaded)section.content.replaceChildren(loadingState(`Loading ${section.label}…`,{compactLabel:`Loading ${section.short}`,kind:section.kind}));
      else section.feedback.append(el('p',{class:'load-progress',role:'status'},el('span',{class:'loading-spinner','aria-hidden':'true'}),el('span',{'aria-hidden':'true'},'Updating…'),el('span',{class:'sr-only'},`Refreshing ${section.label}…`)));
    }
    try {
      const query=instance?`?instance_id=${encodeURIComponent(instance)}`:'';
      const errorQuery=new URLSearchParams({status:'open',limit:'4'});if(instance)errorQuery.set('instance_id',instance);
      const requests=[
        recentRuns(),
        // The whole-fleet scan is shared with Fleet health and Instances; a rate-limited scan shows the last one.
        fleetMetrics({signal:controller.signal,fresh:true,instance}),
        api('/api/errors?'+errorQuery,{signal:controller.signal})
      ];
      const results=[];
      // Render independently: a slow fleet scan must not delay the findings.
      await Promise.all(requests.map(async(request,index)=>{
        let result;
        try{result={status:'fulfilled',value:await request};}
        catch(reason){result={status:'rejected',reason};}
        if(controller.signal.aborted)return;
        results[index]=result;
        // A failed refresh never discards this section's last good content.
        {
        const section=sections[index];section.feedback.replaceChildren();
        if(result.status==='fulfilled'){
          section.content.replaceChildren();section.loaded=true;
          if(index===1){const ran=new Date(result.value?.window_end);section.stamp.replaceChildren(asOf(Number.isNaN(ran.getTime())?new Date():ran));}
        }else{
          if(!section.loaded)section.content.replaceChildren();
          section.feedback.append(loadFailure(result.reason,refresh,`Could not refresh ${section.label}`));
        }
      }
      if(index===1&&results[1].status==='fulfilled'){
        coverage.replaceChildren();
        const metrics=results[1].value;
        // Null means the data could not be counted; it is shown as missing, never as zero or failure.
        // Fleet totals are null when any instance could not be counted. Show what the counted instances
        // reported instead, and say how much of the fleet that covers; nothing counted stays Unavailable.
        const rows=metrics.data||[];
        const counted=flag=>rows.filter(row=>row[flag]);
        const cover=done=>`From ${done.length} of ${rows.length} instances`;
        const sum=(done,key)=>done.reduce((total,row)=>total+(Number.isFinite(row[key])?row[key]:0),0);
        const wfDone=counted('workflows_complete'),exDone=counted('executions_complete');
        const workflows=metrics.workflows??(wfDone.length?sum(wfDone,'observed_workflows'):null);
        const executions=metrics.executions_24h??(exDone.length?sum(exDone,'observed_executions_24h'):null);
        const finished=metrics.success_rate_percent===null&&metrics.executions_24h===null&&exDone.length?sum(exDone,'terminal'):null;
        const rate=metrics.success_rate_percent!==null?`${Math.round(metrics.success_rate_percent)}%`
          :finished?`${Math.round(sum(exDone,'successes')/finished*100)}%`
          :metrics.terminal_executions_24h===0||finished===0?'No completed runs':null;
        const partial={workflows:metrics.workflows===null&&wfDone.length?cover(wfDone):null,
          executions:metrics.executions_24h===null&&exDone.length?cover(exDone):null};
        const incomplete=metrics.data.filter(row=>!row.workflows_complete||!row.executions_complete);
        const about=['Failures that the instances’ telemetry reported in the last 24 hours.','Executions started in the last 24 hours.',
          'Finished runs that succeeded. Running and waiting runs are not counted.','Workflows on the instances in view.'];
        for(const [index,[label,value,glyph,hint]] of [['Silent failures · 24h',metrics.confirmed_silent_error_spans_24h,'errors'],['Runs · 24h',executions,'play',partial.executions],['Success rate',rate,'check',partial.executions],['Total workflows',workflows,'workflows',partial.workflows]].entries()){
          const unknown=value===null,text=unknown?'Unavailable':String(value),labelId=`metric-label-${index}`,valueId=`metric-value-${index}`;
          const detail=unknown?(incomplete.length?'Coverage incomplete':'Not reported'):hint;
          // The label names the metric via an h2 heading; the value is associated with it via aria-describedby.
          stats.append(el('article',{class:`card metric-cell${unknown?' metric-unknown':''}`,'aria-labelledby':labelId,'aria-describedby':valueId,title:about[index]},el('div',{class:'metric-label'},icon(glyph),el('h3',{class:'muted metric-title',id:labelId,'aria-describedby':valueId},label)),el('p',{class:`metric-number${unknown||text.length>12?' metric-text':''}`,id:valueId},text),detail?el('p',{class:'metric-hint'},detail):null));
        }
        if(incomplete.length){
          const missing=row=>!row.workflows_complete&&!row.executions_complete?'workflow and execution history incomplete':!row.workflows_complete?'workflow list incomplete':'execution history incomplete';
          coverage.prepend(el('section',{class:'coverage-partial',role:'status'},icon('info'),el('div',{},
            el('h2',{},'Incomplete coverage'),
            el('p',{},'Some totals show as Unavailable. Missing data is not a failure.'),
            el('ul',{},incomplete.map(row=>el('li',{},el('strong',{},row.name),`: ${missing(row)}`))),
            )));
        }
        renderFleet(metrics);
      }
      if(index===2&&results[2].status==='fulfilled'){
        const rows=results[2].value.data;
        // All clear is one quiet line; the panel's own View all errors link is the way on.
        if(!rows.length)findings.append(el('div',{class:'queue-clear',role:'status'},icon('check'),
          el('div',{},el('p',{class:'queue-clear-title'},'No open findings'),el('p',{class:'muted'},'Nothing needs attention right now.'))));
        for(const row of rows){
          const occurrences=row.count===null||row.count===undefined?'Occurrences unavailable':`${row.count} ${row.count===1?'occurrence':'occurrences'}`;
          // Time and execution are shown only when the record carries them.
          const parts=[el('span',{},`${instanceName(row.instance_id)} · ${row.node_name||'Workflow'} · ${occurrences}`),row.occurred_at?relativeTime(row.occurred_at):null,row.execution_id?el('span',{},`Execution ${row.execution_id}`):null].filter(Boolean);
          // The title opens this error's evidence on Errors; everything else is in one row menu, as there.
          const execution=row.execution_id?n8nUrl(row.instance_id,row.workflow_id,row.execution_id):null;
          const menu=rowMenu([
            execution?{label:'Open execution in n8n',href:execution,external:true}:null,
            {label:'Explain with AI',href:explainHref(row)},
            can('operator')?{label:'Repair with AI',href:repairHref(row)}:null,
          ],`More actions for ${row.workflow_name}`);
          findings.append(el('article',{class:'finding-row'},
            el('div',{class:'finding-title'},el('a',{class:'finding-link',href:'#errors?'+new URLSearchParams({focus:row.id})},row.workflow_name),severityPill(row.error_type),menu),
            el('p',{},row.error_message),el('div',{class:'finding-meta'},parts.flatMap((part,position)=>position?[el('span',{class:'finding-sep','aria-hidden':'true'},'·'),part]:[part]))));
        }
      }
      if(index===0&&results[0].status==='fulfilled'&&!results[0].value)feed.append(emptyExecutions());
      else if(index===0&&results[0].status==='fulfilled'){
        const rows=(results[0].value.data||[]).slice(0,10);
        await workflowNames(rows);
        if(controller.signal.aborted)return;
        const fleetWide=!instance;
        feed.append(rows.length?table(['Workflow',...(fleetWide?['Instance']:[]),'Status','Started','Duration','Execution'],rows.map(row=>{
          const name=names.get(nameKey(row.instanceId,row.workflowId));
          return [
            el('div',{class:'run-workflow'},el('span',{class:'run-workflow-name'},name||'Unknown workflow'),name?null:el('span',{class:'run-workflow-id'},row.workflowId??'')),
            ...(fleetWide?[instanceName(row.instanceId)]:[]),
            runBadge(row.status),
            relativeTime(row.startedAt),
            el('span',{class:'run-duration'},duration(row.startedAt,row.stoppedAt,row.status)),
            // The run ID opens that execution in n8n when the instance URL allows it.
            n8nUrl(row.instanceId,row.workflowId,row.id)?el('a',{class:'run-id',href:n8nUrl(row.instanceId,row.workflowId,row.id),target:'_blank',rel:'noopener noreferrer','aria-label':`${row.id}: open execution in n8n`},String(row.id),icon('external')):el('span',{class:'run-id'},String(row.id)),
          ];
        }),{numeric:fleetWide?[4,5]:[3,4],className:'runs-table'}):empty('No recent executions',fleetWide?'No instance returned saved executions.':'This instance returned no execution records.',[button('Refresh executions',()=>refresh(),'ghost')],'play'));
        if(results[0].value.partial)feed.append(el('p',{class:'muted run-note'},'Some instances could not be read; their runs are missing here.'));
      }
      }));
    }finally{busy=false;refreshButton.disabled=false;}
  }
  const run=()=>refresh().catch(error=>{if(!controller.signal.aborted)sections[1].feedback.replaceChildren(loadFailure(error,run,'Could not refresh the dashboard'));});
  run();
  const timer=setInterval(()=>{if(!document.hidden)run();},30000);
  return()=>{controller.abort();clearInterval(timer);};
}
