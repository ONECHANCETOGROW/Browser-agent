// OCTG page agent — injected on demand into a frame's ISOLATED world by the service worker.
// Website-agnostic: it only knows about DOM/ARIA concepts, never about specific sites.
// Every entry point returns plain JSON: {ok:true,...} or {ok:false,error:{code,message,details?}}.
(() => {
  if (window.__octg && window.__octg.version === 1) return;

  const OCTG = { version: 1 };
  const registry = { snapshotId: null, map: new Map() }; // local_id -> {ref, fp}
  const counters = { mutations: 0 };

  const err = (code, message, details) => ({ ok: false, error: { code, message, ...(details ? { details } : {}) } });
  const clip = (s, n) => { s = (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------------------------------------------------------------- mutation counter
  try {
    new MutationObserver((list) => { counters.mutations += list.length; }).observe(document, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ['class', 'style', 'hidden', 'open', 'aria-expanded', 'aria-hidden', 'aria-checked', 'disabled', 'value', 'checked'],
    });
  } catch (_) { /* document not ready for observing; evidence still works without it */ }

  // ---------------------------------------------------------------- DOM helpers
  function* walkElements(root) {
    const all = root.querySelectorAll('*');
    for (const el of all) {
      yield el;
      if (el.shadowRoot) yield* walkElements(el.shadowRoot);
    }
  }

  function isVisible(el) {
    if (!el.isConnected) return false;
    try { if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false; } catch (_) {}
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  }

  function deepActive() {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  }

  function deepFromPoint(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  function composedParent(n) {
    return n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
  }
  function composedContains(a, b) {
    for (let n = b; n; n = composedParent(n)) if (n === a) return true;
    return false;
  }

  const IMPLICIT_ROLES = {
    A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox', SUMMARY: 'button',
    H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', H5: 'heading', H6: 'heading', IMG: 'img',
  };
  const INPUT_ROLES = {
    button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox', radio: 'radio',
    range: 'slider', number: 'spinbutton', search: 'searchbox', file: 'file', color: 'color', date: 'date',
    'datetime-local': 'date', month: 'date', week: 'date', time: 'date',
  };
  const INTERACTIVE_ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'switch', 'combobox', 'textbox', 'searchbox', 'slider', 'spinbutton', 'treeitem', 'listbox']);

  function roleOf(el) {
    const explicit = (el.getAttribute('role') || '').split(/\s+/)[0];
    if (explicit) return explicit;
    if (el.tagName === 'INPUT') return INPUT_ROLES[(el.type || 'text').toLowerCase()] || 'textbox';
    if (el.tagName === 'SELECT' && (el.multiple || el.size > 1)) return 'listbox';
    if (el.isContentEditable) return 'textbox';
    return IMPLICIT_ROLES[el.tagName] || '';
  }

  function textOfIds(ids) {
    return ids.split(/\s+/).map((id) => { const n = document.getElementById(id); return n ? n.textContent : ''; }).join(' ');
  }

  function nameOf(el) {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = clip(textOfIds(lb), 120); if (t) return t; }
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return clip(al, 120);
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      const type = (el.type || '').toLowerCase();
      if (['button', 'submit', 'reset'].includes(type)) return clip(el.value || (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : ''), 120);
      if (type === 'image') return clip(el.alt || el.value || '', 120);
      if (el.labels && el.labels.length) { const t = clip([...el.labels].map((l) => l.textContent).join(' '), 120); if (t) return t; }
      return clip(el.placeholder || el.title || el.name || '', 120);
    }
    if (tag === 'IMG') return clip(el.alt || el.title || '', 120);
    const inner = clip(el.innerText || el.textContent, 120);
    if (inner) return inner;
    const img = el.querySelector && el.querySelector('img[alt],svg title');
    if (img) return clip(img.alt || img.textContent, 120);
    return clip(el.title || '', 120);
  }

  function cssPath(el) {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && parts.length < 6; n = n.parentElement) {
      if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) { parts.unshift('#' + CSS.escape(n.id)); break; }
      let part = n.tagName.toLowerCase();
      const sibs = n.parentElement ? [...n.parentElement.children].filter((c) => c.tagName === n.tagName) : [];
      if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
      parts.unshift(part);
    }
    return parts.join(' > ');
  }

  const fingerprint = (el) => [el.tagName, roleOf(el), (el.type || ''), nameOf(el).slice(0, 40)].join('|');

  // Fields whose contents must never leave the page in any output: passwords plus anything that declares or looks like a secret.
  const SECRET_HINT = /pass(word|code)?\b|pwd|\botp\b|one[-_ ]?time|verification[-_ ]?code|\bcvv\b|\bcvc\b|card[-_ ]?(number|no)|\bssn\b|social[-_ ]?security|\bsecret\b|api[-_ ]?key|\btoken\b|\bpin\b/i;
  function isSensitive(el) {
    if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') return false;
    if ((el.type || '').toLowerCase() === 'password') return true;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (/(^|\s)(current-password|new-password|one-time-code|cc-[a-z-]+)(\s|$)/.test(ac)) return true;
    return SECRET_HINT.test([el.name, el.id, el.placeholder, el.getAttribute('aria-label')].filter(Boolean).join(' '));
  }

  const editableKind = (el) => {
    if (el.tagName === 'TEXTAREA') return 'textarea';
    if (el.tagName === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      return ['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'file', 'range', 'color', 'hidden'].includes(t) ? null : 'input';
    }
    return el.isContentEditable ? 'contenteditable' : null;
  };

  function describeShort(el) {
    if (!el) return null;
    return { tag: el.tagName.toLowerCase(), role: roleOf(el) || undefined, name: nameOf(el) || undefined, id: el.id || undefined };
  }

  // ---------------------------------------------------------------- element collection
  function isCandidate(el, budget) {
    const tag = el.tagName;
    if (tag === 'INPUT') return (el.type || '').toLowerCase() !== 'hidden';
    if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'SUMMARY') return true;
    if (tag === 'A') return el.hasAttribute('href') || !!el.getAttribute('role');
    const role = (el.getAttribute('role') || '').split(/\s+/)[0];
    if (INTERACTIVE_ROLES.has(role)) return true;
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) return true;
    if (el.hasAttribute('onclick')) return true;
    if (budget.heuristics && !['HTML', 'BODY', 'SCRIPT', 'STYLE'].includes(tag)) {
      if (performance.now() > budget.deadline) { budget.heuristics = false; return false; }
      const ti = el.getAttribute('tabindex');
      const cs = getComputedStyle(el);
      if (cs.cursor === 'pointer' || (ti !== null && Number(ti) >= 0 && role)) {
        const par = el.parentElement;
        if (par && getComputedStyle(par).cursor === 'pointer') return false; // child of an already-clickable area
        return !el.closest('a[href],button,[role=button],[role=link]') || el.closest('a[href],button,[role=button],[role=link]') === el;
      }
    }
    return false;
  }

  function describe(el, localId, opts) {
    const rect = el.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const role = roleOf(el) || (el.hasAttribute('onclick') || getComputedStyle(el).cursor === 'pointer' ? 'clickable' : '');
    const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : undefined;
    const d = {
      local_id: localId,
      tag: el.tagName.toLowerCase(),
      role,
      name: nameOf(el),
      in_viewport: rect.bottom > 0 && rect.right > 0 && rect.top < vh && rect.left < vw && rect.width > 0 && rect.height > 0,
      rect: { x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
    if (type) d.type = type;
    if (el.tagName === 'A' && el.href) d.href = clip(el.href, 200);
    if (el.placeholder) d.placeholder = clip(el.placeholder, 60);
    if ((el.tagName === 'BUTTON' || (el.tagName === 'INPUT' && (type === 'submit' || type === 'image'))) && el.form && (el.type === 'submit' || el.type === 'image')) d.submits_form = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') d.disabled = true;
    if (el.readOnly) d.readonly = true;
    if (el.required) d.required = true;
    if (el === deepActive()) d.focused = true;
    if (isSensitive(el)) { d.sensitive = true; d.value = el.value ? '[hidden]' : ''; }
    else if (type === 'checkbox' || type === 'radio') d.checked = !!el.checked;
    else if (el.tagName === 'SELECT') {
      d.value = el.value;
      d.options = [...el.options].slice(0, 40).map((o) => ({ value: o.value, label: clip(o.text, 60), selected: o.selected, ...(o.disabled ? { disabled: true } : {}) }));
      if (el.options.length > 40) d.options_truncated = el.options.length;
      if (el.multiple) d.multiple = true;
    } else if (type === 'file') { d.multiple = !!el.multiple; if (el.accept) d.accept = el.accept; d.files = el.files ? el.files.length : 0; }
    else if (editableKind(el) === 'contenteditable') d.value = clip(el.innerText, 80);
    else if ('value' in el && el.tagName !== 'BUTTON' && type !== 'button' && type !== 'submit') d.value = clip(el.value, 80);
    const ac = el.getAttribute('aria-checked'), ae = el.getAttribute('aria-expanded'), ap = el.getAttribute('aria-pressed'), as = el.getAttribute('aria-selected');
    if (ac !== null) d.checked = ac === 'true';
    if (ae !== null) d.expanded = ae === 'true';
    if (ap !== null) d.pressed = ap === 'true';
    if (as !== null) d.selected = as === 'true';
    if (opts && opts.include_selectors) d.selector = cssPath(el);
    return d;
  }

  function pageSummary() {
    const vw = window.innerWidth, vh = window.innerHeight;
    const se = document.scrollingElement || document.documentElement;
    const headings = [...document.querySelectorAll('h1,h2,h3')].filter(isVisible).slice(0, 25).map((h) => ({ level: Number(h.tagName[1]), text: clip(h.innerText, 100) }));
    return {
      url: location.href, title: document.title, lang: document.documentElement.lang || undefined, ready: document.readyState,
      viewport: { w: vw, h: vh },
      scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY), max_x: Math.max(0, se.scrollWidth - vw), max_y: Math.max(0, se.scrollHeight - vh) },
      headings,
      forms: document.forms.length,
      dialogs: findDialogs(),
      alerts: [...document.querySelectorAll('[role=alert],[role=status][aria-live],.alert')].filter(isVisible).slice(0, 5).map((a) => clip(a.innerText, 160)),
      iframes: [...document.querySelectorAll('iframe,frame')].slice(0, 20).map((f) => ({ src: clip(f.src || f.getAttribute('src') || '', 160), visible: isVisible(f) })),
      focused: describeShort(deepActive() === document.body ? null : deepActive()),
    };
  }

  function findDialogs() {
    const out = [];
    for (const d of document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true]')) {
      if (!isVisible(d)) continue;
      out.push({ role: d.getAttribute('role') || (d.tagName === 'DIALOG' ? 'dialog' : 'modal'), name: nameOf(d).slice(0, 80) || undefined, text: clip(d.innerText, 200) });
      if (out.length >= 5) break;
    }
    return out;
  }

  // ---------------------------------------------------------------- inspect
  OCTG.inspect = (opts) => {
    opts = opts || {};
    const max = opts.max_elements || 300;
    const budget = { heuristics: opts.heuristics !== false, deadline: performance.now() + 400 };
    registry.snapshotId = opts.snapshot_id || ('local_' + Date.now().toString(36));
    registry.map = new Map();
    const elements = [];
    let total = 0;
    let n = 0;
    const hiddenOk = (el) => el.tagName === 'INPUT' && ['file', 'checkbox', 'radio'].includes((el.type || '').toLowerCase());
    for (const el of walkElements(document)) {
      if (!isCandidate(el, budget)) continue;
      const visible = isVisible(el);
      if (!visible && !hiddenOk(el)) continue;
      total++;
      if (opts.viewport_only && visible) {
        const r = el.getBoundingClientRect();
        if (r.bottom <= 0 || r.top >= window.innerHeight || r.right <= 0 || r.left >= window.innerWidth) continue;
      } else if (opts.viewport_only && !visible) continue;
      if (elements.length >= max) continue;
      const localId = 'l' + (++n);
      registry.map.set(localId, { ref: new WeakRef(el), fp: fingerprint(el) });
      const d = describe(el, localId, opts);
      if (!visible) d.hidden = true;
      elements.push(d);
    }
    const res = { ok: true, snapshot_id: registry.snapshotId, elements, total_candidates: total, truncated: total > elements.length && !opts.viewport_only };
    if (opts.summary !== false && window === window.top) res.page = pageSummary();
    return res;
  };

  OCTG.pageState = () => ({ ok: true, page: pageSummary(), evidence: OCTG.evidence().evidence });

  // ---------------------------------------------------------------- text
  OCTG.readText = (opts) => {
    opts = opts || {};
    const max = opts.max_chars || 6000;
    let text = '';
    if (opts.scope === 'page') {
      text = (document.body && document.body.innerText) || '';
    } else {
      const vw = window.innerWidth, vh = window.innerHeight;
      const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
      const parts = [];
      let lastParent = null;
      const range = document.createRange();
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const t = node.nodeValue.replace(/\s+/g, ' ').trim();
        if (!t) continue;
        const p = node.parentElement;
        if (!p || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(p.tagName) || !isVisible(p)) continue;
        range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
        parts.push((lastParent && p !== lastParent ? '\n' : ' ') + t);
        lastParent = p;
        if (parts.join('').length > max * 2) break;
      }
      text = parts.join('').trim();
    }
    text = text.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    return { ok: true, scope: opts.scope === 'page' ? 'page' : 'viewport', text: text.slice(0, max), total_chars: text.length, truncated: text.length > max, url: location.href, title: document.title };
  };

  OCTG.textPresent = (opts) => {
    const hay = (document.body && document.body.innerText) || '';
    const ci = opts.case_insensitive !== false;
    const found = ci ? hay.toLowerCase().includes(String(opts.text).toLowerCase()) : hay.includes(opts.text);
    return { ok: true, found };
  };

  // ---------------------------------------------------------------- find text
  // Searches the rendered page text (DOM text nodes, open shadow roots), never screenshots and never the contents of form controls
  // (inputs, textareas, selects), so a typed password/OTP/card number can never come back through here.
  const FIND_SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TEXTAREA', 'SELECT', 'OPTION', 'INPUT']);
  const INLINE_DISPLAY = new Set(['inline', 'contents', 'ruby', 'ruby-text']);
  const FIND_MAX_MATCHES = 5000;
  const FIND_BUDGET_MS = 3000;
  const isWs = (c) => c === ' ' || c === '\n' || c === '\t' || c === '\r' || c === '\f' || c === ' ' || (c > '῿' && /\s/.test(c));
  const isWordChar = (c) => !!c && /[\p{L}\p{N}_]/u.test(c);

  // One lower-cased (unless case-sensitive), whitespace-collapsed haystack per run of text that flows together, with a map from every
  // haystack character back to its text node and offset, so a match can become a precise DOM Range.
  function buildHay(nodes, caseSensitive) {
    const chars = [], ni = [], off = [];
    let lastSpace = true;
    for (let k = 0; k < nodes.length; k++) {
      const s = nodes[k].nodeValue;
      for (let i = 0; i < s.length; i++) {
        let c = s[i];
        if (isWs(c)) { if (lastSpace) continue; c = ' '; lastSpace = true; } else {
          lastSpace = false;
          if (!caseSensitive) { const l = c.toLowerCase(); if (l.length === 1) c = l; }
        }
        chars.push(c); ni.push(k); off.push(i);
      }
    }
    return { hay: chars.join(''), ni, off, nodes };
  }

  function normalizeQuery(q, caseSensitive) {
    const s = String(q);
    let out = '', lastSpace = true;
    for (let i = 0; i < s.length; i++) { // UTF-16 units, exactly like buildHay, so both sides normalise identically
      let c = s[i];
      if (isWs(c)) { if (lastSpace) continue; c = ' '; lastSpace = true; } else {
        lastSpace = false;
        if (!caseSensitive) { const l = c.toLowerCase(); if (l.length === 1) c = l; }
      }
      out += c;
    }
    return out.replace(/ $/, '');
  }

  const firstRect = (range) => {
    for (const r of range.getClientRects()) if (r.width > 0 && r.height > 0) return r;
    return range.getBoundingClientRect();
  };

  OCTG.findText = (a) => {
    const caseSensitive = a.case_sensitive === true;
    const q = normalizeQuery(typeof a.text === 'string' ? a.text : '', caseSensitive);
    if (!q) return err('INVALID_PARAMS', 'text must contain something other than whitespace.');
    const maxMatches = Math.min(Math.max(Number(a.max_matches) || 10, 1), 50);
    const ctxChars = Math.min(Math.max(Number.isFinite(Number(a.context_chars)) ? Number(a.context_chars) : 80, 0), 300);
    const wantScroll = a.scroll !== false && (window === window.top || a.scroll_all_frames === true);
    const focusIdx = Number.isInteger(a.scroll_index) && a.scroll_index >= 0 ? a.scroll_index : 0;
    const wholeWord = a.whole_word === true;
    const deadline = performance.now() + FIND_BUDGET_MS;

    // 1. collect text runs in document order: a run is consecutive text nodes with the same block ancestor and visibility
    const visCache = new Map(), blockCache = new Map();
    const visible = (el) => { if (!visCache.has(el)) visCache.set(el, isVisible(el)); return visCache.get(el); };
    const blockOf = (el) => {
      let n = el, found = null;
      const trail = [];
      while (n) {
        if (blockCache.has(n)) { found = blockCache.get(n); break; }
        trail.push(n);
        if (!INLINE_DISPLAY.has(getComputedStyle(n).display)) { found = n; break; }
        n = composedParent(n);
      }
      if (!found) found = document.body || document.documentElement;
      for (const t of trail) blockCache.set(t, found);
      return found;
    };
    const runs = [];
    const roots = [document.body || document.documentElement];
    for (let ri = 0; ri < roots.length; ri++) {
      const walker = document.createTreeWalker(roots[ri], NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
          if (n.nodeType === 1) {
            if (FIND_SKIP.has(n.tagName)) return NodeFilter.FILTER_REJECT;
            if (n.shadowRoot) roots.push(n.shadowRoot);
            return NodeFilter.FILTER_SKIP;
          }
          return n.nodeValue ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        },
      });
      let cur = null;
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const p = node.parentElement || (node.parentNode && node.parentNode.host) || null;
        if (!p) continue;
        const vis = visible(p);
        const block = blockOf(p);
        const separator = !/\S/.test(node.nodeValue); // whitespace between inline elements ("<b>a</b> <i>b</i>") keeps words apart, but never starts a run
        if (separator && (!cur || cur.block !== block || cur.vis !== vis)) continue;
        if (!cur || cur.block !== block || cur.vis !== vis) { cur = { block, vis, nodes: [] }; runs.push(cur); }
        cur.nodes.push(node);
      }
    }

    // 2. search every run
    let total = 0, hidden = 0, capped = false, truncated = false;
    const matches = [];
    let focus = null; // {range, startEl, rec}
    let headings = null, regRev = null;
    const headingFor = (node) => {
      if (!headings) headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')].filter(isVisible);
      for (let i = headings.length - 1; i >= 0; i--) if (headings[i].compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING || headings[i].contains(node)) return clip(headings[i].innerText || headings[i].textContent, 100);
      return undefined;
    };
    const interactiveAncestor = (el) => {
      if (!regRev) {
        regRev = new Map();
        for (const [lid, rec] of registry.map) { const e = rec.ref.deref(); if (e && e.isConnected) regRev.set(e, lid); }
      }
      let n = el;
      for (let d = 0; n && d < 8; d++, n = composedParent(n)) if (regRev.has(n)) return { el: n, local_id: regRev.get(n) };
      return null;
    };
    const original = (h, from, to) => {
      let s = '';
      for (let i = from; i < to; i++) s += h.hay[i] === ' ' ? ' ' : h.nodes[h.ni[i]].nodeValue[h.off[i]];
      return s;
    };
    // Lightweight semantics from the DOM/ARIA alone, so the CALLER can decide which occurrence it wants. Nothing here ranks, filters or
    // reorders matches: a phrase in a navigation link, a section heading and a paragraph are all reported, each with what it is.
    const CONTROL_ROLES = new Set(['button', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'checkbox', 'radio', 'switch', 'combobox', 'treeitem']);
    const semantics = (startEl) => {
      let heading = null, link = null, control = null;
      let n = startEl;
      for (let d = 0; n && d < 10 && n !== document.body; d++, n = composedParent(n)) {
        const explicit = (n.getAttribute('role') || '').split(/\s+/)[0];
        if (!heading && (/^H[1-6]$/.test(n.tagName) || explicit === 'heading')) heading = n;
        if (!link && ((n.tagName === 'A' && n.hasAttribute('href')) || explicit === 'link')) link = n;
        if (!control && (n.tagName === 'BUTTON' || n.tagName === 'LABEL' || n.tagName === 'SUMMARY' || CONTROL_ROLES.has(explicit))) control = n;
      }
      const el = heading || link || control || startEl;
      const out = { element_tag: el ? el.tagName.toLowerCase() : undefined, is_heading: !!heading, is_link: !!link };
      if (heading) {
        const lvl = /^H([1-6])$/.exec(heading.tagName);
        out.heading_level = lvl ? Number(lvl[1]) : (parseInt(heading.getAttribute('aria-level'), 10) || 2); // ARIA: role=heading defaults to level 2
      }
      out.role = heading ? 'heading' : link ? 'link' : (control ? roleOf(control) : (startEl ? roleOf(startEl) : '')) || null;
      out.match_type = heading ? 'heading' : link ? 'link' : control ? 'other' : 'body_text';
      return out;
    };
    const visibilityOf = (rect) => {
      const vw = window.innerWidth, vh = window.innerHeight;
      if (rect.bottom <= 0) return 'above';
      if (rect.top >= vh) return 'below';
      if (rect.right <= 0 || rect.left >= vw) return 'beside';
      return 'in_view';
    };
    const record = (h, pos, end, idx) => {
      const sn = h.nodes[h.ni[pos]], en = h.nodes[h.ni[end - 1]];
      const range = document.createRange();
      range.setStart(sn, h.off[pos]);
      range.setEnd(en, h.off[end - 1] + 1);
      const startEl = sn.parentElement || (sn.parentNode && sn.parentNode.host);
      const rect = firstRect(range);
      const from = Math.max(0, pos - ctxChars), to = Math.min(h.hay.length, end + ctxChars);
      const ia = interactiveAncestor(startEl);
      const rec = {
        local_index: idx, text: clip(original(h, pos, end), 120),
        context: (from > 0 ? '…' : '') + original(h, from, pos) + '[[' + clip(original(h, pos, end), 120) + ']]' + original(h, end, to) + (to < h.hay.length ? '…' : ''),
        ...semantics(startEl), nearest_heading: headingFor(sn) || null,
        visibility: visibilityOf(rect), in_viewport: visibilityOf(rect) === 'in_view' && rect.width > 0 && rect.height > 0,
        page_y: Math.round(rect.top + window.scrollY),
      };
      if (ia) rec.local_id = ia.local_id;
      return { rec, range, startEl };
    };

    for (const run of runs) {
      if (performance.now() > deadline) { truncated = true; break; }
      const h = buildHay(run.nodes, caseSensitive);
      let pos = h.hay.indexOf(q);
      while (pos !== -1) {
        const end = pos + q.length;
        if (!wholeWord || (!isWordChar(h.hay[pos - 1]) && !isWordChar(h.hay[end]))) {
          if (!run.vis) hidden++;
          else {
            if (total >= FIND_MAX_MATCHES) { capped = true; break; }
            const wantRec = matches.length < maxMatches || (wantScroll && total === focusIdx);
            if (wantRec) {
              const m = record(h, pos, end, total);
              if (matches.length < maxMatches) matches.push(m.rec);
              if (wantScroll && total === focusIdx) focus = m;
            }
            total++;
          }
        }
        pos = h.hay.indexOf(q, end);
      }
      if (capped) break;
    }

    // 3. bring the focused match into view (viewport only; the page itself is not modified)
    let focused = null, scroll = null;
    if (focus) {
      const before = { x: Math.round(window.scrollX), y: Math.round(window.scrollY) };
      try { focus.startEl.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (_) { /* detached; reported through in_viewport */ }
      let r = firstRect(focus.range);
      const box = scrollableAncestor(focus.startEl);
      if (box) { const cr = box.getBoundingClientRect(); box.scrollBy({ top: r.top + r.height / 2 - (cr.top + cr.height / 2), behavior: 'instant' }); r = firstRect(focus.range); }
      const dy = r.top + r.height / 2 - window.innerHeight / 2;
      if (Math.abs(dy) > 2) { window.scrollBy({ top: dy, behavior: 'instant' }); r = firstRect(focus.range); }
      try { if (window.frameElement) window.frameElement.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (_) { /* cross-origin frame: Chrome propagates the scroll itself */ }
      const se = document.scrollingElement || document.documentElement;
      const visAfter = visibilityOf(r);
      focused = { ...focus.rec, visibility: visAfter, in_viewport: visAfter === 'in_view' && r.width > 0 && r.height > 0, page_y: Math.round(r.top + window.scrollY), scrolled: true };
      scroll = { before, after: { x: Math.round(window.scrollX), y: Math.round(window.scrollY) }, max_y: Math.max(0, Math.round(se.scrollHeight - window.innerHeight)) };
    }
    return {
      ok: true, total, hidden, matches, focused, scroll, scan_truncated: truncated, count_capped: capped, snapshot_id: registry.snapshotId,
      url: location.href, title: document.title,
    };
  };

  // ---------------------------------------------------------------- evidence (before/after comparison)
  function hashStr(s) { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }

  OCTG.evidence = () => {
    const se = document.scrollingElement || document.documentElement;
    const text = ((document.body && document.body.innerText) || '').slice(0, 30000);
    const act = deepActive();
    return {
      ok: true,
      evidence: {
        url: location.href, title: document.title, ready: document.readyState,
        scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY), max_y: Math.max(0, se.scrollHeight - window.innerHeight) },
        text_len: text.length, text_sig: hashStr(text),
        interactive_estimate: document.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=link]').length,
        mutations: counters.mutations,
        dialogs: findDialogs().map((d) => d.text.slice(0, 80)),
        focused: act && act !== document.body ? describeShort(act) : null,
      },
    };
  };

  // ---------------------------------------------------------------- element resolution
  function resolve(a) {
    if (!a.local_id) return err('INVALID_PARAMS', 'Missing element reference.');
    if (registry.snapshotId !== a.snapshot_id) {
      return err('STALE_ELEMENT', 'Element belongs to snapshot ' + a.snapshot_id + ' but this document is at ' + (registry.snapshotId || 'no snapshot') + '. Re-run inspect_page.',
        { reason: registry.snapshotId ? 'snapshot_superseded' : 'document_changed' });
    }
    const rec = registry.map.get(a.local_id);
    if (!rec) return err('ELEMENT_NOT_FOUND', 'No element ' + a.local_id + ' in snapshot ' + a.snapshot_id + '.');
    const el = rec.ref.deref();
    if (!el || !el.isConnected) return err('STALE_ELEMENT', 'The element was removed from the page. Re-run inspect_page.', { reason: 'detached' });
    if (fingerprint(el) !== rec.fp) return err('STALE_ELEMENT', 'The element changed since it was inspected. Re-run inspect_page.', { reason: 'changed' });
    return { ok: true, el };
  }

  const isDisabled = (el) => !!el.disabled || el.getAttribute('aria-disabled') === 'true';

  function revealIfNeeded(el) {
    const r = el.getBoundingClientRect();
    if (r.top < 0 || r.left < 0 || r.bottom > window.innerHeight || r.right > window.innerWidth) {
      el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    }
  }

  // ---------------------------------------------------------------- click
  function mouseSeq(el, x, y) {
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, screenX: x, screenY: y, button: 0, detail: 1 };
    const ptr = { pointerId: 1, pointerType: 'mouse', isPrimary: true, width: 1, height: 1 };
    el.dispatchEvent(new PointerEvent('pointerdown', { ...base, ...ptr, buttons: 1 }));
    el.dispatchEvent(new MouseEvent('mousedown', { ...base, buttons: 1 }));
    if (typeof el.focus === 'function') el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent('pointerup', { ...base, ...ptr, buttons: 0 }));
    el.dispatchEvent(new MouseEvent('mouseup', { ...base, buttons: 0 }));
    // Anchors that would open a new tab are intercepted: without user activation Chrome blocks them.
    let blockedTarget = null;
    const guard = (e) => {
      if (e.defaultPrevented) return;
      const a = e.composedPath().find((n) => n && n.tagName === 'A' && n.href);
      if (a && a.target && !['_self', '_top', '_parent'].includes(a.target) && !/^javascript:/i.test(a.href)) {
        e.preventDefault();
        blockedTarget = a.href;
      }
    };
    window.addEventListener('click', guard, false);
    try { el.dispatchEvent(new MouseEvent('click', { ...base, buttons: 0 })); } finally { window.removeEventListener('click', guard, false); }
    return blockedTarget;
  }

  OCTG.click = (a) => {
    let el, x, y;
    if (a.local_id) {
      const r = resolve(a);
      if (!r.ok) return r;
      el = r.el;
      if (isDisabled(el)) return err('ELEMENT_NOT_INTERACTABLE', 'The element is disabled.', { element: describeShort(el) });
      if (el.tagName === 'INPUT' && el.type === 'file') return err('ELEMENT_NOT_INTERACTABLE', 'File inputs open a native chooser that cannot be driven without the debugger permission. Use upload_file.', { element: describeShort(el) });
      const visible = isVisible(el);
      if (visible) revealIfNeeded(el);
      const rect = el.getBoundingClientRect();
      x = Math.min(Math.max(rect.left + rect.width / 2, 1), window.innerWidth - 1);
      y = Math.min(Math.max(rect.top + rect.height / 2, 1), window.innerHeight - 1);
      if (visible && !a.force) {
        const hit = deepFromPoint(x, y);
        if (hit && !composedContains(el, hit) && !composedContains(hit, el)) {
          return err('ELEMENT_NOT_INTERACTABLE', 'Another element covers the click point.', { element: describeShort(el), obscured_by: describeShort(hit) });
        }
      }
    } else if (typeof a.x === 'number' && typeof a.y === 'number') {
      x = a.x; y = a.y;
      if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return err('INVALID_PARAMS', 'Coordinates are outside the viewport.', { viewport: { w: window.innerWidth, h: window.innerHeight } });
      el = deepFromPoint(x, y);
      if (!el) return err('ELEMENT_NOT_FOUND', 'Nothing at the given coordinates.');
    } else return err('INVALID_PARAMS', 'click needs element_id or x and y.');
    const open = mouseSeq(el, x, y);
    return { ok: true, element: describeShort(el), clicked_at: { x: Math.round(x), y: Math.round(y) }, ...(open ? { open_in_new_tab: open } : {}) };
  };

  // ---------------------------------------------------------------- typing
  function nativeSet(el, v) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
  }
  const fire = (el, type, init) => el.dispatchEvent(new (type === 'input' || type === 'beforeinput' ? InputEvent : Event)(type, { bubbles: true, cancelable: type === 'beforeinput', composed: true, ...(init || {}) }));
  const readValue = (el, kind) => (kind === 'contenteditable' ? el.innerText : el.value);
  const norm = (s) => String(s).replace(/\r\n/g, '\n');

  function writeText(el, kind, text, clear, mode) {
    el.focus({ preventScroll: true });
    if (kind === 'contenteditable') {
      if (clear) {
        const sel = getSelection(); const range = document.createRange();
        range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
        document.execCommand('delete');
      } else {
        const sel = getSelection(); sel.selectAllChildren(el); sel.collapseToEnd();
      }
      if (text) { if (!document.execCommand('insertText', false, text)) { el.textContent = (clear ? '' : el.textContent) + text; fire(el, 'input', { inputType: 'insertText', data: text }); } }
      else fire(el, 'input', { inputType: 'deleteContentBackward' });
      return;
    }
    if (mode === 'keys') {
      if (clear) { nativeSet(el, ''); fire(el, 'input', { inputType: 'deleteContentBackward' }); }
      for (const ch of text) {
        const kInit = { key: ch, bubbles: true, cancelable: true, composed: true };
        el.dispatchEvent(new KeyboardEvent('keydown', kInit));
        el.dispatchEvent(new KeyboardEvent('keypress', kInit));
        nativeSet(el, el.value + ch);
        fire(el, 'input', { inputType: 'insertText', data: ch });
        el.dispatchEvent(new KeyboardEvent('keyup', kInit));
      }
    } else {
      nativeSet(el, clear ? text : el.value + text);
      fire(el, 'input', { inputType: clear ? 'insertReplacementText' : 'insertText', data: text });
    }
    fire(el, 'change');
  }

  OCTG.type = (a) => {
    const r = resolve(a);
    if (!r.ok) return r;
    const el = r.el;
    const kind = editableKind(el);
    if (!kind) return err('NOT_EDITABLE', 'The element does not accept text input.', { element: describeShort(el) });
    if (isDisabled(el)) return err('ELEMENT_NOT_INTERACTABLE', 'The field is disabled.', { element: describeShort(el) });
    if (el.readOnly) return err('ELEMENT_NOT_INTERACTABLE', 'The field is read-only.', { element: describeShort(el) });
    if (isVisible(el)) revealIfNeeded(el);
    const text = String(a.text == null ? '' : a.text);
    const clear = a.clear !== false;
    const before = readValue(el, kind);
    writeText(el, kind, text, clear, a.mode);
    const after = readValue(el, kind);
    const expected = clear ? text : before + text;
    const isPw = isSensitive(el);
    const matches = norm(after) === norm(expected);
    const res = {
      ok: true, element: describeShort(el), typed_length: text.length, value_matches: matches,
      value_after: isPw ? '[hidden: ' + after.length + ' chars]' : clip(after, 200), value_length: after.length,
    };
    if (!matches) res.note = 'Field content differs from what was typed (input mask, maxlength or the page rewrote the value).';
    return res;
  };

  OCTG.clear = (a) => { const r = OCTG.type({ ...a, text: '', clear: true }); if (r.ok && r.value_length !== 0) r.value_matches = false; return r; };

  // ---------------------------------------------------------------- select / check
  OCTG.select = (a) => {
    const r = resolve(a);
    if (!r.ok) return r;
    const el = r.el;
    if (el.tagName !== 'SELECT') return err('NOT_A_SELECT', 'Not a native <select>. For custom dropdowns click to open it, then click the option.', { element: describeShort(el) });
    if (isDisabled(el)) return err('ELEMENT_NOT_INTERACTABLE', 'The select is disabled.', { element: describeShort(el) });
    const opts = [...el.options];
    const wants = (Array.isArray(a.values) ? a.values : [a.option != null ? a.option : a.value != null ? a.value : a.label != null ? a.label : a.index]).filter((v) => v !== undefined);
    if (!wants.length) return err('INVALID_PARAMS', 'select needs option (value, label or index).');
    const picked = [];
    for (const w of wants) {
      let o = typeof w === 'number' ? opts[w] : null;
      if (!o) o = opts.find((x) => x.value === String(w));
      if (!o) o = opts.find((x) => x.text.trim() === String(w).trim());
      if (!o) o = opts.find((x) => x.text.trim().toLowerCase() === String(w).trim().toLowerCase());
      if (!o) o = opts.find((x) => x.text.toLowerCase().includes(String(w).toLowerCase()));
      if (!o) return err('OPTION_NOT_FOUND', 'No option matches ' + JSON.stringify(w) + '.', { available: opts.slice(0, 30).map((x) => ({ value: x.value, label: clip(x.text, 60) })) });
      if (o.disabled) return err('OPTION_DISABLED', 'The option ' + JSON.stringify(w) + ' is disabled.');
      picked.push(o);
    }
    if (isVisible(el)) revealIfNeeded(el);
    el.focus({ preventScroll: true });
    if (el.multiple) opts.forEach((o) => { o.selected = picked.includes(o); }); else el.selectedIndex = picked[0].index;
    fire(el, 'input'); fire(el, 'change');
    const now = opts.filter((o) => o.selected);
    const ok = picked.every((o) => o.selected) && now.length === picked.length;
    return { ok: true, element: describeShort(el), selected: now.map((o) => ({ value: o.value, label: clip(o.text, 60), index: o.index })), value_matches: ok };
  };

  function checkedState(el) {
    if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) return !!el.checked;
    const ac = el.getAttribute('aria-checked');
    return ac === null ? null : ac === 'true';
  }

  OCTG.setChecked = async (a) => {
    const r = resolve(a);
    if (!r.ok) return r;
    const el = r.el;
    const want = a.checked !== false;
    const cur = checkedState(el);
    if (cur === null) return err('NOT_CHECKABLE', 'The element is not a checkbox, radio or ARIA checkable control.', { element: describeShort(el) });
    if (isDisabled(el)) return err('ELEMENT_NOT_INTERACTABLE', 'The control is disabled.', { element: describeShort(el) });
    if (el.type === 'radio' && !want) return err('NOT_CHECKABLE', 'A radio button cannot be unchecked directly; select another radio in the group.', { element: describeShort(el) });
    if (cur !== want) {
      const visible = isVisible(el);
      if (visible) revealIfNeeded(el);
      const rect = el.getBoundingClientRect();
      const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
      if (visible && !a.force) {
        const hit = deepFromPoint(Math.min(Math.max(x, 1), window.innerWidth - 1), Math.min(Math.max(y, 1), window.innerHeight - 1));
        if (hit && !composedContains(el, hit) && !composedContains(hit, el)) return err('ELEMENT_NOT_INTERACTABLE', 'Another element covers the control.', { element: describeShort(el), obscured_by: describeShort(hit) });
      }
      mouseSeq(el, x, y);
      await sleep(60);
    }
    const now = checkedState(el);
    return { ok: true, element: describeShort(el), checked: now, was: cur, value_matches: now === want };
  };

  // ---------------------------------------------------------------- scroll
  function scrollableAncestor(el) {
    for (let n = el; n && n !== document.body; n = composedParent(n)) {
      const cs = getComputedStyle(n);
      if (/(auto|scroll)/.test(cs.overflowY + cs.overflowX) && (n.scrollHeight > n.clientHeight + 2 || n.scrollWidth > n.clientWidth + 2)) return n;
    }
    return null;
  }

  OCTG.scroll = (a) => {
    let target = null; // null => window
    if (a.local_id) {
      const r = resolve(a);
      if (!r.ok) return r;
      if (!a.direction && !a.to) {
        r.el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
        const rect = r.el.getBoundingClientRect();
        return { ok: true, mode: 'into_view', element: describeShort(r.el), in_viewport: rect.bottom > 0 && rect.top < window.innerHeight, moved: true, reached_limit: false, value_matches: true };
      }
      target = scrollableAncestor(r.el);
    }
    const se = document.scrollingElement || document.documentElement;
    if (!target && !a.local_id && se.scrollHeight - window.innerHeight <= 2 && se.scrollWidth - window.innerWidth <= 2) {
      target = scrollableAncestor(deepFromPoint(window.innerWidth / 2, window.innerHeight / 2) || document.body);
    }
    const pos = () => (target ? { x: target.scrollLeft, y: target.scrollTop } : { x: window.scrollX, y: window.scrollY });
    const limits = () => (target
      ? { x: target.scrollWidth - target.clientWidth, y: target.scrollHeight - target.clientHeight }
      : { x: se.scrollWidth - window.innerWidth, y: se.scrollHeight - window.innerHeight });
    const viewH = target ? target.clientHeight : window.innerHeight;
    const viewW = target ? target.clientWidth : window.innerWidth;
    const before = pos();
    const lim0 = limits();
    let dx = 0, dy = 0;
    const amt = a.amount === 'page' ? undefined : (typeof a.amount === 'number' ? a.amount : undefined);
    const stepY = amt !== undefined ? amt : Math.round(viewH * 0.85);
    const stepX = amt !== undefined ? amt : Math.round(viewW * 0.85);
    if (a.to === 'top') dy = -before.y; else if (a.to === 'bottom') dy = lim0.y - before.y;
    else if (a.to === 'left') dx = -before.x; else if (a.to === 'right') dx = lim0.x - before.x;
    else if (a.direction === 'down') dy = stepY; else if (a.direction === 'up') dy = -stepY;
    else if (a.direction === 'right') dx = stepX; else if (a.direction === 'left') dx = -stepX;
    else return err('INVALID_PARAMS', 'scroll needs direction (up/down/left/right) or to (top/bottom/left/right) or an element_id alone.');
    if (target) target.scrollBy({ left: dx, top: dy, behavior: 'instant' }); else window.scrollBy({ left: dx, top: dy, behavior: 'instant' });
    const after = pos();
    const lim = limits();
    const moved = Math.abs(after.x - before.x) > 0.5 || Math.abs(after.y - before.y) > 0.5;
    const atEdge = (dy > 0 && after.y >= lim.y - 1) || (dy < 0 && after.y <= 1) || (dx > 0 && after.x >= lim.x - 1) || (dx < 0 && after.x <= 1) || (dx === 0 && dy === 0);
    return {
      ok: true, mode: 'by', container: target ? describeShort(target) : 'window',
      before: { x: Math.round(before.x), y: Math.round(before.y) }, after: { x: Math.round(after.x), y: Math.round(after.y) },
      max: { x: Math.round(Math.max(0, lim.x)), y: Math.round(Math.max(0, lim.y)) },
      moved, reached_limit: atEdge, value_matches: moved || atEdge,
    };
  };

  // ---------------------------------------------------------------- keyboard
  const KEYCODES = { Enter: 13, Tab: 9, Escape: 27, ' ': 32, Backspace: 8, Delete: 46, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34 };
  const codeFor = (k) => (/^[a-z]$/i.test(k) ? 'Key' + k.toUpperCase() : /^[0-9]$/.test(k) ? 'Digit' + k : k === ' ' ? 'Space' : k);

  function focusables() {
    return [...walkElements(document)].filter((e) => e.matches('a[href],button,input:not([type=hidden]),select,textarea,summary,[tabindex],[contenteditable=""],[contenteditable=true]') && !isDisabled(e) && e.tabIndex >= 0 && isVisible(e));
  }

  OCTG.pressKey = (a) => {
    let el;
    if (a.local_id) { const r = resolve(a); if (!r.ok) return r; el = r.el; } else el = deepActive() || document.body;
    const key = a.key;
    if (!key) return err('INVALID_PARAMS', 'press_key needs key.');
    const mods = new Set((a.modifiers || []).map((m) => String(m).toLowerCase()));
    const init = { key, code: codeFor(key), keyCode: KEYCODES[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0), bubbles: true, cancelable: true, composed: true,
      ctrlKey: mods.has('ctrl') || mods.has('control'), shiftKey: mods.has('shift'), altKey: mods.has('alt'), metaKey: mods.has('meta') || mods.has('cmd') };
    if (a.local_id && typeof el.focus === 'function') el.focus({ preventScroll: true });
    const down = new KeyboardEvent('keydown', init);
    const notPrevented = el.dispatchEvent(down);
    let action = null, note;
    if (notPrevented) {
      const kind = editableKind(el);
      const ctrl = init.ctrlKey || init.metaKey;
      if (key === 'Enter') {
        if (el.tagName === 'A' || el.tagName === 'BUTTON' || (el.tagName === 'INPUT' && ['button', 'submit', 'reset', 'checkbox', 'radio'].includes(el.type)) || el.getAttribute('role') === 'button') { el.click(); action = 'activate'; }
        else if (kind === 'input' && el.form) {
          if (el.form.checkValidity()) { el.form.requestSubmit(); action = 'form_submit'; } else { el.form.reportValidity(); action = 'form_submit_blocked'; note = 'Form validation failed.'; }
        } else if (kind === 'textarea') { writeText(el, kind, '\n', false); action = 'newline'; }
        else if (kind === 'contenteditable') { document.execCommand('insertParagraph'); action = 'newline'; }
      } else if (key === 'Tab') {
        const list = focusables(); const i = list.indexOf(el);
        const next = list[(i + (init.shiftKey ? -1 : 1) + list.length) % list.length];
        if (next) { next.focus(); action = 'focus_moved'; }
      } else if (key === ' ' && !kind && (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' || (el.tagName === 'INPUT' && ['checkbox', 'radio'].includes(el.type)))) { el.click(); action = 'activate'; }
      else if (kind && ctrl && key.toLowerCase() === 'a') { if (kind === 'contenteditable') document.execCommand('selectAll'); else el.select(); action = 'select_all'; }
      else if (kind && (key === 'Backspace' || key === 'Delete')) {
        if (kind === 'contenteditable') document.execCommand(key === 'Backspace' ? 'delete' : 'forwardDelete');
        else {
          const s = el.selectionStart, e = el.selectionEnd;
          let from = s, to = e;
          if (s === e) { if (key === 'Backspace') from = Math.max(0, s - 1); else to = Math.min(el.value.length, e + 1); }
          nativeSet(el, el.value.slice(0, from) + el.value.slice(to)); el.setSelectionRange(from, from);
          fire(el, 'input', { inputType: key === 'Backspace' ? 'deleteContentBackward' : 'deleteContentForward' });
        }
        action = 'delete';
      } else if (kind && key.length === 1 && !ctrl) { writeText(el, kind, key, false); action = 'insert_text'; }
    }
    el.dispatchEvent(new KeyboardEvent('keyup', init));
    return { ok: true, key, target: describeShort(el), default_prevented_by_page: !notPrevented, default_action: action, ...(note ? { note } : {}) };
  };

  // ---------------------------------------------------------------- history (used when the tabs API skips non-activated entries)
  OCTG.historyGo = (a) => {
    const nav = window.navigation;
    const can = nav ? (a.delta < 0 ? nav.canGoBack : nav.canGoForward) : history.length > 1;
    if (!can) return err('NO_HISTORY', 'The page has no history entry in that direction.');
    history.go(a.delta);
    return { ok: true, via: 'history.go' };
  };

  // ---------------------------------------------------------------- upload (no debugger: DataTransfer)
  function b64ToBytes(b64) {
    const bin = atob(b64); const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  OCTG.upload = (a) => {
    const r = resolve(a);
    if (!r.ok) return r;
    const el = r.el;
    if (!(el.tagName === 'INPUT' && el.type === 'file')) return err('NOT_A_FILE_INPUT', 'The element is not an <input type=file>.', { element: describeShort(el) });
    if (isDisabled(el)) return err('ELEMENT_NOT_INTERACTABLE', 'The file input is disabled.', { element: describeShort(el) });
    const files = a.files || [];
    if (!files.length) return err('INVALID_PARAMS', 'No files supplied.');
    if (files.length > 1 && !el.multiple) return err('INVALID_PARAMS', 'This input accepts a single file.');
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(new File([b64ToBytes(f.data_b64)], f.name, { type: f.type || 'application/octet-stream', lastModified: Date.now() }));
    el.files = dt.files;
    fire(el, 'input'); fire(el, 'change');
    const got = [...el.files].map((f) => ({ name: f.name, size: f.size, type: f.type }));
    const ok = got.length === files.length && got.every((g, i) => g.name === files[i].name && g.size === files[i].size);
    return { ok: true, element: describeShort(el), files: got, value_matches: ok };
  };

  // ---------------------------------------------------------------- verification helpers
  OCTG.elementState = (a) => {
    const r = resolve(a);
    if (!r.ok) return r;
    const el = r.el; const kind = editableKind(el);
    return { ok: true, value: isSensitive(el) ? '[hidden]' : (kind ? readValue(el, kind) : ('value' in el ? el.value : null)), checked: checkedState(el), visible: isVisible(el), disabled: isDisabled(el), text: clip(el.innerText || el.textContent, 200) };
  };

  OCTG.findElements = (a) => {
    const budget = { heuristics: true, deadline: performance.now() + 300 };
    const nc = a.name_contains ? String(a.name_contains).toLowerCase() : null;
    const found = [];
    for (const el of walkElements(document)) {
      if (!isCandidate(el, budget) || !isVisible(el)) continue;
      if (a.role && roleOf(el) !== a.role) continue;
      if (a.tag && el.tagName.toLowerCase() !== String(a.tag).toLowerCase()) continue;
      if (nc && !nameOf(el).toLowerCase().includes(nc)) continue;
      found.push(describeShort(el));
      if (found.length >= 10) break;
    }
    return { ok: true, count: found.length, sample: found };
  };

  window.__octg = OCTG;
})();
