const properties = {
  '--color-ash-mist':'color', '--color-paper-white':'color', '--color-ink-black':'color',
  '--color-graphite':'color', '--color-smoke':'color', '--surface-page-canvas':'color',
  '--surface-card-surface':'color', '--surface-nav-pill':'color', '--surface-input':'color',
  '--color-signal-blue':'color', '--action-fill':'color', '--action-text':'color',
  '--selection-fill':'color', '--selection-text':'color', '--color-sky-wash':'color',
  '--border':'color', '--focus':'color', '--shadow-card':'box-shadow', '--shadow-glow-ring':'box-shadow'
};
const cache = new Map();
let sequence = 0;
async function load(theme) {
  if (!cache.has(theme)) {
    const pending = (async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(`/themes/${theme}.json`, {signal:controller.signal,cache:'no-store'});
        if (!response.ok) throw new Error('Theme file unavailable.');
        const data = await response.json();
        if (data.id !== theme || !data.tokens || Object.keys(data.tokens).length !== Object.keys(properties).length) throw new Error('Invalid theme file.');
        for (const [key, property] of Object.entries(properties)) {
          const value = data.tokens[key];
          if (typeof value !== 'string' || value.length > 256 || /url\s*\(|var\s*\(/i.test(value) || !CSS.supports(property, value)) throw new Error('Invalid theme token.');
        }
        return data.tokens;
      } finally { clearTimeout(timer); }
    })();
    cache.set(theme, pending);
    pending.catch(() => { if (cache.get(theme) === pending) cache.delete(theme); });
  }
  return cache.get(theme);
}
export async function setTheme(value) {
  const theme = ['light','dark','n8n'].includes(value) ? value : 'light', current = ++sequence;
  try {
    const tokens = await load(theme);
    if (current !== sequence) return false;
    for (const key of Object.keys(properties)) document.documentElement.style.setProperty(key, tokens[key]);
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme==='light'?'light':'dark';
    return true;
  } catch {
    if (current !== sequence) return false;
    for (const key of Object.keys(properties)) document.documentElement.style.removeProperty(key);
    document.documentElement.dataset.theme = 'light';
    document.documentElement.style.colorScheme = 'light';
    window.dispatchEvent(new CustomEvent('app-error', {detail:new Error('Theme could not load. Showing the default light appearance.')}));
    return false;
  }
}
