// A side panel for one record's evidence, opened from a list row without leaving the list. It is a
// modal dialog: focus moves in, Escape or Close shuts it, and focus returns to the row that opened it.
// On a phone it fills the screen.
import {el} from './html.js';
import {icon} from './icons.js';

let current=null;

// sections: nodes rendered in order; actions: nodes for the footer (the first one is the primary).
export function openPeek({title,eyebrow,meta,sections=[],actions=[],closeLabel='Close details',className=''}={}){
  current?.close();
  const invoker=document.activeElement;
  const heading=el('h2',{class:'peek-title',id:'peek-title'},title);
  const close=el('button',{type:'button',class:'peek-close','aria-label':closeLabel,title:closeLabel},icon('close'));
  const status=el('p',{class:'sr-only peek-status',role:'status'});
  const dialog=el('dialog',{class:`peek${className?` ${className}`:''}`,'aria-labelledby':'peek-title'},status,
    el('header',{class:'peek-head'},
      el('div',{class:'peek-heading'},eyebrow?el('div',{class:'peek-eyebrow'},eyebrow):null,heading,meta?el('p',{class:'peek-meta'},meta):null),
      close),
    el('div',{class:'peek-body'},sections.filter(Boolean)),
    actions.filter(Boolean).length?el('footer',{class:'peek-foot'},actions.filter(Boolean)):null);
  close.addEventListener('click',()=>dialog.close());
  // Page shortcuts (j, k, e…) act on the list behind the panel, so keys typed in the panel stay in it.
  // Escape still closes it: that is the dialog's own close request, not a bubbling key event.
  dialog.addEventListener('keydown',event=>event.stopPropagation());
  // A click on the backdrop (outside the panel box) closes it too.
  dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const box=dialog.getBoundingClientRect();
    if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();});
  // A link in the panel that changes the page closes the panel with it.
  const onRoute=()=>dialog.close();
  addEventListener('hashchange',onRoute);
  dialog.addEventListener('close',()=>{removeEventListener('hashchange',onRoute);dialog.remove();if(current===dialog)current=null;if(invoker?.isConnected)invoker.focus();});
  document.body.append(dialog);
  current=dialog;
  dialog.showModal();
  heading.setAttribute('tabindex','-1');heading.focus();
  return dialog;
}

// A labelled fact list for the panel body: [[label, value], ...]; empty values are left out.
export function peekFacts(rows){
  const shown=rows.filter(([,value])=>value!==null&&value!==undefined&&value!=='');
  return shown.length?el('dl',{class:'peek-facts'},shown.map(([label,value])=>el('div',{},el('dt',{},label),el('dd',{},value)))):null;
}

export function peekSection(title,...children){
  return el('section',{class:'peek-section'},el('h3',{},title),...children);
}

// An identifier with a copy button; the result is announced inside the panel.
export function peekCopy(value,what){
  const text=String(value);
  const copy=el('button',{type:'button',class:'peek-copy','aria-label':`Copy ${what}`,title:`Copy ${what}`},icon('copy'));
  copy.addEventListener('click',async()=>{
    const status=copy.closest('dialog')?.querySelector('.peek-status');
    try{await navigator.clipboard.writeText(text);copy.dataset.copied='';if(status)status.textContent=`Copied ${what}.`;setTimeout(()=>delete copy.dataset.copied,1500);}
    catch{if(status)status.textContent='Copying is not available in this browser.';}
  });
  return el('span',{class:'peek-id'},el('code',{},text),copy);
}

export const closePeek=()=>current?.close();
