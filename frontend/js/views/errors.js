import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,field,heading,table,notice,empty} from '../lib/html.js';
import {severityOf,severityPill,relativeTime,instanceName,explainHref,repairHref,TRIAGE_FILTERS,getSavedViews,saveView,deleteSavedView} from '../lib/triage.js';
import {askText,confirmAction} from '../lib/confirm.js';

const COLUMNS=[['Severity'],['When'],['Workflow / node'],['Instance'],['Count','triage-num'],['Execution'],['Actions','triage-actions-head']];
const LONG_MESSAGE=100;

// Message text stays a text node; long messages keep the full text in a disclosure.
function message(text) {
  const value=text===null||text===undefined?'':String(text);
  if(!value)return el('p',{class:'triage-message triage-missing'},'Message unavailable');
  const clamped=el('p',{class:'triage-message'},value);
  if(value.length<=LONG_MESSAGE&&!value.includes('\n'))return clamped;
  return [clamped,el('details',{class:'triage-full'},el('summary',{},'Full message'),el('p',{},value))];
}

export async function render(container){
  const controller=new AbortController(),signal=controller.signal,instance=state.instanceId;
  const hashQuery = location.hash.slice(1).split('?')[1] || '';
  const initialParams = new URLSearchParams(hashQuery);
  const initialStatus = initialParams.get('status') === 'resolved' ? 'resolved' : 'open';
  const initialFilter = TRIAGE_FILTERS.some(([k]) => k === initialParams.get('type')) ? initialParams.get('type') : 'all';
  let activeViewName = initialParams.get('view') || '';
  let currentSort = { column: 1, direction: 'desc' };
  if (initialParams.has('sort')) {
    const col = Number(initialParams.get('sort'));
    const dir = initialParams.get('dir') === 'asc' ? 'asc' : 'desc';
    if (!Number.isNaN(col)) currentSort = { column: col, direction: dir };
  }

  const status=el('select',{},el('option',{value:'open'},'Open'),el('option',{value:'resolved'},'Resolved'));
  status.value = initialStatus;
  let rows=[],cursor=null,busy=false,queued=false,filter=initialFilter,loadedStatus=status.value;
  const feedback=el('div',{class:'triage-feedback'}),content=el('div',{class:'triage-results',tabindex:-1});
  const count=el('p',{class:'triage-count','aria-live':'polite'}),announcer=el('p',{class:'triage-announce',role:'status'});
  const chips=TRIAGE_FILTERS.map(([key,label])=>el('button',{type:'button',class:'button ghost triage-chip','aria-pressed':String(key===filter),onclick:()=>setFilter(key)},label));
  const next=button('Older errors',()=>load(cursor),'button ghost');next.disabled=true;
  const savedViewsContainer = el('div', { class: 'triage-saved-bar', role: 'region', 'aria-label': 'Saved filter views' });

  function syncHash() {
    const params = new URLSearchParams();
    if (status.value !== 'open') params.set('status', status.value);
    if (filter !== 'all') params.set('type', filter);
    if (currentSort.column !== 1 || currentSort.direction !== 'desc') {
      params.set('sort', String(currentSort.column));
      params.set('dir', currentSort.direction);
    }
    if (activeViewName) params.set('view', activeViewName);
    const qs = params.toString();
    history.replaceState(null, '', qs ? `#errors?${qs}` : '#errors');
  }

  function renderSavedViews() {
    const views = getSavedViews('errors', state.user?.username);
    const select = el('select', { 'aria-label': 'Select saved filter view' });
    select.append(el('option', { value: '' }, views.length ? 'Saved views…' : 'No saved views'));
    for (const v of views) {
      select.append(el('option', { value: v.name, selected: v.name === activeViewName }, v.name));
    }

    select.addEventListener('change', () => {
      const selectedName = select.value;
      if (!selectedName) return;
      const match = views.find(v => v.name === selectedName);
      if (!match) return;
      activeViewName = match.name;
      if (match.filters.status && match.filters.status !== status.value) {
        status.value = match.filters.status;
        load();
      }
      if (match.filters.type) {
        setFilter(match.filters.type, false);
      }
      if (match.filters.sort !== undefined) {
        currentSort = { column: match.filters.sort, direction: match.filters.dir || 'desc' };
        draw();
      }
      syncHash();
      renderSavedViews();
      announcer.textContent = `Applied saved view "${match.name}".`;
    });

    const saveBtn = button('Save view', async () => {
      const name = await askText({
        title: 'Save current filter view',
        label: 'View name',
        description: 'Store this combination of status, type filter, and sorting in your browser.',
        confirmLabel: 'Save view',
        placeholder: 'e.g. Open silent failures'
      });
      if (!name) return;
      const currentFilters = {
        status: status.value,
        type: filter,
        sort: currentSort.column,
        dir: currentSort.direction
      };
      saveView('errors', state.user?.username, name, currentFilters);
      activeViewName = name;
      syncHash();
      renderSavedViews();
      announcer.textContent = `View "${name}" saved.`;
    }, 'ghost');

    const deleteBtn = button('Delete view', async () => {
      if (!activeViewName) return;
      const ok = await confirmAction({
        title: 'Delete saved view',
        target: activeViewName,
        consequence: 'This view will be permanently removed from your browser storage.',
        confirmLabel: 'Delete view',
        danger: true
      });
      if (!ok) return;
      deleteSavedView('errors', state.user?.username, activeViewName);
      const deletedName = activeViewName;
      activeViewName = '';
      syncHash();
      renderSavedViews();
      announcer.textContent = `View "${deletedName}" deleted.`;
    }, 'ghost');
    deleteBtn.disabled = !activeViewName;

    const activeBadge = activeViewName
      ? el('div', { class: 'active-view-indicator' },
          el('span', { class: 'badge info' }, `Active view: ${activeViewName}`),
          button('Clear view', () => {
            activeViewName = '';
            syncHash();
            renderSavedViews();
          }, 'ghost')
        )
      : null;

    savedViewsContainer.replaceChildren(
      el('div', { class: 'saved-views-controls' }, select, saveBtn, deleteBtn),
      activeBadge
    );
  }

  status.addEventListener('change', () => {
    if (activeViewName) {
      activeViewName = '';
      renderSavedViews();
    }
    syncHash();
    load();
  }, {signal});

  container.append(heading('Error triage','Confirmed errors and evidence-based findings, newest first.'),
    el('div',{class:'toolbar'},field('Status',status),button('Refresh',()=>load(),'button ghost')),
    savedViewsContainer,
    el('div',{class:'triage-filterbar'},el('div',{class:'triage-filters',role:'group','aria-label':'Filter loaded errors by type'},chips),
      el('div',{class:'triage-summary'},count,el('p',{},'Filters apply to loaded errors only.'))),
    announcer,feedback,content,next);

  renderSavedViews();

  const visibleRows=()=>filter==='all'?rows:rows.filter(row=>severityOf(row.error_type).kind===filter);
  function updateCount(){count.textContent=rows.length?`Showing ${visibleRows().length} of ${rows.length} loaded`:'No errors loaded';}
  function setFilter(key, clearActive = true){
    filter=key;
    if (clearActive && activeViewName) {
      activeViewName = '';
      renderSavedViews();
    }
    chips.forEach((chip,index)=>chip.setAttribute('aria-pressed',String(TRIAGE_FILTERS[index][0]===key)));
    syncHash();
    draw();
  }
  function draw(){
    updateCount();
    const shown=visibleRows();
    if(!rows.length){content.replaceChildren(empty('Nothing to triage',loadedStatus==='open'?'No open errors for this instance selection. An empty list does not prove every execution succeeded; telemetry coverage may be incomplete.':'No resolved errors for this instance selection. Errors marked resolved from the Open list appear here.',loadedStatus==='resolved'?[button('Check again',()=>load(),'ghost'),button('Back to Open',()=>{status.value='open';load();},'ghost')]:[button('Check again',()=>load(),'ghost')]));return;}
    if(!shown.length){content.replaceChildren(empty('No loaded errors of this type',cursor?'Choose All, or load older errors to look further back.':'Choose All to see the other loaded errors.',[button('Show all types',()=>setFilter('all'),'ghost')]));return;}
    content.replaceChildren(table(
      ['Severity','When','Workflow / node','Instance','Count','Execution','Actions'],
      shown.map(renderRowCells),
      {
        className:'triage-table',
        wrapClass:'triage-table-wrap',
        ariaLabel:'Errors table',
        numeric:[4],
        sortable:{0:'text',1:'date',2:'text',3:'text',4:'number',5:'text'},
        initialSort:currentSort,
        onSort:({column,direction})=>{
          currentSort={column,direction};
          if(activeViewName){
            activeViewName='';
            renderSavedViews();
          }
          syncHash();
        }
      }
    ));
  }
  function renderRowCells(row){
    const workflow=row.workflow_name||'Workflow name unavailable',node=row.node_name||'Workflow',name=`${workflow} · ${node}`;
    const cell=(label,className,...children)=>el('td',{class:className,'data-label':label},children);
    const actions=el('div',{class:'triage-action-group'},el('a',{class:'button ghost',href:explainHref(row)},'Explain with AI'));
    // Repair pins this error's workflow as the AI target; applying any change still needs an operator's approval.
    if(can('operator'))actions.append(el('a',{class:'button ghost',href:repairHref(row)},'Repair with AI'));
    if(loadedStatus==='open'&&can('operator')){
      const resolve=button('Mark resolved',()=>resolveRow(row,resolve,name),'button ghost');
      resolve.dataset.action='resolve';actions.append(resolve);
    }
    return [
      {sortValue:severityOf(row.error_type).label,content:cell('Severity','triage-severity',severityPill(row.error_type))},
      {sortValue:row.occurred_at?new Date(row.occurred_at).getTime():0,content:cell('When','triage-when',relativeTime(row.occurred_at))},
      {sortValue:`${workflow} · ${node}`,content:cell('Workflow / node','triage-subject',el('div',{class:'triage-subject-body'},el('div',{class:'triage-title'},el('strong',{},workflow),el('span',{class:'triage-node'},node)),message(row.error_message)))},
      {sortValue:instanceName(row.instance_id),content:cell('Instance','triage-instance',instanceName(row.instance_id))},
      {sortValue:row.count??0,content:cell('Count','triage-num',row.count===null||row.count===undefined?el('span',{class:'triage-missing'},'Unavailable'):String(row.count))},
      {sortValue:row.execution_id?String(row.execution_id):'',content:cell('Execution','triage-execution',row.execution_id?String(row.execution_id):el('span',{class:'triage-missing'},'Unavailable'))},
      cell('Actions','triage-actions',actions)
    ];
  }
  async function resolveRow(row,control,name){
    control.disabled=true;feedback.replaceChildren();announcer.textContent='';
    try{await api(`/api/errors/${encodeURIComponent(row.id)}/resolve`,{method:'POST'});}
    catch(error){
      if(signal.aborted)return;
      control.disabled=false;
      notice(feedback,Object.assign(new Error(`Could not mark ${name} resolved. ${error.message}`),{status:error.status,code:error.code}));
      control.focus();return;
    }
    if(signal.aborted)return;
    // Remove only this row; the rest of the loaded page, filters and cursor stay as they are.
    rows=rows.filter(item=>item!==row);
    draw();
    announcer.textContent=`${name} marked resolved.`;
  }
  async function load(before=null){
    if(busy){if(!before)queued=true;return;}
    busy=true;next.disabled=true;content.setAttribute('aria-busy','true');
    const requested=status.value;
    try{
      const params=new URLSearchParams({status:requested,limit:String(pageSize())});
      if(instance)params.set('instance_id',instance);
      if(before)params.set('before',before);
      const result=await api('/api/errors?'+params,{signal});
      if(!before){feedback.replaceChildren();announcer.textContent='';}
      loadedStatus=requested;rows=before?rows.concat(result.data):result.data;cursor=result.next_cursor;
      draw();
    }catch(error){if(!signal.aborted)notice(feedback,error);}
    finally{
      busy=false;content.removeAttribute('aria-busy');next.disabled=!cursor;
      if(queued&&!signal.aborted){queued=false;load();}
    }
  }
  await load();return()=>controller.abort();
}
