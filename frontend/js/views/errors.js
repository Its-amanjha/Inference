import {api} from '../api.js';
import {state,can,pageSize} from '../state.js';
import {el,button,heading,table,notice,empty} from '../lib/html.js';
import {icon} from '../lib/icons.js';
import {severityOf,severityPill,relativeTime,parseTimestamp,instanceName,explainHref,repairHref,n8nUrl,TRIAGE_FILTERS,getSavedViews,saveView,deleteSavedView,registerShortcuts} from '../lib/triage.js';
import {askText,confirmAction} from '../lib/confirm.js';
import {rowMenu} from '../lib/row-menu.js';
import {segmented} from '../lib/select-menu.js';
import {openPeek,peekFacts,peekSection,peekCopy,closePeek} from '../lib/peek.js';

const SORT_COL_KEYS = ['severity', 'workflow', 'instance', 'count', 'when'];
// Severity sorts by weight, not by its label: execution errors, silent failures, then warnings.
const SEVERITY_RANK = {execution:4, silent:3, zero_output:2, other:1};
const rowParts = row => {const workflow=row.workflow_name||'Workflow name unavailable',node=row.node_name||'Workflow';return {workflow,node,name:`${workflow} · ${node}`};};
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

// The row shows the first line of the message; the details panel shows all of it.
function message(text) {
  const value=text===null||text===undefined?'':String(text);
  return value?el('p',{class:'triage-message'},value):el('p',{class:'triage-message triage-missing'},'Message unavailable');
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

  // Open and Resolved switch often and are only two, so they sit side by side instead of in a dropdown.
  const status=segmented({label:'Status',options:[{value:'open',label:'Open',glyph:'errors'},{value:'resolved',label:'Resolved',glyph:'check'}],value:initialStatus,onChange:()=>statusChanged()});
  status.value = initialStatus;
  let rows=[],cursor=null,busy=false,queued=false,filter=initialFilter,loadedStatus=status.value;
  const selectedIds = new Set();
  const feedback=el('div',{class:'triage-feedback'}),content=el('div',{class:'triage-results',tabindex:-1});
  const count=el('p',{class:'triage-count','aria-live':'polite'}),announcer=el('p',{class:'triage-announce',role:'status','aria-live':'polite'});
  const chips=TRIAGE_FILTERS.map(([key,label])=>el('button',{type:'button',class:'button ghost triage-chip','aria-pressed':String(key===filter),onclick:()=>setFilter(key)},label));
  const next=button('Older errors',()=>load(cursor),'button ghost');next.hidden=true;
  const savedViewsContainer = el('details', { class: 'triage-saved-dropdown triage-saved-bar', role: 'group', 'aria-label': 'Saved filter views' });
  const bulkBar = el('div', { class: 'triage-bulk-strip triage-bulk-bar', role: 'region', 'aria-label': 'Bulk actions' });
  // After a resolve the row leaves the list; this bar offers it back until the next change.
  const undoBar = el('div', { class: 'undo-bar', role: 'status' });
  undoBar.hidden = true;
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

  function statusChanged() {
    selectedIds.clear();
    if (activeViewName) {
      activeViewName = '';
      renderSavedViews();
    }
    syncHash();
    load();
  }

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

  const shortcutsBtn = button('Shortcuts (?)', () => window.openShortcutsDialog ? window.openShortcutsDialog(shortcutsBtn) : null, 'button ghost hide-on-touch');
  shortcutsBtn.setAttribute('aria-label', 'Keyboard shortcuts');
  shortcutsBtn.setAttribute('title', 'Keyboard shortcuts (?)');

  const commandStrip = el('div', { class: 'triage-command-strip' },
    el('div', { class: 'triage-command-main' },
      el('div', { class: 'triage-status-group' }, status.node),
      el('div', { class: 'triage-filters', role: 'group', 'aria-label': 'Filter loaded errors by type' }, chips)
    ),
    el('div', { class: 'triage-command-actions' },
      savedViewsContainer,
      button('Refresh', () => load(), 'button ghost'),
      shortcutsBtn
    )
  );

  container.append(
    heading('Error triage', 'Failures from every instance, newest first. Open one to see what happened.'),
    commandStrip,
    contextStrip,
    undoBar,
    announcer, feedback,
    // The list and its pagination live in one card, so "Older errors" belongs visibly to the list.
    el('section', { class: 'card result-card' }, content, el('div', { class: 'result-card-foot' }, next))
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
      key: ['j', 'J'],
      display: 'j',
      description: 'Focus next error row',
      handler: () => {
        const cur = getFocusedRowIndex();
        focusRow(cur === -1 ? 0 : cur + 1);
      }
    },
    {
      key: ['k', 'K'],
      display: 'k',
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
      key: ['o', 'O'],
      display: 'o',
      description: 'Open details of focused error',
      handler: () => { const row = focusedRow(); if (row) openDetails(row); }
    },
    {
      key: ['e', 'E'],
      display: 'e',
      description: 'Resolve selected errors (or focused error); Undo brings them back',
      handler: () => {
        if (!can('operator') || loadedStatus !== 'open') return;
        if (selectedIds.size > 0) { bulkResolve(); return; }
        const row = focusedRow();
        if (row) resolveRows([row]);
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

  function bulkResolve() {
    return resolveRows(rows.filter(r => selectedIds.has(r.id)), { confirm: true });
  }

  const rowFor = row => content.querySelector(`.triage-open[data-id="${CSS.escape(String(row.id))}"]`)?.closest('tr');
  function focusAt(position) {
    const trList = getRowElements();
    if (!trList.length) { (content.querySelector('.triage-table-wrap') || content).focus(); return; }
    trList[Math.max(0, Math.min(position, trList.length - 1))].querySelector('.triage-open')?.focus();
  }
  function failureNotice(failures, verb, done) {
    if (!failures.length) return;
    const names = failures.map(f => `${rowParts(f.row).name} (${f.error?.message || `Could not ${verb}`})`).join('; ');
    notice(feedback, Object.assign(new Error(done
      ? `${plural(done, 'error')} ${verb === 'resolve' ? 'marked resolved' : 'reopened'}, but ${failures.length} failed: ${names}.`
      : `Could not ${verb} ${plural(failures.length, 'error')}: ${names}.`), { code: 'PARTIAL_BULK_FAILURE' }));
  }

  // Resolving removes rows from the open list and offers Undo; nothing is deleted.
  async function resolveRows(list, { confirm = false } = {}) {
    if (!can('operator') || loadedStatus !== 'open' || !list.length) return;
    if (confirm && list.length > 1) {
      const ok = await confirmAction({
        title: 'Resolve selected errors',
        target: plural(list.length, 'error'),
        consequence: 'They leave the open queue. You can undo this right after.',
        confirmLabel: `Resolve ${plural(list.length, 'error')}`,
        danger: false
      });
      if (!ok) return;
    }
    const firstTr = rowFor(list[0]), position = firstTr ? getRowElements().indexOf(firstTr) : 0;
    content.setAttribute('aria-busy', 'true');
    feedback.replaceChildren();
    const done = [], failures = [];
    for (const row of list) {
      if (signal.aborted) return;
      try {
        await api(`/api/errors/${encodeURIComponent(row.id)}/resolve`, { method: 'POST', signal });
        done.push({ row, index: rows.indexOf(row) });
      } catch (error) {
        if (signal.aborted) return;
        failures.push({ row, error });
      }
    }
    if (signal.aborted) return;
    content.removeAttribute('aria-busy');
    const ids = new Set(done.map(d => d.row.id));
    rows = rows.filter(r => !ids.has(r.id));
    for (const d of done) { selectedIds.delete(d.row.id); d.row.status = 'resolved'; }
    failureNotice(failures, 'resolve', done.length);
    draw();
    focusAt(position);
    if (!done.length) { announcer.textContent = ''; return; }
    const label = done.length === 1 ? `${rowParts(done[0].row).name} marked resolved.` : `${plural(done.length, 'error')} marked resolved.`;
    const what = done.length === 1 ? rowParts(done[0].row).name : plural(done.length, 'error');
    undoBar.replaceChildren(el('span', {}, label), button('Undo', () => reopenRows(done.map(d => d.row), { restore: done }), 'ghost', { 'aria-label': `Undo: reopen ${what}` }));
    undoBar.hidden = false;
  }

  // Reopen is Undo on the open list (rows return where they were) and a row action on the resolved list.
  async function reopenRows(list, { restore = null } = {}) {
    if (!can('operator') || !list.length) return;
    content.setAttribute('aria-busy', 'true');
    feedback.replaceChildren();
    const done = [], failures = [];
    for (const row of list) {
      if (signal.aborted) return;
      try {
        await api(`/api/errors/${encodeURIComponent(row.id)}/reopen`, { method: 'POST', signal });
        done.push(row);
      } catch (error) {
        if (signal.aborted) return;
        failures.push({ row, error });
      }
    }
    if (signal.aborted) return;
    content.removeAttribute('aria-busy');
    undoBar.hidden = true;
    undoBar.replaceChildren();
    for (const row of done) { row.status = 'open'; row.resolved_at = null; }
    const back = new Set(done.map(r => r.id));
    if (restore && loadedStatus === 'open') {
      for (const item of restore.filter(i => back.has(i.row.id)).sort((a, b) => a.index - b.index)) rows.splice(Math.min(Math.max(item.index, 0), rows.length), 0, item.row);
    } else if (loadedStatus === 'resolved') {
      rows = rows.filter(r => !back.has(r.id));
    }
    failureNotice(failures, 'reopen', done.length);
    draw();
    if (done.length && restore && loadedStatus === 'open') rowFor(done[0])?.querySelector('.triage-open')?.focus();
    if (done.length) announcer.textContent = done.length === 1 ? `${rowParts(done[0]).name} reopened.` : `${plural(done.length, 'error')} reopened.`;
  }

  // Secondary actions for a row; the row's own title opens the details panel.
  function menuItems(row) {
    const execution = row.execution_id ? n8nUrl(row.instance_id, row.workflow_id, row.execution_id) : null;
    const workflowLink = n8nUrl(row.instance_id, row.workflow_id);
    return [
      execution ? { label: 'Open execution in n8n', href: execution, external: true } : null,
      workflowLink ? { label: 'Open workflow in n8n', href: workflowLink, external: true } : null,
      { label: 'Explain with AI', href: explainHref(row) },
      can('operator') ? { label: 'Repair with AI', href: repairHref(row) } : null,
      can('operator') && loadedStatus === 'open' ? { label: 'Mark resolved', onSelect: () => resolveRows([row]) } : null,
      can('operator') && loadedStatus === 'resolved' ? { label: 'Reopen', glyph: 'history', onSelect: () => reopenRows([row]) } : null,
    ];
  }

  // The evidence for one error: full message, when and how often, and the way back into n8n.
  function openDetails(row) {
    const { workflow, node } = rowParts(row);
    const execution = row.execution_id ? n8nUrl(row.instance_id, row.workflow_id, row.execution_id) : null;
    const workflowLink = n8nUrl(row.instance_id, row.workflow_id);
    const external = (href, label, className) => el('a', { class: className, href, target: '_blank', rel: 'noopener noreferrer' }, label, icon('external'));
    const related = el('div', { class: 'peek-related' }, el('p', { class: 'muted' }, 'Looking for notes…'));
    const resolvedAt = parseTimestamp(row.resolved_at);
    let triage = null;
    if (can('operator') && loadedStatus === 'open') triage = button('Mark resolved', () => { closePeek(); return resolveRows([row]); }, 'ghost');
    else if (can('operator') && loadedStatus === 'resolved') triage = button('Reopen', () => { closePeek(); return reopenRows([row]); }, 'ghost');
    openPeek({
      eyebrow: severityPill(row.error_type),
      title: workflow,
      meta: `${node} · ${instanceName(row.instance_id)}`,
      sections: [
        el('p', { class: 'peek-message' }, row.error_message ? String(row.error_message) : el('span', { class: 'triage-missing' }, 'Message unavailable')),
        peekFacts([
          ['Last seen', relativeTime(row.occurred_at)],
          ['Occurrences', row.count === null || row.count === undefined ? null : String(row.count)],
          ['Execution', row.execution_id ? peekCopy(row.execution_id, 'execution ID') : 'Not recorded'],
          ['Workflow', row.workflow_id ? el('span', { class: 'peek-inline' }, peekCopy(row.workflow_id, 'workflow ID'), workflowLink ? external(workflowLink, 'Open in n8n', 'peek-link') : null) : null],
          ['Status', row.status === 'resolved' ? (resolvedAt ? `Resolved ${resolvedAt.toLocaleString()}` : 'Resolved') : 'Open'],
        ]),
        peekSection('Notes about this workflow', related),
      ],
      actions: [
        execution ? external(execution, 'Open execution in n8n', 'button') : workflowLink ? external(workflowLink, 'Open workflow in n8n', 'button') : null,
        el('a', { class: 'button ghost', href: explainHref(row) }, 'Explain with AI'),
        can('operator') ? el('a', { class: 'button ghost', href: repairHref(row) }, 'Repair with AI') : null,
        triage,
      ],
    });
    // Runbooks and past incidents that name the workflow.
    api('/api/notes/search?q=' + encodeURIComponent(workflow), { signal }).then(result => {
      const notes = (result.data || []).slice(0, 3);
      related.replaceChildren(notes.length
        ? el('ul', { class: 'peek-links' }, notes.map(n => el('li', {}, el('a', { href: '#notes?' + new URLSearchParams({ path: n.path }) }, n.path))))
        : el('p', { class: 'muted' }, 'No notes mention this workflow.'));
    }, () => { if (!signal.aborted) related.replaceChildren(el('p', { class: 'muted' }, 'Notes could not be searched.')); });
  }

  function focusedRow() {
    const cur = getFocusedRowIndex();
    if (cur === -1) return null;
    const id = getRowElements()[cur]?.querySelector('.triage-open')?.dataset.id;
    return rows.find(r => String(r.id) === id) || null;
  }

  function draw(){
    if (announceTimer) { clearTimeout(announceTimer); announceTimer = null; }
    updateCount();
    const shown=visibleRows();
    if(!rows.length){
      selectedIds.clear();
      updateSelectionUI();
      content.replaceChildren(empty('Nothing to triage',loadedStatus==='open'?'No open errors right now.':'No resolved errors yet.',loadedStatus==='resolved'?[button('Check again',()=>load(),'ghost'),button('Back to Open',()=>{status.value='open';statusChanged();},'ghost')]:[button('Check again',()=>load(),'ghost')],'check'));
      return;
    }
    if(!shown.length){
      updateSelectionUI();
      content.replaceChildren(empty('No loaded errors of this type',cursor?'Choose All, or load older errors to look further back.':'Choose All to see the other loaded errors.',[button('Show all types',()=>setFilter('all'),'ghost')],'filter'));
      return;
    }

    const canSelect = can('operator') && loadedStatus === 'open';
    const colOffset = canSelect ? 1 : 0;
    const sortColIndex = SORT_COL_KEYS.indexOf(currentSort.key) + colOffset;

    const sortable = {};
    SORT_COL_KEYS.forEach((k, idx) => {
      sortable[idx + colOffset] = k === 'when' ? 'date' : k === 'count' ? 'number' : k === 'severity' ? 'rank' : 'text';
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
      headers = [selectAllHead, 'Severity', 'Error', 'Instance', 'Count', 'Last seen', el('span', { class: 'sr-only' }, 'Actions')];
    } else {
      headers = ['Severity', 'Error', 'Instance', 'Count', 'Last seen', el('span', { class: 'sr-only' }, 'Actions')];
    }

    const tableNode = table(
      headers,
      shown.map(row => renderRow(row, canSelect)),
      {
        className: 'triage-table',
        wrapClass: 'triage-table-wrap',
        ariaLabel: 'Errors table',
        numeric: [3 + colOffset],
        sortable,
        initialSort: { column: sortColIndex >= colOffset ? sortColIndex : (4 + colOffset), direction: currentSort.direction },
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
    const {workflow,node,name}=rowParts(row);
    const cell=(label,className,...children)=>el('td',{class:className,'data-label':label},children);
    // The title is the row's one inline action: it opens the evidence. Everything else is in the menu.
    const open=el('button',{type:'button',class:'triage-open','data-id':String(row.id),'aria-label':`Details for ${name}`},el('strong',{},workflow),el('span',{class:'triage-node'},node));
    // el() adds an action glyph to buttons whose text starts with a verb ("Send webinar email"); a title has none.
    open.querySelector(':scope > svg')?.remove();
    open.addEventListener('click',()=>openDetails(row));

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

    const severity = severityOf(row.error_type);
    const subjectCell = cell('Error','triage-subject',el('div',{class:'triage-subject-body'},open,message(row.error_message)));
    subjectCell._rowMeta = { workflow, node, severity: severity.label };
    const occurred = parseTimestamp(row.occurred_at);
    const known = row.count !== null && row.count !== undefined;

    cells.push(
      {sortValue:SEVERITY_RANK[severity.kind]??0,content:cell('Severity','triage-severity',severityPill(row.error_type))},
      {sortValue:name,content:subjectCell},
      {sortValue:instanceName(row.instance_id),content:cell('Instance','triage-instance',instanceName(row.instance_id))},
      {sortValue:known?row.count:0,content:cell('Count','triage-num',known?[String(row.count),el('span',{class:'triage-count-unit'},row.count===1?' occurrence':' occurrences')]:el('span',{class:'triage-missing'},'Unavailable'))},
      {sortValue:occurred?occurred.getTime():0,content:cell('Last seen','triage-when',relativeTime(row.occurred_at))},
      cell('Actions','triage-actions',rowMenu(menuItems(row),`More actions for ${name}`))
    );

    return {
      cells,
      className: isRowSelected ? 'is-selected' : '',
      selected: isRowSelected
    };
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
      if(!before){feedback.replaceChildren();announcer.textContent='';undoBar.hidden=true;undoBar.replaceChildren();}
      loadedStatus=requested;rows=before?rows.concat(result.data):result.data;cursor=result.next_cursor;
      draw();
    }catch(error){if(!signal.aborted)notice(feedback,error);}
    finally{
      busy=false;content.removeAttribute('aria-busy');next.disabled=false;next.hidden=!cursor;
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
    const workflow = tr.querySelector('.triage-open strong')?.textContent?.trim() || 'Workflow';
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
