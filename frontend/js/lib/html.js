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
export function field(label, input) {
  if (!input.id) input.id = `field-${++sequence}`;
  return el('div',{class:'field'}, el('label',{for:input.id},label), input);
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
export function table(headers, rows, options = {}) { const numeric=new Set(options?.numeric||[]),align=index=>numeric.has(index)?'num':undefined; return el('div',{class:'table-wrap',tabindex:0,role:'region','aria-label':headers.filter(Boolean).join(', ')+' table'},el('table',{},el('thead',{},el('tr',{},headers.map((text,index)=>el('th',{scope:'col',class:align(index)},text)))),el('tbody',{},rows.map(row=>el('tr',{},row.map((cell,index)=>el('td',{class:align(index)},cell))))))); }
export function jsonView(value) { return el('pre',{},JSON.stringify(value,null,2)); }
