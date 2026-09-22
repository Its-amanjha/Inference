import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,field,heading,table,notice,empty,jsonView} from '../lib/html.js';
import {confirmAction,stateBadge} from '../lib/confirm.js';
import {repairHref,relativeTime} from '../lib/triage.js';
import {loadingState,loadFailure,asOf} from '../lib/payload.js';
import {icon} from '../lib/icons.js';
export async function render(container) {
  const controller=new AbortController(), signal=controller.signal;
  const instances=state.instances.filter(item=>!state.instanceId||item.id===state.instanceId);
  container.append(heading('Workflows','Search your fleet. Every action stays tied to the instance shown on its section.'));
  if(!instances.length){container.append(empty('Connect an instance','Add an n8n instance to browse its workflows.',[el('a',{class:'button ghost',href:'#instances'},'Open instances')]));return()=>controller.abort();}
  const input=el('input',{type:'search',placeholder:'Workflow name',maxLength:120,autocomplete:'off',role:'combobox','aria-autocomplete':'list','aria-expanded':'false','aria-controls':'workflow-suggestions'}),sections=el('div',{class:'stack'});
  const suggestions=el('ul',{id:'workflow-suggestions',class:'picker-list',role:'listbox','aria-label':'Workflow suggestions',hidden:true});
  const suggestionStatus=el('span',{class:'sr-only',role:'status'});
  let suggestionTimer,suggestionRequest,suggestionRows=[],activeSuggestion=-1,suggestionVersion=0;
  function closeSuggestions(){
    clearTimeout(suggestionTimer);suggestionRequest?.abort();suggestionVersion++;
    suggestions.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');activeSuggestion=-1;
  }
  function pickSuggestion(index){
    const row=suggestionRows[index];if(!row)return;
    input.value=row.name;closeSuggestions();searchFleet();
  }
  function highlightSuggestion(){
    [...suggestions.children].forEach((node,index)=>node.setAttribute('aria-selected',String(index===activeSuggestion)));
    const selected=suggestions.children[activeSuggestion];
    if(selected){input.setAttribute('aria-activedescendant',selected.id);selected.scrollIntoView({block:'nearest'});}
  }
  async function suggest(){
    const query=input.value.trim();if(query.length<2||signal.aborted)return;
    const version=++suggestionVersion,request=new AbortController();suggestionRequest=request;
    let next=0,failed=0;const groups=Array(instances.length);
    suggestionStatus.textContent='Finding workflow suggestions.';
    await Promise.all(Array.from({length:Math.min(3,instances.length)},async()=>{
      while(next<instances.length&&!request.signal.aborted){
        const index=next++,instance=instances[index];
        try{
          const result=await api('/api/n8n_proxy/workflows?'+new URLSearchParams({instance_id:instance.id,name:query,limit:'5'}),{signal:request.signal});
          groups[index]=(result.data||[]).map(row=>({...row,name:row.name||row.id,instance}));
        }catch(error){if(error.name!=='AbortError')failed++;}
      }
    }));
    if(request.signal.aborted||signal.aborted||version!==suggestionVersion||document.activeElement!==input)return;
    suggestionRows=groups.flat().filter(Boolean).slice(0,10);activeSuggestion=-1;
    suggestions.replaceChildren(...suggestionRows.map((row,index)=>{
      const node=el('li',{class:'picker-option',role:'option',id:`workflow-suggestion-${index}`,'aria-selected':'false'},icon('workflows'),el('span',{},el('span',{class:'picker-name'},row.name),el('span',{class:'picker-id'},`${row.instance.name} · ${row.id}`)));
      node.addEventListener('pointerdown',event=>event.preventDefault());node.addEventListener('click',()=>pickSuggestion(index));return node;
    }));
    suggestions.hidden=!suggestionRows.length;input.setAttribute('aria-expanded',String(!suggestions.hidden));
    suggestionStatus.textContent=`${suggestionRows.length} suggestions.${failed?' Some instances could not be searched.':''} Press Search for full results.`;
  }
  function queueSuggestions(){closeSuggestions();suggestionTimer=setTimeout(suggest,300);}
  input.addEventListener('input',queueSuggestions,{signal});
  input.addEventListener('focus',queueSuggestions,{signal});
  input.addEventListener('blur',closeSuggestions,{signal});
  input.addEventListener('keydown',event=>{
    if(event.key==='Escape'){if(!suggestions.hidden){event.preventDefault();event.stopPropagation();}closeSuggestions();}
    else if(!suggestions.hidden&&['ArrowDown','ArrowUp'].includes(event.key)){
      event.preventDefault();activeSuggestion=(activeSuggestion+(event.key==='ArrowDown'?1:-1)+suggestionRows.length)%suggestionRows.length;highlightSuggestion();
    }else if(event.key==='Enter'&&!suggestions.hidden&&activeSuggestion>=0){event.preventDefault();pickSuggestion(activeSuggestion);}
  },{signal});
  signal.addEventListener('abort',closeSuggestions,{once:true});
  let searching=false, term='';
  const search=button('Search',searchFleet);
  container.append(el('form',{class:'toolbar workflow-search',onsubmit:event=>{event.preventDefault();searchFleet();}},el('div',{class:'workflow-search-field'},field('Find workflows',input),suggestions,suggestionStatus),search),sections);
  const entries=instances.map(instance=>{
    const list=el('div'),detail=el('section',{class:'stack',hidden:true}),messages=el('div'),stamp=el('div');
    messages.append(loadingState(`Waiting to load workflows on ${instance.name}…`));
    const entry={instance,list,detail,messages,stamp,cursor:null,loading:false,loaded:false};
    entry.next=button('Next page',()=>load(entry,entry.cursor),'ghost');
    entry.next.setAttribute('aria-label',`Next workflow page on ${instance.name}`);
    entry.retry=button('Refresh instance',()=>load(entry),'ghost',{'aria-label':`Refresh workflows on ${instance.name}`});
    sections.append(el('section',{class:'card stack','aria-label':`Workflows on ${instance.name}`},el('h2',{},instance.name),messages,stamp,list,el('div',{class:'toolbar'},entry.retry,entry.next),detail));
    return entry;
  });
  async function load(entry,after=null){
    if(signal.aborted||entry.loading)return;
    entry.loading=true;entry.next.disabled=entry.retry.disabled=true;
    entry.messages.replaceChildren(entry.loaded?el('p',{class:'load-progress',role:'status'},`Refreshing workflows on ${entry.instance.name}…`):loadingState(`Loading workflows on ${entry.instance.name}…`));
    const {instance,list,detail}=entry;
    try {
      const params=new URLSearchParams({instance_id:instance.id,limit:String(pageSize())});
      if(term)params.set('name',term);if(after)params.set('cursor',after);
      const result=await api('/api/n8n_proxy/workflows?'+params,{signal});
      if(signal.aborted)return;
      entry.cursor=result.next_cursor;
      entry.messages.replaceChildren();entry.stamp.replaceChildren(asOf());entry.loaded=true;
      list.replaceChildren(result.data.length?table(['Workflow','Tags','Updated','State','Actions'],result.data.map(row=>{
        const id=encodeURIComponent(row.id),query='?instance_id='+encodeURIComponent(instance.id),name=row.name??row.id;
        const actions=el('div',{class:'toolbar row-actions'},button('Inspect',async()=>{
          const item=await api(`/api/n8n_proxy/workflows/${id}${query}`,{signal});
          detail.hidden=false;detail.replaceChildren(el('h3',{},row.name),jsonView(item));
        },'ghost',{'aria-label':`Inspect workflow ${name}`}),el('a',{class:'button ghost',href:'#promote?'+new URLSearchParams({workflow_id:row.id,source_instance_id:instance.id}),'aria-label':`Promote workflow ${name}`},'Promote'),el('a',{class:'button ghost',href:'#codelab?'+new URLSearchParams({workflow_id:row.id,instance_id:instance.id}),'aria-label':`Open workflow ${name} in CodeLab`},'CodeLab'),
          can('operator')?el('a',{class:'button ghost',href:repairHref({instance_id:instance.id,workflow_id:row.id}),'aria-label':`Repair workflow ${name} with AI`},'Repair with AI'):null);
        const badge=typeof row.active==='boolean'?stateBadge(row.active?'success':'',row.active?'Active':'Inactive'):stateBadge('warning','Unknown',String(row.active));
        const toggle=button(row.active?'Deactivate':'Activate',async()=>{
          const target=[el('strong',{},name),' on ',el('strong',{},instance.name)];
          const ok=await confirmAction(row.active
            ?{title:'Deactivate workflow',target,consequence:'Its triggers stop, so it no longer runs automatically on this instance until it is activated again.',confirmLabel:'Deactivate workflow'}
            :{title:'Activate workflow',target,consequence:'Its triggers start on this instance, so it can run automatically.',confirmLabel:'Activate workflow'});
          if(!ok)return;
          toggle.disabled=true;
          try {await api(`/api/n8n_proxy/workflows/${id}/${row.active?'deactivate':'activate'}${query}`,{method:'POST'});await load(entry);}
          catch(error){notice(entry.messages,error);}
          finally{toggle.disabled=!can('operator');}
        },'ghost',{'aria-label':`${row.active?'Deactivate':'Activate'} workflow ${name} on ${instance.name}`});toggle.disabled=!can('operator');
        return [
          { sortValue: row.name ?? row.id, content: name },
          (row.tags??[]).map(tag=>tag.name).join(', ')||'—',
          // Relative, with the exact time on hover: short enough to stay on one line.
          { sortValue: row.updatedAt ? new Date(row.updatedAt).getTime() : 0, content: row.updatedAt ? relativeTime(row.updatedAt) : '—' },
          { sortValue: row.active ? 'Active' : 'Inactive', content: el('div',{class:'state-cell'},badge,toggle) },
          actions
        ];
      }),{
        sortable: { 0: 'text', 2: 'date', 3: 'text' },
        initialSort: { column: 0, direction: 'asc' },
        className: 'workflows-table'
      }):empty('No matching workflows','Try another name or create a workflow in this instance.',[button('Clear search',()=>{input.value='';searchFleet();},'ghost')]));
    } catch(error){if(!signal.aborted)entry.messages.replaceChildren(loadFailure(error,()=>load(entry,after),`Could not load workflows on ${instance.name}`));}
    finally{entry.loading=false;entry.next.disabled=searching||!entry.cursor;entry.retry.disabled=searching;}
  }
  async function searchFleet(){
    if(searching||entries.some(entry=>entry.loading))return;
    closeSuggestions();
    searching=true;search.disabled=true;input.disabled=true;term=input.value.trim();
    entries.forEach(entry=>{entry.next.disabled=entry.retry.disabled=true;entry.detail.hidden=true;});
    let index=0;
    try {await Promise.all(Array.from({length:Math.min(3,entries.length)},async()=>{
      while(index<entries.length&&!signal.aborted)await load(entries[index++]);
    }));}finally{searching=false;search.disabled=false;input.disabled=false;entries.forEach(entry=>{entry.next.disabled=!entry.cursor;entry.retry.disabled=false;});}
  }
  // Return cleanup immediately so navigation can cancel even the first slow load.
  searchFleet();return()=>controller.abort();
}
