import {api} from '../api.js';
import {el,button,heading,empty,notice} from '../lib/html.js';
import {rawDisclosure,probeSummary,reachabilityBadge,checkedLine} from '../lib/payload.js';
export async function render(container) {
  const controller=new AbortController();
  const content=el('div',{className:'grid'});
  const refresh=button('Check fleet',load,'button ghost');
  container.append(heading('Fleet health','Live connection and public API capability checks.'),refresh,content);
  function card(row){
    const {list,notes,diagnostics}=probeSummary(row);
    return el('article',{className:'card stack decision-card'},
      el('div',{className:'decision-head'},el('h2',{},row.name),reachabilityBadge(row)),
      el('p',{className:'muted'},checkedLine(row)),
      list,
      diagnostics.length?notes:el('p',{},'Public API checks passed.'),
      rawDisclosure(row));
  }
  async function load(){
    refresh.disabled=true;content.replaceChildren(el('p',{className:'muted',role:'status'},'Checking connections…'));
    try {const result=await api('/api/n8n_proxy/health',{signal:controller.signal});
      content.replaceChildren(...result.data.map(card));
      if(!result.data.length)content.append(empty('Connect your first instance','Add an n8n instance to check fleet health.'));
    }catch(error){if(!controller.signal.aborted)notice(content,error);}finally{refresh.disabled=false;}
  }
  await load();return()=>controller.abort();
}
