// Conflicts, Documents, SEO, Sources, Activity, Security and Settings pages.
import { api } from './api.js';
import { fieldSection } from './fields.js';
import { activityTable, exportButton, recordsPanel, sourcesTable } from './shared.js';
import { researchCard } from './v_clients.js';
import { campaignsCard } from './v_collab.js';
import { ago, chip, confirmDialog, el, emptyState, field, fmtTime, modal, notice, plural, stateChip, toast } from './ui.js';

const where = (scope, scopeName) => (scope === 'me' ? '#/my-business' : `#/clients/${scope}/business`);

// ================================================================== conflicts and confirmations
export async function viewConflicts(ctx) {
  const d = await api.get('/conflicts');
  const imp = await api.get('/imports');
  const page = el('div', { class: 'stack' });
  const again = () => { ctx.rerender(); ctx.refreshBadges(); };
  const act = async (fn, msg) => { try { await fn(); toast(msg, 'ok'); again(); } catch (e) { toast(e.message, 'bad'); } };

  const candidate = (c, x) => el('div', { class: 'cand', style: 'border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:8px 0' },
    el('div', { class: 'val' }, el('span', { class: 'v' + (x.value == null ? ' masked' : ''), text: x.value == null ? x.mask : x.value }),
      chip(x.verification === 'VERIFIED' ? 'Verified' : 'Not confirmed', x.verification === 'VERIFIED' ? 'ok' : 'warn'), el('span', { class: 'src', text: x.source + (x.source_ref ? ' · ' + x.source_ref : '') })),
    el('div', { class: 'row', style: 'margin-top:6px' },
      el('button', { type: 'button', class: 'small primary', text: 'Keep this one', on: { click: () => act(() => api.post(`/scope/${c.scope}/resolve`, { key: c.key, fact_id: x.fact_id }), 'Conflict resolved. The other value is kept as history.') } }),
      el('button', { type: 'button', class: 'small', text: 'Reject this one', on: { click: () => act(() => api.post(`/fact/${x.fact_id}/reject`), 'Rejected.') } })));
  const conflictCard = (c) => el('div', { class: 'card' },
    el('div', { class: 'row space' }, el('div', null, el('b', { text: c.label }), el('span', { class: 'muted', text: ' · ' + c.scope_name }), c.sensitivity === 'HIGH' ? chip('Protected: masked', 'high') : null),
      el('a', { class: 'btn small', href: where(c.scope), text: 'Edit by hand' })),
    c.candidates.map((x) => candidate(c, x)));
  const pendingRow = (p) => el('tr', null,
    el('td', null, el('a', { href: where(p.scope), text: p.scope_name })), el('td', null, p.label, p.sensitivity === 'HIGH' ? el('div', null, chip('Protected', 'high')) : null),
    el('td', { class: 'mono', text: p.mask }), el('td', { class: 'muted', text: p.source }),
    el('td', { class: 'nowrap' }, el('button', { type: 'button', class: 'small primary', text: 'Confirm', on: { click: () => act(() => api.post(`/fact/${p.fact_id}/confirm`), 'Confirmed.') } }), ' ',
      el('button', { type: 'button', class: 'small', text: 'Reject', on: { click: () => act(() => api.post(`/fact/${p.fact_id}/reject`), 'Rejected.') } })));

  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Conflicts and confirmations' }),
    el('p', { text: 'Whenever two sources disagree, or a value has not been confirmed by you, it waits here. The agent does not use a conflicting or unconfirmed value, and nothing is ever overwritten silently.' }))));
  if (imp.staged.length) page.append(el('div', { class: 'notice warn' }, `${plural(imp.staged.length, 'import')} waiting for review. Conflicts found while importing are decided in the import review: `, el('a', { href: '#/imports', text: 'open Imports' }), '.'));
  page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: `Conflicts (${d.conflicts.length})` })),
    d.conflicts.length ? el('div', { class: 'stack', style: 'padding:0 18px 18px' }, d.conflicts.map(conflictCard)) : emptyState('No conflicts', 'Everything stored agrees with itself.')));
  const head = el('thead', null, el('tr', null, ['Scope', 'Field', 'Value', 'Source', ''].map((h) => el('th', { text: h }))));
  page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: `Waiting for your confirmation (${d.pending.length})` })),
    d.pending.length ? el('div', { class: 'tablewrap' }, el('table', null, head, el('tbody', null, d.pending.map(pendingRow)))) : emptyState('Nothing waiting', 'Everything stored has been confirmed by you.')));
  ctx.mount(page);
}

// ================================================================== documents
export async function viewDocuments(ctx) {
  const d = await api.get('/documents');
  const scopes = ctx.scopes();
  const name = (id) => (!id ? scopes.owner.name : (scopes.clients.find((c) => c.id === id) || { name: id }).name);
  const page = el('div', { class: 'stack' });
  const again = () => ctx.rerender();
  const run = async (fn, msg) => { try { const r = await fn(); toast(typeof msg === 'function' ? msg(r) : msg, 'ok'); again(); } catch (e) { toast(e.message, 'bad'); } };
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Documents' }),
    el('p', { text: 'Documents are only ever read from folders you have authorized and enrolled, one owner per folder. Authorizing and enrolling stay in the terminal on purpose: that is what stops any web page or model from widening what the agent can read.' }))));
  page.append(el('div', { class: 'card' }, el('h2', { text: 'The five steps' }), el('ol', { style: 'padding-left:1.2rem;margin:10px 0 0' }, d.steps.map((s) => el('li', { style: 'margin:8px 0' },
    el('b', { text: s.title }), ' ', s.done ? chip('done', 'ok') : chip('to do', 'gray'), el('div', { class: 'muted small', text: s.who }), s.command ? el('div', null, el('code', { text: s.command })) : null))),
    el('div', { class: 'actions', style: 'margin-top:12px' },
      el('button', { type: 'button', text: 'Scan enrolled folders', disabled: !d.enrolled.length, on: { click: () => run(() => api.post('/documents/scan'), (r) => `Scan finished: ${r.report.new_documents ?? r.report.added ?? 0} new.`) } }),
      el('button', { type: 'button', text: 'Read scanned documents', disabled: !d.documents.length, on: { click: () => run(() => api.post('/documents/index', {}), 'Documents were read. Facts found are only candidates until you confirm them.') } }))));
  d.authorized.length && page.append(el('div', { class: 'card' }, el('h3', { text: 'Authorized folders' }), el('ul', { style: 'margin:6px 0 0;padding-left:1.1rem' }, d.authorized.map((a) => el('li', { class: 'mono', text: a.path + (d.enrolled.some((e) => e.path.toLowerCase() === a.path.toLowerCase()) ? '   [enrolled]' : '') })))));
  page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: `Documents (${d.documents.length})` })), d.documents.length ? el('div', { class: 'tablewrap' }, el('table', null,
    el('thead', null, el('tr', null, ['Document', 'Belongs to', 'Type', 'Status', 'Facts found', 'Note', ''].map((h) => el('th', { text: h })))),
    el('tbody', null, d.documents.map((x) => el('tr', null, el('td', null, x.name, x.sensitivity === 'HIGH' ? el('div', null, chip('Protected', 'high')) : null), el('td', { text: name(x.client_id) }), el('td', { text: x.doc_type }), el('td', null, stateChip(x.state)),
      el('td', { text: String(x.fields_found ?? 0) }), el('td', { class: 'muted small', text: x.note || '' }), el('td', { class: 'nowrap' },
        ['INDEXED', 'DISCOVERED'].includes(x.state) ? el('button', { type: 'button', class: 'small primary', text: 'Approve for use', on: { click: () => run(() => api.post(`/documents/${x.doc_id}/approve`), 'Approved.') } }) : null, ' ',
        x.state !== 'EXCLUDED' && x.state !== 'BLOCKED' ? el('button', { type: 'button', class: 'small', text: 'Exclude', on: { click: () => run(() => api.post(`/documents/${x.doc_id}/exclude`), 'Excluded.') } }) : null)))))) :
    emptyState('No documents yet', 'Follow the steps above. The agent never reads a folder you have not authorized and enrolled.')));
  ctx.mount(page);
}

// ================================================================== SEO workspace
export async function viewSeo(ctx, cid) {
  const scopes = ctx.scopes();
  const d = await api.get('/seo' + (cid ? '?client=' + encodeURIComponent(cid) : ''));
  const page = el('div', { class: 'stack' });
  const picker = el('select', { style: 'max-width:300px', attrs: { 'aria-label': 'Choose a client' }, on: { change: (e) => { location.hash = '#/seo' + (e.target.value ? '/' + e.target.value : ''); } } },
    el('option', { value: '', text: 'All clients' }), scopes.clients.filter((c) => !c.archived).map((c) => el('option', { value: c.id, text: c.name, selected: c.id === cid })));
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'SEO workspace' }), el('p', { text: 'Each client’s SEO targets, campaigns and research: keywords and places they want to be found for, dated search observations, audits and reports. Research runs through the AI collaboration layer.' })),
    el('div', { class: 'actions' }, picker, exportButton('seo', cid || 'all', 'Export SEO targets'))));
  page.append(notice(d.engine));
  if (!d.clients.length) { page.append(el('div', { class: 'card' }, emptyState('No clients yet', 'Add a client first; then add their SEO targets here.', el('a', { class: 'btn primary', href: '#/clients/new', text: '+ Add client' })))); ctx.mount(page); return; }
  if (!cid) {
    page.append(el('div', { class: 'card flush' }, el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Client', 'SEO targets', 'Sample keywords', 'Research sessions', 'Observations', ''].map((h) => el('th', { text: h })))),
      el('tbody', null, d.clients.map((c) => el('tr', { class: 'click', on: { click: () => { location.hash = '#/seo/' + c.client_id; } } }, el('td', null, el('b', { text: c.name })), el('td', { text: String(c.targets.length) }),
        el('td', null, c.targets.slice(0, 3).map((t) => el('span', { class: 'tag', text: t.keyword }))), el('td', { text: String(c.summary.sessions || 0) }), el('td', { text: String(c.summary.observations || 0) }), el('td', null, el('a', { class: 'btn small', href: '#/seo/' + c.client_id, text: 'Open' })))))))));
    ctx.mount(page);
    return;
  }
  const c = d.clients[0];
  const archived = (scopes.clients.find((x) => x.id === cid) || {}).archived;
  const refresh = () => ctx.rerender();
  page.append(el('div', { class: 'scope' }, chip('Client: ' + c.name, 'warn')));
  page.append(recordsPanel({ client: cid, kind: 'seo', rows: c.targets, onChange: refresh, canEdit: !archived, description: 'One row per keyword and the location it matters for.' }));
  page.append(fieldSection({ scope: cid, title: 'Target lists and goals', fields: c.fields, onChange: refresh, locked: archived }));
  page.append(campaignsCard(c));
  page.append(researchCard(c));
  ctx.mount(page);
}

// ================================================================== sources
export async function viewSources(ctx) {
  const d = await api.get('/sources');
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Sources' }), el('p', { text: 'Where your information came from. Every imported value remembers its file, sheet or section, and line or row; the values themselves are never copied into this list.' }))));
  page.append(el('div', { class: 'card flush' }, sourcesTable(d.items)));
  page.append(el('div', { class: 'card' }, el('h2', { text: 'Ways to bring information in' }), el('div', { class: 'grid g3', style: 'margin-top:10px' }, d.kinds.map((k) => el('div', { class: 'card', style: 'box-shadow:none' }, el('b', { text: k.label }), ' ', k.available ? chip('available', 'ok') : chip('later', 'gray'),
    k.note ? el('p', { class: 'muted small', text: k.note }) : null))), el('p', { class: 'muted small', style: 'margin-top:10px', text: 'Anything added later (such as Google Sheets) goes through the same checking and review as a file: it never gets its own database.' })));
  ctx.mount(page);
}

// ================================================================== activity
export async function viewActivity(ctx) {
  const scopes = ctx.scopes();
  const sel = el('select', { style: 'max-width:300px', attrs: { 'aria-label': 'Show activity for' } }, el('option', { value: '', text: 'Everything' }), el('option', { value: 'me', text: scopes.owner.name + ' (my business)' }),
    scopes.clients.map((c) => el('option', { value: c.id, text: c.name })));
  const holder = el('div', { class: 'card flush' });
  const load = async () => { const a = await api.get('/activity?n=150' + (sel.value ? '&scope=' + encodeURIComponent(sel.value) : '')); holder.replaceChildren(activityTable(a.events, { showScope: !sel.value })); };
  sel.addEventListener('change', load);
  const page = el('div', { class: 'stack' }, el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Activity' }), el('p', { text: 'Every change, import, confirmation and conflict resolution. It records what happened and where, never the values, and never any password, code, key or card.' })), el('div', { class: 'actions' }, sel)), holder);
  await load();
  ctx.mount(page);
}

// ================================================================== security
async function pinDialog(isSet, repaint, remove = false) {
  const cur = el('input', { type: 'password', inputMode: 'numeric', autocomplete: 'off', attrs: { 'aria-label': 'Current PIN' } });
  const one = el('input', { type: 'password', inputMode: 'numeric', autocomplete: 'off', maxLength: 12, attrs: { 'aria-label': 'New PIN' } });
  const two = el('input', { type: 'password', inputMode: 'numeric', autocomplete: 'off', maxLength: 12, attrs: { 'aria-label': 'Repeat the new PIN' } });
  const err = el('div', { class: 'err' });
  const body = el('div', { class: 'stack' }, isSet ? field('Current PIN', cur) : null, remove ? el('p', { text: 'The Hub will ask for the long access code again.' }) : [field('New PIN (4 to 12 digits)', one), field('Repeat the new PIN', two)], err);
  const ok = await modal({ title: remove ? 'Remove the PIN' : (isSet ? 'Change the PIN' : 'Set a PIN'), body, buttons: [{ label: 'Cancel', value: false }, { label: remove ? 'Remove PIN' : 'Save PIN', value: true, kind: 'primary',
    validate: () => {
      err.textContent = '';
      if (!remove && !/^[0-9]{4,12}$/.test(one.value)) { err.textContent = 'A PIN is 4 to 12 digits (numbers only).'; return false; }
      if (!remove && one.value !== two.value) { err.textContent = 'The two PINs are not the same.'; return false; }
      return true;
    } }] });
  if (!ok) return;
  try {
    await api.post('/pin', remove ? { current: cur.value, remove: true } : { current: cur.value, new: one.value });
    toast(remove ? 'PIN removed.' : 'PIN saved. Use it the next time you unlock the Hub.', 'ok');
    repaint(!remove);
  } catch (e) { toast(e.message, 'bad'); }
}

export async function viewSecurity(ctx) {
  const d = await api.get('/security');
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Security' }), el('p', { text: 'How your information is protected, and the backups that keep it safe.' }))));
  page.append(el('div', { class: 'grid g2' }, el('div', { class: 'card' }, el('h2', { text: 'Status' }), el('dl', { class: 'kv', style: 'margin-top:10px' },
    el('dt', { text: 'Protected values' }), el('dd', null, chip(d.strong ? 'Encrypted with your Windows account' : 'Weak protection', d.strong ? 'ok' : 'bad'), ` ${d.protected_values} stored`),
    el('dt', { text: 'Database' }), el('dd', null, chip(d.healthy ? 'Healthy' : 'Needs attention', d.healthy ? 'ok' : 'bad')),
    el('dt', { text: 'Network' }), el('dd', null, chip(d.local_only ? 'This computer only' : 'NOT local only', d.local_only ? 'ok' : 'bad')),
    el('dt', { text: 'Folders' }), el('dd', { text: `${d.folders.authorized} authorized, ${d.folders.enrolled} enrolled` }),
    el('dt', { text: 'Hub sign-ins' }), el('dd', null, `${d.hub_sessions} active `, el('button', { type: 'button', class: 'small', text: 'End all sessions', on: { click: async () => { if (await confirmDialog({ title: 'End all Hub sessions?', message: 'You will have to unlock the Hub again.', confirmLabel: 'End sessions' })) { try { await api.post('/sessions/end-all'); } catch (_) { /* locked already */ } location.reload(); } } } }))),
    ), el('div', { class: 'card' }, el('h2', { text: 'What is and is not shared' }), el('ul', { style: 'margin:10px 0 0;padding-left:1.1rem' }, d.statements.map((s) => el('li', { text: s }))))));
  const pinRow = el('div', { class: 'card' });
  const paintPin = (isSet) => pinRow.replaceChildren(el('div', { class: 'head' }, el('div', null, el('h2', { text: 'Sign-in PIN' }),
    el('p', { class: 'muted small', text: 'A short number (4 to 12 digits) that unlocks the Hub instead of the long access code. Only a scrambled version is stored. Too many wrong tries lock signing in for longer and longer.' })),
    el('div', { class: 'actions' }, chip(isSet ? 'PIN is set' : 'No PIN yet', isSet ? 'ok' : 'warn'),
      el('button', { type: 'button', class: 'primary', text: isSet ? 'Change PIN' : 'Set a PIN', on: { click: () => pinDialog(isSet, paintPin) } }),
      isSet ? el('button', { type: 'button', text: 'Remove PIN', on: { click: () => pinDialog(true, paintPin, true) } }) : null)));
  paintPin(!!d.pin_set);
  page.append(pinRow);
  const back = el('div', { class: 'card flush' });
  const paintBack = (list) => back.replaceChildren(el('div', { class: 'head' }, el('div', null, el('h2', { text: 'Backups' }), el('p', { class: 'muted small', text: 'A backup is a complete copy of the database. One is also made automatically before every import and every upgrade.' })),
    el('button', { type: 'button', class: 'primary', text: 'Back up now', on: { click: async (ev) => { ev.currentTarget.disabled = true; try { const r = await api.post('/backup'); toast(`Backup made: ${r.name}`, 'ok'); const s = await api.get('/security'); paintBack(s.backups); } catch (e) { toast(e.message, 'bad'); } } } })),
    list.length ? el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['File', 'Kind', 'Made', 'Size'].map((h) => el('th', { text: h })))), el('tbody', null, list.map((b) => el('tr', null, el('td', { class: 'mono', text: b.name }),
      el('td', { text: b.kind }), el('td', { class: 'nowrap', text: ago(b.modified), title: fmtTime(b.modified) }), el('td', { class: 'nowrap', text: Math.max(1, Math.round(b.size / 1024)) + ' KB' })))))) : emptyState('No backup yet', 'Press “Back up now”.'),
    el('p', { class: 'muted small', style: 'padding:0 18px 14px', text: 'Backups sit next to the database in the agent’s data folder. To restore one: stop the agent, copy the backup over business.db, start the agent again.' }));
  paintBack(d.backups);
  page.append(back);
  ctx.mount(page);
}

// ================================================================== settings
export async function viewSettings(ctx) {
  const s = await api.get('/settings');
  const scopes = ctx.scopes();
  const page = el('div', { class: 'stack' });
  const inTasks = el('input', { type: 'checkbox', checked: s.business_in_tasks });
  const intent = el('input', { type: 'checkbox', checked: s.intent_detection });
  const dflt = el('select', null, el('option', { value: '', text: 'None: ask me each time' }), scopes.clients.filter((c) => !c.archived).map((c) => el('option', { value: c.id, text: c.name, selected: s.default_scope === c.id })));
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Settings' }), el('p', { text: 'How the agent uses what you have recorded.' }))));
  page.append(el('div', { class: 'card stack' },
    el('h2', { text: 'Use in tasks' }),
    el('label', { class: 'check' }, inTasks, el('span', null, el('b', { text: 'Let tasks use my business and client information. ' }), 'When off, the agent works without it. Takes effect when the agent restarts.')),
    el('label', { class: 'check' }, intent, el('span', null, el('b', { text: 'Notice when a task is about my business. ' }), 'The agent switches the information on by itself when the wording mentions it.')),
    field('Default client for tasks', dflt, 'Used only for a task about “the client” whose wording does not name one. A client named in the wording always wins, and tasks about your own business are unaffected.'),
    el('div', { class: 'actions' }, el('button', { type: 'button', class: 'primary', text: 'Save settings', on: { click: async () => {
      try { await api.post('/settings', { business_in_tasks: inTasks.checked, intent_detection: intent.checked, default_scope: dflt.value || null }); toast('Settings saved.' + (inTasks.checked !== s.business_in_tasks ? ' Restart the agent for the first one to take effect.' : ''), 'ok'); await ctx.reloadScopes(); } catch (e) { toast(e.message, 'bad'); }
    } } }))));
  page.append(el('div', { class: 'card' }, el('h2', { text: 'This computer' }), el('dl', { class: 'kv', style: 'margin-top:10px' }, el('dt', { text: 'Hub address' }), el('dd', { class: 'mono', text: location.origin + '/hub/' }),
    el('dt', { text: 'Agent' }), el('dd', { class: 'mono', text: `${s.agent.host}:${s.agent.port}` }), el('dt', { text: 'Data folder' }), el('dd', { class: 'mono', text: s.data_dir }))));
  ctx.mount(page);
}
