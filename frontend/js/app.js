import {api} from './api.js';
import {el,button,notice} from './lib/html.js';
import {state,can,applyPreferences,isOwnEvent} from './state.js';
import {renderAuth} from './wizard.js';
import {setTheme} from './themes.js';
import {icon} from './lib/icons.js';
import {openPalette} from './lib/palette.js';
import {selectMenu} from './lib/select-menu.js';
import {cachedProbe} from './lib/payload.js';
import {closePeek,openPeek,peekSection} from './lib/peek.js';

const routes = {'dashboard':'Overview','workflows':'Workflows','promote':'Promote','errors':'Errors','observe':'Observability','codelab':'CodeLab','assistant':'AI workspace','containers':'Containers','notes':'Notes','backup':'Backups','secrets':'Secrets','instances':'Instances','settings':'Settings'};
const adminOnly = new Set(['containers','backup','secrets']);
// Pages whose every action changes something: viewers do not see them.
const operatorOnly = new Set(['promote']);
const root=document.querySelector('#app');
const mobileNavigation=window.matchMedia('(max-width:700px)');
mobileNavigation.addEventListener('change',()=>{const menu=root.querySelector('.nav-more');if(menu)menu.open=!mobileNavigation.matches;});
document.querySelector('.skip-link')?.addEventListener('click',event=>{event.preventDefault();const main=document.querySelector('#main');if(main){main.tabIndex=-1;main.focus();main.scrollIntoView({block:'start'});}});
let cleanup=()=>{}, generation=0, socket=null, reconnect=null, shell=null, selector=null, authPending=false;
let reconnectDelay=1000, pendingBar=null, pendingCount=0;
let currentViewShortcuts=[], currentViewTitle='', pendingSequence=null, pendingTimer=null;

// Toasts: one per distinct message (repeats raise a count), dismissible, paused while hovered or focused.
const TOAST_MS=8000, TOAST_LIMIT=3, toasts=new Map();
// Phone screens reserve the stack's height so the last controls stay reachable underneath it (see live.css).
function measureToasts() {
  const area=document.querySelector('#toasts');
  if(!area)return;
  const height=area.querySelector('.toast')?Math.ceil(area.getBoundingClientRect().height)+24:0;
  document.documentElement.style.setProperty('--toast-reserve',`${height}px`);
}
function removeToast(record) {
  clearTimeout(record.timer);
  if(toasts.get(record.message)===record)toasts.delete(record.message);
  if(record.node.contains(document.activeElement)){const main=document.querySelector('#main');if(main){main.tabIndex=-1;main.focus({preventScroll:true});}}
  record.node.remove();
  measureToasts();
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
  toasts.set(text,record); area.append(node); scheduleToast(record); measureToasts();
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
  const connection=(phase,message)=>{const node=document.querySelector('#connection-status');if(!node)return;node.dataset.state=phase;node.title=message;const label=node.querySelector('.live-label');if(label)label.textContent=message;};
  connection('connecting','Connecting to updates');
  socket.onopen=()=>{reconnectDelay=1000;connection('live','Live updates connected');};
  socket.onmessage=event=>{
    let packet; try{packet=JSON.parse(event.data);}catch{return;}
    if(!packet.event || ['connected','heartbeat'].includes(packet.event))return;
    const labels={'error:reported':'An execution error was reported.','silent_failure:detected':'A silent failure was detected.','workflow:promoted':'A workflow promotion changed.','langgraph:run':'An assistant run changed.','errors.changed':'Execution errors updated.','backups.changed':'Backup history updated.','notes.changed':'Notes updated.'};
    const errorEvent=['error:reported','silent_failure:detected','errors.changed'].includes(packet.event);
    const notify=!errorEvent || state.preferences.error_notifications;
    // A note this tab just saved or deleted is already confirmed on screen.
    const own=packet.event==='notes.changed'&&isOwnEvent('notes.changed');
    if(labels[packet.event] && notify && !own)toast(labels[packet.event]);
    window.dispatchEvent(new CustomEvent('inference-event',{detail:packet}));
    const route=location.hash.slice(1).split('?')[0] || 'dashboard';
    if(['dashboard','errors','observe'].includes(route) && notify)showPendingUpdates();
  };
  socket.onclose=event=>{
    connection('reconnecting','Reconnecting to updates');
    if([4401,4403].includes(event.code)){window.dispatchEvent(new Event('session-expired'));return;}
    if(state.user){reconnect=setTimeout(connectSocket,reconnectDelay);reconnectDelay=Math.min(30000,reconnectDelay*2);}
  };
}
async function loadInstances() {
  const result=await api('/api/n8n_proxy/instances'); state.instances=result.data || [];
  if(state.instanceId&&!state.instances.some(item=>item.id===state.instanceId)) state.instanceId='';
  paintSelector();
}
// The switcher lists every instance with its last known reachability (green, red, or grey when not
// checked yet) and its host, so the choice shows where it points.
function paintSelector(){
  if(!selector)return;
  const host=url=>{try{return new URL(url).host;}catch{return url||'';}};
  selector.setOptions([{value:'',label:'All instances',glyph:'fleet-health',detail:`${state.instances.length} connected`},
    ...state.instances.map(item=>{const probe=cachedProbe(item);return {value:item.id,label:item.name,detail:host(item.url),dot:probe?(probe.reachable?'up':'down'):'unknown'};})],state.instanceId);
}
const THEMES=[['light','Light'],['dark','Dark'],['n8n','n8n inspired']];
const ROLE_LABELS={admin:'Administrator',operator:'Operator',viewer:'Viewer'};
function initials(name){const parts=String(name||'').split(/[\s._-]+/).filter(Boolean);return ((parts[0]?.[0]||'?')+(parts[1]?.[0]||'')).toUpperCase();}
function syncThemeChoice(theme){root.querySelectorAll('input[name=color-theme]').forEach(input=>{input.checked=input.value===theme;});}
// Account disclosure: who is signed in, the theme choice and sign out. Closes on Escape, outside clicks and links.
function accountMenu(){
  const trigger=el('button',{type:'button',class:'account-trigger','aria-label':'Account menu','aria-expanded':'false','aria-controls':'account-panel'},el('span',{class:'avatar','aria-hidden':'true'},initials(state.user.username)));
  const themes=el('fieldset',{class:'theme-choice'},el('legend',{},'Theme'),el('div',{class:'theme-options'},THEMES.map(([value,label])=>el('label',{class:'theme-option'},el('input',{type:'radio',name:'color-theme',value,checked:state.preferences.theme===value}),el('span',{},label)))));
  const signOut=el('button',{type:'button',class:'account-item'},icon('logout'),'Sign out');
  const panel=el('div',{class:'account-panel',id:'account-panel',hidden:true},
    el('div',{class:'account-who'},el('span',{class:'avatar','aria-hidden':'true'},initials(state.user.username)),el('div',{},el('strong',{},state.user.username),el('span',{},ROLE_LABELS[state.user.role]||state.user.role))),
    themes,el('div',{class:'account-actions'},el('a',{class:'account-item',href:'#settings'},icon('settings'),'Settings'),signOut));
  const menu=el('div',{class:'account-menu'},trigger,panel);
  const outside=event=>{if(!menu.contains(event.target))setOpen(false);};
  function setOpen(open){panel.hidden=!open;trigger.setAttribute('aria-expanded',String(open));document[open?'addEventListener':'removeEventListener']('pointerdown',outside,true);}
  trigger.addEventListener('click',()=>setOpen(panel.hidden));
  menu.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden){event.stopPropagation();setOpen(false);trigger.focus();}});
  panel.addEventListener('click',event=>{if(event.target.closest('a'))setOpen(false);});
  themes.addEventListener('change',async event=>{
    try{const preferences=await api('/api/admin/preferences');preferences.theme=event.target.value;const saved=await api('/api/admin/preferences',{method:'PUT',json:preferences});applyPreferences(saved);await setTheme(saved.theme);}
    catch(error){syncThemeChoice(state.preferences.theme);toast(error.message);}
  });
  signOut.addEventListener('click',async()=>{setOpen(false);try{await api('/api/auth/logout',{method:'POST'});}catch(error){toast(error.message);return;}await authenticate();});
  return menu;
}
function renderShell() {
  selector=selectMenu({label:'Selected instance',className:'scope-button',onChange:value=>{state.instanceId=value;navigate();}});
  paintSelector();
  const link=id=>el('a',{href:`#${id}`,'data-route':id},icon(id),el('span',{},routes[id]));
  const groups=[['Monitor',['dashboard','errors','observe']],['Orchestrate',['workflows','promote','assistant']],['Manage',['instances','containers','notes','backup','secrets','settings']]];
  const navigation=el('nav',{'aria-label':'All views'},groups.map(([title,ids])=>{
    const labelId=`nav-group-${title.toLowerCase()}`;
    return el('div',{class:'nav-group',role:'group','aria-labelledby':labelId},el('p',{class:'nav-group-title',id:labelId},title),ids.filter(id=>(!adminOnly.has(id)||can('admin'))&&(!operatorOnly.has(id)||can('operator'))).map(link));
  }));
  const more=el('details',{class:'nav-more',open:!mobileNavigation.matches},el('summary',{},icon('menu'),'Menu'),navigation);
  const brand=el('a',{class:'brand',href:'#dashboard',title:'Inference · Execution Anomaly System'},el('span',{class:'brand-mark','aria-hidden':'true'}),el('span',{class:'brand-name'},'Inference'));
  const sidebar=el('aside',{class:'sidebar'},el('div',{class:'sidebar-head'},brand),more);
  // Scope first, then the current view: the same breadcrumb shape as project switchers in hosted consoles.
  const scope=el('div',{class:'scope-switcher'},selector.node);
  // A clock replaces the connected label; the state text returns only when updates stop. The clock sits
  // outside the live region so screen readers hear connection changes, not every minute.
  const clock=el('time',{class:'live-clock'});
  const tick=()=>{const now=new Date();clock.dateTime=now.toISOString();
    clock.replaceChildren(el('span',{class:'live-clock-date'},now.toLocaleDateString([],{weekday:'short',day:'numeric',month:'short'})),
      el('span',{class:'live-clock-time'},now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})));};
  tick();clearInterval(window.__inferenceClock);window.__inferenceClock=setInterval(tick,15000);
  const status=el('span',{id:'connection-status',class:'live-status','data-state':'connecting',title:'Connecting to updates'},el('span',{class:'live-dot','aria-hidden':'true'}),clock,el('span',{class:'live-label',role:'status'},'Connecting to updates'));
  const topbar=el('header',{class:'topbar'},el('div',{class:'topbar-context'},scope,el('span',{class:'crumb-separator','aria-hidden':'true'},'/'),el('span',{class:'crumb-current',id:'current-view'},'Overview')),el('div',{class:'topbar-actions'},status,accountMenu()));
  shell=el('main',{id:'main',class:'page',tabIndex:-1});
  root.replaceChildren(el('div',{class:'app-layout'},sidebar,el('div',{class:'workspace'},topbar,shell)));
}

export function isTyping(e) {
  const target = e?.target;
  if (!target) return false;
  const tag = target.tagName?.toLowerCase();
  if (tag === 'textarea' || target.isContentEditable) return true;
  if (tag === 'input') {
    const type = target.type?.toLowerCase();
    return type !== 'checkbox' && type !== 'radio' && type !== 'button' && type !== 'submit' && type !== 'reset';
  }
  if (tag === 'select') return true;
  return false;
}

export function registerShortcuts(shortcuts, title = '') {
  const items = Array.isArray(shortcuts) ? shortcuts : [shortcuts];
  const registered = items.map(item => {
    const keys = Array.isArray(item.key) ? item.key : [item.key];
    return {
      keys: keys.map(k => String(k)),
      display: item.display || keys.join(' / '),
      description: item.description || '',
      handler: item.handler
    };
  });
  currentViewShortcuts.push(...registered);
  if (title) currentViewTitle = title;
  return () => {
    currentViewShortcuts = currentViewShortcuts.filter(s => !registered.includes(s));
    if (currentViewShortcuts.length === 0) currentViewTitle = '';
  };
}
window.registerShortcuts = registerShortcuts;

function formatKeyNodes(displayStr) {
  const parts = String(displayStr).split(' ');
  const nodes = [];
  parts.forEach((p, idx) => {
    if (idx > 0) nodes.push(document.createTextNode(' '));
    if (p === '/' && parts.length > 1) {
      nodes.push(el('span', {}, '/'));
    } else {
      nodes.push(el('kbd', {}, p));
    }
  });
  return nodes;
}

const GLOBAL_SHORTCUTS = [
  { display: '?', description: 'Show this list' },
  { display: '/', description: 'Search or filter this page' },
  { display: 'Ctrl+K', description: 'Jump to a page, workflow or instance' },
  { display: 'g d', description: 'Go to Overview' },
  { display: 'g e', description: 'Go to Errors' },
  { display: 'g o', description: 'Go to Observability' },
  { display: 'g w', description: 'Go to Workflows' },
  { display: 'g a', description: 'Go to AI workspace' },
  { display: 'g c', description: 'Go to CodeLab' },
  { display: 'g i', description: 'Go to Instances' },
  { display: 'g n', description: 'Go to Notes' },
  { display: 'g b', description: 'Go to Backups' },
  { display: 'g s', description: 'Go to Settings' }
];
export function openShortcutsDialog() {
  if (document.querySelector('dialog.shortcuts-panel[open]')) return;
  const list = rows => el('dl', { class: 'shortcuts-list' }, rows.map(s =>
    el('div', { class: 'shortcut-row' }, el('dt', {}, ...formatKeyNodes(s.display)), el('dd', {}, s.description))));
  // A key this page gives its own meaning (such as /) is listed once, under the page.
  const pageKeys = new Set(currentViewShortcuts.map(s => s.display));
  openPeek({ eyebrow: 'Help', title: 'Keyboard shortcuts', closeLabel: 'Close keyboard shortcuts', className: 'shortcuts-panel',
    sections: [
      currentViewShortcuts.length ? peekSection(currentViewTitle || 'This page', list(currentViewShortcuts)) : null,
      peekSection('Anywhere', list(GLOBAL_SHORTCUTS.filter(s => !pageKeys.has(s.display)))),
    ] });
}
window.openShortcutsDialog = openShortcutsDialog;
// A page can select an instance the way the switcher does, such as CodeLab offering the instances.
addEventListener('choose-instance', event => {
  const id = String(event.detail || '');
  if (!state.instances.some(item => item.id === id)) return;
  state.instanceId = id; if (selector) selector.value = id; navigate();
});

function matchShortcutKey(shortcut, eventKey) {
  const normEvent = String(eventKey).toLowerCase();
  return shortcut.keys.some(k => {
    const normK = String(k).toLowerCase();
    return normK === normEvent || k === eventKey;
  });
}

function onKeyDown(e) {
  if (e.defaultPrevented) return;
  if ((e.ctrlKey || e.metaKey) && !e.altKey && String(e.key).toLowerCase() === 'k') {
    if (isTyping(e)) return;
    if (document.querySelector('dialog[open]')) return;
    e.preventDefault();
    openPalette();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (document.querySelector('dialog[open]')) return;
  if (document.querySelector('.palette-overlay')) return;
  if (isTyping(e)) return;

  if (pendingSequence === 'g') {
    clearTimeout(pendingTimer);
    pendingSequence = null;
    const navTargets = {
      'd': 'dashboard',
      'e': 'errors',
      'w': 'workflows',
      'o': 'observe',
      'a': 'assistant',
      's': 'settings',
      'i': 'instances',
      'n': 'notes',
      'b': 'backup',
      'c': 'codelab',
      'f': 'fleet-health'
    };
    const dest = navTargets[e.key.toLowerCase()];
    if (dest) {
      e.preventDefault();
      location.hash = `#${dest}`;
      return;
    }
  }

  if (e.key === '?' || (e.key === '/' && e.shiftKey)) {
    e.preventDefault();
    openShortcutsDialog();
    return;
  }

  const viewMatch = currentViewShortcuts.find(s => matchShortcutKey(s, e.key));
  if (viewMatch) {
    e.preventDefault();
    viewMatch.handler(e);
    return;
  }

  if (e.key === 'g' || e.key === 'G') {
    e.preventDefault();
    pendingSequence = 'g';
    pendingTimer = setTimeout(() => { pendingSequence = null; }, 1200);
    return;
  }

  if (e.key === '/') {
    e.preventDefault();
    const searchOrFilter = document.querySelector('#main input[type="search"], #main input[type="text"], .scope-switcher button');
    searchOrFilter?.focus();
    return;
  }
}

async function navigate() {
  if(!state.user||!shell)return;
  const current=++generation; closePeek(); cleanup();cleanup=()=>{}; clearPendingUpdates();
  currentViewShortcuts = [];
  currentViewTitle = '';
  pendingSequence = null;
  clearTimeout(pendingTimer);
  let id=location.hash.slice(1).split('?')[0] || 'dashboard';
  // Fleet health merged into the Instances fleet page: old links and "g f" land there under its own
  // name, so the title, breadcrumb and heading never disagree with the page.
  if(id==='fleet-health'){const query=location.hash.split('?')[1];history.replaceState(null,'',`#instances${query?`?${query}`:''}`);id='instances';}
  const navId=id==='codelab'?'assistant':id;
  root.querySelectorAll('[data-route]').forEach(node=>{if(node.dataset.route===navId)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  root.querySelectorAll('.nav-more').forEach(node=>node.open=!mobileNavigation.matches);
  paintSelector();
  const currentView=document.querySelector('#current-view');if(currentView)currentView.textContent=routes[id]||'Page not found';
  const container=el('div',{class:'stack'});shell.replaceChildren(container); document.title=`${routes[id] || 'Page not found'} · Inference`;
  if(!routes[id]){container.append(el('h1',{},'Page not found'),el('a',{href:'#dashboard'},'Return to overview'));return;}
  if(adminOnly.has(id)&&!can('admin')){container.append(el('h1',{},'Administrator access required'),el('p',{},'Ask an administrator to manage this part of your workspace.'));return;}
  if(operatorOnly.has(id)&&!can('operator')){container.append(el('h1',{},'Operator access required'),el('p',{},'Your account can read the fleet. Ask an administrator for the operator role to promote workflows.'));return;}
  container.append(el('p',{class:'muted',role:'status'},`Loading ${routes[id].toLowerCase()}…`));
  try {
    const moduleId=['assistant','codelab'].includes(id)?'studio':id;
    const module=await import(`./views/${moduleId}.js`);
    if(current!==generation)return;
    container.replaceChildren();
    const dispose=await module.render(container);
    if(current!==generation){if(typeof dispose==='function')dispose();return;}
    cleanup=typeof dispose==='function'?dispose:()=>{};
  } catch(error){if(current===generation){container.replaceChildren();notice(container,error);container.append(button('Try again',navigate,'ghost'));}}
}
async function signedIn(user) {
  cleanup();state.user=user; renderShell();
  try { const prefs=await api('/api/admin/preferences');applyPreferences(prefs);setTheme(prefs.theme);syncThemeChoice(state.preferences.theme);await loadInstances(); } catch(error){toast(error.message);}
  connectSocket(); await navigate();
}
async function authenticate() {
  if(authPending)return; authPending=true; ++generation;closePeek();cleanup();closeSocket();state.user=null;applyPreferences();shell=null;setTheme('light');
  try { const status=await api('/api/auth/status'); if(status.user)await signedIn(status.user);else cleanup=renderAuth(root,status.setup_required,signedIn); }
  catch(error){root.replaceChildren(el('main',{id:'main',class:'page'},el('h1',{},'Could not reach your workspace'),el('p',{},error.message),button('Try again',authenticate)));}
  finally {authPending=false;}
}
window.addEventListener('hashchange',navigate);
window.addEventListener('session-expired',()=>{if(state.user)toast('Your session ended. Sign in again.');authenticate();});
window.addEventListener('instances-changed',async()=>{try{await loadInstances();}catch(error){toast(error.message);}});
window.addEventListener('preferences-changed',event=>{applyPreferences(event.detail);setTheme(state.preferences.theme);syncThemeChoice(state.preferences.theme);});
window.addEventListener('pagehide',()=>{cleanup();closeSocket();});
window.addEventListener('keydown', onKeyDown);
// Keep the account menu's theme choice on the theme actually applied, including fallbacks and direct setTheme calls.
new MutationObserver(()=>{const applied=document.documentElement.dataset.theme;if(['light','dark','n8n'].includes(applied))syncThemeChoice(applied);}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
authenticate();
