import {api as request} from '../api.js';
import {el,button,field,formCard,notice,empty,heading} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {confirmAction,stateBadge} from '../lib/confirm.js';
import {can} from '../state.js';
import {relativeLabel} from '../lib/triage.js';
import {rawDisclosure,probeSummary,reachabilityBadge,checkedLine,loadingState,loadFailure,cachedProbe,forgetProbe,checkConnection,asOf,instanceStats,fleetMetrics} from '../lib/payload.js';

function probeResult(result){
  const {list,notes,diagnostics}=probeSummary(result);
  return el('div',{className:'stack decision-probe',role:'status'},
    el('div',{className:'decision-head'},el('h3',{},'Connection test'),reachabilityBadge(result)),
    el('p',{className:'muted',title:result.checked_at},checkedLine(result)),asOf(new Date(result.checked_at)),
    list,
    diagnostics.length?notes:el('p',{},'Public API checks passed.'),
    rawDisclosure(result));
}

export async function render(container) {
  const controller=new AbortController(), signal=controller.signal;
  const messages=el('div'), list=el('div',{class:'grid'}), editor=el('div');
  container.append(heading('Instances','Connect your fleet. API keys stay encrypted on the server.'),messages,editor,list);
  let writing=false;
  let generation=0,checks=null;
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
  // and (administrators only) token create-once-copy / list / revoke.
  function telemetry(item){
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
    api('/api/otel/traces?'+new URLSearchParams({instance_id:item.id,limit:'1'}),{signal}).then(result=>{
      if(signal.aborted)return;
      const first=result.data?.[0],start=first?new Date(Number(BigInt(first.start_ns)/1000000n)):null;
      arrival.replaceChildren(!start||Number.isNaN(start.getTime())?'No spans received yet.':
        ['Last span received ',relativeLabel(start),'.']);
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
        await paint();
      }finally{create.disabled=false;}
    }),'ghost',{'aria-label':`Create telemetry token for ${item.name}`});
    async function paint(){
      try{
        const result=await api('/api/otel/tokens?'+new URLSearchParams({instance_id:item.id}),{signal});
        if(signal.aborted)return;
        tokens.replaceChildren(...(result.data||[]).map(row=>el('div',{class:'telemetry-token-row'},
          el('span',{},row.name),
          stateBadge(row.revoked_at?'warning':'success',row.revoked_at?'Revoked':'Active'),
          row.revoked_at?null:button('Revoke',()=>act(async()=>{
            if(!await confirmAction({title:'Revoke telemetry token',target:`${row.name} on ${item.name}`,
              consequence:'The exporter using this token stops delivering spans. The instance itself is untouched.',
              confirmLabel:'Revoke token',danger:true}))return;
            await api('/api/otel/tokens/'+encodeURIComponent(row.id),{method:'DELETE'});
            await paint();
          }),'danger',{'aria-label':`Revoke telemetry token ${row.name} on ${item.name}`}))));
        if(!result.data?.length)tokens.append(el('p',{class:'muted'},'No tokens yet.'));
      }catch(error){if(!signal.aborted)tokens.replaceChildren(el('p',{role:'alert'},error?.message||'Could not load tokens.')); }
    }
    body.append(el('div',{class:'telemetry-tokens'},el('h3',{},'Exporter tokens'),created,
      el('div',{class:'telemetry-create-row'},field('Token name',name),create),tokens));
    act(paint);
    return details;
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
  async function load(){
    const current=++generation;checks?.abort();checks=new AbortController();const checkSignal=checks.signal;
    const result=await api('/api/n8n_proxy/instances',{signal});if(signal.aborted||current!==generation)return;list.replaceChildren();
    if(!result.data.length)list.append(empty('No connections yet','Add your first n8n instance to explore workflows and executions.',can('admin')?[button('Add connection',()=>edit())]:[]));
    const pending=[],slots=new Map();
    for(const item of result.data){const detail=el('div'),feedback=el('div'),health=el('div',{class:'probe-status'}),actions=el('div',{class:'toolbar'});
      // Numbers arrive from one fleet scan below; dashes until then, never zeros.
      const stats=el('div',{class:'instance-stats-slot'},instanceStats(null));slots.set(item.id,stats);
      const added=item.created_at?new Date(item.created_at.replace(' ','T')+(/Z|[+-]\d\d:?\d\d$/.test(item.created_at)?'':'Z')):null;
      const meta=el('p',{class:'instance-meta'},[added&&!Number.isNaN(added.getTime())?`Added ${added.toLocaleDateString([],{day:'numeric',month:'short',year:'numeric'})}`:null,
        item.api_key_configured?'API key saved':'No API key'].filter(Boolean).join(' · '));
      const cached=cachedProbe(item);let checking=false;
      const paint=result=>health.replaceChildren(reachabilityBadge(result),el('p',{class:'muted',title:result?.checked_at},checkedLine(result)));
      paint(cached);
      const recheck=button('Re-check connection',()=>check(),'ghost',{'aria-label':`Re-check connection ${item.name}`});
      async function check(){
        if(checking||signal.aborted||checkSignal.aborted)return;checking=true;recheck.disabled=true;
        feedback.replaceChildren(loadingState(`Checking connection to ${item.name}…`,{rows:2}));
        try{const result=await checkConnection(item,{signal:checkSignal});
          if(signal.aborted||checkSignal.aborted||current!==generation)return;
          paint(result);feedback.replaceChildren();detail.replaceChildren(probeResult(result));
        }catch(error){if(!signal.aborted&&!checkSignal.aborted)feedback.replaceChildren(loadFailure(error,check,`Could not check ${item.name}`));}
        finally{checking=false;recheck.disabled=false;}
      }
      if(!cached)pending.push(check);
      if(can('admin'))actions.append(recheck,button('Edit',()=>edit(item),'ghost',{'aria-label':`Edit connection ${item.name}`}),button('Remove',()=>act(async()=>{if(!await confirmAction({title:'Remove connection',target:item.name,consequence:'Inference stops reading this instance and deletes its saved API key. Workflows and executions in n8n are untouched.',confirmLabel:'Remove connection',danger:true}))return;await api('/api/n8n_proxy/instances/'+encodeURIComponent(item.id),{method:'DELETE'});forgetProbe(item.id);await load();window.dispatchEvent(new Event('instances-changed'));}),'danger',{'aria-label':`Remove connection ${item.name}`}));
      list.append(el('section',{class:'card stack instance-connection','aria-label':`Connection ${item.name}`},
        el('div',{class:'instance-connection-head'},el('div',{},el('h2',{},item.name),el('p',{class:'muted'},item.url)),health),
        stats,meta,actions,feedback,detail,telemetry(item)));
    }
    // One fleet scan fills every card's numbers; a rate limit or an unreachable instance leaves dashes.
    if(slots.size)fleetMetrics({signal}).then(metrics=>{if(signal.aborted||current!==generation)return;
      for(const [id,slot] of slots)slot.replaceChildren(instanceStats(metrics?.data?.find(row=>row.instance_id===id)||null));}).catch(()=>{});
    let index=0;
    // Deep link from Observability ("Set up telemetry"): open and reveal that section.
    const telemetryTarget=new URLSearchParams(location.hash.split('?')[1]||'').get('telemetry');
    if(telemetryTarget!==null&&!signal.aborted){
      const sections=[...list.querySelectorAll('details.telemetry-section')];
      const section=sections.find(node=>node.id==='telemetry-'+telemetryTarget)||sections[0];
      if(section){section.open=true;section.scrollIntoView({block:'nearest'});
        section.querySelector('summary')?.focus({preventScroll:true});}
    }
    // Fill only missing cache entries automatically, with bounded per-instance work.
    await Promise.all(Array.from({length:Math.min(3,pending.length)},async()=>{
      while(index<pending.length&&!checkSignal.aborted)await pending[index++]();
    }));
  }
  if(can('admin'))container.insertBefore(button('Add instance',()=>edit()),editor);
  act(load);return()=>{controller.abort();checks?.abort();};
}
