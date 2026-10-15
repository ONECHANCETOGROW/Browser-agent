// Small UI toolkit for the Knowledge Hub: DOM builder (text only, never HTML), icons, toasts, modal dialogs, chips, empty/error/loading states.
// Nothing here ever sets innerHTML: every string that comes from the agent, a file or a person is inserted as text.

const SVGNS = 'http://www.w3.org/2000/svg';

// A conditional child that is null / undefined / false must simply be absent (the native append() would print the word "null").
const skip = (x) => x === null || x === undefined || x === false;
const nativeAppend = Element.prototype.append;
const nativeReplace = Element.prototype.replaceChildren;
Element.prototype.append = function (...kids) { return nativeAppend.apply(this, kids.flat(Infinity).filter((x) => !skip(x))); };
Element.prototype.replaceChildren = function (...kids) { return nativeReplace.apply(this, kids.flat(Infinity).filter((x) => !skip(x))); };

export function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else if (k === 'attrs') for (const [a, b] of Object.entries(v)) e.setAttribute(a, b);
    else if (k in e && k !== 'list') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

const ICONS = {
  home: 'M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z',
  building: 'M4 21V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v17M15 9h4a1 1 0 0 1 1 1v11M8 7h3M8 11h3M8 15h3M3 21h18',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h6',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16M21 21l-4.3-4.3',
  link: 'M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1',
  upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
  activity: 'M22 12h-4l-3 9L9 3l-3 9H2',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  gear: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6M19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3 1a7 7 0 0 0-2-1.2L14 3h-4l-.6 2.7a7 7 0 0 0-2 1.2l-2.3-1-2 3.4 2 1.5a7 7 0 0 0 0 2.4l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 2 1.2L10 21h4l.6-2.7a7 7 0 0 0 2-1.2l2.3 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2',
  alert: 'M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0',
  target: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20M12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4',
};

export function icon(name) {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', '18'); s.setAttribute('height', '18'); s.setAttribute('fill', 'none');
  s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '1.8'); s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round'); s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS(SVGNS, 'path');
  p.setAttribute('d', ICONS[name] || ICONS.file);
  s.append(p);
  return s;
}

// ---------------------------------------------------------------- toasts
export function toast(text, kind = 'info', ms = 5200) {
  const t = el('div', { class: 'toast ' + kind, text });
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? Math.max(ms, 9000) : ms);
  return t;
}

// ---------------------------------------------------------------- modal (native <dialog>: focus trap, Esc to close)
export function modal({ title, body, buttons, wide = false, onOpen }) {
  const dlg = document.getElementById('modal');
  return new Promise((resolve) => {
    dlg.className = 'modal' + (wide ? ' wide' : '');
    let done = false;
    const finish = (v) => { if (done) return; done = true; if (dlg.open) dlg.close(); dlg.replaceChildren(); resolve(v); };
    const foot = el('div', { class: 'mf' }, (buttons || [{ label: 'OK', value: true, kind: 'primary' }]).map((b) => el('button', {
      type: 'button', class: b.kind || '', text: b.label, disabled: !!b.disabled,
      on: { click: () => { if (b.validate && !b.validate()) return; finish(b.value); } },
    })));
    dlg.replaceChildren(el('div', { class: 'mh' }, el('h2', { text: title })), el('div', { class: 'mb' }, body), foot);
    dlg.onclose = () => finish(null);
    dlg.oncancel = () => finish(null);
    dlg.showModal();
    if (onOpen) onOpen(dlg);
    const first = dlg.querySelector('input:not([type=hidden]), select, textarea');
    if (first) first.focus(); else foot.querySelector('button.primary, button:last-child')?.focus();
  });
}

export async function confirmDialog({ title, message, confirmLabel = 'Confirm', danger = false, details }) {
  const body = el('div', null, el('p', { text: message }), details || null);
  const r = await modal({ title, body, buttons: [{ label: 'Cancel', value: false }, { label: confirmLabel, value: true, kind: danger ? 'danger solid' : 'primary' }] });
  return r === true;
}

// ---------------------------------------------------------------- small pieces
export const chip = (text, kind = 'gray', title) => el('span', { class: 'chip ' + kind, text, title });
export function statusChip(status) {
  const m = { VERIFIED: ['Verified', 'ok'], UNVERIFIED: ['Needs your confirmation', 'warn'], MISSING: ['Not set', 'gray'], CONFLICTING: ['Conflict', 'bad'] }[status] || [status, 'gray'];
  return chip(m[0], m[1]);
}
export const sensChip = (s) => (s === 'HIGH' ? chip('Protected', 'high', 'Stored encrypted and never shown to an AI model') : null);
export function stateChip(s) {
  const t = String(s || '');
  const k = /imported|committed|verified|approved|ready|new|added|active/i.test(t) ? 'ok' : /conflict|failed|rejected|problem/i.test(t) ? 'bad' : /waiting|preview|unverified|pending|partial|update/i.test(t) ? 'warn' : 'gray';
  return chip(t, k);
}
export const skeleton = (n = 4) => el('div', null, Array.from({ length: n }, (_, i) => el('div', { class: 'skeleton', style: `width:${90 - i * 9}%` })));
export function emptyState(title, text, ...actions) {
  return el('div', { class: 'empty-state' }, el('h3', { text: title }), text ? el('p', { text }) : null, actions.length ? el('div', { class: 'actions' }, actions) : null);
}
export function errorState(message, retry) {
  return el('div', { class: 'card' }, el('div', { class: 'notice bad' }, el('strong', { text: 'That did not work. ' }), message),
    retry ? el('div', { class: 'actions', style: 'margin-top:12px' }, el('button', { type: 'button', text: 'Try again', on: { click: retry } })) : null);
}
export function notice(text, kind = '') { return el('div', { class: 'notice ' + kind, text }); }

const fmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
export const fmtTime = (ts) => (ts ? fmt.format(new Date(ts * 1000)) : '—');
export function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 86400 * 14) return Math.floor(s / 86400) + ' days ago';
  return fmt.format(new Date(ts * 1000));
}

let uid = 0;
export function field(labelText, input, hint, error) {
  const id = 'f' + (++uid);
  input.id = id;
  return el('div', { class: 'field' }, el('label', { for: id, text: labelText }), input, hint ? el('div', { class: 'hint', text: hint }) : null, error ? el('div', { class: 'err', text: error }) : null);
}

export function chipsInput(initial = [], placeholder = 'Type and press Enter') {
  const values = [...initial];
  const input = el('input', { type: 'text', placeholder, maxLength: 300, attrs: { 'aria-label': placeholder } });
  const box = el('div', { class: 'chips-input' });
  const paint = () => {
    box.replaceChildren(...values.map((v, i) => el('span', { class: 'tag' }, v, el('button', { type: 'button', text: '×', attrs: { 'aria-label': 'Remove ' + v }, on: { click: () => { values.splice(i, 1); paint(); } } }))), input);
  };
  const add = () => { const v = input.value.trim(); if (v && !values.some((x) => x.toLowerCase() === v.toLowerCase())) values.push(v); input.value = ''; paint(); input.focus(); };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } else if (e.key === 'Backspace' && !input.value && values.length) { values.pop(); paint(); input.focus(); } });
  input.addEventListener('blur', () => { if (input.value.trim()) add(); });
  paint();
  return { el: box, values: () => { if (input.value.trim()) add(); return [...values]; } };
}

export function downloadBlob(name, blob) {
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

export const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
export const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
