// AI prices: where step costs come from. An administrator's own price wins, then the OpenRouter catalog
// (refreshed every six hours when on), then a few bundled prices. A model called through a host with its own
// prices (Groq, Azure, Bedrock, ...) is priced only by a price of your own.
import {el,button,field,notice,table} from './html.js';
import {icon} from './icons.js';
import {rowMenu} from './row-menu.js';
import {parseTimestamp,relativePhrase} from './triage.js';

const SOURCES={override:'Yours',openrouter:'OpenRouter',bundled:'Bundled'};
const SHOWN=12;

export function priceBook({api,signal}){
  const messages=el('div',{'aria-live':'polite'});
  const status=el('p',{class:'price-sync-status'});
  const syncActions=el('div',{class:'toolbar'});
  const search=el('input',{type:'search',placeholder:'e.g. gpt-4o-mini',autocomplete:'off',spellcheck:'false'});
  const results=el('div');
  let rows=[],sync={};

  const money=value=>value===undefined||value===null||value===''?'—':`$${value}`;
  function paintSync(){
    const updated=parseTimestamp(sync.last_success||'');
    const live=rows.filter(row=>row.source==='openrouter').length;
    status.replaceChildren(sync.enabled
      ?`OpenRouter prices update every six hours${updated?`; the last update was ${relativePhrase(updated)}`:''}. ${live} models.`
        +(sync.failures?' The latest attempt failed; the prices already fetched stay in use.':'')
      :`OpenRouter prices are off${live?`. ${live} models from the last update stay in use`:''}.`);
    syncActions.replaceChildren(...[
      button(sync.enabled?'Update now':'Turn on automatic updates',()=>run(async()=>{
        await api('/api/otel/prices/sync',{method:'POST'});
        messages.replaceChildren();notice(messages,'Prices updated from OpenRouter.');await reload();
      }),'ghost'),
      sync.enabled?button('Turn off automatic updates',()=>run(async()=>{
        await api('/api/otel/prices/sync',{method:'DELETE'});
        messages.replaceChildren();notice(messages,'Automatic updates are off. The prices already fetched stay in use.');await reload();
      }),'ghost'):null].filter(Boolean));
  }
  function paintResults(){
    const needle=search.value.trim().toLowerCase();
    // With no search, your own and the bundled prices; the catalog is found by searching.
    const matches=needle?rows.filter(row=>row.model.toLowerCase().includes(needle)):rows.filter(row=>row.source!=='openrouter');
    const shown=matches.slice(0,SHOWN);
    results.replaceChildren(...[shown.length?table(['Model','Input per 1M tokens','Output per 1M tokens','Source',''],shown.map(row=>[
      el('code',{},row.model),money(row.input_per_million),money(row.output_per_million),SOURCES[row.source]||row.source||'—',
      rowMenu([{label:'Set your price',glyph:'edit',onSelect:()=>openForm(row)},
        row.source==='override'?{label:'Remove your price',danger:true,onSelect:()=>run(async()=>{
          await api('/api/otel/prices/'+encodeURIComponent(row.model),{method:'DELETE'});
          messages.replaceChildren();notice(messages,`Your price for ${row.model} is removed.`);await reload();})}:null],`Price actions for ${row.model}`),
    ]),{numeric:[1,2],className:'price-table'}):el('p',{class:'muted'},needle?'No model matches. Add your own price for it below.':'No prices of your own yet.'),
    matches.length>SHOWN?el('p',{class:'muted'},`${matches.length-SHOWN} more match. Search to narrow it down.`):null].filter(Boolean));
  }

  const model=el('input',{required:true,maxLength:256,autocomplete:'off',spellcheck:'false',class:'control-mono',placeholder:'Exact model ID, e.g. openai/gpt-oss-20b'});
  const input=el('input',{type:'text',inputmode:'decimal',required:true,pattern:'[0-9]+(\\.[0-9]+)?',placeholder:'0.15'});
  const output=el('input',{type:'text',inputmode:'decimal',required:true,pattern:'[0-9]+(\\.[0-9]+)?',placeholder:'0.60'});
  const cached=el('input',{type:'text',inputmode:'decimal',pattern:'[0-9]+(\\.[0-9]+)?',placeholder:'Optional'});
  const save=el('button',{type:'submit',class:'button'},'Save price');
  const cancel=button('Cancel',()=>{form.hidden=true;add.hidden=false;form.reset();add.focus();},'ghost');
  const form=el('form',{class:'price-form',hidden:true},
    el('div',{class:'form-row'},field('Model ID',model,{hint:'As the step reports it; the price applies to this exact ID.'}),field('Cached input per 1M tokens (USD)',cached)),
    el('div',{class:'form-row'},field('Input per 1M tokens (USD)',input),field('Output per 1M tokens (USD)',output)),
    el('div',{class:'form-actions'},cancel,save));
  const add=button('Add your price',()=>openForm(),'ghost');
  function openForm(row=null){
    form.hidden=false;add.hidden=true;
    model.value=row?.model||search.value.trim();
    input.value=row?.input_per_million||'';output.value=row?.output_per_million||'';cached.value=row?.cached_input_per_million||'';
    (row?input:model).focus();
  }
  form.addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    const json={input_per_million:input.value.trim(),output_per_million:output.value.trim(),...(cached.value.trim()?{cached_input_per_million:cached.value.trim()}:{})};
    const saved=await api('/api/otel/prices/'+encodeURIComponent(model.value.trim()),{method:'PUT',json});
    messages.replaceChildren();
    notice(messages,saved.repriced?`Price saved. ${saved.repriced} AI ${saved.repriced===1?'step was':'steps were'} priced with it.`:'Price saved. New AI steps with this model are priced with it.');
    form.hidden=true;add.hidden=false;form.reset();search.value=saved.model;await reload();
  });},{signal});

  let searchTimer=0;
  search.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(paintResults,200);},{signal});
  async function run(fn){try{await fn();}catch(error){if(!signal.aborted){messages.replaceChildren();notice(messages,error);}}}
  async function reload(){
    const result=await api('/api/otel/prices',{signal});
    rows=Array.isArray(result.data)?result.data:[];sync=result.sync||{};
    paintSync();paintResults();
  }
  const node=el('section',{class:'card form-card price-book'},
    el('div',{class:'form-card-head'},el('h2',{class:'form-card-title'},icon('settings'),'How AI steps are priced'),
      el('p',{},'AI steps are priced from their token counts: your own prices first, then the OpenRouter catalog, then a few bundled prices. A model called through a host with its own prices, such as Groq, needs a price of your own.')),
    el('div',{class:'form-card-body is-wide'},messages,status,syncActions,el('div',{class:'price-search'},field('Find a model',search)),results,add,form));
  return {node,load:()=>run(reload)};
}
