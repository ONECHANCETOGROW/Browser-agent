// The generic editor for catalog fields (a business's or a client's). One component renders every kind: plain text, e-mail, phone, web address, date, choice lists, multi-line text,
// addresses (several parts saved together), lists of entries (services, links ...) and protected values (masked, revealed on request, replaced by typing a new one).
// It only ASKS the agent to save: validation, secrets refusal, encryption, history and the "this replaces something" confirmation are decided by the agent's Python code.

import { api } from './api.js';
import { chip, confirmDialog, el, modal, sensChip, statusChip, toast } from './ui.js';

const LONG = new Set(['identity.description', 'marketing.content_notes', 'account.internal_notes', 'services.description']);
const PLACEHOLDER = { url: 'https://example.com', email: 'name@example.com', phone: '+1 555 010 1000', date: 'YYYY-MM-DD', pin: '123456' };

function inputFor(f, initial, partKind) {
  const kind = partKind || f.kind;
  const high = f.sensitivity === 'HIGH';
  let inp;
  if (f.enum && !partKind) {
    inp = el('select', null, el('option', { value: '', text: 'Choose…' }), f.enum.map((o) => el('option', { value: o, text: o })));
  } else if (LONG.has(f.key) && !partKind) {
    inp = el('textarea', { rows: 4, maxLength: 2000 });
  } else {
    const type = high ? 'password' : { email: 'email', phone: 'tel', url: 'url', date: 'date' }[kind] || 'text';
    inp = el('input', { type, maxLength: 2000, placeholder: PLACEHOLDER[kind] || '', attrs: { autocomplete: high ? 'new-password' : 'off', spellcheck: 'false' } });
  }
  inp.setAttribute('aria-label', f.label);
  if (initial) inp.value = initial;
  return inp;
}

function show(v, f) {
  if (v && typeof v === 'object') return f.parts.map((p) => v[p.name]).filter(Boolean).join(', ');
  return v == null ? '' : String(v);
}

export function fieldSection({ scope, title, intro, fields, onChange, locked = false }) {
  const editors = new Map();      // editor id -> { key, read(): value|null, changed(): bool }
  let seq = 0;
  const root = el('div', { class: 'card' });
  const bar = el('div', { class: 'savebar', hidden: true });
  const count = el('span', { class: 'muted' });
  const boxes = [];               // { f, box } so a refused value can be marked next to its field

  const refreshBar = () => {
    const n = [...editors.values()].filter((e) => e.changed()).length;
    bar.hidden = n === 0;
    count.textContent = n === 1 ? '1 unsaved change' : `${n} unsaved changes`;
  };

  function register(key, read, changed, onGone) {
    const id = ++seq;
    editors.set(id, { key, read, changed, onGone });
    return id;
  }
  const drop = (id) => { editors.delete(id); refreshBar(); };

  // ---- an editor for one value (single input, or the parts of an address)
  function editor(f, key, current, { onCancel, placeholder } = {}) {
    const wrap = el('div', { class: 'entry', style: 'width:100%' });
    let read, changed;
    if (f.compound) {
      const inputs = f.parts.map((p) => { const i = inputFor(f, current && current[p.name], p.kind); i.dataset.part = p.name; i.setAttribute('aria-label', p.label); return [p, i]; });
      wrap.append(el('div', { class: 'parts' }, inputs.map(([p, i]) => el('div', null, el('label', { text: p.label }), i))));
      read = () => { const v = {}; for (const [p, i] of inputs) if (i.value.trim()) v[p.name] = i.value.trim(); return Object.keys(v).length ? v : null; };
      changed = () => { const r = read(); return !!r && JSON.stringify(r) !== JSON.stringify(Object.fromEntries(Object.entries(current || {}).filter(([, x]) => x))); };
      for (const [, i] of inputs) i.addEventListener('input', refreshBar);
    } else {
      const initial = current == null ? '' : String(current);
      const inp = inputFor(f, initial);
      if (placeholder) inp.placeholder = placeholder;
      wrap.append(inp);
      if (f.sensitivity === 'HIGH' && inp.type === 'password') {
        wrap.append(el('button', { type: 'button', class: 'small', text: 'Show', on: { click: (e) => { inp.type = inp.type === 'password' ? 'text' : 'password'; e.currentTarget.textContent = inp.type === 'password' ? 'Show' : 'Hide'; } } }));
      }
      read = () => (inp.value.trim() ? inp.value.trim() : null);
      changed = () => !!read() && read() !== initial;
      inp.addEventListener('input', refreshBar);
    }
    const id = register(key, read, changed);
    if (onCancel) wrap.append(el('button', { type: 'button', class: 'small', text: 'Cancel', on: { click: () => { drop(id); onCancel(); } } }));
    wrap.dataset.editor = String(id);
    return wrap;
  }

  // ---- reveal / actions
  async function reveal(c, node) {
    try {
      const r = await api.post(`/fact/${c.fact_id}/reveal`);
      node.textContent = r.value;
      setTimeout(() => { node.textContent = c.mask || '••••••'; }, (r.hide_after_s || 10) * 1000);
    } catch (e) { toast(e.message, 'bad'); }
  }
  const act = async (fn, okText) => { try { await fn(); if (okText) toast(okText, 'ok'); await onChange(); } catch (e) { toast(e.message, 'bad'); } };

  function valueLine(f, entry, cand) {
    const high = f.sensitivity === 'HIGH' || entry.sensitivity === 'HIGH';
    const v = cand.value;
    const node = el('span', { class: 'v' + (high || v == null ? ' masked' : ''), text: v == null ? (cand.mask || '••••••') : show(v, f) });
    return { node, high, v };
  }

  function renderEntry(f, entry) {
    const row = el('div');
    const high = f.sensitivity === 'HIGH' || entry.sensitivity === 'HIGH';
    const key = entry.display_key;
    if (entry.status === 'CONFLICTING') {
      row.append(el('div', { class: 'notice warn', text: 'Two different values are stored. The agent will not use either until you choose.' }));
      for (const c of entry.candidates) {
        const { node, v } = valueLine(f, entry, c);
        const line = el('div', { class: 'cand' }, el('div', { class: 'val' }, node, chip(c.verification === 'VERIFIED' ? 'Verified' : 'Not confirmed', c.verification === 'VERIFIED' ? 'ok' : 'warn'), el('span', { class: 'src', text: c.source + (c.source_ref ? ' · ' + c.source_ref : '') })),
          el('div', { class: 'row', style: 'margin-top:6px' },
            el('button', { type: 'button', class: 'small primary', text: 'Keep this one', on: { click: () => act(() => api.post(`/scope/${scope}/resolve`, { key, fact_id: c.fact_id }), 'Conflict resolved') } }),
            el('button', { type: 'button', class: 'small', text: 'Reject this one', on: { click: () => act(() => api.post(`/fact/${c.fact_id}/reject`), 'Rejected') } }),
            high || v == null ? el('button', { type: 'button', class: 'small ghost', text: 'Reveal', on: { click: () => reveal(c, node) } }) : null));
        row.append(line);
      }
      return row;
    }
    const c = entry.candidates[0];
    if (!c) return row;
    const { node } = valueLine(f, entry, c);
    const view = el('div', { class: 'entry' }, node);
    if (entry.status === 'UNVERIFIED') view.append(chip('Needs your confirmation', 'warn'));
    view.append(el('span', { class: 'src', text: c.source + (c.source_ref ? ' · ' + c.source_ref : '') }));
    if (!c.format_ok) view.append(chip('check format', 'warn', c.format_note));
    const tools = el('span', { class: 'row' });
    if (!locked) {
      tools.append(el('button', { type: 'button', class: 'small ghost', text: 'Edit', on: { click: () => startEdit() } }));
      if (entry.status === 'UNVERIFIED') tools.append(el('button', { type: 'button', class: 'small ghost', text: 'Confirm', on: { click: () => act(() => api.post(`/fact/${c.fact_id}/confirm`), 'Confirmed') } }));
      tools.append(el('button', { type: 'button', class: 'small ghost', text: 'Remove', on: { click: async () => {
        if (await confirmDialog({ title: 'Remove this value?', message: `“${f.label}” stops being used. The history is kept; you can enter a value again at any time.`, confirmLabel: 'Remove', danger: true })) {
          act(() => api.post(`/scope/${scope}/remove`, { key }), 'Removed');
        }
      } } }));
    }
    if (high) tools.append(el('button', { type: 'button', class: 'small ghost', text: 'Reveal', on: { click: () => reveal(c, node) } }));
    view.append(tools);
    row.append(view);
    function startEdit() {
      const ed = editor(f, key, c.value, { placeholder: high ? 'Type the new value (the current one stays protected)' : '', onCancel: () => { ed.replaceWith(view); view.hidden = false; } });
      view.hidden = true;
      view.after(ed);
      ed.querySelector('input, textarea, select')?.focus();
    }
    return row;
  }

  function renderField(f) {
    const box = el('div', { class: 'fld' });
    box.append(el('div', { class: 'fh' }, el('b', { text: f.label }), statusChip(f.status), sensChip(f.sensitivity)));
    boxes.push({ f, box });
    if (f.many) {
      const list = el('div');
      for (const e of f.entries) list.append(renderEntry(f, e));
      const adders = el('div');
      const addRow = () => {
        const ed = editor(f, f.key, null, { onCancel: () => ed.remove(), placeholder: f.entries.length ? 'Add another' : '' });
        adders.append(ed);
        ed.querySelector('input, textarea, select')?.focus();
      };
      box.append(list, adders);
      if (!locked) {
        if (!f.entries.length) addRow(); else box.append(el('button', { type: 'button', class: 'small ghost', text: '+ Add another', on: { click: addRow } }));
      }
    } else {
      const e = f.entries[0];
      if (e.status === 'MISSING') {
        if (locked) box.append(el('span', { class: 'muted', text: 'Not set' }));
        else box.append(editor(f, f.key, null));
      } else box.append(renderEntry(f, e));
      if (e.status === 'CONFLICTING' && !locked) {
        box.append(el('button', { type: 'button', class: 'small ghost', text: 'Enter a different value instead', on: { click: (ev) => { ev.currentTarget.replaceWith(editor(f, f.key, null)); } } }));
      }
    }
    return box;
  }

  async function save() {
    const items = [];
    for (const e of editors.values()) if (e.changed()) items.push({ key: e.key, value: e.read() });
    if (!items.length) return;
    root.querySelectorAll('.err.server').forEach((n) => n.remove());
    let body = { items };
    for (let round = 0; round < 3; round++) {
      let res;
      try { res = await api.post(`/scope/${scope}/save`, body); } catch (e) { toast(e.message, 'bad'); return; }
      if (res.ok) {
        const n = res.saved.length;
        toast(n ? `Saved ${n} value${n === 1 ? '' : 's'}.` : 'Nothing changed.', 'ok');
        await onChange();
        return;
      }
      if (res.problems) {
        for (const p of res.problems) {
          const hit = boxes.find((b) => b.f.key === String(p.key).replace(/\[\d*\]/g, '[]') || b.f.key === String(p.key));
          (hit ? hit.box : root).append(el('div', { class: 'err server', text: p.message }));
        }
        toast('Some values could not be saved. Nothing was changed: fix the marked fields.', 'bad');
        return;
      }
      if (res.needs_confirmation) {
        const list = el('ul', null, res.needs_confirmation.map((n) => el('li', null, el('b', { text: n.label + ': ' }), n.message, n.reason === 'replace' ? el('div', { class: 'muted small', text: `Now: ${n.existing}  →  New: ${n.new}` }) : null)));
        const ok = await modal({ title: 'Please confirm', body: list, buttons: [{ label: 'Cancel', value: false }, { label: 'Yes, save', value: true, kind: 'primary' }] });
        if (!ok) return;
        body = { items, confirm_replace: true, allow_format: true };
        continue;
      }
      toast(res.error || 'The agent refused that.', 'bad');
      return;
    }
  }

  function discard() { editors.clear(); onChange(); }

  root.append(el('div', { class: 'head' }, el('h2', { text: title })), intro ? el('p', { class: 'muted', text: intro }) : null);
  for (const f of fields) root.append(renderField(f));
  bar.append(count, el('span', { class: 'row' }, el('button', { type: 'button', text: 'Discard', on: { click: discard } }), el('button', { type: 'button', class: 'primary', text: 'Save changes', on: { click: save } })));
  root.append(bar);
  return root;
}
