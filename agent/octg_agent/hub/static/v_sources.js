// Data sources (Google Search Console, Bing Webmaster Tools): are they connected, and which property belongs to which client. Only the owner maps (PIN); the collaborator never sees other properties.
import { api } from './api.js';
import { chip, confirmDialog, el, emptyState, field, fmtTime, modal, notice, toast } from './ui.js';

const when = (t) => (t ? fmtTime(t) : 'never');

function table(head, rows) {
  return el('div', { class: 'tablewrap' }, el('table', null, el('thead', null, el('tr', null, head.map((h) => el('th', { text: h })))), el('tbody', null, rows)));
}

async function verify(src, ctx) {
  try { await api.post(`/connectors/${src.source}/verify`); toast(`${src.label}: the credentials work.`, 'ok'); } catch (e) { toast(`${src.label}: ${e.message}`, 'bad'); }
  ctx.rerender();
}

async function mapDialog(src, d, ctx) {
  const client = el('select', { attrs: { 'aria-label': 'Client' } }, el('option', { value: '', text: 'Choose a client…' }), d.clients.map((c) => el('option', { value: c.id, text: c.name })));
  const prop = el('select', { attrs: { 'aria-label': 'Property' }, disabled: true }, el('option', { value: '', text: 'Choose the client first' }));
  const mismatch = el('input', { type: 'checkbox', attrs: { 'aria-label': 'This property really belongs to this client even though its domain differs' } });
  const mismatchRow = el('label', { class: 'check', hidden: true }, mismatch, ' This property really belongs to this client even though its domain differs from their website.');
  const pin = el('input', { type: 'password', inputMode: 'numeric', autocomplete: 'off', maxLength: 12, attrs: { 'aria-label': 'Your PIN' } });
  const err = el('div', { class: 'err' });
  let props = [];
  const showMismatch = () => { const p = props.find((x) => x.property === prop.value); mismatchRow.hidden = !(p && !p.matches_client); };
  prop.addEventListener('change', showMismatch);
  client.addEventListener('change', async () => {
    err.textContent = '';
    prop.disabled = true; prop.replaceChildren(el('option', { value: '', text: 'Looking up what the credentials can see…' }));
    if (!client.value) { prop.replaceChildren(el('option', { value: '', text: 'Choose the client first' })); return; }
    try {
      props = (await api.get(`/connectors/${src.source}/properties?client=${encodeURIComponent(client.value)}`)).properties;
      prop.replaceChildren(el('option', { value: '', text: props.length ? 'Choose a property…' : 'These credentials can see no properties' }),
        props.map((p) => el('option', { value: p.property, disabled: !p.usable || !!p.mapped_to, text: `${p.property} — ${p.permission}${p.matches_client ? ' · matches this client' : ''}${p.mapped_to ? ' · already mapped to ' + p.mapped_to : ''}${!p.usable ? ' · cannot read data' : ''}` })));
      prop.disabled = false;
    } catch (e) { prop.replaceChildren(el('option', { value: '', text: 'Could not list properties' })); err.textContent = e.message; }
  });
  const body = el('div', { class: 'stack' }, el('p', { class: 'muted small', text: `A property serves exactly one client. Choosing it here lets the collaborator read ${src.label} data for that client only.` }),
    field('Client', client), field('Property', prop), mismatchRow, field('Your PIN (required)', pin), err);
  const ok = await modal({ title: `Map a ${src.label} property`, body, wide: true, buttons: [{ label: 'Cancel', value: false }, { label: 'Map property', value: true, kind: 'primary',
    validate: () => { err.textContent = ''; if (!client.value || !prop.value) { err.textContent = 'Choose a client and a property.'; return false; } if (!/^[0-9]{4,12}$/.test(pin.value)) { err.textContent = 'Type your PIN (4 to 12 digits).'; return false; } return true; } }] });
  if (!ok) return;
  try { await api.post('/connectors/map', { client_id: client.value, source: src.source, property: prop.value, allow_mismatch: mismatch.checked, pin: pin.value }); toast('Property mapped.', 'ok'); ctx.rerender(); } catch (e) { toast(e.message, 'bad'); }
}

async function unmap(m, ctx) {
  const pin = el('input', { type: 'password', inputMode: 'numeric', autocomplete: 'off', maxLength: 12, attrs: { 'aria-label': 'Your PIN' } });
  const err = el('div', { class: 'err' });
  const ok = await modal({ title: 'Remove this mapping?', body: el('div', { class: 'stack' }, el('p', { text: `${m.client_name} will no longer get ${m.property} data. The history already collected stays.` }), field('Your PIN (required)', pin), err),
    buttons: [{ label: 'Cancel', value: false }, { label: 'Remove', value: true, kind: 'danger solid', validate: () => { if (!/^[0-9]{4,12}$/.test(pin.value)) { err.textContent = 'Type your PIN.'; return false; } return true; } }] });
  if (!ok) return;
  try { await api.post('/connectors/unmap', { map_id: m.map_id, pin: pin.value }); toast('Mapping removed.', 'ok'); ctx.rerender(); } catch (e) { toast(e.message, 'bad'); }
}

export async function sourcesCard(ctx) {
  let d;
  try { d = await api.get('/connectors'); } catch (_) { return null; }
  const card = el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('div', null, el('h2', { text: 'Data sources' }),
    el('p', { class: 'muted small', text: 'First-party search data for your clients. Credentials are files you put in the agent\'s connectors folder; they are never shown here. You decide which property belongs to which client, and a property can belong to only one.' }))));
  card.append(table(['Source', 'Status', 'Credentials', 'Last check', 'Clients mapped', ''], d.sources.map((s) => el('tr', null,
    el('td', null, el('b', { text: s.label })),
    el('td', null, chip(s.connected ? 'Connected' : 'Not connected', s.connected ? 'ok' : 'warn'), s.connected ? null : el('div', { class: 'muted small', text: s.missing })),
    el('td', { class: 'small' }, s.credentials_present ? [s.credential_kind.replace('_', ' '), s.identity && s.identity.includes('@') ? el('div', { class: 'muted', text: `add ${s.identity} as a user of each property` }) : null] : 'none yet'),
    el('td', { class: 'small', text: s.verified_at ? `${when(s.verified_at)}${s.last_verification && !s.last_verification.ok ? ' — failed: ' + s.last_verification.code : ''}` : 'never' }),
    el('td', { text: String(s.mapped_clients) }),
    el('td', { class: 'nowrap' }, s.credentials_present ? [el('button', { type: 'button', class: 'small', text: 'Verify now', on: { click: () => verify(s, ctx) } }), ' ',
      el('button', { type: 'button', class: 'small primary', text: 'Map a property…', on: { click: () => mapDialog(s, d, ctx) } })] : el('span', { class: 'muted small', text: 'add the credentials file first' }))))));
  if (d.mappings.length) {
    card.append(el('h3', { style: 'padding:12px 18px 0', text: 'Mappings' }), table(['Client', 'Source', 'Property', 'Permission', 'Mapped', 'Last check', ''], d.mappings.map((m) => el('tr', null,
      el('td', { text: m.client_name }), el('td', { text: m.source.replace(/_/g, ' ') }), el('td', { class: 'mono', text: m.property }), el('td', { text: m.permission }), el('td', { class: 'nowrap', text: when(m.mapped_at) }),
      el('td', { class: 'small', text: m.last_error ? 'problem: ' + m.last_error : (m.last_ok_at ? when(m.last_ok_at) : '—') }),
      el('td', null, el('button', { type: 'button', class: 'small danger', text: 'Remove', on: { click: () => unmap(m, ctx) } }))))));
  } else {
    card.append(emptyState('No property mapped yet', 'Once credentials are in place, map each client\'s Search Console / Bing property here. Until then audits run without that data and say so.'));
  }
  return card;
}
