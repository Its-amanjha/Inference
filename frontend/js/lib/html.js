import {icon,actionIcon,registerIcons} from './icons.js';
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (name.startsWith('on') && typeof value === 'function') node.addEventListener(name.slice(2).toLowerCase(), value);
    else if (name === 'className' || name === 'class') node.className = value;
    else if (name === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (name === 'dataset') Object.assign(node.dataset, value);
    else if (['value','checked','selected','disabled','hidden','textContent'].includes(name)) node[name] = value;
    else if (!name.toLowerCase().startsWith('on') && name !== 'innerHTML') node.setAttribute(name, value === true ? '' : String(value));
  }
  return node;
}
export function button(label, callback, className = 'button') {
  const node = el('button', {type:'button', class:className.includes('button') ? className : `button ${className}`}, actionIcon(label),label);
  let busy = false;
  node.addEventListener('click', async event => {
    if (node.disabled || busy) return;
    busy = true; node.setAttribute('aria-busy','true');
    try { await callback(event); } catch (error) { if (error.name !== 'AbortError') window.dispatchEvent(new CustomEvent('app-error',{detail:error})); }
    finally { busy = false; node.removeAttribute('aria-busy'); }
  });
  return node;
}
let sequence = 0;
// A labelled control. options.hint adds help text under the control, linked with aria-describedby.
// Checkboxes and radios sit beside their label; password inputs get a show/hide toggle.
export function field(label, input, options = {}) {
  if (!input.id) input.id = `field-${++sequence}`;
  const hint = options.hint ? el('p',{class:'field-hint',id:`${input.id}-hint`},options.hint) : null;
  if (hint) input.setAttribute('aria-describedby',[input.getAttribute('aria-describedby'),hint.id].filter(Boolean).join(' '));
  if (input.type === 'checkbox' || input.type === 'radio') return el('div',{class:'field field-check'}, input, el('div',{class:'field-check-text'},el('label',{for:input.id},label),hint));
  return el('div',{class:'field'}, el('label',{for:input.id},label), input.type === 'password' ? secretControl(input,label) : input, hint);
}
// Masked by default. The toggle keeps one accessible name and reports its state with aria-pressed.
function secretControl(input, label) {
  input.setAttribute('autocapitalize','off'); input.setAttribute('spellcheck','false');
  const noun = /^[A-Z][a-z]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label;
  const toggle = el('button',{type:'button',class:'control-reveal','aria-controls':input.id,'aria-pressed':'false','aria-label':`Show ${noun}`,title:'Show'},icon('eye'));
  toggle.addEventListener('click',()=>{
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed',String(show)); toggle.title = show ? 'Hide' : 'Show';
    toggle.replaceChildren(icon(show ? 'eye-off' : 'eye'));
  });
  return el('div',{class:'control-secret'}, input, toggle);
}
// Settings card: heading and description, fields, then a footer with an optional note and the actions (primary last).
export function formCard({title, description, body = [], note, actions = [], className = '', heading = 'h2', flat = false} = {}) {
  const head = title || description ? el('div',{class:'form-card-head'}, typeof title === 'string' ? el(heading,{},title) : title, description ? el('p',{},description) : null) : null;
  const foot = note || actions.length ? el('div',{class:'form-card-foot'}, note ? el('p',{class:'form-card-note'},note) : null, el('div',{class:'form-actions'},actions)) : null;
  return el('form',{class:['card form-card',flat?'is-flat':'',className].filter(Boolean).join(' ')}, head, el('div',{class:'form-card-body'},body), foot);
}
export function notice(container, error, options = {}) {
  if (error?.name === 'AbortError' || !container.isConnected) return;
  const failed = error instanceof Error;
  const node = el('div',{class:failed?'error':'notice',role:failed?'alert':'status'}, error?.message || String(error));
  const {icon: iconName, action} = options;
  if (iconName) node.prepend(icon(iconName));
  if (action) node.append(action);
  container.prepend(node); return node;
}
export function heading(title, description = '') { const route=location.hash.slice(1).split('?')[0]||'dashboard';return el('header',{class:'page-heading'},el('div',{class:'heading-title'},el('span',{class:'heading-icon'},icon(route)),el('h1',{},title)),description ? el('p',{},description) : null); }
// options.actions (array of Nodes: links, buttons or both) renders a centred action
// row below the body. Omitting it renders exactly the historic two-argument output.
export function empty(title, body = '', actions = []) {
  const node = el('section',{class:'card empty'},el('h2',{},title),el('p',{},body));
  if (actions && actions.length) node.append(el('div',{class:'empty-actions'},...actions));
  return node;
}
// options.numeric lists zero-based column indexes that hold numbers (right-aligned, tabular numerals).
// options.stack controls phone row-stacking (defaults to true; false preserves standard horizontal scroll).
// options.sortable maps column index to 'text'|'number'|'date' for opt-in sortable columns.
// options.initialSort sets { column: index, direction: 'asc'|'desc' }.
// options.onSort receives ({ column, direction }) on sort change.
// options.className adds classes to <table>.
// options.wrapClass adds classes to .table-wrap.
export function table(headers, rows, options = {}) {
  const numeric = new Set(options?.numeric || []);
  const stack = options?.stack !== false;
  const sortable = options?.sortable || null;
  const onSort = typeof options?.onSort === 'function' ? options.onSort : null;
  const align = index => numeric.has(index) ? 'num' : undefined;

  const resolveLabel = index => {
    const raw = headers[index];
    if (typeof raw === 'string' && raw.trim()) return raw.trim();
    if (raw instanceof Node) {
      const text = raw.textContent?.trim();
      if (text) return text;
      const aria = raw.getAttribute?.('aria-label');
      if (aria) return aria;
    }
    if (raw && typeof raw === 'object' && raw.label) return String(raw.label).trim();
    if (index === headers.length - 1) return 'Actions';
    return '';
  };

  function parseSortValue(cell, type) {
    let raw = cell;
    if (cell && typeof cell === 'object') {
      if ('sortValue' in cell) raw = cell.sortValue;
      else if ('value' in cell) raw = cell.value;
      else if (cell instanceof Node) raw = cell.dataset?.sortValue ?? cell.textContent;
    }
    if (type === 'number') {
      const num = typeof raw === 'number' ? raw : Number(String(raw).replace(/[^0-9.-]+/g, ''));
      return Number.isFinite(num) ? num : (raw === null || raw === undefined || raw === '' ? -Infinity : 0);
    }
    if (type === 'date') {
      if (raw instanceof Date) return raw.getTime();
      if (typeof raw === 'number' && Number.isFinite(raw)) return raw;
      if (typeof raw === 'string' && raw.trim()) {
        const parsed = Date.parse(raw.trim());
        if (!Number.isNaN(parsed)) return parsed;
      }
      return 0;
    }
    return String(raw ?? '').trim().toLowerCase();
  }

  let currentSortCol = options?.initialSort?.column ?? null;
  let currentSortDir = options?.initialSort?.direction ?? 'asc';

  const getRowCells = r => Array.isArray(r) ? (r.cells || r) : (r?.cells || []);

  const renderedRows = rows.map((row, origIndex) => {
    const rowCells = getRowCells(row);
    const rowClass = row?.className || (row?.selected ? 'is-selected' : '');
    const tr = el('tr', { class: rowClass || undefined }, rowCells.map((cell, index) => {
      const label = resolveLabel(index);
      const attrs = {class: align(index)};
      if (stack && label) attrs['data-label'] = label;
      const target = cell && typeof cell === 'object' && 'content' in cell ? cell.content : cell;
      if (target instanceof HTMLTableCellElement || (target && target.tagName === 'TD')) {
        if (stack && label && !target.hasAttribute('data-label')) target.setAttribute('data-label', label);
        return target;
      }
      return el('td', attrs, ...(Array.isArray(target) ? target : [target]));
    }));
    return { row, tr, origIndex };
  });

  const tbody = el('tbody', {}, renderedRows.map(r => r.tr));
  const announcer = el('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
  const thElements = [];
  let mobileSelect = null;

  function applySort(colIndex, dir, announce = true) {
    if (!sortable || !(colIndex in sortable)) return;
    const type = sortable[colIndex] || 'text';
    currentSortCol = colIndex;
    currentSortDir = dir;

    // Parse each cell once; the comparator below only compares cached keys, so a
    // sort costs O(n) parses instead of O(n log n). Order is unchanged: ties keep
    // insertion order and only the comparison flips with direction.
    const collator = type === 'text' ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' }) : null;
    const decorated = renderedRows.map(entry => ({ entry, key: parseSortValue(getRowCells(entry.row)[colIndex], type) }));
    decorated.sort((a, b) => {
      let cmp = 0;
      if (type === 'number' || type === 'date') {
        cmp = a.key < b.key ? -1 : (a.key > b.key ? 1 : 0);
      } else {
        cmp = collator.compare(a.key, b.key);
      }
      if (cmp === 0) return a.entry.origIndex - b.entry.origIndex;
      return dir === 'asc' ? cmp : -cmp;
    });
    renderedRows.length = 0;
    for (const item of decorated) renderedRows.push(item.entry);

    tbody.replaceChildren(...renderedRows.map(r => r.tr));

    thElements.forEach((item, idx) => {
      if (!item.sortBtn) return;
      const isSorted = idx === currentSortCol;
      const label = resolveLabel(idx);
      if (isSorted) {
        item.th.setAttribute('aria-sort', currentSortDir === 'asc' ? 'ascending' : 'descending');
        const nextDir = currentSortDir === 'asc' ? 'descending' : 'ascending';
        item.sortBtn.setAttribute('aria-label', `Sort by ${label}, currently sorted ${currentSortDir === 'asc' ? 'ascending' : 'descending'}. Activate to sort ${nextDir}.`);
        item.indicator.className = `sort-indicator is-sorted is-${currentSortDir}`;
        item.indicator.replaceChildren(icon(currentSortDir === 'asc' ? 'sort-asc' : 'sort-desc'));
      } else {
        item.th.setAttribute('aria-sort', 'none');
        item.sortBtn.setAttribute('aria-label', `Sort by ${label}, currently unsorted. Activate to sort ascending.`);
        item.indicator.className = 'sort-indicator is-unsorted';
        item.indicator.replaceChildren(icon('sort-none'));
      }
    });

    if (mobileSelect) {
      mobileSelect.value = `${currentSortCol}:${currentSortDir}`;
    }

    const headerLabel = resolveLabel(colIndex);
    if (announce) {
      announcer.textContent = `Table sorted by ${headerLabel}, ${currentSortDir === 'asc' ? 'ascending' : 'descending'}.`;
      if (onSort) onSort({ column: currentSortCol, direction: currentSortDir });
    }
  }

  registerIcons({
    'sort-asc': 'm7 14 5-5 5 5',
    'sort-desc': 'm7 10 5 5 5-5',
    'sort-none': 'm7 15 5 5 5-5 M7 9l5-5 5 5',
  });

  const thead = el('thead', {}, el('tr', {}, headers.map((rawHeader, index) => {
    const isSortable = Boolean(sortable && index in sortable);
    const label = resolveLabel(index);
    const thClasses = [align(index), isSortable ? 'is-sortable' : ''].filter(Boolean).join(' ');
    const th = el('th', { scope: 'col', class: thClasses || undefined });

    if (isSortable) {
      const isCurrent = index === currentSortCol;
      th.setAttribute('aria-sort', isCurrent ? (currentSortDir === 'asc' ? 'ascending' : 'descending') : 'none');
      const indicator = el('span', {
        class: `sort-indicator ${isCurrent ? `is-sorted is-${currentSortDir}` : 'is-unsorted'}`,
        'aria-hidden': 'true'
      }, icon(isCurrent ? (currentSortDir === 'asc' ? 'sort-asc' : 'sort-desc') : 'sort-none'));

      const nextDir = isCurrent ? (currentSortDir === 'asc' ? 'descending' : 'ascending') : 'ascending';
      const sortBtn = el('button', {
        type: 'button',
        class: 'table-sort-btn',
        'aria-label': `Sort by ${label}, currently ${isCurrent ? `sorted ${currentSortDir === 'asc' ? 'ascending' : 'descending'}` : 'unsorted'}. Activate to sort ${nextDir}.`
      }, el('span', { class: 'table-sort-label' }, typeof rawHeader === 'string' ? rawHeader : (rawHeader ?? label)), indicator);

      sortBtn.addEventListener('click', () => {
        const next = index === currentSortCol && currentSortDir === 'asc' ? 'desc' : 'asc';
        applySort(index, next);
      });

      th.append(sortBtn);
      thElements.push({ th, sortBtn, indicator });
    } else {
      th.append(rawHeader instanceof Node ? rawHeader : (typeof rawHeader === 'string' ? rawHeader : (rawHeader ?? '')));
      thElements.push({ th, sortBtn: null, indicator: null });
    }
    return th;
  })));

  if (sortable && currentSortCol !== null && currentSortCol in sortable) {
    applySort(currentSortCol, currentSortDir, false);
  }

  let mobileSortControl = null;
  if (sortable && Object.keys(sortable).length > 0 && stack) {
    const sortId = `table-sort-${++sequence}`;
    mobileSelect = el('select', { id: sortId, 'aria-label': 'Sort table' });

    for (const [colStr, colType] of Object.entries(sortable)) {
      const colIdx = Number(colStr);
      const colLabel = resolveLabel(colIdx);
      const ascDesc = colType === 'number'
        ? ['Lowest first', 'Highest first']
        : colType === 'date'
        ? ['Oldest first', 'Newest first']
        : ['A to Z', 'Z to A'];

      mobileSelect.append(
        el('option', { value: `${colIdx}:asc` }, `${colLabel} (${ascDesc[0]})`),
        el('option', { value: `${colIdx}:desc` }, `${colLabel} (${ascDesc[1]})`)
      );
    }

    if (currentSortCol !== null && `${currentSortCol}:${currentSortDir}` in mobileSelect) {
      mobileSelect.value = `${currentSortCol}:${currentSortDir}`;
    }

    mobileSelect.addEventListener('change', () => {
      const [colStr, dir] = mobileSelect.value.split(':');
      applySort(Number(colStr), dir);
    });

    mobileSortControl = el('div', { class: 'table-mobile-sort', role: 'group', 'aria-label': 'Sort table' },
      el('label', { for: sortId }, 'Sort by'),
      mobileSelect
    );
  }

  const tableClasses = [
    stack ? 'table-stack' : 'table-scroll',
    options?.className || ''
  ].filter(Boolean).join(' ');

  const wrapClasses = [
    'table-wrap',
    stack ? 'table-wrap-stack' : 'table-scroll',
    options?.wrapClass || ''
  ].filter(Boolean).join(' ');

  const tableEl = el('table', { class: tableClasses, 'data-stack': stack ? 'true' : 'false' }, thead, tbody);

  return el('div', {
    class: wrapClasses,
    tabindex: 0,
    role: 'region',
    'aria-label': options?.ariaLabel || (headers.filter(h => typeof h === 'string' && h.trim()).join(', ') + ' table')
  }, announcer, mobileSortControl, tableEl);
}
export function jsonView(value) { return el('pre',{},JSON.stringify(value,null,2)); }
