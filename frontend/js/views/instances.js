import {api as request} from '../api.js';
import {el,button,field,formCard,notice,empty,heading,table} from '../lib/html.js';
import {icon,registerIcons} from '../lib/icons.js';
import {confirmAction,actionMenu,stateBadge} from '../lib/confirm.js';
import {can} from '../state.js';
import {relativeLabel} from '../lib/triage.js';
import {rawDisclosure,probeSummary,reachabilityBadge,checkedLine,loadingState,loadFailure,cachedProbe,rememberProbe,forgetProbe,checkConnection,asOf,fleetMetrics} from '../lib/payload.js';

registerIcons({
  server:'M4 6h16M4 10h16M4 14h16M4 18h16 M2 4a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v16a2 2 0 0 1 2 2H4a2 2 0 0 1-2-2V4z M6 6h.01 M6 10h.01 M6 14h.01 M6 18h.01',
  shield:'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
});

function probeResult(result){
  const {list,notes,diagnostics}=probeSummary(result);
  return el('div',{className:'stack decision-probe',role:'status'},
    el('div',{className:'decision-head'},el('h3',{},'Connection test'),reachabilityBadge(result)),
    el('p',{className:'muted',title:result.checked_at},checkedLine(result)),asOf(new Date(result.checked_at)),
    list,
    diagnostics.length?notes:el('p',{},'Public API checks passed.'),
    rawDisclosure(result));
}

// Probe outcome in plain words: reachable with full API access, reachable with
// limits, unreachable, or not checked yet.
function classifyProbe(result){
  if(!result||typeof result.reachable!=='boolean')return 'untested';
  if(!result.reachable)return 'unreachable';
  const caps=result.capabilities||{};
  const full=caps.workflow_read==='supported'&&caps.execution_read==='supported'
    &&!(result.diagnostics||[]).length;
  return full?'operational':'degraded';
}
const CLASS_RANK={operational:0,degraded:1,unreachable:2,untested:3};

export async function render(container) {
  const controller=new AbortController(), signal=controller.signal;
  const entryRoute=(location.hash.slice(1).split('?')[0]||'dashboard');
  const messages=el('div'), editor=el('div');
  container.append(
    heading(entryRoute==='fleet-health'?'Fleet health':'Instances',
      'Connections, health and telemetry for every n8n instance.'),
    messages,editor);
  if(can('admin'))container.append(button('Add instance',()=>edit()));
  const rollup=el('div',{class:'fleet-summary-strip',role:'region','aria-label':'Fleet health totals'});
  const totalNum=el('p',{class:'metric-number'},'0'),operationalNum=el('p',{class:'metric-number'},'0'),
    degradedNum=el('p',{class:'metric-number'},'0'),unreachableNum=el('p',{class:'metric-number'},'—');
  rollup.append(
    el('div',{class:'fleet-metric-cell'},el('h3',{},'Total instances'),totalNum),
    el('div',{class:'fleet-metric-cell metric-operational'},el('h3',{},'Operational'),operationalNum),
    el('div',{class:'fleet-metric-cell metric-degraded'},el('h3',{},'Degraded'),degradedNum),
    el('div',{class:'fleet-metric-cell metric-unreachable'},el('h3',{},'Unreachable'),unreachableNum));
  const filterAll=button('All instances',()=>setFilter('all'),'ghost fleet-filter-btn',{'aria-pressed':'true'});
  const filterIssues=button('Issues only',()=>setFilter('issues'),'ghost fleet-filter-btn',{'aria-pressed':'false'});
  const recheckAll=button('Check fleet',()=>checkAll(),'ghost');
  const toolbar=el('div',{class:'fleet-toolbar'},el('div',{class:'fleet-filter-group'},filterAll,filterIssues),recheckAll);
  const tableWrap=el('div');
  const detail=el('section',{class:'card stack fleet-detail',hidden:true,'aria-label':'Instance details'});
  container.append(rollup,toolbar,tableWrap,detail);
  let writing=false;
  let generation=0,checks=null,currentFilter='all',fleetTable=null,entries=[],versions={};
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  async function copyText(text,btn){
    try{await navigator.clipboard.writeText(text);}
    catch{
      const area=el('textarea',{value:text,readonly:true,style:'position:fixed;opacity:0'});
      document.body.append(area);area.select();
      try{document.execCommand('copy');}catch{}area.remove();
    }
    if(btn?.isConnected){const label=btn.textContent;btn.textContent='Copied';
      setTimeout(()=>{if(btn.isConnected)btn.textContent=label;},1500);}
  }
  // Per-instance telemetry setup: exporter URL, env snippet, last received span,
  // and (administrators only) token create-shown-once-copy / list / revoke.
  function telemetry(entry){
    const item=entry.item;
    const base=location.origin+'/api/otel';
    const body=el('div',{class:'telemetry-body'});
    const details=el('details',{class:'telemetry-section',id:`telemetry-${item.id}`},
      el('summary',{},'Telemetry'),body);
    const arrival=el('p',{role:'status'},'Checking for received spans…');
    const snippet=el('pre',{class:'mono telemetry-snippet'},
      ['N8N_OTEL_ENABLED=true',
       `N8N_OTEL_EXPORTER_OTLP_ENDPOINT=${base}`,
       'N8N_OTEL_EXPORTER_OTLP_TRACING_PATH=/v1/traces',
       'N8N_OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer PASTE_TOKEN_HERE',
       `OTEL_EXPORTER_OTLP_ENDPOINT=${base}`,
       'OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer PASTE_TOKEN_HERE',
       'OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf'].join('\n'));
    const copyUrl=button('Copy URL',()=>copyText(base,copyUrl),'ghost');
    const copySnippet=button('Copy settings',()=>copyText(snippet.textContent,copySnippet),'ghost');
    body.append(
      el('p',{},'Exporter URL: ',el('code',{class:'mono'},base),' ',copyUrl),
      el('div',{class:'telemetry-snippet-row'},snippet,copySnippet),
      arrival);
    lastSpan(item).then(when=>{
      if(signal.aborted)return;
      arrival.replaceChildren(!when?'No spans received yet.':['Last span received ',relativeLabel(when),'.']);
    }).catch(()=>{if(!signal.aborted)arrival.replaceChildren('No spans received yet.');});
    if(!can('admin')){
      body.append(el('p',{class:'muted'},'An administrator manages exporter tokens.'));
      return details;
    }
    const created=el('div'),tokens=el('div');
    const name=el('input',{autocomplete:'off',maxLength:120,placeholder:'Token name'});
    const copyToken=button('Copy token',()=>copyText(created.querySelector('.telemetry-token')?.textContent||'',copyToken),'ghost');
    const create=button('Create token',()=>act(async()=>{
      const label=name.value.trim();if(!label){name.focus();return;}
      create.disabled=true;
      try{
        const result=await api('/api/otel/tokens',{method:'POST',json:{instance_id:item.id,name:label}});
        name.value='';
        created.replaceChildren(el('p',{role:'alert'},
          el('strong',{},'Copy this token now. You will not see it again.')),
          el('div',{class:'telemetry-token-row'},el('code',{class:'mono telemetry-token'},result.token),copyToken));
        delete item.tokenRows;
        entry.tele=await tokenState(item);
        await paint();
        paintRows();
      }finally{create.disabled=false;}
    }),'ghost',{'aria-label':`Create telemetry token for ${item.name}`});
    async function paint(){
      try{
        const rows=await tokenList(item);
        if(signal.aborted)return;
        tokens.replaceChildren(...rows.map(row=>el('div',{class:'telemetry-token-row'},
          el('span',{},row.name),
          stateBadge(row.revoked_at?'warning':'success',row.revoked_at?'Revoked':'Active'),
          row.revoked_at?null:button('Revoke',()=>act(async()=>{
            if(!await confirmAction({title:'Revoke telemetry token',target:`${row.name} on ${item.name}`,
              consequence:'The exporter using this token stops delivering spans. The instance itself is untouched.',
              confirmLabel:'Revoke token',danger:true}))return;
            await api('/api/otel/tokens/'+encodeURIComponent(row.id),{method:'DELETE'});
            delete item.tokenRows;
            entry.tele=await tokenState(item);
            await paint();
            paintRows();
          }),'danger',{'aria-label':`Revoke telemetry token ${row.name} on ${item.name}`}))));
        if(!rows.length)tokens.append(el('p',{class:'muted'},'No tokens yet.'));
      }catch(error){if(!signal.aborted)tokens.replaceChildren(el('p',{role:'alert'},error?.message||'Could not load tokens.')); }
    }
    body.append(el('div',{class:'telemetry-tokens'},el('h3',{},'Exporter tokens'),created,
      el('div',{class:'telemetry-create-row'},field('Token name',name),create),tokens));
    act(paint);
    return details;
  }
  // Shared telemetry facts for one instance: token list (administrators) and last span.
  // Cached per instance so the row and the details panel read the same result.
  function tokenList(item){
    if(!can('admin'))return Promise.resolve([]);
    item.tokenRows=item.tokenRows||api('/api/otel/tokens?'+new URLSearchParams({instance_id:item.id}),{signal})
      .then(result=>result.data||[]).catch(()=>[]);
    return item.tokenRows;
  }
  function lastSpan(item){
    item.lastSpan=item.lastSpan||api('/api/otel/traces?'+new URLSearchParams({instance_id:item.id,limit:'1'}),{signal})
      .then(result=>{
        const first=result.data?.[0],start=first?new Date(Number(BigInt(first.start_ns)/1000000n)):null;
        return start&&!Number.isNaN(start.getTime())?start:null;
      }).catch(()=>null);
    return item.lastSpan;
  }
  function tokenState(item){
    return Promise.all([tokenList(item),lastSpan(item)]).then(([rows,when])=>({
      live:rows.some(row=>!row.revoked_at),when}));
  }
  function telemetryLabel(entry){
    const known=entry.tele;
    if(!known)return '—';
    if(!can('admin'))return known.when?`Last span ${relativeLabel(known.when)}`:'No spans yet';
    if(!known.live)return 'No token';
    return known.when?`Token active · last span ${relativeLabel(known.when)}`:'Token active · no spans yet';
  }
  function edit(item={}) {
    const name=el('input',{required:true,value:item.name||'',maxLength:128,autocomplete:'off'});
    const url=el('input',{type:'url',required:true,value:item.url||'',placeholder:'https://n8n.example.com',autocomplete:'off',spellcheck:'false'});
    const key=el('input',{type:'password',autocomplete:'new-password',required:!item.id,placeholder:item.id?'Leave blank to keep the saved key':''});
    const form=formCard({title:item.id?'Edit connection':'Add connection',
      description:item.id?item.url:'Inference reads workflows and executions through the n8n public API.',
      body:[el('div',{class:'form-row'},field('Name',name),field('n8n base URL',url,{})),
        field('API key',key,{hint:item.id?'A key is already saved. Paste a new one only to replace it.':'Create one in n8n under Settings → n8n API.'})],
      note:[icon('lock'),'API keys are encrypted on this server.'],
      actions:[button('Cancel',()=>editor.replaceChildren(),'ghost'),el('button',{class:'button',type:'submit'},'Save connection')]});
    form.addEventListener('submit',event=>{event.preventDefault();act(async()=>{
      const body={name:name.value,url:url.value};if(key.value)body.api_key=key.value;
      await api('/api/n8n_proxy/instances'+(item.id?'/'+encodeURIComponent(item.id):''),{method:item.id?'PATCH':'POST',json:body});if(item.id)forgetProbe(item.id);key.value='';editor.replaceChildren();await load();window.dispatchEvent(new Event('instances-changed'));
    });});editor.replaceChildren(form);name.focus();
  }
  function numbers(entry){
    const evidence=entry.evidence,complete=evidence?.workflows_complete;
    const workflows=complete&&Number.isFinite(evidence.observed_workflows)?evidence.observed_workflows:null;
    const active=complete&&Number.isFinite(evidence.active_workflows)?evidence.active_workflows:null;
    const runs=entry.evidence?.executions_complete&&Number.isFinite(evidence.observed_executions_24h)?evidence.observed_executions_24h:null;
    const terminal=runs!==null&&Number.isFinite(evidence.terminal)?evidence.terminal:null;
    const successes=Number.isFinite(evidence?.successes)?evidence.successes:null;
    return {workflows,active,runs,failed:terminal!==null&&successes!==null?terminal-successes:null};
  }
  function formatRow(entry){
    const {workflows,active,runs,failed}=numbers(entry);
    const rank=CLASS_RANK[classifyProbe(entry.probe)]??3;
    const open=button('Details',()=>openDetails(entry),'ghost',{'aria-label':`Manage connection ${entry.item.name}`});
    return [
      {sortValue:entry.item.name.toLowerCase(),content:el('div',{class:'fleet-instance-cell'},el('strong',{},entry.item.name),el('span',{class:'muted fleet-instance-url'},entry.item.url))},
      {sortValue:rank,content:entry.probe?reachabilityBadge(entry.probe):el('span',{class:'muted'},'—')},
      {sortValue:(entry.version||'').toLowerCase(),content:entry.version?el('span',{class:'mono'},entry.version):el('span',{class:'muted'},'—')},
      {sortValue:workflows??'',content:workflows===null?el('span',{class:'muted'},'—'):el('span',{},String(workflows),active!==null?el('span',{class:'muted'},` (${active} active)`):null)},
      {sortValue:runs??'',content:runs===null?el('span',{class:'muted'},'—'):String(runs)},
      {sortValue:failed??'',content:failed===null?el('span',{class:'muted'},'—'):String(failed)},
      {sortValue:telemetryLabel(entry),content:el('span',{class:entry.tele?'':'muted'},telemetryLabel(entry))},
      {sortValue:'',content:open},
    ];
  }
  function visibleEntries(){
    if(currentFilter!=='issues')return entries;
    return entries.filter(entry=>['degraded','unreachable'].includes(classifyProbe(entry.probe)));
  }
  function paintRows(){
    if(!fleetTable)return;
    fleetTable.setRows(visibleEntries().map(formatRow));
    const issues=entries.filter(entry=>['degraded','unreachable'].includes(classifyProbe(entry.probe))).length;
    filterAll.textContent=`All instances (${entries.length})`;
    filterIssues.textContent=`Issues only (${issues})`;
  }
  function setFilter(name){
    currentFilter=name;
    filterAll.setAttribute('aria-pressed',String(name==='all'));
    filterIssues.setAttribute('aria-pressed',String(name==='issues'));
    paintRows();
  }
  function paintRollup(){
    let operational=0,degraded=0,unreachable=0;
    for(const entry of entries){
      const kind=classifyProbe(entry.probe);
      if(kind==='operational')operational++;
      else if(kind==='degraded')degraded++;
      else if(kind==='unreachable')unreachable++;
    }
    totalNum.textContent=String(entries.length);
    operationalNum.textContent=String(operational);
    degradedNum.textContent=String(degraded);
    unreachableNum.textContent=entries.length?String(unreachable):'—';
  }
  function openDetails(entry){
    const item=entry.item;
    detail.hidden=false;
    const health=el('div',{class:'probe-status'});
    const feedback=el('div'),probeDetail=el('div');
    const paint=result=>health.replaceChildren(reachabilityBadge(result),el('p',{class:'muted',title:result?.checked_at},checkedLine(result)));
    paint(entry.probe||{reachable:null});
    let checking=false;
    const recheck=button('Re-check connection',()=>checkEntry(entry,{paint,feedback,probeDetail}),'ghost',{'aria-label':`Re-check connection ${item.name}`});
    const actions=el('div',{class:'toolbar'});
    if(can('admin')){
      actions.append(recheck,button('Edit',()=>edit(item),'ghost',{'aria-label':`Edit connection ${item.name}`}),
        actionMenu([button('Remove',()=>act(async()=>{
          if(!await confirmAction({title:'Remove connection',target:item.name,
            consequence:'Inference stops reading this instance and deletes its saved API key. Workflows and executions in n8n are untouched.',
            confirmLabel:'Remove connection',danger:true}))return;
          await api('/api/n8n_proxy/instances/'+encodeURIComponent(item.id),{method:'DELETE'});
          forgetProbe(item.id);detail.hidden=true;await load();window.dispatchEvent(new Event('instances-changed'));
        }),'danger',{'aria-label':`Remove connection ${item.name}`})],`More actions for ${item.name}`));
    }else actions.append(recheck);
    if(entry.probe)probeDetail.replaceChildren(probeResult(entry.probe));
    detail.replaceChildren(
      el('div',{class:'instance-connection-head'},el('div',{},el('h2',{},item.name),el('p',{class:'muted'},item.url)),health),
      actions,feedback,probeDetail,telemetry(entry));
    detail.scrollIntoView({block:'nearest'});
    detail.querySelector('h2')?.setAttribute('tabindex','-1');
    detail.querySelector('h2')?.focus({preventScroll:true});
  }
  async function checkEntry(entry,slots={}){
    if(entry.checking||signal.aborted||entry.checkSignal?.aborted)return;
    entry.checking=true;
    try{
      const result=await checkConnection(entry.item,{signal:entry.checkSignal});
      if(signal.aborted||entry.checkSignal?.aborted)return;
      rememberProbe(entry.item,result);
      entry.probe=result;
      slots.paint?.(result);
      if(slots.probeDetail)slots.probeDetail.replaceChildren(probeResult(result));
      if(slots.feedback)slots.feedback.replaceChildren();
      paintRollup();paintRows();
    }catch(error){
      if(!signal.aborted&&!entry.checkSignal?.aborted&&slots.feedback)
        slots.feedback.replaceChildren(loadFailure(error,()=>checkEntry(entry,slots),`Could not check ${entry.item.name}`));
    }finally{entry.checking=false;}
  }
  async function checkAll(){
    let index=0;
    await Promise.all(Array.from({length:Math.min(3,entries.length)},async()=>{
      while(index<entries.length&&!signal.aborted)await checkEntry(entries[index++]);
    }));
  }
  async function load(){
    const current=++generation;checks?.abort();checks=new AbortController();
    for(const entry of entries)entry.checkSignal=checks.signal;
    const result=await api('/api/n8n_proxy/instances',{signal});if(signal.aborted||current!==generation)return;
    tableWrap.replaceChildren();detail.hidden=true;
    entries=result.data.map(item=>({item,probe:cachedProbe(item),evidence:null,version:null,tele:null,checking:false,checkSignal:checks.signal}));
    if(!entries.length){
      tableWrap.append(empty('No connections yet','Add your first n8n instance to explore workflows and executions.',can('admin')?[button('Add connection',()=>edit())]:[]));
      paintRollup();filterAll.textContent='All instances (0)';filterIssues.textContent='Issues only (0)';
      return;
    }
    fleetTable=table(['Instance','Reachability','Version','Workflows','Runs · 24h','Failed · 24h','Telemetry',''],
      entries.map(formatRow),{numeric:[3,4,5],sortable:{0:'text',1:'number',2:'text',3:'number',4:'number',5:'number',6:'text'},initialSort:{column:0,direction:'asc'}});
    tableWrap.append(fleetTable);
    paintRollup();paintRows();
    // Owned-bundle versions for administrators; anyone else sees a dash.
    if(can('admin')){
      api('/api/docker_mgr/bundles',{signal}).then(bundles=>{
        if(signal.aborted||current!==generation)return;
        for(const entry of entries){
          const match=(bundles.data||[]).find(row=>row.instance_id===entry.item.id);
          entry.version=match?.version||null;
        }
        paintRows();
      }).catch(()=>{});
    }
    // One fleet scan fills every row's numbers; a rate limit leaves dashes.
    fleetMetrics({signal}).then(metrics=>{
      if(signal.aborted||current!==generation)return;
      for(const entry of entries)
        entry.evidence=metrics?.data?.find(row=>row.instance_id===entry.item.id)||null;
      paintRows();
    }).catch(()=>{});
    // Telemetry state per row, same cached facts as the details panel.
    for(const entry of entries){
      tokenState(entry.item).then(state=>{
        if(signal.aborted||current!==generation)return;
        entry.tele=state;paintRows();
      }).catch(()=>{});
    }
    // Fill only missing probe cache entries automatically, with bounded work.
    const pending=entries.filter(entry=>!entry.probe);
    let index=0;
    await Promise.all(Array.from({length:Math.min(3,pending.length)},async()=>{
      while(index<pending.length&&!checks.signal.aborted)await checkEntry(pending[index++]);
    }));
    // Deep link ("Set up telemetry" / palette): open that instance's details.
    const telemetryTarget=new URLSearchParams(location.hash.split('?')[1]||'').get('telemetry');
    if(telemetryTarget!==null&&!signal.aborted&&current===generation){
      const entry=entries.find(row=>row.item.id===telemetryTarget)||entries[0];
      if(entry){openDetails(entry);detail.querySelector('details.telemetry-section').open=true;}
    }
  }
  act(load);return()=>{controller.abort();checks?.abort();};
}
