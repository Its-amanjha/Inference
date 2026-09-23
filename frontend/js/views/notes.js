import {api as request} from '../api.js';
import {el,button,notice,empty,heading} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {can} from '../state.js';
import {renderMarkdown} from '../lib/markdown.js';
import {confirmAction} from '../lib/confirm.js';

// One workspace: the library on the left, the open note on the right with its editor and a live
// preview side by side. Paths name notes, so the path is the note's title.
export async function render(container){
  const controller=new AbortController(),signal=controller.signal;
  const messages=el('div'),list=el('ul',{class:'notes-list'});
  const search=el('input',{type:'search',placeholder:'Search notes','aria-label':'Search notes'});
  let revision=null,loadedPath=null,selection=0,previewTimer=0,savedText=null,editing=false;
  // Opening another note or starting a new one replaces the editor, so unsaved text is only dropped on request.
  const unsaved=()=>editing&&(loadedPath?content.value!==savedText:Boolean(path.value.trim()||content.value.trim()));
  const keepText=async()=>unsaved()&&!await confirmAction({title:'Discard your changes',target:loadedPath||path.value.trim()||'New note',
    consequence:'The note on screen has changes that are not saved. They are lost if you continue.',confirmLabel:'Discard changes',danger:true});
  const path=el('input',{id:'note-path',class:'notes-title',placeholder:'Name it, e.g. Runbooks/Deploy.md',required:true,autocomplete:'off',spellcheck:'false'});
  const content=el('textarea',{id:'note-content',class:'notes-editor',placeholder:'Write Markdown…'});
  const preview=el('div',{class:'markdown-preview notes-preview',role:'region','aria-label':'Markdown preview'});
  const links=el('div',{class:'notes-links'});
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
  const showPreview=()=>{renderMarkdown(preview,content.value);if(!content.value.trim())preview.replaceChildren(el('p',{class:'notes-preview-empty'},'The preview appears here as you write.'));};
  content.addEventListener('input',()=>{clearTimeout(previewTimer);previewTimer=setTimeout(showPreview,250);},{signal});
  const markActive=()=>{for(const item of list.querySelectorAll('.notes-item'))item.toggleAttribute('aria-current',item.dataset.path===loadedPath);};
  const split=notePath=>{const parts=String(notePath).split('/');const file=parts.pop();return {name:file.replace(/\.md$/i,''),folder:parts.join('/')};};

  async function open(notePath){if(notePath!==loadedPath&&await keepText())return;const token=++selection;const note=await api('/api/notes/note?path='+encodeURIComponent(notePath),{signal});if(token!==selection)return;
    showEditor(true);loadedPath=note.path;path.value=note.path;path.readOnly=true;revision=note.revision;content.value=note.content;savedText=note.content;editing=true;showPreview();markActive();syncDelete();
    const backlinks=await api('/api/notes/backlinks?path='+encodeURIComponent(notePath),{signal});if(token!==selection)return;
    links.replaceChildren(backlinks.data.length?el('div',{},el('h3',{},'Linked from'),el('ul',{class:'backlinks'},...backlinks.data.map(n=>el('li',{},button(n.path,()=>act(()=>open(n.path)),'ghost'))))):'');
  }
  async function load(){const data=await api(search.value?'/api/notes/search?q='+encodeURIComponent(search.value):'/api/notes',{signal});
    list.replaceChildren(...data.data.map(n=>{const {name,folder}=split(n.path);
      const item=el('li',{class:'notes-item','data-path':n.path},el('button',{type:'button',class:'notes-item-button','aria-label':n.path},
        el('span',{class:'notes-item-name'},name),folder?el('span',{class:'notes-item-folder'},folder):null,n.excerpt?el('span',{class:'notes-item-excerpt'},n.excerpt):null));
      item.firstChild.addEventListener('click',()=>act(()=>open(n.path)));return item;}));
    if(!data.data.length)list.append(el('li',{class:'notes-list-empty'},empty(search.value?'No matching notes':'No notes yet',search.value?'Try another search.':'Runbooks and fleet notes live here.',
      search.value?[button('Clear search',()=>{search.value='';act(load);},'ghost')]:[],search.value?'search':'notes')));
    markActive();syncDelete();
  }
  const startNote=async()=>{if(await keepText())return;selection++;showEditor(true);editing=true;savedText=null;loadedPath=null;revision=null;path.value='';path.readOnly=false;content.value='';showPreview();links.replaceChildren();markActive();syncDelete();path.focus();};
  const saveNote=()=>act(async()=>{
    if(!path.reportValidity())return;
    const result=await api('/api/notes/note',{method:'PUT',json:{path:path.value,content:content.value,expected_revision:revision}});
    revision=result.revision;loadedPath=result.path;path.readOnly=true;content.value=result.content;savedText=result.content;showPreview();syncDelete();notice(messages,'Note saved.');await load();
  });
  const deleteButton=button('Delete note',()=>deleteNote(),'danger');
  const syncDelete=()=>{deleteButton.hidden=!loadedPath;};
  const deleteNote=()=>act(async()=>{
    if(!loadedPath||!revision){notice(messages,'Open a note before deleting it.');return;}
    const notePath=loadedPath,noteRevision=revision;
    if(!await confirmAction({title:'Delete note',target:notePath,consequence:'The Markdown file is permanently deleted from the notes folder.',confirmLabel:'Delete note',danger:true}))return;
    if(loadedPath!==notePath||revision!==noteRevision)throw new Error('The open note changed. Review it before deleting.');
    await api('/api/notes/note?path='+encodeURIComponent(notePath)+'&expected_revision='+encodeURIComponent(noteRevision),{method:'DELETE'});
    selection++;revision=null;loadedPath=null;savedText=null;editing=false;path.value='';path.readOnly=false;content.value='';showPreview();links.replaceChildren();showEditor(false);await load();
  });

  const searchForm=el('form',{class:'notes-search',role:'search'},search,button('Search',()=>act(load),'ghost'));
  searchForm.addEventListener('submit',event=>{event.preventDefault();act(load);});
  // The library filters as you type, after a short pause.
  let searchTimer=0;
  search.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>act(load),300);},{signal});
  const library=el('aside',{class:'notes-library','aria-label':'Notes library'},
    el('div',{class:'notes-library-head'},el('h2',{},'Your notes'),can('operator')?button('New note',startNote,'ghost',{'aria-label':'New note'}):null),
    searchForm,list);
  // Enter in the title saves instead of reloading the page.
  const head=el('form',{class:'notes-head'},
    el('label',{class:'sr-only',for:'note-path'},'Markdown path'),path,
    can('operator')?el('div',{class:'notes-actions'},deleteButton,button('Save note',saveNote)):null);
  head.addEventListener('submit',event=>{event.preventDefault();if(can('operator'))saveNote();});
  const panes=el('div',{class:'notes-split'},
      el('div',{class:'notes-pane'},el('label',{class:'notes-pane-label',for:'note-content'},'Markdown content'),content),
      el('div',{class:'notes-pane'},el('span',{class:'notes-pane-label','aria-hidden':'true'},'Preview'),preview));
  // Until a note is open or started there is nothing to edit, so the pane says what to do instead.
  const blank=el('div',{class:'notes-blank'},icon('notes'),el('h2',{},'No note open'),
    el('p',{},can('operator')?'Pick a note from your library, or start one with New note.':'Pick a note from your library to read it.'));
  const workspace=el('section',{class:'notes-main','aria-label':'Open note'},blank,head,panes,links);
  function showEditor(shown){blank.hidden=shown;head.hidden=!shown;panes.hidden=!shown;links.hidden=!shown;}
  showEditor(false);
  container.append(heading('Notes','Runbooks and linked notes for your fleet.'),messages,el('div',{class:'notes-shell'},library,workspace));
  showPreview();
  await act(load);
  // #notes?path=… opens that note, e.g. from an error's details panel.
  const linked=new URLSearchParams(location.hash.split('?')[1]||'').get('path');
  if(linked)await act(()=>open(linked));
  return()=>{selection++;clearTimeout(previewTimer);controller.abort();};
}
