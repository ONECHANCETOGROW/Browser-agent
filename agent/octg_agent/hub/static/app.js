// OCTG Knowledge Hub: start-up, sign-in, navigation, the "current scope" bar and search. Each page is its own module.
import { api, setCsrf } from './api.js';
import { chip, debounce, el, errorState, field, icon, skeleton, toast } from './ui.js';
import { viewDashboard } from './v_dashboard.js';
import { viewMyBusiness } from './v_business.js';
import { viewClientsList, viewClientNew, viewClientDetail } from './v_clients.js';
import { viewImports } from './v_imports.js';
import { viewConflicts, viewDocuments, viewSeo, viewSources, viewActivity, viewSecurity, viewSettings } from './v_more.js';
import { viewCollab } from './v_collab.js';
import { viewAsk } from './v_ask.js';
import { viewInbox } from './v_inbox.js';

const $ = (id) => document.getElementById(id);
const NAV = [
  ['dashboard', 'Dashboard', 'home', '#/'], ['ask', 'Ask the agent', 'search', '#/ask'], ['inbox', 'Inbox', 'alert', '#/inbox'], ['my-business', 'My Business', 'building', '#/my-business'], ['clients', 'Clients', 'users', '#/clients'], ['documents', 'Documents', 'file', '#/documents'],
  ['seo', 'SEO', 'target', '#/seo'], ['approvals', 'AI & Approvals', 'shield', '#/approvals'], ['sources', 'Sources', 'link', '#/sources'], ['imports', 'Imports', 'upload', '#/imports'], ['conflicts', 'Conflicts', 'alert', '#/conflicts'],
  ['activity', 'Activity', 'activity', '#/activity'], ['security', 'Security', 'shield', '#/security'], ['settings', 'Settings', 'gear', '#/settings'],
];
let routeSeq = 0;
let scopeData = null;
let badges = { conflicts: 0, pending: 0, staged: 0 };

// ---------------------------------------------------------------- sign in
async function showLogin(message) {
  $('app').hidden = true;
  const login = $('login');
  login.hidden = false;
  let pinSet = false;
  try { pinSet = !!(await api.get('/session')).pin_set; } catch (_) { /* the agent is not reachable: the error shows on sign-in */ }
  const label = pinSet ? 'PIN' : 'Access code';
  const input = el('input', { type: 'password', autocomplete: 'off', name: 'octg-access', placeholder: label, inputMode: pinSet ? 'numeric' : 'text', attrs: { 'aria-label': label } });
  const msg = el('div', { class: 'err', text: message || '' });
  const go = async () => {
    msg.textContent = '';
    try {
      const r = await api.post('/login', { token: input.value.trim() });
      input.value = '';
      await enter(r.csrf);
    } catch (e) { msg.textContent = e.message; input.select(); }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  login.replaceChildren(el('div', { class: 'card stack' },
    el('span', { class: 'logo' }), el('h1', { text: 'OCTG Knowledge Hub' }),
    el('p', { class: 'muted', text: 'Your private business and client knowledge base. It runs only on this computer.' }),
    field(label, input, pinSet ? 'Forgot it? Double-click the “OCTG Knowledge Hub” shortcut: it opens this page already unlocked.'
      : 'Easiest: double-click open_knowledge_hub.bat in the agent folder: it opens this page already unlocked. Otherwise paste the access code printed by: python -m octg_agent token --api. After signing in you can set a PIN on the Security page.'), msg,
    el('div', { class: 'actions' }, el('button', { type: 'button', class: 'primary', text: 'Unlock', on: { click: go } }))));
  input.focus();
}

async function enter(csrf) {
  setCsrf(csrf);
  $('login').hidden = true;
  $('app').hidden = false;
  buildNav();
  await reloadScopes();
  route();
  refreshBadges();
}

document.addEventListener('hub:locked', () => showLogin('The Hub was locked (or the agent restarted). Unlock it again.'));
$('lock').addEventListener('click', async () => { try { await api.post('/logout'); } catch (_) { /* already locked */ } setCsrf(null); showLogin('Locked.'); });

// ---------------------------------------------------------------- scopes
async function reloadScopes() { scopeData = (await api.get('/scopes')); return scopeData; }

function buildNav() {
  const nav = $('nav');
  nav.className = 'nav';
  nav.replaceChildren(...NAV.map(([id, label, ic, href]) => el('a', { href, dataset: { nav: id } }, icon(ic), label, id === 'conflicts' ? el('span', { class: 'badge', id: 'badge-conflicts', hidden: true }) : null,
    id === 'imports' ? el('span', { class: 'badge soft', id: 'badge-imports', hidden: true }) : null,
    id === 'approvals' ? el('span', { class: 'badge', id: 'badge-approvals', hidden: true }) : null,
    id === 'inbox' ? el('span', { class: 'badge', id: 'badge-inbox', hidden: true }) : null)));
}

async function refreshBadges() {
  try {
    const c = await api.get('/conflicts');
    badges.conflicts = c.conflicts.length;
    badges.pending = c.pending.length;
    const i = await api.get('/imports');
    badges.staged = i.staged.length;
  } catch (_) { return; }
  try {
    const cl = await api.get('/collab');
    const n = cl.approvals.filter((a) => a.status === 'proposed').length;
    const ba = $('badge-approvals');
    if (ba) { ba.textContent = n; ba.hidden = !n; ba.title = `${n} action(s) waiting for your approval`; }
  } catch (_) { /* collaboration service off */ }
  try {
    const ib = $('badge-inbox');
    const n = (await api.get('/inbox')).open;
    if (ib) { ib.textContent = n; ib.hidden = !n; ib.title = `${n} request(s) from the AI are waiting for you`; }
  } catch (_) { /* collaboration service off */ }
  const bc = $('badge-conflicts'), bi = $('badge-imports');
  if (bc) { bc.textContent = badges.conflicts + badges.pending; bc.hidden = !(badges.conflicts + badges.pending); bc.title = `${badges.conflicts} conflict(s), ${badges.pending} value(s) waiting for confirmation`; }
  if (bi) { bi.textContent = badges.staged; bi.hidden = !badges.staged; bi.title = `${badges.staged} import(s) waiting for review`; }
}

function paintScope(scope) {
  const bar = $('scopeBar');
  const sel = el('select', { attrs: { 'aria-label': 'Switch between your business and your clients' } });
  sel.append(el('option', { value: '', text: 'Switch scope…' }), el('optgroup', { label: 'Your business' }, el('option', { value: 'me', text: scopeData.owner.name + ' (my business)' })),
    el('optgroup', { label: 'Clients' }, scopeData.clients.filter((c) => !c.archived).map((c) => el('option', { value: c.id, text: c.name })), scopeData.clients.filter((c) => c.archived).map((c) => el('option', { value: c.id, text: c.name + ' (archived)' }))));
  sel.value = scope && scope.id ? scope.id : '';
  sel.addEventListener('change', () => { if (sel.value === 'me') location.hash = '#/my-business'; else if (sel.value) location.hash = '#/clients/' + sel.value; });
  let pill;
  if (!scope) pill = el('span', { class: 'pill' }, el('small', { text: 'CURRENT SCOPE' }), el('b', { text: 'All of your information' }));
  else if (scope.id === 'me') pill = el('span', { class: 'pill' }, el('small', { text: 'CURRENT SCOPE' }), el('b', { text: scopeData.owner.name }));
  else pill = el('span', { class: 'pill client' }, el('small', { text: 'CURRENT CLIENT' }), el('b', { text: scope.name }));
  const dflt = scopeData.default_scope;
  const bits = [pill, sel];
  if (scope && scope.id !== 'me') {
    const isDefault = dflt === scope.id;
    bits.push(isDefault ? chip('Default client for tasks', 'ok', 'Tasks about "the client" use this client unless the wording names another') : el('button', { type: 'button', class: 'small', text: 'Use as default client for tasks', on: { click: async () => {
      try { await api.post('/settings', { default_scope: scope.id }); toast(`${scope.name} is now the default client for tasks.`, 'ok'); await reloadScopes(); paintScope(scope); } catch (e) { toast(e.message, 'bad'); }
    } } }));
  }
  bar.replaceChildren(...bits);
}

// ---------------------------------------------------------------- router
function route() {
  const id = ++routeSeq;
  const hash = location.hash.replace(/^#\/?/, '');
  const segs = hash.split('/').filter(Boolean).map(decodeURIComponent);
  const view = $('view');
  const page = segs[0] || 'dashboard';
  for (const a of document.querySelectorAll('#nav a')) a.toggleAttribute('aria-current', a.dataset.nav === page);
  for (const a of document.querySelectorAll('#nav a')) { if (a.dataset.nav === page) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); }
  $('side').classList.remove('open');
  $('menu').setAttribute('aria-expanded', 'false');
  view.replaceChildren(skeleton());
  const ctx = {
    mount(node, { focus = true } = {}) { if (id !== routeSeq) return false; view.replaceChildren(node); if (focus) { const h = view.querySelector('h1'); if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); } } return true; },
    stale: () => id !== routeSeq, scopes: () => scopeData, reloadScopes, setScope: paintScope, refreshBadges, go: (h) => { location.hash = h; }, rerender: () => route(), segs,
  };
  const run = async () => {
    try {
      if (page === 'dashboard') { paintScope(null); await viewDashboard(ctx); }
      else if (page === 'ask') { paintScope(null); await viewAsk(ctx); }
      else if (page === 'inbox') { paintScope(null); await viewInbox(ctx); }
      else if (page === 'my-business') { paintScope({ id: 'me' }); await viewMyBusiness(ctx, segs[1]); }
      else if (page === 'clients') {
        if (segs[1] === 'new') { paintScope(null); await viewClientNew(ctx); }
        else if (segs[1]) { const c = scopeData.clients.find((x) => x.id === segs[1]); paintScope(c ? { id: c.id, name: c.name } : null); await viewClientDetail(ctx, segs[1], segs[2] || 'overview'); }
        else { paintScope(null); await viewClientsList(ctx); }
      } else if (page === 'documents') { paintScope(null); await viewDocuments(ctx); }
      else if (page === 'seo') { paintScope(null); await viewSeo(ctx, segs[1]); }
      else if (page === 'approvals') { paintScope(null); await viewCollab(ctx); }
      else if (page === 'sources') { paintScope(null); await viewSources(ctx); }
      else if (page === 'imports') { paintScope(null); await viewImports(ctx, segs[1]); }
      else if (page === 'conflicts') { paintScope(null); await viewConflicts(ctx); }
      else if (page === 'activity') { paintScope(null); await viewActivity(ctx); }
      else if (page === 'security') { paintScope(null); await viewSecurity(ctx); }
      else if (page === 'settings') { paintScope(null); await viewSettings(ctx); }
      else ctx.mount(el('div', { class: 'card' }, el('h1', { text: 'Page not found' }), el('p', { text: 'Use the menu on the left.' })));
    } catch (e) {
      if (id === routeSeq && e.code !== 'LOCKED') ctx.mount(errorState(e.message, () => route()), { focus: false });
    }
  };
  run();
}
window.addEventListener('hashchange', route);
$('menu').addEventListener('click', () => { const open = $('side').classList.toggle('open'); $('menu').setAttribute('aria-expanded', String(open)); });

// ---------------------------------------------------------------- search
const results = $('results');
const doSearch = debounce(async () => {
  const q = $('q').value.trim();
  if (q.length < 2) { results.hidden = true; return; }
  try {
    const r = await api.post('/search', { q });
    results.replaceChildren(...(r.results.length ? r.results.map((x) => el('a', { href: x.link, role: 'option', on: { click: () => { results.hidden = true; $('q').value = ''; } } },
      el('b', { text: x.title }), el('small', { text: `${x.type} · ${x.scope_name}${x.snippet ? ' · ' + x.snippet : ''}` }))) : [el('div', { class: 'empty', text: 'Nothing matches. Protected values (tax ids, internal notes) are never searched.' })]));
    results.hidden = false;
  } catch (e) { results.hidden = true; }
}, 250);
$('q').addEventListener('input', doSearch);
$('q').addEventListener('keydown', (e) => { if (e.key === 'Escape') { results.hidden = true; } if (e.key === 'ArrowDown') results.querySelector('a')?.focus(); });
document.addEventListener('click', (e) => { if (!results.contains(e.target) && e.target !== $('q')) results.hidden = true; });
results.addEventListener('keydown', (e) => {
  const links = [...results.querySelectorAll('a')], i = links.indexOf(document.activeElement);
  if (e.key === 'ArrowDown' && i < links.length - 1) { e.preventDefault(); links[i + 1].focus(); }
  if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? links[i - 1] : $('q')).focus(); }
  if (e.key === 'Escape') { results.hidden = true; $('q').focus(); }
});

// ---------------------------------------------------------------- go
(async function boot() {
  try {
    const frag = new URLSearchParams(location.hash.replace(/^#/, ''));
    const code = frag.get('code');
    if (code) {   // a one-time code from `open_knowledge_hub.bat`: spend it, then remove it from the address bar
      history.replaceState(null, '', location.pathname);
      try { const r = await api.post('/login', { code }); await enter(r.csrf); return; } catch (e) { showLogin(e.message); return; }
    }
    const s = await api.get('/session');
    if (s.authenticated) await enter(s.csrf); else showLogin();
  } catch (e) {
    showLogin(e.message);
  }
})();
export { reloadScopes, refreshBadges };
