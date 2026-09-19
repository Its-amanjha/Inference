import {api as request,apiFetch} from '../api.js';
import {el,button,field,formCard,notice,empty,heading,jsonView,table} from '../lib/html.js';
import {can,pageSize} from '../state.js';
import {confirmAction,actionMenu,stateBadge,humanize} from '../lib/confirm.js';

const RUN_STATES={succeeded:'success',success:'success',failed:'error',partial:'warning',uncertain:'warning',running:'info',deleting:'info'};
const S3_KEYS=['endpoint','bucket','region','prefix','credentials_ref'];

function runStatus(run){
  const label=run.status==='deleted'?'Local file deleted':humanize(run.status);
  return el('span',{class:'run-status'},stateBadge(RUN_STATES[run.status]||'',label,run.status),run.code?el('span',{class:'muted'},run.code):null);
}

export async function render(container){
  if(!can('admin')){container.append(empty('Administrator access required','An administrator manages encrypted backups.'));return;}
  const controller=new AbortController(),signal=controller.signal,messages=el('div'),schedules=el('div',{class:'grid'}),history=el('div'),editor=el('div');let next=null;
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  async function download(path,options={}){if(writing)throw new Error('A backup operation is already running.');writing=true;let response;try{response=await apiFetch(path,{...options,signal});}finally{writing=false;}if(!response.ok){let data={};try{data=await response.json();}catch{}throw new Error(data.detail||'Backup download failed.');}const url=URL.createObjectURL(await response.blob());const a=el('a',{href:url,download:'inference.ibackup'});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function runActions(r){
    const id=encodeURIComponent(r.id),when=new Date(r.created_at*1000).toLocaleString();
    // Only runs with a recorded archive hash have a local file; the server rejects the rest.
    const hasFile=Boolean(r.sha256)&&r.status!=='deleted';
    return el('div',{class:'toolbar'},
      hasFile?button('Download',()=>act(()=>download('/api/backups/runs/'+id+'/download'))):null,
      button('Inspect',()=>messages.replaceChildren(jsonView(r)),'ghost'),
      r.sha256&&['uncertain','partial'].includes(r.status)?button('Reconcile',()=>act(async()=>{await api('/api/backups/runs/'+id+'/reconcile',{method:'POST'});await runs();}),'ghost'):null,
      hasFile?button('Delete local file',()=>act(async()=>{
        if(!await confirmAction({title:'Delete local archive',target:[el('strong',{},'Backup from '+when),' (run '+r.id+')'],consequence:'Deletes the archive file stored on this server. The run stays in history and any S3 copy remains.',confirmLabel:'Delete local file',danger:true}))return;
        await api('/api/backups/runs/'+id+'/file',{method:'DELETE'});await runs();
      }),'danger'):null);
  }
  async function runs(append=false){const result=await api('/api/backups/runs?limit='+pageSize()+(append&&next?'&before='+encodeURIComponent(next):''),{signal});next=result.next_cursor;if(!append)history.replaceChildren();history.querySelector('[data-more]')?.remove();history.append(table(['Created','Status','Remote','Actions'],result.data.map(r=>[new Date(r.created_at*1000).toLocaleString(),runStatus(r),r.remote_status||'—',runActions(r)])));if(!result.data.length&&!append){const scheds=await api('/api/backups/schedules',{signal});const runnable=scheds.data.find(s=>s.enabled);history.append(empty('No recorded runs','Scheduled backups and manual schedule runs appear here.',runnable?[button('Run backup now',()=>act(async()=>{await api('/api/backups/schedules/'+encodeURIComponent(runnable.id)+'/run',{method:'POST'});await runs();await load();}),'ghost')]:[button('Add schedule',()=>edit())]));}if(next){const more=button('Load older runs',()=>act(()=>runs(true)),'ghost');more.dataset.more='true';history.append(more);}}
  function scheduleCard(s){
    const id=encodeURIComponent(s.id);
    const main=s.enabled
      ?button('Run now',()=>act(async()=>{const result=await api('/api/backups/schedules/'+id+'/run',{method:'POST'});notice(messages,'Backup outcome: '+result.status);await runs();await load();}))
      :button('Enable',()=>act(async()=>{await api('/api/backups/schedules/'+id,{method:'PUT',json:{name:s.name,interval_minutes:s.interval_minutes,passphrase_ref:s.passphrase_ref,enabled:true,s3:s.s3?Object.fromEntries(S3_KEYS.map(key=>[key,s.s3[key]||''])):null}});await load();notice(messages,'Schedule '+s.name+' enabled. The next automatic backup runs in '+s.interval_minutes+' minutes.');}));
    const remove=button('Delete schedule',()=>act(async()=>{
      if(!await confirmAction({title:'Delete schedule',target:s.name,consequence:'Automatic backups from this schedule stop. Existing archives remain.',confirmLabel:'Delete schedule',danger:true}))return;
      await api('/api/backups/schedules/'+id,{method:'DELETE'});await load();
    }),'danger');
    return el('section',{class:'card stack'},
      el('div',{class:'title-row'},el('h3',{},s.name),stateBadge(s.enabled?'success':'',s.enabled?'Enabled':'Paused')),
      el('p',{},'Every '+s.interval_minutes+' minutes'),
      el('p',{class:'muted'},'Next: '+new Date(s.next_run_at*1000).toLocaleString()),
      s.enabled?null:el('p',{class:'muted'},'Enable this schedule to run a backup.'),
      el('div',{class:'toolbar'},main,button('Edit',()=>edit(s),'ghost'),actionMenu([remove])));
  }
  async function load(){const result=await api('/api/backups/schedules',{signal});schedules.replaceChildren(...result.data.map(scheduleCard));if(!result.data.length)schedules.append(empty('No automatic backups','Add a schedule using a vault or environment passphrase reference.',[button('Open backup schedules',()=>{document.getElementById('backup-schedules')?.scrollIntoView();},'ghost')]));}
  function edit(s={}){const name=el('input',{value:s.name||'',required:true}),interval=el('input',{type:'number',min:15,max:525600,value:s.interval_minutes||1440,required:true}),ref=el('input',{value:s.passphrase_ref||'',placeholder:'$BACKUP_PASSWORD',required:true,class:'control-mono',autocomplete:'off',spellcheck:'false'}),enabled=el('input',{type:'checkbox',checked:s.enabled??true}),remote=el('input',{type:'checkbox',checked:Boolean(s.s3)}),remoteFields=el('div',{class:'form-subgroup'}),inputs={};for(const [key,label] of [['endpoint','HTTPS endpoint'],['bucket','Bucket'],['region','Region'],['prefix','Object prefix (optional)'],['credentials_ref','Credentials reference']]){inputs[key]=el('input',{value:s.s3?.[key]||'',required:key!=='prefix'});remoteFields.append(field(label,inputs[key]));}function toggle(){remoteFields.hidden=!remote.checked;for(const input of Object.values(inputs))input.disabled=!remote.checked;}remote.addEventListener('change',toggle);toggle();const form=formCard({title:s.id?'Edit schedule':'New schedule',description:'Creates an encrypted backup on a fixed interval.',
      body:[el('div',{class:'form-row'},field('Name',name),field('Interval in minutes',interval,{hint:'At least 15. 1440 runs once a day.'})),field('Export passphrase reference',ref,{hint:'Reference a saved secret, for example $BACKUP_PASSWORD.'}),field('Enabled',enabled,{hint:'Paused schedules keep their settings and do not run.'}),field('Also upload to S3',remote),remoteFields],
      actions:[button('Cancel',()=>editor.replaceChildren(),'ghost'),el('button',{type:'submit',class:'button'},'Save schedule')]});
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{await api('/api/backups/schedules'+(s.id?'/'+encodeURIComponent(s.id):''),{method:s.id?'PUT':'POST',json:{name:name.value,interval_minutes:Number(interval.value),passphrase_ref:ref.value,enabled:enabled.checked,s3:remote.checked?Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,input.value])):null}});editor.replaceChildren();await load();});});editor.replaceChildren(form);}
  const passphrase=el('input',{type:'password',minLength:14,maxLength:256,required:true,autocomplete:'new-password'}),exportForm=formCard({title:'Download an encrypted snapshot',description:'A one-off archive of your Inference data, secrets and notes.',body:[field('Export passphrase',passphrase,{hint:'At least 14 characters. Keep it somewhere separate: restoring needs it.'})],actions:[el('button',{class:'button',type:'submit'},'Export backup')]});
  exportForm.addEventListener('submit',e=>{e.preventDefault();act(async()=>{await download('/api/backups/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({passphrase:passphrase.value})});passphrase.value='';});});
  container.append(heading('Backups','Encrypted recovery copies of your Inference data, secrets and notes.'),messages,exportForm,el('div',{class:'toolbar'},el('h2',{id:'backup-schedules'},'Schedules'),button('Add schedule',()=>edit()),button('Refresh',()=>act(async()=>{await load();await runs();}),'ghost')),editor,schedules,el('h2',{},'Run history'),history,el('section',{class:'card stack'},el('h2',{},'Restore offline'),el('p',{},'Stop Inference, choose an empty destination, and run this command from the project environment. It prompts for your passphrase.'),el('code',{},'python -m backend.modules.backups.restore backup.ibackup /path/to/empty-destination'),el('p',{class:'muted'},'After verification, set INFERENCE_DATA_DIR to the restored destination. External n8n databases and Docker volumes are not included.')));
  await Promise.allSettled([act(load),act(()=>runs())]);return()=>controller.abort();
}
