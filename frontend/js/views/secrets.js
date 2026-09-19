import {api as request} from '../api.js';
import {el,button,field,formCard,notice,empty,heading} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {can} from '../state.js';
import {confirmAction} from '../lib/confirm.js';

export async function render(container){
  if(!can('admin')){container.append(empty('Administrator access required','Secrets are managed by administrators.'));return;}
  const controller=new AbortController(),signal=controller.signal,messages=el('div'),list=el('div',{class:'stack'}),editor=el('div');
  let names=[],templates=[];
  let writing=false;
  const api=async(path,options={})=>{
    if(signal.aborted)throw new DOMException('View closed','AbortError');
    const write=options.method&&options.method!=='GET';
    if(write&&writing)throw new Error('An operation is already running. Wait for its outcome.');
    if(write)writing=true;
    try{return await request(path,options);}finally{if(write)writing=false;}
  };
  const act=async fn=>{try{await fn();}catch(e){if(!signal.aborted)notice(messages,e);}};
  async function load(){const result=await api('/api/admin/secrets',{signal});names=result.data;list.replaceChildren(...names.map(name=>el('div',{class:'toolbar'},el('code',{},'$'+name),button('Replace',()=>edit(name),'ghost'),button('Delete',()=>act(async()=>{if(!await confirmAction({title:'Delete secret',target:'$'+name,consequence:'The encrypted value is removed. References to this secret will stop resolving.',confirmLabel:'Delete secret',danger:true}))return;await api('/api/admin/secrets/'+encodeURIComponent(name),{method:'DELETE'});await load();}),'danger'))));if(!names.length)list.append(empty('Your vault is empty','Save a secret or choose a credential template.'));}
  function edit(existing=''){
    const name=el('input',{value:existing,required:true,pattern:'[A-Za-z_][A-Za-z0-9_]*',class:'control-mono',autocomplete:'off',spellcheck:'false',autocapitalize:'characters'}),kind=el('select',{},el('option',{value:'text'},'Text secret'),el('option',{value:'json'},'Compound JSON secret'),...templates.map(t=>el('option',{value:t.id},t.title))),inputs=el('div',{class:'form'});name.readOnly=Boolean(existing);let value,fields=[];
    function change(){inputs.replaceChildren();fields=[];const template=templates.find(t=>t.id===kind.value);if(template){for(const f of template.fields){const input=el('input',{type:f.sensitive?'password':'text',required:f.required,autocomplete:'new-password'});fields.push([f.name,input]);inputs.append(field(f.label+(f.required?'':' (optional)'),input));}}else{value=kind.value==='json'?el('textarea',{rows:6,required:true,'aria-label':'Compound JSON value',class:'control-mono',spellcheck:'false'}):el('input',{type:'password',required:true,autocomplete:'new-password'});inputs.append(field('New secret value',value));}}
    kind.addEventListener('change',change);change();if(existing){kind.replaceChildren(el('option',{value:'text'},'Text secret'),el('option',{value:'json'},'Compound JSON secret'));}
    const form=formCard({title:existing?'Replace secret':'Create secret',
      description:existing?'The new value overwrites the saved one. The previous value cannot be recovered.':'Values are encrypted on this server. After saving, only the name is shown.',
      body:[el('div',{class:'form-row'},field('Vault name',name,{hint:existing?'Names cannot change.':'Letters, numbers and underscores.'}),field('Format',kind)),inputs],
      note:[icon('lock'),'Reference it as $NAME in model, MCP and backup settings.'],
      actions:[button('Cancel',()=>editor.replaceChildren(),'ghost'),el('button',{type:'submit',class:'button'},existing?'Replace encrypted value':'Save encrypted secret')]});
    form.addEventListener('submit',e=>{e.preventDefault();act(async()=>{const template=templates.find(t=>t.id===kind.value);if(!template&&names.includes(name.value)&&!await confirmAction({title:'Replace secret value',target:'$'+name.value,consequence:'The entire saved value is overwritten. The previous value cannot be recovered.',confirmLabel:'Replace value'}))return;
      if(template)await api('/api/admin/secret-templates/'+encodeURIComponent(template.id)+'/apply',{method:'POST',json:{name:name.value,values:Object.fromEntries(fields.map(([key,input])=>[key,input.value]))}});
      else await api('/api/admin/secrets',{method:'POST',json:{name:name.value,value:kind.value==='json'?JSON.parse(value.value):value.value}});
      form.reset();editor.replaceChildren();await load();notice(messages,'Secret saved.');});});editor.replaceChildren(form);
  }
  container.append(heading('Secrets','Encrypted values stay on the server. Use $NAME references in configuration.'),messages,button('Create secret',()=>edit()),editor,el('section',{class:'card'},list));
  await act(async()=>{templates=(await api('/api/admin/secret-templates',{signal})).data;await load();});return()=>controller.abort();
}
