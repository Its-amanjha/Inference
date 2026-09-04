import {api} from './api.js';
import {el,button,notice} from './lib/html.js';
import {state,can,applyPreferences} from './state.js';
import {renderAuth} from './wizard.js';
import {setTheme} from './themes.js';
import {icon} from './lib/icons.js';

const routes = {'dashboard':'Overview','workflows':'Workflows','promote':'Promote','errors':'Errors','observe':'Observability','fleet-health':'Fleet health','codelab':'CodeLab','assistant':'AI workspace','containers':'Containers','notes':'Notes','backup':'Backups','secrets':'Secrets','instances':'Instances','settings':'Settings'};
const adminOnly = new Set(['containers','backup','secrets']);
const root=document.querySelector('#app');
const mobileNavigation=window.matchMedia('(max-width:700px)');
mobileNavigation.addEventListener('change',()=>{const menu=root.querySelector('.nav-more');if(menu)menu.open=!mobileNavigation.matches;});
document.querySelector('.skip-link')?.addEventListener('click',event=>{event.preventDefault();const main=document.querySelector('#main');if(main){main.tabIndex=-1;main.focus();main.scrollIntoView({block:'start'});}});
let cleanup=()=>{}, generation=0, socket=null, reconnect=null, shell=null, selector=null, authPending=false;
let reconnectDelay=1000, pendingBar=null, pendingCount=0;

// Toasts: one per distinct message (repeats raise a count), dismissible, paused while hovered or focused.
const TOAST_MS=8000, TOAST_LIMIT=3, toasts=new Map();
function removeToast(record) {
  clearTimeout(record.timer);
  if(toasts.get(record.message)===record)toasts.delete(record.message);
  if(record.node.contains(document.activeElement)){const main=document.querySelector('#main');if(main){main.tabIndex=-1;main.focus({preventScroll:true});}}
  record.node.remove();
}
function scheduleToast(record, delay=TOAST_MS) {
  clearTimeout(record.timer); record.remaining=delay; record.started=Date.now();
  if(!record.paused)record.timer=setTimeout(()=>removeToast(record),delay);
}
function toast(message) {
  const area=document.querySelector('#toasts'); if(!area)return;
  const text=String(message), existing=toasts.get(text);
  if(existing?.node.isConnected){
    existing.repeats+=1; existing.count.textContent=`×${existing.repeats}`; existing.count.hidden=false;
    scheduleToast(existing); return;
  }
  const count=el('span',{class:'toast-count','aria-hidden':'true',hidden:true});
  const dismiss=el('button',{type:'button',class:'toast-dismiss','aria-label':'Dismiss notification'},icon('close'));
  const node=el('div',{class:'toast'},el('span',{class:'toast-message'},text),count,dismiss);
  const record={message:text,node,count,repeats:1,timer:null,paused:false,remaining:TOAST_MS,started:0};
  const pause=()=>{if(record.paused)return;record.paused=true;clearTimeout(record.timer);record.remaining=Math.max(0,record.remaining-(Date.now()-record.started));};
  const resume=()=>{if(!record.paused||node.matches(':hover')||node.contains(document.activeElement))return;record.paused=false;scheduleToast(record,Math.max(record.remaining,2000));};
  node.addEventListener('mouseenter',pause); node.addEventListener('focusin',pause);
  node.addEventListener('mouseleave',resume); node.addEventListener('focusout',()=>setTimeout(resume));
  dismiss.addEventListener('click',()=>removeToast(record));
  for(const [,other] of toasts)if(!other.node.isConnected)toasts.delete(other.message);
  while(area.children.length>=TOAST_LIMIT){const oldest=area.firstElementChild,match=[...toasts.values()].find(item=>item.node===oldest);if(match)removeToast(match);else oldest.remove();}
  toasts.set(text,record); area.append(node); scheduleToast(record);
}
window.addEventListener('app-error',event=>toast(event.detail?.message || 'The operation could not be completed.'));

// Live events on reading views offer a refresh instead of rebuilding the page under the reader.
function clearPendingUpdates() { pendingBar?.remove(); pendingBar=null; pendingCount=0; }
function showPendingUpdates() {
  if(!shell)return;
  pendingCount+=1;
  if(!pendingBar||!pendingBar.isConnected){
    const refresh=button('Refresh view',async()=>{clearPendingUpdates();await navigate();shell?.focus({preventScroll:true});},'ghost');
    pendingBar=el('div',{class:'live-updates',role:'status'},el('span',{class:'live-updates-text'},'New activity since this view loaded.'),el('span',{class:'live-updates-count','aria-hidden':'true'}),refresh);
    shell.prepend(pendingBar);
  }
  pendingBar.querySelector('.live-updates-count').textContent=pendingCount>1?`${pendingCount} updates`:'';
}
function closeSocket() { clearTimeout(reconnect); if(socket){socket.onclose=null;socket.close();socket=null;} }
function connectSocket() {
  closeSocket(); if(!state.user)return;
  socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/ws`);
  const connection=message=>{const node=document.querySelector('#connection-status');if(node)node.textContent=message;};
  connection('Connecting to updates');
  socket.onopen=()=>{reconnectDelay=1000;connection('Live updates connected');};
  socket.onmessage=event=>{
    let packet; try{packet=JSON.parse(event.data);}catch{return;}
    if(!packet.event || ['connected','heartbeat'].includes(packet.event))return;
    const labels={'error:reported':'An execution error was reported.','silent_failure:detected':'A silent failure was detected.','workflow:promoted':'A workflow promotion changed.','langgraph:run':'An assistant run changed.','errors.changed':'Execution errors updated.','backups.changed':'Backup history updated.','notes.changed':'Notes updated.'};
    const errorEvent=['error:reported','silent_failure:detected','errors.changed'].includes(packet.event);
    const notify=!errorEvent || state.preferences.error_notifications;
    if(labels[packet.event] && notify)toast(labels[packet.event]);
    window.dispatchEvent(new CustomEvent('inference-event',{detail:packet}));
    const route=location.hash.slice(1).split('?')[0] || 'dashboard';
    if(['dashboard','errors','observe'].includes(route) && notify)showPendingUpdates();
  };
  socket.onclose=event=>{
    connection('Reconnecting to updates');
    if([4401,4403].includes(event.code)){window.dispatchEvent(new Event('session-expired'));return;}
    if(state.user){reconnect=setTimeout(connectSocket,reconnectDelay);reconnectDelay=Math.min(30000,reconnectDelay*2);}
  };
}
async function loadInstances() {
  const result=await api('/api/n8n_proxy/instances'); state.instances=result.data || [];
  if(state.instanceId&&!state.instances.some(item=>item.id===state.instanceId)) state.instanceId='';
  if(selector){selector.replaceChildren(el('option',{value:''},'All instances'),...state.instances.map(item=>el('option',{value:item.id},item.name)));selector.value=state.instanceId;}
}
function renderShell() {
  selector=el('select',{'aria-label':'Selected instance',onchange:()=>{state.instanceId=selector.value;navigate();}});
  const link=id=>el('a',{href:`#${id}`,'data-route':id},icon(id),el('span',{},routes[id]));
  const groups=[['Monitor',['dashboard','errors','observe','fleet-health']],['Orchestrate',['workflows','promote','assistant']],['Manage',['instances','containers','notes','backup','secrets','settings']]];
  const navigation=el('nav',{'aria-label':'All views'},groups.map(([title,ids])=>{
    const labelId=`nav-group-${title.toLowerCase()}`;
    return el('div',{class:'nav-group',role:'group','aria-labelledby':labelId},el('p',{class:'nav-group-title',id:labelId},title),ids.filter(id=>!adminOnly.has(id)||can('admin')).map(link));
  }));
  const more=el('details',{class:'nav-more',open:!mobileNavigation.matches},el('summary',{},icon('menu'),'Explore'),navigation);
  const brand=el('a',{class:'brand',href:'#dashboard'},el('span',{class:'brand-mark','aria-hidden':'true'}),el('span',{},'Inference',el('small',{},'Execution Anomaly System')));
  const sidebar=el('aside',{class:'sidebar'},brand,more,el('div',{class:'sidebar-foot'},el('span',{class:'badge'},'Self-hosted'),el('p',{},'Your infrastructure. Your control.')));
  const theme=el('select',{'aria-label':'Color theme',class:'theme-picker'},el('option',{value:'light'},'Light'),el('option',{value:'dark'},'Dark'),el('option',{value:'n8n'},'n8n inspired'));
  theme.value=state.preferences.theme;
  theme.addEventListener('change',async()=>{
    theme.disabled=true;
    try{const preferences=await api('/api/admin/preferences');preferences.theme=theme.value;const saved=await api('/api/admin/preferences',{method:'PUT',json:preferences});applyPreferences(saved);await setTheme(saved.theme);}
    catch(error){theme.value=state.preferences.theme;toast(error.message);}
    finally{theme.disabled=false;}
  });
  const nav=el('header',{class:'workspace-bar'},el('div',{class:'workspace-context'},el('span',{},'Workspace'),el('strong',{id:'current-view'},'Overview')),el('div',{class:'nav-selector'},selector),el('div',{class:'account'},el('span',{class:'account-name'},state.user.username),theme,button('Sign out',async()=>{await api('/api/auth/logout',{method:'POST'});await authenticate();},'ghost')));
  shell=el('main',{id:'main',class:'page',tabIndex:-1});
  root.replaceChildren(el('div',{class:'app-layout'},sidebar,el('div',{class:'workspace'},nav,shell,el('footer',{class:'footer'},el('span',{},'Inference / Execution Anomaly System'),el('span',{id:'connection-status',role:'status'},'Connecting to updates')))));
}
async function navigate() {
  if(!state.user||!shell)return;
  const current=++generation; cleanup();cleanup=()=>{}; clearPendingUpdates();
  const id=location.hash.slice(1).split('?')[0] || 'dashboard';
  root.querySelectorAll('[data-route]').forEach(node=>{if(node.dataset.route===(id==='codelab'?'assistant':id))node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  root.querySelectorAll('.nav-more').forEach(node=>node.open=!mobileNavigation.matches);
  const currentView=document.querySelector('#current-view');if(currentView)currentView.textContent=routes[id]||'Page not found';
  const container=el('div',{class:'stack'});shell.replaceChildren(container); document.title=`${routes[id] || 'Page not found'} · Inference`;
  if(!routes[id]){container.append(el('h1',{},'Page not found'),el('a',{href:'#dashboard'},'Return to overview'));return;}
  if(adminOnly.has(id)&&!can('admin')){container.append(el('h1',{},'Administrator access required'),el('p',{},'Ask an administrator to manage this part of your workspace.'));return;}
  container.append(el('p',{class:'muted',role:'status'},`Loading ${routes[id].toLowerCase()}…`));
  try {
    const module=await import(`./views/${['assistant','codelab'].includes(id)?'studio':id}.js`);
    if(current!==generation)return;
    container.replaceChildren();
    const dispose=await module.render(container);
    if(current!==generation){if(typeof dispose==='function')dispose();return;}
    cleanup=typeof dispose==='function'?dispose:()=>{};
  } catch(error){if(current===generation){container.replaceChildren();notice(container,error);container.append(button('Try again',navigate,'ghost'));}}
}
async function signedIn(user) {
  cleanup();state.user=user; renderShell();
  try { const prefs=await api('/api/admin/preferences');applyPreferences(prefs);setTheme(prefs.theme);root.querySelector('.theme-picker').value=state.preferences.theme;await loadInstances(); } catch(error){toast(error.message);}
  connectSocket(); await navigate();
}
async function authenticate() {
  if(authPending)return; authPending=true; ++generation;cleanup();closeSocket();state.user=null;applyPreferences();shell=null;setTheme('light');
  try { const status=await api('/api/auth/status'); if(status.user)await signedIn(status.user);else cleanup=renderAuth(root,status.setup_required,signedIn); }
  catch(error){root.replaceChildren(el('main',{id:'main',class:'page'},el('h1',{},'Could not reach your workspace'),el('p',{},error.message),button('Try again',authenticate)));}
  finally {authPending=false;}
}
window.addEventListener('hashchange',navigate);
window.addEventListener('session-expired',()=>{if(state.user)toast('Your session ended. Sign in again.');authenticate();});
window.addEventListener('instances-changed',async()=>{try{await loadInstances();}catch(error){toast(error.message);}});
window.addEventListener('preferences-changed',event=>{applyPreferences(event.detail);setTheme(state.preferences.theme);const picker=root.querySelector('.theme-picker');if(picker)picker.value=state.preferences.theme;});
window.addEventListener('pagehide',()=>{cleanup();closeSocket();});
// Keep the header picker on the theme actually applied, including fallbacks and direct setTheme calls.
new MutationObserver(()=>{const picker=root.querySelector('.theme-picker'),applied=document.documentElement.dataset.theme;if(picker&&['light','dark','n8n'].includes(applied))picker.value=applied;}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
authenticate();
