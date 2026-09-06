import {api as request} from '../api.js';
import {el,button,field,notice,empty,heading} from '../lib/html.js';
import {can} from '../state.js';
import {rawDisclosure,probeSummary,reachabilityBadge,checkedLine} from '../lib/payload.js';

function probeResult(result){
  const {list,notes,diagnostics}=probeSummary(result);
  return el('div',{className:'stack decision-probe',role:'status'},
    el('div',{className:'decision-head'},el('h3',{},'Connection test'),reachabilityBadge(result)),
    el('p',{className:'muted'},checkedLine(result)),
    list,
    diagnostics.length?notes:el('p',{},'Public API checks passed.'),
    rawDisclosure(result));
}

export async function render(container) {
  const controller=new AbortController(), signal=controller.signal;
  const messages=el('div'), list=el('div',{class:'grid'}), editor=el('div');
  container.append(heading('Instances','Connect your fleet. API keys stay encrypted on the server.'),messages,editor,list);
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  function edit(item={}) {
    const name=el('input',{required:true,value:item.name||'',maxLength:128});
    const url=el('input',{type:'url',required:true,value:item.url||''});
    const key=el('input',{type:'password',autocomplete:'new-password',required:!item.id});
    const form=el('form',{class:'card stack'},el('h2',{},item.id?'Edit connection':'Add connection'),field('Name',name),field('n8n base URL',url),field(item.id?'New API key (leave blank to keep)':'API key',key),el('button',{class:'button',type:'submit'},'Save connection'),button('Cancel',()=>editor.replaceChildren(),'ghost'));
    form.addEventListener('submit',event=>{event.preventDefault();act(async()=>{
      const body={name:name.value,url:url.value};if(key.value)body.api_key=key.value;
      await api('/api/n8n_proxy/instances'+(item.id?'/'+encodeURIComponent(item.id):''),{method:item.id?'PATCH':'POST',json:body});key.value='';editor.replaceChildren();await load();window.dispatchEvent(new Event('instances-changed'));
    });});editor.replaceChildren(form);name.focus();
  }
  async function load(){
    const result=await api('/api/n8n_proxy/instances',{signal});list.replaceChildren();
    if(!result.data.length)list.append(empty('No connections yet','Add your first n8n instance to explore workflows and executions.'));
    for(const item of result.data){const detail=el('div'), actions=el('div',{class:'toolbar'});
      if(can('admin'))actions.append(button('Test connection',()=>act(async()=>detail.replaceChildren(probeResult(await api('/api/n8n_proxy/instances/'+encodeURIComponent(item.id)+'/probe',{method:'POST'})))),'ghost'),button('Edit',()=>edit(item),'ghost'),button('Remove',()=>act(async()=>{if(!confirm('Remove '+item.name+' from Inference? Its n8n data will remain.'))return;await api('/api/n8n_proxy/instances/'+encodeURIComponent(item.id),{method:'DELETE'});await load();window.dispatchEvent(new Event('instances-changed'));}),'ghost'));
      list.append(el('section',{class:'card stack'},el('h2',{},item.name),el('p',{class:'muted'},item.url),actions,detail));
    }
  }
  if(can('admin'))container.insertBefore(button('Add instance',()=>edit()),editor);
  await act(load);return()=>controller.abort();
}
