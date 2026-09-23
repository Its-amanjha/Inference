import {el,button} from '../lib/html.js';
// One AI workspace window: the assistant conversation and the manual CodeLab editor share the page and keep their local state.
export async function render(container){
  let closed=false;
  const panels={chat:el('section',{class:'stack',id:'studio-chat'}),code:el('section',{class:'stack',id:'studio-code',hidden:true})};
  const loaded={},cleanups=[];
  const initial=location.hash.startsWith('#codelab')||new URLSearchParams(location.hash.split('?')[1]||'').get('tab')==='code'?'code':'chat';
  const controls={};
  async function show(tab){
    for(const [name,panel] of Object.entries(panels)){panel.hidden=name!==tab;controls[name].setAttribute('aria-pressed',String(name===tab));}
    if(!loaded[tab])loaded[tab]=(async()=>{const module=await import(tab==='chat'?'./assistant.js':'./codelab.js');if(closed)return;const dispose=await module.render(panels[tab]);if(closed)dispose?.();else cleanups.push(dispose);})();
    await loaded[tab];
    // The switch sits under the page's own title, so the tab bar never stands above or repeats it.
    const head=panels[tab].querySelector(':scope > .page-heading-row, :scope > .page-heading');
    if(head)head.after(nav);else panels[tab].prepend(nav);
  }
  for(const [tab,label] of [['chat','Conversation'],['code','CodeLab']]){controls[tab]=button(label,()=>show(tab),'ghost');controls[tab].setAttribute('aria-controls',panels[tab].id);}
  const nav=el('nav',{class:'studio-tabs','aria-label':'AI workspace'},controls.chat,controls.code);
  container.append(panels.chat,panels.code);
  await show(initial);
  return()=>{closed=true;for(const dispose of cleanups)dispose?.();};
}
