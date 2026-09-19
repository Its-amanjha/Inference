const nativeFetch = window.fetch.bind(window);
const mutations = new Set(['POST','PUT','PATCH','DELETE']);
export function readCookie(name) {
  const value = document.cookie.split('; ').find(item=>item.startsWith(`${name}=`));
  if (!value) return ''; try { return decodeURIComponent(value.slice(name.length+1)); } catch { return ''; }
}
export async function apiFetch(input, options = {}) {
  const {json, ...init} = options;
  if (json !== undefined) { init.body = JSON.stringify(json); init.headers = new Headers(init.headers); init.headers.set('Content-Type','application/json'); }
  const base = new Request(input instanceof Request ? input : new URL(input, location.href), init);
  const local = new URL(base.url).origin === location.origin;
  const protectedWrite = local && mutations.has(base.method);
  const send = () => {
    const request = base.clone();
    if (protectedWrite) { const csrf = readCookie('agd_csrf'); if (csrf) request.headers.set('X-AGD-CSRF',csrf); }
    return nativeFetch(request);
  };
  let response = await send();
  if (protectedWrite && response.status === 403) {
    let body; try { body = await response.clone().json(); } catch { /* No retry for non-JSON failures. */ }
    if (body?.code === 'csrf_failed') {
      const status = await nativeFetch('/api/auth/status',{credentials:'same-origin',signal:base.signal});
      if (status.ok) response = await send();
    }
  }
  if (local && response.status === 401 && !base.url.endsWith('/api/auth/login')) window.dispatchEvent(new Event('session-expired'));
  return response;
}
// Callers must import apiFetch/api explicitly; this module never replaces window.fetch,
// so third-party or later code cannot silently inherit credential and CSRF handling.
export async function api(path, options = {}) {
  const response = await apiFetch(path, options);
  let body; try { body = await response.json(); } catch { body = null; }
  if (!response.ok) { const error = new Error(body?.detail || `Request failed (${response.status}).`); error.status=response.status; error.code=body?.code; throw error; }
  return body;
}
