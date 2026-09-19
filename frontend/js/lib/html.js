import {icon,actionIcon} from './icons.js';
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
export function notice(container, error) {
  if (error?.name === 'AbortError' || !container.isConnected) return;
  const failed = error instanceof Error;
  const node = el('div',{class:failed?'error':'notice',role:failed?'alert':'status'}, error?.message || String(error));
  container.prepend(node); return node;
}
export function heading(title, description = '') { const route=location.hash.slice(1).split('?')[0]||'dashboard';return el('header',{class:'page-heading'},el('div',{class:'heading-title'},el('span',{class:'heading-icon'},icon(route)),el('h1',{},title)),description ? el('p',{},description) : null); }
export function empty(title, body = '') { return el('section',{class:'card empty'},el('h2',{},title),el('p',{},body)); }
// options.numeric lists zero-based column indexes that hold numbers (right-aligned, tabular numerals).
// options.stack controls phone row-stacking (defaults to true; false preserves standard horizontal scroll).
export function table(headers, rows, options = {}) {
  const numeric = new Set(options?.numeric || []);
  const stack = options?.stack !== false;
  const align = index => numeric.has(index) ? 'num' : undefined;
  const resolveLabel = index => {
    const raw = headers[index];
    if (typeof raw === 'string' && raw.trim()) return raw.trim();
    if (index === headers.length - 1) return 'Actions';
    return '';
  };
  return el('div', {
    class: ['table-wrap', stack ? 'table-wrap-stack' : 'table-scroll'].filter(Boolean).join(' '),
    tabindex: 0,
    role: 'region',
    'aria-label': headers.filter(Boolean).join(', ') + ' table'
  }, el('table', stack ? {class: 'table-stack', 'data-stack': 'true'} : {class: 'table-scroll', 'data-stack': 'false'},
    el('thead', {}, el('tr', {}, headers.map((text, index) => el('th', {scope: 'col', class: align(index)}, text)))),
    el('tbody', {}, rows.map(row => el('tr', {}, row.map((cell, index) => {
      const label = resolveLabel(index);
      const attrs = {class: align(index)};
      if (stack && label) attrs['data-label'] = label;
      return el('td', attrs, cell);
    }))))
  ));
}
export function jsonView(value) { return el('pre',{},JSON.stringify(value,null,2)); }
