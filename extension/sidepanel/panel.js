// Side panel: a thin view over the service worker's state, plus the task controls. It holds no secrets beyond the pairing token the user
// types here, and it never sees model API keys (those live only in the local agent).
(() => {
  const $ = (id) => document.getElementById(id);
  let port = null;
  let clearedBefore = 0;
  let lastState = null;
  let lastToastTs = 0;
  let providersKey = '';

  const ACTIVE = new Set(['PENDING', 'PLANNING', 'RUNNING', 'PAUSED', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_USER']);
  const AGENT_LABEL = { idle: 'Not configured', connecting: 'Connecting…', connected: 'Connected', disconnected: 'Disconnected', auth_failed: 'Rejected (check token)' };
  const STATUS_LABEL = { PENDING: 'Starting', PLANNING: 'Planning', RUNNING: 'Running', PAUSED: 'Paused', WAITING_FOR_APPROVAL: 'Waiting for approval', WAITING_FOR_USER: 'Waiting for you', COMPLETED: 'Completed', FAILED: 'Failed', CANCELLED: 'Cancelled' };

  function connectPort() {
    port = chrome.runtime.connect({ name: 'octg-panel' });
    port.onMessage.addListener((msg) => {
      if (msg.type === 'state') render(msg.state);
      if (msg.type === 'toast') $('connMsg').textContent = msg.text;
      if (msg.type === 'biz_result' && window.octgBiz) window.octgBiz.onResult(msg); // Business Brain views (relayed once, never stored)
      if (msg.type === 'panel_result') { const v = String(msg.command).startsWith('ask_') ? window.octgAsk : window.octgInbox; if (v) v.onResult(msg); } // Ask / Inbox (relayed once, never stored)
    });
    port.onDisconnect.addListener(() => setTimeout(connectPort, 500)); // service worker restarted
  }
  const ui = (command, extra) => port.postMessage({ type: 'ui', command, ...(extra || {}) });
  window.octgSend = ui;

  // ---- sections: Task (the original panel), Ask, Inbox
  const TABS = ['task', 'ask', 'inbox'];
  let currentSection = 'task';
  let inboxOpen = 0;
  function updateBanner() {
    const n = inboxOpen;
    const c = $('inboxCount');
    c.textContent = String(n);
    c.hidden = !n;
    $('inboxBanner').hidden = !(n && currentSection !== 'inbox');
    $('inboxBannerText').textContent = n === 1 ? 'The AI is waiting for you to do something.' : `The AI is waiting for you on ${n} things.`;
  }
  function setInboxCount(n) { inboxOpen = Math.max(0, Number(n) || 0); updateBanner(); }
  function showSection(name) {
    currentSection = name;
    for (const t of TABS) $('tab-' + t).hidden = t !== name;
    for (const b of document.querySelectorAll('#mainTabs .mtab')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
    if (window.octgAsk) { if (name === 'ask') window.octgAsk.show(); else window.octgAsk.hide(); }
    if (window.octgInbox) { if (name === 'inbox') window.octgInbox.show(); else window.octgInbox.hide(); }
    updateBanner();
  }
  window.octgInboxCount = setInboxCount;
  window.octgOpenConnection = () => { showSection('task'); $('conn').open = true; $('conn').scrollIntoView({ block: 'center' }); }; // "Open connection settings" from Ask / Inbox
  window.octgCurrentTab = () => (lastState && lastState.browser && lastState.browser.tab ? lastState.browser.tab : null); // the tab the owner is on (for "attach this tab")
  for (const b of document.querySelectorAll('#mainTabs .mtab')) b.addEventListener('click', () => showSection(b.dataset.tab));
  $('inboxOpen').addEventListener('click', () => showSection('inbox'));

  function renderProviders(p) {
    const key = JSON.stringify(p);
    if (key === providersKey) return;
    providersKey = key;
    const sel = $('model');
    const current = sel.value || localStorageGet('octg.model') || 'auto';
    const auto = document.createElement('option');
    auto.value = 'auto';
    auto.textContent = 'Auto (' + ((p && p.policy) || 'LOCAL_FIRST').toLowerCase().replace(/_/g, ' ') + ')';
    const opts = [auto];
    for (const pr of (p && p.providers) || []) {
      const o = document.createElement('option');
      o.value = pr.name;
      o.textContent = `${pr.available ? '● ' : '○ '}${pr.name} — ${pr.model}${pr.available ? '' : ' (unavailable)'}`;
      opts.push(o);
    }
    sel.replaceChildren(...opts);
    sel.value = [...sel.options].some((o) => o.value === current) ? current : 'auto';
  }

  function localStorageGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* optional convenience */ } }

  function render(s) {
    lastState = s;
    const c = s.connection;
    const t = s.task;
    const active = !!(t && ACTIVE.has(t.status));
    $('badge').textContent = c.state;
    $('badge').className = 'badge ' + c.state;
    $('agentStatus').textContent = AGENT_LABEL[c.state] || c.state;
    $('browserStatus').textContent = s.browser.tab_count + ' tab' + (s.browser.tab_count === 1 ? '' : 's');
    const tab = s.browser.tab;
    const pageUrl = (t && active && t.current_url) || (tab && tab.url) || '';
    $('website').textContent = s.browser.current_website || '—';
    $('tabId').textContent = tab ? '#' + tab.tab_id : '—';
    $('url').textContent = pageUrl || '—';
    $('title').textContent = (tab && tab.title) || '—';

    // task block
    const st = $('status');
    st.replaceChildren();
    if (t) {
      const chip = document.createElement('span');
      chip.className = 'chip ' + t.status;
      chip.textContent = STATUS_LABEL[t.status] || t.status;
      st.append(chip);
    } else st.textContent = c.state === 'connected' ? 'Idle' : '—';
    $('task').textContent = t ? t.user_instruction : ((s.agent && s.agent.task) || '—');
    $('action').textContent = (t && active && (t.current_step || (s.agent && s.agent.current_action))) || (s.agent && s.agent.current_action) || (c.state === 'connected' ? 'idle' : '—');
    const u = t && t.usage;
    $('usage').textContent = u && u.model_calls ? Object.entries(u.by_provider).map(([p, v]) => `${p} ×${v.calls}`).join(', ') : '—';

    // controls
    const connected = c.state === 'connected';
    if (window.octgAsk) window.octgAsk.sync({ connected });
    if (window.octgInbox) { window.octgInbox.sync({ connected }); window.octgInbox.setOpenCount(s.inbox || 0); }
    if ((s.inbox || 0) !== inboxOpen) setInboxCount(s.inbox || 0);
    $('run').disabled = !connected || active;
    $('taskInput').disabled = active;
    $('model').disabled = active;
    if (window.octgVoice) window.octgVoice.sync({ active }); // voice follows the same one-task-at-a-time rule as the Task box
    const bz = window.octgBiz; // Business Brain: the section and the per-task switch exist only when the agent reports the feature flag ON
    if (bz) {
      bz.sync({ connected });
      $('bizTaskRow').hidden = !bz.state.enabled;
      $('bizMode').disabled = active;
      const b = t && t.business;
      $('bizChip').hidden = !b;
      if (b) {
        const cl = b.client;
        const who = cl ? (cl.pending ? ' · Client: not selected yet' + (cl.candidates && cl.candidates.length ? ' (several match: ' + cl.candidates.join(', ') + ')' : '') : ' · Client: ' + cl.name + ' (' + (cl.reason || 'selected') + ')') : '';
        $('bizChip').textContent = 'Business Brain: ' + (b.active ? 'on' : 'off') + ' — ' + b.reason + who + (b.fills ? ' · ' + b.fills + ' field(s) filled' : '');
      }
      $('bizClient').disabled = active;
    }
    const paused = t && t.status === 'PAUSED';
    $('pause').hidden = !(active && !paused && t.status !== 'WAITING_FOR_APPROVAL');
    $('resume').hidden = !paused;
    $('stop').hidden = !active;

    // approval / waiting for user
    const pa = t && t.pending_approval;
    $('approval').hidden = !pa;
    if (pa) {
      $('approvalText').textContent = pa.description;
      $('approvalRisk').textContent = 'Risk: ' + (pa.risk.level || '?') + ((pa.risk.reasons || [])[0] ? ' — ' + pa.risk.reasons[0] : '') + (pa.reason ? ' · ' + pa.reason : '');
    }
    const waiting = t && t.status === 'WAITING_FOR_USER';
    $('waiting').hidden = !waiting;
    $('clientPick').hidden = !(waiting && t.waiting_kind === 'client_needed' && bz && bz.state.clients.length);
    if (waiting) $('waitingText').textContent = t.waiting_message || 'The agent needs your help to continue.';

    // result / error
    const res = $('result');
    const done = t && !active;
    res.hidden = !(done && (t.result || t.error));
    if (done) {
      res.className = 'result' + (t.status === 'COMPLETED' ? '' : ' bad');
      res.textContent = (t.status === 'COMPLETED' ? 'Done: ' : STATUS_LABEL[t.status] + ': ') + (t.result || t.error || '');
    }

    // restricted page warning
    const r = $('restricted');
    r.hidden = !(tab && !tab.supported);
    if (tab && !tab.supported) r.textContent = 'This Chrome page cannot be controlled by the extension. ' + (tab.restricted_reason || '');

    renderProviders(s.providers);
    if (s.toast && s.toast.ts > lastToastTs) {
      lastToastTs = s.toast.ts;
      const to = $('toast');
      to.textContent = s.toast.text;
      to.hidden = false;
      setTimeout(() => { to.hidden = true; }, 7000);
    }

    // connection form
    if (document.activeElement !== $('agentUrl')) $('agentUrl').value = c.url;
    $('token').placeholder = c.has_token ? 'token saved — paste to replace' : 'paste token';
    $('connMsg').textContent = c.error || '';
    if (c.state !== 'connected' && !c.has_token) $('conn').open = true;

    // activity: the task's own steps when there is a task, otherwise the connection/browser log
    const entries = (t ? t.activity.map((a) => ({ ts: a.ts * 1000, k: a.kind, text: a.text, ok: !/FAILED|failed|denied|Unusable/.test(a.text) })) : s.log.map((l) => ({ ts: l.ts, k: l.kind, text: l.text, ok: l.ok })))
      .filter((e) => e.ts > clearedBefore).reverse();
    $('log').replaceChildren(...entries.map((e) => {
      const li = document.createElement('li');
      if (!e.ok) li.className = 'bad';
      const time = document.createElement('time');
      time.textContent = new Date(e.ts).toLocaleTimeString([], { hour12: false });
      const k = document.createElement('span');
      k.className = 'k';
      k.textContent = e.k;
      const span = document.createElement('span');
      span.textContent = e.text;
      li.append(time, k, span);
      return li;
    }));
  }

  $('run').addEventListener('click', () => {
    const instruction = $('taskInput').value.trim();
    if (!instruction) { $('taskInput').focus(); return; }
    const m = $('model').value;
    localStorageSet('octg.model', m);
    const bizMode = $('bizTaskRow').hidden ? 'auto' : $('bizMode').value;
    const cv = $('bizTaskRow').hidden ? 'auto' : $('bizClient').value;
    ui('start_task', { instruction, provider: m === 'auto' ? null : m, ...(bizMode === 'auto' ? {} : { business: bizMode === 'on' }), ...(cv === 'auto' ? {} : { client: cv }) });
  });
  $('taskInput').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('run').click(); });
  $('approve').addEventListener('click', () => ui('approve'));
  $('deny').addEventListener('click', () => ui('deny'));
  $('pause').addEventListener('click', () => ui('pause'));
  $('resume').addEventListener('click', () => ui('resume'));
  $('ready').addEventListener('click', () => ui('resume'));
  $('clientPickBtn').addEventListener('click', () => { const id = $('clientPickSel').value; if (id) ui('select_client', { client_id: id }); });
  $('stop').addEventListener('click', () => ui('stop'));
  $('connect').addEventListener('click', () => {
    const token = $('token').value;
    port.postMessage({ type: 'configure', url: $('agentUrl').value, ...(token ? { token } : {}), enabled: true });
    $('token').value = '';
  });
  $('disconnect').addEventListener('click', () => port.postMessage({ type: 'configure', enabled: false, url: $('agentUrl').value }));
  $('clear').addEventListener('click', () => { clearedBefore = Date.now(); if (lastState) render(lastState); });

  connectPort();
})();
