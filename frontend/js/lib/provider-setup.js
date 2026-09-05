import {el,button,field,notice} from './html.js';
import {api} from '../api.js';

export function providerSetup(host,onSaved,signal){
  const presets={custom:['OpenAI-compatible','','Exact model name from your provider'],openai:['OpenAI','https://api.openai.com/v1','Exact model ID'],openrouter:['OpenRouter','https://openrouter.ai/api/v1','provider/model'],groq:['Groq','https://api.groq.com/openai/v1','Exact model ID'],anthropic:['Anthropic','https://api.anthropic.com/v1','Exact model ID'],ollama:['Local Ollama','http://127.0.0.1:11434/v1','Installed model, e.g. qwen3:0.6b']};
  const kind=el('select',{},Object.entries(presets).map(([value,[label]])=>el('option',{value},label)));
  const id=el('input',{required:true,value:'main-config',pattern:'[A-Za-z0-9_-]{1,64}'});
  const base=el('input',{type:'url',required:true,placeholder:'https://your-provider.example/v1'}),model=el('input',{required:true,maxLength:256});
  const key=el('input',{type:'password',autocomplete:'new-password',placeholder:'Paste the API key supplied by your provider'});
  const ref=el('input',{placeholder:'$B_AI_API_KEY.api_key'}),keyless=el('input',{type:'checkbox'}),tools=el('input',{type:'checkbox'});
  const messages=el('div',{'aria-live':'polite'}),test=button('Test connection',async()=>{messages.replaceChildren();try{await api(`/api/assistant/providers/${encodeURIComponent(id.value.trim())}/test`,{method:'POST',signal});notice(messages,'Connected. Select this model in Chat to start a conversation.');}catch(e){notice(messages,e);}},'ghost');test.disabled=true;
  let savedRef=null,saving=false;
  const save=el('button',{type:'submit',class:'button'},'Save model');
  const keyField=field('API key',key);
  function preset(){base.value=presets[kind.value][1];model.placeholder=presets[kind.value][2];keyField.hidden=kind.value==='ollama';test.disabled=true;}
  preset();kind.addEventListener('change',preset,{signal});
  const advanced=el('details',{},el('summary',{},'Advanced: saved secrets and tool access'),el('div',{class:'stack'},field('Saved secret reference (optional)',ref),el('p',{class:'muted'},'Use this instead of pasting a key. Text secret: $NAME. API-key template: $NAME.api_key.'),field('This service does not require an API key',keyless),field('Enable tool calls',tools),el('p',{class:'muted'},'Start with chat only. Enable tools after the connection works; changes still require approval.')));
  const form=el('form',{class:'stack provider-setup'},el('h3',{},'Connect a model'),el('p',{},'Choose a service, copy the connection details it gives you, then test the connection.'),
    el('div',{class:'form-grid'},field('Provider',kind),field('Connection name',id),field('API base URL',base),field('Model ID',model)),
    el('p',{class:'muted'},'Use the base URL ending in /v1, not /chat/completions. The model must match an ID supported by your provider.'),keyField,
    el('p',{class:'muted'},'API keys are encrypted on this server. Ollama needs a running local server and an installed model; no key is needed.'),advanced,
    el('div',{class:'actions'},save,test),el('p',{class:'muted'},'Test connection sends a short message to the saved model and may use provider credits. It cannot run tools.'),messages);
  form.addEventListener('input',()=>{test.disabled=true;},{signal});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saving)return;saving=true;save.disabled=true;messages.replaceChildren();
    try{
      const name=id.value.trim(),url=base.value.trim().replace(/\/(chat\/completions|messages)\/?$/,'');base.value=url;
      if(key.value&&ref.value.trim())throw new Error('Use an API key or a saved secret reference, not both.');
      if(key.value){
        const secretName='ASSISTANT_'+name.replace(/-/g,'_')+'_'+crypto.randomUUID().replaceAll('-','');
        await api('/api/admin/secrets',{method:'POST',json:{name:secretName,value:key.value.trim()},signal});
        savedRef='$'+secretName;key.value='';ref.value=savedRef;
      }
      const reference=ref.value.trim()||savedRef;
      if(kind.value!=='ollama'&&!(kind.value==='custom'&&keyless.checked)&&!reference)throw new Error('Paste your provider API key, or open Advanced to use a saved secret reference.');
      await api('/api/assistant/config/provider',{method:'PUT',json:{id:name,config:{kind:kind.value,base_url:url,model:model.value.trim(),api_key_ref:reference||null,tools_enabled:tools.checked}},signal});
      await onSaved();test.disabled=false;notice(messages,'Model saved. Click Test connection to check the URL, key and model.');
    }catch(e){notice(messages,e);if(savedRef)notice(messages,'Your key is already saved securely. Its reference is retained under Advanced; correct the other fields and save again.');}
    finally{saving=false;save.disabled=false;}
  },{signal});
  host.append(form);
}
