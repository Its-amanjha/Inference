// A workflow's steps in run order, read from the node list and connections that the workflow
// list already returns (masked). Used for the one-line step strip on a workflow row and the step
// list in its details panel. Sticky notes are not steps.
import {el} from './html.js';

const NAMED={webhook:'Webhook',httpRequest:'HTTP Request',code:'Code',function:'Code',functionItem:'Code',if:'IF',switch:'Switch',
  set:'Edit Fields',merge:'Merge',splitInBatches:'Loop',scheduleTrigger:'Schedule',cron:'Schedule',manualTrigger:'Manual',
  respondToWebhook:'Respond to Webhook',executeWorkflow:'Run sub-workflow',executeWorkflowTrigger:'Sub-workflow trigger',
  errorTrigger:'Error trigger',wait:'Wait',noOp:'No operation',filter:'Filter',aggregate:'Aggregate',splitOut:'Split out',
  googleSheets:'Google Sheets',gmail:'Gmail',slack:'Slack',telegram:'Telegram',postgres:'Postgres',mySql:'MySQL',
  agent:'AI Agent',lmChatOpenAi:'OpenAI chat model',openAi:'OpenAI',chainLlm:'LLM chain'};

// "n8n-nodes-base.httpRequest" -> "HTTP Request"; unknown types become readable words.
export function nodeKind(type){
  const tail=String(type||'').split('.').pop()||'';
  if(NAMED[tail])return NAMED[tail];
  const words=tail.replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/[_-]+/g,' ').trim();
  return words?words[0].toUpperCase()+words.slice(1):'Node';
}

const isNote=node=>/stickyNote$/i.test(String(node?.type||''));
const isTrigger=node=>/trigger$/i.test(String(node?.type||''))||/\.webhook$/i.test(String(node?.type||''));

// Breadth-first from the triggers (or from nodes nothing points at), then any node not reached.
export function flowOrder(workflow){
  const nodes=(Array.isArray(workflow?.nodes)?workflow.nodes:[]).filter(node=>node&&typeof node.name==='string'&&!isNote(node));
  if(!nodes.length)return [];
  const byName=new Map(nodes.map(node=>[node.name,node]));
  const next=new Map(),incoming=new Set();
  for(const [source,outputs] of Object.entries(workflow?.connections||{})){
    const targets=[];
    for(const group of Object.values(outputs||{}))for(const branch of Array.isArray(group)?group:[])for(const link of Array.isArray(branch)?branch:[])
      if(link&&byName.has(link.node)&&!targets.includes(link.node))targets.push(link.node);
    next.set(source,targets);targets.forEach(name=>incoming.add(name));
  }
  const starts=nodes.filter(isTrigger);
  const queue=(starts.length?starts:nodes.filter(node=>!incoming.has(node.name))).map(node=>node.name);
  const seen=new Set(),order=[];
  while(queue.length){const name=queue.shift();if(seen.has(name)||!byName.has(name))continue;seen.add(name);order.push(byName.get(name));queue.push(...(next.get(name)||[]));}
  for(const node of nodes)if(!seen.has(node.name))order.push(node);
  return order.map(node=>({name:node.name,kind:nodeKind(node.type),trigger:isTrigger(node),disabled:node.disabled===true,branches:(next.get(node.name)||[]).length}));
}

// One line: the first few step names joined by chevrons, then "+N more".
export function flowStrip(workflow,limit=5){
  const steps=flowOrder(workflow);
  if(!steps.length)return null;
  const shown=steps.slice(0,limit),rest=steps.length-shown.length;
  // Screen readers get every step as one sentence; the visual chips are hidden from them.
  return el('p',{class:'flow-strip'},el('span',{class:'sr-only'},`${steps.length} step${steps.length===1?'':'s'}: ${steps.map(step=>step.name).join(', ')}`),
    shown.map((step,index)=>[index?el('span',{class:'flow-sep','aria-hidden':'true'},'›'):null,
      el('span',{class:`flow-step${step.disabled?' is-disabled':''}`,'aria-hidden':'true',title:`${step.name} · ${step.kind}${step.disabled?' · disabled':''}`},step.name)]),
    rest>0?el('span',{class:'flow-more','aria-hidden':'true'},`+${rest}`):null);
}

// Every step with its kind, in run order, for the details panel.
export function flowList(workflow){
  const steps=flowOrder(workflow);
  if(!steps.length)return el('p',{class:'muted'},'This workflow has no steps yet.');
  return el('ol',{class:'flow-list'},steps.map(step=>el('li',{class:step.disabled?'is-disabled':undefined},
    el('span',{class:'flow-list-name'},step.name),
    el('span',{class:'flow-list-kind'},[step.kind,step.trigger?'starts the workflow':null,step.branches>1?`splits into ${step.branches}`:null,step.disabled?'disabled':null].filter(Boolean).join(' · ')))));
}
