// Command palette: jump to any view, any workflow by name across the fleet, any
// instance. One text field (combobox) over a grouped listbox; arrows move,
// Enter jumps, Escape closes, focus returns to the invoker.
import {el} from './html.js';
import {icon} from './icons.js';
import {api} from '../api.js';
import {state} from '../state.js';

let session = null;

export function openPalette(invoker) {
  if (session) return;
  const trigger = invoker instanceof HTMLElement ? invoker
    : (document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const controller = new AbortController();
  const input = el('input', {type: 'text', role: 'combobox', 'aria-expanded': 'false',
    'aria-controls': 'palette-list', 'aria-autocomplete': 'list', autocomplete: 'off',
    spellcheck: 'false', placeholder: 'Jump to a view, workflow or instance…',
    'aria-label': 'Jump to a view, workflow or instance'});
  const list = el('ul', {id: 'palette-list', class: 'palette-list', role: 'listbox', 'aria-label': 'Matches'});
  const hint = el('p', {class: 'muted palette-hint', id: 'palette-hint'}, 'Type to search workflows across the fleet.');
  input.setAttribute('aria-describedby', 'palette-hint');
  const box = el('div', {class: 'palette', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette'},
    input, list, hint);
  const overlay = el('div', {class: 'palette-overlay'}, box);

  const views = [...document.querySelectorAll('.nav-group a[data-route]')].map(link => ({
    group: 'Views', glyph: (link.getAttribute('href') || '#dashboard').slice(1), label: link.querySelector('span')?.textContent?.trim() || link.textContent.trim(),
    detail: null, href: link.getAttribute('href') || '#dashboard',
  }));
  let workflows = [], searchTimer = 0, searchVersion = 0, search = null;
  let items = [], active = -1;

  // Workflows are looked up by name on every instance as you type (a few per instance), so any
  // workflow can be reached, not only those on a first page. Choosing one opens its page.
  function searchWorkflows(needle) {
    // Only the latest text is searched: the previous scan is cancelled and its results cleared.
    clearTimeout(searchTimer);
    search?.abort();
    const version = ++searchVersion;
    workflows = [];
    if (needle.length < 2) return;
    search = new AbortController();
    const signal = AbortSignal.any([controller.signal, search.signal]);
    searchTimer = setTimeout(async () => {
      const instances = state.instances, groups = Array(instances.length);
      let next = 0;
      await Promise.all(Array.from({length: Math.min(3, instances.length)}, async () => {
        while (next < instances.length && !signal.aborted) {
          const index = next++, instance = instances[index];
          try {
            const result = await api('/api/n8n_proxy/workflows?' + new URLSearchParams({instance_id: instance.id, name: needle, limit: '5'}), {signal});
            groups[index] = (result.data || []).map(row => ({
              group: 'Workflows', glyph: 'workflows', label: row.name || row.id, detail: instance.name, instanceId: instance.id,
              href: '#workflows?' + new URLSearchParams({instance_id: instance.id, workflow_id: row.id}),
            }));
          } catch { groups[index] = []; }
        }
      }));
      if (version !== searchVersion || session !== record) return;
      workflows = groups.flat().filter(Boolean);
      paint();
    }, 250);
  }

  function collect() {
    const needle = input.value.trim().toLowerCase();
    const matches = [...views];
    for (const instance of state.instances) {
      matches.push({group: 'Instances', glyph: 'instances', label: instance.name, detail: instance.url,
        href: '#instances?telemetry=' + encodeURIComponent(instance.id)});
    }
    if (!needle) return matches;
    // The server already matched workflow names; views and instances are matched here.
    return [...matches.filter(item => (item.label + ' ' + (item.detail || '')).toLowerCase().includes(needle)), ...workflows.slice(0, 10)];
  }

  function paint() {
    items = collect();
    active = items.length ? 0 : -1;
    list.replaceChildren();
    let lastGroup = null;
    items.forEach((item, index) => {
      if (item.group !== lastGroup) {
        lastGroup = item.group;
        const head = el('li', {class: 'palette-group', role: 'presentation', 'aria-hidden': 'true'}, item.group);
        list.append(head);
      }
      const node = el('li', {class: 'palette-option', role: 'option', id: `palette-option-${index}`,
        'aria-selected': String(index === active)},
        item.glyph ? el('span', {class: 'palette-icon', 'aria-hidden': 'true'}, icon(item.glyph)) : null,
        el('span', {class: 'palette-label'}, item.label),
        item.detail ? el('span', {class: 'muted palette-detail'}, item.detail) : null);
      node.addEventListener('pointerdown', event => event.preventDefault());
      node.addEventListener('click', () => choose(index));
      list.append(node);
    });
    input.setAttribute('aria-expanded', String(items.length > 0));
    const options = [...list.querySelectorAll('[role=option]')];
    if (active >= 0 && options[active]) {
      input.setAttribute('aria-activedescendant', options[active].id);
      options[active].scrollIntoView({block: 'nearest'});
    } else input.removeAttribute('aria-activedescendant');
    hint.textContent = items.length
      ? `${items.length} match${items.length === 1 ? '' : 'es'}.`
      : 'No matches.';
  }

  function move(delta) {
    if (!items.length) return;
    active = (active + delta + items.length) % items.length;
    [...list.querySelectorAll('[role=option]')].forEach((node, index) => {
      node.setAttribute('aria-selected', String(index === active));
      if (index === active) {
        input.setAttribute('aria-activedescendant', node.id);
        node.scrollIntoView({block: 'nearest'});
      }
    });
  }

  function choose(index) {
    const item = items[index];
    close();
    if (!item) return;
    // A workflow on an instance outside the current scope opens under All instances, where it is listed.
    if (item.instanceId && state.instanceId && state.instanceId !== item.instanceId) state.instanceId = '';
    location.hash = item.href;
  }

  function close() {
    if (session !== record) return;
    session = null;
    controller.abort();
    overlay.remove();
    document.removeEventListener('pointerdown', outside, true);
    if (trigger?.isConnected) trigger.focus();
  }

  function outside(event) { if (!box.contains(event.target)) close(); }

  const record = {close};
  session = record;
  document.addEventListener('pointerdown', outside, true);
  input.addEventListener('input', () => {
    searchWorkflows(input.value.trim());
    paint();
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
    else if (event.key === 'Enter') { event.preventDefault(); choose(active); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    // The field is the dialog's only control: Tab must not move focus to the page behind it.
    else if (event.key === 'Tab') event.preventDefault();
  });
  document.body.append(overlay);
  input.focus();
}
