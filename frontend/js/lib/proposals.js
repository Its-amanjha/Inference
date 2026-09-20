// Inline repair proposal cards and repair-turn details. Every change shown here comes from the
// server-built preview; model-authored operations are never rendered as a diff.
import {el} from './html.js';
import {icon,actionIcon} from './icons.js';
import {rawDisclosure,instanceName,formatTime,plural} from './payload.js';

export const REPAIR_TOOLS=new Set(['propose_workflow_patch','propose_restore_workflow','propose_create_workflow']);
export const isRepairAction=item=>REPAIR_TOOLS.has(item?.name);
export const isRound2Action=item=>['workflow_create','execution_retry'].includes(item?.preview?.kind);
export const RATE_LIMITED_TEXT='Provider rate limited; wait about a minute, then try again.';
export const INCOMPLETE_TEXT="The model's answer was cut off. Nothing was proposed or applied.";

const STATUS={pending:['Pending','info','clock'],executing:['Applying','info','refresh'],succeeded:['Applied','success','check'],
  failed:['Not applied','error','blocked'],stale:['Stale','warning','errors'],cancelled:['Cancelled','neutral','close'],
  expired:['Expired','neutral','clock'],uncertain:['Outcome unknown','warning','errors']};

export function statusBadge(status){
  const [label,tone,glyph]=STATUS[status]||[String(status||'Unknown'),'neutral','unknown'];
  return el('span',{className:`badge decision-badge proposal-status${tone==='neutral'?'':` ${tone}`}`,'data-status':String(status||'unknown')},icon(glyph),label);
}

export function effectiveStatus(view,now=Date.now()){
  if(view.acting)return 'executing';
  const status=view.status||view.action?.status||'pending';
  return status==='pending'&&Number(view.action?.expires_at)*1000<=now?'expired':status;
}

const ERRORS={
  context_stale:'The workflow changed after this proposal was made. Nothing was written.',
  approval_stale:'The workflow changed after this proposal was made. Nothing was written.',
  workflow_active:'The workflow is active. Deactivate it, then apply the change. Nothing was written.',
  target_mismatch:'This proposal names a different workflow than the pinned repair target. Nothing was written.',
  upstream_outcome_unknown:'n8n did not confirm the write, so the change may or may not have been saved.',
  history_unavailable:'Workflow history is unavailable for this workflow, so the earlier version cannot be restored. Nothing was written.',
  repair_error_text_disabled:'An administrator turned off error text in repair conversations.',
  repair_webhook_directory_disabled:'An administrator turned off sharing other workflows’ trigger addresses with the AI.',
  invalid_patch:'The change no longer applies cleanly to the workflow. Nothing was written.',
  self_call_blocked:'This workflow would call its own trigger in a loop. Ask the AI to use the service it should call instead.',
};
export const statusForError=code=>({context_stale:'stale',approval_stale:'stale',upstream_outcome_unknown:'uncertain'})[code];
export const describeRepairError=error=>ERRORS[error?.code]||error?.message||String(error);

export const formatBytes=bytes=>{
  const value=Number(bytes);
  if(bytes===null||bytes===undefined||!Number.isFinite(value)||value<0)return null;
  return value<1024?plural(Math.round(value),'byte'):`${(value/1024).toFixed(value<10240?1:0)} KB`;
};

const TOOL_LABEL={get_workflow_context:'Workflow context',get_error_context:'Error context',propose_workflow_patch:'Change proposal',propose_restore_workflow:'Restore proposal',propose_create_workflow:'Create proposal'};
const STEP_STATUS={done:['Done','success','check'],rate_limited:['Rate limited','warning','clock'],failed:['Failed','error','blocked']};

// One ordered list per repair turn. The rate-limit sentence is shown once, by the turn footer.
export function turnSteps(steps){
  return el('ol',{className:'repair-steps','aria-label':'Steps in this turn'},steps.filter(step=>step&&typeof step==='object').map(step=>{
    const [label,tone,glyph]=STEP_STATUS[step.status]||[String(step.status||'Unknown'),'neutral','unknown'];
    const size=formatBytes(step.bytes);
    const text=step.kind==='model_request'?`Asked ${step.provider_id?`model ${step.provider_id}`:'the model'}`
      :step.kind==='context_read'?`Read ${(TOOL_LABEL[step.tool]||step.tool||'context').toLowerCase()}`:String(step.tool||step.kind||'Step');
    const message=['provider_rate_limited','provider_incomplete'].includes(step.code)||!step.message?null:String(step.message);
    return el('li',{'data-status':String(step.status||'unknown')},
      el('div',{className:'repair-step'},el('span',{className:'repair-step-text'},text,size?el('span',{className:'repair-step-size'},` · ${size}`):null),
        el('span',{className:`badge decision-badge repair-step-status${tone==='neutral'?'':` ${tone}`}`},icon(glyph),label)),
      message?el('p',{className:'repair-step-message'},message):null);
  }));
}

export function contextSent(log){
  return el('details',{className:'context-sent'},el('summary',{},'Context sent'),el('ul',{},log.filter(item=>item&&typeof item==='object').map(item=>{
    const count=Number(item.redaction_count);
    const redacted=item.redaction_count===null||item.redaction_count===undefined||!Number.isFinite(count)?null:count?`${plural(count,'value')} redacted`:'nothing redacted';
    return el('li',{},[TOOL_LABEL[item.tool]||item.tool||'Context',formatBytes(item.bytes),redacted].filter(Boolean).join(' · '));
  })));
}

// "What the AI sees": sentences from RepairContextResponse fields, then the exact response.
export function contextOverview(result){
  const count=Number(result?.redaction?.count),size=formatBytes(result?.bytes),lines=[];
  lines.push(el('p',{className:'repair-redactions'},Number.isFinite(count)&&result?.redaction?.count!==undefined?(count?`${plural(count,'value')} redacted before sending`:'No values needed redaction'):'Redaction count unavailable'));
  lines.push(el('p',{className:'repair-context-note'},`${size?`About ${size}, s`:'S'}ent as untrusted text: the AI treats it as data, never as instructions.`));
  const workflow=result?.workflow;
  if(workflow)lines.push(el('p',{},`Workflow “${workflow.name??workflow.id}” · ${plural(Array.isArray(workflow.nodes)?workflow.nodes.length:0,'node')} · ${workflow.active?'Active':'Inactive'}`));
  const error=result?.error;
  if(error)lines.push(el('p',{},error.detail==='withheld'?'Error text is withheld from the AI.':error.detail==='unavailable'?'Error details are unavailable for this execution.'
    :`Error${error.last_node?` in “${error.last_node}”`:''}: ${error.message||error.name||'no message recorded'}`));
  lines.push(rawDisclosure(result,'Raw context'));
  return lines;
}

const shortPath=path=>typeof path==='string'?path.replace(/^parameters\./,''):'';
function valueText(value){
  if(value===null||value===undefined)return 'Not set';
  if(typeof value==='string')return value===''?'Empty text':value;
  try{return JSON.stringify(value);}catch{return String(value);}
}
function values(change){
  const pair=(label,value,className)=>[el('dt',{},label),el('dd',{className},change.masked?el('span',{className:'proposal-masked'},'Masked'):valueText(value))];
  return el('dl',{className:'proposal-values'},pair('Before',change.before,'proposal-before'),pair('After',change.after,'proposal-after'));
}
const MARKER={add:'+',remove:'-',keep:' '};
const LINE_LABEL={add:'Added line: ',remove:'Removed line: '};
function codeLines(change){
  const lines=Array.isArray(change.lines)?change.lines:[];
  return el('div',{className:'proposal-code',tabindex:0,role:'region','aria-label':`Code change in ${change.node||'node'}`},
    el('pre',{className:'proposal-diff'},lines.map(line=>{
      const op=Object.hasOwn(MARKER,line?.op)?line.op:'keep';
      return el('span',{className:`diff-line diff-${op}`},el('span',{className:'diff-marker','aria-hidden':'true'},MARKER[op]),
        LINE_LABEL[op]?el('span',{className:'sr-only'},LINE_LABEL[op]):null,el('code',{},String(line?.text??'')));
    })));
}
const quoted=name=>`“${name??'Unnamed node'}”`;
const note=text=>el('p',{className:'proposal-detail'},text);
function changeItem(change){
  const kind=change?.kind;
  const item=(glyph,title,...body)=>el('li',{className:'proposal-change','data-kind':String(kind||'unknown')},el('div',{className:'proposal-change-head'},icon(glyph),el('span',{},title)),...body);
  switch(kind){
    case 'parameter': return item('edit',[el('strong',{},change.node??'Node'),` · ${shortPath(change.path)}`],values(change),change.truncated?note('Values are shortened for display.'):null);
    case 'setting': return item('settings',[el('strong',{},'Workflow setting'),` · ${change.path??''}`],values(change),change.truncated?note('Values are shortened for display.'):null);
    case 'code': return item('codelab',[el('strong',{},change.node??'Code'),` · ${shortPath(change.path)}`,change.language?el('span',{className:'proposal-language'},change.language==='python'?'Python':'JavaScript'):null],
      codeLines(change),change.truncated?note('Code is shortened for display.'):null);
    case 'node_added': return item('add',['Add node ',el('strong',{},quoted(change.node)),change.type?el('span',{className:'proposal-type'},` ${change.type}`):null]);
    case 'node_removed': return item('delete',['Remove node ',el('strong',{},quoted(change.node)),change.type?el('span',{className:'proposal-type'},` ${change.type}`):null]);
    case 'node_disabled': return item(change.disabled?'stop':'play',[change.disabled?'Disable node ':'Enable node ',el('strong',{},quoted(change.node))]);
    case 'connection_added': case 'connection_removed':
      return item('workflows',[kind==='connection_added'?'Connect ':'Disconnect ',el('strong',{},quoted(change.from_node)),' to ',el('strong',{},quoted(change.to_node))],
        note(`${change.connection_type||'main'} connection, output ${change.output_index??0} to input ${change.input_index??0}`));
    default: return item('unknown',`Change of type ${String(kind||'unknown')}. Review it in the raw server preview.`);
  }
}

const RISK={new_egress_host:'Sends data to a new host',code_node_added:'Adds a Code node',command_node_added:'Adds a node that can run system commands',
  error_workflow_changed:'Changes which error workflow runs',trigger_changed:'Changes how the workflow is triggered',node_removed:'Removes a node',
  self_webhook_call:'Calls this workflow’s own trigger (can loop forever)'};
const PRECONDITION={workflow_active:['Workflow is inactive','Workflow is active. Deactivate it before applying.']};
const RECONCILE={matches_desired:'The workflow now matches this change, so it was written.',matches_before:'The workflow still matches the version before this change. Nothing was written.',
  different:'The workflow matches neither this change nor the version before it. Something else changed it; review it in n8n.',not_found:'The workflow was not found on this instance.'};

const preconditionKey=item=>`${item?.code}:${item?.action_id||''}`;
export function preconditionsOf(view){
  let list=Array.isArray(view.action?.preview?.preconditions)?view.action.preview.preconditions.filter(item=>item&&typeof item==='object'):[];
  if(view.forceActive&&!list.some(item=>item.code==='workflow_active'))list=[...list,{code:'workflow_active',satisfied:false,tool:'deactivate_workflow',action_id:null}];
  return list.map(item=>({...item,satisfied:view.forceActive&&item.code==='workflow_active'?false:Boolean(item.satisfied)||Boolean(view.deactivated?.has(preconditionKey(item)))}));
}
export {preconditionKey};

function targetLine(action){
  const target=action?.target&&typeof action.target==='object'?action.target:action?.arguments||{};
  if(action?.preview?.kind==='workflow_create')return `New workflow “${target.workflow_name||'Unnamed workflow'}” on ${target.instance_name||instanceName(target.instance_id)}`;
  const workflow=target.workflow_name?`“${target.workflow_name}” (${target.workflow_id})`:String(target.workflow_id??'unknown');
  return `Workflow ${workflow} on ${target.instance_name||instanceName(target.instance_id)}`;
}

// Round-2 writes do not have the patch/restore undo snapshot. A lost response never offers Apply again.
function round2Outcome(view,status,control){
  const result=view.result,follow=[];
  if(status==='pending')return note('Will be created inactive. Activation is a separate approval. Nothing has been created yet.');
  if(status==='executing')return el('p',{className:'proposal-state',role:'status'},'Creating the inactive workflow. Wait for verification.');
  if(status==='succeeded'){
    if(view.canOperate&&result?.verified===true&&result.workflow_id&&result.active===false&&!view.activated)follow.push(control('Activate workflow',view.handlers.activate,'ghost'));
    return el('div',{className:'proposal-result'},
      el('p',{},result?.verified===true?'Created inactive and verified by reading the workflow back.':'Creation confirmed. Load verification details to see the new workflow ID.'),
      result?.workflow_id?note(`Workflow ${result.workflow_id}${view.activated?' · Activated by a separate approval':''}`):control('Load creation details',view.handlers.reconcile,'ghost'),
      ...follow);
  }
  if(status==='uncertain')return el('div',{className:'proposal-uncertain',role:'status'},icon('errors'),el('div',{},
    el('p',{className:'proposal-state-title'},'Creation outcome unknown'),
    el('p',{},'The workflow may have been created. Check this instance before proposing another creation. This approval will not run again.'),
    result?.workflow_id?note(`Returned workflow ID: ${result.workflow_id}. Verification is incomplete.`):null,
    view.reconcile?note('Reconcile on the instance. Loading saved details does not repeat the operation.'):null,
    control('Load saved creation details',view.handlers.reconcile,'ghost')));
  const text={stale:'Nothing was created: the instance connection changed. Start a new proposal for the intended instance.',
    failed:'Creation failed. Review the error before requesting a new proposal.',cancelled:'Creation cancelled. Nothing was created.',
    expired:'Creation proposal expired. Nothing was created.'};
  return el('p',{className:'proposal-state'},text[status]||`Creation status: ${status}. Check the instance before taking another action.`);
}

// Re-renders a card in place; focus returns to the same control when it still exists.
export function renderProposal(card,view,handlers){
  const {action}=view,preview=action?.preview&&typeof action.preview==='object'?action.preview:null;
  const round2=isRound2Action(action);
  const status=effectiveStatus(view),safe=String(action?.id||'proposal').replace(/[^A-Za-z0-9_-]/g,'_');
  const focused=card.contains(document.activeElement)?document.activeElement.dataset?.control||'card':null;
  const rawOpen=card.querySelector(':scope > .raw-payload')?.open;
  const control=(label,handler,variant='')=>{
    const node=el('button',{type:'button',className:`button${variant?` ${variant}`:''}`,'data-control':label},actionIcon(label),label);
    if(round2)node.setAttribute('aria-label',`${label}: ${targetLine(action)}; proposal ${action.id}`);
    node.addEventListener('click',async()=>{
      if(node.disabled||node.getAttribute('aria-busy')==='true')return;
      node.disabled=true;node.setAttribute('aria-busy','true');
      try{await handler();}
      catch(error){if(error?.name!=='AbortError')handlers.fail(error);}
      finally{node.disabled=false;node.removeAttribute('aria-busy');}
    });
    return node;
  };
  const preconditions=preconditionsOf(view);
  const blocked=!preview||preconditions.some(item=>!item.satisfied);
  const sections=[];
  if(status==='pending'&&preconditions.length)sections.push(el('section',{className:'proposal-section'},el('h4',{},'Before you apply'),
    el('ul',{className:'proposal-preconditions'},preconditions.map(item=>{
      const [done,todo]=PRECONDITION[item.code]||[`${item.code}: done`,`${item.code}: required before applying`];
      const row=el('li',{className:item.satisfied?'is-satisfied':'is-blocking','data-code':String(item.code)},icon(item.satisfied?'check':'blocked'),el('span',{},item.satisfied?done:todo));
      if(!item.satisfied&&item.code==='workflow_active'&&view.canOperate)row.append(control('Deactivate workflow',()=>handlers.deactivate(item),'ghost'));
      return row;
    }))));
  if(preview){
    const changes=Array.isArray(preview.changes)?preview.changes.map(changeItem):[];
    const omitted=Number(preview.changes_omitted)||0;
    if(omitted>0)changes.push(el('li',{className:'proposal-change proposal-omitted'},el('div',{className:'proposal-change-head'},icon('info'),el('span',{},`${plural(omitted,'more change')} not shown here.`))));
    if(!changes.length)changes.push(el('li',{className:'proposal-change'},el('div',{className:'proposal-change-head'},icon('info'),el('span',{},preview.diff?.changed===false?'The server found no differences from the current workflow.':'The server listed no changes.'))));
    sections.push(el('section',{className:'proposal-section'},el('h4',{},'Changes'),el('ul',{className:'proposal-changes'},changes)));
    const risks=Array.isArray(preview.risks)?preview.risks.filter(item=>item&&typeof item==='object'):[];
    if(risks.length)sections.push(el('section',{className:'proposal-section'},el('h4',{},'Risks to review'),el('ul',{className:'proposal-risks'},risks.map(risk=>
      el('li',{'data-code':String(risk.code)},icon('errors'),el('span',{},`${RISK[risk.code]||`Risk: ${risk.code}`}${risk.node?` in “${risk.node}”`:''}${risk.detail?`: ${risk.detail}`:''}`))))));
    if(preview.model_summary)sections.push(el('div',{className:'proposal-model-summary'},el('p',{className:'proposal-label'},'AI description (unverified)'),el('p',{className:'pre-wrap'},preview.model_summary)));
  }else sections.push(el('p',{className:'proposal-state'},'The server returned no preview for this proposal, so it cannot be applied here.'));

  let state;
  if(round2)state=round2Outcome({...view,handlers},status,control);
  else if(status==='pending')state=el('p',{className:'proposal-state'},'Nothing is written until you apply it.');
  else if(status==='executing')state=el('p',{className:'proposal-state',role:'status'},'Applying. Wait for the outcome before doing anything else.');
  else if(status==='stale')state=el('div',{className:'proposal-stale'},el('p',{className:'proposal-state-title'},'Nothing was written'),
    el('p',{},'The workflow changed after this proposal was made, so it can no longer be applied.'),el('div',{className:'proposal-actions'},control('Ask AI to re-read',handlers.reread,'ghost')));
  else if(status==='uncertain')state=el('div',{className:'proposal-uncertain',role:'status'},icon('errors'),el('div',{},
    el('p',{className:'proposal-state-title'},'Outcome unknown'),
    el('p',{},'n8n did not confirm the write. The change may or may not have been saved. Inference never applies it again automatically.'),
    view.reconcile?el('p',{className:'proposal-reconcile','data-state':String(view.reconcile)},RECONCILE[view.reconcile]||`Current state: ${view.reconcile}.`):null,
    el('div',{className:'proposal-actions'},control('Check current state',handlers.reconcile,'ghost'))));
  else if(status==='succeeded'){
    const result=view.result&&typeof view.result==='object'?view.result:null,active=view.activated?true:result?.active;
    const follow=[];
    if(view.canOperate)follow.push(control('Undo this change',handlers.undo,'ghost'));
    if(view.canOperate&&active!==true)follow.push(control('Activate workflow',handlers.activate,'ghost'));
    state=el('div',{className:'proposal-result'},
      el('p',{className:'proposal-state'},result?.verified===false?'Applied, but reading the workflow back did not confirm it. Check it in n8n.':result?.verified?'Applied and verified by reading the workflow back.':'Applied.'),
      result?el('p',{className:'proposal-detail'},[result.repair_id?`Repair ${result.repair_id}`:null,active===true?'Workflow is active':active===false?'Workflow is inactive':null].filter(Boolean).join(' · ')):null,
      follow.length?el('div',{className:'proposal-actions'},follow):null);
  }
  else if(status==='failed')state=el('p',{className:'proposal-state'},'Not applied. Ask the AI for a new proposal if the change is still needed.');
  else if(status==='cancelled')state=el('p',{className:'proposal-state'},'Cancelled. Nothing was written.');
  else if(status==='expired')state=el('p',{className:'proposal-state'},'Expired before it was applied. Nothing was written.');
  else state=el('p',{className:'proposal-state'},`Status: ${status}.`);

  let footer=null;
  if(status==='pending'){
    const apply=preview?.apply&&typeof preview.apply==='object'?preview.apply:{};
    const primary=control(apply.label||'Apply change',handlers.apply,apply.danger?'danger':'');
    const hint=!view.canOperate?'Only operators and administrators can apply changes.':!preview?'This proposal has no server preview.':blocked?'Finish the steps under Before you apply first.':null;
    primary.disabled=blocked||!view.canOperate;
    if(hint)primary.setAttribute('aria-describedby',`proposal-${safe}-hint`);
    footer=el('div',{className:'proposal-footer'},el('div',{className:'proposal-actions'},primary,control('Cancel',handlers.cancel,'ghost')),hint?el('p',{className:'proposal-hint',id:`proposal-${safe}-hint`},hint):null);
  }
  const feedback=el('div',{className:'proposal-feedback'},view.feedback?.error?el('div',{className:'error',role:'alert'},describeRepairError(view.feedback.error))
    :view.feedback?.text?el('div',{className:'notice',role:'status'},view.feedback.text):null);
  const raw=rawDisclosure(preview,'Raw server preview');
  if(rawOpen)raw.open=true;
  card.dataset.status=status;
  card.setAttribute('aria-labelledby',`proposal-${safe}-title`);
  card.replaceChildren(
    el('div',{className:'proposal-head'},el('h3',{className:'proposal-title',id:`proposal-${safe}-title`},preview?.summary_text||(action?.name==='propose_restore_workflow'?'Restore an earlier workflow version':'Change a workflow')),statusBadge(status)),
    el('p',{className:'proposal-target'},targetLine(action),status==='pending'&&Number.isFinite(Number(action?.expires_at))?` · Expires ${formatTime(Number(action.expires_at))}`:''),
    state,...sections,footer,feedback,raw);
  if(focused){
    const target=focused==='card'?null:[...card.querySelectorAll('[data-control]')].find(node=>node.dataset.control===focused&&!node.disabled);
    (target||card).focus({preventScroll:true});
  }
  return status;
}

export const proposalShell=id=>el('article',{className:'proposal-card','data-action-id':String(id),tabindex:-1});
