// Ask: type a plain sentence ("go to Google and search for painters in Alabama ... tell me what you find"), the agent runs it (read-only) and answers here in plain language.
// Nothing is stored in the browser: the conversation is the agent's own record, so it is still here after a reload.
import { api } from './api.js';
import { chip, el, emptyState, fmtTime, icon, notice, toast } from './ui.js';

const RUNNING = ['queued', 'running', 'awaiting_approval'];
const EXAMPLE = 'Go to Google and search for painters in Alabama, search for best painters in Alabama, and search for affordable painters in Alabama. Tell me what you find.';

function answer(a, ctx, refresh) {
  const box = el('div', { class: 'ask-msg agent' }, el('div', { class: 'who', text: 'Agent' }));
  if (RUNNING.includes(a.status)) {
    box.append(el('div', { class: 'ask-working' }, el('span', { class: 'spin', attrs: { 'aria-hidden': 'true' } }), el('span', { text: a.progress ? `Working: ${a.progress}` : 'Starting…' })),
      el('p', { class: 'muted small', text: 'Searches are spaced 20 to 45 seconds apart on purpose, so Google does not block this browser. A few searches take about a minute or two.' }),
      el('div', { class: 'actions' }, el('button', { type: 'button', class: 'small', text: 'Cancel', on: { click: async () => { try { await api.post(`/asks/${encodeURIComponent(a.ask_id)}/cancel`); toast('Cancelling…', 'ok'); refresh(); } catch (e) { toast(e.message, 'bad'); } } } })));
    return box;
  }
  if (a.status !== 'succeeded') {
    const why = a.error_code === 'EXTENSION_NOT_CONNECTED' ? 'Chrome is not connected to the agent. Open Chrome with the OCTG extension, wait a few seconds, then send the question again.' : (a.error || 'The run did not finish.');
    box.append(notice(why, 'bad'));
    return box;
  }
  const label = { complete: ['Done', 'ok'], partial: ['Partly done', 'warn'], blocked: ['Stopped: Google asked for a human check', 'bad'] }[a.outcome] || ['Done', 'ok'];
  box.append(el('div', { class: 'head-row' }, chip(label[0], label[1]), a.finished_at ? el('span', { class: 'muted small', text: fmtTime(a.finished_at) }) : null), el('div', { class: 'report', text: a.report || '(no answer)' }));
  const actions = el('div', { class: 'actions' });
  actions.append(el('button', { type: 'button', class: 'small', text: 'Copy answer', on: { click: async () => { try { await navigator.clipboard.writeText(a.report || ''); toast('Copied.', 'ok'); } catch (_) { toast('Could not copy: select the text instead.', 'warn'); } } } }));
  if (a.remaining && a.remaining.length) {
    actions.append(el('button', { type: 'button', class: 'small primary', text: `Try again (${a.remaining.length} search${a.remaining.length === 1 ? '' : 'es'} left)`, title: 'Do this after you have completed Google’s check yourself in Chrome',
      on: { click: async () => {
        try { await api.post('/asks', { text: a.text, client_id: a.client_id, queries: a.remaining, location: a.location || '' }); toast('Running the remaining searches…', 'ok'); refresh(); } catch (e) { toast(e.message, 'bad'); }
      } } }));
  }
  box.append(actions);
  return box;
}

export async function viewAsk(ctx) {
  const sc = ctx.scopes();
  const clients = sc.clients.filter((c) => !c.archived);
  const text = el('textarea', { rows: 4, placeholder: EXAMPLE, attrs: { 'aria-label': 'What should the agent do?' }, maxLength: 4000 });
  const client = el('select', { attrs: { 'aria-label': 'Which client is this for?' } }, clients.map((c) => el('option', { value: c.id, text: c.name })));
  client.value = clients.some((c) => c.id === sc.default_scope) ? sc.default_scope : (clients[0] ? clients[0].id : '');
  const where = el('input', { type: 'text', placeholder: 'Optional, e.g. Alabama,United States', maxLength: 120, attrs: { 'aria-label': 'Search as if in this place (optional)' } });
  const reply = el('div');
  const feed = el('div', { class: 'stack' });
  const send = el('button', { type: 'button', class: 'primary', text: 'Send' });
  let timer = null;

  const paint = (asks) => {
    feed.replaceChildren(...(asks.length ? asks.map((a) => el('div', { class: 'card ask' },
      el('div', { class: 'ask-msg you' }, el('div', { class: 'who' }, 'You', el('span', { class: 'muted small', text: `  ·  ${a.client_name || ''}  ·  ${fmtTime(a.asked_at)}` })), el('div', { class: 'said', text: a.text || (a.queries || []).join(', ') })),
      answer(a, ctx, refresh))) : [emptyState('Nothing asked yet', 'Type a sentence above, for example: ' + EXAMPLE)]));
  };
  async function refresh() {
    if (ctx.stale()) { clearTimeout(timer); return; }
    let asks = [];
    try { asks = (await api.get('/asks')).asks; } catch (e) { if (e.code !== 'LOCKED') feed.replaceChildren(notice(e.message, 'bad')); return; }
    if (ctx.stale()) return;
    paint(asks);
    clearTimeout(timer);
    if (asks.some((a) => RUNNING.includes(a.status))) timer = setTimeout(refresh, 3000);
  }
  const go = async () => {
    reply.replaceChildren();
    if (!text.value.trim()) { reply.replaceChildren(notice('Write what you want the agent to do first.', 'warn')); return; }
    if (!client.value) { reply.replaceChildren(notice('Add a client first: the agent needs to know whose website to look for.', 'warn')); return; }
    send.disabled = true;
    try {
      const r = await api.post('/asks', { text: text.value, client_id: client.value, location: where.value.trim() });
      if (!r.understood) { reply.replaceChildren(el('div', { class: 'ask-msg agent' }, el('div', { class: 'who', text: 'Agent' }), el('p', { text: r.reply }),
        el('p', { class: 'muted small', text: 'For now I can run Google searches from here. Write something like: search for painters in Alabama.' }))); return; }
      reply.replaceChildren(notice(`I understood: search Google for ${r.queries.map((q) => `“${q}”`).join(', ')}.${r.notes && r.notes.length ? ' ' + r.notes.join(' ') : ''}`, 'ok'));
      text.value = '';
      refresh();
    } catch (e) { reply.replaceChildren(notice(e.message, e.code === 'BUSY' ? 'warn' : 'bad')); } finally { send.disabled = false; }
  };
  send.addEventListener('click', go);
  text.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } });
  const page = el('div', { class: 'stack' },
    el('h1', { text: 'Ask the agent' }),
    el('p', { class: 'muted', text: 'Tell the agent what to look up in plain words. It searches in your Chrome (read-only: it never clicks, types or changes anything), then answers here: what it searched, whether your client’s website is on the first page, who is, and the map listings.' }),
    el('div', { class: 'card stack' }, el('div', { class: 'field' }, el('label', { text: 'What should I do?' }), text, el('div', { class: 'hint', text: 'Ctrl+Enter sends. Up to 10 searches at a time.' })),
      el('div', { class: 'ask-row' }, el('div', { class: 'field' }, el('label', { text: 'For which client?' }), client, el('div', { class: 'hint', text: 'Its website is what I look for in the results.' })),
        el('div', { class: 'field' }, el('label', { text: 'Search as if in…' }), where, el('div', { class: 'hint', text: 'Leave empty to use what Chrome shows you.' }))),
      el('div', { class: 'actions' }, send, el('span', { class: 'muted small' }, icon('search'), ' Only Google searches for now.')), reply),
    feed);
  ctx.mount(page);
  await refresh();
}
