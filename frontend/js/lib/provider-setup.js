import {el,button,field,formCard,notice} from './html.js';
import {icon} from './icons.js';
import {api} from '../api.js';

export function providerSetup(host,onSaved,signal){
  const presets={custom:['OpenAI-compatible','','Exact model name from your provider'],openai:['OpenAI','https://api.openai.com/v1','Exact model ID'],openrouter:['OpenRouter','https://openrouter.ai/api/v1','provider/model'],groq:['Groq','https://api.groq.com/openai/v1','Exact model ID'],anthropic:['Anthropic','https://api.anthropic.com/v1','Exact model ID'],ollama:['Local Ollama','http://127.0.0.1:11434/v1','Installed model, e.g. qwen2.5:3b']};
  const kind=el('select',{},Object.entries(presets).map(([value,[label]])=>el('option',{value},label)));
  const id=el('input',{required:true,value:'main-config',pattern:'[A-Za-z0-9_-]{1,64}',autocomplete:'off',spellcheck:'false'});
  // Once someone names the connection themselves, changing the provider must not overwrite it.
  id.addEventListener('input',()=>{id.dataset.edited='true';});
  const base=el('input',{type:'url',required:true,placeholder:'https://your-provider.example/v1',autocomplete:'off',spellcheck:'false'}),model=el('input',{required:true,maxLength:256,autocomplete:'off',spellcheck:'false'});
  const key=el('input',{type:'password',autocomplete:'new-password',placeholder:'Paste the API key supplied by your provider'});
  const ref=el('input',{placeholder:'$B_AI_API_KEY.api_key',class:'control-mono',autocomplete:'off',spellcheck:'false'}),keyless=el('input',{type:'checkbox'}),tools=el('input',{type:'checkbox'});
  // A local endpoint needs explicit egress permission: loopback and private addresses are not global,
  // so backend/net.py rejects them unless a CIDR entry covers them. Without this the prefilled Ollama
  // URL fails with provider_configuration_invalid and the form gives no clue why.
  const EGRESS_VAR='INFERENCE_EGRESS_ALLOW_CIDRS';
  const egress=el('p',{class:'field-hint','aria-live':'polite',hidden:true});
  let allowList=null;  // null until the deployment answers; non-admins never learn it.
  const ipv4=text=>{
    const parts=String(text).split('.');
    if(parts.length!==4)return null;
    let value=0;
    for(const part of parts){if(!/^\d{1,3}$/.test(part)||Number(part)>255)return null;value=value*256+Number(part);}
    return value;
  };
  // The address the server must be allowed to reach, and the CIDR that permits exactly it. Only literal
  // loopback/private addresses and localhost get a hint: other hostnames resolve at connection time.
  const egressTarget=value=>{
    try{
      const host=new URL(value).hostname.replace(/^\[|\]$/g,'');
      if(host==='localhost')return {kind:'loopback',address:'127.0.0.1',cidr:'127.0.0.1/32'};
      if(host==='::1')return {kind:'loopback',address:'::1',cidr:'::1/128'};
      if(ipv4(host)===null)return null;
      if(/^127\./.test(host))return {kind:'loopback',address:host,cidr:`${host}/32`};
      if(/^10\./.test(host)||/^192\.168\./.test(host)||/^172\.(1[6-9]|2\d|3[01])\./.test(host))
        return {kind:'private',address:host,cidr:`${host}/32`};
      return null;
    }catch{return null;}
  };
  // Mirrors backend/net.py: an allowlist entry permits the address when the address lies inside its network.
  const permits=(entry,target)=>{
    if(target.address==='::1')return entry==='::1/128'||entry==='::/0';
    const [network,bits]=String(entry).split('/'),start=ipv4(network),size=Number(bits);
    if(start===null||!Number.isInteger(size)||size<0||size>32)return false;
    const block=2**(32-size);
    return Math.floor(ipv4(target.address)/block)===Math.floor(start/block);
  };
  function paintEgress(){
    const target=egressTarget(base.value.trim());
    egress.hidden=!target;
    if(!target)return;
    const where=target.kind==='loopback'?'loopback':`the private address ${target.address}`;
    egress.textContent=allowList===null
      ? `A ${target.kind} address needs egress permission: start the server with ${EGRESS_VAR}=${target.cidr}.`
      : allowList.some(entry=>permits(entry,target))
        ? `This deployment already permits egress to ${where}, so a model server there is reachable.`
        : `This deployment does not permit egress to ${where} yet. Restart the server with ${EGRESS_VAR}=${target.cidr}, otherwise saving succeeds but the connection fails.`;
  }
  // Admin-only and best effort: a denied or failed lookup leaves the static instruction in place.
  api('/api/admin/settings',{signal}).then(result=>{
    allowList=Array.isArray(result?.allow_cidrs)?result.allow_cidrs:[];
    paintEgress();
  },()=>{});
  const messages=el('div',{class:'form-messages','aria-live':'polite'}),test=button('Test connection',async()=>{messages.replaceChildren();try{await api(`/api/assistant/providers/${encodeURIComponent(id.value.trim())}/test`,{method:'POST',signal});notice(messages,'Connected. Select this model in Chat to start a conversation.');}catch(e){notice(messages,e);}},'ghost');test.disabled=true;
  let savedRef=null,saving=false;
  const save=el('button',{type:'submit',class:'button'},'Save model');
  const keyField=field('API key',key,{hint:'Encrypted on this server. Local Ollama does not need a key.'});
  // A known service already knows its own endpoint, so the URL only takes space in the main form for
  // "OpenAI-compatible"; the disclosure opens itself there, which is also where the browser check fills it.
  // Declared before preset() runs: it sets advanced.open on the first call.
  const advanced=el('details',{class:'form-disclosure'},el('summary',{},icon('chevron'),'Advanced: endpoint, saved secrets and tool access'),el('div',{},
    el('div',{},field('API base URL',base,{hint:'Use the base URL ending in /v1, not /chat/completions.'}),egress),
    field('Saved secret reference (optional)',ref,{hint:'Use this instead of pasting a key. Text secret: $NAME. API-key template: $NAME.api_key.'}),
    field('This service does not require an API key',keyless),
    field('Enable tool calls',tools,{hint:'Start with chat only. Enable tools after the connection works; changes still require approval.'})));
  function preset(){
    base.value=presets[kind.value][1];model.placeholder=presets[kind.value][2];
    keyField.hidden=kind.value==='ollama';test.disabled=true;
    // Only a custom endpoint has to be typed; the rest arrive filled in, so the disclosure stays shut.
    // A local endpoint is the exception: its egress requirement lives in Advanced, so open it there.
    advanced.open=kind.value==='custom'||kind.value==='ollama';
    if(!id.dataset.edited)id.value=kind.value==='custom'?'main-config':kind.value;
    paintEgress();
  }
  preset();kind.addEventListener('change',preset,{signal});
  base.addEventListener('input',paintEgress,{signal});
  const form=formCard({title:'Connect a model',heading:'h3',flat:true,className:'provider-setup',
    description:'Choose a service, copy the connection details it gives you, then test the connection.',
    body:[el('div',{class:'form-row'},field('Connection name',id,{hint:'Names this connection in the model list.'}),field('Provider',kind)),
      el('div',{class:'form-row'},field('Model ID',model,{hint:'Must match an ID supported by your provider.'}),keyField),
      advanced,messages],
    note:'Test connection sends a short message to the saved model and may use provider credits. It cannot run tools.',
    actions:[test,save]});
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
