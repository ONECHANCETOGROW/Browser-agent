// Shared helpers for the OCTG service worker.

export const VERSION = '0.1.0';
export const PROTOCOL = 1;

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class ActionError extends Error {
  constructor(code, message, details) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

export function errorPayload(e) {
  if (e instanceof ActionError) return { code: e.code, message: e.message, ...(e.details ? { details: e.details } : {}) };
  return { code: 'INTERNAL_ERROR', message: String((e && e.message) || e) };
}

export function withTimeout(promise, ms, code, message) {
  let timer;
  const t = new Promise((_, rej) => { timer = setTimeout(() => rej(new ActionError(code, message)), ms); });
  return Promise.race([promise, t]).finally(() => clearTimeout(timer));
}

// ---------------------------------------------------------------- restricted page handling
export const UNSUPPORTED_MESSAGE = 'This Chrome page cannot be controlled by the extension.';

let fileAccess = false;
export async function refreshFileAccess() {
  try { fileAccess = await chrome.extension.isAllowedFileSchemeAccess(); } catch (_) { fileAccess = false; }
  return fileAccess;
}

const SCHEME_REASONS = {
  'chrome:': 'Chrome internal pages (chrome://) block extension scripts.',
  'chrome-untrusted:': 'Chrome internal pages block extension scripts.',
  'chrome-search:': 'Chrome internal pages block extension scripts.',
  'chrome-extension:': 'Extension pages cannot be scripted by another extension.',
  'chrome-error:': 'This is Chrome\'s network error page.',
  'devtools:': 'DevTools pages cannot be scripted.',
  'view-source:': 'view-source pages cannot be scripted.',
  'about:': 'about: pages cannot be scripted.',
  'edge:': 'Browser internal pages block extension scripts.',
  'data:': 'data: documents cannot be scripted.',
  'blob:': 'blob: documents cannot be scripted.',
  'ftp:': 'FTP pages cannot be scripted.',
};

/** Decide whether the extension can inject into a page with this URL. Pure apart from the cached file-access flag. */
export function classifyUrl(url) {
  if (!url) return { supported: false, reason: 'The tab has no committed URL yet.' };
  let u;
  try { u = new URL(url); } catch (_) { return { supported: false, reason: 'The URL could not be parsed.' }; }
  if (u.protocol === 'http:' || u.protocol === 'https:') {
    // The OCTG Knowledge Hub is the owner's own management page for their business data. The browser agent (and so a model) must never read, click or type there,
    // whatever the task says: on this machine's loopback address, anything under /hub is a page it may not script. (The Hub also refuses requests that do not come from its own page.)
    if (['127.0.0.1', 'localhost', '[::1]', '::1'].includes(u.hostname) && (u.pathname === '/hub' || u.pathname.startsWith('/hub/'))) {
      return { supported: false, reason: "This is the OCTG Knowledge Hub, the owner's own management page. The browser agent cannot use it." };
    }
    if (u.hostname === 'chromewebstore.google.com' || (u.hostname === 'chrome.google.com' && u.pathname.startsWith('/webstore'))) {
      return { supported: false, reason: 'The Chrome Web Store blocks extension scripts.' };
    }
    return { supported: true };
  }
  if (u.protocol === 'file:') {
    return fileAccess ? { supported: true } : { supported: false, reason: 'Enable "Allow access to file URLs" for this extension to control local files.' };
  }
  return { supported: false, reason: SCHEME_REASONS[u.protocol] || `The ${u.protocol} scheme cannot be scripted.` };
}

export function unsupportedError(tab, cls) {
  return new ActionError('UNSUPPORTED_PAGE', UNSUPPORTED_MESSAGE, { url: tab && (tab.url || tab.pendingUrl), reason: (cls && cls.reason) || undefined });
}

/** Translate raw chrome.* failures into structured errors. */
export function mapChromeError(e, tab) {
  if (e instanceof ActionError) return e;
  const m = String((e && e.message) || e);
  if (/cannot access|extensions gallery|cannot be scripted|not allowed to access|chrome-error|is not allowed|activeTab/i.test(m)) {
    return new ActionError('UNSUPPORTED_PAGE', UNSUPPORTED_MESSAGE, { url: tab && (tab.url || tab.pendingUrl), reason: m });
  }
  if (/no tab with id|tab.*(was )?closed/i.test(m)) return new ActionError('TAB_NOT_FOUND', m);
  if (/frame.*(was )?removed|no frame with id|document.*(was )?unloaded|context.*(destroyed|invalidated)/i.test(m)) {
    return new ActionError('PAGE_NAVIGATED', 'The page navigated or was reloaded while the action ran. Re-run inspect_page and retry.', { raw: m });
  }
  return new ActionError('INTERNAL_ERROR', m);
}
