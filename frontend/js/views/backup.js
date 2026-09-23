import {api as request,apiFetch} from '../api.js';
import {el,button,field,formCard,notice,empty,heading,table} from '../lib/html.js';
import {can,pageSize} from '../state.js';
import {confirmAction,stateBadge,humanize} from '../lib/confirm.js';
import {describeCode} from '../lib/payload.js';
import {rowMenu} from '../lib/row-menu.js';
import {selectMenu} from '../lib/select-menu.js';

const RUN_STATES={succeeded:'success',success:'success',failed:'error',partial:'warning',uncertain:'warning',running:'info',deleting:'info'};
const S3_KEYS=['endpoint','bucket','region','prefix','credentials_ref'];

// The API keeps storing minutes; the form offers named frequencies instead of a number.
const FREQUENCIES=[
  {value:'60',label:'Hourly',glyph:'clock'},
  {value:'360',label:'Every 6 hours',glyph:'clock'},
  {value:'1440',label:'Daily',glyph:'clock'},
  {value:'10080',label:'Weekly',glyph:'clock'},
];
function frequencyLabel(minutes){
  const match=FREQUENCIES.find(option=>option.value===String(minutes));
  return match?match.label:`Every ${minutes} minutes`;
}
// "Next run in 23 h": a short relative phrase with the exact time on hover.
function nextRunIn(epochSeconds){
  if(!Number.isFinite(epochSeconds))return 'Next run unknown';
  const date=new Date(epochSeconds*1000);
  const diff=Math.round((date.getTime()-Date.now())/60000);
  const phrase=diff<1?'due now':diff<60?`in ${diff} min`:diff<4320?`in ${Math.round(diff/60)} h`:`in ${Math.round(diff/1440)} d`;
  return el('span',{title:date.toLocaleString()},`Next run ${phrase}`);
}

function runStatus(run){
  const label=run.status==='deleted'?'Local file deleted':humanize(run.status);
  return el('span',{class:'run-status'},stateBadge(RUN_STATES[run.status]||'',label,run.status),run.code?el('span',{class:'muted'},describeCode(run.code)):null);
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
    const reconcile=r.sha256&&['uncertain','partial'].includes(r.status);
    const more=[
      reconcile?{label:'Reconcile',onSelect:()=>act(async()=>{await api('/api/backups/runs/'+id+'/reconcile',{method:'POST'});await runs();})}:null,
      hasFile?{label:'Delete local file',danger:true,onSelect:()=>act(async()=>{
        if(!await confirmAction({title:'Delete local archive',target:[el('strong',{},'Backup from '+when),' (run '+r.id+')'],consequence:'Deletes the archive file stored on this server. The run stays in history and any S3 copy remains.',confirmLabel:'Delete local file',danger:true}))return;
        await api('/api/backups/runs/'+id+'/file',{method:'DELETE'});await runs();
      })}:null,
    ].filter(Boolean);
    return el('div',{class:'toolbar backup-run-actions'},
      hasFile?button('Download',()=>act(()=>download('/api/backups/runs/'+id+'/download')),'ghost',{'aria-label':`Download backup from ${when}`}):null,
      more.length?rowMenu(more,`More actions for backup from ${when}`):null);
  }
  async function runs(append=false){const result=await api('/api/backups/runs?limit='+pageSize()+(append&&next?'&before='+encodeURIComponent(next):''),{signal});next=result.next_cursor;if(!append)history.replaceChildren();history.querySelector('[data-more]')?.remove();if(result.data.length){const page=table(['Created','Status','Remote','Actions'],result.data.map(r=>[new Date(r.created_at*1000).toLocaleString(),runStatus(r),r.remote_status?humanize(r.remote_status):'—',runActions(r)]));const existing=append?history.querySelector('table'):null;if(existing)existing.querySelector('tbody').append(...page.querySelector('tbody').children);else history.append(page);}if(!result.data.length&&!append){const scheds=await api('/api/backups/schedules',{signal});const runnable=scheds.data.find(s=>s.enabled);history.append(el('p',{class:'muted history-empty'},runnable?'No backups yet. Use Run now on a schedule to make the first one.':'No backups yet. Add a schedule, or download a snapshot below.'));}if(next){const more=button('Load older runs',()=>act(()=>runs(true)),'ghost');more.dataset.more='true';history.append(more);}}
  function scheduleCard(s){
    const id=encodeURIComponent(s.id);
    const main=s.enabled
      ?button('Run now',()=>act(async()=>{const result=await api('/api/backups/schedules/'+id+'/run',{method:'POST'});notice(messages,'Backup outcome: '+humanize(result.status)+'.');await runs();await load();}),'ghost',{'aria-label':`Run backup now: ${s.name}`})
      :button('Enable',()=>act(async()=>{await api('/api/backups/schedules/'+id,{method:'PUT',json:{name:s.name,interval_minutes:s.interval_minutes,passphrase_ref:s.passphrase_ref,enabled:true,s3:s.s3?Object.fromEntries(S3_KEYS.map(key=>[key,s.s3[key]||''])):null}});await load();await runs();notice(messages,`Schedule ${s.name} enabled (${frequencyLabel(s.interval_minutes).toLowerCase()}).`);}),'ghost',{'aria-label':`Enable schedule ${s.name}`});
    // Deletion is secondary, so it sits behind the row's More actions menu and confirms.
    const remove={label:'Delete schedule',danger:true,onSelect:()=>act(async()=>{
      if(!await confirmAction({title:'Delete schedule',target:s.name,consequence:'Automatic backups from this schedule stop. Existing archives remain.',confirmLabel:'Delete schedule',danger:true}))return;
      await api('/api/backups/schedules/'+id,{method:'DELETE'});await load();await runs();
    })};
    return el('section',{class:'card stack'},
      el('div',{class:'title-row'},el('h3',{},s.name),stateBadge(s.enabled?'success':'',s.enabled?'Enabled':'Paused')),
      el('p',{class:'schedule-when'},el('span',{},frequencyLabel(s.interval_minutes)),el('span',{class:'muted','aria-hidden':'true'},' · '),
        s.enabled?el('span',{class:'muted'},nextRunIn(s.next_run_at)):el('span',{class:'muted'},'Paused: it does not run until you enable it')),
      el('div',{class:'toolbar'},main,button('Edit',()=>edit(s),'ghost',{'aria-label':`Edit schedule ${s.name}`}),rowMenu([remove],`More actions for ${s.name}`)));
  }
  let scheduleCount=0;
  // An open schedule form replaces the empty state it would otherwise sit above.
  const syncEditor=()=>{schedules.hidden=Boolean(editor.firstChild)&&!scheduleCount;addSchedule.hidden=Boolean(editor.firstChild);};
  async function load(){const result=await api('/api/backups/schedules',{signal});scheduleCount=result.data.length;schedules.replaceChildren(...result.data.map(scheduleCard));if(!result.data.length)schedules.append(empty('No automatic backups','Add a schedule. It encrypts a backup on a fixed interval with a passphrase kept in the vault or environment.',[],'backup'));syncEditor();}
  function edit(s={}){
    const name=el('input',{value:s.name||'',required:true});
    const minutes=Number(s.interval_minutes)||1440;
    // A stored interval outside the named set stays selectable as its own minutes value.
    const frequency=selectMenu({label:'Frequency',options:[
      ...FREQUENCIES.map(option=>({...option})),
      ...(FREQUENCIES.some(option=>option.value===String(minutes))?[]:[{value:String(minutes),label:`Every ${minutes} minutes`,glyph:'clock'}]),
    ],value:String(minutes),onChange:()=>{}});
    frequency.button.id=`backup-frequency-${s.id||'new'}`;
    const frequencyField=el('div',{class:'codelab-field'},el('label',{class:'codelab-field-label',for:frequency.button.id},'Frequency'),frequency.node);
    // The passphrase stays a reference: pick a saved secret or keep typing a $REFERENCE.
    const refListId=`backup-passphrase-${s.id||'new'}`;
    const ref=el('input',{value:s.passphrase_ref||'',placeholder:'$BACKUP_PASSWORD',required:true,class:'control-mono',autocomplete:'off',spellcheck:'false',list:refListId});
    const refField=field('Export passphrase reference',ref,{hint:'Pick a saved secret, or type a $REFERENCE.'});
    const suggestions=el('datalist',{id:refListId});
    refField.append(suggestions);
    // Suggestions are a convenience; typing a reference always works, so the form does not wait for them.
    api('/api/admin/secrets',{signal}).then(saved=>{if(!signal.aborted)suggestions.replaceChildren(...(saved.data||[]).map(secret=>el('option',{value:'$'+secret})));},()=>{});
    const enabled=el('input',{type:'checkbox',checked:s.enabled??true}),remote=el('input',{type:'checkbox',checked:Boolean(s.s3)}),remoteFields=el('div',{class:'form-subgroup'}),inputs={};for(const [key,label] of [['endpoint','HTTPS endpoint'],['bucket','Bucket'],['region','Region'],['prefix','Object prefix (optional)'],['credentials_ref','Credentials reference']]){inputs[key]=el('input',{value:s.s3?.[key]||'',required:key!=='prefix'});remoteFields.append(field(label,inputs[key]));}function toggle(){remoteFields.hidden=!remote.checked;for(const input of Object.values(inputs))input.disabled=!remote.checked;}remote.addEventListener('change',toggle);toggle();const form=formCard({title:s.id?'Edit schedule':'New schedule',description:'Creates an encrypted backup on a fixed interval.',
      body:[el('div',{class:'form-row'},field('Name',name),frequencyField),refField,field('Enabled',enabled,{}),field('Also upload to S3',remote),remoteFields],
      actions:[button('Cancel',()=>{editor.replaceChildren();syncEditor();addSchedule.focus();},'ghost'),el('button',{type:'submit',class:'button'},'Save schedule')]});
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{await api('/api/backups/schedules'+(s.id?'/'+encodeURIComponent(s.id):''),{method:s.id?'PUT':'POST',json:{name:name.value,interval_minutes:Number(frequency.value),passphrase_ref:ref.value,enabled:enabled.checked,s3:remote.checked?Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,input.value])):null}});editor.replaceChildren();await load();await runs();notice(messages,s.id?'Schedule saved.':'Schedule created. It will run automatically.');});});editor.replaceChildren(form);syncEditor();name.focus();}
  const passphrase=el('input',{type:'password',minLength:14,maxLength:256,required:true,autocomplete:'new-password'}),exportForm=formCard({title:'Download an encrypted snapshot',description:'A one-off archive of your Inference data, secrets and notes.',body:[field('Export passphrase',passphrase,{hint:'At least 14 characters. Restoring needs it.'})],actions:[el('button',{class:'button ghost',type:'submit'},'Download snapshot')]});
  exportForm.addEventListener('submit',e=>{e.preventDefault();act(async()=>{await download('/api/backups/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({passphrase:passphrase.value})});passphrase.value='';});});
  const addSchedule=button('Add schedule',()=>edit());
  const restoreCmd='python -m backend.modules.backups.restore backup.ibackup /path/to/empty-destination';
  const copyRestore=button('Copy command',()=>act(async()=>{
    try{await navigator.clipboard.writeText(restoreCmd);notice(messages,'Restore command copied.');}
    catch{notice(messages,new Error('Copying is not available in this browser.'));}
  }),'ghost',{'aria-label':'Copy restore command'});
  // Schedules and Run history share one section-heading style.
  const sectionTitle=(text,extra={})=>el('h2',{class:'backup-section-title',...extra},text);
  container.append(el('div',{class:'page-heading-row'},heading('Backups','Encrypted recovery copies of your Inference data, secrets and notes.'),el('div',{class:'actions'},button('Refresh',()=>act(async()=>{await load();await runs();}),'ghost'),addSchedule)),messages,sectionTitle('Schedules',{id:'backup-schedules'}),editor,schedules,sectionTitle('Run history'),history,exportForm,el('section',{class:'card stack'},el('h2',{class:'backup-card-title'},'Restore offline'),el('p',{},'Stop Inference, choose an empty destination, and run this command from the project environment. It prompts for your passphrase.'),el('div',{class:'restore-command-row'},el('code',{},restoreCmd),copyRestore),el('p',{class:'muted'},'After verification, set INFERENCE_DATA_DIR to the restored destination. External n8n databases and Docker volumes are not included.')));
  await Promise.allSettled([act(load),act(()=>runs())]);return()=>controller.abort();
}
