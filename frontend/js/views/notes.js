import {api as request} from '../api.js';
import {el,button,notice,empty,heading} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {can,expectOwnEvent} from '../state.js';
import {renderMarkdown} from '../lib/markdown.js';
import {confirmAction} from '../lib/confirm.js';
import {rowMenu} from '../lib/row-menu.js';
import {relativeLabel} from '../lib/triage.js';

// One workspace: the library on the left, the open note on the right with its editor and a live
// preview side by side. Paths name notes, so the path is the note's title.
export async function render(container){
  const controller=new AbortController(),signal=controller.signal;
  const messages=el('div'),list=el('ul',{class:'notes-list'});
  const search=el('input',{type:'search',placeholder:'Search notes','aria-label':'Search notes'});
  let revision=null,loadedPath=null,selection=0,previewTimer=0,savedText=null,editing=false,known=[];
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
  // [[Name]] resolves the way the server's backlinks do: beside the open note, then from the top of the
  // library, then by file name when exactly one note has it.
  const resolveLink=target=>{
    let name=target.split('#')[0].trim();const from=loadedPath||path.value.trim();
    if(!name)return from||null;
    if(!/\.md$/i.test(name))name+='.md';
    const fold=value=>value.toLowerCase(),folder=from.includes('/')?from.slice(0,from.lastIndexOf('/')+1):'';
    for(const candidate of [folder+name,name]){const hit=known.find(row=>fold(row.path)===fold(candidate));if(hit)return hit.path;}
    const named=name.includes('/')?[]:known.filter(row=>fold(row.path.split('/').pop())===fold(name));
    return named.length===1?named[0].path:null;
  };
  const noteHref=notePath=>'#notes?'+new URLSearchParams({path:notePath});
  const showPreview=()=>{renderMarkdown(preview,content.value,{wikilink:target=>{const hit=resolveLink(target);return hit?noteHref(hit):null;}});if(!content.value.trim())preview.replaceChildren(el('p',{class:'notes-preview-empty'},'The preview appears here as you write.'));};
  content.addEventListener('input',()=>{clearTimeout(previewTimer);previewTimer=setTimeout(showPreview,250);},{signal});
  // A note link opens in place, so unsaved text is still guarded.
  preview.addEventListener('click',event=>{
    const link=event.target.closest?.('a.wikilink');if(!link)return;
    event.preventDefault();act(()=>open(new URLSearchParams(link.getAttribute('href').split('?')[1]).get('path')));
  },{signal});
  const markActive=()=>{for(const item of list.querySelectorAll('.notes-item'))item.toggleAttribute('aria-current',item.dataset.path===loadedPath);};
  const split=notePath=>{const parts=String(notePath).split('/');const file=parts.pop();return {name:file.replace(/\.md$/i,''),folder:parts.join('/')};};

  async function open(notePath){if(notePath!==loadedPath&&await keepText())return;const token=++selection;const note=await api('/api/notes/note?path='+encodeURIComponent(notePath),{signal});if(token!==selection)return;
    showEditor(true);loadedPath=note.path;path.value=note.path;revision=note.revision;content.value=note.content;savedText=note.content;editing=true;showPreview();markActive();syncDelete();
    history.replaceState(history.state,'',noteHref(note.path));
    const backlinks=await api('/api/notes/backlinks?path='+encodeURIComponent(notePath),{signal});if(token!==selection)return;
    links.replaceChildren(backlinks.data.length?el('div',{},el('h3',{},'Linked from'),el('ul',{class:'backlinks'},...backlinks.data.map(n=>el('li',{},button(n.path,()=>act(()=>open(n.path)),'ghost'))))):'');
  }
  async function load(){const data=await api(search.value?'/api/notes/search?q='+encodeURIComponent(search.value):'/api/notes',{signal});
    if(!search.value)known=data.data;
    list.replaceChildren(...data.data.map(n=>{const {name,folder}=split(n.path);
      const modified=n.modified?new Date(n.modified):null,meta=[folder,modified&&!isNaN(modified)?relativeLabel(modified):''].filter(Boolean).join(' · ');
      const item=el('li',{class:'notes-item','data-path':n.path},el('button',{type:'button',class:'notes-item-button','aria-label':n.path},
        el('span',{class:'notes-item-name'},name),meta?el('span',{class:'notes-item-folder'},meta):null,n.excerpt?el('span',{class:'notes-item-excerpt'},n.excerpt):null));
      item.firstChild.addEventListener('click',()=>act(()=>open(n.path)));return item;}));
    // An empty library is a short line in the list, not a second empty state beside the editor's.
    if(!data.data.length)list.append(el('li',{class:'notes-list-empty'},
      el('p',{},search.value?'No notes match.':'No notes yet.'),
      search.value?button('Clear search',()=>{search.value='';act(load);},'ghost'):null));
    markActive();syncDelete();
  }
  const startNote=async()=>{if(await keepText())return;selection++;showEditor(true);editing=true;savedText=null;loadedPath=null;revision=null;path.value='';content.value='';showPreview();links.replaceChildren();markActive();syncDelete();path.focus();};
  const saveNote=()=>act(async()=>{
    if(!path.reportValidity())return;
    expectOwnEvent('notes.changed');
    const result=await api('/api/notes/note',{method:'PUT',json:{path:path.value,content:content.value,expected_revision:revision}});
    revision=result.revision;loadedPath=result.path;content.value=result.content;savedText=result.content;showPreview();syncDelete();
    messages.replaceChildren();notice(messages,'Note saved.');
    history.replaceState(history.state,'',noteHref(result.path));await load();
  });
  const title=el('h2',{class:'notes-crumb'});
  const noteMenu=rowMenu([
    // Without HTTPS the browser has no clipboard, so the link is shown to copy by hand.
    {label:'Copy link',glyph:'copy',onSelect:()=>act(async()=>{const link=`[[${loadedPath.replace(/\.md$/i,'')}]]`;
      messages.replaceChildren();if(!navigator.clipboard){notice(messages,`Copy this link into another note: ${link}`);return;}
      await navigator.clipboard.writeText(link);notice(messages,'Link copied. Paste it into another note.');})},
    can('operator')?{label:'Delete note',danger:true,onSelect:()=>deleteNote()}:null,
  ],'Note actions');
  // A saved note's path is its identity, so it reads as a title (folder, then name); a new note is named in the field.
  const syncDelete=()=>{
    const saved=Boolean(loadedPath);noteMenu.hidden=!saved;title.hidden=!saved;path.hidden=saved;
    if(saved){const {name,folder}=split(loadedPath);title.replaceChildren(...folder.split('/').filter(Boolean).flatMap(part=>[el('span',{class:'notes-crumb-folder'},part),el('span',{class:'notes-crumb-sep','aria-hidden':'true'},'/')]),el('span',{},name));}
  };
  const deleteNote=()=>act(async()=>{
    if(!loadedPath||!revision){notice(messages,'Open a note before deleting it.');return;}
    const notePath=loadedPath,noteRevision=revision;
    if(!await confirmAction({title:'Delete note',target:notePath,consequence:'The Markdown file is permanently deleted from the notes folder.',confirmLabel:'Delete note',danger:true}))return;
    if(loadedPath!==notePath||revision!==noteRevision)throw new Error('The open note changed. Review it before deleting.');
    expectOwnEvent('notes.changed');
    await api('/api/notes/note?path='+encodeURIComponent(notePath)+'&expected_revision='+encodeURIComponent(noteRevision),{method:'DELETE'});
    selection++;revision=null;loadedPath=null;savedText=null;editing=false;path.value='';content.value='';showPreview();links.replaceChildren();showEditor(false);syncDelete();
    history.replaceState(history.state,'','#notes');await load();
  });

  const searchForm=el('form',{class:'notes-search',role:'search'},icon('search'),search);
  searchForm.addEventListener('submit',event=>{event.preventDefault();act(load);});
  // The library filters as you type, after a short pause.
  let searchTimer=0;
  search.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>act(load),300);},{signal});
  const newNote=el('button',{type:'button',class:'button ghost icon-only','aria-label':'New note',title:'New note'},icon('add'));
  newNote.addEventListener('click',()=>startNote(),{signal});
  const library=el('aside',{class:'notes-library','aria-label':'Notes library'},
    el('h2',{class:'sr-only'},'Your notes'),
    el('div',{class:'notes-library-top'},searchForm,can('operator')?newNote:null),list);
  // Enter in the title saves instead of reloading the page.
  const head=el('form',{class:'notes-head'},
    el('label',{class:'sr-only',for:'note-path'},'Markdown path'),path,title,
    el('div',{class:'notes-actions'},can('operator')?button('Save note',saveNote):null,noteMenu));
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
  // Otherwise the note edited most recently opens, so the workspace starts on real content.
  const linked=new URLSearchParams(location.hash.split('?')[1]||'').get('path');
  const recent=[...known].sort((a,b)=>String(b.modified||'').localeCompare(String(a.modified||'')))[0];
  if(linked||recent)await act(()=>open(linked||recent.path));
  return()=>{selection++;clearTimeout(previewTimer);controller.abort();};
}
