// OCTG side panel: "Ask" (type a sentence, the agent searches Google read-only and answers in plain language) and "Inbox" (instructions the AI asked YOU to do, with your answers).
//
// Rules this file keeps:
//  * It holds no data: nothing is written to storage and nothing is kept between openings; the agent's own record is shown again each time.
//  * All text is inserted with textContent (never innerHTML), so nothing the agent, the AI or a page contains can become markup or script.
//  * It cannot approve, deny or change anything. It sends a sentence to search for, quick answers, and (only when you press the button) the text and a screenshot of the tab you are on.
//
// The DOM and the transport are injected so the logic is unit-tested without a browser.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; return; }
  root.OCTG_ASK_UI = api;
  const d = root.document;
  if (!d || !d.getElementById('askBody')) return;
  const send = (cmd, extra) => { if (root.octgSend) root.octgSend(cmd, extra); };
  const timers = { setTimeout: root.setTimeout.bind(root), clearTimeout: root.clearTimeout.bind(root) };
  const copy = (t) => (root.navigator && root.navigator.clipboard ? root.navigator.clipboard.writeText(t) : Promise.reject(new Error('no clipboard')));
  const openConnection = () => { if (root.octgOpenConnection) root.octgOpenConnection(); };
  root.octgAsk = api.createAskView({ doc: d, body: d.getElementById('askBody'), send, copy, openConnection, ...timers });
  root.octgInbox = api.createInboxView({ doc: d, body: d.getElementById('inboxBody'), send, openConnection, currentTab: () => (root.octgCurrentTab ? root.octgCurrentTab() : null), onCount: (n) => { if (root.octgInboxCount) root.octgInboxCount(n); }, ...timers });
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const RUNNING = ['queued', 'running', 'awaiting_approval'];
  const EXAMPLE = 'Go to Google and search for painters in Alabama, search for best painters in Alabama, and search for affordable painters in Alabama. Tell me what you find.';
  const POLL_MS = 3000;

  function helpers(doc) {
    const el = (tag, cls, text) => { const e = doc.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = String(text); return e; };
    const btn = (label, onClick, cls) => { const b = el('button', cls || '', label); b.type = 'button'; b.addEventListener('click', onClick); return b; };
    return { el, btn };
  }
  const when = (ts) => { try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch (_) { return ''; } };

  function requester(o, prefix) {
    const pending = new Map();
    let n = 0;
    return {
      request(command, extra, then) { const id = prefix + (++n); pending.set(id, then || null); o.send(command, { ...(extra || {}), request_id: id }); return id; },
      onResult(msg) { const fn = pending.get(msg.request_id); if (!pending.has(msg.request_id)) return; pending.delete(msg.request_id); if (fn) fn(msg); },
    };
  }

  // ================================================================================================= Ask
  function createAskView(o) {
    const { el, btn } = helpers(o.doc);
    const rq = requester(o, 'ask_');
    const st = { clients: [], connected: false, visible: false, asks: [], timer: null, loaded: false };
    const text = el('textarea'); text.rows = 4; text.placeholder = EXAMPLE; text.maxLength = 4000;
    const client = el('select');
    const where = el('input'); where.type = 'text'; where.placeholder = 'Optional, e.g. Alabama,United States';
    const reply = el('div');
    const feed = el('div', 'ask-feed');
    const sendBtn = btn('Send', () => go());
    const label = (t) => el('label', '', t);
    const sendRow = el('div', 'row');
    sendRow.append(sendBtn);
    // shown whenever the panel is not connected to the agent: the client list and the answers come from the agent, so without it nothing can load
    const offline = el('div', 'ask-note warn');
    offline.append(el('span', '', 'Not connected to the agent, so your clients and answers cannot load. '), btn('Open connection settings', () => { if (o.openConnection) o.openConnection(); }, 'link'));
    sendBtn.disabled = true;
    o.body.replaceChildren(
      offline,
      el('p', 'muted', 'Tell the agent what to look up, in plain words. It searches Google in this Chrome (read-only: it never clicks, types or changes anything) and answers here.'),
      label('What should I do?'), text, label('For which client? (its website is what I look for)'), client, label('Search as if in… (optional)'), where,
      sendRow, reply, feed);

    function say(node) { reply.replaceChildren(...(node ? [node] : [])); }
    function note(textValue, kind) { return el('div', 'ask-note ' + (kind || ''), textValue); }

    function paintClients() {
      if (!st.connected) { const op = el('option', '', '(not connected)'); op.value = ''; client.replaceChildren(op); return; }
      if (!st.loaded) { const op = el('option', '', 'Loading your clients…'); op.value = ''; client.replaceChildren(op); return; }
      if (!st.clients.length) { const op = el('option', '', 'No clients yet: add one in the Knowledge Hub'); op.value = ''; client.replaceChildren(op); return; }
      client.replaceChildren(...st.clients.map((c) => { const op = el('option', '', c.name); op.value = c.id; return op; }));
      if (st.defaultClient && st.clients.some((c) => c.id === st.defaultClient)) client.value = st.defaultClient;
    }
    function paintConnection() { offline.hidden = st.connected; sendBtn.disabled = !st.connected; paintClients(); }

    function answer(a) {
      const box = el('div', 'ask-agent');
      box.append(el('div', 'ask-who', 'Agent'));
      if (RUNNING.includes(a.status)) {
        box.append(el('div', 'ask-working', a.progress ? 'Working: ' + a.progress : 'Starting…'), el('div', 'muted', 'Searches are spaced out on purpose so Google does not block this browser. It takes a minute or two.'),
          btn('Cancel', () => rq.request('ask_cancel', { ask_id: a.ask_id }, () => refresh()), 'secondary'));
        return box;
      }
      if (a.status !== 'succeeded') {
        const why = a.error_code === 'EXTENSION_NOT_CONNECTED' ? 'Chrome is not connected to the agent. Open Connection below, press Connect, then send again.' : (a.error || 'The run did not finish.');
        box.append(note(why, 'bad'));
        return box;
      }
      const stateText = { complete: 'Done', partial: 'Partly done', blocked: 'Stopped: Google asked for a human check' }[a.outcome] || 'Done';
      box.append(el('div', 'ask-state ' + (a.outcome === 'blocked' ? 'bad' : a.outcome === 'partial' ? 'warn' : 'ok'), stateText + (a.finished_at ? ' · ' + when(a.finished_at) : '')),
        el('div', 'ask-report', a.report || '(no answer)'));
      const row = el('div', 'row');
      row.append(btn('Copy answer', () => { Promise.resolve(o.copy ? o.copy(a.report || '') : null).then(() => say(note('Copied.', 'ok')), () => say(note('Could not copy: select the text instead.', 'warn'))); }, 'secondary'));
      if (a.remaining && a.remaining.length) {
        row.append(btn(`Try again (${a.remaining.length} left)`, () => rq.request('ask_send', { text: a.text, client_id: a.client_id, queries: a.remaining, location: a.location || '' }, (m) => {
          if (!m.ok) say(note(m.error || 'That did not work.', m.code === 'BUSY' ? 'warn' : 'bad')); else say(note('Running the remaining searches…', 'ok'));
          refresh();
        })));
      }
      box.append(row);
      return box;
    }

    function paint() {
      feed.replaceChildren(...(st.asks.length ? st.asks.map((a) => { const card = el('div', 'ask-card'); card.append(el('div', 'ask-you', (a.client_name ? a.client_name + ' · ' : '') + when(a.asked_at)), el('div', 'ask-said', a.text || (a.queries || []).join(', ')), answer(a)); return card; }) : [el('div', 'muted', 'Nothing asked yet.')]));
    }

    function schedule() {
      if (st.timer) { o.clearTimeout(st.timer); st.timer = null; }
      if (st.visible && st.connected && st.asks.some((a) => RUNNING.includes(a.status))) st.timer = o.setTimeout(() => { st.timer = null; refresh(); }, POLL_MS);
    }

    function refresh() {
      if (!st.connected) return;
      rq.request('ask_list', {}, (m) => { if (m.ok) { st.asks = m.panel.asks || []; paint(); schedule(); } });
    }

    function go() {
      say(null);
      if (!st.connected) { say(note('Not connected to the agent. Press “Open connection settings” above, connect, then send again.', 'warn')); return; }
      if (!text.value.trim()) { say(note('Write what you want the agent to do first.', 'warn')); return; }
      if (!client.value) { say(note(st.loaded ? 'There is no client to search for yet: add one in the Knowledge Hub first (the agent needs to know whose website to look for).' : 'Your clients are still loading: try again in a moment.', 'warn')); return; }
      sendBtn.disabled = true;
      rq.request('ask_send', { text: text.value, client_id: client.value, location: where.value.trim() }, (m) => {
        sendBtn.disabled = !st.connected;
        if (!m.ok) { say(note(m.error || 'That did not work.', m.code === 'BUSY' ? 'warn' : 'bad')); return; }
        const p = m.panel;
        if (!p.understood) { say(note(p.reply + ' For now I can run Google searches from here. Write something like: search for painters in Alabama.', 'warn')); return; }
        say(note('I understood: search Google for ' + p.queries.map((q) => '“' + q + '”').join(', ') + '.' + (p.notes && p.notes.length ? ' ' + p.notes.join(' ') : ''), 'ok'));
        text.value = '';
        refresh();
      });
    }

    function load() {
      if (!st.connected) return;
      if (!st.loaded) rq.request('ask_clients', {}, (m) => { if (m.ok) { st.loaded = true; st.clients = m.panel.clients || []; st.defaultClient = m.panel.default; paintClients(); } else say(note(m.error || 'The client list could not be loaded.', 'bad')); });
      refresh();
    }
    paintConnection();

    return {
      state: st, onResult: (m) => rq.onResult(m), go,
      show() { st.visible = true; load(); },
      hide() { st.visible = false; if (st.timer) { o.clearTimeout(st.timer); st.timer = null; } },
      sync({ connected }) {
        const was = st.connected;
        st.connected = !!connected;
        if (!st.connected) { if (st.timer) { o.clearTimeout(st.timer); st.timer = null; } st.loaded = false; }
        if (was !== st.connected) { paintConnection(); if (st.connected && st.visible) load(); }
      },
    };
  }

  // ================================================================================================= Inbox
  function createInboxView(o) {
    const { el, btn } = helpers(o.doc);
    const rq = requester(o, 'inbox_');
    const st = { connected: false, visible: false, requests: [], open: 0, drafts: {}, choice: {}, texts: {}, attaching: {}, notes: {} };

    function count(n) { st.open = n; if (o.onCount) o.onCount(n); }

    function refresh() {
      if (!st.connected) return;
      rq.request('inbox_list', {}, (m) => { if (m.ok) { st.requests = m.panel.requests || []; count(m.panel.open || 0); paint(); } });
    }

    function card(r) {
      const box = el('div', 'inbox-card');
      const head = el('div', 'inbox-head');
      head.append(el('b', '', r.title), el('span', 'muted', when(r.created_at)));
      box.append(head);
      if (r.client_name) box.append(el('div', 'muted', 'About: ' + r.client_name));
      if (r.related) box.append(el('div', 'muted', 'Related: ' + r.related));
      const ol = el('ol', 'inbox-steps');
      for (const s of r.steps) ol.append(el('li', '', s));
      box.append(ol);
      if (r.question) box.append(el('div', 'inbox-question', r.question));
      const msg = el('div', 'ask-note ' + ((st.notes[r.or_id] || {}).kind || ''), (st.notes[r.or_id] || {}).text || '');
      msg.hidden = !st.notes[r.or_id];
      const ta = el('textarea'); ta.rows = 3; ta.placeholder = 'What do you see? (please do not type passwords here)'; ta.value = st.texts[r.or_id] || '';
      ta.addEventListener('input', () => { st.texts[r.or_id] = ta.value; });
      if (r.choices && r.choices.length) {
        const row = el('div', 'row inbox-choices');
        for (const c of r.choices) {
          const b = btn(c, () => { st.choice[r.or_id] = st.choice[r.or_id] === c ? '' : c; paint(); }, st.choice[r.or_id] === c ? '' : 'secondary');
          b.setAttribute('aria-pressed', st.choice[r.or_id] === c ? 'true' : 'false');
          row.append(b);
        }
        box.append(row);
      }
      box.append(ta);
      // optional: what the owner is looking at
      const draft = st.drafts[r.or_id];
      const tab = o.currentTab ? o.currentTab() : null;
      if (draft) {
        const line = el('div', 'inbox-attached');
        line.append(el('span', '', `Attached: ${draft.title || draft.url || 'this tab'} · ${draft.chars} characters of text${draft.screenshot ? ' + a screenshot' : ''}`),
          btn('Remove', () => { delete st.drafts[r.or_id]; paint(); }, 'link'));
        box.append(line);
      } else if (st.attaching[r.or_id]) {
        box.append(el('div', 'muted', 'Reading this tab…'));
      } else {
        const can = !!(tab && tab.supported !== false && tab.tab_id !== undefined && tab.tab_id !== null);
        const b = btn('Attach this tab’s text and a screenshot', () => attach(r), 'link');
        b.disabled = !can || !st.connected;
        box.append(b, el('div', 'muted', can ? 'Only the tab you are on, and only when you press this. Card numbers and secrets are removed from the text; the screenshot is not edited.' : 'Open the page itself first: Chrome’s own pages cannot be read.'));
      }
      const actions = el('div', 'row');
      actions.append(btn('Send answer', () => reply(r)), btn('Skip', () => rq.request('inbox_dismiss', { or_id: r.or_id }, (m) => { if (!m.ok) setNote(r.or_id, m.error, 'bad'); refresh(); }), 'secondary'));
      box.append(msg, actions);
      return box;
    }

    function setNote(id, text, kind) { st.notes[id] = text ? { text, kind: kind || '' } : undefined; if (!text) delete st.notes[id]; paint(); }

    function attach(r) {
      const tab = o.currentTab ? o.currentTab() : null;
      if (!tab || tab.tab_id === undefined || tab.tab_id === null) { setNote(r.or_id, 'There is no tab to attach.', 'warn'); return; }
      st.attaching[r.or_id] = true;
      setNote(r.or_id, '');
      rq.request('inbox_attach', { or_id: r.or_id, tab_id: tab.tab_id }, (m) => {
        delete st.attaching[r.or_id];
        if (!m.ok) { setNote(r.or_id, m.error || 'The tab could not be read.', 'bad'); return; }
        st.drafts[r.or_id] = m.panel.attachment;
        paint();
      });
      paint();
    }

    function reply(r) {
      const draft = st.drafts[r.or_id];
      const body = { or_id: r.or_id, text: st.texts[r.or_id] || '', choice: st.choice[r.or_id] || '', attachments: draft ? [draft.att_id] : [] };
      if (!body.text.trim() && !body.choice && !draft) { setNote(r.or_id, 'Write what you see, press a quick answer, or attach the tab first.', 'warn'); return; }
      rq.request('inbox_reply', body, (m) => {
        if (!m.ok) { setNote(r.or_id, m.error || 'That did not work.', 'bad'); return; }
        delete st.drafts[r.or_id]; delete st.choice[r.or_id]; delete st.texts[r.or_id]; delete st.notes[r.or_id];
        count(m.panel.open || 0);
        refresh();
      });
    }

    function paint() {
      const open = st.requests.filter((r) => r.status === 'open');
      const done = st.requests.filter((r) => r.status !== 'open').slice(0, 8);
      const kids = [];
      if (!st.connected) {
        const off = el('div', 'ask-note warn');
        off.append(el('span', '', 'Not connected to the agent, so requests cannot load. '), btn('Open connection settings', () => { if (o.openConnection) o.openConnection(); }, 'link'));
        kids.push(off);
      }
      if (st.connected && !open.length) kids.push(el('div', 'inbox-empty', 'Nothing waiting for you. When the AI needs you to do or look at something, it appears here with a badge on the extension icon.'));
      for (const r of open) kids.push(card(r));
      if (done.length) {
        const det = el('details', 'inbox-done');
        det.append(el('summary', '', `Earlier (${done.length})`));
        for (const r of done) {
          const line = el('div', 'inbox-old');
          line.append(el('b', '', r.title), el('span', 'muted', ` · ${r.status}${r.answered_at ? ' · ' + when(r.answered_at) : ''}`));
          if (r.reply_text || r.reply_choice) line.append(el('div', '', [r.reply_choice, r.reply_text].filter(Boolean).join(' — ').slice(0, 300)));
          det.append(line);
        }
        kids.push(det);
      }
      o.body.replaceChildren(...kids);
    }

    paint();
    return {
      state: st, onResult: (m) => rq.onResult(m), refresh,
      show() { st.visible = true; refresh(); },
      hide() { st.visible = false; },
      setOpenCount(n) { if (n !== st.open) { st.open = n; if (st.visible) refresh(); } },
      sync({ connected }) { const was = st.connected; st.connected = !!connected; if (st.connected && !was) refresh(); if (!st.connected) paint(); },
    };
  }

  return { createAskView, createInboxView, EXAMPLE };
});
