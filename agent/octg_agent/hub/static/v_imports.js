// Import Center: read a spreadsheet or a Markdown knowledge file, REVIEW every value (accept, leave out, resolve conflicts), then confirm. Nothing is saved until the confirmation at the end.
// The file is only data: the agent compares it with what is stored and never treats it as an instruction.
import { api } from './api.js';
import { importsTable } from './v_dashboard.js';
import { ago, chip, confirmDialog, downloadBlob, el, emptyState, field, fmtTime, modal, notice, plural, stateChip, toast } from './ui.js';

const ACTION = { NEW: ['New', 'ok'], UPDATE: ['Update', 'info'], UNCHANGED: ['Already stored', 'gray'], CONFLICT: ['Conflict', 'bad'], SKIPPED: ['Not re-added', 'gray'], PENDING: ['Waiting', 'warn'] };
const KIND = { spreadsheet: 'Excel workbook', markdown: 'Markdown file' };

// ================================================================== landing
export async function viewImports(ctx, id) {
  if (id && id.startsWith('stg_')) return viewReview(ctx, id);
  if (id) return viewHistory(ctx, id);
  const [list, scopes] = [await api.get('/imports'), ctx.scopes()];
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'Imports' }),
    el('p', { text: 'Bring information in from a spreadsheet or a Markdown knowledge file. You always review it first: nothing is saved until you confirm, and nothing is trusted just because it is in a file.' }))));

  // ---- spreadsheet
  const xl = el('input', { type: 'file', accept: '.xlsx', attrs: { 'aria-label': 'Choose an Excel workbook' } });
  const xlErr = el('div', { class: 'err' });
  const xlBtn = el('button', { type: 'button', class: 'primary', text: 'Read and preview', on: { click: async () => {
    xlErr.textContent = '';
    const f = xl.files && xl.files[0];
    if (!f) { xlErr.textContent = 'Choose an .xlsx workbook first.'; return; }
    xlBtn.disabled = true; xlBtn.textContent = 'Reading…';
    try { const v = await api.upload('/imports/spreadsheet', await f.arrayBuffer(), { 'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(f.name) }); location.hash = '#/imports/' + v.id; }
    catch (e) { xlErr.textContent = e.message; toast(e.message, 'bad'); } finally { xlBtn.disabled = false; xlBtn.textContent = 'Read and preview'; }
  } } });

  // ---- markdown
  const scopeSel = el('select', { attrs: { 'aria-label': 'Whose knowledge is this?' } }, el('option', { value: '', text: 'Choose whose knowledge this is…' }), el('option', { value: 'me', text: `My business (${scopes.owner.name})` }),
    el('option', { value: 'new', text: '＋ A NEW client (create it from this file)' }),
    scopes.clients.filter((c) => !c.archived).map((c) => el('option', { value: c.id, text: 'Client: ' + c.name })));
  const newName = el('input', { type: 'text', maxlength: '120', placeholder: 'Only if the file does not give a "Business name:" line', attrs: { 'aria-label': 'Client name (only if the file does not say it)' } });
  const newName_f = field('Client name (optional)', newName, 'The client is told apart by name. If the file has a “Business name: …” line, that is used and this box is ignored.');
  newName_f.hidden = true;
  scopeSel.addEventListener('change', () => { newName_f.hidden = scopeSel.value !== 'new'; });
  const tpl = (kind, label) => el('button', { type: 'button', class: 'small', text: label, on: { click: async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try { const { blob, name } = await api.download('/imports/markdown-template/' + kind); downloadBlob(name, blob); toast(`Saved ${name}: fill it in, then import it here.`, 'ok'); }
    catch (e) { toast(e.message, 'bad'); } finally { b.disabled = false; }
  } } });
  const md = el('input', { type: 'file', accept: '.md,.markdown,text/markdown', attrs: { 'aria-label': 'Choose a Markdown file' } });
  const mdErr = el('div', { class: 'err' });
  const authSel = el('select', { attrs: { 'aria-label': 'Or choose a file from an authorized folder' } }, el('option', { value: '', text: '…or pick a file from an authorized folder' }));
  api.get('/markdown-files').then((r) => { for (const f of r.files) authSel.append(el('option', { value: f.path, text: f.name + '  —  ' + f.path })); if (!r.files.length) authSel.append(el('option', { value: '', text: r.authorized ? '(no .md files in your authorized folders)' : '(no folder is authorized yet: python -m octg_agent fs allow "<folder>")', disabled: true })); }).catch(() => {});
  const mdBtn = el('button', { type: 'button', class: 'primary', text: 'Read and analyse', on: { click: async () => {
    mdErr.textContent = '';
    if (!scopeSel.value) { mdErr.textContent = 'Choose whose knowledge this is first: your business, one client, or a new client. It is never guessed from the text.'; scopeSel.focus(); return; }
    const f = md.files && md.files[0];
    if (!f && !authSel.value) { mdErr.textContent = 'Choose a Markdown (.md) file.'; return; }
    mdBtn.disabled = true; mdBtn.textContent = 'Reading…';
    try {
      const v = await api.post('/imports/markdown', f ? { scope: scopeSel.value, client_name: newName.value.trim(), name: f.name, text: await f.text() } : { scope: scopeSel.value, client_name: newName.value.trim(), path: authSel.value });
      location.hash = '#/imports/' + v.id;
    } catch (e) { mdErr.textContent = e.message; toast(e.message, 'bad'); } finally { mdBtn.disabled = false; mdBtn.textContent = 'Read and analyse'; }
  } } });

  page.append(el('div', { class: 'grid g2' },
    el('div', { class: 'card stack' }, el('h2', { text: 'Import a spreadsheet' }), el('p', { class: 'muted', text: 'The OCTG workbook (My Business, Clients, Client Services, Client Locations, Client SEO). Columns are matched by name; anything unclear is asked, not guessed.' }),
      field('Excel workbook (.xlsx)', xl), xlErr, el('div', { class: 'actions' }, xlBtn, el('a', { class: 'small', href: '#/imports', text: '' }))),
    el('div', { class: 'card stack' }, el('h2', { text: 'Import a Markdown knowledge file' }), el('p', { class: 'muted', text: 'Labelled lines (Website: …), tables and lists under headings like Services become candidates. Free prose is left alone. You decide what is kept.' }),
      field('This file is about', scopeSel, 'Required. Everything in the file belongs to exactly this scope. “A NEW client” creates the client from the file after you review it.'), newName_f, field('Markdown file (.md)', md), authSel,
        el('div', { class: 'actions' }, el('span', { class: 'muted small', text: 'No file yet? Download a blank one:' }), tpl('client', 'Client template (.md)'), tpl('me', 'My business template (.md)')), mdErr, el('div', { class: 'actions' }, mdBtn))));

  if (list.staged.length) {
    page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Waiting for your review' })), el('div', { class: 'tablewrap' }, el('table', null,
      el('thead', null, el('tr', null, ['Source', 'Type', 'Scope', 'Rows', 'Status', ''].map((h) => el('th', { text: h })))),
      el('tbody', null, list.staged.map((s) => { const d = s.records; return el('tr', null, el('td', { text: s.name }), el('td', { text: KIND[s.kind] || s.kind }), el('td', { text: s.scope_name }),
        el('td', { text: String(d.clients + d.services + d.locations + d.seo_targets + (d.business_profile ? 1 : 0)) }), el('td', null, stateChip(s.status)),
        el('td', { class: 'nowrap' }, el('a', { class: 'btn small primary', href: '#/imports/' + s.id, text: 'Review' }))); })))),
      el('p', { class: 'muted small', style: 'padding:0 18px 12px', text: 'A preview is kept only in memory for two hours (and until the agent restarts).' })));
  }
  page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Import history' })), importsTable(list.imports)));
  page.append(el('details', { class: 'card' }, el('summary', { text: 'Prefer the terminal? The command-line importer still works' }),
    el('p', { class: 'muted small' }, 'python -m octg_agent business import "<file>.xlsx" --dry-run  ·  python -m octg_agent business template')));
  ctx.mount(page);
}

// ================================================================== review of one staged import
async function viewReview(ctx, sid) {
  let v = await api.get('/staged/' + sid);
  const page = el('div');
  let limit = 150, filterAction = 'all', filterText = '';
  const post = async (act, payload) => { try { const r = await api.post(`/staged/${sid}/${act}`, payload || {}); if (r.rejected) return r; v = r; paint(); return r; } catch (e) { toast(e.message, 'bad'); if (e.code === 'NO_SUCH_PREVIEW') location.hash = '#/imports'; return null; } };

  function paint() {
    const p = v.preview;
    const md = v.kind === 'markdown';
    const node = el('div', { class: 'stack' });
    node.append(el('div', { class: 'crumbs' }, el('a', { href: '#/imports', text: 'Imports' }), ' › Review'));
    node.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: md ? 'Review a Markdown file' : 'Review a workbook' }),
      el('dl', { class: 'kv', style: 'margin-top:8px' }, el('dt', { text: 'SOURCE' }), el('dd', null, el('b', { text: v.name }), ' ', chip(KIND[v.kind] || v.kind, 'info'), ' ', el('span', { class: 'mono muted', text: p.sha256 })),
        el('dt', { text: 'SCOPE' }), el('dd', null, md ? el('b', { text: v.scope_name }) : el('span', null, el('b', { text: 'Several' }), ' — each row names its own client; “My Business” is your own business'), md && v.scope !== 'me' ? chip('Client', 'warn') : null),
        el('dt', { text: 'STATUS' }), el('dd', null, stateChip(v.status), md ? el('span', { class: 'muted small', text: '  Nothing becomes verified merely because it appeared in the file; you accept what is kept.' }) : null))),
      el('div', { class: 'actions' }, el('button', { type: 'button', class: 'danger', text: 'Reject import', on: { click: async () => {
        if (!(await confirmDialog({ title: 'Reject this import?', message: 'Nothing will be saved. A record of the rejection (without any values) is kept in the import history.', confirmLabel: 'Reject', danger: true }))) return;
        const r = await post('reject'); if (r && r.rejected) { toast('Import rejected. Nothing was saved.', 'ok'); ctx.refreshBadges(); location.hash = '#/imports'; }
      } } }))));

    const f = p.facts, rc = p.records;
    node.append(el('div', { class: 'card row' }, chip(`${f.new} new`, 'ok'), chip(`${f.updated} updated`, 'info'), chip(`${f.unchanged} already stored`, 'gray'), chip(`${f.conflict} in conflict`, f.conflict ? 'bad' : 'gray'),
      p.excluded ? chip(`${p.excluded} left out`, 'warn') : null, el('span', { class: 'muted small', text: `${plural(rc.clients.new + rc.clients.updated + rc.clients.unchanged + rc.clients.conflict + rc.clients.pending, 'client')} · ${plural(p.detected.services, 'service')} · ${plural(p.detected.locations, 'location')} · ${plural(p.detected.seo_targets, 'SEO target')}` }),
      p.sensitive.count ? chip(`${p.sensitive.count} protected value(s)`, 'high', 'Shown masked, stored encrypted, never shown to an AI model') : null));

    // ---- decisions
    const todo = el('div', { class: 'stack' });
    for (const m of p.mapping_needed) {
      const sel = el('select', null, el('option', { value: '', text: 'Choose what this column is…' }), m.candidates.map((c) => el('option', { value: c, text: c })));
      todo.append(el('div', { class: 'card' }, el('h3', { text: `Column “${m.header}” in ${m.sheet}` }), el('p', { class: 'muted', text: m.note }), el('div', { class: 'row' }, sel,
        el('button', { type: 'button', class: 'primary small', text: 'Use this', on: { click: () => { if (!sel.value) { toast('Choose a field first.', 'warn'); return; } post('map', { sheet: m.sheet, header: m.header, column: sel.value }); } } }),
        el('button', { type: 'button', class: 'small', text: 'Skip this column', on: { click: () => post('skip', { sheet: m.sheet, header: m.header }) } }))));
    }
    for (const d of p.duplicates) {
      const decided = d.decision;
      todo.append(el('div', { class: 'card' }, el('div', { class: 'row space' }, el('h3', { text: 'Possible duplicate client' }), decided ? chip('Decided: ' + decided, 'ok') : chip('Needs your decision', 'warn')),
        el('p', null, el('b', { text: d.spreadsheet_client }), ' in the file may be the same as ', el('b', { text: d.possible_duplicate_of }), ' that you already have. ', el('span', { class: 'muted', text: 'Because: ' + d.because.join(', ') + '.' })),
        d.problem ? notice(d.problem, 'warn') : null,
        el('div', { class: 'row', style: 'margin-top:8px' }, d.choices.includes('same') ? el('button', { type: 'button', class: 'small primary', text: 'Same client (link them)', on: { click: () => post('decide', { id: d.id, choice: 'same' }) } }) : null,
          d.choices.includes('separate') ? el('button', { type: 'button', class: 'small', text: 'Different client', on: { click: () => post('decide', { id: d.id, choice: 'separate' }) } }) : null)));
    }
    const undecided = p.conflicts.filter((c) => !c.decision);
    if (p.conflicts.length) {
      const card = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('div', null, el('h2', { text: `Conflicts (${p.conflicts.length})` }), el('p', { class: 'muted small', text: 'The file disagrees with something you already have. Nothing is overwritten unless you choose to.' })),
        undecided.length ? el('button', { type: 'button', class: 'small', text: 'Keep the existing value for all', on: { click: () => post('keep-all') } }) : null));
      card.append(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Client', 'Field', 'Existing value', 'Value in the file', 'Your choice'].map((h) => el('th', { text: h })))),
        el('tbody', null, p.conflicts.map((c) => el('tr', null, el('td', { text: c.client }), el('td', null, c.field, c.sensitivity === 'HIGH' ? el('div', null, chip('Protected: masked', 'high')) : null),
          el('td', null, c.differences ? c.differences.map((d) => el('div', { class: 'small', text: `${d.attribute}: ${d.existing}` })) : el('span', { class: 'mono', text: String(c.existing) })),
          el('td', null, c.differences ? c.differences.map((d) => el('div', { class: 'small', text: `${d.attribute}: ${d.spreadsheet}` })) : el('span', { class: 'mono', text: String(c.spreadsheet) })),
          el('td', { class: 'nowrap' }, el('button', { type: 'button', class: 'small' + (c.decision === 'keep' ? ' primary' : ''), text: 'Keep existing', on: { click: () => post('decide', { id: c.id, choice: 'keep' }) } }), ' ',
            el('button', { type: 'button', class: 'small' + (c.decision === 'sheet' ? ' primary' : ''), text: 'Accept new', on: { click: () => post('decide', { id: c.id, choice: 'sheet' }) } }),
            c.decision ? el('div', { class: 'faint small', text: c.decision === 'keep' ? 'Keeping the existing value' : 'Replacing with the new value' }) : null)))))));
      todo.append(card);
    }
    if (todo.children.length) node.append(el('div', { class: 'stack' }, el('h2', { text: 'Needs your decision' }), todo));

    // ---- problems
    const errs = p.issues.filter((i) => i.level === 'error'), infos = p.issues.filter((i) => i.level !== 'error');
    if (errs.length) node.append(el('div', { class: 'card' }, el('h2', { text: `${plural(errs.length, 'problem')} (left out unless fixed)` }), el('ul', { style: 'margin:8px 0 0;padding-left:1.1rem' }, errs.slice(0, 40).map((i) => el('li', null, el('b', { text: `${i.sheet}${i.row ? ' line/row ' + i.row : ''}${i.column ? ' · ' + i.column : ''}: ` }), i.message))), errs.length > 40 ? el('p', { class: 'muted small', text: `…and ${errs.length - 40} more` }) : null));
    if (infos.length || p.warnings.length) node.append(el('details', { class: 'card' }, el('summary', { text: `Notes (${infos.length + p.warnings.length})` }), el('ul', { style: 'margin:8px 0 0;padding-left:1.1rem' },
      p.warnings.map((w) => el('li', { text: w })), infos.map((i) => el('li', { class: 'muted', text: `${i.sheet}${i.row ? ' ' + i.row : ''}${i.column ? ' · ' + i.column : ''}: ${i.message}` })))));
    if (md && v.info) {
      const i = v.info;
      node.append(el('div', { class: 'card' }, el('h2', { text: 'What was in the file' }), el('p', { class: 'muted', text: `${i.lines} lines · ${plural((i.headings || []).length, 'heading')} · ${plural(i.tables || 0, 'table')} · ${plural(i.list_items || 0, 'list item')} · ${plural(i.pairs || 0, 'labelled line')} · ${plural(i.paragraphs || 0, 'paragraph')} of free text (left as they are)${i.code_blocks ? ' · ' + plural(i.code_blocks, 'code block') + ' skipped' : ''}` }),
        (i.headings || []).length ? el('p', { class: 'small' }, (i.headings || []).slice(0, 14).map((h) => el('span', { class: 'tag', text: h }))) : null));
    }

    // ---- items
    const items = v.items.filter((it) => (filterAction === 'all' || (filterAction === 'left' ? !it.included : (filterAction === 'attention' ? it.action === 'CONFLICT' : it.action === filterAction))) &&
      (!filterText || [it.field, it.scope, it.new, it.existing, it.section].join(' ').toLowerCase().includes(filterText)));
    const fsel = el('select', { attrs: { 'aria-label': 'Filter' }, style: 'max-width:200px', on: { change: (e) => { filterAction = e.target.value; limit = 150; paint(); } } },
      [['all', 'Everything'], ['NEW', 'New'], ['UPDATE', 'Updates'], ['CONFLICT', 'Conflicts'], ['UNCHANGED', 'Already stored'], ['left', 'Left out']].map(([k, t]) => el('option', { value: k, text: t, selected: filterAction === k })));
    const ftxt = el('input', { type: 'search', placeholder: 'Filter…', value: filterText, style: 'max-width:240px', attrs: { 'aria-label': 'Filter values' }, on: { input: (e) => { filterText = e.target.value.toLowerCase(); limit = 150; const pos = e.target.selectionStart; paint(); const n = document.querySelector('#view input[type=search]'); if (n) { n.focus(); n.setSelectionRange(pos, pos); } } } });
    const tbl = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('div', null, el('h2', { text: md ? 'Candidate information' : 'Everything in the file' }),
      el('p', { class: 'muted small', text: md ? 'Untick anything you do not want. Candidates are not facts until you confirm the import.' : 'Untick anything you do not want imported.' })), el('div', { class: 'row' }, fsel, ftxt)));
    if (!items.length) tbl.append(emptyState('Nothing to show', 'Change the filter, or the file had nothing recognisable.'));
    else tbl.append(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Keep', 'Scope', md ? 'Field' : 'Field', 'Value', 'Already stored', 'What happens', 'Found in'].map((h) => el('th', { text: h })))),
      el('tbody', null, items.slice(0, limit).map((it) => {
        const [lab, kind] = it.included ? (ACTION[it.action] || [it.action, 'gray']) : ['Left out', 'gray'];
        return el('tr', { style: it.included ? '' : 'opacity:.55' }, el('td', null, el('input', { type: 'checkbox', checked: it.included, attrs: { 'aria-label': `Keep ${it.field}` }, on: { change: (e) => post(e.target.checked ? 'include' : 'exclude', { uid: it.uid }) } })),
          el('td', { text: it.scope }), el('td', null, it.field, it.sensitivity === 'HIGH' ? el('div', null, chip('Protected', 'high')) : null), el('td', { class: it.sensitivity === 'HIGH' ? 'mono' : '', text: String(it.new ?? '') }),
          el('td', { class: it.sensitivity === 'HIGH' ? 'mono muted' : 'muted', text: it.existing == null ? '—' : String(it.existing) }),
          el('td', null, chip(lab, kind), it.decision ? el('div', { class: 'faint small', text: it.decision === 'keep' ? 'keeping existing' : 'replacing' }) : null, it.note ? el('div', { class: 'faint small', text: it.note }) : null),
          el('td', { class: 'muted small', text: `${it.section}${it.row ? ' · ' + (md ? 'line ' : 'row ') + it.row : ''}` }));
      })))));
    if (items.length > limit) tbl.append(el('div', { class: 'row', style: 'padding:10px 18px' }, el('button', { type: 'button', text: `Show more (${items.length - limit} hidden)`, on: { click: () => { limit += 300; paint(); } } })));
    node.append(tbl);

    // ---- action bar
    const blocking = v.blocking_if_problems_left_out.map((m) => m.replace(/(decision\(s\) are needed): [A-Za-z0-9, ]+$/, '$1 (see the choices above)'));
    const bar = el('div', { class: 'savebar' }, el('div', null, blocking.length ? el('div', null, el('b', { text: 'Not ready yet: ' }), blocking.join(' · ')) : el('div', null, el('b', { text: 'Ready to confirm. ' }), el('span', { class: 'muted', text: 'Nothing has been saved yet.' }))),
      el('div', { class: 'row' }, el('a', { class: 'btn', href: '#/imports', text: 'Back' }), el('button', { type: 'button', class: 'primary', text: 'Review and confirm import…', disabled: blocking.length > 0, on: { click: () => confirmImport(p, errs) } })));
    node.append(bar);
    page.replaceChildren(node);
  }

  async function confirmImport(p, errs) {
    const md = v.kind === 'markdown';
    const verify = el('input', { type: 'checkbox', checked: md });
    const high = el('input', { type: 'checkbox' });
    const leave = el('input', { type: 'checkbox' });
    const body = el('div', null,
      el('p', null, el('b', { text: `${p.facts.new + p.facts.updated + p.conflicts.filter((c) => c.decision === 'sheet').length} value(s)` }), ` and ${p.records.services.new + p.records.locations.new + p.records.seo_targets.new} new row(s) will be saved`, v.kind === 'spreadsheet' ? `, ${p.records.clients.new} new client(s)` : '', '. A backup of the database is made first, and the whole import is all-or-nothing.'),
      el('label', { class: 'check' }, verify, el('span', null, el('b', { text: 'Mark what I keep as verified. ' }), md ? 'You reviewed each candidate above; accepting them confirms them.' : 'Leave this unticked to confirm them later, value by value.')),
      p.high_writes ? el('label', { class: 'check' }, high, el('span', null, `I understand ${plural(p.high_writes, 'protected value')} will be stored encrypted and never shown to an AI model.`)) : null,
      errs.length ? el('label', { class: 'check' }, leave, el('span', null, `Leave out the ${plural(errs.length, 'row or cell')} with problems.`)) : null);
    const ok = await modal({ title: 'Confirm import', body, buttons: [{ label: 'Cancel', value: false }, { label: 'Import now', value: true, kind: 'primary', validate: () => {
      if (p.high_writes && !high.checked) { toast('Please tick the box about protected values.', 'warn'); return false; }
      if (errs.length && !leave.checked) { toast('Tick the box to leave out the rows with problems, or go back and fix the file.', 'warn'); return false; }
      return true; } }] });
    if (ok !== true) return;
    try {
      const r = await api.post(`/staged/${sid}/commit`, { assume_verified: verify.checked, confirm_sensitive: high.checked, accept_errors: leave.checked });
      toast(`Imported. ${r.counts.facts_new + r.counts.facts_updated + r.counts.facts_chosen} value(s) and ${r.counts.records_new + r.counts.records_updated + r.counts.records_chosen} row(s) saved.`, 'ok');
      await ctx.reloadScopes(); ctx.refreshBadges();
      location.hash = '#/imports/' + r.import_id;
    } catch (e) { toast(e.message, 'bad'); if (e.data && e.data.blocking) { v.blocking_if_problems_left_out = e.data.blocking; paint(); } }
  }
  paint();
  ctx.mount(page);
}

// ================================================================== one finished import
async function viewHistory(ctx, id) {
  const d = await api.get('/imports/' + id);
  const c = d.counts || {};
  const page = el('div', { class: 'stack' });
  const status = d.status === 'committed' ? ((c.items_rejected || 0) + (c.facts_kept || 0) + (c.records_kept || 0) ? 'Partially accepted' : 'Imported') : d.status === 'rejected' ? 'Rejected' : d.status === 'failed' ? 'Failed' : d.status;
  page.append(el('div', { class: 'crumbs' }, el('a', { href: '#/imports', text: 'Imports' }), ' › ' + d.session_id));
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: d.workbook }), el('dl', { class: 'kv', style: 'margin-top:8px' },
    el('dt', { text: 'Import ID' }), el('dd', { class: 'mono', text: d.session_id }), el('dt', { text: 'Type' }), el('dd', { text: KIND[d.kind] || d.kind }),
    el('dt', { text: 'Scope' }), el('dd', { text: d.kind === 'markdown' ? (d.scope_name || 'My business') : 'Several (named in the file)' }), el('dt', { text: 'Fingerprint' }), el('dd', { class: 'mono', text: d.sha256 }),
    el('dt', { text: 'Date' }), el('dd', { text: fmtTime(d.created_at) }), el('dt', { text: 'Status' }), el('dd', null, stateChip(status)),
    el('dt', { text: 'Verification' }), el('dd', null, chip(d.verification === 'VERIFIED' ? 'Verified' : 'Not confirmed yet', d.verification === 'VERIFIED' ? 'ok' : 'warn'), d.last_verified ? el('span', { class: 'muted small', text: ' ' + fmtTime(d.last_verified) }) : null))),
    el('div', { class: 'actions' }, d.status === 'committed' && d.verification !== 'VERIFIED' ? el('button', { type: 'button', class: 'primary', text: 'Mark everything from this import verified', on: { click: async () => {
      if (!(await confirmDialog({ title: 'Mark this import verified?', message: 'You are saying every value and row it added is correct, so the agent may use them.', confirmLabel: 'Mark verified' }))) return;
      try { const r = await api.post(`/imports/${id}/verify`); toast(`Verified ${r.facts} value(s) and ${r.records} row(s).`, 'ok'); ctx.rerender(); ctx.refreshBadges(); } catch (e) { toast(e.message, 'bad'); }
    } } }) : null)));
  if (d.note) page.append(notice(d.note, d.status === 'failed' ? 'bad' : ''));
  const groups = { added: 'Accepted — added', updated: 'Accepted — updated', replaced: 'Accepted — replaced an existing value', 'kept existing': 'Kept the existing value', rejected: 'Rejected (left out)', skipped: 'Not re-added', unchanged: 'Already stored' };
  const by = {};
  for (const it of d.items) (by[it.decision] ||= []).push(it);
  page.append(el('div', { class: 'card row' }, Object.keys(groups).filter((k) => by[k]).map((k) => chip(`${groups[k].replace('Accepted — ', '')}: ${by[k].length}`, k === 'rejected' ? 'warn' : /added|updated|replaced/.test(k) ? 'ok' : 'gray'))));
  if (!d.items.length) page.append(el('div', { class: 'card' }, emptyState(d.status === 'committed' ? 'No itemised record' : 'Nothing was saved', d.status === 'committed' ? 'This import was made with an earlier version (terminal) that did not record each value.' : 'No values were written.')));
  for (const k of Object.keys(groups)) {
    if (!by[k]) continue;
    page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: `${groups[k]} (${by[k].length})` })), el('div', { class: 'tablewrap' }, el('table', null,
      el('thead', null, el('tr', null, ['Scope', 'What', 'Value', 'Found in', 'Note'].map((h) => el('th', { text: h })))),
      el('tbody', null, by[k].slice(0, 300).map((it) => el('tr', null, el('td', { text: it.scope_name }), el('td', null, it.label, it.sensitivity === 'HIGH' ? el('div', null, chip('Protected', 'high')) : null), el('td', { class: it.sensitivity === 'HIGH' ? 'mono' : '', text: it.mask }),
        el('td', { class: 'muted small', text: `${it.source_sheet}${it.source_row ? ' · ' + it.source_row : ''}` }), el('td', { class: 'muted small', text: it.detail || '' }))))))));
  }
  ctx.mount(page);
}
