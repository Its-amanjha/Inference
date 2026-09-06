import {api as request} from '../api.js';
import {el,button,field,notice,empty,heading} from '../lib/html.js';
import {can} from '../state.js';
import {renderMarkdown} from '../lib/markdown.js';
import {confirmAction} from '../lib/confirm.js';

export async function render(container){
  const controller=new AbortController(),signal=controller.signal;
  const messages=el('div'),list=el('div',{class:'stack'}),editor=el('div',{class:'card stack'}),search=el('input',{type:'search',placeholder:'Search literal text', 'aria-label':'Search notes'});
  let revision=null,loadedPath=null,selection=0;
  const path=el('input',{placeholder:'Runbooks/Deploy.md',required:true}),content=el('textarea',{rows:20,'aria-label':'Markdown content'}),links=el('div'),preview=el('div',{class:'markdown-preview',role:'region','aria-label':'Markdown preview'});
  content.readOnly=!can('operator');path.readOnly=!can('operator');
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  async function open(notePath){const token=++selection;const note=await api('/api/notes/note?path='+encodeURIComponent(notePath),{signal});if(token!==selection)return;loadedPath=note.path;path.value=note.path;path.readOnly=true;revision=note.revision;content.value=note.content;renderMarkdown(preview,note.content);
    const backlinks=await api('/api/notes/backlinks?path='+encodeURIComponent(notePath),{signal});if(token!==selection)return;links.replaceChildren(el('h3',{},'Linked from'),...backlinks.data.map(n=>button(n.path,()=>act(()=>open(n.path)),'ghost')));if(!backlinks.data.length)links.append(el('p',{class:'muted'},'No backlinks yet.'));
  }
  async function load(){const data=await api(search.value?'/api/notes/search?q='+encodeURIComponent(search.value):'/api/notes',{signal});list.replaceChildren(...data.data.map(n=>el('div',{},button(n.path,()=>act(()=>open(n.path)),'ghost'),n.excerpt?el('p',{class:'muted'},n.excerpt):null)));if(!data.data.length)list.append(empty('No matching notes','Create a Markdown note or change your search.'));}
  const controls=el('div',{class:'toolbar'});
  if(can('operator'))controls.append(button('New note',()=>{selection++;loadedPath=null;revision=null;path.value='';path.readOnly=false;content.value='';preview.textContent='';links.replaceChildren();path.focus();},'ghost'),button('Save note',()=>act(async()=>{if(!path.reportValidity())return;const result=await api('/api/notes/note',{method:'PUT',json:{path:path.value,content:content.value,expected_revision:revision}});revision=result.revision;loadedPath=result.path;path.readOnly=true;renderMarkdown(preview,result.content);notice(messages,'Note saved.');await load();})),button('Delete note',()=>act(async()=>{if(!loadedPath||!revision)return;const notePath=loadedPath,noteRevision=revision;if(!await confirmAction({title:'Delete note',target:notePath,consequence:'The Markdown file is permanently deleted from the notes folder.',confirmLabel:'Delete note',danger:true}))return;if(loadedPath!==notePath||revision!==noteRevision)throw new Error('The open note changed. Review it before deleting.');await api('/api/notes/note?path='+encodeURIComponent(notePath)+'&expected_revision='+encodeURIComponent(noteRevision),{method:'DELETE'});selection++;revision=null;loadedPath=null;path.value='';path.readOnly=false;content.value='';preview.textContent='';links.replaceChildren();await load();}),'danger'));
  controls.append(button('Preview Markdown',()=>{renderMarkdown(preview,content.value);},'ghost'));
  editor.append(field('Markdown path',path),content,controls,el('h3',{},'Markdown preview'),preview,links);
  container.append(heading('Notes','Runbooks and linked Markdown notes for your fleet.'),messages,el('div',{class:'toolbar'},search,button('Search',()=>act(load),'ghost')),el('div',{class:'grid'},el('aside',{class:'card'},list),editor));
  search.addEventListener('keydown',e=>{if(e.key==='Enter')act(load);});await act(load);return()=>{selection++;controller.abort();};
}
