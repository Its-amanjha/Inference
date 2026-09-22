import {api as request} from '../api.js';
import {el,button,field,formCard,notice,empty,heading,jsonView} from '../lib/html.js';
import {icon} from '../lib/icons.js';
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
  let engineAvailable=true;
  const provisionButton=button('Provision bundle',provision);
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  function emptyInventory(){
    return engineAvailable
      ? empty('No owned bundles','Provision a matching n8n and task runner pair. Existing host containers remain outside this inventory.',[button('Provision your first bundle',provision)])
      : empty('No owned bundles','Remote n8n connections are managed in Instances. This inventory only lists Docker bundles created by Inference.',[el('a',{class:'button ghost',href:'#instances'},'Open instances')]);
  }
  async function checkEngine(){
    try{
      const result=await api('/api/docker_mgr/status',{signal});
      engineAvailable=true;service.replaceChildren(dockerStatus(result));
    }catch(error){
      if(signal.aborted)return;
      if(error.code!=='docker_unavailable')throw error;
      engineAvailable=false;
      service.replaceChildren(el('section',{class:'card docker-unavailable',role:'status'},icon('containers'),el('div',{},
        el('h2',{},'Docker management is unavailable on this server.'),
        el('p',{},'Your connected n8n instances can still be monitored and managed through their APIs. This page only manages containers on the server running Inference.'),
        el('details',{},el('summary',{},'How to enable container management'),el('p',{},'Run Inference on a Linux host with an accessible Docker Unix socket. Adding a VPS URL in Instances does not grant access to that VPS\u2019s Docker engine.')))));
    }finally{provisionButton.disabled=!engineAvailable;}
  }
  async function refresh(){
    await Promise.allSettled([act(checkEngine),act(load)]);
    if(!signal.aborted&&list.querySelector('.empty'))list.replaceChildren(emptyInventory());
  }
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
    }),className,{'aria-label':`${action==='start'?'Start':'Stop'} bundle ${name}`});
    const reconcile=className=>button('Reconcile',()=>act(()=>operate(base+'/reconcile',{method:'POST'})),className,{'aria-label':`Reconcile bundle ${name}`});
    const remove=button('Remove',()=>act(async()=>{
      if(!await confirmAction({title:'Remove bundle',target:name,consequence:'Removes this bundle’s containers and network. The n8n data volume is preserved.',confirmLabel:'Remove bundle',danger:true}))return;
      await operate(base,{method:'DELETE'});
    }),'danger',{'aria-label':`Remove bundle ${name}`});
    const purge=button('Purge data',()=>act(async()=>{
      if(!await confirmAction({title:'Purge bundle data',target:name,consequence:'Removes the containers and network and permanently deletes the owned n8n data volume and everything stored in it. This cannot be undone.',confirmLabel:'Purge data',danger:true,requireText:'PURGE'}))return;
      results.delete(item.id);
      try{await api(base+'?purge_data=true',{method:'DELETE'});}finally{await load().catch(()=>{});}
    }),'danger',{'aria-label':`Purge data of bundle ${name}`});
    const inspect=button('Inspect',()=>act(async()=>{detail.replaceChildren(jsonView(await api(base)));}),'ghost',{'aria-label':`Inspect bundle ${name}`});
    const primary=[],more=[];
    if(item.status==='ready'){primary.push(lifecycle('stop','button'));more.push(reconcile('ghost'));}
    else if(item.status==='stopped'){primary.push(lifecycle('start','button'));more.push(reconcile('ghost'));}
    else{primary.push(reconcile('button'));more.push(lifecycle('start','ghost'),lifecycle('stop','ghost'));}
    const actions=el('div',{class:'toolbar'},primary,inspect,actionMenu([...more,remove,purge],`More actions for ${name}`));
    list.append(el('section',{class:'card stack'},el('div',{class:'title-row'},el('h2',{},name),bundleBadge(item.status)),item.version?el('p',{class:'muted'},'n8n '+item.version):null,actions,detail));}
    if(!result.data.length)list.append(emptyInventory());}
  function provision(){
    const specs=[['name','Bundle name','text','','Used for the container and volume names.'],
      ['version','Exact n8n / runner version','text','','Both images use this tag, for example 2.19.0.'],
      ['port','Published host port','number','5678','The host port n8n is published on.'],
      ['bind_host','Host bind IP','text','127.0.0.1','127.0.0.1 keeps the bundle reachable only from this machine.'],
      ['instance_url','n8n URL reachable from Inference','url','','How this server reaches n8n, for example http://127.0.0.1:5678.'],
      ['exporter_base_url','Inference telemetry base URL reachable from n8n','url','','How n8n reaches Inference to send telemetry.']];
    const inputs={},fields={};
    for(const [key,label,type,value,hint] of specs){inputs[key]=el('input',{type,value,required:true,autocomplete:'off'});fields[key]=field(label,inputs[key],{hint});}
    const result=el('div');const submit=el('button',{type:'submit',class:'button'},'Provision bundle');
    const form=formCard({title:'Provision n8n + runners',description:'Creates a matching n8n and task runner pair owned by this installation.',
      body:[el('fieldset',{class:'form-group'},el('legend',{},'Container'),el('div',{class:'form-row'},fields.name,fields.version)),
        el('fieldset',{class:'form-group'},el('legend',{},'Network'),el('div',{class:'form-row'},fields.port,fields.bind_host),fields.instance_url,fields.exporter_base_url,result)],
      note:'Downloads matching images and creates a persistent data volume. Complete n8n owner setup and add its API key in Instances afterwards.',
      actions:[button('Cancel',()=>editor.replaceChildren(),'ghost'),submit]});
    let request=null;form.addEventListener('input',()=>{request=null;});form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{if(!request){if(!await confirmAction({title:'Create n8n bundle',target:inputs.name.value,consequence:`Downloads the n8n and task runner ${inputs.version.value} images, then creates the containers, a network and a persistent data volume published on ${inputs.bind_host.value}:${inputs.port.value}.`,confirmLabel:'Create bundle'}))return;request={spec:{name:inputs.name.value,version:inputs.version.value,port:Number(inputs.port.value),bind_host:inputs.bind_host.value,exporter_base_url:inputs.exporter_base_url.value},instance_url:inputs.instance_url.value,idempotency_key:crypto.randomUUID(),readiness_timeout:90};}submit.disabled=true;try{result.replaceChildren(jsonView(await api('/api/docker_mgr/bundles',{method:'POST',json:request})));await load();window.dispatchEvent(new Event('instances-changed'));}finally{submit.disabled=false;}});});editor.replaceChildren(form);}
  container.append(heading('Containers','Provision and manage bundles owned by this Inference installation.'),messages,service,el('div',{class:'toolbar'},provisionButton,button('Refresh',refresh,'ghost')),editor,list);
  await refresh();return()=>controller.abort();
}
