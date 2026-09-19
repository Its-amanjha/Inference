import {state} from '../state.js';
import {el,button,heading,empty} from '../lib/html.js';
import {rawDisclosure,probeSummary,reachabilityBadge,checkedLine,loadingState,loadFailure,asOf,cachedProbe,checkConnection} from '../lib/payload.js';
export async function render(container) {
  const controller=new AbortController();
  let round=null;
  const content=el('div',{class:'grid'});
  // Do not return the pending promise to button(): Cancel must remain clickable.
  const refresh=button('Check fleet',()=>{if(round)round.abort();else start();},'ghost');
  container.append(heading('Fleet health','Live connection and public API capability checks.'),refresh,content);
  function resultView(result){
    const {list,notes,diagnostics}=probeSummary(result);
    return el('div',{class:'stack'},reachabilityBadge(result),
      el('p',{class:'muted',title:result.checked_at},checkedLine(result)),
      asOf(new Date(result.checked_at)),list,
      diagnostics.length?notes:el('p',{},'Public API checks passed.'),
      rawDisclosure(result));
  }
  const entries=state.instances.map(instance=>{
    const cached=cachedProbe(instance),body=el('div'),feedback=el('div');
    if(cached)body.append(resultView(cached));
    feedback.append(loadingState(`Checking connections on ${instance.name}…`));
    content.append(el('article',{class:'card stack decision-card','aria-label':`Health of ${instance.name}`},
      el('h2',{},instance.name),feedback,body));
    return {instance,body,feedback};
  });
  if(!entries.length){content.append(empty('Connect your first instance','Add an n8n instance to check fleet health.'));refresh.disabled=true;}
  function start(selected=entries){
    if(round||controller.signal.aborted||!selected.length)return;
    const current=new AbortController();round=current;refresh.textContent='Cancel';
    const cancel=()=>current.abort();controller.signal.addEventListener('abort',cancel,{once:true});
    selected.forEach(entry=>entry.feedback.replaceChildren(loadingState(`Checking connections on ${entry.instance.name}…`)));
    let index=0;
    async function worker(){
      while(index<selected.length&&!current.signal.aborted){
        const entry=selected[index++];
        try{
          const result=await checkConnection(entry.instance,{signal:current.signal});
          if(current.signal.aborted||controller.signal.aborted)return;
          entry.body.replaceChildren(resultView(result));entry.feedback.replaceChildren();
        }catch(error){
          if(current.signal.aborted||controller.signal.aborted)return;
          const failure=loadFailure(error,()=>start([entry]),`Could not check ${entry.instance.name}`);
          // The fleet Cancel control stays available; avoid overlapping retry rounds.
          failure.querySelector('button').disabled=true;entry.feedback.replaceChildren(failure);
        }
      }
    }
    Promise.all(Array.from({length:Math.min(3,selected.length)},worker)).finally(()=>{
      controller.signal.removeEventListener('abort',cancel);
      if(controller.signal.aborted)return;
      if(current.signal.aborted)for(const entry of selected){
        if(entry.feedback.querySelector('.loading-state'))entry.feedback.replaceChildren(
          el('p',{class:'load-progress',role:'status'},`Check cancelled for ${entry.instance.name}.${entry.body.childElementCount?' Previous result kept.':''}`));
      }
      content.querySelectorAll('.load-notice button').forEach(node=>node.disabled=false);
      round=null;refresh.textContent='Check fleet';
    });
  }
  start();return()=>{controller.abort();round?.abort();};
}
