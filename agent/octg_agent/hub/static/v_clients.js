// Clients: the table, "Add client", and the client page with its tabs. Everything on a client page is scoped to that one client by the agent.
import { api } from './api.js';
import { fieldSection } from './fields.js';
import { activityTable, exportButton, recordsPanel, sourcesTable } from './shared.js';
import { ago, chip, chipsInput, confirmDialog, el, emptyState, field, fmtTime, modal, plural, stateChip, toast } from './ui.js';

const safeUrl = (u) => /^https?:\/\/[^\s]+$/i.test(u || '') ? u : (/^[\w.-]+\.[a-z]{2,}(\/\S*)?$/i.test(u || '') ? 'https://' + u : null);

// ================================================================== list
export async function viewClientsList(ctx) {
  const d = await api.get('/clients');
  let rows = d.clients;
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Clients' }), el('p', { text: 'Every client is kept completely separate from your business and from each other. Open one to see and edit everything about them.' })),
    el('div', { class: 'actions' }, exportButton('clients', 'all', 'Export all'), el('a', { class: 'btn', href: '#/imports', text: 'Import…' }), el('a', { class: 'btn primary', href: '#/clients/new', text: '+ Add client' }))));
  const filter = el('input', { type: 'search', placeholder: 'Filter clients…', attrs: { 'aria-label': 'Filter clients' }, style: 'max-width:300px' });
  const status = el('select', { attrs: { 'aria-label': 'Show' }, style: 'max-width:200px' }, el('option', { value: 'active', text: 'Active clients' }), el('option', { value: 'archived', text: 'Archived clients' }), el('option', { value: 'all', text: 'All clients' }));
  const holder = el('div', { class: 'card flush' });
  const paint = () => {
    const q = filter.value.trim().toLowerCase();
    const list = rows.filter((c) => (status.value === 'all' || (status.value === 'archived') === c.archived) && (!q || [c.name, c.ref, c.website, c.location, c.services.join(' ')].join(' ').toLowerCase().includes(q)));
    if (!rows.length) { holder.replaceChildren(emptyState('No clients yet', 'Add your first client by typing their details, or import a workbook or a Markdown file.', el('a', { class: 'btn primary', href: '#/clients/new', text: '+ Add client' }), el('a', { class: 'btn', href: '#/imports', text: 'Import…' }))); return; }
    if (!list.length) { holder.replaceChildren(emptyState('No client matches', 'Try another word, or change the filter.')); return; }
    holder.replaceChildren(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Client', 'Website', 'Primary location', 'Services', 'SEO status', 'Status', 'Last updated', ''].map((h) => el('th', { text: h })))),
      el('tbody', null, list.map((c) => {
        const url = safeUrl(c.website);
        return el('tr', { class: 'click', on: { click: (e) => { if (!e.target.closest('button, a')) location.hash = '#/clients/' + c.client_id; } } },
          el('td', null, el('b', { text: c.name }), c.ref ? el('div', { class: 'muted small', text: c.ref }) : null, el('div', { class: 'progress', style: 'width:90px;height:5px;margin-top:5px', title: `${c.completion}% of key details recorded` }, el('i', { style: `width:${c.completion}%` }))),
          el('td', null, c.website ? (url ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: c.website }) : c.website) : el('span', { class: 'faint', text: '—' })),
          el('td', { text: c.location || '—' }),
          el('td', null, c.services.length ? [c.services.slice(0, 3).map((s) => el('span', { class: 'tag', text: s })), c.services_count > 3 ? el('span', { class: 'muted small', text: `+${c.services_count - 3}` }) : null] : el('span', { class: 'faint', text: '—' })),
          el('td', null, c.seo_targets ? chip(`${plural(c.seo_targets, 'target')}`, 'ok') : chip('No targets yet', 'gray')),
          el('td', null, c.archived ? chip('Archived', 'gray') : (c.status ? chip(c.status, /active/i.test(c.status) ? 'ok' : 'info') : el('span', { class: 'faint', text: '—' })), c.pending ? el('div', null, chip(`${c.pending} to confirm`, 'warn')) : null),
          el('td', { class: 'nowrap', text: ago(c.updated_at), title: fmtTime(c.updated_at) }),
          el('td', { style: 'min-width:190px' }, el('a', { class: 'btn small', href: '#/clients/' + c.client_id, text: 'View' }), ' ', el('a', { class: 'btn small', href: '#/clients/' + c.client_id + '/business', text: 'Edit' }), ' ',
            el('button', { type: 'button', class: 'small', text: c.archived ? 'Restore' : 'Archive', on: { click: async () => {
              if (!c.archived && !(await confirmDialog({ title: `Archive ${c.name}?`, message: 'The client is hidden from task choices and cannot be changed until restored. Nothing is deleted.', confirmLabel: 'Archive', danger: true }))) return;
              try { await api.post(`/clients/${c.client_id}/${c.archived ? 'restore' : 'archive'}`); toast(c.archived ? 'Client restored.' : 'Client archived.', 'ok'); await ctx.reloadScopes(); ctx.rerender(); } catch (e) { toast(e.message, 'bad'); }
            } } })));
      })))));
  };
  filter.addEventListener('input', paint);
  status.addEventListener('change', paint);
  page.append(el('div', { class: 'row' }, filter, status, el('span', { class: 'muted small', text: plural(rows.length, 'client') })), holder);
  paint();
  ctx.mount(page);
}

// ================================================================== add client
export async function viewClientNew(ctx) {
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'crumbs' }, el('a', { href: '#/clients', text: 'Clients' }), ' › Add client'), el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Add a client' }),
    el('p', { text: 'Fill in what you know. Only a name is needed; anything left blank stays blank (nothing is guessed). You can add or change everything later.' }))));
  const inp = {};
  const mk = (name, label, o = {}) => { const i = o.area ? el('textarea', { rows: 3, maxLength: 2000 }) : el('input', { type: o.type || 'text', maxLength: 300, placeholder: o.ph || '', attrs: { autocomplete: 'off' } }); inp[name] = i; return field(label, i, o.hint); };
  const addr = {};
  const mkA = (name, label, ph) => { const i = el('input', { type: 'text', maxLength: 200, placeholder: ph || '', attrs: { autocomplete: 'off' } }); addr[name] = i; return field(label, i); };
  const areas = chipsInput([], 'Add a town or area, then press Enter');
  const services = chipsInput([], 'Add a service, then press Enter');
  const seoRows = el('div', { class: 'stack' });
  const seo = [];
  const addSeo = () => {
    const r = { keyword: el('input', { type: 'text', placeholder: 'keyword, e.g. emergency locksmith', maxLength: 300, attrs: { 'aria-label': 'Keyword' } }), location: el('input', { type: 'text', placeholder: 'location, e.g. Tampa, FL', maxLength: 300, attrs: { 'aria-label': 'Location' } }),
      priority: el('select', { attrs: { 'aria-label': 'Priority' } }, el('option', { value: '', text: 'Priority' }), ['High', 'Medium', 'Low'].map((x) => el('option', { value: x, text: x }))) };
    seo.push(r);
    const row = el('div', { class: 'row', style: 'align-items:stretch' }, el('div', { style: 'flex:2;min-width:180px' }, r.keyword), el('div', { style: 'flex:2;min-width:160px' }, r.location), el('div', { style: 'flex:1;min-width:110px' }, r.priority),
      el('button', { type: 'button', class: 'small', text: 'Remove', on: { click: () => { seo.splice(seo.indexOf(r), 1); row.remove(); } } }));
    seoRows.append(row);
  };
  addSeo();
  const err = el('div', { class: 'notice bad', hidden: true });
  const form = el('div', { class: 'stack' },
    el('div', { class: 'card' }, el('h2', { text: 'Business' }), el('div', { class: 'cols', style: 'margin-top:10px' }, mk('business_name', 'Business name'), mk('legal_name', 'Legal name'), mk('trade_name', 'Trade name'), mk('website', 'Website', { type: 'url', ph: 'https://example.com' }),
      mk('email', 'Email', { type: 'email' }), mk('phone', 'Phone', { type: 'tel', ph: '+1 555 010 1000' }), mk('contact_person', 'Contact person')), mk('description', 'Description', { area: true })),
    el('div', { class: 'card' }, el('h2', { text: 'Address' }), el('div', { class: 'cols', style: 'margin-top:10px' }, mkA('line1', 'Street address'), mkA('line2', 'Address line 2'), mkA('city', 'City'), mkA('state', 'State / province'), mkA('country', 'Country'), mkA('postal_code', 'ZIP / postal code'))),
    el('div', { class: 'card' }, el('h2', { text: 'Service areas' }), el('p', { class: 'muted small', text: 'Towns or areas the client serves.' }), areas.el),
    el('div', { class: 'card' }, el('h2', { text: 'Services' }), el('p', { class: 'muted small', text: 'What the client offers. Priorities and details can be added on the Services tab.' }), services.el),
    el('div', { class: 'card' }, el('div', { class: 'head' }, el('div', null, el('h2', { text: 'SEO targets' }), el('p', { class: 'muted small', text: 'Keywords the client wants to be found for, and where. Nothing is researched or ranked by saving these.' })),
      el('button', { type: 'button', class: 'small', text: '+ Add another', on: { click: addSeo } })), seoRows),
    el('div', { class: 'card' }, el('h2', { text: 'Account' }), el('div', { class: 'cols', style: 'margin-top:10px' }, mk('status', 'Status', { ph: 'e.g. Active' }), mk('plan', 'Plan'), mk('onboarding_date', 'Onboarding date', { type: 'date' })),
      mk('internal_notes', 'Internal notes (protected)', { area: true, hint: 'Stored encrypted and never shown to an AI model.' })),
    err);
  const submit = async (allow) => {
    err.hidden = true;
    const data = { allow_format: !!allow };
    for (const [k, i] of Object.entries(inp)) if (i.value.trim()) data[k] = i.value.trim();
    const a = Object.fromEntries(Object.entries(addr).filter(([, i]) => i.value.trim()).map(([k, i]) => [k, i.value.trim()]));
    if (Object.keys(a).length) data.address = a;
    data.service_areas = areas.values();
    data.services = services.values();
    data.seo_targets = seo.map((r) => ({ keyword: r.keyword.value.trim(), location: r.location.value.trim(), priority: r.priority.value })).filter((r) => r.keyword);
    let res;
    try { res = await api.post('/clients', data); } catch (e) { toast(e.message, 'bad'); return; }
    if (res.ok) { toast(`${res.name} was added.`, 'ok'); await ctx.reloadScopes(); ctx.refreshBadges(); location.hash = '#/clients/' + res.client_id; return; }
    if (res.problems) { err.replaceChildren(el('b', { text: 'Nothing was saved. Please fix: ' }), el('ul', null, res.problems.map((p) => el('li', { text: p.message })))); err.hidden = false; err.scrollIntoView({ block: 'center' }); return; }
    if (res.needs_confirmation) {
      const ok = await modal({ title: 'Please check', body: el('ul', null, res.needs_confirmation.map((n) => el('li', null, el('b', { text: n.label + ': ' }), n.message))), buttons: [{ label: 'Go back', value: false }, { label: 'Save anyway', value: true, kind: 'primary' }] });
      if (ok) await submit(true);
    }
  };
  page.append(form, el('div', { class: 'actions' }, el('button', { type: 'button', class: 'primary', text: 'Add client', on: { click: () => submit(false) } }), el('a', { class: 'btn', href: '#/clients', text: 'Cancel' })));
  ctx.mount(page);
}

// ================================================================== one client
const TABS = [['overview', 'Overview'], ['business', 'Business information'], ['contact', 'Contact'], ['locations', 'Locations'], ['services', 'Services'], ['seo', 'SEO targets'], ['documents', 'Documents'],
  ['sources', 'Sources'], ['research', 'Research'], ['notes', 'Notes'], ['activity', 'Activity']];

export async function viewClientDetail(ctx, cid, tab) {
  const o = await api.get('/clients/' + cid);
  const cl = o.client;
  const page = el('div', { class: 'stack' });
  const rerender = async () => { await ctx.reloadScopes(); ctx.rerender(); ctx.refreshBadges(); };
  page.append(el('div', { class: 'crumbs' }, el('a', { href: '#/clients', text: 'Clients' }), ' › ' + cl.name));
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: cl.name }), el('p', null, cl.ref ? chip('ID ' + cl.ref, 'gray') : null, ' ', cl.archived ? chip('Archived', 'gray') : chip('Active', 'ok'), ' ',
    o.is_default ? chip('Default client for tasks', 'ok') : null)),
    el('div', { class: 'actions' }, exportButton('clients', cid, 'Export'), el('button', { type: 'button', text: 'Rename', disabled: cl.archived, on: { click: async () => {
      const i = el('input', { type: 'text', value: cl.name, maxLength: 120 });
      const r = await modal({ title: 'Rename client', body: field('Name used to pick this client', i, 'Tasks refer to a client by this name, so it must be unique.'), buttons: [{ label: 'Cancel', value: false }, { label: 'Rename', value: true, kind: 'primary' }] });
      if (r !== true) return;
      try { await api.post(`/clients/${cid}/update`, { name: i.value }); toast('Renamed.', 'ok'); await rerender(); } catch (e) { toast(e.message, 'bad'); }
    } } }), el('button', { type: 'button', class: cl.archived ? 'primary' : 'danger', text: cl.archived ? 'Restore' : 'Archive', on: { click: async () => {
      if (!cl.archived && !(await confirmDialog({ title: `Archive ${cl.name}?`, message: 'The client is hidden from task choices and cannot be changed until restored. Nothing is deleted.', confirmLabel: 'Archive', danger: true }))) return;
      try { await api.post(`/clients/${cid}/${cl.archived ? 'restore' : 'archive'}`); toast(cl.archived ? 'Client restored.' : 'Client archived.', 'ok'); await rerender(); } catch (e) { toast(e.message, 'bad'); }
    } } }))));
  if (cl.archived) page.append(el('div', { class: 'notice warn', text: 'This client is archived: it is read-only and cannot be chosen for tasks. Restore it to make changes.' }));
  page.append(el('div', { class: 'tabs', role: 'tablist' }, TABS.map(([id, label]) => el('a', { href: `#/clients/${cid}/${id}`, ...(id === tab ? { attrs: { 'aria-current': 'page' } } : {}), text: label }))));
  const body = el('div', { class: 'stack' });
  page.append(body);
  const refresh = () => ctx.rerender();
  const locked = cl.archived;
  try {
    if (tab === 'overview') {
      const c = o.completion;
      const act = await api.get(`/clients/${cid}/activity?n=6`);
      body.append(el('div', { class: 'grid g4' },
        el('div', { class: 'card stat' }, el('b', { text: c.percent + '%' }), el('span', { text: 'Key details recorded' }), el('div', { class: 'progress', style: 'margin:8px 0 0' }, el('i', { style: `width:${c.percent}%` }))),
        ...[['Services', o.counts.services, 'services'], ['Locations', o.counts.locations, 'locations'], ['SEO targets', o.counts.seo_targets, 'seo'], ['Documents', o.counts.documents, 'documents'], ['Sources', o.counts.sources, 'sources']].map(([l, n, t]) =>
          el('div', { class: 'card stat' }, el('b', { text: String(n) }), el('span', { text: l }), el('a', { href: `#/clients/${cid}/${t}`, text: 'Open' })))));
      body.append(el('div', { class: 'grid g2' }, el('div', { class: 'card' }, el('h2', { text: 'Still to do' }), c.todo.length ? el('ul', { style: 'margin:8px 0 0;padding-left:1.1rem' }, c.todo.map((t) => el('li', null, t.label, ' ', chip(t.status === 'MISSING' ? 'not set' : t.status === 'UNVERIFIED' ? 'needs confirmation' : 'conflict', 'warn')))) : el('p', { class: 'muted', text: 'The key details are recorded and verified.' }),
        el('div', { class: 'actions', style: 'margin-top:12px' }, el('a', { class: 'btn', href: `#/clients/${cid}/business`, text: 'Edit business information' }))),
        el('div', { class: 'card' }, el('h2', { text: 'Recent activity' }), act.events.length ? el('ul', { style: 'list-style:none;margin:8px 0 0;padding:0' }, act.events.slice().reverse().map((e) => el('li', { class: 'row space', style: 'padding:6px 0;border-bottom:1px solid var(--line)' }, el('span', null, el('b', { text: e.text }), e.field ? ' · ' + e.field : ''), el('span', { class: 'faint small', text: ago(e.ts) })))) : el('p', { class: 'muted', text: 'Nothing yet.' }))));
    } else if (['business', 'contact', 'notes'].includes(tab)) {
      const s = await api.get(`/clients/${cid}/section/${tab}`);
      body.append(fieldSection({ scope: cid, title: s.title, fields: s.fields, onChange: refresh, locked, intro: tab === 'notes' ? 'Internal notes are protected: stored encrypted and never shown to an AI model.' : undefined }));
    } else if (tab === 'locations' || tab === 'services' || tab === 'seo') {
      const sec = await api.get(`/clients/${cid}/section/${tab}`);
      const kind = { locations: 'location', services: 'service', seo: 'seo' }[tab];
      const rows = (await api.get(`/clients/${cid}/records/${kind}`)).rows;
      body.append(recordsPanel({ client: cid, kind, rows, onChange: refresh, canEdit: !locked, description: { location: 'Places the client works in or wants to be found in. Each row can have a type and a priority.', service: 'One row per service, with an optional category, priority and notes.', seo: 'One row per keyword and the location it matters for. This is the structured input for future SEO research.' }[kind] }),
        fieldSection({ scope: cid, title: tab === 'seo' ? 'SEO settings and lists' : (tab === 'locations' ? 'Address and service-area lists' : 'Service lists'), fields: sec.fields, onChange: refresh, locked }));
    } else if (tab === 'documents') {
      const d = await api.get(`/clients/${cid}/sources`);
      const docs = d.documents.documents;
      body.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Documents for this client' }), el('a', { class: 'btn small', href: '#/documents', text: 'Manage documents' })),
        docs.length ? el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Document', 'Type', 'Status'].map((h) => el('th', { text: h })))), el('tbody', null, docs.map((x) => el('tr', null, el('td', { text: x.name }), el('td', { text: x.doc_type }), el('td', null, stateChip(x.state))))))) :
          emptyState('No documents for this client', 'Documents come only from folders you authorize and enroll for this client. The Documents page shows the steps.', el('a', { class: 'btn', href: '#/documents', text: 'Open Documents' }))));
    } else if (tab === 'sources') {
      const d = await api.get(`/clients/${cid}/sources`);
      body.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Where this client’s information came from' })), sourcesTable(d.sources, { showScope: false }),
        el('p', { class: 'muted small', style: 'padding:0 18px 14px', text: 'Each value also shows its own source underneath it on the other tabs.' })));
    } else if (tab === 'research') {
      const s = (await api.get('/seo?client=' + cid)).clients[0];
      body.append(researchCard(s));
    } else if (tab === 'activity') {
      const a = await api.get(`/clients/${cid}/activity?n=120`);
      body.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Activity for this client' })), activityTable(a.events, { showScope: false })));
    }
  } catch (e) { body.append(el('div', { class: 'notice bad', text: e.message })); }
  ctx.mount(page);
}

export function researchCard(s) {
  const card = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Research sessions and observations' })));
  card.append(el('p', { class: 'muted small', style: 'padding:0 18px', text: 'Research (search observations, audits, competitor checks) records dated sessions and observations here, never permanent facts. A position is what one browser was shown at that time.' }));
  if (!s || (!s.sessions.length && !s.observations.length)) { card.append(emptyState('No research yet', 'Add SEO targets on the SEO targets tab: they are what the research will use.')); return card; }
  card.append(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Observed', 'What'].map((h) => el('th', { text: h })))), el('tbody', null, s.observations.map((o) => el('tr', null, el('td', { class: 'nowrap', text: fmtTime(o.observed_at) }), el('td', { text: o.text })))))));
  return card;
}
