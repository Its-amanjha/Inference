import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,field,heading,table,notice,empty} from '../lib/html.js';
import {severityOf,severityPill,relativeTime,instanceName,explainHref,repairHref,TRIAGE_FILTERS,getSavedViews,saveView,deleteSavedView,registerShortcuts} from '../lib/triage.js';
import {askText,confirmAction} from '../lib/confirm.js';

const COLUMNS=[['Severity'],['When'],['Workflow / node'],['Instance'],['Count','triage-num'],['Execution'],['Actions','triage-actions-head']];
const LONG_MESSAGE=100;
const SORT_COL_KEYS = ['severity', 'when', 'workflow', 'instance', 'count', 'execution'];

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
  let currentSort = { key: 'when', direction: 'desc' };
  if (initialParams.has('sort')) {
    const raw = initialParams.get('sort');
    const dir = initialParams.get('dir') === 'asc' ? 'asc' : 'desc';
    if (SORT_COL_KEYS.includes(raw)) {
      currentSort = { key: raw, direction: dir };
    } else {
      const num = Number(raw);
      if (!Number.isNaN(num) && num >= 0 && num < SORT_COL_KEYS.length) {
        currentSort = { key: SORT_COL_KEYS[num], direction: dir };
      }
    }
  }

  const status=el('select',{},el('option',{value:'open'},'Open'),el('option',{value:'resolved'},'Resolved'));
  status.value = initialStatus;
  let rows=[],cursor=null,busy=false,queued=false,filter=initialFilter,loadedStatus=status.value;
  const selectedIds = new Set();
  const feedback=el('div',{class:'triage-feedback'}),content=el('div',{class:'triage-results',tabindex:-1});
  const count=el('p',{class:'triage-count','aria-live':'polite'}),announcer=el('p',{class:'triage-announce',role:'status','aria-live':'polite'});
  const chips=TRIAGE_FILTERS.map(([key,label])=>el('button',{type:'button',class:'button ghost triage-chip','aria-pressed':String(key===filter),onclick:()=>setFilter(key)},label));
  const next=button('Older errors',()=>load(cursor),'button ghost');next.disabled=true;
  const savedViewsContainer = el('details', { class: 'triage-saved-dropdown triage-saved-bar', role: 'group', 'aria-label': 'Saved filter views' });
  const bulkBar = el('div', { class: 'triage-bulk-strip triage-bulk-bar', role: 'region', 'aria-label': 'Bulk actions' });
  bulkBar.hidden = true;
  const contextStrip = el('div', { class: 'triage-context-strip' });

  function syncHash() {
    const params = new URLSearchParams();
    if (status.value !== 'open') params.set('status', status.value);
    if (filter !== 'all') params.set('type', filter);
    if (currentSort.key !== 'when' || currentSort.direction !== 'desc') {
      params.set('sort', currentSort.key);
      params.set('dir', currentSort.direction);
    }
    if (activeViewName) params.set('view', activeViewName);
    const qs = params.toString();
    history.replaceState(null, '', qs ? `#errors?${qs}` : '#errors');
  }

  function updateContextStrip() {
    if (selectedIds.size > 0 && can('operator') && loadedStatus === 'open') {
      contextStrip.replaceChildren(bulkBar);
    } else {
      let activeBadge = null;
      if (activeViewName) {
        const clearActiveBtn = button('Clear view', () => {
          activeViewName = '';
          syncHash();
          renderSavedViews();
          updateContextStrip();
        }, 'ghost');
        clearActiveBtn.setAttribute('aria-label', `Clear active view: ${activeViewName}`);
        activeBadge = el('div', { class: 'active-view-indicator' },
          el('span', { class: 'badge info' }, `Active view: ${activeViewName}`),
          clearActiveBtn
        );
      }

      contextStrip.replaceChildren(
        el('div', { class: 'triage-context-left' },
          count,
          el('p', { class: 'triage-scope-note' }, 'Filters apply to loaded errors only.'),
          activeBadge
        )
      );
    }
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
        const key = typeof match.filters.sort === 'string' && SORT_COL_KEYS.includes(match.filters.sort)
          ? match.filters.sort
          : (SORT_COL_KEYS[Number(match.filters.sort)] || 'when');
        currentSort = { key, direction: match.filters.dir || 'desc' };
        draw();
      }
      syncHash();
      savedViewsContainer.open = false;
      renderSavedViews();
      updateContextStrip();
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
        sort: currentSort.key,
        dir: currentSort.direction
      };
      saveView('errors', state.user?.username, name, currentFilters);
      activeViewName = name;
      syncHash();
      savedViewsContainer.open = false;
      renderSavedViews();
      updateContextStrip();
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
      savedViewsContainer.open = false;
      renderSavedViews();
      updateContextStrip();
      announcer.textContent = `View "${deletedName}" deleted.`;
    }, 'ghost');
    deleteBtn.disabled = !activeViewName;

    const summaryLabel = activeViewName ? `View: ${activeViewName}` : 'Saved views';
    const summary = el('summary', {
      class: 'button ghost triage-saved-toggle',
      'aria-label': activeViewName ? `Saved views menu, active view: ${activeViewName}` : 'Saved views menu'
    }, summaryLabel);

    const panelActions = el('div', { class: 'triage-saved-panel-actions' }, saveBtn, deleteBtn);

    const panel = el('div', { class: 'triage-saved-panel' },
      select,
      panelActions
    );

    const wasOpen = savedViewsContainer.open;
    savedViewsContainer.replaceChildren(summary, panel);
    if (wasOpen) savedViewsContainer.open = true;
  }

  status.addEventListener('change', () => {
    selectedIds.clear();
    if (activeViewName) {
      activeViewName = '';
      renderSavedViews();
    }
    syncHash();
    load();
  }, {signal});

  document.addEventListener('click', (e) => {
    if (savedViewsContainer.open && !savedViewsContainer.contains(e.target)) {
      savedViewsContainer.open = false;
    }
  }, { signal });

  savedViewsContainer.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && savedViewsContainer.open) {
      e.stopPropagation();
      savedViewsContainer.open = false;
      savedViewsContainer.querySelector('summary')?.focus();
    }
  });

  const shortcutsBtn = button('Shortcuts (?)', () => window.openShortcutsDialog ? window.openShortcutsDialog(shortcutsBtn) : null, 'button ghost');
  shortcutsBtn.setAttribute('aria-label', 'Keyboard shortcuts');
  shortcutsBtn.setAttribute('title', 'Keyboard shortcuts (?)');

  const commandStrip = el('div', { class: 'triage-command-strip' },
    el('div', { class: 'triage-command-main' },
      el('div', { class: 'triage-status-group' }, field('Status', status)),
      el('div', { class: 'triage-filters', role: 'group', 'aria-label': 'Filter loaded errors by type' }, chips)
    ),
    el('div', { class: 'triage-command-actions' },
      savedViewsContainer,
      button('Refresh', () => load(), 'button ghost'),
      shortcutsBtn
    )
  );

  container.append(
    heading('Error triage', 'Confirmed errors and evidence-based findings, newest first.'),
    commandStrip,
    contextStrip,
    announcer, feedback, content, next
  );

  const unregisterShortcuts = registerShortcuts([
    {
      key: '/',
      display: '/',
      description: 'Focus status filter',
      handler: () => status.focus()
    },
    {
      key: ['r', 'R'],
      display: 'r',
      description: 'Refresh error list',
      handler: () => load()
    },
    {
      key: ['j', 'ArrowDown'],
      display: 'j / ↓',
      description: 'Focus next error row',
      handler: () => {
        const cur = getFocusedRowIndex();
        focusRow(cur === -1 ? 0 : cur + 1);
      }
    },
    {
      key: ['k', 'ArrowUp'],
      display: 'k / ↑',
      description: 'Focus previous error row',
      handler: () => {
        const cur = getFocusedRowIndex();
        focusRow(cur === -1 ? 0 : cur - 1);
      }
    },
    {
      key: ['x', 'X'],
      display: 'x',
      description: 'Toggle selection of focused row',
      handler: () => {
        const cur = getFocusedRowIndex();
        if (cur !== -1) {
          const tr = getRowElements()[cur];
          const cb = tr?.querySelector('input.triage-row-select');
          if (cb && !cb.disabled) {
            cb.checked = !cb.checked;
            cb.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
      }
    },
    {
      key: ['e', 'E'],
      display: 'e',
      description: 'Resolve selected errors (or focused error)',
      handler: () => {
        if (can('operator') && loadedStatus === 'open') {
          if (selectedIds.size > 0) {
            bulkResolve();
          } else {
            const cur = getFocusedRowIndex();
            if (cur !== -1) {
              const tr = getRowElements()[cur];
              const resolveBtn = tr?.querySelector('button[data-action="resolve"]');
              if (resolveBtn && !resolveBtn.disabled) {
                resolveBtn.click();
              }
            }
          }
        }
      }
    }
  ], 'Error triage');

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
  function updateSelectionUI() {
    const canSelect = can('operator') && loadedStatus === 'open';
    if (!canSelect) {
      bulkBar.hidden = true;
      bulkBar.replaceChildren();
      updateContextStrip();
      return;
    }
    const visible = visibleRows();
    const visibleCount = visible.length;
    const selectedVisibleCount = visible.filter(r => selectedIds.has(r.id)).length;
    const selectAllBox = container.querySelector('.triage-select-all-box');
    if (selectAllBox) {
      if (visibleCount > 0 && selectedVisibleCount === visibleCount) {
        selectAllBox.checked = true;
        selectAllBox.indeterminate = false;
      } else if (selectedVisibleCount > 0) {
        selectAllBox.checked = false;
        selectAllBox.indeterminate = true;
      } else {
        selectAllBox.checked = false;
        selectAllBox.indeterminate = false;
      }
    }
    if (selectedIds.size > 0) {
      bulkBar.hidden = false;
      const countSpan = el('span', { class: 'triage-bulk-count', 'aria-live': 'polite' }, `${selectedIds.size} error${selectedIds.size === 1 ? '' : 's'} selected`);
      const scopeSpan = el('span', { class: 'triage-bulk-scope' }, `(${visibleCount} visible)`);
      const resolveBtn = button(`Resolve selected (${selectedIds.size})`, bulkResolve, 'button');
      resolveBtn.setAttribute('aria-label', `Resolve ${selectedIds.size} selected error${selectedIds.size === 1 ? '' : 's'}`);
      const selectAllBtn = button('Select all visible', () => {
        visibleRows().forEach(r => selectedIds.add(r.id));
        draw();
      }, 'button ghost');
      const clearBtn = button('Clear selection', () => {
        selectedIds.clear();
        draw();
      }, 'button ghost');
      bulkBar.replaceChildren(
        el('div', { class: 'triage-bulk-left' }, countSpan, scopeSpan),
        el('div', { class: 'triage-bulk-actions' }, resolveBtn, selectAllBtn, clearBtn)
      );
    } else {
      bulkBar.hidden = true;
      bulkBar.replaceChildren();
    }
    updateContextStrip();
  }

  async function bulkResolve() {
    if (!can('operator') || loadedStatus !== 'open') return;
    const toResolve = rows.filter(r => selectedIds.has(r.id));
    if (!toResolve.length) return;
    const count = toResolve.length;
    const ok = await confirmAction({
      title: 'Resolve selected errors',
      target: `${count} error${count === 1 ? '' : 's'}`,
      consequence: 'Marking these errors resolved removes them from the open triage queue.',
      confirmLabel: `Resolve ${count} error${count === 1 ? '' : 's'}`,
      danger: false
    });
    if (!ok) return;

    content.setAttribute('aria-busy', 'true');
    feedback.replaceChildren();
    announcer.textContent = `Resolving ${count} error${count === 1 ? '' : 's'}…`;

    const successes = [];
    const failures = [];

    for (const item of toResolve) {
      if (signal.aborted) return;
      try {
        await api(`/api/errors/${encodeURIComponent(item.id)}/resolve`, { method: 'POST', signal });
        successes.push(item);
      } catch (err) {
        failures.push({ item, error: err });
      }
    }

    if (signal.aborted) return;
    content.removeAttribute('aria-busy');

    if (successes.length > 0) {
      const successIds = new Set(successes.map(s => s.id));
      rows = rows.filter(r => !successIds.has(r.id));
      successes.forEach(s => selectedIds.delete(s.id));
    }

    if (failures.length > 0) {
      const failSummary = failures.map(f => {
        const name = `${f.item.workflow_name || 'Workflow'} · ${f.item.node_name || 'Node'}`;
        return `${name} (${f.error?.message || 'Failed to resolve'})`;
      }).join('; ');
      const msg = successes.length > 0
        ? `Marked ${successes.length} error${successes.length === 1 ? '' : 's'} resolved, but ${failures.length} failed: ${failSummary}.`
        : `Failed to resolve ${failures.length} error${failures.length === 1 ? '' : 's'}: ${failSummary}.`;
      const err = new Error(msg);
      err.code = 'PARTIAL_BULK_FAILURE';
      notice(feedback, err);
      announcer.textContent = `${successes.length} resolved, ${failures.length} failed.`;
    } else {
      announcer.textContent = `${successes.length} error${successes.length === 1 ? '' : 's'} marked resolved.`;
    }

    draw();
  }

  function draw(){
    if (announceTimer) { clearTimeout(announceTimer); announceTimer = null; }
    updateCount();
    const shown=visibleRows();
    if(!rows.length){
      selectedIds.clear();
      updateSelectionUI();
      content.replaceChildren(empty('Nothing to triage',loadedStatus==='open'?'No open errors for this instance selection. An empty list does not prove every execution succeeded; telemetry coverage may be incomplete.':'No resolved errors for this instance selection. Errors marked resolved from the Open list appear here.',loadedStatus==='resolved'?[button('Check again',()=>load(),'ghost'),button('Back to Open',()=>{status.value='open';selectedIds.clear();load();},'ghost')]:[button('Check again',()=>load(),'ghost')]));
      return;
    }
    if(!shown.length){
      updateSelectionUI();
      content.replaceChildren(empty('No loaded errors of this type',cursor?'Choose All, or load older errors to look further back.':'Choose All to see the other loaded errors.',[button('Show all types',()=>setFilter('all'),'ghost')]));
      return;
    }

    const canSelect = can('operator') && loadedStatus === 'open';
    const colOffset = canSelect ? 1 : 0;
    const sortColIndex = SORT_COL_KEYS.indexOf(currentSort.key) + colOffset;

    const sortable = {};
    SORT_COL_KEYS.forEach((k, idx) => {
      sortable[idx + colOffset] = k === 'when' ? 'date' : (k === 'count' ? 'number' : 'text');
    });

    let selectAllBox = null;
    let headers;
    if (canSelect) {
      selectAllBox = el('input', {
        type: 'checkbox',
        class: 'triage-select-all-box',
        'aria-label': 'Select all visible errors'
      });
      const visibleCount = shown.length;
      const selectedVisibleCount = shown.filter(r => selectedIds.has(r.id)).length;
      if (visibleCount > 0 && selectedVisibleCount === visibleCount) {
        selectAllBox.checked = true;
      } else if (selectedVisibleCount > 0) {
        selectAllBox.indeterminate = true;
      }
      selectAllBox.addEventListener('change', () => {
        const shouldSelect = selectAllBox.checked;
        shown.forEach(r => {
          if (shouldSelect) selectedIds.add(r.id);
          else selectedIds.delete(r.id);
        });
        draw();
      });
      const selectAllHead = el('label', { class: 'triage-checkbox-label', title: 'Select all visible errors' }, selectAllBox);
      headers = [selectAllHead, 'Severity', 'When', 'Workflow / node', 'Instance', 'Count', 'Execution', 'Actions'];
    } else {
      headers = ['Severity', 'When', 'Workflow / node', 'Instance', 'Count', 'Execution', 'Actions'];
    }

    const tableNode = table(
      headers,
      shown.map(row => renderRow(row, canSelect)),
      {
        className: 'triage-table',
        wrapClass: 'triage-table-wrap',
        ariaLabel: 'Errors table',
        numeric: [4 + colOffset],
        sortable,
        initialSort: { column: sortColIndex >= 0 ? sortColIndex : (1 + colOffset), direction: currentSort.direction },
        onSort: ({ column, direction }) => {
          const normalized = column - colOffset;
          const key = SORT_COL_KEYS[normalized] || 'when';
          currentSort = { key, direction };
          if (activeViewName) {
            activeViewName = '';
            renderSavedViews();
          }
          syncHash();
        }
      }
    );
    content.replaceChildren(tableNode);
    updateSelectionUI();
  }

  function renderRow(row, canSelect){
    const workflow=row.workflow_name||'Workflow name unavailable',node=row.node_name||'Workflow',name=`${workflow} · ${node}`;
    const cell=(label,className,...children)=>el('td',{class:className,'data-label':label},children);
    const actions=el('div',{class:'triage-action-group'},el('a',{class:'button ghost',href:explainHref(row)},'Explain with AI'));
    // Repair pins this error's workflow as the AI target; applying any change still needs an operator's approval.
    if(can('operator'))actions.append(el('a',{class:'button ghost',href:repairHref(row)},'Repair with AI'));
    if(loadedStatus==='open'&&can('operator')){
      const resolve=button('Mark resolved',()=>resolveRow(row,resolve,name),'button ghost');
      resolve.dataset.action='resolve';
      actions.append(resolve);
    }

    const isRowSelected = selectedIds.has(row.id);
    const cells = [];
    if (canSelect) {
      const checkbox = el('input', {
        type: 'checkbox',
        class: 'triage-row-select',
        'aria-label': `Select error: ${name}`,
        checked: isRowSelected
      });
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) {
          selectedIds.add(row.id);
        } else {
          selectedIds.delete(row.id);
        }
        checkbox.closest('tr')?.classList.toggle('is-selected', checkbox.checked);
        updateSelectionUI();
      });
      cells.push(
        el('td', { class: 'triage-select-cell', 'data-label': 'Select' },
          el('label', { class: 'triage-checkbox-label' }, checkbox)
        )
      );
    }

    const severityLabel = severityOf(row.error_type).label;
    const subjectCell = cell('Workflow / node','triage-subject',el('div',{class:'triage-subject-body'},el('div',{class:'triage-title'},el('strong',{},workflow),el('span',{class:'triage-node'},node)),message(row.error_message)));
    subjectCell._rowMeta = { workflow, node, severity: severityLabel };

    cells.push(
      {sortValue:severityLabel,content:cell('Severity','triage-severity',severityPill(row.error_type))},
      {sortValue:row.occurred_at?new Date(row.occurred_at).getTime():0,content:cell('When','triage-when',relativeTime(row.occurred_at))},
      {sortValue:`${workflow} · ${node}`,content:subjectCell},
      {sortValue:instanceName(row.instance_id),content:cell('Instance','triage-instance',instanceName(row.instance_id))},
      {sortValue:row.count??0,content:cell('Count','triage-num',row.count===null||row.count===undefined?el('span',{class:'triage-missing'},'Unavailable'):String(row.count))},
      {sortValue:row.execution_id?String(row.execution_id):'',content:cell('Execution','triage-execution',row.execution_id?String(row.execution_id):el('span',{class:'triage-missing'},'Unavailable'))},
      cell('Actions','triage-actions',actions)
    );

    return {
      cells,
      className: isRowSelected ? 'is-selected' : '',
      selected: isRowSelected
    };
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
    selectedIds.delete(row.id);
    // Remove only this row; the rest of the loaded page, filters and cursor stay as they are.
    rows=rows.filter(item=>item!==row);
    const tr=control.closest('tr');
    const siblings=tr?.isConnected?[...tr.parentElement.children]:[];
    const position=tr?siblings.indexOf(tr):-1;
    draw();
    const trList=getRowElements();
    if(trList.length){
      const target=position<0?trList[0]:trList[Math.min(position,trList.length-1)];
      (target.querySelector('[data-action=resolve]')||target.querySelector('a,button'))?.focus();
    }else{
      (content.querySelector('.triage-table-wrap')||content).focus();
    }
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
  function getRowElements() {
    return Array.from(content.querySelectorAll('tbody tr'));
  }

  let announceTimer = null;

  function getRowMeta(tr) {
    const meta = tr.querySelector('.triage-subject')?._rowMeta;
    if (meta) return meta;
    const workflow = tr.querySelector('.triage-title strong')?.textContent?.trim() || 'Workflow';
    const node = tr.querySelector('.triage-node')?.textContent?.trim() || 'Workflow';
    const severity = tr.querySelector('.triage-severity')?.textContent?.trim() || 'Error';
    return { workflow, node, severity };
  }

  function focusRow(index) {
    const trList = getRowElements();
    if (!trList.length) return;
    const clamped = Math.max(0, Math.min(index, trList.length - 1));
    let focusedTr = null;
    trList.forEach((tr, i) => {
      const isCurrent = i === clamped;
      tr.classList.toggle('is-focused', isCurrent);
      if (isCurrent) {
        tr.setAttribute('tabindex', '-1');
        tr.focus();
        tr.scrollIntoView({ block: 'nearest' });
        focusedTr = tr;
      }
    });
    if (focusedTr) {
      if (announceTimer) clearTimeout(announceTimer);
      announceTimer = setTimeout(() => {
        const meta = getRowMeta(focusedTr);
        const text = `Row ${clamped + 1} of ${trList.length}: ${meta.workflow}, ${meta.node}, ${meta.severity}`;
        if (announcer.textContent === text) {
          announcer.textContent = '';
        }
        announcer.textContent = text;
      }, 150);
    }
  }

  function getFocusedRowIndex() {
    const trList = getRowElements();
    const active = document.activeElement;
    const found = trList.findIndex(tr => tr === active || tr.contains(active));
    if (found !== -1) return found;
    return trList.findIndex(tr => tr.classList.contains('is-focused'));
  }

  await load();
  return () => {
    if (announceTimer) clearTimeout(announceTimer);
    unregisterShortcuts();
    controller.abort();
  };
}
