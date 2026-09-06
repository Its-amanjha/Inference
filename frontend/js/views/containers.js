import {api as request} from '../api.js';
import {el,button,field,notice,empty,heading,jsonView} from '../lib/html.js';
import {can} from '../state.js';
import {confirmAction,actionMenu,stateBadge,humanize} from '../lib/confirm.js';

const BUNDLE_STATES={ready:['success','Running'],stopped:['','Stopped'],deploying:['info','Deploying'],starting:['info','Starting'],stopping:['info','Stopping'],deleting:['info','Removing'],partial:['warning','Partial'],uncertain:['warning','Outcome unknown'],removed:['','Removed']};
const STATUS_LABELS={available:'Docker engine',transport:'Transport',version:'Version',api_version:'API version'};

function bundleBadge(status){
  if(!status)return stateBadge('warning','Unknown');
  const [kind,label]=BUNDLE_STATES[status]||['',humanize(status)];
  return stateBadge(kind,label,status);
}

function dockerStatus(value){
  const lines=[];
  for(const [key,item] of Object.entries(value&&typeof value==='object'?value:{})){
    if(item===null||item===undefined||typeof item==='object')continue;
    const shown=key==='available'?stateBadge(item?'success':'error',item?'Available':'Unavailable',String(item)):String(item);
    lines.push(el('dt',{},STATUS_LABELS[key]||humanize(key)),el('dd',{},shown));
  }
  return el('section',{class:'card stack docker-status','aria-label':'Docker status'},el('h2',{},'Docker'),
    lines.length?el('dl',{class:'status-lines'},lines):el('p',{class:'muted'},'Docker returned no status fields.'),
    el('details',{},el('summary',{},'Raw status'),jsonView(value)));
}

export async function render(container){
  if(!can('admin')){container.append(empty('Administrator access required','An administrator manages n8n bundles.'));return;}
  const controller=new AbortController(),signal=controller.signal,messages=el('div'),list=el('div',{class:'grid'}),editor=el('div'),service=el('div');
  const results=new Map();
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  async function load(){const result=await api('/api/docker_mgr/bundles',{signal});list.replaceChildren();for(const item of result.data){
    const detail=el('div'),id=encodeURIComponent(item.id),name=item.name||item.id,base='/api/docker_mgr/bundles/'+id;
    if(results.has(item.id))detail.append(jsonView(results.get(item.id)));
    // Operations reload the inventory so status and the offered actions stay current, even after a failure.
    const operate=async(path,options)=>{try{results.set(item.id,await api(path,options));}finally{await load().catch(()=>{});}};
    const lifecycle=(action,className)=>button(action==='start'?'Start':'Stop',()=>act(async()=>{
      const ok=await confirmAction(action==='start'
        ?{title:'Start bundle',target:name,consequence:'Starts the n8n and task runner containers and waits for them to report healthy.',confirmLabel:'Start bundle'}
        :{title:'Stop bundle',target:name,consequence:'Stops the n8n and task runner containers. Workflows on this instance do not run until the bundle is started again. The data volume is kept.',confirmLabel:'Stop bundle'});
      if(!ok)return;
      await operate(base+'/'+action,{method:'POST'});
    }),className);
    const reconcile=className=>button('Reconcile',()=>act(()=>operate(base+'/reconcile',{method:'POST'})),className);
    const remove=button('Remove',()=>act(async()=>{
      if(!await confirmAction({title:'Remove bundle',target:name,consequence:'Removes this bundle’s containers and network. The n8n data volume is preserved.',confirmLabel:'Remove bundle',danger:true}))return;
      await operate(base,{method:'DELETE'});
    }),'danger');
    const purge=button('Purge data',()=>act(async()=>{
      if(!await confirmAction({title:'Purge bundle data',target:name,consequence:'Removes the containers and network and permanently deletes the owned n8n data volume and everything stored in it. This cannot be undone.',confirmLabel:'Purge data',danger:true,requireText:'PURGE'}))return;
      results.delete(item.id);
      try{await api(base+'?purge_data=true',{method:'DELETE'});}finally{await load().catch(()=>{});}
    }),'danger');
    const inspect=button('Inspect',()=>act(async()=>{detail.replaceChildren(jsonView(await api(base)));}),'ghost');
    const primary=[],more=[];
    if(item.status==='ready'){primary.push(lifecycle('stop','button'));more.push(reconcile('ghost'));}
    else if(item.status==='stopped'){primary.push(lifecycle('start','button'));more.push(reconcile('ghost'));}
    else{primary.push(reconcile('button'));more.push(lifecycle('start','ghost'),lifecycle('stop','ghost'));}
    const actions=el('div',{class:'toolbar'},primary,inspect,actionMenu([...more,remove,purge]));
    list.append(el('section',{class:'card stack'},el('div',{class:'title-row'},el('h2',{},name),bundleBadge(item.status)),item.version?el('p',{class:'muted'},'n8n '+item.version):null,actions,detail));}
    if(!result.data.length)list.append(empty('No owned bundles','Provision a matching n8n and task runner pair. Existing host containers remain outside this inventory.'));}
  function provision(){const inputs={};const fields=[['name','Bundle name','text',''],['version','Exact n8n / runner version','text',''],['port','Published host port','number','5678'],['bind_host','Host bind IP','text','127.0.0.1'],['instance_url','n8n URL reachable from Inference','url',''],['exporter_base_url','Inference telemetry base URL reachable from n8n','url','']];const form=el('form',{class:'card stack'},el('h2',{},'Provision n8n + runners'));
    for(const [key,label,type,value] of fields){inputs[key]=el('input',{type,value,required:true});form.append(field(label,inputs[key]));}const result=el('div');const submit=el('button',{type:'submit',class:'button'},'Provision bundle');form.append(el('p',{class:'muted'},'Downloads matching images and creates a persistent data volume. Complete n8n owner setup and add its API key in Instances afterwards.'),submit,button('Cancel',()=>editor.replaceChildren(),'ghost'),result);
    let request=null;form.addEventListener('input',()=>{request=null;});form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{if(!request){if(!await confirmAction({title:'Create n8n bundle',target:inputs.name.value,consequence:`Downloads the n8n and task runner ${inputs.version.value} images, then creates the containers, a network and a persistent data volume published on ${inputs.bind_host.value}:${inputs.port.value}.`,confirmLabel:'Create bundle'}))return;request={spec:{name:inputs.name.value,version:inputs.version.value,port:Number(inputs.port.value),bind_host:inputs.bind_host.value,exporter_base_url:inputs.exporter_base_url.value},instance_url:inputs.instance_url.value,idempotency_key:crypto.randomUUID(),readiness_timeout:90};}submit.disabled=true;try{result.replaceChildren(jsonView(await api('/api/docker_mgr/bundles',{method:'POST',json:request})));await load();window.dispatchEvent(new Event('instances-changed'));}finally{submit.disabled=false;}});});editor.replaceChildren(form);}
  container.append(heading('Containers','Provision and manage bundles owned by this Inference installation.'),messages,service,el('div',{class:'toolbar'},button('Provision bundle',provision),button('Refresh',()=>act(load),'ghost')),editor,list);
  await Promise.allSettled([act(async()=>{service.replaceChildren(dockerStatus(await api('/api/docker_mgr/status',{signal})));}),act(load)]);return()=>controller.abort();
}
