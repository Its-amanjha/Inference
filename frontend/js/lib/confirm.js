// Action hierarchy helpers: target-naming confirmations and state badges.
import {el,button,field} from './html.js';

let active=null, sequence=0;

export function humanize(value){
  const text=String(value??'').replace(/[_-]+/g,' ').trim();
  return text?text[0].toUpperCase()+text.slice(1):'Unknown';
}

export function stateBadge(kind,label,raw){
  return el('span',{class:kind?`badge ${kind}`:'badge',title:raw===undefined||raw===null?undefined:String(raw)},label);
}


// Asks for one value in the app's dialog instead of a browser prompt. Resolves the trimmed text, or null when cancelled.
export function askText({title,label,description,value='',confirmLabel='Continue',placeholder}={}){
  if(active)return Promise.resolve(null);
  const invoker=document.activeElement instanceof HTMLElement?document.activeElement:null;
  const fallback=invoker?.closest('main, [role="main"], #app, .view-content, .content, section') || invoker?.parentElement || null;
  const id=`ask-${++sequence}`;
  return new Promise(resolve=>{
    let result=null;
    const input=el('input',{type:'text',value,placeholder,autocomplete:'off',spellcheck:'false'});
    const cancel=button('Cancel',()=>finish(null),'ghost');
    const confirm=button(confirmLabel,()=>finish(input.value.trim()),'button');
    const form=el('form',{class:'confirm-form',novalidate:true},
      el('h2',{id:`${id}-title`},title),
      description?el('p',{class:'confirm-consequence',id:`${id}-description`},description):null,
      field(label,input),
      el('div',{class:'confirm-actions'},cancel,confirm));
    const dialog=el('dialog',{class:'confirm-dialog','aria-labelledby':`${id}-title`,'aria-describedby':description?`${id}-description`:undefined},form);
    form.addEventListener('submit',event=>{event.preventDefault();finish(input.value.trim());});
    const onRoute=()=>finish(null);
    function finish(text){result=text;if(dialog.open)dialog.close();}
    dialog.addEventListener('cancel',()=>{result=null;});
    dialog.addEventListener('close',()=>{
      window.removeEventListener('hashchange',onRoute);
      dialog.remove();active=null;
      if(invoker?.isConnected)invoker.focus();
      else if(fallback?.isConnected){if(!fallback.hasAttribute('tabindex'))fallback.setAttribute('tabindex','-1');fallback.focus();}
      resolve(result);
    },{once:true});
    active=dialog;
    window.addEventListener('hashchange',onRoute);
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}

// Resolves true only when the person explicitly confirms. One dialog is open at a time.
export function confirmAction({title,target,consequence,confirmLabel='Confirm',danger=false,requireText}={}){
  if(active)return Promise.resolve(false);
  const invoker=document.activeElement instanceof HTMLElement?document.activeElement:null;
  const fallback=invoker?.closest('main, [role="main"], #app, .view-content, .content, section') || invoker?.parentElement || null;
  const id=`confirm-${++sequence}`;
  return new Promise(resolve=>{
    let result=false;
    const input=requireText?el('input',{type:'text',autocomplete:'off',autocapitalize:'off',spellcheck:'false'}):null;
    const cancel=button('Cancel',()=>finish(false),'ghost');
    const confirm=button(confirmLabel,()=>finish(true),danger?'danger':'button');
    if(input){confirm.disabled=true;input.addEventListener('input',()=>{confirm.disabled=input.value!==requireText;});}
    const targetNodes=typeof target==='string'?el('strong',{},target):target;
    const describedBy=[target!==undefined&&target!==null?`${id}-target`:null,consequence?`${id}-consequence`:null].filter(Boolean).join(' ');
    const form=el('form',{class:'confirm-form',novalidate:true},
      el('h2',{id:`${id}-title`},title),
      targetNodes!==undefined&&targetNodes!==null?el('p',{class:'confirm-target',id:`${id}-target`},targetNodes):null,
      consequence?el('p',{class:'confirm-consequence',id:`${id}-consequence`},consequence):null,
      input?field(`Type ${requireText} to confirm`,input):null,
      el('div',{class:'confirm-actions'},cancel,confirm));
    const dialog=el('dialog',{class:danger?'confirm-dialog is-danger':'confirm-dialog','aria-labelledby':`${id}-title`,'aria-describedby':describedBy||undefined},form);
    form.addEventListener('submit',event=>{event.preventDefault();if(!confirm.disabled)finish(true);});
    const onRoute=()=>finish(false);
    function finish(value){result=value;if(dialog.open)dialog.close();}
    dialog.addEventListener('cancel',()=>{result=false;});
    dialog.addEventListener('close',()=>{
      window.removeEventListener('hashchange',onRoute);
      dialog.remove();active=null;
      if(invoker?.isConnected)invoker.focus();
      else if(fallback?.isConnected){if(!fallback.hasAttribute('tabindex'))fallback.setAttribute('tabindex','-1');fallback.focus();}
      resolve(result);
    },{once:true});
    active=dialog;
    window.addEventListener('hashchange',onRoute);
    document.body.append(dialog);
    dialog.showModal();
    (input||(danger?cancel:confirm)).focus();
  });
}
