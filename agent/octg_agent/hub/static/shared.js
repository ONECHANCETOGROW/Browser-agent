// Pieces used by several pages: export button, sources / activity tables, the rows editor for services, locations and SEO targets.
import { api } from './api.js';
import { ago, chip, confirmDialog, downloadBlob, el, emptyState, field, fmtTime, modal, stateChip, toast } from './ui.js';

export function exportButton(kind, scope, label) {
  return el('button', { type: 'button', text: label, title: 'An Excel file you can open and re-import. Protected values (tax ids, internal notes) are never exported.', on: { click: async (ev) => {
    const b = ev.currentTarget;
    b.disabled = true;
    try {
      const { blob, name } = await api.download(`/export/${kind}` + (scope ? `?scope=${encodeURIComponent(scope)}` : ''));
      downloadBlob(name, blob);
      toast(`Exported ${name}. Protected values are not included.`, 'ok');
    } catch (e) { toast(e.message, 'bad'); } finally { b.disabled = false; }
  } } });
}

const KIND = { spreadsheet: 'Excel workbook', markdown: 'Markdown file', manual: 'Entered by hand', document: 'Authorized documents', google_sheets: 'Google Sheets', pdf: 'PDF' };

export function sourcesTable(items, { showScope = true } = {}) {
  if (!items.length) return emptyState('No sources yet', 'Values you type, import or confirm will be listed here with where they came from.');
  const head = ['Source ID', 'Type', 'Name', ...(showScope ? ['Scope'] : []), 'Fingerprint', 'Imported', 'Last verified', 'Verification'];
  return el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, head.map((h) => el('th', { text: h })))), el('tbody', null, items.map((s) => el('tr', { class: s.source_id.startsWith('imp_') ? 'click' : '', on: s.source_id.startsWith('imp_') ? { click: () => { location.hash = '#/imports/' + s.source_id; } } : {} },
    el('td', null, el('span', { class: 'mono', text: s.source_id })), el('td', { text: KIND[s.kind] || s.kind }), el('td', { text: s.name }), showScope ? el('td', { text: s.scope_name }) : null,
    el('td', null, el('span', { class: 'mono', text: s.sha256 || '—' })), el('td', { class: 'nowrap', text: ago(s.imported_at), title: fmtTime(s.imported_at) }), el('td', { class: 'nowrap', text: s.last_verified ? ago(s.last_verified) : '—' }),
    el('td', null, stateChip(s.verification === 'VERIFIED' ? 'Verified' : 'Unverified')))))));
}

export function activityTable(events, { showScope = true } = {}) {
  if (!events.length) return emptyState('Nothing yet', 'Changes, imports and confirmations appear here. Values themselves are never shown.');
  return el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['When', 'What', 'Field', ...(showScope ? ['Scope'] : []), 'By', 'Detail'].map((h) => el('th', { text: h })))),
    el('tbody', null, events.slice().reverse().map((e) => el('tr', null, el('td', { class: 'nowrap', text: ago(e.ts), title: fmtTime(e.ts) }), el('td', null, el('b', { text: e.text })), el('td', { text: e.field }),
      showScope ? el('td', { text: e.scope_name }) : null, el('td', { text: e.actor }), el('td', { class: 'muted small', text: e.detail }))))));
}

// ---- services / locations / SEO targets of ONE client
const FORMS = {
  service: { title: 'service', label: 'Services', cols: ['service', 'category', 'priority', 'notes'], fields: [
    ['service', 'Service', 'text', true], ['category', 'Category', 'text'], ['priority', 'Priority', 'priority'], ['notes', 'Notes', 'area']] },
  location: { title: 'location', label: 'Locations', cols: ['location', 'city', 'state_region', 'location_type', 'priority'], fields: [
    ['location', 'Place name (town, neighbourhood or area)', 'text'], ['city', 'City', 'text'], ['state_region', 'State / province', 'text'], ['country', 'Country', 'text'], ['postal_code', 'ZIP / postal code', 'text'],
    ['location_type', 'Type', 'ltype'], ['priority', 'Priority', 'priority'], ['notes', 'Notes', 'area']] },
  seo: { title: 'SEO target', label: 'SEO targets', cols: ['keyword', 'location', 'service', 'priority'], fields: [
    ['keyword', 'Keyword', 'text', true], ['location', 'Location the client wants to rank in', 'text'], ['service', 'Service', 'text'], ['priority', 'Priority', 'priority'], ['notes', 'Notes', 'area']] },
};
const HEAD = { service: 'Service', category: 'Category', priority: 'Priority', notes: 'Notes', location: 'Location', city: 'City', state_region: 'State', location_type: 'Type', keyword: 'Keyword', service_: 'Service' };

export function recordsPanel({ client, kind, rows, onChange, canEdit = true, description }) {
  const cfg = FORMS[kind];
  const card = el('div', { class: 'card flush' });
  const head = el('div', { class: 'head' }, el('div', null, el('h2', { text: cfg.label }), description ? el('p', { class: 'muted small', text: description }) : null),
    canEdit ? el('button', { type: 'button', class: 'primary', text: `+ Add ${cfg.title}`, on: { click: () => edit(null) } }) : null);
  card.append(head);
  async function edit(row) {
    const inputs = {};
    const body = el('div', { class: 'cols' });
    for (const [name, label, type, required] of cfg.fields) {
      let inp;
      if (type === 'priority') inp = el('select', null, el('option', { value: '', text: '—' }), ['High', 'Medium', 'Low'].map((x) => el('option', { value: x, text: x })));
      else if (type === 'ltype') inp = el('select', null, el('option', { value: '', text: '—' }), [['primary', 'Primary'], ['service_area', 'Service area'], ['target_area', 'Target area'], ['other', 'Other']].map(([v, t]) => el('option', { value: v, text: t })));
      else if (type === 'area') inp = el('textarea', { rows: 3, maxLength: 2000 });
      else inp = el('input', { type: 'text', maxLength: 300 });
      if (row && row[name] != null) inp.value = row[name];
      inputs[name] = inp;
      body.append(field(label + (required ? ' *' : ''), inp));
    }
    const err = el('div', { class: 'err' });
    body.append(err);
    const r = await modal({ title: row ? `Edit ${cfg.title}` : `Add ${cfg.title}`, body, wide: true, buttons: [{ label: 'Cancel', value: false }, { label: 'Save', value: true, kind: 'primary' }] });
    if (r !== true) return;
    const rec = {};
    for (const [name] of cfg.fields) if (inputs[name].value.trim()) rec[name] = inputs[name].value.trim();
    try {
      await api.post(`/clients/${client}/records/${kind}`, { rec, rec_id: row ? row.rec_id : undefined });
      toast(row ? 'Saved.' : 'Added.', 'ok');
      await onChange();
    } catch (e) { toast(e.message, 'bad'); }
  }
  if (!rows.length) {
    card.append(emptyState(`No ${cfg.label.toLowerCase()} yet`, canEdit ? `Add them one at a time, or import a workbook or Markdown file.` : '', canEdit ? el('button', { type: 'button', class: 'primary', text: `+ Add ${cfg.title}`, on: { click: () => edit(null) } }) : null));
    return card;
  }
  const cols = cfg.cols;
  const removeRow = async (r) => {
    if (!(await confirmDialog({ title: 'Remove this row?', message: 'It stops being used. The history is kept.', confirmLabel: 'Remove', danger: true }))) return;
    try { await api.post(`/clients/${client}/records/${kind}/${r.rec_id}/retire`); toast('Removed.', 'ok'); await onChange(); } catch (e) { toast(e.message, 'bad'); }
  };
  const cell = (r, c) => el('td', null, c === 'priority' && r[c] ? chip(r[c], r[c] === 'High' ? 'bad' : r[c] === 'Medium' ? 'warn' : 'gray') : (r[c] || ''));
  const tr = (r) => el('tr', null, cols.map((c) => cell(r, c)), el('td', { class: 'muted small', text: r.source }),
    el('td', { class: 'nowrap' }, canEdit ? [el('button', { type: 'button', class: 'small ghost', text: 'Edit', on: { click: () => edit(r) } }),
      el('button', { type: 'button', class: 'small ghost', text: 'Remove', on: { click: () => removeRow(r) } })] : null));
  card.append(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, [...cols.map((c) => el('th', { text: HEAD[c] || c })), el('th', { text: 'Source' }), el('th', { text: '' })])), el('tbody', null, rows.map(tr)))));
  return card;
}
