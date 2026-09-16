import {api} from './api.js';
import {el,field,notice} from './lib/html.js';
import {icon} from './lib/icons.js';

export function renderAuth(root, setup, onSuccess) {
  const controller = new AbortController();
  const username = el('input',{name:'username',autocomplete:'username',required:true,maxLength:64,pattern:'[A-Za-z0-9_.-]+'});
  const password = el('input',{name:'password',type:'password',autocomplete:setup?'new-password':'current-password',required:true,minLength:setup?14:1,maxLength:256});
  const code = el('input',{name:'code',autocomplete:'one-time-code',maxLength:64});
  const token = el('input',{name:'bootstrap',type:'password',autocomplete:'off'});
  const confirmation = el('input',{name:'confirmation',type:'password',autocomplete:'new-password',required:true,minLength:14});
  const submit = el('button',{type:'submit',class:'button'},setup?'Create admin account':'Sign in');
  const errorArea = el('div',{'aria-live':'polite'});
  const form = el('form',{class:'form'},field('Username',username),field('Password',password,setup?{hint:'Use at least 14 characters.'}:{}),
    setup ? [field('Confirm password',confirmation),
      el('details',{class:'form-disclosure'},el('summary',{},icon('chevron'),'Have a setup token?'),el('div',{},field('Setup token',token)))] : field('Verification or recovery code (if enabled)',code),
    errorArea,submit);
  form.addEventListener('submit', async event=>{
    event.preventDefault(); if (submit.disabled) return; errorArea.replaceChildren();
    if (setup && password.value !== confirmation.value) { notice(errorArea,new Error('Passwords do not match.')); confirmation.focus(); return; }
    submit.disabled=true;
    try {
      const headers = setup && token.value ? {'X-Inference-Bootstrap':token.value} : {};
      await api(`/api/auth/${setup?'setup':'login'}`,{method:'POST',headers,json:{username:username.value,password:password.value,code:setup?'':code.value},signal:controller.signal});
      password.value=''; confirmation.value=''; token.value=''; code.value='';
      const status = await api('/api/auth/status',{signal:controller.signal});
      if (!status.user) throw new Error('Your browser did not retain the sign-in cookie. Use HTTPS, or configure HTTP cookies for local development.');
      onSuccess(status.user);
    } catch(error) {
      if (error.name !== 'AbortError') notice(errorArea,error);
      if (error.code === 'already_initialized') window.dispatchEvent(new Event('session-expired'));
    } finally { submit.disabled=false; }
  });
  root.replaceChildren(el('main',{id:'main',class:'auth-layout'},
    el('section',{class:'auth-intro'},el('a',{class:'brand',href:'/'},el('span',{class:'brand-mark','aria-hidden':'true'}),'Inference'),
      el('h1',{},'Execution',el('br'), 'Anomaly System'),
      el('div',{class:'auth-rule'}),el('p',{},'A little clarity. Across your fleet.')),
    el('section',{class:'card auth-card'},
      el('h2',{},setup?'Create your workspace':'Sign in to Inference'),el('p',{class:'muted'},setup?'Set up your administrator account.':'Enter your credentials to continue.'),form)));
  username.focus();
  return ()=>controller.abort();
}
