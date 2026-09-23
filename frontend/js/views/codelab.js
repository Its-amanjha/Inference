import {api} from '../api.js';
import {state,can} from '../state.js';
import {el,button,heading,notice,empty} from '../lib/html.js';
import {confirmAction} from '../lib/confirm.js';
import {createIdPicker,loadInstanceWorkflows} from '../lib/repair-target.js';
import {selectMenu} from '../lib/select-menu.js';
let monacoPromise;
function loadMonaco(){if(window.monaco?.editor)return Promise.resolve(window.monaco);if(monacoPromise)return monacoPromise;monacoPromise=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Editor download timed out. The text editor remains available.')),12000);function loaded(){window.require.config({paths:{vs:'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min/vs'}});window.require(['vs/editor/editor.main'],()=>{clearTimeout(timer);resolve(window.monaco);},error=>{clearTimeout(timer);reject(error);});}if(window.require?.config){loaded();return;}const script=document.createElement('script');script.src='https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min/vs/loader.js';script.onload=loaded;script.onerror=()=>{clearTimeout(timer);reject(new Error('Editor download unavailable. Continue with the text editor.'));};document.head.append(script);});return monacoPromise;}
const LANGUAGES={javaScript:'JavaScript',python:'Python',pythonNative:'Python'};
export async function render(container){
  const controller=new AbortController(),instance=new URLSearchParams(location.hash.split('?')[1]||'').get('instance_id')||state.instanceId;container.append(heading('CodeLab',`Edit Code nodes with workflow revision checks. Instance: ${state.instances.find(item=>item.id===instance)?.name||instance||'none selected'}.`));
  if(!instance){container.append(empty('Choose an instance','Pick one in the switcher at the top of the page before loading a workflow.',[],'instances'));return()=>controller.abort();}
  // Picking a workflow loads it; Load workflow is for a pasted ID or a reload.
  const workflowPicker=createIdPicker({label:'Workflow',placeholder:'Search by name or paste an ID',overlay:true,
    load:signal=>loadInstanceWorkflows(instance,signal),
    emptyText:'No workflows found on this instance.',
    // loadWorkflow clears the loaded node only after unsaved edits are confirmed away.
    onPick:(id,known)=>{if(id&&known)loadWorkflow();},signal:controller.signal});
  const workflow=workflowPicker.input;
  workflowPicker.set(new URLSearchParams(location.hash.split('?')[1]||'').get('workflow_id')||'');
  // Switching node replaces the editor's text too, so it asks first like Load and Reload do.
  const nodes=selectMenu({label:'Code node',options:[{value:'',label:'Load a workflow first'}],onChange:async()=>{if(await keepEdits()){nodes.value=loadedNode;return;}readNode();}});nodes.disabled=true;
  const languageName=el('span',{class:'codelab-language'},'—');
  let language='javascript';
  const textarea=el('textarea',{rows:20,spellcheck:false,'aria-label':'Code editor',hidden:true}),editorHost=el('div',{className:'code-editor',hidden:true,role:'region','aria-label':'Code editor (advanced)'}),messages=el('div',{class:'codelab-messages'});editorHost.style.height='480px';
  const intro=el('p',{class:'codelab-intro muted'},'Pick a workflow to edit the code in its Code nodes. Changes are written only to inactive workflows, after a revision check.');
  let editor=null,snapshot=null,loadedWorkflow=null,loadedNode=null,nodeNames=new Map(),sequence=0,active=false,loadedCode=null;
  // Loading or reloading replaces the editor's text, so unsaved edits are only discarded on request.
  const current=()=>editor?editor.getValue():textarea.value;
  const keepEdits=async()=>loadedCode!==null&&current()!==loadedCode&&!await confirmAction({title:'Discard your edits',target:nodeNames.get(loadedNode)||loadedNode||'this node',
    consequence:'Loading replaces the code in the editor with the version saved in n8n. Your unsaved changes are lost.',confirmLabel:'Discard and load',danger:true});
  const save=button('Deploy to n8n',async()=>{
    if(!snapshot||active||!can('operator'))return;
    const nodeName=nodeNames.get(loadedNode)||loadedNode;
    if(!await confirmAction({title:'Deploy code to n8n',target:nodeName,
      consequence:`The code replaces that node's code in ${workflowPicker.getName()||`workflow ${loadedWorkflow}`}. The workflow must stay inactive, and its revision is checked before anything is written.`,
      confirmLabel:'Deploy code'}))return;
    save.disabled=true;const saving=sequence;try{const result=await api(`/api/n8n_proxy/workflows/${encodeURIComponent(loadedWorkflow)}/nodes/${encodeURIComponent(loadedNode)}?instance_id=${encodeURIComponent(instance)}`,{method:'PUT',json:{expected_hash:snapshot.workflow_hash,code:editor?editor.getValue():textarea.value}});if(saving!==sequence)return;snapshot=result;loadedCode=current();messages.replaceChildren(el('p',{role:'status'},'Code saved and verified.'));}catch(error){if(saving!==sequence)return;snapshot=null;notice(messages,error);messages.append(el('p',{},'Reload the node before another deployment. Copy your edits first.'));}finally{save.disabled=!snapshot||!can('operator')||active;}});save.disabled=true;
  const advanced=button('Use advanced editor',async()=>{try{const monaco=await loadMonaco();if(controller.signal.aborted||editor)return;editorHost.hidden=false;editor=monaco.editor.create(editorHost,{value:textarea.value,language,theme:['dark','n8n'].includes(document.documentElement.dataset.theme)?'vs-dark':'vs',automaticLayout:true,minimap:{enabled:false},fontSize:14,scrollBeyondLastLine:false});textarea.hidden=true;}catch(error){if(!controller.signal.aborted)notice(messages,error);}},'button ghost');
  // The editor and its actions appear once there is code to edit.
  const foot=el('div',{class:'toolbar codelab-foot',hidden:true},save,button('Reload node',async()=>{if(await keepEdits())return;await readNode();},'button ghost'),advanced);
  const showEditor=shown=>{intro.hidden=shown;foot.hidden=!shown;if(!editor)textarea.hidden=!shown;else editorHost.hidden=!shown;};
  function clear(){sequence++;snapshot=null;save.disabled=true;}
  // Typing a new search does not unload the code on screen: Deploy writes to the loaded workflow and node.
  async function readNode(){const current=++sequence;snapshot=null;save.disabled=true;const id=workflowPicker.getId(),node=nodes.value;if(!node)return;try{const result=await api(`/api/n8n_proxy/workflows/${encodeURIComponent(id)}/nodes/${encodeURIComponent(node)}?instance_id=${encodeURIComponent(instance)}`,{signal:controller.signal});if(current!==sequence)return;snapshot=result;loadedWorkflow=id;loadedNode=node;textarea.value=result.code;loadedCode=result.code;language=result.language==='javaScript'?'javascript':'python';languageName.textContent=language==='javascript'?'JavaScript':'Python';if(editor){editor.setValue(result.code);window.monaco.editor.setModelLanguage(editor.getModel(),language);}showEditor(true);save.disabled=!can('operator')||active;messages.replaceChildren(el('p',{className:'muted'},active?'This workflow is active. Deactivate it in Workflows, then reload before editing.':'Language follows the existing n8n node. Deployment preserves its language.'));}catch(error){if(!controller.signal.aborted)notice(messages,error);}}
  async function loadWorkflow(){if(await keepEdits())return;clear();const started=sequence;load.disabled=true;
    try{const id=workflowPicker.getId();if(!id){workflow.focus();return;}
      const result=await api(`/api/n8n_proxy/workflows/${encodeURIComponent(id)}?instance_id=${encodeURIComponent(instance)}`,{signal:controller.signal});if(started!==sequence)return;active=result.active!==false;
      const code=(result.nodes||[]).filter(node=>node.type==='n8n-nodes-base.code');
      nodeNames=new Map(code.map(node=>[node.id,node.name]));
      nodes.setOptions(code.length?code.map(node=>({value:node.id,label:node.name,glyph:'codelab',detail:LANGUAGES[node.parameters?.language]||'JavaScript'})):[{value:'',label:'No Code nodes'}]);
      nodes.disabled=!code.length;
      if(!code.length){showEditor(false);intro.hidden=true;messages.replaceChildren(empty('No Code nodes','Add a JavaScript or Python Code node to this workflow in n8n, then reload it here.',[button('Reload workflow',()=>loadWorkflow(),'ghost')],'codelab'));return;}
      await readNode();}
    catch(error){if(!controller.signal.aborted)notice(messages,error);}finally{load.disabled=false;}}
  const load=button('Load workflow',()=>loadWorkflow(),'button ghost codelab-load');
  const language_=el('div',{class:'codelab-field'},el('span',{class:'codelab-field-label'},'Language'),languageName);
  const nodeField=el('div',{class:'codelab-field'},el('span',{class:'codelab-field-label'},'Code node'),nodes.node);
  container.append(el('section',{className:'card stack codelab'},el('div',{className:'codelab-bar'},workflowPicker.node,load,nodeField,language_),intro,messages,textarea,editorHost,foot));
  if(workflowPicker.getId())loadWorkflow();
  return()=>{controller.abort();sequence++;editor?.getModel()?.dispose();editor?.dispose();};
}
