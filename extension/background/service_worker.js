// OCTG Browser Agent — MV3 service worker entry point.
// All chrome.* event listeners are registered synchronously here so Chrome can wake the worker for them.
import { refreshFileAccess } from './common.js';
import { bridge, bizListeners, panelListeners, connect, configure, ensureConnected, isLocalUrl, loadConfig, sendUi, wireEvents, DEFAULT_URL } from './bridge.js';
import { currentTab, registerListeners, subscribe, tabSupport } from './tabs.js';
import { dropSnapshot, restoreSnapshots } from './page.js';

registerListeners();
wireEvents();
chrome.tabs.onRemoved.addListener((tabId) => dropSnapshot(tabId));
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
chrome.alarms.create('octg-keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'octg-keepalive') ensureConnected(); });
chrome.runtime.onInstalled.addListener(() => refreshFileAccess());
// Config written from elsewhere (another extension context, test harness) takes effect immediately.
chrome.storage.onChanged.addListener((changes, area) => {
  const nv = area === 'local' && changes.octg_config && changes.octg_config.newValue;
  if (nv && JSON.stringify(nv) !== JSON.stringify(bridge.config)) configure(nv);
});

// ---------------------------------------------------------------- side panel plumbing
const ports = new Set();
let pushTimer = null;

async function panelState() {
  const [cur, all] = await Promise.all([currentTab().catch(() => null), chrome.tabs.query({}).catch(() => [])]);
  let website = null;
  if (cur && cur.url) { try { website = new URL(cur.url).hostname || cur.url; } catch (_) { website = cur.url; } }
  const cls = cur ? tabSupport(cur) : null;
  return {
    connection: { state: bridge.state, error: bridge.lastError, url: bridge.config.url, has_token: !!bridge.config.token, enabled: bridge.config.enabled },
    agent: bridge.agent,
    task: bridge.task,
    providers: bridge.providers,
    inbox: bridge.inbox,
    toast: bridge.toast,
    browser: {
      tab_count: all.length,
      current_website: website,
      tab: cur ? { tab_id: cur.id, title: cur.title, url: cur.url, supported: cls.supported, restricted_reason: cls.reason } : null,
    },
    log: bridge.log.slice(-80),
  };
}

function schedulePush() {
  if (!ports.size || pushTimer) return;
  pushTimer = setTimeout(async () => {
    pushTimer = null;
    const state = await panelState();
    for (const p of ports) { try { p.postMessage({ type: 'state', state }); } catch (_) { ports.delete(p); } }
  }, 120);
}

bridge.listeners.add(schedulePush);
bizListeners.add((m) => { for (const p of ports) { try { p.postMessage(m); } catch (_) { ports.delete(p); } } }); // relayed, not stored
panelListeners.add((m) => { for (const p of ports) { try { p.postMessage(m); } catch (_) { ports.delete(p); } } }); // Ask / Inbox results: relayed once, not stored
subscribe(schedulePush);

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'octg-panel' || port.sender?.id !== chrome.runtime.id || !(port.sender.url || '').startsWith(`chrome-extension://${chrome.runtime.id}/`)) return; // our own pages only
  ports.add(port);
  port.onDisconnect.addListener(() => ports.delete(port));
  port.onMessage.addListener(async (msg) => {
    if (msg.type === 'ui') {
      const { type, ...rest } = msg;
      if (!sendUi(rest.command, rest)) bridge.toast = { ok: false, text: 'Not connected to the local agent. Open Connection and press Connect.', ts: Date.now() };
    } else if (msg.type === 'configure') {
      const url = (msg.url || '').trim() || DEFAULT_URL;
      if (!isLocalUrl(url)) { port.postMessage({ type: 'toast', ok: false, text: 'The agent URL must be ws://127.0.0.1, ws://localhost or ws://[::1].' }); return; }
      await configure({ url, token: msg.token !== undefined ? msg.token.trim() : undefined, enabled: msg.enabled });
    }
    schedulePush();
  });
  schedulePush();
});

// ---------------------------------------------------------------- start-up
(async () => {
  await refreshFileAccess();
  await restoreSnapshots();
  await loadConfig();
  connect();
  schedulePush();
})();
