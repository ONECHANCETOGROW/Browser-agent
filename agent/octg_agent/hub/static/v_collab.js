// Collaboration page: what the AI collaborator (Cloudy) asked this agent to do, what came back, and the approvals only the owner can decide.
import { api } from './api.js';
import { chip, el, emptyState, field, fmtTime, modal, notice, toast } from './ui.js';
import { loopsCard } from './v_loops.js';
import { sourcesCard } from './v_sources.js';

const AP_KIND = { proposed: ['Waiting for you', 'warn'], approved: ['Approved', 'ok'], denied: ['Denied', 'bad'], executed: ['Done (not yet verified)', 'warn'], verified: ['Done and verified', 'ok'], failed: ['Failed', 'bad'], withdrawn: ['Withdrawn', 'gray'] };
const JOB_KIND = { succeeded: 'ok', failed: 'bad', timed_out: 'bad', cancelled: 'gray', rejected: 'bad', queued: 'warn', running: 'warn', awaiting_approval: 'warn' };
const apChip = (s) => { const m = AP_KIND[s] || [s, 'gray']; return chip(m[0], m[1]); };

function detailsBlock(obj) {
  const pre = el('pre', { class: 'small', style: 'white-space:pre-wrap;max-height:320px;overflow:auto;background:var(--bg-soft,#f6f6f4);padding:10px;border-radius:8px', text: JSON.stringify(obj || {}, null, 2) });
  return pre;
}

async function decide(a, approve, ctx) {
  const note = el('textarea', { rows: 3, placeholder: approve ? 'Optional note (e.g. "go ahead after 6pm")' : 'Why not? (optional)', attrs: { 'aria-label': 'Note' } });
  const pin = approve ? el('input', { type: 'password', inputMode: 'numeric', autocomplete: 'off', maxLength: 12, attrs: { 'aria-label': 'Your PIN' } }) : null;
  const pinErr = el('div', { class: 'err' });
  const body = el('div', { class: 'stack' },
    el('p', null, el('b', { text: a.title })),
    a.description ? el('p', { text: a.description }) : null,
    el('p', { class: 'muted small', text: `Kind: ${a.action_kind} · Client: ${a.client_name || a.client_id || '—'} · Requested by ${a.requested_by} on ${fmtTime(a.requested_at)}` }),
    detailsBlock(a.details), note, pin ? field('Your PIN (required to approve)', pin, 'Approving needs the PIN you use to unlock the Hub. No PIN yet? Set one on the Security page.') : null, pinErr,
    notice(approve ? 'Approving lets the collaborator carry out exactly this action. It will report back what it did, and this agent will check the result itself.' : 'Denying stops this action. Nothing else is affected.', approve ? 'warn' : ''));
  const r = await modal({ title: approve ? 'Approve this action?' : 'Deny this action?', body, wide: true, buttons: [{ label: 'Cancel', value: false }, { label: approve ? 'Approve' : 'Deny', value: true, kind: approve ? 'primary' : 'danger solid',
    validate: () => { pinErr.textContent = ''; if (approve && !/^[0-9]{4,12}$/.test(pin.value)) { pinErr.textContent = 'Type your PIN (4 to 12 digits) to approve.'; return false; } return true; } }] });
  if (!r) return;
  try {
    await api.post(`/approvals/${encodeURIComponent(a.approval_id)}/${approve ? 'approve' : 'deny'}`, { note: note.value, pin: pin ? pin.value : undefined });
    toast(approve ? 'Approved.' : 'Denied.', approve ? 'ok' : 'info');
    ctx.rerender();
  } catch (e) { toast(e.message, 'bad'); }
}

export async function viewCollab(ctx) {
  const [d, lp] = [await api.get('/collab'), await api.get('/loops')];
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'AI collaboration' }),
    el('p', { text: 'Work that your AI collaborator (Cloudy) delegated to this agent: research requests, their results and evidence, and the high-impact actions waiting for your decision. Only you can approve; the collaborator can only ask.' }))));
  page.append(loopsCard(lp.loops, ctx));
  const sc = await sourcesCard(ctx);
  if (sc) page.append(sc);
  const pending = d.approvals.filter((a) => a.status === 'proposed');
  const ap = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: `Approvals ${pending.length ? '(' + pending.length + ' waiting)' : ''}` })));
  if (!d.approvals.length) ap.append(emptyState('Nothing to approve', 'Production deploys, publishing and other high-impact actions appear here before they happen.'));
  else ap.append(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Requested', 'What', 'Client', 'Kind', 'State', ''].map((h) => el('th', { text: h })))),
    el('tbody', null, d.approvals.map((a) => el('tr', null, el('td', { class: 'nowrap', text: fmtTime(a.requested_at) }), el('td', null, el('b', { text: a.title }), a.decision_note ? el('div', { class: 'muted small', text: 'Note: ' + a.decision_note }) : null),
      el('td', { text: a.client_name || '—' }), el('td', { text: a.action_kind }), el('td', null, apChip(a.status)),
      el('td', { class: 'nowrap' }, a.status === 'proposed' ? [el('button', { type: 'button', class: 'small primary', text: 'Review & approve', on: { click: () => decide(a, true, ctx) } }), ' ',
        el('button', { type: 'button', class: 'small', text: 'Deny', on: { click: () => decide(a, false, ctx) } })] : el('button', { type: 'button', class: 'small', text: 'Details', on: { click: () => modal({ title: a.title, wide: true, body: detailsBlock(a) }) } }))))))));
  page.append(ap);
  const tasks = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Collaboration tasks' })));
  if (!d.tasks.length) tasks.append(emptyState('No tasks yet', 'When Cloudy starts a task (for example an SEO audit for a client) it is listed here with every request it made.'));
  else tasks.append(el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Started', 'Task', 'Client', 'Requests', 'Records', 'State'].map((h) => el('th', { text: h })))),
    el('tbody', null, d.tasks.map((t) => el('tr', null, el('td', { class: 'nowrap', text: fmtTime(t.created_at) }), el('td', null, el('b', { text: t.title }), t.outcome ? el('div', { class: 'muted small', text: t.outcome }) : null),
      el('td', { text: t.client_name || '—' }), el('td', { text: `${t.jobs}${t.jobs_failed ? ' (' + t.jobs_failed + ' failed)' : ''}` }),
      el('td', null, t.artifacts.slice(0, 6).map((a) => el('span', { class: 'tag', title: a.title, text: a.kind }))), el('td', null, chip(t.status, t.status === 'completed' ? 'ok' : t.status === 'open' ? 'warn' : 'bad'))))))));
  page.append(tasks);
  const reqs = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Recent requests' })));
  reqs.append(d.requests.length ? el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['When', 'Capability', 'From', 'State'].map((h) => el('th', { text: h })))),
    el('tbody', null, d.requests.map((j) => el('tr', null, el('td', { class: 'nowrap', text: fmtTime(j.created_at) }), el('td', null, el('code', { text: j.capability })), el('td', { text: j.caller }),
      el('td', null, chip(j.status, JOB_KIND[j.status] || 'gray'))))))) : emptyState('No requests yet', ''));
  page.append(reqs);
  ctx.mount(page);
}

export function campaignsCard(c) {
  const card = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'SEO campaigns' })));
  const camps = c.campaigns || [];
  if (!camps.length) { card.append(emptyState('No campaign yet', 'Ask Cloudy to set up an SEO campaign for this client; it appears here with its keywords, runs and baseline.')); return card; }
  for (const k of camps) {
    const outside = k.keywords.filter((x) => x.area_status === 'outside');
    card.append(el('div', { style: 'padding:12px 18px', class: 'stack' },
      el('div', null, el('b', { text: k.name }), ' ', chip(k.status, k.status === 'baseline_set' || k.status === 'active' ? 'ok' : 'warn'), ' ', el('span', { class: 'muted small', text: `${k.website || ''} · ${k.language}-${k.country} · research: ${k.research_status || '—'} · baseline: ${k.baseline_date ? fmtTime(k.baseline_date) : 'not yet'}` })),
      k.objective ? el('p', { class: 'small', text: k.objective }) : null,
      el('div', null, k.keywords.slice(0, 40).map((x) => el('span', { class: 'tag', title: x.area_status, text: x.keyword + (x.location ? ' · ' + x.location : '') }))),
      outside.length ? notice(`${outside.length} keyword location(s) are outside this client's known service areas: confirm them before they are researched.`, 'warn') : null,
      k.runs.length ? el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, ['Run', 'Kind', 'State', 'Summary'].map((h) => el('th', { text: h })))),
        el('tbody', null, k.runs.slice(0, 12).map((r) => el('tr', null, el('td', { class: 'nowrap', text: fmtTime(r.started_at) }), el('td', { text: r.kind }), el('td', { text: r.status }), el('td', { class: 'small', text: r.summary || '' })))))) : null));
  }
  return card;
}
