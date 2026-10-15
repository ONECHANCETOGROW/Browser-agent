// My Business: OCTG's own profile, section by section. Same editor as clients; a separate scope that is never mixed with a client.
import { api } from './api.js';
import { fieldSection } from './fields.js';
import { exportButton, sourcesTable } from './shared.js';
import { chip, el, emptyState } from './ui.js';

export async function viewMyBusiness(ctx, section) {
  const d = await api.get('/me');
  const ids = d.sections.map((s) => s.id);
  const current = section === 'sources' ? 'sources' : (ids.includes(section) ? section : ids[0]);
  const c = d.completion;
  const page = el('div', { class: 'stack' });
  page.append(el('div', { class: 'page-head' }, el('div', { class: 'grow' }, el('h1', { text: 'My Business' }),
    el('p', { text: 'Your own business information, used when a task is about your business. It is kept apart from every client. Type values here; you never need a terminal.' })),
    el('div', { class: 'actions' }, exportButton('business', null, 'Export to Excel'), el('a', { class: 'btn', href: '#/imports', text: 'Import…' }))));
  page.append(el('div', { class: 'card' }, el('div', { class: 'row space' }, el('b', { text: `Profile ${c.percent}% complete` }), el('span', { class: 'muted small', text: `${c.done} of ${c.total} key details verified` })),
    el('div', { class: 'progress', style: 'margin-top:8px' }, el('i', { style: `width:${c.percent}%` })),
    c.todo.length ? el('p', { class: 'muted small', style: 'margin-top:8px', text: 'Still to do: ' + c.todo.map((t) => t.label).join(', ') }) : null));
  const tabs = el('div', { class: 'tabs', role: 'tablist' }, d.sections.map((s) => {
    const n = s.fields.filter((f) => f.status !== 'MISSING').length;
    return el('a', { href: '#/my-business/' + s.id, ...(s.id === current ? { attrs: { 'aria-current': 'page' } } : {}) }, s.title, n ? el('span', { class: 'faint', text: ` · ${n}` }) : null);
  }), el('a', { href: '#/my-business/sources', ...(current === 'sources' ? { attrs: { 'aria-current': 'page' } } : {}) }, 'Sources'));
  page.append(tabs);
  if (current === 'sources') {
    page.append(el('div', { class: 'card flush' }, el('div', { class: 'head' }, el('h2', { text: 'Where your business information came from' })),
      sourcesTable(d.sources, { showScope: false }), el('p', { class: 'muted small', style: 'padding:0 18px 14px', text: 'Each value also shows its own source underneath it on the other tabs.' })));
  } else {
    const s = d.sections.find((x) => x.id === current);
    page.append(fieldSection({ scope: 'me', title: s.title, fields: s.fields, onChange: () => { ctx.reloadScopes().then(() => ctx.rerender()); ctx.refreshBadges(); },
      intro: current === 'tax' || current === 'registration' ? 'These are protected: stored encrypted with your Windows account, shown masked, and never given to an AI model.' : undefined }));
  }
  ctx.mount(page);
}
