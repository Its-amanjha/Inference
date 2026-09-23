import {el} from './html.js';

// Deliberately small Markdown dialect. Source text never becomes HTML.
// [[Note]] and [[Note|label]] link to other notes when options.wikilink(target) returns an address for them;
// a link to a note that does not exist stays visible as text.
function inline(text, depth = 0, options = {}) {
  if (depth >= 6) return [text];
  const nodes = [];
  const pattern = /(\[\[[^\[\]\n]+\]\]|`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|!?\[[^\]\n]+\]\([^\s)]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    nodes.push(text.slice(offset, match.index));
    const token = match[0];
    if (token.startsWith('[[')) {
      const [target, alias] = token.slice(2, -2).split('|');
      const label = (alias || target).trim(), href = target.trim() ? options.wikilink?.(target.trim()) : null;
      nodes.push(href ? el('a', {href, class:'wikilink'}, label) : el('span', {class:'wikilink is-missing', title:'No note with this name yet'}, label));
    } else if (token.startsWith('`')) nodes.push(el('code', {}, token.slice(1, -1)));
    else if (token.startsWith('!')) nodes.push(token);
    else if (token.startsWith('[')) {
      const end = token.indexOf(']('), label = token.slice(1, end), href = token.slice(end + 2, -1);
      let allowed = false;
      try { allowed = ['https:', 'http:', 'mailto:'].includes(new URL(href).protocol); } catch { /* Keep unsupported destinations literal. */ }
      nodes.push(allowed ? el('a', {href, target:'_blank', rel:'noopener noreferrer'}, label) : token);
    } else {
      const width = token.startsWith('**') || token.startsWith('__') ? 2 : 1;
      nodes.push(el(width === 2 ? 'strong' : 'em', {}, ...inline(token.slice(width, -width), depth + 1, options)));
    }
    offset = match.index + token.length;
  }
  nodes.push(text.slice(offset));
  return nodes;
}

export function renderMarkdown(container, source, options = {}) {
  container.replaceChildren();
  if (source === null || source === undefined || source === '') return;
  const text = String(source);
  // Oversized/dense documents retain a usable text preview with bounded DOM work.
  if (text.length > 262144 || text.split('\n').length > 5000 || (text.match(/[`*_\[]/g) || []).length > 10000) {
    container.append(el('p', {class:'muted'}, 'Large document: showing plain text.'), el('pre', {}, text));
    return;
  }
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [], list = null, quote = null;
  const flush = () => { if (paragraph.length) container.append(el('p', {}, ...inline(paragraph.join('\n'), 0, options))); paragraph = []; };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const fence = line.match(/^\s{0,3}(`{3,}|~{3,})([^`]*)$/);
    if (fence) {
      flush(); list = quote = null;
      const code = [], marker = fence[1][0], length = fence[1].length;
      while (++index < lines.length) {
        const closing = lines[index].trim();
        if (closing.length >= length && [...closing].every(char => char === marker)) break;
        code.push(lines[index]);
      }
      container.append(el('pre', {}, el('code', {}, code.join('\n'))));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    const item = line.match(/^\s{0,3}(?:([-+*])|([0-9]{1,9})[.)])\s+(.+)$/);
    const quoted = line.match(/^>\s?(.*)$/);
    if (!line.trim()) { flush(); list = quote = null; }
    else if (heading) { flush(); list = quote = null; container.append(el(`h${heading[1].length}`, {}, ...inline(heading[2], 0, options))); }
    else if (/^\s{0,3}(---+|\*\*\*+|___+)\s*$/.test(line)) { flush(); list = quote = null; container.append(el('hr')); }
    else if (item) {
      flush(); quote = null;
      const tag = item[2] ? 'ol' : 'ul';
      if (!list || list.localName !== tag) { list = el(tag, item[2] ? {start:item[2]} : {}); container.append(list); }
      list.append(el('li', {}, ...inline(item[3], 0, options)));
    } else if (quoted) {
      flush(); list = null;
      if (!quote) { quote = el('blockquote'); container.append(quote); }
      quote.append(el('p', {}, ...inline(quoted[1], 0, options)));
    } else { list = quote = null; paragraph.push(line); }
  }
  flush();
}
