// OCTG Business Brain panel (Phase 3): a view over what the local agent reports. It only appears when the agent says the feature flag is ON.
//
// Rules this file keeps:
//  * Values are MASKED. The agent sends masks; a value exists in the page only after an explicit Reveal click, for a few seconds, in one text node.
//  * It holds no data: nothing is written to storage, the service worker keeps nothing (results are relayed once, in memory), and a hidden/collapsed
//    panel or a tab switch wipes any revealed value.
//  * It can only confirm, reject, resolve, approve or exclude things the agent already found. Entering values, importing a workbook, enrolling folders and every
//    other way of WIDENING access stays in the local CLI (the Sources and Imports tabs say so). The Imports tab PREVIEWS a workbook (nothing is saved).
//    A previewed workbook is read in memory, sent once, and its preview is shown masked and dropped when you leave the tab.
//  * All text is inserted with textContent (never innerHTML), so nothing a document or a page contains can become markup or script.
//
// The DOM and the transport are injected so the logic is unit-tested without a browser.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; return; }
  root.OCTG_BIZ_UI = api;
  const d = root.document;
  if (!d || !d.getElementById('biz')) return;
  root.octgBiz = api.createBizPanel({
    doc: d, root: d.getElementById('biz'), body: d.getElementById('bizBody'), msg: d.getElementById('bizMsg'), tabs: d.getElementById('bizTabs'),
    clientSelect: d.getElementById('bizClient'), pickBox: d.getElementById('clientPick'), pickSelect: d.getElementById('clientPickSel'),
    setTimeout: root.setTimeout.bind(root), clearTimeout: root.clearTimeout.bind(root), send: (cmd, extra) => { if (root.octgSend) root.octgSend(cmd, extra); },
  });
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TABS = [['profile', 'Profile'], ['clients', 'Clients'], ['documents', 'Documents'], ['sources', 'Sources'], ['imports', 'Imports'], ['security', 'Security'], ['activity', 'Activity']];
  const COMMAND = { profile: 'biz_profile', clients: 'biz_clients', documents: 'biz_docs', sources: 'biz_sources', imports: 'biz_imports', security: 'biz_status', activity: 'biz_audit' };
  const MAX_PREVIEW_BYTES = 3 * 1024 * 1024;
  // sub-views of one client (Clients tab): each is scoped to that client by the agent; the panel only ever names the client id it was shown
  const CLIENT_TABS = [['profile', 'Profile', 'biz_client'], ['docs', 'Documents', 'biz_client_docs'], ['sources', 'Sources', 'biz_client_sources'], ['seo', 'SEO', 'biz_client_seo'], ['activity', 'Activity', 'biz_client_audit']];
  const GROUPS = { identity: 'Identity', contact: 'Contact', addresses: 'Addresses', registration: 'Registration', taxation: 'Tax identifiers', owners: 'Owners', services: 'Services', social: 'Social', other: 'Other' };
  const STATUS_HELP = { VERIFIED: 'You entered or confirmed this value.', UNVERIFIED: 'Found in a document or an import. Not used until you confirm it.',
                        MISSING: 'No value. The agent will ask you instead of guessing.', CONFLICTING: 'Two different values. The agent stops and asks until you choose one.' };

  function createBizPanel(o) {
    const doc = o.doc;
    const st = { enabled: false, tab: 'profile', data: {}, showMissing: false, reveal: null, n: 0, connected: false, client: null, clientTab: 'profile', clients: [], preview: null, previewing: false };
    const pending = new Map();
    let revealTimer = null;

    const el = (tag, cls, text) => { const e = doc.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = String(text); return e; };
    const btn = (label, onClick, cls) => { const b = el('button', cls || 'link', label); b.type = 'button'; b.addEventListener('click', onClick); return b; };

    function say(text, bad) { if (o.msg) { o.msg.textContent = text || ''; o.msg.hidden = !text; o.msg.className = 'muted' + (bad ? ' bad' : ''); } }

    function request(command, extra, then) {
      const id = 'biz_' + (++st.n);
      pending.set(id, then || null);
      o.send(command, { ...(extra || {}), request_id: id });
      return id;
    }

    function wipeReveal() {
      if (revealTimer) { o.clearTimeout(revealTimer); revealTimer = null; }
      if (st.reveal) { st.reveal.node.textContent = '••••••'; st.reveal = null; }
    }

    function chip(text, cls, title) { const c = el('span', 'chip biz-' + cls, text); if (title) c.title = title; return c; }

    // ---------------------------------------------------------------- views
    function viewProfile(data, clientId) {
      const wrap = el('div');
      const fields = (data.fields || []);
      const shown = fields.filter((f) => st.showMissing || f.status !== 'MISSING');
      const missing = fields.length - fields.filter((f) => f.status !== 'MISSING').length;
      const toggle = el('label', 'voice-opt');
      const cb = doc.createElement('input'); cb.type = 'checkbox'; cb.checked = st.showMissing;
      cb.addEventListener('change', () => { st.showMissing = !!cb.checked; render(); });
      toggle.append(cb, el('span', '', ` Show empty fields (${missing})`));
      wrap.append(toggle);
      if (!shown.length) wrap.append(el('p', 'muted', 'No business facts yet. Add them from the terminal: python -m octg_agent business set <field>'));
      const groups = {};
      for (const f of shown) (groups[f.group] ||= []).push(f);
      for (const g of Object.keys(GROUPS).filter((k) => groups[k]).concat(Object.keys(groups).filter((k) => !GROUPS[k]))) {
        wrap.append(el('h3', 'biz-group', GROUPS[g] || g));
        for (const f of groups[g]) wrap.append(fieldRow(f, clientId));
      }
      return wrap;
    }

    function fieldRow(f, clientId) {
      const row = el('div', 'biz-field');
      const head = el('div', 'biz-head');
      head.append(el('span', 'biz-label', f.label), chip(f.status, f.status, STATUS_HELP[f.status]), chip(f.sensitivity, 'sens-' + f.sensitivity, 'Sensitivity'));
      row.append(head);
      const live = (f.facts || []).filter((x) => x.state === 'LIVE');
      for (const x of live) {
        const line = el('div', 'biz-fact');
        const val = el('span', 'mono biz-mask', x.mask || '••••');
        line.append(val, el('span', 'muted', ' ' + sourceText(x)));
        if (!x.format_ok) line.append(chip('format?', 'warn', x.format_note || 'Does not match the usual format'));
        line.append(btn('Reveal', () => doReveal(x, val)));
        if (f.status === 'CONFLICTING') line.append(btn('Use this one', () => act('biz_resolve', { key: f.key, fact_id: x.fact_id, ...(clientId ? { client_id: clientId } : {}) })));
        else if (x.verification !== 'VERIFIED') line.append(btn('Confirm', () => act('biz_confirm', { fact_id: x.fact_id })));
        if (x.verification !== 'VERIFIED' || f.status === 'CONFLICTING') line.append(btn('Reject', () => act('biz_reject', { fact_id: x.fact_id })));
        row.append(line);
      }
      if (!live.length) row.append(el('div', 'muted', STATUS_HELP.MISSING));
      return row;
    }

    function sourceText(x) {
      const s = { USER_ENTERED: 'entered by you', USER_CONFIRMED: 'confirmed by you', DOCUMENT: 'from a document', IMPORT: 'imported' }[x.source_type] || x.source_type;
      return s + (x.doc_id ? ` (${x.doc_id})` : x.source_ref ? ` (${x.source_ref})` : '');
    }

    function viewDocs(data) {
      const wrap = el('div');
      const docs = data.documents || [];
      if (!docs.length) wrap.append(el('p', 'muted', 'No documents. Enroll a folder in the terminal, then scan it: python -m octg_agent business docs enroll <folder>'));
      for (const d of docs) {
        const row = el('div', 'biz-field');
        const head = el('div', 'biz-head');
        head.append(el('span', 'biz-label', d.label), chip(d.state, d.state), chip(d.sensitivity, 'sens-' + d.sensitivity));
        row.append(head, el('div', 'muted', `${d.name} · ${d.fields_found} fact(s) found${d.note ? ' · ' + d.note : ''}`));
        const actions = el('div', 'biz-fact');
        if (['INDEXED', 'DISCOVERED'].includes(d.state)) actions.append(btn('Approve for use', () => act('biz_doc_approve', { doc_id: d.doc_id })));
        if (d.state !== 'EXCLUDED' && d.state !== 'BLOCKED') actions.append(btn('Exclude', () => act('biz_doc_exclude', { doc_id: d.doc_id })));
        row.append(actions);
        wrap.append(row);
      }
      return wrap;
    }

    function viewSources(data) {
      const wrap = el('div');
      wrap.append(el('h3', 'biz-group', 'Authorized folders'));
      for (const r of data.authorized || []) wrap.append(el('div', 'mono', r.path + ((data.enrolled || []).some((e) => e.path.toLowerCase() === r.path.toLowerCase()) ? '   [enrolled]' : '')));
      if (!(data.authorized || []).length) wrap.append(el('p', 'muted', 'None.'));
      wrap.append(el('p', 'muted', 'Folders can only be added or removed in the terminal, so no web page or model can widen what the agent reads:'));
      wrap.append(el('code', '', 'python -m octg_agent fs allow <folder>'), el('br'), el('code', '', data.cli || 'python -m octg_agent business docs enroll <folder>'));
      return wrap;
    }

    function viewSecurity(data) {
      const wrap = el('div');
      const lines = [
        'Business values are never sent to any AI model. Models only see field names and whether a value is available.',
        `Protection of sensitive values at rest: ${data.cipher}${data.cipher_strong ? ' (tied to your Windows account)' : ' (weaker: the key is stored beside the data)'}.`,
        `${data.live_facts} stored fact(s), ${data.encrypted_facts} encrypted. Database healthy: ${data.healthy ? 'yes' : 'NO'}.`,
        `Folders: ${data.folders ? data.folders.authorized : 0} authorized, ${data.folders ? data.folders.enrolled : 0} enrolled.`,
        `Revealed values hide again after ${data.reveal_seconds} seconds.`,
      ];
      for (const l of lines) wrap.append(el('p', '', l));
      wrap.append(el('h3', 'biz-group', 'Models'));
      for (const p of data.providers || []) wrap.append(el('div', '', `${p.name}: ${p.local ? 'runs on this computer' : 'cloud service'}`));
      return wrap;
    }

    function viewActivity(data) {
      const ol = el('ol', 'biz-audit');
      for (const e of (data.events || []).slice().reverse()) {
        const li = el('li');
        li.append(el('time', '', new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false })), el('span', 'k', e.event),
                  el('span', '', [e.field_key || e.doc_id, e.outcome, e.provider ? `${e.provider}/${e.locality}` : '', e.detail].filter(Boolean).join(' · ')));
        ol.append(li);
      }
      if (!ol.children.length) ol.append(el('li', 'muted', 'Nothing yet.'));
      return ol;
    }

    // ---------------------------------------------------------------- imports (preview only)
    function readFile(file) {
      if (o.readFile) return o.readFile(file);
      return new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result).split(',')[1] || '');
        r.onerror = () => reject(r.error);
        r.readAsDataURL(file);
      });
    }

    function previewFile(file) {
      if (!file) return;
      if (file.size > MAX_PREVIEW_BYTES) { say('That file is larger than 3 MB: import it from the terminal instead.', true); return; }
      if (!/\.xlsx$/i.test(file.name || '')) { say('Choose an .xlsx workbook (CSV files are imported from the terminal).', true); return; }
      say('');
      st.previewing = true;
      st.preview = null;
      render();
      readFile(file).then((b64) => {
        request('biz_import_preview', { name: file.name, data_b64: b64 }, (res) => {
          st.previewing = false;
          if (res.ok) st.preview = (res.biz && res.biz.preview) || null; else say(res.error || 'The agent refused that file.', true);
          render();
        });
      }, () => { st.previewing = false; say('That file could not be read.', true); render(); });
    }

    function viewImports(data) {
      const wrap = el('div');
      wrap.append(el('p', 'muted', 'Import your business and client information from an Excel workbook. This tab only PREVIEWS a workbook (nothing is saved). To import, use the terminal, where you confirm every change:'));
      wrap.append(el('code', '', data.cli || ''), el('br'), el('code', '', 'python -m octg_agent business template'), el('span', 'muted', '   (makes the blank workbook)'));
      wrap.append(el('h3', 'biz-group', 'Preview a workbook'));
      const input = doc.createElement('input');
      input.type = 'file';
      input.accept = '.xlsx';
      input.addEventListener('change', () => previewFile(input.files && input.files[0]));
      wrap.append(input);
      if (st.previewing) wrap.append(el('p', 'muted', 'Reading the workbook…'));
      if (st.preview) wrap.append(viewPreview(st.preview));
      wrap.append(el('h3', 'biz-group', 'Past imports'));
      const list = data.imports || [];
      if (!list.length) wrap.append(el('p', 'muted', 'No workbook has been imported yet.'));
      for (const i of list) {
        const c = i.counts || {};
        const row = el('div', 'biz-field');
        const head = el('div', 'biz-head');
        head.append(el('span', 'biz-label', i.workbook), chip(i.status, i.status === 'committed' ? 'VERIFIED' : 'warn'), chip(i.assume_verified ? 'vouched' : 'unverified until confirmed', i.assume_verified ? 'VERIFIED' : 'UNVERIFIED'));
        row.append(head, el('div', 'muted', `${new Date(i.created_at * 1000).toLocaleString()} · ${i.session_id} · ${c.clients_created || 0} client(s), ${c.facts_new || 0} value(s), ${(c.records_new || 0)} row(s)`));
        wrap.append(row);
      }
      return wrap;
    }

    function line(text, cls) { return el('div', cls || '', text); }

    function viewPreview(p) {
      const w = el('div', 'biz-field');
      const d = p.detected || {};
      w.append(el('div', 'biz-label', `Preview of ${p.workbook}`));
      w.append(line(`Detected: ${d.business_profile || 0} business profile, ${d.clients || 0} clients, ${d.services || 0} services, ${d.locations || 0} locations, ${d.seo_targets || 0} SEO targets`));
      const r = p.records || {};
      const c = r.clients || {};
      w.append(line(`Clients: ${c.new || 0} new, ${c.updated || 0} updated, ${c.unchanged || 0} unchanged, ${c.conflict || 0} in conflict, ${c.pending || 0} waiting for a decision`));
      const f = p.facts || {};
      w.append(line(`Values: ${f.new || 0} new, ${f.updated || 0} updated, ${f.unchanged || 0} unchanged, ${f.conflict || 0} in conflict`));
      const m = p.missing || {};
      w.append(line(`Missing: ${m.fields || 0} field(s), ${m.values || 0} value(s) stay missing (nothing is guessed)`));
      w.append(line(`Protected values: ${(p.sensitive && p.sensitive.count) || 0} (shown masked, encrypted when stored, never shown to a model)`));
      for (const s of p.sheets || []) {
        const bits = [s.status];
        if (s.unknown && s.unknown.length) bits.push('unknown columns: ' + s.unknown.join(', '));
        if (s.needs_confirmation && s.needs_confirmation.length) bits.push('NEEDS CONFIRMATION: ' + s.needs_confirmation.join(', '));
        if (s.refused && s.refused.length) bits.push('refused columns: ' + s.refused.join(', '));
        w.append(line(`${s.sheet}: ${bits.join(' · ')}`, 'muted'));
      }
      for (const x of p.conflicts || []) w.append(line(`Conflict: ${x.field} (${x.client}): existing ${x.existing} / workbook ${x.spreadsheet}`, 'bad'));
      for (const x of p.duplicates || []) w.append(line(`Possible duplicate: ${x.spreadsheet_client} may be ${x.possible_duplicate_of} (${(x.because || []).join(', ')})`, 'bad'));
      const errs = (p.issues || []).filter((i) => i.level === 'error');
      if (errs.length) {
        w.append(line(`${errs.length} problem(s) would be left out:`, 'bad'));
        for (const i of errs.slice(0, 15)) w.append(line(`${i.sheet} row ${i.row} ${i.column}: ${i.message}`, 'muted'));
      }
      for (const t of p.warnings || []) w.append(line(t, 'muted'));
      w.append(line('Nothing was saved. To import: run the command above in the terminal; you will be asked to confirm.', 'muted'));
      return w;
    }

    // ---------------------------------------------------------------- clients
    function viewClients(data) {
      const wrap = el('div');
      if (st.client) return viewOneClient(wrap);
      wrap.append(el('p', 'muted', 'Each client is separate from your own business and from every other client. Create or edit clients in the terminal: python -m octg_agent business client create "<name>"'));
      const list = data.clients || [];
      if (!list.length) wrap.append(el('p', 'muted', 'No clients yet.'));
      for (const c of list) {
        const row = el('div', 'biz-field');
        const head = el('div', 'biz-head');
        head.append(el('span', 'biz-label', c.name), chip(`${c.facts} fact(s)`, 'info'), chip(`${c.documents} doc(s)`, 'info'));
        row.append(head);
        const actions = el('div', 'biz-fact');
        actions.append(btn('Open', () => openClient(c.client_id)));
        row.append(actions);
        wrap.append(row);
      }
      return wrap;
    }

    function viewOneClient(wrap) {
      const c = st.clients.find((x) => x.client_id === st.client) || { name: st.client };
      const back = el('div', 'biz-fact');
      back.append(btn('← All clients', () => { st.client = null; load('clients'); }), el('strong', '', c.name));
      wrap.append(back);
      const tabs = el('div', 'biz-tabs');
      for (const [id, label] of CLIENT_TABS) { const b = btn(label, () => loadClient(id), 'tab'); b.dataset.tab = id; b.setAttribute('aria-selected', String(id === st.clientTab)); tabs.append(b); }
      wrap.append(tabs);
      const data = st.data['client:' + st.clientTab];
      if (!data) { wrap.append(el('p', 'muted', 'Loading…')); return wrap; }
      if (st.clientTab === 'profile') wrap.append(viewProfile(data, st.client));
      else if (st.clientTab === 'docs') wrap.append(viewDocs(data));
      else if (st.clientTab === 'activity') wrap.append(viewActivity(data));
      else if (st.clientTab === 'sources') {
        wrap.append(el('h3', 'biz-group', 'Folders enrolled for this client'));
        for (const f of data.enrolled || []) wrap.append(el('div', 'mono', f.path));
        if (!(data.enrolled || []).length) wrap.append(el('p', 'muted', 'None. A folder belongs to exactly one owner; documents in it can only be used for this client.'));
        wrap.append(el('p', 'muted', 'Folders can only be enrolled in the terminal:'), el('code', '', data.cli || ''));
      } else if (st.clientTab === 'seo') {
        wrap.append(el('p', 'muted', 'SEO and local-SEO configuration for this client. Research results will be stored later as dated observations (never as permanent facts). '
                                      + `Recorded so far: ${data.sessions || 0} research session(s), ${data.observations || 0} observation(s).`));
        wrap.append(viewProfile({ fields: data.fields || [] }, st.client));
      }
      return wrap;
    }

    function openClient(id) { st.client = id; st.clientTab = 'profile'; loadClient('profile'); }

    function loadClient(tab) {
      st.clientTab = tab;
      render();
      const sub = CLIENT_TABS.find((t) => t[0] === tab);
      request(sub[2], { client_id: st.client, ...(tab === 'activity' ? { n: 60 } : {}) }, (res) => { if (res.ok) { st.data['client:' + tab] = res.biz; render(); } else say(res.error || 'The agent refused that.', true); });
    }

    function fillClientSelects() {
      const opts = (sel, first) => {
        if (!sel || !doc) return;
        const keep = sel.value;
        const make = (v, t) => { const o = doc.createElement('option'); o.value = v; o.textContent = t; return o; };
        sel.replaceChildren(...first.map(([v, t]) => make(v, t)), ...st.clients.map((c) => make(c.client_id, c.name)));
        sel.value = [...(sel.children || [])].some((o) => o.value === keep) ? keep : (first[0] ? first[0][0] : '');
      };
      opts(o.clientSelect, [['auto', 'Client: auto (only if the wording names exactly one)'], ['me', 'My own business']]);
      opts(o.pickSelect, []);
    }

    const VIEWS = { profile: viewProfile, clients: viewClients, documents: viewDocs, sources: viewSources, imports: viewImports, security: viewSecurity, activity: viewActivity };

    // ---------------------------------------------------------------- behaviour
    function render() {
      wipeReveal();
      if (!o.body) return;
      const data = st.data[st.tab];
      o.body.replaceChildren(data ? VIEWS[st.tab](data) : el('p', 'muted', 'Loading…'));
      if (o.tabs) for (const b of o.tabs.children || []) b.setAttribute('aria-selected', String(b.dataset.tab === st.tab));
    }

    function load(tab) {
      if (tab !== 'imports') { st.preview = null; st.previewing = false; }   // a previewed workbook is not kept when you leave the tab
      st.tab = tab;
      render();
      request(COMMAND[tab], tab === 'activity' ? { n: 60 } : {}, (res) => {
        if (!res.ok) return;
        st.data[tab] = res.biz;
        if (tab === 'clients') { st.clients = res.biz.clients || []; fillClientSelects(); }
        render();
        if (tab === 'clients' && st.client) loadClient(st.clientTab);
      });
    }

    function reload() { if (st.tab === 'clients' && st.client) loadClient(st.clientTab); else load(st.tab); }

    function act(command, extra) {
      say('');
      request(command, extra, (res) => { if (res.ok) reload(); else say(res.error || 'The agent refused that.', true); });
    }

    function doReveal(fact, node) {
      wipeReveal();
      request('biz_reveal', { fact_id: fact.fact_id }, (res) => {
        if (!res.ok) { say(res.error || 'Could not reveal that value.', true); return; }
        node.textContent = res.biz.value;
        st.reveal = { node, factId: fact.fact_id };
        revealTimer = o.setTimeout(wipeReveal, (res.biz.hide_after_s || 10) * 1000);
      });
    }

    function onResult(msg) {
      if (!msg || msg.type !== 'biz_result') return;
      const fn = pending.get(msg.request_id);
      if (fn === undefined) return;
      pending.delete(msg.request_id);
      if (fn) fn(msg);
    }

    function sync({ connected }) {
      const was = st.connected;
      st.connected = !!connected;
      if (st.connected && !was) request('biz_status', {}, (res) => {
        const on = !!(res.ok && res.biz && res.biz.enabled);
        st.enabled = on;
        if (o.root) o.root.hidden = !on;
        if (on) {
          st.data.security = res.biz;
          load(st.tab);
          request('biz_clients', {}, (r) => { if (r.ok) { st.clients = r.biz.clients || []; fillClientSelects(); } });  // for the per-task Client box
        }
      });
      if (!st.connected) { wipeReveal(); st.preview = null; st.enabled = false; if (o.root) o.root.hidden = true; }
    }

    if (o.tabs) {
      for (const [id, label] of TABS) { const b = btn(label, () => load(id), 'tab'); b.dataset.tab = id; o.tabs.append(b); }
    }
    if (o.root && typeof o.root.addEventListener === 'function') o.root.addEventListener('toggle', () => { if (!o.root.open) wipeReveal(); });

    return { onResult, sync, load, wipeReveal, get state() { return st; }, get revealed() { return !!st.reveal; } };
  }

  return { createBizPanel, TABS, COMMAND };
});
