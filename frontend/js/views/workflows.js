import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,field,heading,table,notice,empty,jsonView} from '../lib/html.js';
import {confirmAction,stateBadge} from '../lib/confirm.js';
import {repairHref} from '../lib/triage.js';
import {loadingState,loadFailure,asOf} from '../lib/payload.js';
export async function render(container) {
  const controller=new AbortController(), signal=controller.signal;
  const instances=state.instances.filter(item=>!state.instanceId||item.id===state.instanceId);
  container.append(heading('Workflows','Search your fleet. Every action stays tied to the instance shown on its section.'));
  if(!instances.length){container.append(empty('Connect an instance','Add an n8n instance to browse its workflows.',[el('a',{class:'button ghost',href:'#instances'},'Open instances')]));return()=>controller.abort();}
  const input=el('input',{type:'search',placeholder:'Workflow name',maxLength:120}),sections=el('div',{class:'stack'});
  let searching=false, term='';
  const search=button('Search',searchFleet);
  container.append(el('form',{class:'toolbar',onsubmit:event=>{event.preventDefault();searchFleet();}},field('Find workflows',input),search),el('p',{class:'muted'},'Results and page limits are per instance. An unavailable instance does not hide the rest of your fleet.'),sections);
  const entries=instances.map(instance=>{
    const list=el('div'),detail=el('section',{class:'stack',hidden:true}),messages=el('div'),stamp=el('div');
    messages.append(loadingState(`Waiting to load workflows on ${instance.name}…`));
    const entry={instance,list,detail,messages,stamp,cursor:null,loading:false,loaded:false};
    entry.next=button('Next page',()=>load(entry,entry.cursor),'ghost');
    entry.next.setAttribute('aria-label',`Next workflow page on ${instance.name}`);
    entry.retry=button('Refresh instance',()=>load(entry),'ghost');
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
        const actions=el('div',{class:'toolbar'},button('Inspect',async()=>{
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
          { sortValue: row.updatedAt ? new Date(row.updatedAt).getTime() : 0, content: row.updatedAt ? new Date(row.updatedAt).toLocaleString() : '—' },
          { sortValue: row.active ? 'Active' : 'Inactive', content: el('div',{class:'state-cell'},badge,toggle) },
          actions
        ];
      }),{
        sortable: { 0: 'text', 2: 'date', 3: 'text' },
        initialSort: { column: 0, direction: 'asc' }
      }):empty('No matching workflows','Try another name or create a workflow in this instance.',[button('Clear search',()=>searchFleet(),'ghost')]));
    } catch(error){if(!signal.aborted)entry.messages.replaceChildren(loadFailure(error,()=>load(entry,after),`Could not load workflows on ${instance.name}`));}
    finally{entry.loading=false;entry.next.disabled=searching||!entry.cursor;entry.retry.disabled=searching;}
  }
  async function searchFleet(){
    if(searching||entries.some(entry=>entry.loading))return;
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
