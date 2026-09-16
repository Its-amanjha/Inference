import {icon} from '../lib/icons.js';
import {api as request} from '../api.js';
import {el,button,field,formCard,notice,heading,jsonView,table} from '../lib/html.js';
import {state,can,pageSize} from '../state.js';
import {confirmAction,stateBadge,humanize} from '../lib/confirm.js';

const OUTCOMES={success:['success','Succeeded'],succeeded:['success','Succeeded'],intent:['info','Started'],failure:['error','Failed'],failed:['error','Failed'],unknown:['warning','Unknown'],uncertain:['warning','Unknown']};
function outcomeBadge(value){
  if(value===null||value===undefined||value==='')return '—';
  const raw=String(value),[kind,label]=OUTCOMES[raw.toLowerCase()]||['',humanize(raw)];
  return stateBadge(kind,label,raw);
}

export async function render(container){
  const controller=new AbortController(),signal=controller.signal,messages=el('div'),preferences=el('section',{class:'settings-section'}),factor=el('section',{class:'card form-card'}),admin=el('div',{class:'stack'});
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{messages.replaceChildren();await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  function preferenceFields(value){const theme=el('select',{},el('option',{value:'light'},'Light'),el('option',{value:'dark'},'Dark'),el('option',{value:'n8n'},'n8n inspired'));theme.value=value.theme;const size=el('input',{type:'number',min:10,max:100,value:value.page_size,required:true,class:'control-narrow'}),notifications=el('input',{type:'checkbox',checked:value.error_notifications});return {nodes:[el('div',{class:'form-row'},field('Theme',theme),field('Rows per page',size,{hint:'Between 10 and 100.'})),field('Error notifications',notifications,{hint:'Show alerts for new execution errors and silent failures.'})],value:()=>({theme:theme.value,page_size:Number(size.value),error_notifications:notifications.checked})};}
  async function loadPreferences(){const fields=preferenceFields(await api('/api/admin/preferences',{signal}));const form=formCard({title:'Your preferences',description:'Saved to your account.',body:fields.nodes,actions:[el('button',{type:'submit',class:'button'},icon('save'),'Save preferences')]});form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{const result=await api('/api/admin/preferences',{method:'PUT',json:fields.value()});window.dispatchEvent(new CustomEvent('preferences-changed',{detail:result}));notice(messages,'Preferences saved.');});});preferences.replaceChildren(form);}

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
    else step=[field('Current password',password),field('Authenticator or recovery code',code),el('div',{class:'toolbar'},startSetup('ghost'),confirmSetup('button'),disableFactor())];
    factorStep.replaceChildren(...step);
    factor.replaceChildren(el('div',{class:'form-card-head'},el('div',{class:'title-row'},el('h2',{},'Two-factor authentication'),badge)),el('div',{class:'form-card-body'},factorStep,factorResult));
  }
  renderFactor();

  async function loadPolicy(){const result=await api('/api/admin/settings',{signal}),fields=preferenceFields(result.saved.default_preferences),hours=el('input',{type:'number',min:1,max:168,required:true,value:result.saved.session_hours,disabled:result.environment_locked_fields.includes('session_hours')}),form=formCard({title:'System defaults',description:'Installation-wide preference defaults and the lifetime of new sessions.',actions:[el('button',{type:'submit',class:'button'},icon('save'),'Save system defaults')],body:[field('New session lifetime (hours)',hours,{hint:result.environment_locked_fields.includes('session_hours')?'Set by the deployment environment.':'Between 1 and 168. Existing sessions keep their current expiry.'}),...fields.nodes,el('details',{class:'form-disclosure'},el('summary',{},icon('chevron'),'Deployment configuration (read only)'),el('div',{},jsonView({secure_cookies:result.secure_cookies,effective_session_hours:result.session_hours,allowed_origins:result.allowed_origins,allow_cidrs:result.allow_cidrs,environment_locked_fields:result.environment_locked_fields})))]});
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{await api('/api/admin/settings',{method:'PUT',json:{expected_revision:result.revision,session_hours:Number(hours.value),default_preferences:fields.value()}});await loadAdmin();notice(messages,'System defaults saved. Existing sessions keep their current expiry.');});});return form;}
  function userEditor(user,host,refresh){const username=el('input',{required:true,value:user?.username||'',pattern:'[A-Za-z0-9_.-]+',readOnly:Boolean(user)}),password=el('input',{type:'password',minLength:14,maxLength:256,required:!user,autocomplete:'new-password'}),role=el('select',{},...['viewer','operator','admin'].map(r=>el('option',{value:r},r))),disabled=el('input',{type:'checkbox',checked:Boolean(user?.disabled)});role.value=user?.role||'viewer';const form=el('form',{class:'form user-editor'},el('div',{class:'form-row'},field('Username',username),field('Role',role)),field(user?'New password (optional)':'Password',password,{hint:'At least 14 characters.'}),...(user?[field('Disabled',disabled,{hint:'Disabled accounts cannot sign in.'})]:[]),el('div',{class:'form-actions'},button('Cancel',()=>host.replaceChildren(),'ghost'),el('button',{type:'submit',class:'button'},'Save user')));form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{const body={role:role.value};if(user)body.disabled=disabled.checked;else body.username=username.value;if(password.value)body.password=password.value;await api('/api/admin/users'+(user?'/'+encodeURIComponent(user.username):''),{method:user?'PATCH':'POST',json:body});password.value='';host.replaceChildren();await refresh();});});host.replaceChildren(form);}
  async function loadUsers(){const section=el('section',{class:'card form-card'}),host=el('div'),rows=el('div');async function refresh(){const result=await api('/api/admin/users',{signal});rows.replaceChildren(table(['User','Role','Status','2FA',''],result.data.map(u=>[u.username,u.role,u.disabled?'Disabled':'Enabled',u.totp_enabled?'Enabled':'Off',button('Edit',()=>userEditor(u,host,refresh),'ghost')])));
    const me=result.data.find(u=>u.username===state.user?.username);
    if(me&&!factorPending&&factorEnabled!==Boolean(me.totp_enabled)){factorEnabled=Boolean(me.totp_enabled);renderFactor();}}
    section.append(el('div',{class:'form-card-head card-head-row'},el('h2',{},'Users'),button('Add user',()=>userEditor(null,host,refresh),'ghost')),el('div',{class:'form-card-body is-wide'},host,rows));await refresh();return section;}
  async function loadAudit(){const section=el('section',{class:'card form-card'},el('div',{class:'form-card-head'},el('h2',{},'Audit history'))),rows=el('div'),body=el('div',{class:'form-card-body is-wide'});let cursor=null;async function more(){const result=await api('/api/admin/audit?limit='+pageSize()+(cursor?'&before='+encodeURIComponent(cursor):''),{signal});cursor=result.next_cursor;rows.append(table(['Time','Actor','Action','Target','Outcome'],result.data.map(r=>[r.occurred_at||r.created_at||'—',r.actor,r.action,r.target,outcomeBadge(r.outcome)])));load.disabled=!cursor;}const load=button('Load older events',()=>act(more),'ghost');body.append(rows,load);section.append(body);await more();return section;}
  async function loadAdmin(){const sections=await Promise.all([loadPolicy(),loadUsers(),loadAudit()]);admin.replaceChildren(...sections);}
  container.append(heading('Settings','Your preferences, account protection and application administration.'),messages,preferences,factor,admin);
  const adminLoaded=can('admin')?act(loadAdmin).then(()=>{if(factorEnabled===undefined){factorEnabled=null;renderFactor();}}):null;
  await Promise.allSettled([act(loadPreferences),adminLoaded]);return()=>{controller.abort();password.value='';code.value='';factorResult.replaceChildren();};
}
