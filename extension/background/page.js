// Talking to the injected page agent + snapshot bookkeeping (element ids belong to a snapshot_id).
import { ActionError, mapChromeError, unsupportedError, withTimeout } from './common.js';
import { tabSupport } from './tabs.js';

const PAGE_CALL_TIMEOUT_MS = 12000;
const BLOCKED_HINT = 'The page did not respond. A JavaScript dialog (alert/confirm/prompt) may be blocking it; those cannot be dismissed without the debugger permission.';

export function assertSupported(tab) {
  const cls = tabSupport(tab);
  if (!cls.supported) throw unsupportedError(tab, cls);
}

async function inject(target, tab) {
  try {
    await chrome.scripting.executeScript({ target, files: ['content/page_agent.js'] });
  } catch (e) { throw mapChromeError(e, tab); }
}

/** Call window.__octg[op](args) in one frame (default top frame). Returns the page agent's JSON. */
export async function callPage(tab, op, args = {}, frameId = 0) {
  const target = { tabId: tab.id, frameIds: [frameId] };
  await inject(target, tab);
  let results;
  try {
    results = await withTimeout(
      chrome.scripting.executeScript({ target, func: (o, a) => window.__octg[o](a), args: [op, args] }),
      PAGE_CALL_TIMEOUT_MS, 'PAGE_UNRESPONSIVE', BLOCKED_HINT);
  } catch (e) { throw mapChromeError(e, tab); }
  const r = results && results[0] && results[0].result;
  if (!r) throw new ActionError('PAGE_NOT_READY', 'The page returned no result (it may be navigating).');
  return r;
}

/** Call in every accessible frame. Returns [{frameId, result}] (top frame first). */
export async function callAllFrames(tab, op, args = {}) {
  const target = { tabId: tab.id, allFrames: true };
  await inject(target, tab);
  let results;
  try {
    results = await withTimeout(
      chrome.scripting.executeScript({ target, func: (o, a) => window.__octg[o](a), args: [op, args] }),
      PAGE_CALL_TIMEOUT_MS, 'PAGE_UNRESPONSIVE', BLOCKED_HINT);
  } catch (e) { throw mapChromeError(e, tab); }
  return (results || []).filter((r) => r && r.result).sort((a, b) => a.frameId - b.frameId).map((r) => ({ frameId: r.frameId, result: r.result }));
}

/** Throw an ActionError if the page agent reported failure. */
export function unwrap(r) {
  if (r && r.ok === false) throw new ActionError(r.error.code, r.error.message, r.error.details);
  return r;
}

// ---------------------------------------------------------------- snapshots
const snapshots = new Map(); // tabId -> {snapshot_id, url, created, elements: Map(id -> {frameId, local_id})}
let counter = 0;
const STORE_KEY = 'octg_snapshots';

export const newSnapshotId = () => `snap_${Date.now().toString(36)}_${++counter}`;

export function storeSnapshot(tabId, snap) {
  snapshots.set(tabId, snap);
  const plain = {};
  for (const [id, s] of snapshots) plain[id] = { snapshot_id: s.snapshot_id, url: s.url, created: s.created, elements: [...s.elements] };
  chrome.storage.session.set({ [STORE_KEY]: plain }).catch(() => {});
}

export const dropSnapshot = (tabId) => { snapshots.delete(tabId); };
export const currentSnapshot = (tabId) => snapshots.get(tabId) || null;

export async function restoreSnapshots() {
  try {
    const got = (await chrome.storage.session.get(STORE_KEY))[STORE_KEY] || {};
    for (const [id, s] of Object.entries(got)) snapshots.set(Number(id), { ...s, elements: new Map(s.elements) });
  } catch (_) { /* session storage unavailable: snapshots simply start empty, stale ids will be reported */ }
}

/** Map a public element id to its frame and page-local id, enforcing snapshot freshness. */
export function resolveElement(tabId, params) {
  const id = params.element_id;
  if (!id) throw new ActionError('INVALID_PARAMS', 'element_id is required.');
  const snap = snapshots.get(tabId);
  if (!snap) throw new ActionError('STALE_ELEMENT', 'No inspect_page snapshot exists for this tab. Run inspect_page first.', { reason: 'no_snapshot' });
  if (!params.snapshot_id) throw new ActionError('INVALID_PARAMS', 'snapshot_id is required with element_id (use the snapshot_id returned by inspect_page).', { current_snapshot_id: snap.snapshot_id });
  if (params.snapshot_id !== snap.snapshot_id) {
    throw new ActionError('STALE_ELEMENT', `Element belongs to snapshot ${params.snapshot_id}, but the current snapshot for this tab is ${snap.snapshot_id}. Re-run inspect_page.`, { reason: 'snapshot_superseded', current_snapshot_id: snap.snapshot_id });
  }
  const e = snap.elements.get(id);
  if (!e) throw new ActionError('ELEMENT_NOT_FOUND', `No element ${id} in snapshot ${snap.snapshot_id}.`, { snapshot_id: snap.snapshot_id });
  return { frameId: e.frameId, args: { local_id: e.local_id, snapshot_id: snap.snapshot_id } };
}
