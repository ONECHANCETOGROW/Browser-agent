// Dashboard: where things stand, what needs you, and what changed. No raw sensitive value appears here, only masks and counts.
import { api } from './api.js';
import { ago, chip, el, emptyState, fmtTime, plural, stateChip, toast } from './ui.js';

const KIND = { spreadsheet: 'Excel workbook', markdown: 'Markdown file', manual: 'Entered by hand', document: 'Document' };

export function importsTable(rows, { link = true } = {}) {
  if (!rows.length) return el('p', { class: 'muted', text: 'No imports yet.' });
  return el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Import', 'Type', 'Source', 'When', 'Records', 'Status'].map((h) => el('th', { text: h })))),
    el('tbody', null, rows.map((r) => {
      const c = r.counts || {};
      const recs = (c.facts_new || 0) + (c.facts_updated || 0) + (c.facts_chosen || 0) + (c.records_new || 0) + (c.records_updated || 0) + (c.records_chosen || 0) + (c.clients_created || 0);
      const status = r.status === 'committed' ? ((c.items_rejected || 0) + (c.facts_kept || 0) + (c.records_kept || 0) ? 'Partially accepted' : 'Imported') : r.status === 'rejected' ? 'Rejected' : r.status === 'failed' ? 'Failed' : r.status;
      return el('tr', { class: link ? 'click' : '', on: link ? { click: () => { location.hash = '#/imports/' + r.session_id; } } : {} },
        el('td', null, el('span', { class: 'mono', text: r.session_id })), el('td', { text: KIND[r.kind] || r.kind }), el('td', { text: r.workbook }), el('td', { class: 'nowrap', text: ago(r.created_at), title: fmtTime(r.created_at) }),
        el('td', { text: r.status === 'committed' ? String(recs) : '—' }), el('td', null, stateChip(status)));
    }))));
}

export function changesList(events) {
  if (!events.length) return el('p', { class: 'muted', text: 'No changes yet.' });
  return el('ul', { style: 'list-style:none;margin:0;padding:0' }, events.map((e) => el('li', { class: 'row space', style: 'padding:7px 0;border-bottom:1px solid var(--line)' },
    el('span', null, el('b', { text: e.text }), e.field ? ' · ' + e.field : '', el('span', { class: 'muted', text: ' · ' + e.scope_name })), el('span', { class: 'faint small nowrap', text: ago(e.ts), title: fmtTime(e.ts) }))));
}

export async function viewDashboard(ctx) {
  const d = await api.get('/dashboard');
  const o = d.owner.completion;
  const empty = !d.counts.values && !d.counts.clients;
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Dashboard' }), el('p', { text: 'Your private business and client knowledge base: what is recorded, what needs you, and what changed. Saving and importing here never calls an AI model.' }))));
  if (empty) {
    page.append(el('div', { class: 'card' }, emptyState('Welcome. Nothing is recorded yet.', 'Start with whichever is easiest. You can always change or remove anything later; nothing is ever permanently deleted.',
      el('a', { class: 'btn primary', href: '#/my-business', text: 'Add my business' }), el('a', { class: 'btn', href: '#/clients/new', text: 'Add a client' }), el('a', { class: 'btn', href: '#/imports', text: 'Import a file' }))));
  }
  page.append(el('div', { class: 'grid g4' },
    el('div', { class: 'card stat' }, el('b', { text: o.percent + '%' }), el('span', { text: 'Business profile complete' }), el('div', { class: 'progress', style: 'margin:8px 0 4px' }, el('i', { style: `width:${o.percent}%` })), el('a', { href: '#/my-business', text: 'Open My Business' })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.counts.clients) }), el('span', { text: 'Clients' + (d.counts.archived_clients ? ` (+${d.counts.archived_clients} archived)` : '') }), el('a', { href: '#/clients', text: 'Manage clients' })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.counts.documents) }), el('span', { text: 'Documents' }), el('a', { href: '#/documents', text: 'Open documents' })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.counts.sources) }), el('span', { text: 'Sources' }), el('a', { href: '#/sources', text: 'Where it all came from' })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.pending.count) }), el('span', { text: 'Waiting for your confirmation' }), el('a', { href: '#/conflicts', text: 'Review' })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.conflicts.count) }), el('span', { text: 'Conflicts' }), el('a', { href: '#/conflicts', text: 'Resolve' })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.counts.values) }), el('span', { text: `Values stored (${d.counts.protected_values} protected)` })),
    el('div', { class: 'card stat' }, el('b', { text: String(d.staged) }), el('span', { text: 'Imports waiting for review' }), el('a', { href: '#/imports', text: 'Open imports' }))));
  const left = el('div', { class: 'stack' });
  const right = el('div', { class: 'stack' });
  if (o.todo.length) left.append(el('div', { class: 'card' }, el('div', { class: 'head' }, el('h2', { text: 'Finish your business profile' }), el('a', { href: '#/my-business', text: 'Go to My Business' })),
    el('ul', { style: 'margin:0;padding-left:1.1rem' }, o.todo.map((t) => el('li', null, t.label, ' ', chip(t.status === 'MISSING' ? 'not set' : t.status === 'UNVERIFIED' ? 'needs confirmation' : 'conflict', t.status === 'MISSING' ? 'gray' : 'warn'))))));
  if (d.pending.items.length) left.append(el('div', { class: 'card' }, el('div', { class: 'head' }, el('h2', { text: 'Waiting for your confirmation' }), el('a', { href: '#/conflicts', text: `See all ${d.pending.count}` })),
    el('p', { class: 'muted small', text: 'Found in a document or an import. The agent will not use these until you confirm them.' }),
    el('ul', { style: 'list-style:none;margin:0;padding:0' }, d.pending.items.map((p) => el('li', { class: 'row space', style: 'padding:7px 0;border-bottom:1px solid var(--line)' },
      el('span', null, el('b', { text: p.label }), el('span', { class: 'muted', text: ' · ' + p.scope_name + ' · ' + p.source + ' · ' + p.mask })),
      el('button', { type: 'button', class: 'small primary', text: 'Confirm', on: { click: async (ev) => { ev.currentTarget.disabled = true; try { await api.post(`/fact/${p.fact_id}/confirm`); toast('Confirmed.', 'ok'); ctx.rerender(); ctx.refreshBadges(); } catch (e) { toast(e.message, 'bad'); } } } }))))));
  if (d.conflicts.items.length) left.append(el('div', { class: 'card' }, el('div', { class: 'head' }, el('h2', { text: 'Conflicts' }), el('a', { href: '#/conflicts', text: 'Resolve' })),
    el('ul', { style: 'margin:0;padding-left:1.1rem' }, d.conflicts.items.map((c) => el('li', null, el('b', { text: c.label }), ` (${c.scope_name}) — ${c.candidates.length} different values`)))));
  left.append(el('div', { class: 'card' }, el('div', { class: 'head' }, el('h2', { text: 'Recent imports' }), el('a', { href: '#/imports', text: 'Import Center' })), importsTable(d.imports)));
  right.append(el('div', { class: 'card' }, el('div', { class: 'head' }, el('h2', { text: 'Recent changes' }), el('a', { href: '#/activity', text: 'All activity' })), changesList(d.changes)));
  const s = d.security;
  right.append(el('div', { class: 'card' }, el('div', { class: 'head' }, el('h2', { text: 'Security' }), el('a', { href: '#/security', text: 'Details' })),
    el('dl', { class: 'kv' }, el('dt', { text: 'Protected values' }), el('dd', null, chip(s.strong ? 'Encrypted with your Windows account' : 'Weak protection', s.strong ? 'ok' : 'bad')),
      el('dt', { text: 'Database' }), el('dd', null, chip(s.healthy ? 'Healthy' : 'Needs attention', s.healthy ? 'ok' : 'bad')),
      el('dt', { text: 'Network' }), el('dd', null, chip(s.local_only ? 'This computer only' : 'NOT local only', s.local_only ? 'ok' : 'bad')),
      el('dt', { text: 'Last backup' }), el('dd', { text: s.last_backup ? ago(s.last_backup) : 'No backup yet' }))));
  page.append(el('div', { class: 'grid g2', style: 'grid-template-columns: minmax(0,1.6fr) minmax(0,1fr)' }, left, right));
  ctx.mount(page);
  ctx.refreshBadges();
}
