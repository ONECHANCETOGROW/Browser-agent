// Inbox: what the AI asked you to do ("go here, do this, tell me what you see") and your answers. The Chrome side panel shows the same requests; only the panel can attach the tab you are on.
import { api } from './api.js';
import { chip, el, emptyState, fmtTime, notice, toast } from './ui.js';

export async function viewInbox(ctx) {
  const holder = el('div', { class: 'stack' });
  const picked = {};
  const texts = {};

  async function load() {
    let d;
    try { d = await api.get('/inbox'); } catch (e) { if (e.code !== 'LOCKED') holder.replaceChildren(notice(e.message, 'bad')); return; }
    if (ctx.stale()) return;
    paint(d.requests);
    ctx.refreshBadges();
  }

  async function reply(r) {
    try {
      await api.post(`/inbox/${encodeURIComponent(r.or_id)}/reply`, { text: texts[r.or_id] || '', choice: picked[r.or_id] || '' });
      toast('Answer sent. The AI can read it now.', 'ok');
      delete picked[r.or_id]; delete texts[r.or_id];
      load();
    } catch (e) { toast(e.message, e.code === 'EMPTY_REPLY' ? 'warn' : 'bad'); }
  }

  function card(r) {
    const ta = el('textarea', { rows: 3, placeholder: 'What do you see? (please do not type passwords here)', maxLength: 6000, attrs: { 'aria-label': 'Your answer' } });
    ta.value = texts[r.or_id] || '';
    ta.addEventListener('input', () => { texts[r.or_id] = ta.value; });
    const choices = (r.choices || []).length ? el('div', { class: 'actions' }, r.choices.map((c) => el('button', { type: 'button', class: picked[r.or_id] === c ? 'primary' : '', text: c, attrs: { 'aria-pressed': String(picked[r.or_id] === c) },
      on: { click: () => { picked[r.or_id] = picked[r.or_id] === c ? '' : c; load(); } } }))) : null;
    return el('div', { class: 'card stack' },
      el('div', { class: 'head' }, el('div', null, el('h2', { text: r.title }), el('p', { class: 'muted small', text: [r.client_name, r.related, fmtTime(r.created_at)].filter(Boolean).join(' · ') })), chip('Waiting for you', 'warn')),
      el('ol', null, r.steps.map((s) => el('li', { text: s }))),
      r.question ? el('p', null, el('b', { text: r.question })) : null,
      choices, ta,
      r.want_page ? el('p', { class: 'muted small', text: 'The AI would also like to see the page you are on: open the Chrome side panel (Inbox tab) and press “Attach this tab’s text and a screenshot”.' }) : null,
      el('div', { class: 'actions' }, el('button', { type: 'button', class: 'primary', text: 'Send answer', on: { click: () => reply(r) } }),
        el('button', { type: 'button', text: 'Skip', title: 'Dismiss this request without answering', on: { click: async () => { try { await api.post(`/inbox/${encodeURIComponent(r.or_id)}/dismiss`); load(); } catch (e) { toast(e.message, 'bad'); } } } })));
  }

  function paint(requests) {
    const open = requests.filter((r) => r.status === 'open');
    const done = requests.filter((r) => r.status !== 'open');
    const kids = [];
    if (!open.length) kids.push(el('div', { class: 'card' }, emptyState('Nothing waiting for you', 'When the AI needs you to do or look at something, it appears here and in the Chrome side panel (with a badge on the extension icon).')));
    for (const r of open) kids.push(card(r));
    if (done.length) {
      kids.push(el('div', { class: 'card' }, el('h2', { text: 'Earlier' }), ...done.slice(0, 20).map((r) => el('div', { class: 'stack', style: 'padding:8px 0;border-top:1px solid var(--line)' },
        el('div', null, el('b', { text: r.title }), ' ', chip(r.status === 'answered' ? 'Answered' : r.status === 'dismissed' ? 'Skipped' : 'Withdrawn', r.status === 'answered' ? 'ok' : 'gray'),
          el('span', { class: 'muted small', text: `  ${fmtTime(r.answered_at || r.created_at)}${r.answered_via ? ' · via ' + r.answered_via : ''}` })),
        r.reply_text || r.reply_choice ? el('div', { class: 'small', style: 'white-space:pre-wrap', text: [r.reply_choice, r.reply_text].filter(Boolean).join(' — ') }) : null,
        (r.attachments || []).length ? el('div', { class: 'muted small', text: 'Attached: ' + r.attachments.map((a) => a.kind === 'page_text' ? `page text (${a.chars} characters)` : 'screenshot').join(', ') }) : null))));
    }
    holder.replaceChildren(...kids);
  }

  ctx.mount(el('div', { class: 'stack' }, el('h1', { text: 'Inbox' }),
    el('p', { class: 'muted', text: 'Instructions from the AI that only you can do or see, for example “open this page and tell me what the error says”. Answer here or in the Chrome side panel. The AI reads your answer from its own side; it cannot approve or change anything because of it.' }), holder));
  await load();
}
