import {api as request} from '../api.js';
import {el,button,field,formCard,notice,empty,heading} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {confirmAction} from '../lib/confirm.js';
import {can} from '../state.js';
import {rawDisclosure,probeSummary,reachabilityBadge,checkedLine,loadingState,loadFailure,cachedProbe,forgetProbe,checkConnection,asOf} from '../lib/payload.js';

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
  function edit(item={}) {
    const name=el('input',{required:true,value:item.name||'',maxLength:128,autocomplete:'off'});
    const url=el('input',{type:'url',required:true,value:item.url||'',placeholder:'https://n8n.example.com',autocomplete:'off',spellcheck:'false'});
    const key=el('input',{type:'password',autocomplete:'new-password',required:!item.id,placeholder:item.id?'Leave blank to keep the saved key':''});
    const form=formCard({title:item.id?'Edit connection':'Add connection',
      description:item.id?item.url:'Inference reads workflows and executions through the n8n public API.',
      body:[el('div',{class:'form-row'},field('Name',name),field('n8n base URL',url,{hint:'The address you open n8n at.'})),
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
    const pending=[];
    for(const item of result.data){const detail=el('div'),feedback=el('div'),health=el('div',{class:'probe-status'}),actions=el('div',{class:'toolbar'});
      const cached=cachedProbe(item);let checking=false;
      const paint=result=>health.replaceChildren(reachabilityBadge(result),el('p',{class:'muted',title:result?.checked_at},checkedLine(result)));
      paint(cached);
      const recheck=button('Re-check connection',()=>check(),'ghost');
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
      if(can('admin'))actions.append(recheck,button('Edit',()=>edit(item),'ghost'),button('Remove',()=>act(async()=>{if(!await confirmAction({title:'Remove connection',target:item.name,consequence:'Inference stops reading this instance and deletes its saved API key. Workflows and executions in n8n are untouched.',confirmLabel:'Remove connection',danger:true}))return;await api('/api/n8n_proxy/instances/'+encodeURIComponent(item.id),{method:'DELETE'});forgetProbe(item.id);await load();window.dispatchEvent(new Event('instances-changed'));}),'danger'));
      list.append(el('section',{class:'card stack','aria-label':`Connection ${item.name}`},el('h2',{},item.name),el('p',{class:'muted'},item.url),health,actions,feedback,detail));
    }
    let index=0;
    // Fill only missing cache entries automatically, with bounded per-instance work.
    await Promise.all(Array.from({length:Math.min(3,pending.length)},async()=>{
      while(index<pending.length&&!checkSignal.aborted)await pending[index++]();
    }));
  }
  if(can('admin'))container.insertBefore(button('Add instance',()=>edit()),editor);
  act(load);return()=>{controller.abort();checks?.abort();};
}
