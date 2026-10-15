'use strict';
// The browser agent must never be able to operate the OCTG Knowledge Hub (the owner's data-management page): the extension classifies every Hub page as one it may not script.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

globalThis.chrome = globalThis.chrome || { extension: { isAllowedFileSchemeAccess: async () => false } };
const common = () => import(pathToFileURL(path.join(__dirname, '..', '..', '..', 'extension', 'background', 'common.js')).href);

test('every page of the Knowledge Hub is unsupported for the agent, on any loopback name and port', async () => {
  const { classifyUrl } = await common();
  for (const u of ['http://127.0.0.1:8765/hub/', 'http://127.0.0.1:8765/hub', 'http://localhost:8765/hub/#/clients/cli_1', 'http://127.0.0.1:9999/hub/static/app.js', 'http://[::1]:8765/hub/api/session', 'http://localhost/hub/#/my-business']) {
    const c = classifyUrl(u);
    assert.equal(c.supported, false, u);
    assert.match(c.reason, /Knowledge Hub/, u);
  }
});

test('ordinary pages, other local pages and look-alikes stay supported', async () => {
  const { classifyUrl } = await common();
  for (const u of ['https://example.com/hub/', 'http://127.0.0.1:3000/', 'http://127.0.0.1:3000/app/hub', 'http://localhost:8765/hubspot', 'https://shop.test/', 'http://127.0.0.1:8765/healthz', 'http://127.0.0.1.evil.test/hub/']) {
    assert.equal(classifyUrl(u).supported, true, u);
  }
});

test('the other restrictions are unchanged', async () => {
  const { classifyUrl } = await common();
  for (const u of ['chrome://settings', 'chrome-extension://abc/x.html', 'about:blank', 'data:text/html,hi', '']) assert.equal(classifyUrl(u).supported, false, u);
});
