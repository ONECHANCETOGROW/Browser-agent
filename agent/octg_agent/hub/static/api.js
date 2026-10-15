// The Hub's only way to talk to the agent: same-origin fetches that carry the per-session token. Nothing is stored in the browser (no localStorage, no cookies read by script).

let csrf = null;

export class ApiError extends Error {
  constructor(data, status) { super(data.error || 'The agent refused that.'); this.code = data.code || 'ERROR'; this.status = status; this.data = data; }
}

export const setCsrf = (v) => { csrf = v; };

async function send(method, path, { json, raw, headers } = {}) {
  const h = { ...(headers || {}) };
  if (csrf) h['X-Hub-CSRF'] = csrf;
  let body;
  if (json !== undefined) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json); } else if (raw !== undefined) { body = raw; }
  let res;
  try { res = await fetch('/hub/api' + path, { method, headers: h, body, credentials: 'same-origin', cache: 'no-store' }); } catch (_) { throw new ApiError({ code: 'OFFLINE', error: 'The agent is not reachable. Is it running? (start_agent.bat)' }, 0); }
  if (res.status === 401 && path !== '/login') {
    // Only "you are not signed in" locks the page. A wrong PIN is also HTTP 401 (BAD_PIN) but the session is fine: the caller shows the message and the owner stays where they are.
    let locked = true;
    try { locked = (await res.clone().json()).code === 'LOCKED'; } catch (_) { /* an unreadable 401 is treated as locked */ }
    if (locked) document.dispatchEvent(new Event('hub:locked'));
  }
  return res;
}

async function parse(res) {
  let data = {};
  try { data = await res.json(); } catch (_) { data = { ok: false, error: 'Unexpected answer from the agent.' }; }
  if (!res.ok && !(data && (data.needs_confirmation || data.problems))) throw new ApiError(data, res.status);
  return data;
}

export const api = {
  get: async (path) => parse(await send('GET', path)),
  post: async (path, json) => parse(await send('POST', path, { json: json || {} })),
  upload: async (path, bytes, headers) => parse(await send('POST', path, { raw: bytes, headers })),
  download: async (path) => {
    const res = await send('GET', path);
    if (!res.ok) { let d = {}; try { d = await res.json(); } catch (_) { /* not JSON */ } throw new ApiError(d, res.status); }
    return { blob: await res.blob(), name: res.headers.get('X-Export-Name') || 'export.xlsx' };
  },
};
