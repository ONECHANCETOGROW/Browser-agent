// Tab/window/navigation/download awareness: event ring buffer, navigation tracker, tab resolution.
import { ActionError, classifyUrl, sleep } from './common.js';

// ---------------------------------------------------------------- event bus
const RING_MAX = 500;
const ring = [];
let seq = 0;
const subscribers = new Set();

export function emit(event, data) {
  const entry = { type: 'event', event, seq: ++seq, ts: Date.now(), data };
  ring.push(entry);
  if (ring.length > RING_MAX) ring.shift();
  for (const fn of subscribers) { try { fn(entry); } catch (_) { /* a subscriber must not break event delivery */ } }
  return entry;
}
export const subscribe = (fn) => { subscribers.add(fn); return () => subscribers.delete(fn); };
export const currentSeq = () => seq;
export const eventsSince = (since) => ring.filter((e) => e.seq > since);

const nav = new Map(); // tabId -> {seq, state, url, error, ts}

// ---------------------------------------------------------------- tab model
/** Support classification = URL rules + "Chrome failed to load this page" (error pages keep the failing URL in tab.url). */
export function tabSupport(t) {
  const cls = classifyUrl(t.url || '');
  if (!cls.supported) return cls;
  const n = nav.get(t.id);
  if (n && n.state === 'error' && n.url === t.url) return { supported: false, reason: `Chrome could not load this page (${n.error || 'network error'}).` };
  return cls;
}

export function normalizeTab(t) {
  if (!t) return null;
  const url = t.url || t.pendingUrl || '';
  const cls = tabSupport(t);
  return {
    tab_id: t.id, window_id: t.windowId, index: t.index, active: !!t.active, title: t.title || '', url,
    status: t.status || 'unknown', pinned: !!t.pinned, audible: !!t.audible, incognito: !!t.incognito,
    opener_tab_id: t.openerTabId ?? null, supported: cls.supported, ...(cls.supported ? {} : { restricted_reason: cls.reason }),
  };
}

export async function listTabs() {
  const tabs = await chrome.tabs.query({});
  return tabs.map(normalizeTab);
}

export async function focusedWindowId() {
  try { const w = await chrome.windows.getLastFocused({ windowTypes: ['normal', 'popup'] }); return w.id; } catch (_) { return null; }
}

/** The "current tab": active tab of the last focused browser window. */
export async function currentTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  tabs = tabs.filter((t) => !(t.url || '').startsWith('chrome-extension://' + chrome.runtime.id + '/')); // ignore our own panel page if opened as a tab
  if (!tabs.length) tabs = (await chrome.tabs.query({ active: true })).filter((t) => t.windowId !== undefined);
  return tabs[0] || null;
}

/** Resolve the tab an action applies to: explicit tab_id, else the current tab. */
export async function resolveTab(params) {
  if (params && params.tab_id !== undefined && params.tab_id !== null) {
    try { return await chrome.tabs.get(Number(params.tab_id)); }
    catch (_) { throw new ActionError('TAB_NOT_FOUND', `No tab with id ${params.tab_id}.`, { tab_id: params.tab_id }); }
  }
  const t = await currentTab();
  if (!t) throw new ActionError('NO_ACTIVE_TAB', 'There is no active tab (no normal Chrome window is open).');
  return t;
}

export async function activateTab(tabId) {
  const t = await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(t.windowId, { focused: true });
  return chrome.tabs.get(tabId);
}

// ---------------------------------------------------------------- navigation tracker (main frame only)
export const navOf = (tabId) => nav.get(tabId) || { seq: 0, state: 'idle', url: null, error: null, ts: 0 };
function navSet(tabId, state, extra) {
  const cur = navOf(tabId);
  nav.set(tabId, { ...cur, seq: cur.seq + 1, state, ts: Date.now(), error: null, ...extra });
}

/**
 * Wait for a main-frame navigation that began after `sinceSeq` to finish.
 * Returns {started, state: 'done'|'error'|'timeout'|'idle', error, final_url, load_ms}.
 */
export async function waitNavigation(tabId, sinceSeq, { timeout = 30000, startGrace = 1500 } = {}) {
  const t0 = Date.now();
  let stableSince = 0;
  for (;;) {
    const n = navOf(tabId);
    const started = n.seq > sinceSeq;
    let tab = null;
    try { tab = await chrome.tabs.get(tabId); } catch (_) { return { started, state: 'error', error: 'tab_closed', final_url: null, load_ms: Date.now() - t0 }; }
    if (started && n.state === 'error') return { started, state: 'error', error: n.error, final_url: tab.url, load_ms: Date.now() - t0 };
    if (started && n.state === 'done') return { started, state: 'done', final_url: tab.url, load_ms: Date.now() - t0, ...(n.spa ? { same_document: true } : {}) };
    if (started && tab.status === 'complete') { // listener missed (service worker restarted) or bfcache restore
      stableSince = stableSince || Date.now();
      if (Date.now() - stableSince > 300) return { started, state: 'done', final_url: tab.url, load_ms: Date.now() - t0 };
    } else stableSince = 0;
    const elapsed = Date.now() - t0;
    if (!started && elapsed > startGrace) return { started: false, state: 'idle', final_url: tab.url, load_ms: elapsed };
    if (elapsed > timeout) return { started, state: 'timeout', final_url: tab.url, load_ms: elapsed };
    await sleep(60);
  }
}

// ---------------------------------------------------------------- downloads
const downloadWaiters = new Map();
export function waitDownload(id, timeout = 60000) {
  return new Promise((resolve) => {
    const finish = async () => { clearTimeout(timer); downloadWaiters.delete(id); const [item] = await chrome.downloads.search({ id }); resolve(item || null); };
    const timer = setTimeout(finish, timeout);
    downloadWaiters.set(id, finish);
    chrome.downloads.search({ id }).then(([it]) => { if (it && (it.state === 'complete' || it.state === 'interrupted')) finish(); });
  });
}

// ---------------------------------------------------------------- listeners (must register synchronously at SW top level)
export function registerListeners() {
  chrome.tabs.onCreated.addListener((t) => emit('tab_created', { tab: normalizeTab(t) }));
  chrome.tabs.onRemoved.addListener((tabId, info) => { nav.delete(tabId); emit('tab_removed', { tab_id: tabId, window_id: info.windowId, window_closing: info.isWindowClosing }); });
  chrome.tabs.onActivated.addListener((i) => emit('tab_activated', { tab_id: i.tabId, window_id: i.windowId }));
  chrome.tabs.onReplaced.addListener((added, removed) => emit('tab_replaced', { added_tab_id: added, removed_tab_id: removed }));
  chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
    const keys = ['url', 'title', 'status', 'discarded'].filter((k) => k in change);
    if (keys.length) emit('tab_updated', { tab: normalizeTab(tab), changed: keys });
  });
  chrome.windows.onFocusChanged.addListener((id) => emit('window_focus', { window_id: id }));
  chrome.windows.onCreated.addListener((w) => emit('window_created', { window_id: w.id }));
  chrome.windows.onRemoved.addListener((id) => emit('window_removed', { window_id: id }));

  const main = (d) => d.frameId === 0 && !String(d.url).startsWith('chrome-error://'); // the error page itself must not clear the failure
  chrome.webNavigation.onBeforeNavigate.addListener((d) => { if (main(d)) { navSet(d.tabId, 'loading', { url: d.url }); emit('navigation_started', { tab_id: d.tabId, url: d.url }); } });
  chrome.webNavigation.onCommitted.addListener((d) => { if (main(d)) emit('navigation_committed', { tab_id: d.tabId, url: d.url, transition: d.transitionType, qualifiers: d.transitionQualifiers }); });
  chrome.webNavigation.onCompleted.addListener((d) => { if (main(d)) { navSet(d.tabId, 'done', { url: d.url }); emit('navigation_completed', { tab_id: d.tabId, url: d.url }); } });
  chrome.webNavigation.onErrorOccurred.addListener((d) => { if (main(d)) { navSet(d.tabId, 'error', { url: d.url, error: d.error }); emit('navigation_error', { tab_id: d.tabId, url: d.url, error: d.error }); } });
  chrome.webNavigation.onHistoryStateUpdated.addListener((d) => { if (main(d)) { navSet(d.tabId, 'done', { url: d.url, spa: true }); emit('navigation_spa', { tab_id: d.tabId, url: d.url }); } });
  chrome.webNavigation.onReferenceFragmentUpdated.addListener((d) => { if (main(d)) { navSet(d.tabId, 'done', { url: d.url, spa: true }); emit('navigation_fragment', { tab_id: d.tabId, url: d.url }); } });
  chrome.webNavigation.onCreatedNavigationTarget.addListener((d) => emit('navigation_target_created', { tab_id: d.tabId, source_tab_id: d.sourceTabId, url: d.url }));

  chrome.downloads.onCreated.addListener((it) => emit('download_created', { download_id: it.id, url: it.finalUrl || it.url, filename: it.filename, state: it.state }));
  chrome.downloads.onChanged.addListener((d) => {
    if (d.state || d.error || d.filename) emit('download_changed', { download_id: d.id, state: d.state && d.state.current, error: d.error && d.error.current, filename: d.filename && d.filename.current });
    if (d.state && (d.state.current === 'complete' || d.state.current === 'interrupted') && downloadWaiters.has(d.id)) downloadWaiters.get(d.id)();
  });
}
