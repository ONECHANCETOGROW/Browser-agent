// Generic browser actions. Nothing here knows about any particular website.
import { ActionError, VERSION, PROTOCOL, errorPayload, mapChromeError, sleep } from './common.js';
import {
  activateTab, currentTab, emit, listTabs, navOf, normalizeTab, resolveTab, tabSupport, waitDownload, waitNavigation, focusedWindowId,
} from './tabs.js';
import {
  assertSupported, callAllFrames, callPage, currentSnapshot, newSnapshotId, resolveElement, storeSnapshot, unwrap,
} from './page.js';
import { captureBaseline, settleAndDiff } from './verify.js';

// ---------------------------------------------------------------- helpers
const KNOWN_SCHEME = /^(https?|file|ftp|chrome|chrome-extension|about|data|view-source|blob):/i;

function normalizeUrl(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s) throw new ActionError('INVALID_PARAMS', 'url is required.');
  if (/^javascript:/i.test(s)) throw new ActionError('INVALID_PARAMS', 'javascript: URLs are not allowed.');
  if (!KNOWN_SCHEME.test(s)) s = 'https://' + s;
  try { return new URL(s).href; } catch (_) { throw new ActionError('INVALID_PARAMS', `Not a valid URL: ${raw}`); }
}
const sameUrl = (a, b) => { try { return new URL(a).href === new URL(b).href; } catch (_) { return a === b; } };
const strip = (r) => { const { ok, ...rest } = r; return rest; };
const post = (name, passed, extra) => ({ name, passed: !!passed, ...(extra || {}) });

async function elementOp(ctx, op, extra = {}) {
  const { frameId, args } = resolveElement(ctx.tab.id, ctx.params);
  return unwrap(await callPage(ctx.tab, op, { ...args, ...extra }, frameId));
}
const valueChecks = (r) => (r.value_matches === undefined ? [] : [post('value_matches', r.value_matches, { note: r.note })]);

async function tabBrief(tabId) {
  try { return normalizeTab(await chrome.tabs.get(tabId)); } catch (_) { return null; }
}

// ---------------------------------------------------------------- checks (verify / wait)
async function runChecks(tab, checks) {
  const out = [];
  for (const c of checks) {
    const type = c.type;
    let passed = false, actual;
    const tabNow = await chrome.tabs.get(tab.id).catch(() => null);
    switch (type) {
      case 'url_contains': actual = tabNow && tabNow.url; passed = !!actual && actual.includes(c.value); break;
      case 'url_equals': actual = tabNow && tabNow.url; passed = !!actual && sameUrl(actual, c.value); break;
      case 'url_regex': actual = tabNow && tabNow.url; passed = !!actual && new RegExp(c.value).test(actual); break;
      case 'title_contains': actual = tabNow && tabNow.title; passed = !!actual && actual.toLowerCase().includes(String(c.value).toLowerCase()); break;
      case 'text_present': case 'text_absent': {
        assertSupported(tabNow || tab);
        const frames = await callAllFrames(tabNow || tab, 'textPresent', { text: c.value, case_insensitive: c.case_insensitive });
        const found = frames.some((f) => f.result.found);
        actual = found; passed = type === 'text_present' ? found : !found; break;
      }
      case 'element_exists': case 'element_absent': {
        assertSupported(tabNow || tab);
        const frames = await callAllFrames(tabNow || tab, 'findElements', { role: c.role, name_contains: c.name_contains, tag: c.tag });
        const count = frames.reduce((n, f) => n + (f.result.count || 0), 0);
        actual = count; passed = type === 'element_exists' ? count > 0 : count === 0; break;
      }
      case 'element_value': case 'element_checked': {
        const { frameId, args } = resolveElement(tab.id, c);
        const r = await callPage(tab, 'elementState', args, frameId);
        if (r.ok === false) { actual = r.error.code; passed = false; break; }
        if (type === 'element_value') { actual = r.value; passed = c.equals !== undefined ? r.value === String(c.equals) : String(r.value).includes(String(c.contains)); }
        else { actual = r.checked; passed = r.checked === (c.equals !== false); }
        break;
      }
      case 'tab_count': { const n = (await chrome.tabs.query({})).length; actual = n; passed = c.equals !== undefined ? n === c.equals : n >= (c.at_least || 0); break; }
      case 'tab_active': { actual = !!(tabNow && tabNow.active); passed = actual; break; }
      default: throw new ActionError('INVALID_PARAMS', `Unknown check type: ${type}`);
    }
    out.push({ type, passed, expected: c.value ?? c.equals ?? c.contains ?? c.name_contains ?? undefined, actual });
  }
  return out;
}

// ---------------------------------------------------------------- handlers
const H = {};

H.ping = async () => ({ result: { extension_id: chrome.runtime.id, version: VERSION, protocol: PROTOCOL, ts: Date.now() } });

H.get_tabs = async () => {
  const tabs = await listTabs();
  const win = await focusedWindowId();
  const cur = await currentTab();
  return { result: { tabs, count: tabs.length, active_tab_id: cur ? cur.id : null, focused_window_id: win } };
};

H.get_active_tab = async () => {
  const t = await currentTab();
  if (!t) throw new ActionError('NO_ACTIVE_TAB', 'There is no active tab.');
  return { result: { tab: normalizeTab(t) } };
};

H.get_browser_state = async () => {
  const wins = await chrome.windows.getAll({ populate: true, windowTypes: ['normal', 'popup'] });
  const cur = await currentTab();
  const focused = await focusedWindowId();
  const windows = wins.map((w) => ({ window_id: w.id, focused: w.id === focused, type: w.type, state: w.state, incognito: w.incognito, tabs: (w.tabs || []).map(normalizeTab) }));
  const active = cur ? normalizeTab(cur) : null;
  let website = null;
  try { if (active && active.url) website = new URL(active.url).hostname || active.url; } catch (_) { website = active && active.url; }
  return { result: { windows, tab_count: windows.reduce((n, w) => n + w.tabs.length, 0), active_tab: active, current_website: website, snapshot: cur && currentSnapshot(cur.id) ? { snapshot_id: currentSnapshot(cur.id).snapshot_id, url: currentSnapshot(cur.id).url } : null } };
};

H.get_page_state = async (ctx) => {
  const r = unwrap(await callPage(ctx.tab, 'pageState'));
  return { result: { tab: normalizeTab(await chrome.tabs.get(ctx.tab.id)), page: r.page, evidence: r.evidence } };
};

H.inspect_page = async (ctx) => {
  const p = ctx.params;
  const max = Math.min(Math.max(Number(p.max_elements) || 300, 1), 1000);
  const snapshotId = newSnapshotId();
  const frames = await callAllFrames(ctx.tab, 'inspect', { snapshot_id: snapshotId, viewport_only: !!p.viewport_only, max_elements: max, include_selectors: !!p.include_selectors });
  const top = frames.find((f) => f.frameId === 0);
  if (!top || !top.result.ok) throw new ActionError('PAGE_NOT_READY', 'The top frame did not return an inspection result.');
  const elements = [];
  const map = new Map();
  let total = 0, truncated = false;
  for (const f of frames) {
    if (!f.result.ok) continue;
    total += f.result.total_candidates;
    if (f.result.truncated) truncated = true;
    for (const el of f.result.elements) {
      if (elements.length >= max) { truncated = true; break; }
      const id = 'el_' + String(elements.length + 1).padStart(3, '0');
      const { local_id, ...rest } = el;
      map.set(id, { frameId: f.frameId, local_id });
      elements.push({ id, ...(f.frameId ? { frame: f.frameId } : {}), ...rest });
    }
  }
  const tab = await chrome.tabs.get(ctx.tab.id);
  storeSnapshot(ctx.tab.id, { snapshot_id: snapshotId, url: top.result.page.url, created: Date.now(), elements: map });
  const result = {
    snapshot_id: snapshotId, tab_id: ctx.tab.id, url: tab.url, title: tab.title, page: top.result.page, elements,
    counts: { returned: elements.length, candidates: total, frames: frames.length }, truncated,
    notes: ['rect values are relative to each element\'s own frame viewport', 'element ids are only valid with this snapshot_id; re-inspect after the page changes'],
  };
  if (p.include_text) {
    const t = unwrap(await callPage(ctx.tab, 'readText', { scope: 'viewport', max_chars: p.text_chars || 3000 }));
    result.visible_text = t.text;
  }
  return { result };
};

H.read_page = async (ctx) => {
  const scope = ctx.params.scope === 'page' ? 'page' : 'viewport';
  const max = Math.min(Math.max(Number(ctx.params.max_chars) || 6000, 100), 100000);
  if (scope === 'viewport') {
    const r = unwrap(await callPage(ctx.tab, 'readText', { scope, max_chars: max }));
    return { result: strip(r) };
  }
  const frames = await callAllFrames(ctx.tab, 'readText', { scope, max_chars: max });
  let text = '';
  for (const f of frames) {
    if (!f.result.ok) continue;
    text += (f.frameId === 0 ? '' : `\n--- iframe ${f.result.url} ---\n`) + f.result.text;
  }
  return { result: { scope, text: text.slice(0, max), total_chars: text.length, truncated: text.length > max, url: frames[0] && frames[0].result.url, title: frames[0] && frames[0].result.title, frames: frames.length } };
};

/** Search the page text, bring a match into view, return where it is and what surrounds it. DOM text only; form-control contents are never read. */
H.find_text = async (ctx) => {
  const p = ctx.params;
  const text = typeof p.text === 'string' ? p.text : '';
  if (!text.trim()) throw new ActionError('INVALID_PARAMS', 'text (a non-empty string) is required.');
  const int = (v, d, lo, hi) => Math.min(Math.max(Number.isInteger(v) ? v : d, lo), hi);
  const index = int(p.index, 0, 0, 1000000);
  const maxMatches = int(p.max_matches, 10, 1, 50);
  const a = { text, case_sensitive: p.case_sensitive === true, whole_word: p.whole_word === true, max_matches: maxMatches, context_chars: int(p.context_chars, 80, 0, 300), scroll: p.scroll !== false, scroll_index: index };
  const frames = await callAllFrames(ctx.tab, 'findText', a);
  const top = frames.find((f) => f.frameId === 0);
  if (!top) throw new ActionError('PAGE_NOT_READY', 'The top frame did not return a search result.');
  unwrap(top.result);
  const good = frames.filter((f) => f.result.ok);

  // Snapshot-bound element ids: only handed out when the page's current inspect_page snapshot really covers the element.
  const snap = currentSnapshot(ctx.tab.id);
  const publicId = new Map();
  if (snap) for (const [id, e] of snap.elements) publicId.set(`${e.frameId}:${e.local_id}`, id);
  const bind = (frameId, r, m) => {
    const { local_id, local_index, ...rest } = m;
    const out = { match_index: 0, ...rest };
    if (local_id && snap && r.snapshot_id === snap.snapshot_id && publicId.has(`${frameId}:${local_id}`)) { out.element_id = publicId.get(`${frameId}:${local_id}`); out.snapshot_id = snap.snapshot_id; }
    if (frameId) out.frame = frameId;
    return { out, local_index };
  };

  let offset = 0, total = 0, hidden = 0, capped = false, truncated = false;
  const matches = [], spans = [];
  let focused = null, scroll = null;
  for (const f of good) {
    const r = f.result;
    spans.push({ frameId: f.frameId, offset, count: r.total, r });
    for (const m of r.matches) {
      const { out, local_index } = bind(f.frameId, r, m);
      out.match_index = offset + local_index;
      if (matches.length < maxMatches) matches.push(out);
    }
    if (r.focused && !focused) { const { out, local_index } = bind(f.frameId, r, r.focused); out.match_index = offset + local_index; focused = out; scroll = r.scroll; }
    total += r.total; hidden += r.hidden; capped = capped || r.count_capped; truncated = truncated || r.scan_truncated;
    offset += r.total;
  }
  // the requested match lives in an iframe: ask that frame to scroll to it (the top frame only scrolls its own matches)
  if (a.scroll && !focused && index < total) {
    const span = spans.find((s) => index >= s.offset && index < s.offset + s.count);
    if (span && span.frameId !== 0) {
      const r2 = unwrap(await callPage(ctx.tab, 'findText', { ...a, scroll_index: index - span.offset, scroll_all_frames: true, max_matches: 1 }, span.frameId));
      if (r2.focused) { const { out } = bind(span.frameId, r2, r2.focused); out.match_index = index; focused = out; scroll = r2.scroll; }
    }
  }

  const notes = [];
  if (!total && hidden) notes.push(`${hidden} match(es) exist only in hidden or collapsed content and cannot be scrolled to.`);
  if (total && a.scroll && index >= total) notes.push(`index ${index} is out of range (${total} match(es), 0-based); nothing was scrolled.`);
  if (capped) notes.push('There are more matches than the search keeps counting; refine the text.');
  if (truncated) notes.push('The search stopped at its time budget on a very large page; results may be incomplete.');
  if (focused) notes.push('The viewport moved to the match. Element ids from the current inspect_page snapshot stay valid; call inspect_page to see what is now on screen.');
  const result = {
    query: text.length > 120 ? text.slice(0, 119) + '…' : text, found: total > 0, total_matches: total, hidden_matches: hidden,
    case_sensitive: a.case_sensitive, whole_word: a.whole_word, matches, focused, scroll,
    url: top.result.url, title: top.result.title, frames_searched: good.length, ...(notes.length ? { notes } : {}),
  };
  const checks = a.scroll && total && index < total ? [post('match_in_view', !!(focused && focused.in_viewport), { index })] : [];
  return { result, checks };
};

H.navigate = async (ctx) => {
  const url = normalizeUrl(ctx.params.url);
  const since = navOf(ctx.tab.id).seq;
  try { await chrome.tabs.update(ctx.tab.id, { url }); } catch (e) { throw mapChromeError(e, ctx.tab); }
  const w = await waitNavigation(ctx.tab.id, since, { timeout: ctx.params.timeout_ms || 30000, startGrace: 2000 });
  const tab = await chrome.tabs.get(ctx.tab.id);
  if (w.state === 'error') throw new ActionError('NAVIGATION_FAILED', `Navigation failed: ${w.error || 'unknown error'}`, { requested_url: url, error: w.error });
  const errPage = (tab.url || '').startsWith('chrome-error://');
  const result = { tab_id: tab.id, requested_url: url, final_url: tab.url, title: tab.title, status: tab.status, redirected: !sameUrl(url, tab.url), navigation: w };
  return { result, skipSettle: true, checks: [post('navigation_observed', w.started || sameUrl(url, tab.url)), post('load_completed', w.state === 'done', { state: w.state }), post('not_error_page', !errPage)] };
};

H.new_tab = async (ctx) => {
  const url = ctx.params.url ? normalizeUrl(ctx.params.url) : undefined;
  const t = await chrome.tabs.create({ ...(url ? { url } : {}), active: ctx.params.active !== false, ...(ctx.params.window_id ? { windowId: ctx.params.window_id } : {}) });
  let w = null;
  if (url) w = await waitNavigation(t.id, 0, { timeout: ctx.params.timeout_ms || 30000, startGrace: 2000 });
  if (w && w.state === 'error') throw new ActionError('NAVIGATION_FAILED', `The new tab failed to load: ${w.error}`, { tab_id: t.id, error: w.error });
  const tab = await tabBrief(t.id);
  return { result: { tab, navigation: w }, skipSettle: true, checks: [post('tab_exists', !!tab), ...(url ? [post('load_completed', (w && w.state === 'done') || (!!tab && tab.status === 'complete' && w && w.state === 'idle'), { state: w && w.state })] : [])] };
};

H.switch_tab = async (ctx) => {
  let id = ctx.params.tab_id;
  let matches;
  if (id === undefined || id === null) {
    const m = ctx.params.match;
    if (!m || (!m.url_contains && !m.title_contains)) throw new ActionError('INVALID_PARAMS', 'switch_tab needs tab_id or match {url_contains|title_contains}.');
    const all = (await chrome.tabs.query({})).filter((t) => (!m.url_contains || (t.url || '').toLowerCase().includes(m.url_contains.toLowerCase())) && (!m.title_contains || (t.title || '').toLowerCase().includes(m.title_contains.toLowerCase())));
    if (!all.length) throw new ActionError('TAB_NOT_FOUND', 'No tab matches.', { match: m });
    matches = all.length; id = all[0].id;
  }
  try { await chrome.tabs.get(Number(id)); } catch (_) { throw new ActionError('TAB_NOT_FOUND', `No tab with id ${id}.`, { tab_id: id }); }
  const t = await activateTab(Number(id));
  const win = await chrome.windows.get(t.windowId);
  return { result: { tab: normalizeTab(t), window_focused: win.focused, ...(matches > 1 ? { matched_tabs: matches, note: 'Several tabs matched; the first was used.' } : {}) }, checks: [post('tab_is_active', t.active), post('window_focused', win.focused)] };
};

H.close_tab = async (ctx) => {
  if (ctx.params.tab_id === undefined) throw new ActionError('INVALID_PARAMS', 'close_tab requires an explicit tab_id.');
  const t = ctx.tab;
  const all = await chrome.tabs.query({});
  const wins = await chrome.windows.getAll({ windowTypes: ['normal'] });
  if (all.length === 1 || (wins.length === 1 && all.filter((x) => x.windowId === t.windowId).length === 1 && wins[0].id === t.windowId)) {
    throw new ActionError('LAST_TAB', 'Refusing to close the last tab of the last window (it would end the browser session).', { tab_id: t.id });
  }
  await chrome.tabs.remove(t.id);
  let gone = false;
  for (let i = 0; i < 20 && !gone; i++) { try { await chrome.tabs.get(t.id); await sleep(75); } catch (_) { gone = true; } }
  const cur = await currentTab();
  return { result: { closed_tab_id: t.id, closed_url: t.url, closed_title: t.title, active_tab: cur ? normalizeTab(cur) : null }, checks: [post('tab_gone', gone)] };
};

async function historyOrReload(ctx, how) {
  const since = navOf(ctx.tab.id).seq;
  const before = ctx.tab.url;
  let via = 'tabs_api';
  try {
    if (how === 'reload') await chrome.tabs.reload(ctx.tab.id, { bypassCache: !!ctx.params.bypass_cache });
    else if (how === 'back') await chrome.tabs.goBack(ctx.tab.id);
    else await chrome.tabs.goForward(ctx.tab.id);
  } catch (e) {
    if (how === 'reload') throw mapChromeError(e, ctx.tab);
    // Chrome's tabs API refuses to step onto history entries that never received user activation (a back-button intervention).
    // The page's own history.go() is not subject to that, and navigation.canGoBack tells the truth about whether an entry exists.
    const none = new ActionError('NO_HISTORY', `There is no page to go ${how} to in this tab.`, { raw: String(e.message || e) });
    if (!tabSupport(ctx.tab).supported) throw none;
    const r = await callPage(ctx.tab, 'historyGo', { delta: how === 'back' ? -1 : 1 });
    if (r.ok === false) throw none;
    via = 'history_api';
  }
  const w = await waitNavigation(ctx.tab.id, since, { timeout: ctx.params.timeout_ms || 30000, startGrace: 2000 });
  const tab = await chrome.tabs.get(ctx.tab.id);
  if (w.state === 'error') throw new ActionError('NAVIGATION_FAILED', `Navigation failed: ${w.error}`, { error: w.error });
  const moved = how === 'reload' ? w.started : (w.started || tab.url !== before);
  return { result: { tab_id: tab.id, url_before: before, final_url: tab.url, title: tab.title, navigation: w, via }, skipSettle: true, checks: [post('navigation_observed', moved), post('load_completed', w.state === 'done' || (w.state === 'idle' && how !== 'reload' && tab.url !== before), { state: w.state })] };
}
H.reload = (ctx) => historyOrReload(ctx, 'reload');
H.back = (ctx) => historyOrReload(ctx, 'back');
H.forward = (ctx) => historyOrReload(ctx, 'forward');

H.click = async (ctx) => {
  const p = ctx.params;
  let r;
  if (p.element_id) r = await elementOp(ctx, 'click', { force: !!p.force });
  else r = unwrap(await callPage(ctx.tab, 'click', { x: p.x, y: p.y }));
  const result = strip(r);
  const checks = [];
  if (r.open_in_new_tab) {
    const t = await chrome.tabs.create({ url: r.open_in_new_tab, openerTabId: ctx.tab.id, active: true });
    result.opened_tab = normalizeTab(t);
    result.note = 'The link targets a new tab; the extension opened it directly because Chrome blocks script-initiated popups.';
    checks.push(post('new_tab_opened', !!t));
  }
  return { result, checks };
};

H.type = async (ctx) => {
  if (typeof ctx.params.text !== 'string') throw new ActionError('INVALID_PARAMS', 'text (string) is required.');
  const r = await elementOp(ctx, 'type', { text: ctx.params.text, clear: ctx.params.clear !== false, mode: ctx.params.mode === 'keys' ? 'keys' : 'set' });
  return { result: strip(r), checks: valueChecks(r) };
};
H.clear = async (ctx) => { const r = await elementOp(ctx, 'clear'); return { result: strip(r), checks: valueChecks(r) }; };
H.select = async (ctx) => {
  const p = ctx.params;
  const r = await elementOp(ctx, 'select', { option: p.option, value: p.value, label: p.label, index: p.index, values: p.values });
  return { result: strip(r), checks: valueChecks(r) };
};
H.check = async (ctx) => { const r = await elementOp(ctx, 'setChecked', { checked: true, force: !!ctx.params.force }); return { result: strip(r), checks: valueChecks(r) }; };
H.uncheck = async (ctx) => { const r = await elementOp(ctx, 'setChecked', { checked: false, force: !!ctx.params.force }); return { result: strip(r), checks: valueChecks(r) }; };

H.scroll = async (ctx) => {
  const p = ctx.params;
  const a = { direction: p.direction, amount: p.amount, to: p.to };
  const r = p.element_id ? await elementOp(ctx, 'scroll', a) : unwrap(await callPage(ctx.tab, 'scroll', a));
  return { result: strip(r), checks: [post('scrolled_or_at_limit', r.value_matches, { moved: r.moved, reached_limit: r.reached_limit })] };
};

H.press_key = async (ctx) => {
  const p = ctx.params;
  const a = { key: p.key, modifiers: p.modifiers };
  const r = p.element_id ? await elementOp(ctx, 'pressKey', a) : unwrap(await callPage(ctx.tab, 'pressKey', a));
  return { result: strip(r), checks: r.default_action === 'form_submit_blocked' ? [post('form_submitted', false, { note: r.note })] : [] };
};

H.upload_file = async (ctx) => {
  const files = ctx.params.files;
  if (!Array.isArray(files) || !files.length || !files[0].data_b64) {
    throw new ActionError('INVALID_PARAMS', 'The extension only accepts file contents prepared by the local agent. Call upload_file through the agent API with local_file_path.');
  }
  const r = await elementOp(ctx, 'upload', { files });
  return { result: strip(r), checks: valueChecks(r) };
};

H.screenshot = async (ctx) => {
  const p = ctx.params;
  let tab = ctx.tab;
  let activated = false;
  if (!tab.active) { tab = await activateTab(tab.id); activated = true; await sleep(200); }
  const format = p.format === 'jpeg' ? 'jpeg' : 'png';
  let dataUrl;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format, ...(format === 'jpeg' ? { quality: p.quality || 80 } : {}) });
      break;
    } catch (e) {
      if (/quota|MAX_CAPTURE/i.test(String(e.message)) && attempt < 2) { await sleep(700); continue; }
      throw mapChromeError(e, tab);
    }
  }
  const blob = await (await fetch(dataUrl)).blob();
  const bmp = await createImageBitmap(blob);
  const result = { data_url: dataUrl, format, width: bmp.width, height: bmp.height, bytes: blob.size, tab_id: tab.id, url: tab.url, activated_tab: activated, scope: 'visible_viewport' };
  bmp.close();
  return { result, checks: [post('image_captured', blob.size > 100)] };
};

H.download_file = async (ctx) => {
  const url = normalizeUrl(ctx.params.url);
  if (!/^https?:/i.test(url)) throw new ActionError('INVALID_PARAMS', 'download_file supports http(s) URLs only.');
  if (ctx.params.filename && (/^[a-z]:|^[\\/]/i.test(ctx.params.filename) || ctx.params.filename.split(/[\\/]/).includes('..'))) {
    throw new ActionError('INVALID_PARAMS', 'filename must be relative to Chrome\'s download folder and cannot contain "..".');
  }
  let id;
  try { id = await chrome.downloads.download({ url, ...(ctx.params.filename ? { filename: ctx.params.filename } : {}), conflictAction: 'uniquify', saveAs: false }); }
  catch (e) { throw new ActionError('DOWNLOAD_FAILED', String(e.message || e)); }
  const item = await waitDownload(id, ctx.params.timeout_ms || 60000);
  const result = { download_id: id, state: item && item.state, filename: item && item.filename, bytes: item && (item.fileSize > 0 ? item.fileSize : item.totalBytes), mime: item && item.mime, url: item && (item.finalUrl || item.url), error: item && item.error };
  return { result, checks: [post('download_complete', item && item.state === 'complete', { state: item && item.state, error: item && item.error })] };
};

H.list_downloads = async (ctx) => {
  const items = await chrome.downloads.search({ orderBy: ['-startTime'], limit: Math.min(Number(ctx.params.limit) || 20, 100) });
  return { result: { downloads: items.map((i) => ({ download_id: i.id, state: i.state, filename: i.filename, url: i.finalUrl || i.url, mime: i.mime, bytes: i.fileSize > 0 ? i.fileSize : i.totalBytes, start_time: i.startTime, error: i.error })) } };
};

H.verify = async (ctx) => {
  const checks = ctx.params.checks;
  if (!Array.isArray(checks) || !checks.length) throw new ActionError('INVALID_PARAMS', 'checks (non-empty array) is required.');
  const out = await runChecks(ctx.tab, checks);
  const all = out.every((c) => c.passed);
  return { result: { all_passed: all, checks: out }, checks: [post('all_checks_passed', all, { failed: out.filter((c) => !c.passed).map((c) => c.type) })] };
};

H.wait = async (ctx) => {
  const p = ctx.params;
  if (p.until) {
    const timeout = p.timeout_ms || 10000, poll = p.poll_ms || 250;
    const t0 = Date.now();
    let last = [];
    for (;;) {
      try { last = await runChecks(ctx.tab, p.until); } catch (e) { if (!(e instanceof ActionError) || !['PAGE_NAVIGATED', 'PAGE_NOT_READY', 'STALE_ELEMENT'].includes(e.code)) throw e; last = []; }
      if (last.length && last.every((c) => c.passed)) break;
      if (Date.now() - t0 > timeout) break;
      await sleep(poll);
    }
    const ok = last.length > 0 && last.every((c) => c.passed);
    return { result: { satisfied: ok, waited_ms: Date.now() - t0, checks: last }, skipSettle: true, checks: [post('condition_met', ok)] };
  }
  const ms = Math.min(Math.max(Number(p.ms) || 0, 0), 60000);
  await sleep(ms);
  return { result: { waited_ms: ms }, skipSettle: true };
};

// ---------------------------------------------------------------- registry of extension-side actions
// tab: needs a target tab; page: tab must be scriptable; mutating: capture before/after evidence.
const SPEC = {
  ping: { tab: false },
  get_tabs: { tab: false }, get_active_tab: { tab: false }, get_browser_state: { tab: false },
  get_page_state: { tab: true, page: true }, inspect_page: { tab: true, page: true }, read_page: { tab: true, page: true },
  find_text: { tab: true, page: true, mutating: true },
  navigate: { tab: true, mutating: true }, new_tab: { tab: false, mutating: true, baselineCurrent: true },
  switch_tab: { tab: false, mutating: true, baselineCurrent: true }, close_tab: { tab: true, mutating: true },
  reload: { tab: true, mutating: true }, back: { tab: true, mutating: true }, forward: { tab: true, mutating: true },
  click: { tab: true, page: true, mutating: true }, type: { tab: true, page: true, mutating: true }, clear: { tab: true, page: true, mutating: true },
  select: { tab: true, page: true, mutating: true }, check: { tab: true, page: true, mutating: true }, uncheck: { tab: true, page: true, mutating: true },
  scroll: { tab: true, page: true, mutating: true }, press_key: { tab: true, page: true, mutating: true },
  upload_file: { tab: true, page: true, mutating: true },
  screenshot: { tab: true, page: true },
  download_file: { tab: false, mutating: true, baselineCurrent: true }, list_downloads: { tab: false },
  verify: { tab: true }, wait: { tab: true },
};

export const SUPPORTED_ACTIONS = Object.keys(SPEC);

export async function runAction(req) {
  const t0 = Date.now();
  const base = { request_id: req.request_id, action: req.action };
  const spec = SPEC[req.action];
  if (!spec || !H[req.action]) {
    return { ...base, success: false, result: null, error: { code: 'UNKNOWN_ACTION', message: `Unknown action: ${req.action}` }, verification: null };
  }
  const params = req.parameters || {};
  let tab = null, baseline = null, verification = null;
  try {
    if (spec.tab) tab = await resolveTab(params);
    else if (spec.baselineCurrent) tab = await currentTab();
    if (spec.page) assertSupported(tab);
    if (spec.mutating && tab) baseline = await captureBaseline(tab);
    const out = await H[req.action]({ params, tab, request_id: req.request_id });
    if (baseline) verification = await settleAndDiff(baseline, { skipSettle: !!out.skipSettle });
    const checks = out.checks || [];
    const failed = checks.filter((c) => !c.passed);
    if (verification) verification.checks = checks; else if (checks.length) verification = { checks };
    if (failed.length) {
      return { ...base, success: false, result: out.result, error: { code: 'POSTCONDITION_FAILED', message: `The action ran but could not be confirmed: ${failed.map((c) => c.name).join(', ')}.`, details: { failed } }, verification, duration_ms: Date.now() - t0 };
    }
    return { ...base, success: true, result: out.result, error: null, verification, duration_ms: Date.now() - t0 };
  } catch (e) {
    const err = errorPayload(mapChromeError(e, tab));
    if (baseline) { try { verification = await settleAndDiff(baseline, { skipSettle: true }); } catch (_) { verification = null; } }
    emit('action_error', { action: req.action, code: err.code });
    return { ...base, success: false, result: null, error: err, verification, duration_ms: Date.now() - t0 };
  }
}
