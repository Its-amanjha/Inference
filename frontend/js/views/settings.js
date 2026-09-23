import {icon} from '../lib/icons.js';
import {parseTimestamp} from '../lib/triage.js';
import {api as request} from '../api.js';
import {el,button,field,formCard,notice,heading,table,empty} from '../lib/html.js';
import {state,can} from '../state.js';
import {confirmAction,stateBadge,humanize} from '../lib/confirm.js';
import {segmented} from '../lib/select-menu.js';

const OUTCOMES={success:['success','Succeeded'],succeeded:['success','Succeeded'],deployed:['success','Deployed'],activated:['success','Activated'],restored:['success','Restored'],supported:['success','Supported'],failure:['error','Failed'],failed:['error','Failed'],rejected:['error','Rejected'],denied:['error','Denied'],error:['error','Error'],partial:['warning','Partial'],unknown:['warning','Unknown'],uncertain:['warning','Uncertain'],stale:['warning','Stale'],intent:['info','In progress'],pending:['info','In progress'],executing:['info','In progress'],validating:['info','In progress'],provisioning:['info','In progress'],writing:['info','In progress'],activating:['info','In progress'],restoring:['info','In progress'],expired:['','Expired'],cancelled:['','Cancelled'],untested:['','Untested'],unsupported:['','Unsupported'],removed:['','Removed']};
function outcomeBadge(value){
  if(value===null||value===undefined||value==='')return '—';
  const raw=String(value),[kind,label]=OUTCOMES[raw.toLowerCase()]||['',humanize(raw)];
  return stateBadge(kind,label,raw);
}
// Machine action names become readable phrases; the raw value stays in the title
// for auditors, and anything unmapped falls back to a humanized verbatim label.
const ACTIONS={'auth.setup':'Created the first administrator','auth.login':'Signed in','auth.logout':'Signed out','auth.totp_enabled':'Turned on two-factor authentication','auth.totp_disabled':'Turned off two-factor authentication','auth.totp_enrollment_started':'Started two-factor setup','admin.user_created':'Created a user','admin.user_updated':'Updated a user','admin.secret_saved':'Saved a secret','admin.secret_deleted':'Deleted a secret','admin.secret_template_applied':'Applied a secret template','admin.settings_updated':'Updated system settings','admin.preferences_updated':'Updated preferences','fleet.instance_created':'Connected an instance','fleet.instance_updated':'Updated a connection','fleet.instance_removed':'Removed a connection','fleet.instance_probed':'Tested a connection','fleet.workflow_activated':'Activated a workflow','fleet.workflow_deactivated':'Deactivated a workflow','fleet.execution_deleted':'Deleted an execution','fleet.node_updated':'Updated node code','fleet.changed':'Fleet changed','notes.saved':'Saved a note','notes.deleted':'Deleted a note','notes.changed':'Notes changed','backups.schedule_saved':'Saved a backup schedule','backups.schedule_deleted':'Deleted a backup schedule','backups.run_completed':'Completed a backup run','backups.exported':'Exported a backup','backups.reconciled':'Reconciled a backup','backups.local_deleted':'Deleted a local archive','backups.changed':'Backups changed','credentials.mapping_saved':'Saved a credential mapping','credentials.mapping_deleted':'Deleted a credential mapping','docker.staged':'Staged a bundle','docker.restarted':'Restarted a bundle','docker.changed':'Containers changed','assistant.approval':'Decided a proposal','assistant.configured':'Configured the assistant','assistant.tool_calling_checked':'Checked tool calling','assistant.repair_settings_updated':'Updated repair settings','assistant.workflow_repaired':'Repaired a workflow','assistant.workflow_restored':'Restored a workflow','observability.price_override':'Overrode a price','observability.price_sync':'Synced prices','errors.resolved':'Resolved an error','errors.reopened':'Reopened an error'};
// Audit targets are stored as IDs. Name their kind: backup runs and schedules,
// instance workflows ("Workflow <id>", since names are not stored), or instance names.
function auditTarget(value, action = ''){
  const raw=String(value??'');if(!raw)return '—';
  if(action==='backups.run_completed')return el('span',{title:raw},`Backup run ${raw}`);
  if(action==='backups.schedule_saved'||action==='backups.schedule_deleted')return el('span',{title:raw},`Backup schedule ${raw}`);
  if(action.startsWith('telemetry.token_'))return el('span',{title:raw},`Telemetry token ${raw.slice(0,8)}`);
  const [head,...rest]=raw.split(':'),instance=state.instances.find(item=>item.id===head);
  if(!instance)return raw;
  return el('span',{title:raw},rest.length?`${instance.name} · Workflow ${rest[0]}`:instance.name);
}
const TELEMETRY_ACTIONS={'telemetry.token_created':'Created a telemetry token','telemetry.token_revoked':'Revoked a telemetry token',
  'telemetry.polling_configured':'Changed execution polling','telemetry.retention_changed':'Changed telemetry retention',
  'telemetry.classified':'Checked an execution for silent failures'};
function actionLabel(action){
  const raw=String(action??'');
  if(ACTIONS[raw])return ACTIONS[raw];
  if(TELEMETRY_ACTIONS[raw])return TELEMETRY_ACTIONS[raw];
  if(raw.startsWith('promotion.'))return `Promotion ${humanize(raw.slice(10))}`;
  if(raw.startsWith('docker.'))return `Docker ${humanize(raw.slice(7))}`;
  // An action without its own wording reads as "Area: what happened", never as a dotted code.
  const [area,...rest]=raw.split('.');
  return rest.length?`${humanize(area)}: ${humanize(rest.join(' ')).toLowerCase()}`:humanize(raw);
}

export async function render(container){
  const controller=new AbortController(),signal=controller.signal,messages=el('div'),factor=el('section',{class:'card form-card'});
  // One group per task so an operator jumps straight to it: in-page links move
  // focus to the section heading without passing unrelated fields. Tabs and
  // disclosures were rejected because they would hide fields that later fail
  // validation; nothing here is ever hidden.
  const groups={};
  for(const [key,label] of [['account','Account'],['security','Security'],['deployment','Deployment'],['users','Users'],['audit','Audit']])groups[key]=el('section',{class:'settings-section',id:`settings-${key}`},el('h2',{class:'settings-group-title',tabindex:-1},label));
  const sectionIcons={account:'user',security:'shield',display:'theme',deployment:'containers',users:'user',audit:'history'};
  const navLinks=[['account','Account'],['security','Security'],['display','Display']];
  if(can('admin'))navLinks.push(['deployment','Deployment'],['users','Users'],['audit','Audit']);
  const nav=el('nav',{class:'settings-nav','aria-label':'Settings sections'},
    ...navLinks.map(([key,label])=>el('a',{class:'button ghost',href:`#settings-${key}`},icon(sectionIcons[key]),label)));
  nav.addEventListener('click',event=>{
    const link=event.target.closest('a[href^="#settings-"]');
    if(!link)return;
    const target=document.getElementById(link.hash.slice(1));
    // Never set location.hash: the router would leave this view. Focus the
    // heading so screen readers announce where we landed ("Account, heading"),
    // and flash the section for sighted keyboard users.
    if(target){
      event.preventDefault();target.scrollIntoView();
      for(const flashed of container.querySelectorAll('.settings-flash'))flashed.classList.remove('settings-flash');
      target.classList.add('settings-flash');
      const head=target.querySelector('h2,h3');
      (head||target).focus({preventScroll:true});
    }
  });
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{messages.replaceChildren();await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  // forEveryone: the administrators' defaults, which each person can override under Your preferences.
  function preferenceFields(value,subhead=true,onDirty=null,forEveryone=false){
    // Three themes that switch often sit side by side instead of in a dropdown.
    const theme=segmented({label:forEveryone?'Default theme':'Theme',options:[
      {value:'light',label:'Light'},{value:'dark',label:'Dark'},{value:'n8n',label:'n8n inspired'}],
      value:value.theme,onChange:()=>onDirty?.()});
    // A stored size outside the four offered stays selectable as itself.
    const sizes=[10,25,50,100].includes(Number(value.page_size))?[10,25,50,100]:[10,25,50,100,Number(value.page_size)].sort((a,b)=>a-b);
    const size=segmented({label:forEveryone?'Default rows per list':'Rows per list',options:sizes.map(count=>({value:String(count),label:String(count)})),
      value:String(value.page_size),onChange:()=>onDirty?.()});
    const notifications=el('input',{type:'checkbox',checked:value.error_notifications});
    // The visible labels are hidden from assistive technology: each toggle already names itself.
    const labelled=(text,control,hint)=>el('div',{class:'codelab-field'},el('span',{class:'codelab-field-label','aria-hidden':'true'},text),control.node,
      hint?el('p',{class:'field-hint'},hint):null);
    const display=[el('div',{class:'form-row'},labelled(forEveryone?'Default theme':'Theme',theme),
        labelled(forEveryone?'Default rows per list':'Rows per list',size,'How many errors, traces and backup runs load at a time.')),
      field(forEveryone?'Error notifications on by default':'Error notifications',notifications,{hint:'Show a notice when a new execution error arrives.'})];
    return {nodes:[...(subhead?[el('h3',{id:'settings-display',class:'settings-subhead',tabindex:-1},'Display')]:[]),...display],value:()=>({theme:theme.value,page_size:Number(size.value),error_notifications:notifications.checked})};}
  // One primary at a time: a card's save stays secondary and disabled until that
  // card is edited, and only the edited card's save turns primary.
  function armSave(form,save){
    let dirty=true;
    const mark=()=>{if(dirty)return;dirty=true;save.classList.remove('ghost');save.disabled=false;};
    const reset=()=>{dirty=false;save.classList.add('ghost');save.disabled=true;};
    reset();
    form.addEventListener('input',mark);
    form.addEventListener('change',mark);
    return Object.assign(mark,{reset});
  }
  async function loadPreferences(){
    let mark=()=>{};
    const fields=preferenceFields(await api('/api/admin/preferences',{signal}),true,()=>mark());
    const save=el('button',{type:'submit',class:'button'},icon('save'),'Save preferences');
    const form=formCard({title:'Your preferences',description:'Only for you, on every device you sign in from. They take the place of the defaults for everyone.',body:fields.nodes,actions:[save]});
    mark=armSave(form,save);
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{const result=await api('/api/admin/preferences',{method:'PUT',json:fields.value()});mark.reset();window.dispatchEvent(new CustomEvent('preferences-changed',{detail:result}));notice(messages,'Preferences saved.');});});groups.account.append(form);}

  // Same words as the account menu: Administrator, Operator, Viewer.
  const ROLE_LABELS={admin:'Administrator',operator:'Operator',viewer:'Viewer'};
  const roleLabel=role=>ROLE_LABELS[role]||humanize(role);
  // Two-factor state: true/false once known (admin user list or this session's own actions), null when unavailable, undefined while checking.
  const password=el('input',{type:'password',autocomplete:'current-password','aria-label':'Current password'}),code=el('input',{autocomplete:'one-time-code','aria-label':'Authenticator or recovery code'}),factorResult=el('div'),factorStep=el('div',{class:'factor-step'});
  let factorEnabled=can('admin')?undefined:null,factorPending=false;
  const startSetup=className=>button('Start setup',()=>act(async()=>{
    let result;
    try{result=await api('/api/auth/totp/enroll',{method:'POST',json:{password:password.value}});}
    catch(error){if(error.code==='totp_enabled'){factorEnabled=true;factorPending=false;renderFactor();}throw error;}
    password.value='';factorPending=true;
    factorResult.replaceChildren(el('p',{},'Add this secret to your authenticator, then enter its current code and confirm. Keep this information private.'),el('code',{},result.secret),el('details',{},el('summary',{},'Authenticator setup URI'),el('code',{},result.uri)));
    renderFactor();code.focus();
  }),className);
  const confirmSetup=className=>button('Confirm setup',()=>act(async()=>{
    const result=await api('/api/auth/totp/confirm',{method:'POST',json:{code:code.value}});
    code.value='';factorEnabled=true;factorPending=false;renderFactor();
    factorResult.replaceChildren(el('p',{},'Two-factor authentication enabled. Save these single-use recovery codes somewhere private.'),el('pre',{},result.recovery_codes.join('\n')));
  }),className);
  const disableFactor=()=>button('Disable two-factor',()=>act(async()=>{
    if(!password.value||!code.value)throw new Error('Enter your current password and an authenticator or recovery code to disable two-factor authentication.');
    if(!await confirmAction({title:'Disable two-factor authentication',target:state.user?.username,consequence:'Signing in to this account will require only its password.',confirmLabel:'Disable two-factor',danger:true}))return;
    await api('/api/auth/totp/disable',{method:'POST',json:{password:password.value,code:code.value}});
    password.value='';code.value='';factorEnabled=false;factorPending=false;renderFactor();
    factorResult.replaceChildren(el('p',{},'Two-factor authentication disabled.'));
  }),'danger');
  function renderFactor(){
    const badge=factorPending?stateBadge('info','Setup started'):factorEnabled===true?stateBadge('success','On'):factorEnabled===false?stateBadge('','Off'):null;
    let step;
    if(factorEnabled===undefined&&!factorPending)step=[el('p',{class:'muted'},'Checking two-factor status…')];
    else if(factorPending)step=[field('Authenticator or recovery code',code),el('div',{class:'toolbar'},confirmSetup('button'),button('Cancel setup',()=>{factorPending=false;code.value='';factorResult.replaceChildren();renderFactor();},'ghost'))];
    else if(factorEnabled===true)step=[el('p',{class:'muted'},'To turn it off, enter your current password and a code.'),field('Current password',password),field('Authenticator or recovery code',code),el('div',{class:'toolbar'},disableFactor())];
    else if(factorEnabled===false)step=[el('p',{class:'muted'},'Require an authenticator code in addition to your password when you sign in.'),field('Current password',password),el('div',{class:'toolbar'},startSetup('button'))];
    // Status unknown (non-admins never see the user list): only Start setup, with
    // Confirm revealed after enrollment and Disable behind a disclosure.
    else step=[el('p',{class:'muted'},'Require an authenticator code in addition to your password when you sign in.'),field('Current password',password),el('div',{class:'toolbar'},startSetup('button')),
      el('details',{class:'form-disclosure'},el('summary',{},'Already turned on?'),
        el('p',{class:'muted'},'If setup already finished, enter a code above to turn it off.'),field('Authenticator or recovery code',code),el('div',{class:'toolbar'},disableFactor()))];
    factorStep.replaceChildren(...step);
    factor.replaceChildren(el('div',{class:'form-card-head'},el('div',{class:'title-row'},el('h2',{},'Two-factor authentication'),badge)),el('div',{class:'form-card-body'},factorStep,factorResult));
  }
  renderFactor();

  async function loadPolicy(){const result=await api('/api/admin/settings',{signal});
    let mark=()=>{};
    const fields=preferenceFields(result.saved.default_preferences,false,()=>mark(),true),hours=el('input',{type:'number',min:1,max:168,required:true,value:result.saved.session_hours,disabled:result.environment_locked_fields.includes('session_hours')});
    const save=el('button',{type:'submit',class:'button'},icon('save'),'Save system defaults');
    const form=formCard({title:'Defaults for everyone',description:'What each person starts with until they choose their own under Your preferences, and how long new sessions last.',actions:[save],body:[field('New session lifetime (hours)',hours,{hint:result.environment_locked_fields.includes('session_hours')?'Set by the deployment environment.':'Between 1 and 168. Existing sessions keep their current expiry.'}),...fields.nodes,el('details',{class:'form-disclosure'},el('summary',{},icon('chevron'),'Deployment configuration (read only)'),deploymentFacts(result))]});
    mark=armSave(form,save);
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{await api('/api/admin/settings',{method:'PUT',json:{expected_revision:result.revision,session_hours:Number(hours.value),default_preferences:fields.value()}});await loadAdmin();notice(messages,'System defaults saved. Existing sessions keep their current expiry.');});});return form;}
  // Set by the server's environment; shown as plain facts rather than raw JSON.
  function deploymentFacts(result){
    const list=values=>Array.isArray(values)&&values.length?values.join(', '):'None';
    return el('dl',{class:'status-lines'},[
      ['Secure cookies',result.secure_cookies?'On (HTTPS only)':'Off'],
      ['Session lifetime in effect',`${result.session_hours} hours`],
      ['Allowed browser origins',list(result.allowed_origins)],
      ['Extra egress ranges',list(result.allow_cidrs)],
      ['Fixed by the environment',list(result.environment_locked_fields)],
    ].map(([label,value])=>[el('dt',{},label),el('dd',{},value)]));
  }
  function userEditor(user,host,refresh){
    const username=el('input',{required:true,value:user?.username||'',pattern:'[A-Za-z0-9_.\\-]+',readOnly:Boolean(user)}),password=el('input',{type:'password',minLength:14,maxLength:256,required:!user,autocomplete:'new-password'});
    let mark=()=>{};
    const role=segmented({label:'Role',options:[
      {value:'viewer',label:'Viewer'},{value:'operator',label:'Operator'},{value:'admin',label:'Administrator'}],
      value:user?.role||'viewer',onChange:()=>mark()});
    const roleField=el('div',{class:'codelab-field'},el('span',{class:'codelab-field-label','aria-hidden':'true'},'Role'),role.node);
    const disabled=el('input',{type:'checkbox',checked:Boolean(user?.disabled)});
    const save=el('button',{type:'submit',class:'button'},'Save user');
    const form=el('form',{class:'form user-editor'},el('div',{class:'form-row'},field('Username',username),roleField),field(user?'New password (optional)':'Password',password,{hint:'At least 14 characters.'}),...(user?[field('Disabled',disabled,{})]:[]),el('div',{class:'form-actions'},button('Cancel',()=>host.replaceChildren(),'ghost'),save));
    mark=armSave(form,save);
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{const body={role:role.value};if(user)body.disabled=disabled.checked;else body.username=username.value;if(password.value)body.password=password.value;await api('/api/admin/users'+(user?'/'+encodeURIComponent(user.username):''),{method:user?'PATCH':'POST',json:body});password.value='';host.replaceChildren();await refresh();});});host.replaceChildren(form);}
  async function loadUsers(){const section=el('section',{class:'card form-card'}),host=el('div'),rows=el('div');async function refresh(){const result=await api('/api/admin/users',{signal});rows.replaceChildren(table(['User','Role','Status','2FA',''],result.data.map(u=>[u.username,stateBadge('',roleLabel(u.role),u.role),u.disabled?el('span',{class:'badge warning'},icon('errors'),'Disabled'):el('span',{class:'badge success'},icon('check'),'Enabled'),u.totp_enabled?stateBadge('success','On'):stateBadge('','Off'),button('Edit',()=>userEditor(u,host,refresh),'ghost',{'aria-label':`Edit user ${u.username}`})])));
    const me=result.data.find(u=>u.username===state.user?.username);
    if(me&&!factorPending&&factorEnabled!==Boolean(me.totp_enabled)){factorEnabled=Boolean(me.totp_enabled);renderFactor();}}
    section.append(el('div',{class:'form-card-head card-head-row'},button('Add user',()=>userEditor(null,host,refresh),'ghost')),el('div',{class:'form-card-body is-wide'},host,rows));await refresh();return section;}
  // Ten events per page, newest first. The API pages backwards with a cursor, so Previous
  // reuses the cursors already passed rather than asking for a total the log does not keep.
  async function loadAudit(){
    const section=el('section',{class:'card form-card audit-card'});
    const rows=el('div'),position=el('span',{class:'audit-position','aria-live':'polite'});
    const cursors=[null];let page=0,next=null;
    const when=value=>{const date=parseTimestamp(value);return date?el('time',{dateTime:date.toISOString(),title:date.toLocaleString()},
      date.toLocaleString([],{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})):String(value||'—');};
    const previous=button('Newer',()=>act(()=>show(page-1)),'ghost',{'aria-label':'Newer audit events'});
    const older=button('Older',()=>act(()=>show(page+1)),'ghost',{'aria-label':'Older audit events'});
    async function show(target){
      previous.disabled=older.disabled=true;
      let result;
      try{result=await api('/api/admin/audit?limit=10'+(cursors[target]?'&before='+encodeURIComponent(cursors[target]):''),{signal});}
      catch(error){previous.disabled=page===0;older.disabled=!next;throw error;}
      page=target;next=result.next_cursor??null;cursors[page+1]=next;
      rows.replaceChildren(result.data.length?table(['Time','Actor','Action','Target','Outcome'],result.data.map(r=>[when(r.occurred_at||r.created_at),r.actor,
        el('span',{title:String(r.action??'')},actionLabel(r.action)),auditTarget(r.target,r.action),outcomeBadge(r.outcome)]),{className:'audit-table'})
        :empty('No audit events','Actions taken in Inference appear here.',[],'history'));
      const first=page*10+1;
      position.textContent=result.data.length?`Events ${first}–${first+result.data.length-1}`:'';
      previous.disabled=page===0;older.disabled=!next;
    }
    section.append(el('div',{class:'form-card-body is-wide'},rows,el('div',{class:'audit-pager'},position,el('div',{class:'audit-pager-buttons'},previous,older))));
    await show(0);return section;}
  async function loadAdmin(){
    // Groups persist across reloads (nav anchors point at them), so clear each
    // group's card before appending a fresh one; otherwise every save duplicates
    // the deployment, users and audit cards and their headings.
    for(const key of ['deployment','users','audit'])for(const node of [...groups[key].children])if(!node.matches('h2.settings-group-title'))node.remove();
    const [policy,users,audit]=await Promise.all([loadPolicy(),loadUsers(),loadAudit()]);
    groups.deployment.append(policy);groups.users.append(users);groups.audit.append(audit);
  }
  container.append(heading('Settings','Your preferences, account protection and application administration.'),messages,nav,groups.account,groups.security);
  // Two-factor belongs to everyone with an account, not only administrators.
  groups.security.append(factor);
  if(can('admin'))container.append(groups.deployment,groups.users,groups.audit);
  const adminLoaded=can('admin')?act(loadAdmin).then(()=>{if(factorEnabled===undefined){factorEnabled=null;renderFactor();}}):null;
  await Promise.allSettled([act(loadPreferences),adminLoaded]);return()=>{controller.abort();password.value='';code.value='';factorResult.replaceChildren();};
}
