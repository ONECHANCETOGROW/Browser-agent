// WebSocket client to the local agent (127.0.0.1 only). The extension dials out; the agent never dials in.
import { PROTOCOL, VERSION } from './common.js';
import { runAction } from './actions.js';
import { listTabs, subscribe } from './tabs.js';

export const DEFAULT_URL = 'ws://127.0.0.1:8765/ext';
const CONFIG_KEY = 'octg_config';
const PING_MS = 20000;
const LOG_MAX = 200;
const LOG_EVENTS = new Set(['tab_created', 'tab_removed', 'navigation_completed', 'navigation_error', 'download_created', 'download_changed']);

// A random id for THIS running copy of the extension (one per Chrome profile / service-worker start). The agent logs it, so two windows fighting over the connection can be told apart.
const INSTANCE = Math.random().toString(36).slice(2, 10);

export const bridge = {
  state: 'idle', // idle | connecting | connected | disconnected | auth_failed
  replaced: false, // another Chrome window/profile connected to the agent and took the single connection: this copy stops retrying until the owner presses Connect here
  lastError: null,
  config: { url: DEFAULT_URL, token: '', enabled: false },
  agent: { task: null, current_action: null, message: null },
  task: null, // latest task_update pushed by the agent (summary only: no page text, no field values)
  providers: null, // {policy, policies, providers:[{name, model, available, local}]} - never contains keys
  hardLimitMs: 90000, // no single command may hold the line longer than this (plus what it asked for): a hung page action must never block every command behind it
  runner: runAction,  // replaceable in tests
  inbox: 0, // how many requests the AI is waiting on you for (a number only: the requests themselves are fetched when the panel is open and never stored here)
  toast: null,
  log: [],
  listeners: new Set(),
};

let ws = null;
let pingTimer = null;
let retryTimer = null;
let attempt = 0;
let queue = Promise.resolve();

const notify = () => { for (const fn of bridge.listeners) { try { fn(); } catch (_) { /* panel may be gone */ } } };
export function pushLog(kind, text, ok = true) {
  bridge.log.push({ ts: Date.now(), kind, text, ok });
  if (bridge.log.length > LOG_MAX) bridge.log.shift();
  notify();
}
function setState(s, err) { bridge.state = s; if (err !== undefined) bridge.lastError = err; notify(); }

export function isLocalUrl(raw) {
  try { const u = new URL(raw); return u.protocol === 'ws:' && ['127.0.0.1', 'localhost', '[::1]'].includes(u.hostname); } catch (_) { return false; }
}

export async function loadConfig() {
  const got = (await chrome.storage.local.get(CONFIG_KEY))[CONFIG_KEY];
  if (got) bridge.config = { ...bridge.config, ...got };
}
async function saveConfig() { await chrome.storage.local.set({ [CONFIG_KEY]: bridge.config }); }

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return true; }
  return false;
}

function stopTimers() { clearInterval(pingTimer); pingTimer = null; clearTimeout(retryTimer); retryTimer = null; }

function scheduleReconnect() {
  clearTimeout(retryTimer);
  if (!bridge.config.enabled || !bridge.config.token || bridge.state === 'auth_failed' || bridge.replaced) return;
  const delay = Math.min(1000 * 2 ** attempt, 10000);
  attempt++;
  retryTimer = setTimeout(connect, delay);
}

export function connect() {
  const { url, token, enabled } = bridge.config;
  if (!enabled || !token) { setState('idle'); return; }
  if (!isLocalUrl(url)) { setState('disconnected', 'Refusing to connect: the agent URL must be ws://127.0.0.1, ws://localhost or ws://[::1].'); return; }
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  clearTimeout(retryTimer);
  setState('connecting');
  try { ws = new WebSocket(url); } catch (e) { setState('disconnected', String(e.message || e)); scheduleReconnect(); return; }
  const mine = ws;
  mine.onopen = () => send({ type: 'hello', token, extension_id: chrome.runtime.id, version: VERSION, protocol: PROTOCOL, user_agent: navigator.userAgent, instance: INSTANCE });
  mine.onmessage = (ev) => onMessage(ev.data);
  mine.onerror = () => { /* onclose follows with the details */ };
  mine.onclose = (ev) => {
    if (ws !== mine) return; // superseded by configure()/reconnect; its state was already handled
    ws = null;
    clearInterval(pingTimer);
    if (ev.code === 4000) {
      // "replaced by a newer connection": another Chrome window/profile with this extension connected. Reconnecting at once would take it back and start a tug of war (a new connection every second),
      // so this copy stays quiet until the owner presses Connect here.
      bridge.replaced = true;
      setState('disconnected', 'Another Chrome window or profile connected to the agent and took over. Press Connect here if you want to use THIS one instead.');
      pushLog('connection', 'Taken over by another Chrome window/profile (press Connect to take it back)', false);
      return;
    }
    if (bridge.state !== 'auth_failed') {
      setState('disconnected', ev.code === 1006 ? 'Agent not reachable (is it running?)' : `Closed (${ev.code}${ev.reason ? ': ' + ev.reason : ''})`);
      pushLog('connection', 'Disconnected from agent', false);
    }
    scheduleReconnect();
  };
}

async function onMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch (_) { return; }
  if (msg.type === 'hello_ack') {
    attempt = 0;
    bridge.replaced = false;
    queue = Promise.resolve(); // a new connection starts with an empty line: commands of the old connection are dead, and one that never finished must not block this one
    setState('connected', null);
    pushLog('connection', 'Connected to local agent');
    clearInterval(pingTimer);
    pingTimer = setInterval(() => send({ type: 'ping', ts: Date.now() }), PING_MS);
    send({ type: 'tabs_snapshot', tabs: await listTabs() });
  } else if (msg.type === 'hello_error') {
    setState('auth_failed', `Agent rejected the connection: ${msg.message || msg.code}`);
    pushLog('connection', `Rejected by agent: ${msg.message || msg.code}`, false);
  } else if (msg.type === 'status') {
    bridge.agent = { task: msg.task ?? null, current_action: msg.current_action ?? null, message: msg.message ?? null };
    notify();
  } else if (msg.type === 'task_update') {
    bridge.task = msg.task;
    notify();
  } else if (msg.type === 'providers') {
    bridge.providers = msg;
    notify();
  } else if (msg.type === 'inbox') {
    // The agent says how many AI requests are waiting for you: show it on the toolbar icon so you do not have to open the dashboard to find out.
    bridge.inbox = Math.max(0, Number(msg.open) || 0);
    try { chrome.action.setBadgeText({ text: bridge.inbox ? String(Math.min(bridge.inbox, 99)) : '' }); chrome.action.setBadgeBackgroundColor({ color: '#b42318' }); } catch (_) { /* badge is a convenience */ }
    notify();
  } else if (msg.type === 'ui_result' && typeof msg.command === 'string' && /^(ask|inbox)_/.test(msg.command)) {
    // Ask and Inbox views: handed once to the side panel, never kept in `bridge` state, storage or the shared log.
    const out = { type: 'panel_result', request_id: msg.request_id, command: msg.command, ok: !!msg.ok, error: msg.error, code: msg.code, panel: msg.panel };
    for (const fn of panelListeners) { try { fn(out); } catch (_) { /* a closed panel */ } }
  } else if (msg.type === 'ui_result' && typeof msg.command === 'string' && msg.command.startsWith('biz_')) {
    // Business Brain views and decisions: handed once to the side panel, never kept in `bridge` state, storage or the shared log.
    const out = { type: 'biz_result', request_id: msg.request_id, command: msg.command, ok: !!msg.ok, error: msg.error, code: msg.code, biz: msg.biz };
    for (const fn of bizListeners) { try { fn(out); } catch (_) { /* a closed panel */ } }
  } else if (msg.type === 'ui_result') {
    if (!msg.ok) { bridge.toast = { ok: false, text: msg.error || 'The agent refused the command.', ts: Date.now() }; }
    else if (msg.task) bridge.task = msg.task;
    notify();
  } else if (msg.type === 'ping') {
    send({ type: 'pong', ts: msg.ts });
  } else if (msg.type === undefined && msg.action) {
    queue = queue.then(() => handleCommand(msg)).catch(() => {});
  }
}

export function commandLimitMs(cmd) {
  const p = (cmd && cmd.parameters) || {};
  const asked = Math.max(Number(p.timeout_ms) || 0, Number(p.ms) || 0);
  return Math.min(300000, Math.max(bridge.hardLimitMs, asked ? asked + 20000 : 0));
}

// Commands run one after another. If one never finishes (a page that never loads, a dialog), everything behind it, even a ping, would wait forever and reconnecting would not help, because
// the line lives here. So every command gets a hard limit: when it is exceeded the command is answered with a timeout error and abandoned, and the next one runs.
export async function runWithLimit(cmd, run = bridge.runner) {
  const limit = commandLimitMs(cmd);
  const t0 = Date.now();
  let timer;
  const hung = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ request_id: cmd.request_id, action: cmd.action, success: false, result: null,
      error: { code: 'ACTION_TIMEOUT', message: `${cmd.action} did not finish inside the extension within ${Math.round(limit / 1000)}s and was abandoned so the next commands are not blocked.`, details: { hung_in_extension: true } },
      verification: null, duration_ms: Date.now() - t0 }), limit);
  });
  try { return await Promise.race([run(cmd), hung]); } finally { clearTimeout(timer); }
}

async function handleCommand(cmd) {
  pushLog('action', `▶ ${cmd.action}`);
  const res = await runWithLimit(cmd);
  const ok = send(res);
  pushLog('action', `${res.success ? '✔' : '✖'} ${cmd.action}${res.error ? ' — ' + res.error.code : ''} (${res.duration_ms ?? '?'} ms)${ok ? '' : ' [reply lost: not connected]'}`, res.success);
}

export async function configure({ url, token, enabled }) {
  bridge.replaced = false; // pressing Connect / Disconnect is the owner's decision about THIS copy
  if (url !== undefined) bridge.config.url = url || DEFAULT_URL;
  if (token !== undefined) bridge.config.token = token;
  if (enabled !== undefined) bridge.config.enabled = enabled;
  await saveConfig();
  if (bridge.state === 'auth_failed') bridge.state = 'idle';
  disconnectSocket();
  attempt = 0;
  if (bridge.config.enabled) connect(); else setState(bridge.config.token ? 'disconnected' : 'idle', 'Disconnected by user');
}

function disconnectSocket() {
  stopTimers();
  if (ws) { const w = ws; ws = null; try { w.close(1000, 'client closing'); } catch (_) { /* already closed */ } }
}

export function ensureConnected() { if (bridge.config.enabled && bridge.config.token && !ws && bridge.state !== 'auth_failed' && !bridge.replaced) connect(); }

/** Side-panel -> agent task commands (start_task, approve, deny, pause, resume, stop). Authenticated by the same pairing as everything else. */
export function sendUi(command, payload = {}) {
  return send({ type: 'ui', command, request_id: 'ui_' + Date.now().toString(36), ...payload });
}

export const bizListeners = new Set();
export const panelListeners = new Set();

export function wireEvents() {
  subscribe((entry) => {
    send(entry);
    if (LOG_EVENTS.has(entry.event)) pushLog('event', entry.event + (entry.data && entry.data.url ? ' ' + entry.data.url : ''));
  });
}
