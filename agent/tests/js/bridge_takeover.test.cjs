'use strict';
// extension/background/bridge.js: when another Chrome window/profile takes the agent's single connection (close code 4000) this copy must stop reconnecting by itself
// (otherwise two windows take it from each other every second), say why, and only come back when the owner presses Connect. Other closes still reconnect.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const sockets = [];
class FakeWebSocket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
  send(d) { this.sent.push(JSON.parse(d)); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen && this.onopen(); }
  receive(o) { return this.onmessage && this.onmessage({ data: JSON.stringify(o) }); }
  serverClose(code, reason) { this.readyState = 3; this.onclose && this.onclose({ code, reason: reason || '' }); }
}
const noop = { addListener() {} };
globalThis.WebSocket = FakeWebSocket;
globalThis.chrome = { runtime: { id: 'testid', getURL: (p) => p }, storage: { local: { get: async () => ({}), set: async () => {} }, onChanged: noop },
  tabs: { onCreated: noop, onUpdated: noop, onRemoved: noop, onActivated: noop, query: async () => [] }, webNavigation: { onCompleted: noop, onErrorOccurred: noop, onCommitted: noop, onBeforeNavigate: noop },
  downloads: { onCreated: noop, onChanged: noop }, scripting: {}, action: { setBadgeText() {}, setBadgeBackgroundColor() {} }, windows: { onFocusChanged: noop }, alarms: { create() {}, onAlarm: noop } };

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const load = () => import(pathToFileURL(path.join(__dirname, '..', '..', '..', 'extension', 'background', 'bridge.js')).href);
const CFG = { url: 'ws://127.0.0.1:8765/ext', token: 'secret-token', enabled: true };

async function connected(b) {
  await b.configure(CFG);
  const s = sockets[sockets.length - 1];
  s.open();
  await s.receive({ type: 'hello_ack' });
  await wait(20);
  return s;
}

test('the hello carries a per-copy id so the agent can tell two windows apart', async () => {
  const b = await load();
  const s = await connected(b);
  const hello = s.sent.find((m) => m.type === 'hello');
  assert.match(hello.instance, /^[a-z0-9]{4,10}$/);
  assert.equal(b.bridge.state, 'connected');
  await b.configure({ enabled: false });
});

test('being replaced (code 4000) stops this copy from reconnecting and says why', async () => {
  const b = await load();
  const s = await connected(b);
  const before = sockets.length;
  s.serverClose(4000, 'replaced by a newer connection');
  assert.equal(b.bridge.replaced, true);
  assert.equal(b.bridge.state, 'disconnected');
  assert.match(b.bridge.lastError, /Another Chrome window or profile .* took over/);
  await wait(1400);
  assert.equal(sockets.length, before, 'no reconnect attempt: the tug of war never starts');
  b.ensureConnected();     // the keep-alive alarm must not undo it either
  assert.equal(sockets.length, before);
  await b.configure({ enabled: false });
});

test('pressing Connect on the replaced copy takes the connection back', async () => {
  const b = await load();
  const s = await connected(b);
  s.serverClose(4000, 'replaced by a newer connection');
  assert.equal(b.bridge.replaced, true);
  const n = sockets.length;
  await b.configure(CFG);                      // what the Connect button does
  assert.equal(b.bridge.replaced, false);
  assert.equal(sockets.length, n + 1);
  const s2 = sockets[sockets.length - 1];
  s2.open();
  await s2.receive({ type: 'hello_ack' });
  await wait(20);
  assert.equal(b.bridge.state, 'connected');
  await b.configure({ enabled: false });
});

test('an ordinary lost connection still reconnects by itself', async () => {
  const b = await load();
  const s = await connected(b);
  const n = sockets.length;
  s.serverClose(1006);
  assert.equal(b.bridge.replaced, false);
  await wait(1300);
  assert.equal(sockets.length, n + 1, 'it dialled again after the first back-off');
  await b.configure({ enabled: false });
});

// ---- a hung command must not block the commands behind it
test('a command that never finishes is answered with a timeout and the next command still runs', async () => {
  const b = await load();
  const s = await connected(b);
  b.bridge.hardLimitMs = 60;                                  // short, for the test
  const seen = [];
  b.bridge.runner = (cmd) => { seen.push(cmd.action); return cmd.action === 'new_tab' ? new Promise(() => {}) : Promise.resolve({ request_id: cmd.request_id, action: cmd.action, success: true, result: { ok: 1 }, error: null, verification: null, duration_ms: 1 }); };
  await s.receive({ request_id: 'r1', action: 'new_tab', parameters: { url: 'https://example.com/' } });
  await s.receive({ request_id: 'r2', action: 'ping', parameters: {} });
  await wait(250);
  const replies = s.sent.filter((m) => m.request_id);
  assert.deepEqual(seen, ['new_tab', 'ping'], 'the second command ran although the first never finished');
  const r1 = replies.find((m) => m.request_id === 'r1'), r2 = replies.find((m) => m.request_id === 'r2');
  assert.equal(r1.success, false);
  assert.equal(r1.error.code, 'ACTION_TIMEOUT');
  assert.equal(r1.error.details.hung_in_extension, true);
  assert.equal(r2.success, true);
  await b.configure({ enabled: false });
});

test('the limit grows with what the command itself asked for, and a normal command is untouched', async () => {
  const b = await load();
  b.bridge.hardLimitMs = 90000;
  assert.equal(b.commandLimitMs({ parameters: {} }), 90000);
  assert.equal(b.commandLimitMs({ parameters: { timeout_ms: 30000 } }), 90000);
  assert.equal(b.commandLimitMs({ parameters: { timeout_ms: 120000 } }), 140000);
  assert.equal(b.commandLimitMs({ parameters: { timeout_ms: 900000 } }), 300000, 'never more than five minutes');
  const ok = await b.runWithLimit({ request_id: 'x', action: 'ping', parameters: {} }, async () => ({ success: true, result: { fine: true } }));
  assert.deepEqual(ok, { success: true, result: { fine: true } });
});

test('a new connection starts with an empty line even if the old one was stuck', async () => {
  const b = await load();
  const s = await connected(b);
  b.bridge.hardLimitMs = 3000;                                  // long enough that the stuck command is still holding the line when the connection is renewed
  b.bridge.runner = (cmd) => cmd.action === 'new_tab' ? new Promise(() => {}) : Promise.resolve({ request_id: cmd.request_id, action: cmd.action, success: true, result: {}, error: null, verification: null });
  await s.receive({ request_id: 'a1', action: 'new_tab', parameters: {} });
  s.serverClose(1006);
  await wait(1300);
  const s2 = sockets[sockets.length - 1];
  s2.open();
  await s2.receive({ type: 'hello_ack' });
  await wait(20);
  await s2.receive({ request_id: 'a2', action: 'ping', parameters: {} });
  await wait(100);
  assert.equal(s2.sent.some((m) => m.request_id === 'a2' && m.success === true), true, 'the command after reconnecting ran at once');
  await b.configure({ enabled: false });
});
