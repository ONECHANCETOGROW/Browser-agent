// Before/after evidence. The extension reports facts; it never claims an action "worked" beyond what it can observe.
import { sleep } from './common.js';
import { callPage } from './page.js';
import { currentSeq, eventsSince, navOf, normalizeTab, tabSupport } from './tabs.js';

async function pageEvidence(tabId) {
  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch (_) { return null; }
  if (!tabSupport(tab).supported) return null;
  try {
    const r = await callPage(tab, 'evidence');
    return r.ok ? r.evidence : null;
  } catch (_) { return null; }
}

async function tabIdSet() {
  return new Set((await chrome.tabs.query({})).map((t) => t.id));
}

async function activeIds() {
  const out = {};
  for (const t of await chrome.tabs.query({ active: true })) out[t.windowId] = t.id;
  return out;
}

export async function captureBaseline(tab) {
  const [page, ids, active] = await Promise.all([pageEvidence(tab.id), tabIdSet(), activeIds()]);
  return { t0: Date.now(), tabId: tab.id, windowId: tab.windowId, tab: normalizeTab(tab), page, ids, active, eventSeq: currentSeq(), navSeq: navOf(tab.id).seq };
}

/** Wait until the tab stops loading and the DOM stops mutating (bounded). */
async function settle(base, maxMs) {
  const start = Date.now();
  await sleep(150);
  let last = null;
  while (Date.now() - start < maxMs) {
    let tab;
    try { tab = await chrome.tabs.get(base.tabId); } catch (_) { return; }
    const n = navOf(base.tabId);
    const loading = tab.status === 'loading' || (n.seq > base.navSeq && n.state === 'loading');
    if (!loading) {
      const ev = await pageEvidence(base.tabId);
      const m = ev ? ev.mutations : null;
      if (last !== null && m === last) return;
      last = m;
      if (m === null) return;
    } else last = null;
    await sleep(110);
  }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export async function settleAndDiff(base, { settleMs = 2500, skipSettle = false } = {}) {
  if (!skipSettle) await settle(base, settleMs);
  let tabNow = null;
  try { tabNow = await chrome.tabs.get(base.tabId); } catch (_) { /* tab closed by the action */ }
  const after = await pageEvidence(base.tabId);
  const events = eventsSince(base.eventSeq);
  const idsNow = await tabIdSet();
  const activeNow = await activeIds();

  const newTabs = [];
  for (const id of idsNow) {
    if (!base.ids.has(id)) { try { newTabs.push(normalizeTab(await chrome.tabs.get(id))); } catch (_) { /* opened and closed again */ } }
  }
  const closed = [...base.ids].filter((id) => !idsNow.has(id));
  const navs = events.filter((e) => e.event === 'navigation_committed' && e.data.tab_id === base.tabId).map((e) => e.data.url);
  const downloads = events.filter((e) => e.event === 'download_created').map((e) => ({ download_id: e.data.download_id, url: e.data.url }));
  const activeChanged = !same(base.active, activeNow) ? { before: base.active, after: activeNow } : null;

  const before = base.page;
  const page = (before && after && navs.length === 0) ? {
    text_changed: before.text_sig !== after.text_sig,
    dom_mutations: Math.max(0, after.mutations - before.mutations),
    scroll_changed: before.scroll.x !== after.scroll.x || before.scroll.y !== after.scroll.y,
    dialogs_opened: after.dialogs.filter((d) => !before.dialogs.includes(d)),
    focus_changed: !same(before.focused, after.focused),
    ready_state: after.ready,
  } : (before || after) ? { comparable: false, document_replaced: navs.length > 0, ready_state: after ? after.ready : undefined, note: navs.length > 0 ? 'A new document was loaded, so DOM evidence is not comparable.' : before ? 'The page could not be read after the action.' : 'No page evidence before the action.' } : null;

  const urlAfter = tabNow ? tabNow.url : null;
  const v = {
    tab_id: base.tabId,
    tab_closed: !tabNow,
    settle_ms: Date.now() - base.t0,
    url: { before: base.tab.url, after: urlAfter, changed: !!tabNow && urlAfter !== base.tab.url },
    title: { before: base.tab.title, after: tabNow ? tabNow.title : null, changed: !!tabNow && tabNow.title !== base.tab.title },
    navigated: navs.length > 0,
    navigations: navs,
    new_tabs: newTabs,
    closed_tabs: closed,
    active_tab_changed: activeChanged,
    downloads_started: downloads,
    page,
  };
  v.observed_change = !!(v.url.changed || v.title.changed || v.navigated || newTabs.length || closed.length || activeChanged || downloads.length
    || (page && (page.text_changed || page.scroll_changed || page.focus_changed || (page.dialogs_opened && page.dialogs_opened.length))));
  return v;
}
