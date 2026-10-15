'use strict';
// Unit tests for extension/sidepanel/biz_ui.js: masked values, explicit temporary reveal, no storage, no markup injection, and what the panel can and cannot ask for.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { FakeClock } = require('./helpers.cjs');

const SIDEPANEL = path.join(__dirname, '..', '..', '..', 'extension', 'sidepanel');
const UI = require(path.join(SIDEPANEL, 'biz_ui.js'));

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.attrs = {}; this.className = ''; this._text = ''; this.hidden = false; this.checked = false; this.type = ''; this.title = ''; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  append(...kids) { for (const k of kids) this.children.push(k); }
  replaceChildren(...kids) { this.children = kids; this._text = ''; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = v; }
  click() { for (const f of this.listeners.click || []) f({ type: 'click' }); }
  change(checked) { this.checked = checked; for (const f of this.listeners.change || []) f({ type: 'change' }); }
  all(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.children) c.all(pred, out); return out; }
  button(label) { return this.all((n) => n.tag === 'button' && n.textContent === label); }
  get innerHTMLUsed() { return false; }
}
const doc = { createElement: (tag) => new Node(tag) };

const FIELD = (over) => ({ key: 'taxation.pan', label: 'PAN of business', group: 'taxation', sensitivity: 'HIGH', status: 'VERIFIED',
  facts: [{ fact_id: 'fact_1', mask: 'AB••••••4F', sensitivity: 'HIGH', verification: 'VERIFIED', state: 'LIVE', source_type: 'USER_ENTERED', source_ref: '', doc_id: null, format_ok: true, format_note: '' }], ...over });

function rig(opts) {
  opts = opts || {};
  const clock = new FakeClock();
  const sent = [];
  const root = new Node('details'); root.hidden = true; root.open = true;
  const els = { doc, root, body: new Node('div'), msg: new Node('div'), tabs: new Node('div') };
  const ui = UI.createBizPanel({ ...els, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, send: (cmd, extra) => sent.push({ cmd, ...extra }), ...(opts.panel || {}) });
  const reply = (cmd, biz, extra) => {
    const req = sent.filter((s) => s.cmd === cmd).pop();
    assert.ok(req, 'no request for ' + cmd);
    ui.onResult({ type: 'biz_result', request_id: req.request_id, command: cmd, ok: true, biz, ...(extra || {}) });
  };
  return { ui, clock, sent, els, reply };
}
function enable(r, profile) {
  r.ui.sync({ connected: true });
  r.reply('biz_status', { enabled: true, healthy: true, cipher: 'dpapi', cipher_strong: true, live_facts: 1, encrypted_facts: 1, providers: [{ name: 'local', local: true }, { name: 'cloud', local: false }], folders: { authorized: 1, enrolled: 1 }, reveal_seconds: 10 });
  r.reply('biz_profile', { fields: profile || [FIELD()] });
}

test('the section stays hidden until the agent says the feature is on', () => {
  const r = rig();
  r.ui.sync({ connected: true });
  assert.equal(r.sent[0].cmd, 'biz_status');
  r.reply('biz_status', { enabled: false });
  assert.equal(r.els.root.hidden, true);
  assert.equal(r.sent.length, 1, 'nothing else is requested while the feature is off');
});

test('turning on shows the section and loads the profile', () => {
  const r = rig();
  enable(r);
  assert.equal(r.els.root.hidden, false);
  assert.ok(r.els.body.textContent.includes('PAN of business'));
});

test('disconnecting hides the section again and wipes anything revealed', () => {
  const r = rig();
  enable(r);
  r.ui.sync({ connected: false });
  assert.equal(r.els.root.hidden, true);
  assert.equal(r.ui.state.enabled, false);
});

test('values are masked: only the mask text is in the page, never a value', () => {
  const r = rig();
  enable(r);
  const text = r.els.body.textContent;
  assert.ok(text.includes('AB••••••4F'));
  assert.ok(!text.includes('ABCDE1234F'));
  assert.ok(text.includes('HIGH') && text.includes('VERIFIED') && text.includes('entered by you'));
});

test('reveal is explicit, shows the value in place, and hides it again by itself', async () => {
  const r = rig();
  enable(r);
  r.els.body.button('Reveal')[0].click();
  assert.equal(r.sent.at(-1).cmd, 'biz_reveal');
  assert.equal(r.sent.at(-1).fact_id, 'fact_1');
  assert.ok(!r.els.body.textContent.includes('ABCDE1234F'), 'nothing is shown before the answer arrives');
  r.reply('biz_reveal', { fact_id: 'fact_1', value: 'ABCDE1234F', hide_after_s: 10 });
  assert.ok(r.els.body.textContent.includes('ABCDE1234F'));
  assert.equal(r.ui.revealed, true);
  await r.clock.tick(9000);
  assert.ok(r.els.body.textContent.includes('ABCDE1234F'));
  await r.clock.tick(1500);
  assert.ok(!r.els.body.textContent.includes('ABCDE1234F'), 'hidden after the timeout');
  assert.equal(r.ui.revealed, false);
});

test('switching tab, re-rendering or collapsing wipes a revealed value immediately', () => {
  const r = rig();
  enable(r);
  r.els.body.button('Reveal')[0].click();
  r.reply('biz_reveal', { fact_id: 'fact_1', value: 'ABCDE1234F', hide_after_s: 10 });
  r.ui.load('documents');
  assert.ok(!r.els.body.textContent.includes('ABCDE1234F'));
  assert.equal(r.ui.revealed, false);
  r.ui.load('profile');
  r.reply('biz_profile', { fields: [FIELD()] });
  r.els.body.button('Reveal')[0].click();
  r.reply('biz_reveal', { fact_id: 'fact_1', value: 'ABCDE1234F', hide_after_s: 10 });
  r.els.root.open = false;
  r.els.root.listeners.toggle[0]();
  assert.ok(!r.els.body.textContent.includes('ABCDE1234F'));
});

test('a refused reveal shows the agent message and no value', () => {
  const r = rig();
  enable(r);
  r.els.body.button('Reveal')[0].click();
  const req = r.sent.at(-1);
  r.ui.onResult({ type: 'biz_result', request_id: req.request_id, command: 'biz_reveal', ok: false, error: 'could not decrypt' });
  assert.equal(r.els.msg.textContent, 'could not decrypt');
  assert.equal(r.ui.revealed, false);
});

test('missing fields are collapsed by default and can be shown', () => {
  const r = rig();
  enable(r, [FIELD(), FIELD({ key: 'registration.cin', label: 'CIN', group: 'registration', status: 'MISSING', facts: [] })]);
  assert.ok(!r.els.body.textContent.includes('CIN'));
  assert.ok(r.els.body.textContent.includes('Show empty fields (1)'));
  const cb = r.els.body.all((n) => n.tag === 'input')[0];
  cb.change(true);
  assert.ok(r.els.body.textContent.includes('CIN'));
  assert.ok(r.els.body.textContent.includes('agent will ask you instead of guessing'));
});

test('an unverified fact offers Confirm and Reject; confirming asks the agent and reloads', () => {
  const r = rig();
  enable(r, [FIELD({ status: 'UNVERIFIED', facts: [{ ...FIELD().facts[0], verification: 'UNVERIFIED', source_type: 'DOCUMENT', doc_id: 'doc_1' }] })]);
  assert.ok(r.els.body.textContent.includes('from a document (doc_1)'));
  assert.equal(r.els.body.button('Confirm').length, 1);
  assert.equal(r.els.body.button('Reject').length, 1);
  r.els.body.button('Confirm')[0].click();
  assert.deepEqual([r.sent.at(-1).cmd, r.sent.at(-1).fact_id], ['biz_confirm', 'fact_1']);
  r.reply('biz_confirm', { field: {} });
  assert.equal(r.sent.at(-1).cmd, 'biz_profile', 'the view is reloaded after a decision');
});

test('a verified fact has neither Confirm nor Reject', () => {
  const r = rig();
  enable(r);
  assert.equal(r.els.body.button('Confirm').length, 0);
  assert.equal(r.els.body.button('Reject').length, 0);
});

test('a conflict shows every candidate with Use this one, and resolving sends the key and the chosen fact', () => {
  const f = FIELD({ key: 'addresses.principal', label: 'Principal place of business', group: 'addresses', sensitivity: 'MEDIUM', status: 'CONFLICTING', facts: [
    { ...FIELD().facts[0], fact_id: 'fact_a', mask: '123 E•••' }, { ...FIELD().facts[0], fact_id: 'fact_b', mask: '9 Oth•••', verification: 'UNVERIFIED', source_type: 'DOCUMENT', doc_id: 'doc_2' }] });
  const r = rig();
  enable(r, [f]);
  assert.ok(r.els.body.textContent.includes('CONFLICTING'));
  assert.equal(r.els.body.button('Use this one').length, 2);
  r.els.body.button('Use this one')[1].click();
  assert.deepEqual([r.sent.at(-1).cmd, r.sent.at(-1).key, r.sent.at(-1).fact_id], ['biz_resolve', 'addresses.principal', 'fact_b']);
});

test('documents: approve and exclude go to the agent; blocked documents offer nothing', () => {
  const r = rig();
  enable(r);
  r.ui.load('documents');
  r.reply('biz_docs', { documents: [
    { doc_id: 'doc_1', name: 'gst.txt', label: 'GST registration certificate', doc_type: 'gst_certificate', sensitivity: 'HIGH', state: 'INDEXED', fields_found: 3, note: '' },
    { doc_id: 'doc_2', name: 'bank_statement.txt', label: 'Other document', doc_type: 'other', sensitivity: 'HIGH', state: 'BLOCKED', fields_found: 0, note: 'banking files are not supported' }] });
  assert.equal(r.els.body.button('Approve for use').length, 1);
  assert.equal(r.els.body.button('Exclude').length, 1);
  r.els.body.button('Approve for use')[0].click();
  assert.deepEqual([r.sent.at(-1).cmd, r.sent.at(-1).doc_id], ['biz_doc_approve', 'doc_1']);
});

test('the sources tab points to the terminal: the panel cannot enroll or authorize anything', () => {
  const r = rig();
  enable(r);
  r.ui.load('sources');
  r.reply('biz_sources', { authorized: [{ path: 'D:\\Docs' }], enrolled: [{ path: 'D:\\Docs' }], cli: 'python -m octg_agent business docs enroll <folder>' });
  const t = r.els.body.textContent;
  assert.ok(t.includes('D:\\Docs') && t.includes('[enrolled]') && t.includes('fs allow'));
  assert.equal(r.els.body.all((n) => n.tag === 'input').length, 0, 'no input for adding folders');
});

test('security tab: plain statement, protection, and which models are local or cloud', () => {
  const r = rig();
  enable(r);
  r.ui.load('security');
  r.reply('biz_status', { enabled: true, healthy: true, cipher: 'dpapi', cipher_strong: true, live_facts: 2, encrypted_facts: 1, providers: [{ name: 'local', local: true }, { name: 'gemini', local: false }], folders: { authorized: 1, enrolled: 0 }, reveal_seconds: 10 });
  const t = r.els.body.textContent;
  assert.ok(t.includes('never sent to any AI model'));
  assert.ok(t.includes('local: runs on this computer') && t.includes('gemini: cloud service'));
  assert.ok(t.includes('tied to your Windows account'));
});

test('activity lists audit events without any value field to show', () => {
  const r = rig();
  enable(r);
  r.ui.load('activity');
  r.reply('biz_audit', { events: [{ ts: 1700000000, event: 'use', field_key: 'taxation.gstin', outcome: 'typed', provider: '', locality: '', detail: 'step 3; HIGH' }] });
  assert.ok(r.els.body.textContent.includes('taxation.gstin'));
  assert.equal(r.sent.at(-1).cmd, 'biz_audit');
});

test('text from documents or the agent is shown as text, never as markup', () => {
  const evil = '<img src=x onerror=alert(1)><script>alert(1)</script>';
  const r = rig();
  enable(r, [FIELD({ label: evil, facts: [{ ...FIELD().facts[0], mask: evil, source_ref: evil }] })]);
  assert.ok(r.els.body.textContent.includes(evil), 'shown literally');
  const html = fs.readFileSync(path.join(SIDEPANEL, 'biz_ui.js'), 'utf8');
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(/.test(html.replace(/\/\/.*$/gm, '')), 'biz_ui.js never builds markup from strings');
});

test('the module keeps nothing: no storage, no network, no clipboard', () => {
  const src = fs.readFileSync(path.join(SIDEPANEL, 'biz_ui.js'), 'utf8').replace(/\/\/.*$/gm, '');
  for (const banned of ['localStorage', 'sessionStorage', 'indexedDB', 'chrome.storage', 'fetch(', 'XMLHttpRequest', 'WebSocket', 'navigator.clipboard', 'document.cookie']) {
    assert.ok(!src.includes(banned), banned);
  }
});

test('results for requests it did not make are ignored', () => {
  const r = rig();
  enable(r);
  const before = r.els.body.textContent;
  r.ui.onResult({ type: 'biz_result', request_id: 'someone_else', command: 'biz_profile', ok: true, biz: { fields: [] } });
  r.ui.onResult({ type: 'other' });
  r.ui.onResult(null);
  assert.equal(r.els.body.textContent, before);
});

test('only confirm/reject/resolve/reveal/approve/exclude and read views are ever requested', () => {
  const r = rig();
  enable(r, [FIELD({ status: 'UNVERIFIED', facts: [{ ...FIELD().facts[0], verification: 'UNVERIFIED' }] })]);
  for (const tab of ['documents', 'sources', 'security', 'activity', 'profile']) r.ui.load(tab);
  for (const b of r.els.body.all((n) => n.tag === 'button')) b.click();
  const allowed = new Set(['biz_status', 'biz_profile', 'biz_docs', 'biz_sources', 'biz_audit', 'biz_confirm', 'biz_reject', 'biz_resolve', 'biz_reveal', 'biz_doc_approve', 'biz_doc_exclude',
                           'biz_clients', 'biz_client', 'biz_client_docs', 'biz_client_sources', 'biz_client_seo', 'biz_client_audit']);
  for (const s of r.sent) assert.ok(allowed.has(s.cmd), s.cmd);
});

test('the panel wiring: service worker relays results in memory only, and panel.html loads the module', () => {
  const root = path.join(SIDEPANEL, '..');
  const bridge = fs.readFileSync(path.join(root, 'background', 'bridge.js'), 'utf8');
  const sw = fs.readFileSync(path.join(root, 'background', 'service_worker.js'), 'utf8');
  const html = fs.readFileSync(path.join(SIDEPANEL, 'panel.html'), 'utf8');
  assert.ok(bridge.includes("msg.command.startsWith('biz_')") && bridge.includes('bizListeners'));
  const block = bridge.slice(bridge.indexOf("msg.command.startsWith('biz_')"), bridge.indexOf("} else if (msg.type === 'ui_result') {"));
  const code = block.replace(/\/\/.*$/gm, '').replace('bizListeners', '');
  assert.ok(!/bridge\.|storage|pushLog|toast/.test(code), 'biz results never touch bridge state, storage, the log or toasts');
  assert.ok(sw.includes('bizListeners.add'));
  assert.ok(html.indexOf('biz_ui.js') > html.indexOf('voice_ui.js') && html.indexOf('biz_ui.js') < html.indexOf('panel.js'));
  assert.ok(/<details id="biz"[^>]*hidden/.test(html), 'the section is hidden in the markup');
});


// ------------------------------------------------------------------ Clients (Client Brain)
const CLIENTS = [{ client_id: 'cli_a', name: 'Example Locksmith LLC', archived: false, facts: 3, documents: 1, sessions: 0, observations: 0 },
                 { client_id: 'cli_b', name: 'Example Roofing LLC', archived: false, facts: 2, documents: 0, sessions: 0, observations: 0 }];
const CLIENT_FIELD = (over) => ({ key: 'contact.phone', label: 'Business phone', group: 'contact', sensitivity: 'MEDIUM', status: 'VERIFIED', scope: 'cli_a',
  facts: [{ fact_id: 'fact_c1', mask: '•••••210', sensitivity: 'MEDIUM', verification: 'VERIFIED', state: 'LIVE', source_type: 'USER_ENTERED', source_ref: '', doc_id: null, format_ok: true, format_note: '' }], ...over });

function clientRig() {
  const sel = new Node('select'); sel.value = 'auto';
  const r = rig({ panel: { clientSelect: sel, pickSelect: new Node('select') } });
  r.sel = sel;
  enable(r);
  r.reply('biz_clients', { clients: CLIENTS });
  return r;
}
function openClients(r) {
  r.ui.load('clients');
  r.reply('biz_clients', { clients: CLIENTS });
}

test('Clients tab lists clients with counts and offers no way to create or edit one', () => {
  const r = clientRig();
  openClients(r);
  const t = r.els.body.textContent;
  assert.ok(t.includes('Example Locksmith LLC') && t.includes('Example Roofing LLC') && t.includes('3 fact(s)'));
  assert.ok(t.includes('business client create'), 'creation is a terminal action');
  assert.equal(r.els.body.all((n) => n.tag === 'input' || n.tag === 'textarea').length, 0, 'no field for typing client data');
  assert.equal(r.els.body.button('Open').length, 2);
});

test('the per-task Client box offers Auto, My own business and each client', () => {
  const r = clientRig();
  const labels = r.sel.children.map((o) => o.textContent);
  assert.ok(labels[0].startsWith('Client: auto') && labels[1] === 'My own business' && labels.includes('Example Locksmith LLC') && labels.includes('Example Roofing LLC'));
  assert.deepEqual(r.sel.children.map((o) => o.value).slice(0, 2), ['auto', 'me']);
});

test('opening a client asks the agent for THAT client only and shows masked values', () => {
  const r = clientRig();
  openClients(r);
  r.els.body.button('Open')[0].click();
  const req = r.sent.at(-1);
  assert.deepEqual([req.cmd, req.client_id], ['biz_client', 'cli_a']);
  r.reply('biz_client', { client: CLIENTS[0], fields: [CLIENT_FIELD()] });
  const t = r.els.body.textContent;
  assert.ok(t.includes('Business phone') && t.includes('•••••210') && t.includes('Example Locksmith LLC'));
  assert.ok(!t.includes('Example Roofing'), 'another client never appears inside this client view');
});

test('each client sub-tab requests its own view, always naming the same client', () => {
  const r = clientRig();
  openClients(r);
  r.els.body.button('Open')[0].click();
  r.reply('biz_client', { client: CLIENTS[0], fields: [] });
  for (const [label, cmd] of [['Documents', 'biz_client_docs'], ['Sources', 'biz_client_sources'], ['SEO', 'biz_client_seo'], ['Activity', 'biz_client_audit']]) {
    r.els.body.button(label)[0].click();
    assert.deepEqual([r.sent.at(-1).cmd, r.sent.at(-1).client_id], [cmd, 'cli_a'], label);
  }
});

test('client documents, sources and SEO views render what the agent sent and no controls that widen access', () => {
  const r = clientRig();
  openClients(r);
  r.els.body.button('Open')[0].click();
  r.reply('biz_client', { client: CLIENTS[0], fields: [] });
  r.els.body.button('Documents')[0].click();
  r.reply('biz_client_docs', { client: CLIENTS[0], documents: [{ doc_id: 'doc_9', name: 'gst.txt', label: 'GST registration certificate', doc_type: 'gst_certificate', sensitivity: 'HIGH', state: 'INDEXED', fields_found: 2, note: '', client_id: 'cli_a' }] });
  assert.equal(r.els.body.button('Approve for use').length, 1);
  r.els.body.button('Sources')[0].click();
  r.reply('biz_client_sources', { client: CLIENTS[0], enrolled: [{ path: 'D:\\ClientA' }], cli: 'python -m octg_agent business docs enroll <folder> --client cli_a' });
  assert.ok(r.els.body.textContent.includes('D:\\ClientA') && r.els.body.textContent.includes('--client cli_a'));
  assert.equal(r.els.body.all((n) => n.tag === 'input').length, 0);
  r.els.body.button('SEO')[0].click();
  r.reply('biz_client_seo', { client: CLIENTS[0], sessions: 2, observations: 14, fields: [CLIENT_FIELD({ key: 'seo.target_keywords[0]', label: 'Target keyword', group: 'seo', sensitivity: 'LOW' })] });
  const t = r.els.body.textContent;
  assert.ok(t.includes('2 research session(s), 14 observation(s)') && t.includes('Target keyword') && t.includes('never as permanent facts'));
});

test('resolving a conflict inside a client names the client; a reveal there is wiped when the client view changes', () => {
  const r = clientRig();
  openClients(r);
  r.els.body.button('Open')[0].click();
  const facts = [{ ...CLIENT_FIELD().facts[0], fact_id: 'fact_x', mask: '•••210' }, { ...CLIENT_FIELD().facts[0], fact_id: 'fact_y', mask: '•••999', verification: 'UNVERIFIED', source_type: 'DOCUMENT', doc_id: 'doc_1' }];
  r.reply('biz_client', { client: CLIENTS[0], fields: [CLIENT_FIELD({ status: 'CONFLICTING', facts })] });
  r.els.body.button('Use this one')[1].click();
  assert.deepEqual([r.sent.at(-1).cmd, r.sent.at(-1).key, r.sent.at(-1).fact_id, r.sent.at(-1).client_id], ['biz_resolve', 'contact.phone', 'fact_y', 'cli_a']);
  r.reply('biz_resolve', { field: {} });
  assert.equal(r.sent.at(-1).cmd, 'biz_client', 'the same client view is reloaded');
  r.reply('biz_client', { client: CLIENTS[0], fields: [CLIENT_FIELD()] });
  r.els.body.button('Reveal')[0].click();
  r.reply('biz_reveal', { fact_id: 'fact_c1', value: '555-0100210', hide_after_s: 10 });
  assert.ok(r.els.body.textContent.includes('555-0100210'));
  r.els.body.button('Documents')[0].click();
  assert.ok(!r.els.body.textContent.includes('555-0100210'));
});

test('going back to All clients shows the list again', () => {
  const r = clientRig();
  openClients(r);
  r.els.body.button('Open')[1].click();
  r.reply('biz_client', { client: CLIENTS[1], fields: [] });
  assert.ok(r.els.body.textContent.includes('Example Roofing LLC'));
  r.els.body.button('← All clients')[0].click();
  r.reply('biz_clients', { clients: CLIENTS });
  assert.equal(r.els.body.button('Open').length, 2);
});


// ------------------------------------------------------------------ Imports (spreadsheet import: preview only)
const PREVIEW = {
  workbook: 'OCTG_Business_Data.xlsx', sha256: 'abc123',
  detected: { business_profile: 1, clients: 2, services: 4, locations: 0, seo_targets: 0 },
  records: { clients: { new: 2, updated: 0, unchanged: 0, conflict: 0, pending: 0 } },
  facts: { new: 14, updated: 0, unchanged: 0, conflict: 1, skipped: 0 },
  missing: { fields: 5, values: 9 },
  sensitive: { count: 3, items: [{ scope: 'your own business', field: 'PAN of business', action: 'NEW', value: 'AB••••••4F' }] },
  sheets: [{ sheet: 'Clients', status: 'read', unknown: ['Favourite colour'], needs_confirmation: ['Name'], refused: ['Password'] }],
  conflicts: [{ field: 'Business phone', client: 'Example Locksmith LLC', existing: '•••••000', spreadsheet: '•••••999' }],
  duplicates: [{ spreadsheet_client: 'ABC Locksmith Services', possible_duplicate_of: 'ABC Locksmith', because: ['similar name'] }],
  issues: [{ level: 'error', sheet: 'Client Services', row: 7, column: 'client_id', message: 'matches no client' }, { level: 'info', sheet: 'Clients', row: 2, column: 'description', message: 'note' }],
  warnings: ['sheet "Clients" has 1 formula cell(s)'],
};

function importsRig(fileText) {
  const r = rig({ panel: { readFile: async (f) => Buffer.from(f.text || fileText || 'x').toString('base64') } });
  enable(r);
  r.ui.load('imports');
  r.reply('biz_imports', { imports: [{ session_id: 'imp_1', workbook: 'Old.xlsx', status: 'committed', created_at: 1700000000, assume_verified: false, counts: { clients_created: 2, facts_new: 14, records_new: 5 } }],
                           cli: 'python -m octg_agent business import "<your workbook>.xlsx"', template_cli: 'python -m octg_agent business template', max_bytes: 3145728 });
  return r;
}
const pick = (r, file) => { const input = r.els.body.all((n) => n.tag === 'input')[0]; input.files = [file]; input.listeners.change[0]({ type: 'change' }); return input; };
const tick = () => new Promise((res) => setImmediate(res));

test('the Imports tab explains that importing happens in the terminal and offers no commit control', () => {
  const r = importsRig();
  const t = r.els.body.textContent;
  assert.ok(t.includes('only PREVIEWS') && t.includes('business import') && t.includes('business template') && t.includes('Old.xlsx') && t.includes('unverified until confirmed'));
  const buttons = r.els.body.all((n) => n.tag === 'button').map((b) => b.textContent.toLowerCase());
  assert.ok(!buttons.some((b) => /import|commit|confirm|apply|save/.test(b)), 'no button that could write anything: ' + buttons);
  assert.equal(r.els.body.all((n) => n.tag === 'input').length, 1, 'one file chooser');
  assert.equal(r.els.body.all((n) => n.tag === 'input')[0].accept, '.xlsx');
});

test('choosing a workbook sends its name and content once for a preview and shows the masked result', async () => {
  const r = importsRig('workbook-bytes');
  pick(r, { name: 'OCTG_Business_Data.xlsx', size: 1000 });
  await tick();
  const req = r.sent.filter((s) => s.cmd === 'biz_import_preview').pop();
  assert.equal(req.name, 'OCTG_Business_Data.xlsx');
  assert.equal(req.data_b64, Buffer.from('workbook-bytes').toString('base64'));
  r.reply('biz_import_preview', { preview: PREVIEW, cli: 'python -m octg_agent business import "<your workbook>.xlsx"' });
  const t = r.els.body.textContent;
  for (const needle of ['Preview of OCTG_Business_Data.xlsx', '2 clients', '4 services', 'Protected values: 3', 'Conflict: Business phone', 'Possible duplicate: ABC Locksmith Services', 'unknown columns: Favourite colour',
                        'NEEDS CONFIRMATION: Name', 'refused columns: Password', '1 problem(s) would be left out', 'matches no client', 'Nothing was saved']) assert.ok(t.includes(needle), needle);
  assert.ok(!t.includes('ABCDE1234F'), 'only masks are shown');
});

test('a file that is too large or not an .xlsx is refused in the panel and nothing is sent', async () => {
  const r = importsRig();
  const before = r.sent.length;
  pick(r, { name: 'big.xlsx', size: 4 * 1024 * 1024 });
  await tick();
  assert.ok(r.els.msg.textContent.includes('3 MB'));
  pick(r, { name: 'clients.csv', size: 100 });
  await tick();
  assert.ok(r.els.msg.textContent.includes('.xlsx'));
  assert.equal(r.sent.length, before);
});

test('an agent refusal of a file is shown and no preview remains', async () => {
  const r = importsRig();
  pick(r, { name: 'bad.xlsx', size: 10 });
  await tick();
  const req = r.sent.filter((s) => s.cmd === 'biz_import_preview').pop();
  r.ui.onResult({ type: 'biz_result', request_id: req.request_id, command: 'biz_import_preview', ok: false, error: 'the workbook is damaged', code: 'MALFORMED' });
  assert.ok(r.els.msg.textContent.includes('damaged'));
  assert.equal(r.ui.state.preview, null);
});

test('a preview is dropped when you leave the tab or the agent disconnects', async () => {
  const r = importsRig();
  pick(r, { name: 'w.xlsx', size: 10 });
  await tick();
  r.reply('biz_import_preview', { preview: PREVIEW });
  assert.ok(r.ui.state.preview);
  r.ui.load('profile');
  assert.equal(r.ui.state.preview, null);
  r.ui.load('imports');
  r.reply('biz_imports', { imports: [], cli: '' });
  pick(r, { name: 'w.xlsx', size: 10 });
  await tick();
  r.reply('biz_import_preview', { preview: PREVIEW });
  r.ui.sync({ connected: false });
  assert.equal(r.ui.state.preview, null);
});

test('a hostile workbook name or message is shown as text, never as markup', async () => {
  const r = importsRig();
  pick(r, { name: '<img src=x onerror=alert(1)>.xlsx', size: 10 });
  await tick();
  r.reply('biz_import_preview', { preview: { ...PREVIEW, workbook: '<img src=x onerror=alert(1)>.xlsx', issues: [{ level: 'error', sheet: '<b>', row: 1, column: '<i>', message: '<script>x</script>' }] } });
  assert.ok(r.els.body.textContent.includes('<img src=x onerror=alert(1)>'));
  const src = fs.readFileSync(path.join(SIDEPANEL, 'biz_ui.js'), 'utf8');
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(src.replace(/\/\/.*$/gm, '')));
});

test('the Imports tab only ever requests biz_imports and biz_import_preview (never anything that writes)', async () => {
  const r = importsRig();
  pick(r, { name: 'w.xlsx', size: 10 });
  await tick();
  r.reply('biz_import_preview', { preview: PREVIEW });
  for (const b of r.els.body.all((n) => n.tag === 'button')) b.click();
  const cmds = new Set(r.sent.map((s) => s.cmd));
  assert.ok(cmds.has('biz_imports') && cmds.has('biz_import_preview'));
  for (const c of cmds) assert.ok(!/import_(commit|confirm|decide|verify)|template/.test(c), c);
});
