// Repair target: the workflow (and optional error or execution) a person pins to a repair conversation.
// Only people set it, from a deep link or the Change search. Model output never changes it.
import {api} from '../api.js';
import {state} from '../state.js';
import {el,field,notice} from './html.js';
import {icon,actionIcon,registerIcons} from './icons.js';

registerIcons({target:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M12 12h.01'});

const IDENTIFIER=/^[A-Za-z0-9_-]{1,128}$/;
const ERROR_ID=/^\d{1,18}$/;
const KEYS=[['instance_id',IDENTIFIER],['workflow_id',IDENTIFIER],['error_id',ERROR_ID],['execution_id',IDENTIFIER]];
const LIST_LIMIT=25;
// Keep aligned with backend/modules/assistant/repair_contract.py: MAX_NOTES_CHARS.
export const MAX_NOTES_CHARS=2000;
let sequence=0;

const instanceLabel=id=>state.instances.find(item=>item.id===id)?.name||id;

// Invalid values are reported, never silently repaired or guessed.
export function parseRepairTarget(hash=location.hash){
  const params=new URLSearchParams(hash.split('?')[1]||'');
  if(params.get('mode')!=='repair')return {target:null,rejected:[]};
  const target={mode:'repair'},rejected=[];
  for(const [key,pattern] of KEYS){
    const value=params.get(key);
    if(value===null||value==='')continue;
    if(pattern.test(value))target[key]=value;else rejected.push(key);
  }
  if(!target.instance_id)return {target:null,rejected:rejected.includes('instance_id')?rejected:['instance_id',...rejected]};
  return {target,rejected};
}

export function repairHash(target){
  if(!target)return '#assistant';
  const params=new URLSearchParams({mode:'repair'});
  for(const [key] of KEYS)if(target[key])params.set(key,target[key]);
  return '#assistant?'+params;
}

// The RepairTarget body sent with chat requests while a target is pinned.
export function repairContext(target,sendErrorText,notes=''){
  const context={mode:'repair',instance_id:target.instance_id,send_error_text:Boolean(sendErrorText)};
  for(const key of ['workflow_id','error_id','execution_id'])if(target[key])context[key]=target[key];
  if(notes)context.notes=notes;
  return context;
}

export function contextQuery(target,sendErrorText,notes=''){
  const params=new URLSearchParams({instance_id:target.instance_id});
  if(target.workflow_id)params.set('workflow_id',target.workflow_id);
  if(target.error_id)params.set('error_id',target.error_id);
  else if(target.execution_id)params.set('execution_id',target.execution_id);
  params.set('send_error_text',String(Boolean(sendErrorText)));
  if(notes)params.set('notes',notes);
  return params;
}

export function describeTarget(target,name){
  if(!target)return '';
  const workflow=target.workflow_id?(name?`workflow “${name}” (${target.workflow_id})`:`workflow ${target.workflow_id}`):'no workflow';
  const evidence=target.error_id?`, error ${target.error_id}`:target.execution_id?`, execution ${target.execution_id}`:'';
  return `${workflow} on ${instanceLabel(target.instance_id)}${evidence}`;
}

// Masked workflow metadata (id, name, active) from the assistant's read tool. Raw workflow documents carry
// unmasked parameter values, so the repair UI never requests them.
export async function listWorkflows(instanceId,signal){
  const result=await api('/api/assistant/tools/run',{method:'POST',json:{name:'list_workflows',arguments:{instance_id:instanceId,limit:LIST_LIMIT}},signal});
  return (Array.isArray(result?.data)?result.data:[]).filter(row=>row&&IDENTIFIER.test(String(row.id??'')))
    .map(row=>({id:String(row.id),name:typeof row.name==='string'&&row.name.trim()?row.name:String(row.id),active:row.active}));
}

// First page of workflows on an instance through the fleet API (masked documents; names are readable).
// CodeLab and Promote use this for pickers; the workflow list stays lazy per instance.
export async function loadInstanceWorkflows(instanceId,signal){
  const result=await api('/api/n8n_proxy/workflows?'+new URLSearchParams({instance_id:instanceId,limit:'250'}),{signal});
  const rows=Array.isArray(result?.data)?result.data:[];
  return rows.filter(row=>row&&IDENTIFIER.test(String(row.id??'')))
    .map(row=>({id:String(row.id),name:typeof row.name==='string'&&row.name.trim()?row.name:String(row.id),detail:typeof row.active==='boolean'?(row.active?'Active':'Inactive'):''}));
}

// One searchable ID picker for CodeLab, Promote mappings and saved conversations.
// Human labels with the id as secondary text, filter-as-you-type, arrow/Enter/Escape
// keyboard support, pasted unknown ids kept as "not in this list", lazy per-scope loading
// with an empty state and a Retry on failure. The search box always keeps the raw text,
// so pasting an id never loses anything.
export function createIdPicker({label,placeholder='',hint=null,load,emptyText='',emptyActions=[],clearLabel='Clear',clearable=true,onPick,signal}){
  const listId=`id-picker-list-${++sequence}`;
  const search=el('input',{type:'search',maxLength:256,placeholder,autocomplete:'off',spellcheck:'false',role:'combobox','aria-expanded':'false','aria-controls':listId,'aria-autocomplete':'list'});
  const list=el('ul',{className:'picker-list',id:listId,role:'listbox',hidden:true});
  const status=el('p',{className:'picker-status muted',role:'status'});
  const pickedName=el('strong',{}),pickedId=el('span',{className:'picker-id'});
  const picked=el('p',{className:'picker-picked',hidden:true},'Selected: ',pickedName,' ',pickedId,' ');
  const clearPicked=el('button',{type:'button',className:'button ghost picker-clear'},clearLabel);
  if(clearable)picked.append(clearPicked);
  const node=el('div',{className:'picker'},field(label,search,hint?{hint}:{}),status,list,picked);
  let items=null,loadError=null,loading=null,open=false,activeIndex=-1,selectedId='',selectedKnown=false,pendingResolve=false;

  function visibleRows(){
    const needle=search.value.trim().toLowerCase();
    const rows=!needle?(items||[]):items.filter(row=>(row.name+' '+row.id+' '+(row.detail||'')).toLowerCase().includes(needle));
    return rows.slice(0,50);
  }
  function unknownOption(){
    const text=search.value.trim();
    if(!text||!items||items.some(row=>row.id===text))return null;
    return {id:text,name:text,detail:'',unknown:true};
  }
  function paint(){
    const rows=visibleRows(),extra=unknownOption();
    list.replaceChildren(...rows.map((row,index)=>optionNode(row,index)),...(extra?[optionNode(extra,rows.length,true)]:[]));
    const count=list.childElementCount;
    list.hidden=!open||count===0;
    search.setAttribute('aria-expanded',String(open&&!list.hidden));
    if(activeIndex>=count)activeIndex=count-1;
    syncActive();
  }
  function optionNode(row,index,unknown=false){
    const item=el('li',{className:'picker-option'+(unknown?' picker-unknown':''),id:`${listId}-${index}`,role:'option','aria-selected':String(index===activeIndex)});
    item.append(el('span',{className:'picker-name'},unknown?`Use “${row.id}”`:row.name),el('span',{className:'picker-id'},unknown?'(id not in this list)':row.detail?`${row.detail} · ${row.id}`:row.id));
    item.addEventListener('mousedown',event=>{event.preventDefault();choose(index);});
    return item;
  }
  function syncActive(){
    for(const [index,item] of [...list.children].entries()){
      item.setAttribute('aria-selected',String(index===activeIndex));
      if(index===activeIndex){search.setAttribute('aria-activedescendant',item.id);item.scrollIntoView({block:'nearest'});}
    }
    if(activeIndex<0)search.removeAttribute('aria-activedescendant');
  }
  function choose(index){
    const rows=visibleRows(),extra=unknownOption();
    const row=index<rows.length?rows[index]:extra;
    if(!row)return;
    selectedId=row.id;selectedKnown=!row.unknown;
    search.value=row.id;setOpen(false);paintPicked();
    onPick(row.id,!row.unknown,row);
  }
  function paintPicked(){
    if(!selectedId){picked.hidden=true;return;}
    picked.hidden=false;
    if(selectedKnown){const row=(items||[]).find(row=>row.id===selectedId);pickedName.textContent=row?row.name:selectedId;pickedId.textContent=row?row.id:'';}
    else{pickedName.textContent=selectedId;pickedId.textContent='(id not in this list)';}
  }
  function setOpen(value){
    open=value;
    if(open)ensureLoaded();
    paint();
  }
  function ensureLoaded(){
    if(items!==null||loading||signal.aborted)return Promise.resolve();
    status.textContent='Loading…';
    loading=(async()=>{
      try{
        const loaded=await load(signal);
        if(signal.aborted)return;
        items=Array.isArray(loaded)?loaded:loaded.items||[];
        loadError=null;
        if(items.length===0)status.replaceChildren(emptyText||'Nothing to choose yet.',...emptyActions);
        else status.replaceChildren(loaded.note||'');
        if(pendingResolve)resolvePending();else paintPicked();
      }catch(error){
        if(signal.aborted)return;
        loadError=error;items=null;
        status.replaceChildren(`Could not load the list. ${error?.message||error} `,el('button',{type:'button',className:'button ghost picker-retry'},'Retry'));
        status.querySelector('button').addEventListener('click',()=>{loading=null;ensureLoaded();paint();});
      }finally{loading=null;}
      paint();
    })();
    return loading;
  }
  function resolvePending(){
    selectedKnown=(items||[]).some(row=>row.id===selectedId);
    pendingResolve=false;paintPicked();paint();
  }
  search.addEventListener('focus',()=>setOpen(true),{signal});
  search.addEventListener('input',()=>{activeIndex=-1;if(selectedId&&search.value.trim()!==selectedId){selectedId='';picked.hidden=true;}setOpen(true);},{signal});
  search.addEventListener('blur',()=>setOpen(false),{signal});
  search.addEventListener('keydown',event=>{
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      event.preventDefault();
      const count=list.childElementCount;
      if(!count)return;
      if(activeIndex<0)activeIndex=event.key==='ArrowDown'?0:count-1;
      else activeIndex=event.key==='ArrowDown'?(activeIndex+1)%count:(activeIndex-1+count)%count;
      setOpen(true);syncActive();
    }else if(event.key==='Enter'){
      if(open&&activeIndex>=0){event.preventDefault();choose(activeIndex);}
      else if(open&&unknownOption()){event.preventDefault();choose(visibleRows().length);}
      else if(open&&visibleRows().length===1){event.preventDefault();choose(0);}
    }else if(event.key==='Escape'){setOpen(false);}
  },{signal});
  clearPicked.addEventListener('click',()=>{selectedId='';search.value='';picked.hidden=true;search.focus();onPick('',false);},{signal});
  return {node,input:search,
    get(){return search.value.trim();},
    set(id,name=null){
      search.value=id||'';selectedId=id||'';
      if(!selectedId){picked.hidden=true;return;}
      if(items!==null){selectedKnown=(items||[]).some(row=>row.id===selectedId);paintPicked();}
      else{pendingResolve=true;selectedKnown=Boolean(name);picked.hidden=false;pickedName.textContent=name||selectedId;pickedId.textContent=name?selectedId:'(id not in this list)';ensureLoaded();}
    },
    reload(){items=null;loadError=null;setOpen(true);},
    focus(){search.focus();}};
}

// Toolbar chip with an inline workflow search. onChange receives the next target, or null to clear it.
export function targetChip({signal,onChange}){
  const node=el('div',{className:'repair-target',role:'group','aria-label':'Repair target'});
  const panelId=`repair-target-search-${++sequence}`;
  let target=null,knownName=null,nameNode=null,idNode=null,searchSequence=0;
  const instanceSelect=el('select',{});
  const query=el('input',{type:'search',maxLength:120,placeholder:'Part of the workflow name',autocomplete:'off'});
  const results=el('div',{className:'repair-target-results','aria-live':'polite'});
  const submit=el('button',{type:'submit',className:'button ghost'},actionIcon('Search'),'Search');
  const form=el('form',{className:'repair-target-form',role:'search','aria-label':'Find a workflow to repair'},field('Instance to search',instanceSelect),field('Workflow name',query),submit);
  const panel=el('div',{className:'repair-target-search',id:panelId,hidden:true},form,results,
    el('p',{className:'repair-target-help'},'Pick the workflow to repair. The AI only works on the target you choose.'));
  const change=el('button',{type:'button',className:'button ghost','aria-expanded':'false','aria-controls':panelId},'Change');
  const clear=el('button',{type:'button',className:'button ghost'},'Clear');

  function fillInstances(){
    const options=state.instances.map(row=>el('option',{value:row.id},row.name||row.id));
    if(target&&!state.instances.some(row=>row.id===target.instance_id))options.unshift(el('option',{value:target.instance_id},target.instance_id));
    instanceSelect.replaceChildren(...options);
    if(target)instanceSelect.value=target.instance_id;
  }
  function toggle(open){
    panel.hidden=!open;change.setAttribute('aria-expanded',String(open));
    if(open){fillInstances();query.focus();}
  }
  async function search(){
    const instanceId=instanceSelect.value;
    if(!instanceId){results.replaceChildren(el('p',{className:'muted'},'Choose an instance first.'));return;}
    const current=++searchSequence,name=query.value.trim(),needle=name.toLowerCase();
    submit.disabled=true;
    results.replaceChildren(el('p',{className:'muted',role:'status'},'Searching workflows…'));
    try{
      const rows=await listWorkflows(instanceId,signal);
      if(signal.aborted||current!==searchSequence)return;
      const matches=needle?rows.filter(row=>row.name.toLowerCase().includes(needle)||row.id.toLowerCase().includes(needle)):rows;
      const limit=rows.length>=LIST_LIMIT?el('p',{className:'muted'},`Only the first ${LIST_LIMIT} workflows on ${instanceLabel(instanceId)} can be searched here.`):null;
      if(!matches.length){results.replaceChildren(el('p',{className:'muted'},name?`No workflows match “${name}” on ${instanceLabel(instanceId)}.`:`No workflows found on ${instanceLabel(instanceId)}.`),limit);return;}
      results.replaceChildren(el('ul',{},matches.map(row=>{
        const option=el('button',{type:'button',className:'button ghost repair-target-option'},el('span',{className:'repair-target-option-name'},row.name),el('span',{className:'repair-target-option-id'},` · ${row.id}`));
        option.addEventListener('click',()=>{
          const next={mode:'repair',instance_id:instanceId,workflow_id:row.id};
          // Error and execution evidence belong to one workflow; keep them only when the workflow is unchanged.
          if(target?.instance_id===instanceId&&target?.workflow_id===row.id){if(target.error_id)next.error_id=target.error_id;if(target.execution_id)next.execution_id=target.execution_id;}
          toggle(false);onChange(next);
          if(row.name!==row.id)chip.setName(row.name);
          change.focus();
        },{signal});
        return el('li',{},option);
      })),limit);
    }catch(error){if(!signal.aborted&&current===searchSequence){results.replaceChildren();notice(results,error);}}
    finally{if(current===searchSequence)submit.disabled=false;}
  }
  function paint(){
    nameNode=idNode=null;
    if(!target){node.replaceChildren();return;}
    const part=(label,...value)=>el('span',{className:'repair-target-part'},el('span',{className:'repair-target-label'},label),...value);
    const parts=[icon('target'),part('Instance',el('strong',{},instanceLabel(target.instance_id)))];
    if(target.workflow_id){
      nameNode=el('strong',{},knownName||target.workflow_id);
      idNode=el('span',{className:'repair-target-id',hidden:!knownName},target.workflow_id);
      parts.push(part('Workflow',nameNode,idNode));
    }else parts.push(part('Workflow',el('span',{className:'repair-target-missing'},'None selected')));
    if(target.error_id)parts.push(part('Error',el('strong',{},target.error_id)));
    if(target.execution_id)parts.push(part('Execution',el('strong',{},target.execution_id)));
    node.replaceChildren(...parts,el('span',{className:'repair-target-actions'},change,clear),panel);
  }

  change.addEventListener('click',()=>toggle(panel.hidden),{signal});
  clear.addEventListener('click',()=>{toggle(false);onChange(null);},{signal});
  panel.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();toggle(false);change.focus();}},{signal});
  form.addEventListener('submit',event=>{event.preventDefault();search();},{signal});
  const chip={node,
    set(next){
      const same=Boolean(next&&target&&next.instance_id===target.instance_id&&next.workflow_id===target.workflow_id);
      target=next?{...next}:null;
      if(!same)knownName=null;
      if(!target)toggle(false);
      paint();
    },
    // Updates the name in place so focus inside the chip or its search stays where it is.
    setName(name){
      if(typeof name!=='string'||!name.trim()||!target?.workflow_id)return;
      knownName=name;
      if(nameNode&&idNode){nameNode.textContent=name;idNode.hidden=false;}
    }};
  return chip;
}
