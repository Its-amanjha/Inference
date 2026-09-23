import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,field,heading,table,notice,empty} from '../lib/html.js';
import {confirmAction,stateBadge} from '../lib/confirm.js';
import {repairHref,relativeTime,n8nUrl,registerShortcuts} from '../lib/triage.js';
import {loadingState,loadFailure,asOf} from '../lib/payload.js';
import {icon} from '../lib/icons.js';
import {rowMenu} from '../lib/row-menu.js';
import {openPeek,peekFacts,peekSection,peekCopy,closePeek} from '../lib/peek.js';
import {flowStrip,flowList,flowOrder} from '../lib/workflow-shape.js';

const plural=(count,word)=>`${count} ${word}${count===1?'':'s'}`;

export async function render(container) {
  const controller=new AbortController(), signal=controller.signal;
  const instances=state.instances.filter(item=>!state.instanceId||item.id===state.instanceId);
  container.append(heading('Workflows','Every workflow on every instance. Open one to see its steps.'));
  if(!instances.length){container.append(empty('Connect an instance','Add an n8n instance to browse its workflows.',[el('a',{class:'button ghost',href:'#instances'},'Open instances')],'instances'));return()=>controller.abort();}
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
    const list=el('div'),messages=el('div'),stamp=el('div');
    const bulk=el('div',{class:'workflow-bulk',role:'region','aria-label':`Bulk actions on ${instance.name}`,hidden:true});
    messages.append(loadingState(`Waiting to load workflows on ${instance.name}…`));
    const entry={instance,list,messages,stamp,bulk,rows:[],selected:new Set(),cursor:null,loading:false,loaded:false};
    entry.next=button('Next page',()=>load(entry,entry.cursor),'ghost');
    entry.next.setAttribute('aria-label',`Next workflow page on ${instance.name}`);
    entry.retry=button('Refresh instance',()=>load(entry),'ghost',{'aria-label':`Refresh workflows on ${instance.name}`});
    sections.append(el('section',{class:'card stack','aria-label':`Workflows on ${instance.name}`},el('h2',{},instance.name),messages,stamp,bulk,list,el('div',{class:'toolbar'},entry.retry,entry.next)));
    return entry;
  });

  const promoteHref=(entry,row)=>'#promote?'+new URLSearchParams({workflow_id:row.id,source_instance_id:entry.instance.id});
  const codelabHref=(entry,row)=>'#codelab?'+new URLSearchParams({workflow_id:row.id,instance_id:entry.instance.id});
  const stateOf=row=>typeof row.active==='boolean'?stateBadge(row.active?'success':'',row.active?'Active':'Inactive'):stateBadge('warning','Unknown',String(row.active));
  const external=(href,label,className)=>el('a',{class:className,href,target:'_blank',rel:'noopener noreferrer'},label,icon('external'));

  // Activating starts a workflow's triggers and deactivating stops them; both confirm, on one or many rows.
  async function setActive(entry,list,active){
    if(!can('operator')||!list.length)return;
    const names=list.map(row=>row.name??row.id),many=list.length>1;
    const target=[el('strong',{},many?`${names.slice(0,5).join(', ')}${names.length>5?` and ${names.length-5} more`:''}`:names[0]),' on ',el('strong',{},entry.instance.name)];
    const ok=await confirmAction(active
      ?{title:many?`Activate ${plural(list.length,'workflow')}`:'Activate workflow',target,consequence:many?'Their triggers start on this instance, so they can run automatically.':'Its triggers start on this instance, so it can run automatically.',confirmLabel:many?'Activate workflows':'Activate workflow'}
      :{title:many?`Deactivate ${plural(list.length,'workflow')}`:'Deactivate workflow',target,consequence:many?'Their triggers stop, so they no longer run automatically on this instance until they are activated again.':'Its triggers stop, so it no longer runs automatically on this instance until it is activated again.',confirmLabel:many?'Deactivate workflows':'Deactivate workflow',danger:true});
    if(!ok)return;
    const failed=[];
    for(const row of list){
      if(signal.aborted)return;
      try{await api(`/api/n8n_proxy/workflows/${encodeURIComponent(row.id)}/${active?'activate':'deactivate'}?instance_id=${encodeURIComponent(entry.instance.id)}`,{method:'POST'});}
      catch(error){if(signal.aborted)return;failed.push(`${row.name??row.id} (${error.message})`);}
    }
    await load(entry);
    const verb=active?'activated':'deactivated';
    if(failed.length)notice(entry.messages,new Error(`${list.length-failed.length} of ${list.length} ${verb}. Not changed: ${failed.join('; ')}.`));
    else if(many)notice(entry.messages,`${plural(list.length,'workflow')} ${verb}.`);
  }

  function renderBulk(entry){
    const chosen=entry.rows.filter(row=>entry.selected.has(row.id));
    entry.bulk.hidden=!chosen.length;
    if(!chosen.length){entry.bulk.replaceChildren();return;}
    const active=chosen.filter(row=>row.active===true),inactive=chosen.filter(row=>row.active===false);
    entry.bulk.replaceChildren(el('span',{class:'workflow-bulk-count'},`${chosen.length} selected`),
      el('div',{class:'workflow-bulk-actions'},
        active.length?button(`Deactivate ${active.length}`,()=>setActive(entry,active,false),'danger',{'aria-label':`Deactivate ${plural(active.length,'selected workflow')} on ${entry.instance.name}`}):null,
        inactive.length?button(`Activate ${inactive.length}`,()=>setActive(entry,inactive,true),'ghost',{'aria-label':`Activate ${plural(inactive.length,'selected workflow')} on ${entry.instance.name}`}):null,
        button('Clear selection',()=>{entry.selected.clear();syncChecks(entry);renderBulk(entry);},'ghost')));
  }
  function syncChecks(entry){
    for(const box of entry.list.querySelectorAll('input.workflow-select'))box.checked=entry.selected.has(box.value);
    const all=entry.list.querySelector('input.workflow-select-all');
    if(all){const count=entry.rows.filter(row=>entry.selected.has(row.id)).length;all.checked=count>0&&count===entry.rows.length;all.indeterminate=count>0&&count<entry.rows.length;}
    for(const box of entry.list.querySelectorAll('input.workflow-select'))box.closest('tr')?.classList.toggle('is-selected',box.checked);
  }

  // The workflow in one place: its steps in run order, what starts it, and the way into n8n.
  function openDetails(entry,row){
    const name=row.name??row.id,link=n8nUrl(entry.instance.id,row.id);
    const steps=flowOrder(row),starts=[...new Set(steps.filter(step=>step.trigger).map(step=>step.kind))];
    const tags=(row.tags??[]).map(tag=>tag?.name).filter(Boolean);
    openPeek({
      eyebrow:stateOf(row),
      title:name,
      meta:[entry.instance.name,row.updatedAt?' · updated ':null,row.updatedAt?relativeTime(row.updatedAt):null],
      sections:[
        peekSection(steps.length?`Steps (${steps.length})`:'Steps',flowList(row)),
        peekFacts([
          ['Starts with',starts.length?starts.join(', '):null],
          ['Workflow ID',peekCopy(row.id,'workflow ID')],
          ['Tags',tags.length?tags.join(', '):null],
          ['Created',row.createdAt?relativeTime(row.createdAt):null],
        ]),
      ],
      actions:[
        link?external(link,'Open in n8n','button'):null,
        el('a',{class:'button ghost',href:promoteHref(entry,row)},'Promote'),
        el('a',{class:'button ghost',href:codelabHref(entry,row)},'Open in CodeLab'),
        can('operator')?el('a',{class:'button ghost',href:repairHref({instance_id:entry.instance.id,workflow_id:row.id})},'Repair with AI'):null,
        can('operator')&&typeof row.active==='boolean'?button(row.active?'Deactivate':'Activate',()=>{closePeek();return setActive(entry,[row],!row.active);},row.active?'danger':'ghost',{'aria-label':`${row.active?'Deactivate':'Activate'} workflow ${name} on ${entry.instance.name}`}):null,
      ],
    });
  }

  function renderTable(entry){
    const {instance}=entry,selectable=can('operator'),offset=selectable?1:0;
    let selectAll=null;
    if(selectable){
      selectAll=el('input',{type:'checkbox',class:'workflow-select-all','aria-label':`Select all workflows on ${instance.name}`});
      selectAll.addEventListener('change',()=>{for(const row of entry.rows)selectAll.checked?entry.selected.add(row.id):entry.selected.delete(row.id);syncChecks(entry);renderBulk(entry);});
    }
    const headers=[selectable?el('label',{class:'triage-checkbox-label'},selectAll):null,'Workflow','Tags','State','Updated',el('span',{class:'sr-only'},'Actions')].filter(item=>item!==null);
    const rows=entry.rows.map(row=>{
      const name=row.name??row.id;
      // The name is the row's one inline action: it opens the details panel. The rest is in the menu.
      const open=el('button',{type:'button',class:'workflow-open','data-id':row.id,'aria-label':`Details for workflow ${name} on ${instance.name}`},name);
      open.querySelector(':scope > svg')?.remove();
      open.addEventListener('click',()=>openDetails(entry,row));
      const link=n8nUrl(instance.id,row.id);
      const menu=rowMenu([
        link?{label:'Open in n8n',href:link,external:true}:null,
        {label:'Promote',href:promoteHref(entry,row)},
        {label:'Open in CodeLab',href:codelabHref(entry,row)},
        can('operator')?{label:'Repair with AI',href:repairHref({instance_id:instance.id,workflow_id:row.id})}:null,
        can('operator')&&typeof row.active==='boolean'?{label:row.active?'Deactivate':'Activate',danger:row.active,onSelect:()=>setActive(entry,[row],!row.active)}:null,
      ],`More actions for ${name} on ${instance.name}`);
      const cells=[
        {sortValue:name,content:el('td',{class:'workflow-subject'},open,flowStrip(row))},
        el('td',{class:'workflow-tags'},(row.tags??[]).map(tag=>tag?.name).filter(Boolean).join(', ')||el('span',{class:'triage-missing'},'—')),
        {sortValue:row.active===true?'Active':row.active===false?'Inactive':'Unknown',content:el('td',{class:'workflow-state'},stateOf(row))},
        {sortValue:row.updatedAt?new Date(row.updatedAt).getTime():0,content:el('td',{class:'workflow-updated'},row.updatedAt?relativeTime(row.updatedAt):'—')},
        el('td',{class:'workflow-actions'},menu),
      ];
      if(selectable){
        const box=el('input',{type:'checkbox',class:'workflow-select',value:row.id,'aria-label':`Select workflow ${name}`,checked:entry.selected.has(row.id)});
        box.addEventListener('change',()=>{box.checked?entry.selected.add(row.id):entry.selected.delete(row.id);syncChecks(entry);renderBulk(entry);});
        cells.unshift(el('td',{class:'workflow-select-cell'},el('label',{class:'triage-checkbox-label'},box)));
      }
      return cells;
    });
    return table(headers,rows,{
      sortable:{[offset]:'text',[2+offset]:'text',[3+offset]:'date'},
      initialSort:{column:offset,direction:'asc'},
      className:'workflows-table',
      ariaLabel:`Workflows on ${instance.name} table`,
    });
  }

  async function load(entry,after=null){
    if(signal.aborted||entry.loading)return;
    entry.loading=true;entry.next.disabled=entry.retry.disabled=true;
    entry.messages.replaceChildren(entry.loaded?el('p',{class:'load-progress',role:'status'},`Refreshing workflows on ${entry.instance.name}…`):loadingState(`Loading workflows on ${entry.instance.name}…`));
    try {
      const params=new URLSearchParams({instance_id:entry.instance.id,limit:String(pageSize())});
      if(term)params.set('name',term);if(after)params.set('cursor',after);
      const result=await api('/api/n8n_proxy/workflows?'+params,{signal});
      if(signal.aborted)return;
      entry.cursor=result.next_cursor;entry.rows=result.data||[];entry.selected.clear();
      entry.messages.replaceChildren();entry.stamp.replaceChildren(asOf());entry.loaded=true;
      entry.list.replaceChildren(entry.rows.length?renderTable(entry):empty('No matching workflows','Try another name or create a workflow in this instance.',[button('Clear search',()=>{input.value='';searchFleet();},'ghost')],'search'));
      renderBulk(entry);
    } catch(error){if(!signal.aborted)entry.messages.replaceChildren(loadFailure(error,()=>load(entry,after),`Could not load workflows on ${entry.instance.name}`));}
    finally{entry.loading=false;entry.next.disabled=searching||!entry.cursor;entry.retry.disabled=searching;}
  }
  async function searchFleet(){
    if(searching||entries.some(entry=>entry.loading))return;
    closeSuggestions();
    searching=true;search.disabled=true;input.disabled=true;term=input.value.trim();
    entries.forEach(entry=>{entry.next.disabled=entry.retry.disabled=true;});
    let index=0;
    try {await Promise.all(Array.from({length:Math.min(3,entries.length)},async()=>{
      while(index<entries.length&&!signal.aborted)await load(entries[index++]);
    }));}finally{searching=false;search.disabled=false;input.disabled=false;entries.forEach(entry=>{entry.next.disabled=!entry.cursor;entry.retry.disabled=false;});}
  }
  // j and k move between workflows across every instance section; o opens the focused one.
  const move=step=>{const list=[...container.querySelectorAll('.workflow-open')];if(!list.length)return;
    const index=list.indexOf(document.activeElement),target=list[index===-1?0:Math.max(0,Math.min(list.length-1,index+step))];
    target.focus();target.scrollIntoView({block:'nearest'});};
  const unregister=registerShortcuts([
    {key:['j','J'],display:'j',description:'Focus next workflow',handler:()=>move(1)},
    {key:['k','K'],display:'k',description:'Focus previous workflow',handler:()=>move(-1)},
    {key:['o','O'],display:'o',description:'Open details of focused workflow',handler:()=>{if(document.activeElement?.classList.contains('workflow-open'))document.activeElement.click();}},
  ],'Workflows');
  // Return cleanup immediately so navigation can cancel even the first slow load.
  searchFleet();return()=>{unregister();controller.abort();};
}
