import {api} from '../api.js';
import {state,can} from '../state.js';
import {el,button,field,formCard,heading,notice,empty} from '../lib/html.js';
import {confirmAction,askText} from '../lib/confirm.js';
import {createIdPicker,loadInstanceWorkflows,loadTargetCredentials} from '../lib/repair-target.js';
import {selectMenu} from '../lib/select-menu.js';
import {icon} from '../lib/icons.js';
import {rawDisclosure,instanceName,plural,toneBadge,describeCode,section} from '../lib/payload.js';

const IN_FLIGHT=['validating','provisioning','writing','activating','restoring'];
const list=value=>Array.isArray(value)?value:[];
const quoted=value=>`“${value}”`;

// Sentences follow the backend's diagnostic codes (n8n_promote/transform.py and router.py).
function findingText(item){
  const node=item?.node?` in node ${quoted(item.node)}`:'';
  const errorWorkflow=item?.path==='settings.errorWorkflow';
  switch(item?.code){
    case 'credential_mapping_required':return [`Credential ${quoted(item.name||item.id)}${item.type?` (${item.type})`:''}${node} has no target credential mapping.`,'Add a mapping below under Credential mappings, or a vault reference such as $SERVICE_KEY.'];
    case 'workflow_mapping_required':return [errorWorkflow?`Error workflow ${item.id} has no target workflow mapping.`:`Subworkflow ${item.name?`${quoted(item.name)} (${item.id})`:item.id}${node} has no target workflow mapping.`,'Add a subworkflow ID mapping.'];
    case 'unsupported_workflow_reference':return [errorWorkflow?'The error workflow setting uses a dynamic or unsupported reference.':`A workflow reference${node}${item.path?` at ${item.path}`:''} is dynamic or unsupported.`,'Dynamic references cannot be mapped automatically.'];
    case 'target_active':return ['The target workflow is not confirmed inactive.',item.message||''];
    case 'duplicate_node_name':return [`More than one node is named ${quoted(item.node)}.`,''];
    case 'duplicate_node_id':return [`Node ${quoted(item.node)} shares its ID with another node.`,''];
    case 'credential_type_conflict':return [`Credential ${quoted(item.name||item.id)} is used with more than one type${node}.`,''];
    case 'target_credential_type_conflict':return [`Target credential ${item.target_id} is mapped for more than one credential type${node}.`,''];
    default:return [item?.message||`${describeCode(item?.code)}${node}.`,''];
  }
}
function mappedText(item){
  const node=item?.node?` in node ${quoted(item.node)}`:'';
  if(item?.kind==='credential'){
    const subject=`Credential ${quoted(item.name||item.id)}${item.type?` (${item.type})`:''}${node}`;
    return String(item.target_id).startsWith('pending-')?`${subject} will be created on the target from its vault reference.`:`${subject} maps to target credential ${item.target_id}.`;
  }
  if(item?.path==='settings.errorWorkflow')return `Error workflow ${item.name?`${quoted(item.name)} (${item.id})`:item.id} maps to target workflow ${item.target_id}.`;
  return `Subworkflow ${item?.name?`${quoted(item.name)} (${item?.id})`:item?.id}${node} maps to target workflow ${item?.target_id}.`;
}
function itemRow(glyph,text,hint=''){
  return el('li',{},icon(glyph),el('span',{},text,hint?el('span',{className:'decision-hint'},hint):null));
}
function headline(result,context){
  const diff=result.diff||{},parts=[];
  const added=list(diff.nodes_added).length,removed=list(diff.nodes_removed).length,changed=list(diff.nodes_changed).length;
  if(diff.operation==='create')parts.push(`new workflow with ${plural(added,'node')}`);
  else if(diff.operation==='update'&&diff.changed===false)parts.push('no workflow changes');
  else if(diff.operation==='update'){
    if(added)parts.push(`${plural(added,'node')} added`);
    if(removed)parts.push(`${plural(removed,'node')} removed`);
    if(changed)parts.push(`${plural(changed,'node')} changed`);
    if(diff.connections_changed)parts.push('connections changed');
    if(diff.settings_changed)parts.push('settings changed');
  }
  const blocking=list(result.missing).length+list(result.collisions).length,warnings=list(result.warnings).length;
  if(blocking)parts.push(plural(blocking,'blocking finding'));
  if(warnings)parts.push(plural(warnings,'warning'));
  return `Promote ${context.workflowLabel} from ${context.sourceName} to ${context.targetName}${parts.length?`: ${parts.join(', ')}`:''}.`;
}
function diffSection(diff,targetName){
  const value=diff&&typeof diff==='object'?diff:{};
  const added=list(value.nodes_added),removed=list(value.nodes_removed),changed=list(value.nodes_changed);
  const creating=value.operation==='create';
  const intro=creating?`Creates a new inactive workflow on ${targetName}.`:value.operation==='update'?(value.changed===false?`The existing workflow on ${targetName} already matches the source.`:`Updates the existing inactive workflow on ${targetName}.`):'The change type was not reported. Check the raw payload.';
  const group=(label,rows)=>rows.length?[el('p',{className:'decision-group-label'},`${label} (${rows.length})`),el('ul',{className:'decision-list'},rows)]:[];
  const other=[];
  if(!creating&&value.connections_changed)other.push(itemRow('workflows','Connections between nodes changed.'));
  if(!creating&&value.settings_changed)other.push(itemRow('settings','Workflow settings changed.'));
  return section('Workflow changes',null,'diff',el('p',{},intro),
    ...group(creating?'Nodes':'Added nodes',added.map(name=>itemRow('add',String(name)))),
    ...group('Removed nodes',removed.map(name=>itemRow('delete',String(name)))),
    ...group('Changed nodes',changed.map(row=>itemRow('edit',String(row?.name),list(row?.fields).length?`Fields: ${list(row.fields).join(', ')}`:''))),
    ...group('Other changes',other),
    rawDisclosure(diff));
}
function runStatus(run,targetName){
  const status=run.status;
  if(status==='deployed')return ['Deployed inactive','success',`Deployed inactive to ${targetName}. Activate separately when ready.`];
  if(status==='activated')return ['Activated','success',`Activated on ${targetName}.`];
  if(status==='restored')return ['Restored','success',`The previous workflow version was restored on ${targetName}.`];
  if(status==='rejected')return ['Not deployed','error',`Rejected before any remote write. Nothing was written to ${targetName}.`];
  if(status==='partial')return ['Partially completed','warning',`Some remote changes were made on ${targetName} before the run stopped. Reconcile before creating another preview.`];
  if(status==='uncertain')return ['Outcome uncertain','warning',`A remote write to ${targetName} may have completed. Reconcile before retrying.`];
  if(IN_FLIGHT.includes(status))return ['In progress','warning',`Stage: ${status}. If this run was interrupted, a remote write may have completed. Reconcile before retrying.`];
  return [String(status),'neutral',`Reported status: ${status}.`];
}

// Helper: strip JSON comments (// and /* */) without corrupting string literals.
function stripJsonComments(text){
  return text.replace(/("(?:[^"\\]|\\.)*")|(\/\*[\s\S]*?\*\/|\/\/[^\r\n]*)/g,(m,str)=>str||'');
}

// Helper: parse JSON tolerating comments and trailing commas.
function parseJsonWithComments(text){
  const cleaned=stripJsonComments(text).replace(/,(\s*[}\]])/g,'$1').trim();
  if(!cleaned)return {};
  const parsed=JSON.parse(cleaned);
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw new Error('Must be a JSON object.');
  return parsed;
}

function escapeRegex(str){
  return str.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
}

// Helper: update a key in a raw JSON string in place, preserving comments, formatting, and other keys.
function updateJsonText(rawText,key,newValue){
  const text=rawText.trim();
  if(!text||text==='{}'){
    if(!newValue)return '{}';
    return `{\n  ${JSON.stringify(key)}: ${JSON.stringify(newValue)}\n}`;
  }
  const keyPattern=new RegExp(`("${escapeRegex(key)}"\\s*:\\s*)(?:"(?:[^"\\\\]|\\\\.)*"|\\b[\\w.-]+\\b|null)`,'g');
  if(keyPattern.test(text)){
    if(!newValue){
      return text.replace(new RegExp(`(,\\s*)?"${escapeRegex(key)}"\\s*:\\s*(?:"(?:[^"\\\\]|\\\\.)*"|\\b[\\w.-]+\\b|null)(,\\s*)?`), (match, beforeComma, afterComma) => {
        if(beforeComma&&afterComma)return ',';
        return '';
      });
    }
    return text.replace(keyPattern,`$1${JSON.stringify(newValue)}`);
  }
  if(!newValue)return text;
  const lastBrace=text.lastIndexOf('}');
  if(lastBrace===-1)return text;
  const before=text.slice(0,lastBrace).trimEnd();
  const after=text.slice(lastBrace);
  const needsComma=before.length>0&&!before.endsWith('{')&&!before.endsWith(',');
  return `${before}${needsComma?',':''}\n  ${JSON.stringify(key)}: ${JSON.stringify(newValue)}\n${after}`;
}

export async function render(container){
  const controller=new AbortController();container.append(heading('Promote workflows','Preview exact changes, deploy inactive, then activate separately.'));
  if(!can('operator')){container.append(empty('Operator access required','Ask an administrator for permission to promote workflows.',[],'promote'));return()=>controller.abort();}
  const route=new URLSearchParams(location.hash.split('?')[1]||'');
  // A selectMenu beside its label, following the CodeLab control pattern.
  const labeledMenu=(label,menu)=>{menu.button.id=`promote-${label.toLowerCase().replace(/[^a-z]+/g,'-')}`;return el('div',{class:'codelab-field'},el('label',{class:'codelab-field-label',for:menu.button.id},label),menu.node);};
  // Promotion copies from the less-production instance by default (Staging → Production)
  // when the names make that direction clear; otherwise it keeps the first two.
  const directionDefault=()=>{
    const prod=state.instances.find(item=>/\bprod(uction)?\b/i.test(item.name||item.id));
    const staging=state.instances.find(item=>item!==prod&&/stag/i.test(item.name||item.id))
      ||state.instances.find(item=>item!==prod&&/test|dev|qa|local|playground|sandbox/i.test(item.name||item.id));
    if(prod&&staging)return {source:staging.id,target:prod.id,prod:prod.id};
    return {source:state.instances[0]?.id,target:state.instances[1]?.id,prod:prod?.id};
  };
  const defaults=directionDefault();
  const instanceOptions=()=>state.instances.map(item=>({value:item.id,label:item.name||item.id,glyph:'instances'}));
  const source=selectMenu({label:'Source instance',options:instanceOptions(),
    value:route.get('source_instance_id')||state.instanceId||defaults.source,
    onChange:()=>{
      workflowPicker.setScope(signal=>loadInstanceWorkflows(source.value,signal));
      invalidate();
      if(workflowPicker.getId())inspectWorkflow(workflowPicker.getId());
    }});
  const target=selectMenu({label:'Target instance',options:instanceOptions(),
    value:route.get('target_instance_id')||(defaults.prod&&defaults.prod!==source.value?defaults.prod:null)
      ||state.instances.find(item=>item.id!==source.value)?.id,
    onChange:()=>{
      invalidate();
      if(workflowPicker.getId())inspectWorkflow(workflowPicker.getId());
    }});
  const previewBody=el('div',{className:'stack'});
  const previewBox=el('section',{className:'card stack'},el('h2',{className:'wizard-step'},'2. Review the changes'),previewBody);
  const history=el('section',{className:'card stack'}),messages=el('div');
  let preview=null,previewContext=null,key=null,version=0;
  const runContext=new Map();

  // Promotion copies between two instances: with fewer than two, no target
  // exists and preflight can only fail.
  if(state.instances.length<2){
    container.append(empty('Connect a second instance to promote','Promotion copies a workflow from one instance to another. Connect both first.',
      can('admin')?[el('a',{class:'button ghost',href:'#instances'},'Open instances')]:[],'promote'),messages,history);
    // Past runs keep their recovery actions (check target state, restore, activate) after an instance is
    // gone; with none, the history section is left out.
    await runs();
    if(!history.querySelector('.decision-card'))history.remove();
    return()=>controller.abort();
  }

  const jsonBox=()=>el('textarea',{rows:3,value:'{}',spellcheck:'false',class:'control-mono'});
  const credential=jsonBox(),secrets=jsonBox(),subflows=jsonBox();
  credential.disabled=secrets.disabled=!can('admin');

  // Track active combobox pickers for two-way synchronization.
  const activeCredPickers=new Map();
  const activeSubflowPickers=new Map();

  function invalidate(){activeCredPickers.clear();activeSubflowPickers.clear();mappingBox.replaceChildren();resetPreview();}
  function setPrimary(step){preflight.className=step==='preflight'?'button':'button ghost';deploy.className=step==='deploy'?'button':'button ghost';}
  function resetPreview(){version++;preview=null;previewContext=null;deploy.disabled=true;setPrimary('preflight');previewBody.replaceChildren(el('p',{className:'muted'},'Check changes to review a fresh source and target snapshot.'));}

  // Source workflow combobox
  const workflowPicker=createIdPicker({
    label:'Source workflow',
    placeholder:'Type a name or paste an ID',
    clearAriaLabel:'Clear source workflow selection',
    load:signal=>loadInstanceWorkflows(source.value,signal),
    emptyText:'No workflows found on this instance.',
    emptyActions:[el('a',{class:'button ghost',href:'#instances'},'Open instances')],
    onPick:(id)=>{
      invalidate();
      if(id)inspectWorkflow(id);
    },
    signal:controller.signal
  });

  if(route.get('workflow_id')){
    workflowPicker.set(route.get('workflow_id'));
  }

  const deploy=button('Deploy inactive workflow',async()=>{
    if(!preview)return;
    const context=previewContext,operation=preview.diff?.operation;
    const consequence=operation==='create'?`A new workflow will be created on ${context.targetName} and left inactive.`:operation==='update'?`The existing workflow on ${context.targetName} will be updated and stay inactive.`:`The workflow will be written to ${context.targetName} inactive.`;
    if(!await confirmAction({title:'Deploy inactive workflow',target:`${context.workflowLabel} from ${context.sourceName} to ${context.targetName}`,
      consequence:`${consequence} Activation is a separate step, and source and target are checked again before anything is written.`,
      confirmLabel:'Deploy inactive'}))return;
    deploy.disabled=true;
    try{const result=await api('/api/n8n_promote/deploy',{method:'POST',json:{preview_id:preview.preview_id,idempotency_key:key}});runContext.set(result.id,context);previewBody.append(el('h3',{},'Deployment result'),runCard(result));preview=null;await runs();}
    catch(error){notice(messages,error);previewBody.append(el('p',{className:'muted'},'Check run history before creating another preview. A remote write may have completed.'));await runs();}
  },'ghost');deploy.disabled=true;

  const mappingBox=el('div',{className:'mapping-box'});

  function mapping(input){
    let value;
    try{
      value=parseJsonWithComments(input.value);
    }catch(err){
      throw new Error(`Invalid JSON in mappings: ${err.message}`);
    }
    if(!value||Array.isArray(value)||typeof value!=='object'||Object.values(value).some(item=>typeof item!=='string')){
      throw new Error('Mappings must be JSON objects of source IDs to target strings.');
    }
    return value;
  }

  function savedTarget(box,key){
    try{const obj=mapping(box);const value=obj[key];return typeof value==='string'?value:'';}catch{return '';}
  }

  function credentialRow(item){
    const type=String(item.type||'');
    const sourceId=String(item.id);
    const displayName=item.name||item.id;
    const picker=createIdPicker({
      label:`Target credential for “${displayName}”`,
      placeholder:'Type a name, paste an ID, or a $VAULT_REF',
      hint:'Pick an existing target credential, or type a vault reference such as $SERVICE_KEY. The secret stays encrypted.',
      clearAriaLabel:`Clear target credential for “${displayName}”`,
      clearable:true,
      load:async signal=>{
        const rows=await loadTargetCredentials(target.value,signal);
        return rows.sort((a,b)=>((b.detail===type)-(a.detail===type)));
      },
      emptyText:'No credentials found on the target instance.',
      onPick:id=>{
        if(id.startsWith('$')){
          secrets.value=updateJsonText(secrets.value,sourceId,id);
          credential.value=updateJsonText(credential.value,sourceId,'');
        }else{
          credential.value=updateJsonText(credential.value,sourceId,id);
          secrets.value=updateJsonText(secrets.value,sourceId,'');
        }
        resetPreview();
      },
      signal:controller.signal
    });

    const presetCred=savedTarget(credential,sourceId);
    const presetSecret=savedTarget(secrets,sourceId);
    if(presetCred)picker.set(presetCred);
    else if(presetSecret)picker.set(presetSecret);

    activeCredPickers.set(sourceId,picker);

    return el('div',{className:'mapping-row'},
      el('div',{className:'mapping-source'},
        el('strong',{},`Credential “${displayName}”`),
        type?el('span',{className:'muted'},` (${type})`):null,
        el('br',{}),
        el('span',{className:'picker-id'},sourceId),
        item.node?el('span',{className:'muted'},` in node ${item.node}`):null),
      picker.node);
  }

  function subworkflowRow(item){
    const sourceId=String(item.id);
    const isError=item.path==='settings.errorWorkflow';
    const rowTitle=isError?`Error workflow ${sourceId}`:`Subworkflow ${sourceId}`;
    const picker=createIdPicker({
      label:`Target workflow for ${isError?'error workflow':'subworkflow'} ${sourceId}`,
      placeholder:'Type a name or paste an ID',
      clearAriaLabel:`Clear target workflow for ${rowTitle}`,
      clearable:true,
      load:signal=>loadInstanceWorkflows(target.value,signal),
      emptyText:'No workflows found on the target instance.',
      emptyActions:[el('a',{class:'button ghost',href:'#instances'},'Open instances')],
      onPick:id=>{
        subflows.value=updateJsonText(subflows.value,sourceId,id);
        resetPreview();
      },
      signal:controller.signal
    });

    const preset=savedTarget(subflows,sourceId);
    if(preset)picker.set(preset);

    activeSubflowPickers.set(sourceId,picker);

    return el('div',{className:'mapping-row'},
      el('div',{className:'mapping-source'},
        el('strong',{},rowTitle),
        item.node?el('span',{className:'muted'},` in node ${item.node}`):null),
      picker.node);
  }

  // Two-way sync: when textareas are edited directly, update active comboboxes losslessly.
  function syncFromTextareas(){
    let credMap=null,secMap=null,flowMap=null;
    try{credMap=parseJsonWithComments(credential.value);}catch{}
    try{secMap=parseJsonWithComments(secrets.value);}catch{}
    try{flowMap=parseJsonWithComments(subflows.value);}catch{}

    if(credMap!==null||secMap!==null){
      for(const [k,picker] of activeCredPickers){
        if(credMap&&typeof credMap[k]==='string'){
          picker.set(credMap[k]);
        }else if(secMap&&typeof secMap[k]==='string'){
          picker.set(secMap[k]);
        }else if(credMap!==null&&secMap!==null){
          picker.set('');
        }
      }
    }
    if(flowMap!==null){
      for(const [k,picker] of activeSubflowPickers){
        if(typeof flowMap[k]==='string'){
          picker.set(flowMap[k]);
        }else{
          picker.set('');
        }
      }
    }
    resetPreview();
  }

  for(const input of [credential,secrets,subflows]){
    input.addEventListener('input',syncFromTextareas,{signal:controller.signal});
  }

  async function inspectWorkflow(wfId){
    if(!wfId||!source.value)return;
    try{
      const doc=await api('/api/n8n_proxy/workflows/'+encodeURIComponent(wfId)+'?instance_id='+encodeURIComponent(source.value),{signal:controller.signal});
      if(controller.signal.aborted)return;
      const creds=[],flows=[];
      const seenCreds=new Set(),seenFlows=new Set();
      for(const node of list(doc?.nodes)){
        if(node.credentials&&typeof node.credentials==='object'){
          for(const [type,item] of Object.entries(node.credentials)){
            if(item?.id&&!seenCreds.has(item.id)){
              seenCreds.add(item.id);
              creds.push({id:String(item.id),name:String(item.name||item.id),type,node:node.name});
            }
          }
        }
        if(node.type==='n8n-nodes-base.executeWorkflow'&&node.parameters?.workflowId){
          const subId=String(node.parameters.workflowId);
          if(!seenFlows.has(subId)){
            seenFlows.add(subId);
            flows.push({id:subId,node:node.name,path:'parameters.workflowId'});
          }
        }
      }
      if(doc?.settings?.errorWorkflow&&!seenFlows.has(String(doc.settings.errorWorkflow))){
        flows.push({id:String(doc.settings.errorWorkflow),path:'settings.errorWorkflow'});
      }
      renderMappingRows(creds,flows);
    }catch{
      // Fallback: preflight will return exact findings
    }
  }

  function renderMappingRows(creds,flows){
    activeCredPickers.clear();
    activeSubflowPickers.clear();
    mappingBox.replaceChildren();
    if(creds.length===0&&flows.length===0)return;
    const rows=[];
    if(can('admin')&&creds.length){
      rows.push(el('div',{className:'mapping-box-head'},el('h3',{},'Credential mappings')));
      rows.push(...creds.map(credentialRow));
    }
    if(flows.length){
      rows.push(el('div',{className:'mapping-box-head'},el('h3',{},'Subworkflow mappings')));
      rows.push(...flows.map(subworkflowRow));
    }
    mappingBox.append(...rows);
  }

  function renderMappings(result){
    const creds=list(result.missing).filter(item=>item?.code==='credential_mapping_required');
    const flows=list(result.missing).filter(item=>item?.code==='workflow_mapping_required');
    if(creds.length||flows.length){
      renderMappingRows(creds,flows);
    }
  }

  function describeSelection(){
    const id=workflowPicker.getId();
    const name=workflowPicker.getName();
    return {workflowLabel:name?quoted(name):`workflow ${id}`,sourceName:instanceName(source.value),targetName:instanceName(target.value)};
  }

  function renderPreview(result,context){
    const blocking=[...list(result.missing),...list(result.collisions)],warnings=list(result.warnings),mapped=list(result.mapped);
    renderMappings(result);
    // replaceChildren turns null into the text "null", so absent sections are dropped first.
    previewBody.replaceChildren(...[
      el('div',{className:'decision-status'},result.can_deploy?toneBadge('Ready for deployment','success'):toneBadge('Resolve the findings before deployment','error')),
      el('p',{className:'decision-headline'},headline(result,context)),
      blocking.length?section('Blocking findings',blocking.length,'blocking',el('ul',{className:'decision-list'},blocking.map(item=>itemRow('blocked',...findingText(item)))),rawDisclosure({missing:result.missing,collisions:result.collisions})):null,
      warnings.length?section('Warnings',warnings.length,'warning',el('ul',{className:'decision-list'},warnings.map(item=>itemRow('errors',item?.message||`${describeCode(item?.code)}.`))),rawDisclosure(result.warnings)):null,
      mapped.length?section('Mapped references',mapped.length,'mapped',el('ul',{className:'decision-list'},mapped.map(item=>itemRow('check',mappedText(item)))),rawDisclosure(result.mapped)):null,
      diffSection(result.diff,context.targetName),
      el('p',{className:'muted'},'Preview expires after 15 minutes. Source and target are checked again before deployment.'),deploy].filter(Boolean));
  }

  const preflight=button('Check changes',async()=>{
    messages.replaceChildren();
    // Client-side validation: catch errors in form before sending request
    if(!source.value||!target.value){
      notice(messages,new Error('Choose both a source and a target instance before running preflight.'));
      (source.value?target.button:source.button).focus();
      return;
    }
    if(source.value===target.value){
      notice(messages,new Error('Choose distinct source and target instances. Workflows cannot be promoted to the same instance.'));
      target.button.focus();
      return;
    }
    const wfId=workflowPicker.getId();
    if(!wfId){
      notice(messages,new Error('Choose or enter a source workflow to promote.'));
      workflowPicker.focus();
      return;
    }

    let cMap={},sMap={},subMap={};
    try{
      cMap=mapping(credential);
    }catch(err){
      notice(messages,err);
      credential.focus();
      return;
    }
    try{
      sMap=mapping(secrets);
    }catch(err){
      notice(messages,err);
      secrets.focus();
      return;
    }
    try{
      subMap=mapping(subflows);
    }catch(err){
      notice(messages,err);
      subflows.focus();
      return;
    }

    // Check for conflicting mappings across credential and secret maps
    const conflicts=Object.keys(cMap).filter(k=>Object.prototype.hasOwnProperty.call(sMap,k));
    if(conflicts.length>0){
      notice(messages,new Error(`Credential “${conflicts[0]}” is mapped in both Credential IDs and Secrets. Choose one mapping method.`));
      return;
    }

    preflight.disabled=true;
    const current=++version;
    preview=null;previewContext=null;deploy.disabled=true;
    try{
      const context=describeSelection();
      const result=await api('/api/n8n_promote/preflight',{method:'POST',json:{
        source_instance_id:source.value,
        target_instance_id:target.value,
        workflow_id:wfId,
        credential_map:cMap,
        secret_map:sMap,
        subworkflow_map:subMap
      }});
      if(current!==version)return;
      preview=result;
      previewContext=context;
      key=crypto.randomUUID();
      renderPreview(result,context);
      deploy.disabled=!result.can_deploy;setPrimary(result.can_deploy?'deploy':'preflight');
    }catch(error){
      notice(messages,error);
    }finally{
      preflight.disabled=false;
    }
  });

  const jsonChecks=[];
  const jsonStatus=input=>{
    const note=el('p',{className:'json-status',role:'status'});
    const check=()=>{try{parseJsonWithComments(input.value||'{}');note.replaceChildren(icon('check'),'Valid JSON');note.classList.remove('is-error');}
      catch(error){note.replaceChildren(icon('errors'),`Not valid JSON: ${error.message}`);note.classList.add('is-error');}};
    let timer=0;input.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(check,400);});jsonChecks.push(check);return note;
  };
  // The JSON editor opens as a side panel from the right, like every other panel: close at the top,
  // Done at the bottom, and a click outside the panel closes it too. The text stays when it closes.
  const jsonDone=el('button',{type:'button',className:'button'},icon('check'),'Done');
  const jsonClose=el('button',{type:'button',className:'peek-close','aria-label':'Close mappings',title:'Close mappings'},icon('close'));
  const jsonDialog=el('dialog',{className:'peek json-drawer','aria-labelledby':'mappings-json-title'},
    el('header',{className:'peek-head'},
      el('div',{className:'peek-heading'},el('h2',{className:'peek-title',id:'mappings-json-title'},'Mappings as JSON'),
        el('p',{className:'peek-meta'},'The same mappings as the pickers, as text. Use vault references such as $SERVICE, never plaintext secrets. New credential mappings need an administrator.')),
      jsonClose),
    el('div',{className:'peek-body'},
      field('Credential IDs: source → target',credential),jsonStatus(credential),
      field('Secrets: source credential → vault reference',secrets),jsonStatus(secrets),
      field('Subworkflow IDs: source → target',subflows),jsonStatus(subflows)),
    el('footer',{className:'peek-foot'},jsonDone));
  jsonDone.addEventListener('click',()=>jsonDialog.close());
  jsonClose.addEventListener('click',()=>jsonDialog.close());
  jsonDialog.addEventListener('click',event=>{if(event.target!==jsonDialog)return;const box=jsonDialog.getBoundingClientRect();
    if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)jsonDialog.close();});
  const editJson=el('button',{type:'button',className:'button ghost mapping-json-button'},icon('codelab'),'Edit as JSON');
  editJson.addEventListener('click',()=>{jsonChecks.forEach(check=>check());jsonDialog.showModal();credential.focus();});
  const rawDisclosureNode=el('div',{className:'mapping-json-row'},editJson,jsonDialog);

  const chooser=formCard({
    title:'1. Choose source and target',
    description:'Preflight compares a fresh snapshot of both instances.',
    body:[
      // One grid, so every edge lines up: source and target side by side, and under each the source
      // workflow and the mappings to the target.
      el('div',{className:'form-row promote-grid'},labeledMenu('Source instance',source),labeledMenu('Target instance',target),
        workflowPicker.node,el('div',{className:'codelab-field promote-mappings'},el('span',{className:'codelab-field-label'},'Credential and workflow mappings'),rawDisclosureNode)),
      mappingBox
    ],
    actions:[preflight]
  });

  chooser.addEventListener('submit',event=>{event.preventDefault();});
  container.append(chooser,messages,previewBox,history);

  function runCard(run){
    const context=runContext.get(run.id),targetName=instanceName(run.target_instance_id);
    const [label,tone,sentence]=runStatus(run,targetName);
    const facts=[];
    if(run.error_code)facts.push(`Reason: ${describeCode(run.error_code)}.`);
    if(run.target_workflow_id)facts.push(`Target workflow ID: ${run.target_workflow_id}`);
    if(list(run.created_credentials).length)facts.push(`${plural(run.created_credentials.length,'credential')} created on the target and retained.`);
    const card=el('article',{className:'card stack decision-card'},
      el('div',{className:'decision-head'},el('h3',{},context?`${context.workflowLabel} to ${targetName}`:`Promotion to ${targetName}`),toneBadge(label,tone)),
      el('p',{},sentence),
      facts.length?el('ul',{className:'decision-facts'},facts.map(text=>el('li',{},text))):null);
    const recovery=list(run.recovery_actions);
    if(run.status==='rejected')card.append(el('p',{className:'muted'},'Next step: run preflight again to create a fresh preview.'));
    if(recovery.includes('new_preview_with_explicit_mappings'))card.append(el('p',{className:'muted'},'Next step: reconcile, then create a new preview with explicit mappings for any retained credentials.'));
    const controls=el('div',{className:'actions'});
    for(const action of recovery){if(!['activate','restore','reconcile'].includes(action))continue;const label=action==='activate'?'Activate with binding checks':action==='restore'?'Restore previous workflow':'Check target state';const control=button(label,async()=>{
      if(action==='activate'&&!await confirmAction({title:'Activate workflow',target:`${context?context.workflowLabel:'The deployed workflow'} on ${targetName}`,
        consequence:'The target snapshot and credential bindings are checked first. Once activated, the workflow runs on its own triggers.',confirmLabel:'Activate workflow'}))return;
      if(action==='restore'&&!await confirmAction({title:'Restore previous workflow',target:targetName,
        consequence:'The previous inactive workflow is written back. Newly created workflows and credentials are not deleted.',confirmLabel:'Restore workflow'}))return;
      let body;
      if(action==='reconcile'){
        const value=await askText({title:'Check target state',label:'Known created workflow ID',
          description:'Leave this empty if the run already recorded a target workflow.',value:run.target_workflow_id||'',confirmLabel:'Reconcile'});
        if(value===null)return;
        body=value?{target_workflow_id:value}:{};
      }
      control.disabled=true;try{const result=await api(`/api/n8n_promote/runs/${encodeURIComponent(run.id)}/${action}`,{method:'POST',...(body?{json:body}:{})});card.replaceWith(runCard(result));}catch(error){notice(card,error);}finally{control.disabled=false;}
    },'button ghost',{'aria-label':`${label}, run ${run.id}`});controls.append(control);}
    if(controls.childElementCount)card.append(controls);
    return card;
  }

  async function runs(){
    try{
      const result=await api('/api/n8n_promote/runs',{signal:controller.signal});
      history.replaceChildren(el('h2',{className:'wizard-step'},'3. Deployment history'),button('Refresh runs',runs,'button ghost'),...result.data.map(runCard));
      if(!result.data.length)history.append(el('p',{className:'muted'},'No promotions yet. Each deployment, its result and any recovery steps appear here.'));
    }catch(error){
      if(!controller.signal.aborted)notice(history,error);
    }
  }

  invalidate();
  if(workflowPicker.getId())inspectWorkflow(workflowPicker.getId());
  await runs();
  return()=>controller.abort();
}
