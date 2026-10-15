'use strict';
// Unit tests for extension/sidepanel/ask_ui.js: the Ask view and the Inbox view (AI -> owner requests). No markup injection, nothing stored, only the commands the panel is allowed to send.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { FakeClock } = require('./helpers.cjs');

const UI = require(path.join(__dirname, '..', '..', '..', 'extension', 'sidepanel', 'ask_ui.js'));

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {}; this.className = ''; this._text = ''; this.hidden = false; this.disabled = false; this.value = ''; this.type = ''; this.title = ''; this.placeholder = ''; this.rows = 0; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(_v) { throw new Error('innerHTML must never be used'); }
  append(...kids) { for (const k of kids.flat(Infinity)) this.children.push(k); }
  replaceChildren(...kids) { this.children = kids.flat(Infinity); this._text = ''; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = v; }
  click() { if (this.disabled) return; for (const f of this.listeners.click || []) f({ type: 'click' }); }
  type_(v) { this.value = v; for (const f of this.listeners.input || []) f({ type: 'input' }); }
  all(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.children) if (c instanceof Node) c.all(pred, out); return out; }
  button(label) { const r = this.all((n) => n.tag === 'button' && n.textContent === label); assert.ok(r.length, 'no button ' + label + ' in: ' + this.textContent.slice(0, 300)); return r[0]; }
  hasButton(label) { return this.all((n) => n.tag === 'button' && n.textContent === label).length > 0; }
  byClass(cls) { return this.all((n) => n.className.split(' ').includes(cls)); }
}
const doc = { createElement: (tag) => new Node(tag) };

function ask() {
  const clock = new FakeClock();
  const sent = [];
  const copied = [];
  const opened = [];
  const body = new Node('div');
  const ui = UI.createAskView({ doc, body, send: (cmd, extra) => sent.push({ cmd, ...extra }), copy: async (t) => { copied.push(t); }, openConnection: () => opened.push(1), setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  const reply = (cmd, panel, extra) => { const req = sent.filter((s) => s.cmd === cmd).pop(); assert.ok(req, 'no request for ' + cmd); ui.onResult({ type: 'panel_result', request_id: req.request_id, command: cmd, ok: true, panel, ...(extra || {}) }); };
  const fail = (cmd, error, code) => { const req = sent.filter((s) => s.cmd === cmd).pop(); ui.onResult({ type: 'panel_result', request_id: req.request_id, command: cmd, ok: false, error, code }); };
  return { ui, clock, sent, body, reply, fail, copied, opened };
}
const CLIENTS = { clients: [{ id: 'cli_1', name: 'Example Painting Co' }, { id: 'cli_2', name: 'Example Towing' }], default: 'cli_2' };
const DONE = { ask_id: 'ct_1', text: 'search for a', client_id: 'cli_1', client_name: 'Example Painting Co', asked_at: 1.7e9, status: 'succeeded', outcome: 'complete', report: 'I searched Google for 1 phrase.', remaining: [], queries: ['a'], finished_at: 1.7e9 };

test('showing the Ask tab loads the clients and the history, once connected', () => {
  const r = ask();
  r.ui.show();
  assert.equal(r.sent.length, 0, 'nothing is requested while not connected');
  r.ui.sync({ connected: true });
  assert.deepEqual(r.sent.map((s) => s.cmd).sort(), ['ask_clients', 'ask_list']);
  r.reply('ask_clients', CLIENTS);
  r.reply('ask_list', { asks: [] });
  assert.match(r.body.textContent, /Nothing asked yet/);
  const sel = r.body.all((n) => n.tag === 'select')[0];
  assert.equal(sel.children.length, 2);
  assert.equal(sel.value, 'cli_2', 'the owner’s default client is preselected');
});

test('a sentence is sent with the chosen client; what the agent understood is shown', () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.reply('ask_list', { asks: [] });
  r.body.button('Send').click();
  assert.match(r.body.textContent, /Write what you want the agent to do first/);
  const ta = r.body.all((n) => n.tag === 'textarea')[0];
  ta.value = 'search for painters in Alabama';
  r.body.button('Send').click();
  const s = r.sent.filter((x) => x.cmd === 'ask_send')[0];
  assert.deepEqual({ text: s.text, client_id: s.client_id, location: s.location }, { text: 'search for painters in Alabama', client_id: 'cli_2', location: '' });
  assert.equal(r.body.button('Send').disabled, true, 'a double click cannot send twice');
  r.reply('ask_send', { understood: true, ask_id: 'ct_1', queries: ['painters in Alabama'], notes: [] });
  assert.match(r.body.textContent, /I understood: search Google for “painters in Alabama”/);
  assert.equal(ta.value, '', 'the box is cleared after sending');
  assert.equal(r.body.button('Send').disabled, false);
});

test('a sentence with no search is answered, not sent as a run', () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.body.all((n) => n.tag === 'textarea')[0].value = 'go to google and tell me';
  r.body.button('Send').click();
  r.reply('ask_send', { understood: false, reply: 'I could not find what to search for.' });
  assert.match(r.body.textContent, /I could not find what to search for\. For now I can run Google searches/);
});

test('a refusal such as BUSY is shown plainly', () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.body.all((n) => n.tag === 'textarea')[0].value = 'search for x y';
  r.body.button('Send').click();
  r.fail('ask_send', 'A search is still running.', 'BUSY');
  assert.match(r.body.textContent, /A search is still running/);
  assert.equal(r.body.byClass('warn').filter((n) => !n.hidden).length, 1, 'BUSY is shown as a warning, not an error');
});

test('a running search shows progress, polls every 3 s while visible, and stops when hidden', async () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.reply('ask_list', { asks: [{ ...DONE, status: 'running', progress: 'search 2 of 3: best painters', report: null }] });
  assert.match(r.body.textContent, /Working: search 2 of 3: best painters/);
  assert.ok(r.body.hasButton('Cancel'));
  const before = r.sent.length;
  await r.clock.tick(3000);
  assert.equal(r.sent.length, before + 1);
  assert.equal(r.sent[r.sent.length - 1].cmd, 'ask_list');
  r.ui.hide();
  r.reply('ask_list', { asks: [{ ...DONE, status: 'running', progress: '', report: null }] });
  const n = r.sent.length;
  await r.clock.tick(9000);
  assert.equal(r.sent.length, n, 'no polling while the Ask tab is not shown');
  r.body.button('Cancel').click();
  assert.equal(r.sent.filter((x) => x.cmd === 'ask_cancel')[0].ask_id, 'ct_1');
});

test('the answer is shown as text, can be copied, and a stopped run offers Try again for exactly what is left', async () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.reply('ask_list', { asks: [{ ...DONE, outcome: 'blocked', report: 'I ran 1 of 3 searches.<img src=x onerror=alert(1)>', remaining: ['two', 'three'], location: 'Alabama,United States' }] });
  assert.match(r.body.textContent, /Stopped: Google asked for a human check/);
  assert.match(r.body.textContent, /<img src=x onerror=alert\(1\)>/, 'markup is displayed as text, never interpreted');
  r.body.button('Copy answer').click();
  await new Promise((res) => setImmediate(res));
  assert.equal(r.copied[0], 'I ran 1 of 3 searches.<img src=x onerror=alert(1)>');
  r.body.button('Try again (2 left)').click();
  const s = r.sent.filter((x) => x.cmd === 'ask_send').pop();
  assert.deepEqual({ queries: s.queries, client_id: s.client_id, location: s.location }, { queries: ['two', 'three'], client_id: 'cli_1', location: 'Alabama,United States' });
});

test('a failed run explains what to do when Chrome is not connected', () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.reply('ask_list', { asks: [{ ...DONE, status: 'failed', error_code: 'EXTENSION_NOT_CONNECTED', error: 'x', report: null }] });
  assert.match(r.body.textContent, /Chrome is not connected to the agent/);
});

test('losing the connection stops polling and reconnecting reloads', async () => {
  const r = ask();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('ask_clients', CLIENTS);
  r.reply('ask_list', { asks: [{ ...DONE, status: 'running', report: null }] });
  r.ui.sync({ connected: false });
  const n = r.sent.length;
  await r.clock.tick(9000);
  assert.equal(r.sent.length, n);
  r.ui.sync({ connected: true });
  assert.ok(r.sent.length > n, 'reconnecting asks again');
});

// ------------------------------------------------------------------------------------------------- inbox
function inbox(tab) {
  const clock = new FakeClock();
  const sent = [];
  const counts = [];
  const body = new Node('div');
  const state = { tab: tab === undefined ? { tab_id: 7, supported: true, title: 'Verify', url: 'https://example.test/verify' } : tab };
  const ui = UI.createInboxView({ doc, body, send: (cmd, extra) => sent.push({ cmd, ...extra }), currentTab: () => state.tab, onCount: (n) => counts.push(n), setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  const reply = (cmd, panel, extra) => { const req = sent.filter((s) => s.cmd === cmd).pop(); assert.ok(req, 'no request for ' + cmd); ui.onResult({ type: 'panel_result', request_id: req.request_id, command: cmd, ok: true, panel, ...(extra || {}) }); };
  const fail = (cmd, error, code) => { const req = sent.filter((s) => s.cmd === cmd).pop(); ui.onResult({ type: 'panel_result', request_id: req.request_id, command: cmd, ok: false, error, code }); };
  return { ui, sent, body, counts, reply, fail, state };
}
const REQ = { or_id: 'or_1', status: 'open', title: 'Verify the account', steps: ['Open the verify page', 'Click Verify', 'Read the message'], question: 'What does the page say?', choices: ['It worked', 'I see an error'], want_page: true,
  client_name: 'Example Painting Co', related: 'Example loop', created_at: 1.7e9, attachments: [] };

test('an open request shows its steps, the question and quick answers; the count is reported', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  r.ui.show();
  assert.equal(r.sent[0].cmd, 'inbox_list');
  r.reply('inbox_list', { requests: [REQ], open: 1 });
  const t = r.body.textContent;
  for (const needle of ['Verify the account', 'Open the verify page', 'Click Verify', 'Read the message', 'What does the page say?', 'It worked', 'I see an error', 'About: Example Painting Co', 'Related: Example loop', 'do not type passwords']) assert.ok(t.includes(needle) || r.body.all((n) => n.placeholder.includes('passwords')).length, needle);
  assert.deepEqual(r.counts, [1]);
  assert.equal(r.body.byClass('inbox-steps')[0].children.length, 3);
});

test('a quick answer plus text is sent as one reply, and the request leaves the list', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('inbox_list', { requests: [REQ], open: 1 });
  r.body.button('Send answer').click();
  assert.match(r.body.textContent, /Write what you see, press a quick answer/);
  assert.equal(r.sent.filter((s) => s.cmd === 'inbox_reply').length, 0, 'an empty reply is not sent');
  r.body.button('I see an error').click();
  const ta = r.body.all((n) => n.tag === 'textarea')[0];
  ta.type_('The page says: code expired');
  r.body.button('Send answer').click();
  const s = r.sent.filter((x) => x.cmd === 'inbox_reply')[0];
  assert.deepEqual({ or_id: s.or_id, text: s.text, choice: s.choice, attachments: s.attachments }, { or_id: 'or_1', text: 'The page says: code expired', choice: 'I see an error', attachments: [] });
  r.reply('inbox_reply', { request: { ...REQ, status: 'answered' }, open: 0 });
  assert.deepEqual(r.counts.slice(-1), [0]);
  r.reply('inbox_list', { requests: [{ ...REQ, status: 'answered', reply_text: 'The page says: code expired', reply_choice: 'I see an error', answered_at: 1.7e9 }], open: 0 });
  assert.match(r.body.textContent, /Nothing waiting for you/);
  assert.match(r.body.textContent, /Earlier \(1\)/);
  assert.match(r.body.textContent, /I see an error — The page says: code expired/);
});

test('attaching the tab sends the tab id the owner is on, shows what was attached, and the reply carries it', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('inbox_list', { requests: [REQ], open: 1 });
  r.body.button('Attach this tab’s text and a screenshot').click();
  const a = r.sent.filter((x) => x.cmd === 'inbox_attach')[0];
  assert.deepEqual({ or_id: a.or_id, tab_id: a.tab_id }, { or_id: 'or_1', tab_id: 7 });
  assert.match(r.body.textContent, /Reading this tab/);
  r.reply('inbox_attach', { attachment: { att_id: 'att_9', url: 'https://example.test/verify', title: 'Verify your account', chars: 120, screenshot: true } });
  assert.match(r.body.textContent, /Attached: Verify your account · 120 characters of text \+ a screenshot/);
  r.body.button('Send answer').click();
  assert.deepEqual(r.sent.filter((x) => x.cmd === 'inbox_reply')[0].attachments, ['att_9']);
});

test('an attachment can be removed before sending, and an unreadable tab is explained', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('inbox_list', { requests: [REQ], open: 1 });
  r.body.button('Attach this tab’s text and a screenshot').click();
  r.reply('inbox_attach', { attachment: { att_id: 'att_9', url: 'u', title: 't', chars: 5, screenshot: false } });
  r.body.button('Remove').click();
  assert.ok(r.body.hasButton('Attach this tab’s text and a screenshot'));
  r.body.button('Attach this tab’s text and a screenshot').click();
  r.fail('inbox_attach', 'This tab cannot be read by the extension (Chrome’s own pages such as chrome:// block it).', 'TAB_RESTRICTED');
  assert.match(r.body.textContent, /cannot be read by the extension/);
});

test('on a page the extension cannot read the attach button is disabled with the reason', () => {
  const r = inbox({ tab_id: 3, supported: false, title: 'Extensions', url: 'chrome://extensions' });
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('inbox_list', { requests: [REQ], open: 1 });
  assert.equal(r.body.button('Attach this tab’s text and a screenshot').disabled, true);
  assert.match(r.body.textContent, /Chrome’s own pages cannot be read/);
});

test('Skip dismisses a request; a refused reply shows the reason and keeps what was typed', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('inbox_list', { requests: [REQ], open: 1 });
  r.body.all((n) => n.tag === 'textarea')[0].type_('my answer');
  r.body.button('Send answer').click();
  r.fail('inbox_reply', 'that request is already answered', 'NOT_OPEN');
  assert.match(r.body.textContent, /already answered/);
  assert.equal(r.body.all((n) => n.tag === 'textarea')[0].value, 'my answer');
  r.body.button('Skip').click();
  assert.equal(r.sent.filter((x) => x.cmd === 'inbox_dismiss')[0].or_id, 'or_1');
});

test('titles and steps written by the AI are shown as text, never as markup', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  r.ui.show();
  r.reply('inbox_list', { requests: [{ ...REQ, title: '<b onmouseover=alert(1)>Hi</b>', steps: ['<script>alert(1)</script>'] }], open: 1 });
  assert.match(r.body.textContent, /<b onmouseover=alert\(1\)>Hi<\/b>/);
  assert.match(r.body.textContent, /<script>alert\(1\)<\/script>/);
});

test('a new request announced by the agent refreshes the open list; nothing is requested while hidden', () => {
  const r = inbox();
  r.ui.sync({ connected: true });
  const n = r.sent.length;
  r.ui.setOpenCount(2);
  assert.equal(r.sent.length, n, 'hidden: the count is only a number, the list is not fetched');
  r.ui.show();
  const m = r.sent.length;
  r.ui.setOpenCount(3);
  assert.equal(r.sent.length, m + 1, 'visible: a changed count refreshes the list');
});

// ------------------------------------------------------------------------------------------------- not connected
test('when the panel is not connected it says so, offers the connection settings, and does not tell the owner to add a client', () => {
  const r = ask();
  r.ui.show();
  assert.match(r.body.textContent, /Not connected to the agent, so your clients and answers cannot load/);
  assert.equal(r.body.byClass('warn')[0].hidden, false);
  assert.equal(r.body.button('Send').disabled, true, 'nothing can be sent while offline');
  assert.match(r.body.textContent, /\(not connected\)/);
  r.body.all((n) => n.tag === 'textarea')[0].value = 'search for painters in Alabama';
  r.ui.go();
  assert.match(r.body.textContent, /Not connected to the agent\. Press “Open connection settings”/);
  assert.doesNotMatch(r.body.textContent, /Add a client|no client to search/);
  assert.equal(r.sent.length, 0);
  r.body.button('Open connection settings').click();
  assert.equal(r.opened.length, 1);
});

test('connecting hides the notice, shows that clients are loading, then lists them; an empty client list is a different message', () => {
  const r = ask();
  r.ui.show();
  r.ui.sync({ connected: true });
  assert.equal(r.body.byClass('warn')[0].hidden, true);
  assert.equal(r.body.button('Send').disabled, false);
  assert.match(r.body.textContent, /Loading your clients/);
  r.body.all((n) => n.tag === 'textarea')[0].value = 'search for painters in Alabama';
  r.ui.go();
  assert.match(r.body.textContent, /still loading/);
  r.reply('ask_clients', { clients: [], default: null });
  assert.match(r.body.textContent, /No clients yet: add one in the Knowledge Hub/);
  r.ui.go();
  assert.match(r.body.textContent, /no client to search for yet: add one in the Knowledge Hub/);
  r.ui.sync({ connected: false });
  assert.equal(r.body.byClass('warn')[0].hidden, false, 'losing the connection shows the notice again');
});

test('the inbox also offers the connection settings when offline', () => {
  const opened = [];
  const body = new Node('div');
  const ui = UI.createInboxView({ doc, body, send: () => {}, openConnection: () => opened.push(1), currentTab: () => null, setTimeout() {}, clearTimeout() {} });
  ui.show();
  assert.match(body.textContent, /Not connected to the agent, so requests cannot load/);
  assert.doesNotMatch(body.textContent, /Nothing waiting for you/);
  body.button('Open connection settings').click();
  assert.equal(opened.length, 1);
});
