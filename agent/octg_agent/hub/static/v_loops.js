// Improvement loops: what each research -> plan -> implement -> wait -> verify cycle is doing, what it is waiting for, how far it is from its goal, and the owner's controls.
import { api } from './api.js';
import { chip, confirmDialog, el, emptyState, fmtTime, modal, notice, toast } from './ui.js';

const STATUS = { active: ['Running', 'ok'], paused: ['Paused', 'gray'], blocked: ['Blocked', 'bad'], goal_reached: ['Goal reached', 'ok'], stopped: ['Stopped', 'gray'], exhausted: ['Ended: goal not reached', 'warn'] };
const PHASE = { measuring: 'collecting measurements', awaiting_plan: "waiting for the collaborator's plan", awaiting_implementation: 'waiting for the changes to go live', waiting: 'waiting for the re-check date', done: 'finished' };
const VERDICT = { met_confirmed: ['Reached (confirmed)', 'ok'], met_unconfirmed: ['Reached once: confirming', 'warn'], not_met: ['Not yet', 'gray'], insufficient: ['No usable data yet', 'warn'] };
const CHANGE = { improved: ['Improved', 'ok'], worse: ['Worse', 'bad'], same: ['Same', 'gray'], new: ['First observation', 'info'], no_data: ['No data', 'warn'] };
const END = { max_rounds: 'the round limit', deadline: 'the deadline', budget: 'the request budget', goal: 'the goal', manual: 'a manual stop' };

const statusChip = (s) => { const m = STATUS[s] || [s, 'gray']; return chip(m[0], m[1]); };
const when = (t) => (t ? fmtTime(t) : '—');
const val = (o) => (!o ? '—' : o.status === 'found' ? String(o.value) : o.status === 'not_found' ? 'not present' : 'unavailable');

async function control(l, act, ctx, reason = '') {
  try {
    await api.post(`/loops/${encodeURIComponent(l.loop_id)}/${act.replace('_', '-')}`, { reason });
    toast({ pause: 'Loop paused.', resume: 'Loop resumed.', stop: 'Loop stopped.', check_now: 'Checking now…' }[act] || 'Done.', 'ok');
    ctx.rerender();
  } catch (e) { toast(e.message, 'bad'); }
}

function table(head, rows) {
  return el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, head.map((h) => el('th', { text: h })))), el('tbody', null, rows)));
}

async function detail(l) {
  let d;
  try { d = (await api.get(`/loops/${encodeURIComponent(l.loop_id)}`)).loop; } catch (e) { toast(e.message, 'bad'); return; }
  const body = el('div', { class: 'stack' });
  body.append(el('p', { class: 'muted small', text: `${d.client_name || d.client_id} · ${d.kind} · started ${when(d.created_at)} · ${d.requests_used} research request(s) used${d.goal.max_requests ? ' of ' + d.goal.max_requests : ''} · round ${d.round}${d.goal.max_rounds ? ' of ' + d.goal.max_rounds : ''}${d.goal.deadline ? ' · deadline ' + when(d.goal.deadline) : ''}` }));
  if (d.summary) body.append(notice(d.summary + (d.outcome ? ` (ended by ${END[d.outcome] || d.outcome})` : ''), d.status === 'goal_reached' ? 'ok' : 'warn'));
  for (const b of d.blockers) body.append(notice(`${b.code}: ${b.message}`, 'bad'));
  const gp = (d.goal_progress && d.goal_progress.targets) || [];
  if (gp.length) {
    body.append(el('h3', { text: 'Goal' }), table(['Target', 'Status', 'Latest', 'Confirmations', 'Notes'], gp.map((t) => {
      const v = VERDICT[t.verdict] || [t.verdict, 'gray'];
      return el('tr', null, el('td', { text: t.label }), el('td', null, chip(v[0], v[1])), el('td', { text: t.current ? val(t.current) : '—' }), el('td', { text: t.needed ? `${t.streak || 0} of ${t.needed}` : '—' }),
        el('td', { class: 'small', text: [t.volatile ? 'Volatile: positions are swinging, so progress is not trusted.' : '', t.reason || ''].filter(Boolean).join(' ') }));
    })));
  }
  const comps = d.comparisons || [];
  const last = comps[comps.length - 1];
  if (last) {
    body.append(el('h3', { text: `Before and after (round ${last.round})` }), table(['Measured', 'Baseline', 'Previous', 'Now', 'Change'], last.rows.map((r) => {
      const c = CHANGE[r.change] || [r.change, 'gray'];
      return el('tr', null, el('td', { text: r.label }), el('td', { text: val(r.baseline) }), el('td', { text: val(r.previous) }), el('td', { text: val(r.now) }),
        el('td', null, chip(c[0], c[1]), r.note ? el('div', { class: 'muted small', text: r.note }) : null));
    })));
  }
  body.append(el('h3', { text: 'Rounds' }), table(['Round', 'Plan', 'Changes', 'Measured', 'Result'], d.rounds.map((r) => el('tr', null,
    el('td', { text: r.n === 0 ? 'Baseline' : String(r.n) }),
    el('td', { class: 'small' }, r.plan ? (r.plan.items.length ? r.plan.items.map((i) => el('div', null, `${i.title}${r.item_effects && r.item_effects[i.fingerprint] ? ' → ' + r.item_effects[i.fingerprint].replace('_', ' ') : ''}`)) : 'Wait only (no change)') : '—'),
    el('td', { text: r.changes ? String(r.changes.length) : '—' }), el('td', { class: 'nowrap', text: when(r.measured_at) }),
    el('td', { class: 'small', text: r.comparison_summary ? Object.entries(r.comparison_summary).filter(([, n]) => n).map(([k, n]) => `${n} ${k.replace('_', ' ')}`).join(', ') : '—' })))));
  if (d.sources.length) body.append(el('h3', { text: 'Data sources' }), table(['Source', 'Connected', 'Detail'], d.sources.map((s) => el('tr', null, el('td', { text: s.source }), el('td', null, chip(s.connected ? 'Yes' : 'No', s.connected ? 'ok' : 'warn')), el('td', { class: 'small', text: s.detail || '' })))));
  body.append(el('h3', { text: 'History' }), el('div', { class: 'small', style: 'max-height:240px;overflow:auto' }, (d.events || []).slice(-60).map((e) => el('div', null, el('span', { class: 'muted', text: when(e.at) + '  ' }), el('b', { text: e.event.replace(/_/g, ' ') }), e.detail ? ' — ' + e.detail : ''))));
  await modal({ title: d.title, wide: true, body, buttons: [{ label: 'Close', value: true, kind: 'primary' }] });
}

export function loopsCard(loops, ctx) {
  const card = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('div', null, el('h2', { text: 'Improvement loops' }),
    el('p', { class: 'muted small', text: 'Each loop measures where a client stands, waits for the collaborator to plan and make changes (production deploys still need your PIN), waits for the results to show, measures again and compares with the baseline until the goal is confirmed or a limit ends it. Nothing runs while a loop waits.' }))));
  if (!loops.length) { card.append(emptyState('No loops yet', 'When Cloudy starts one for a client it appears here, with its goal, what it is waiting for and the before-and-after results.')); return card; }
  card.append(table(['Loop', 'Client', 'State', 'Waiting for', 'Goal', ''], loops.map((l) => {
    const tg = (l.goal_progress && l.goal_progress.targets) || [];
    const done = tg.filter((t) => t.verdict === 'met_confirmed').length;
    const na = l.next_action || {};
    const active = ['active', 'blocked', 'paused'].includes(l.status);
    return el('tr', null,
      el('td', null, el('b', { text: l.title }), el('div', { class: 'muted small', text: `${l.kind} · round ${l.round}` })),
      el('td', { text: l.client_name || '—' }),
      el('td', null, statusChip(l.status), l.status === 'active' ? el('div', { class: 'muted small', text: PHASE[l.phase] || l.phase }) : null),
      el('td', { class: 'small' }, l.status === 'active' || l.status === 'blocked' ? [`${na.who || ''}: ${na.what || ''}`, l.next_check_at ? el('div', { class: 'muted', text: 're-check ' + when(l.next_check_at) }) : null, ...(l.blockers || []).map((b) => el('div', { class: 'muted', text: b.message }))] : (l.summary || '')),
      el('td', { class: 'small', text: tg.length ? `${done} of ${tg.length} target(s) confirmed` : 'ends by limits only' }),
      el('td', { class: 'nowrap' },
        el('button', { type: 'button', class: 'small', text: 'Details', on: { click: () => detail(l) } }), ' ',
        active && l.status !== 'paused' ? el('button', { type: 'button', class: 'small', text: 'Pause', on: { click: () => control(l, 'pause', ctx) } }) : null,
        l.status === 'paused' || l.status === 'blocked' ? el('button', { type: 'button', class: 'small primary', text: 'Resume', on: { click: () => control(l, 'resume', ctx) } }) : null, ' ',
        l.status === 'active' ? el('button', { type: 'button', class: 'small', text: 'Check now', title: 'Run the due research immediately (read-only)', on: { click: () => control(l, 'check_now', ctx) } }) : null, ' ',
        active ? el('button', { type: 'button', class: 'small danger', text: 'Stop', on: { click: async () => { if (await confirmDialog({ title: 'Stop this loop?', message: 'It ends for good and anything still being measured is cancelled. Its history stays.', confirmLabel: 'Stop loop', danger: true })) control(l, 'stop', ctx, 'stopped by the owner'); } } }) : null));
  })));
  return card;
}
