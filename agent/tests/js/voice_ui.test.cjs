'use strict';
// Unit tests for extension/sidepanel/voice_ui.js: the microphone button, the Task box, auto-run, and how voice reaches the EXISTING Run path.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { flush, FakeClock, FakeMedia, FakeEngine, El } = require('./helpers.cjs');

const SIDEPANEL = path.join(__dirname, '..', '..', '..', 'extension', 'sidepanel');
const STT = require(path.join(SIDEPANEL, 'stt.js'));
const UI = require(path.join(SIDEPANEL, 'voice_ui.js'));

function rig(opts) {
  opts = opts || {};
  const clock = new FakeClock();
  const engine = opts.engine || new FakeEngine();
  const media = opts.media || new FakeMedia();
  const els = { textarea: new El({ value: opts.text || '' }), mic: new El(), status: new El({ hidden: true }), install: new El({ hidden: true }), permission: new El({ hidden: true }),
                autorun: new El({ checked: opts.autorun !== false }), run: new El({ disabled: !!opts.runDisabled }) };
  // The "existing Run handler": registered AFTER voice_ui (as in the panel, where panel.js loads last), it just reads the Task box like a typed task.
  const submitted = [];
  const doc = new El();
  const ui = UI.mountVoiceInput({ STT, engine, mediaDevices: media, els, doc, language: opts.language, storage: { getItem: () => null }, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
                                  openPermissionPage: () => submitted.push('permission-page') });
  els.run.addEventListener('click', () => { const t = els.textarea.value.trim(); if (t) submitted.push(t); });
  return { ui, els, engine, media, clock, submitted, doc };
}
const speak = async (r, final, interim) => { await r.els.mic.click(); await flush(); r.engine.result(final || '', interim || ''); };
const stop = async (r) => { r.els.mic.click(); await flush(); };

test('1. initial state: idle, clickable, labelled, nothing shown', () => {
  const r = rig();
  assert.equal(r.els.mic.dataset.state, 'idle');
  assert.equal(r.els.mic.attrs['aria-pressed'], 'false');
  assert.equal(r.els.mic.attrs['aria-label'], 'Start voice input');
  assert.equal(r.els.mic.disabled, false);
  assert.equal(r.els.status.hidden, true);
  assert.equal(r.els.install.hidden && r.els.permission.hidden, true);
});

test('2/3. click starts listening (visible state), click again stops and shows Transcribing…', async () => {
  const r = rig();
  r.els.mic.click();
  await flush();
  assert.equal(r.els.mic.dataset.state, 'listening');
  assert.equal(r.els.mic.attrs['aria-pressed'], 'true');
  assert.equal(r.els.mic.title, 'Stop listening');
  assert.match(r.els.status.textContent, /Listening/);
  r.engine.autoEndOnStop = false;
  r.els.mic.click();
  assert.equal(r.els.mic.dataset.state, 'processing');
  assert.match(r.els.status.textContent, /Transcribing/);
  assert.equal(r.els.mic.disabled, true, 'no duplicate start while transcribing');
});

test('4/11. interim text appears in the Task box as it is spoken and is replaced, not piled up', async () => {
  const r = rig();
  await speak(r, '', 'open');
  assert.equal(r.els.textarea.value, 'open');
  r.engine.result('', 'open google');
  assert.equal(r.els.textarea.value, 'open google');
  r.engine.result('open google', 'and search');
  assert.equal(r.els.textarea.value, 'open google and search');
});

test('11. dictation is appended to what was already typed, never overwriting it', async () => {
  const r = rig({ text: 'Please', autorun: false });
  await speak(r, 'open google', '');
  assert.equal(r.els.textarea.value, 'Please open google');
  await stop(r);
  assert.equal(r.els.textarea.value, 'Please open google');
});

test('5/12. final transcript is kept in the Task box and the microphone returns to idle', async () => {
  const r = rig({ autorun: false });
  await speak(r, 'Open Google and search for One Chance to Grow.', '');
  await stop(r);
  assert.equal(r.els.textarea.value, 'Open Google and search for One Chance to Grow.');
  assert.equal(r.els.mic.dataset.state, 'idle');
  assert.equal(r.els.mic.disabled, false);
  assert.deepEqual(r.submitted, [], 'with auto-run off nothing is submitted');
});

test('6. empty speech: the Task box is restored, a short message is shown and NOTHING is submitted', async () => {
  const r = rig({ text: 'typed earlier' });
  r.els.mic.click();
  await flush();
  r.els.mic.click();
  await flush();
  assert.equal(r.els.textarea.value, 'typed earlier');
  assert.match(r.els.status.textContent, /didn't catch/i);
  await r.clock.tick(10000);
  assert.deepEqual(r.submitted, []);
  assert.equal(r.els.mic.dataset.state, 'idle');
  assert.equal(r.ui.autoRunPending, false);
});

test('7/13. a recognition error shows a concise message, keeps the words, and the microphone can be retried', async () => {
  const r = rig();
  await speak(r, 'open', 'goo');
  r.engine.error('network');
  assert.equal(r.els.mic.dataset.state, 'error');
  assert.equal(r.els.mic.disabled, false, 'retry is possible');
  assert.match(r.els.status.textContent, /lost its connection/);
  assert.match(r.els.status.textContent, /check it before running/);
  assert.equal(r.els.textarea.value, 'open goo');
  await r.clock.tick(10000);
  assert.deepEqual(r.submitted, [], 'a failed dictation is never auto-run');
  r.els.mic.click();
  await flush();
  assert.equal(r.els.mic.dataset.state, 'listening');
});

test('8. permission denied: says so, offers the permission page, and retry works', async () => {
  const media = new FakeMedia();
  media.failWith = 'NotAllowedError';
  const r = rig({ media });
  r.els.mic.click();
  await flush();
  assert.equal(r.els.mic.dataset.state, 'permission_denied');
  assert.match(r.els.status.textContent, /Microphone permission is required/);
  assert.equal(r.els.permission.hidden, false);
  r.els.permission.click();
  assert.deepEqual(r.submitted, ['permission-page']);
  media.failWith = null;
  r.els.mic.click();
  await flush();
  assert.equal(r.els.mic.dataset.state, 'listening');
  assert.equal(r.els.permission.hidden, true);
});

test('9. unsupported: explains why, and offers the explicit one-time install only when that is what is missing', async () => {
  let r = rig({ engine: new FakeEngine({ status: 'unavailable', reason: 'no_api', local: false }) });
  r.els.mic.click();
  await flush();
  assert.equal(r.els.mic.dataset.state, 'unsupported');
  assert.match(r.els.status.textContent, /isn't supported by this browser/);
  assert.equal(r.els.install.hidden, true);
  r = rig({ engine: new FakeEngine({ status: 'unavailable', reason: 'no_local_engine', local: false }) });
  r.els.mic.click();
  await flush();
  assert.match(r.els.status.textContent, /cloud recognition is off/);
  assert.match(r.els.status.textContent, /send your audio to Google/, 'it says plainly what cloud recognition would mean');
  const engine = new FakeEngine({ status: 'needs_install', reason: 'needs_install', local: true, canInstall: true });
  r = rig({ engine });
  r.els.mic.click();
  await flush();
  assert.equal(r.els.install.hidden, false);
  assert.match(r.els.status.textContent, /one-time download/);
  assert.equal(engine.installs.length, 0, 'nothing is downloaded until the user clicks install');
  r.els.install.click();
  await flush();
  assert.deepEqual(engine.installs, ['en-US']);
});

test('10. while a dictation is running a second click stops it instead of starting another', async () => {
  const r = rig({ autorun: false });
  r.els.mic.click();
  r.els.mic.click(); // before the first start finished: ignored
  await flush();
  assert.equal(r.engine.starts.length, 1);
  assert.equal(r.media.requests.length, 1);
});

test('14. auto-run presses the EXISTING Run button (same handler a typed task uses) with exactly the transcript, after a short review window', async () => {
  const r = rig();
  await speak(r, 'Open Google and search for One Chance to Grow.', '');
  await stop(r);
  assert.deepEqual(r.submitted, [], 'not instantly: the transcript can still be reviewed');
  assert.equal(r.ui.autoRunPending, true);
  assert.match(r.els.status.textContent, /Running in 2 s/);
  await r.clock.tick(UI.AUTORUN_DELAY_MS - 1);
  assert.deepEqual(r.submitted, []);
  await r.clock.tick(2);
  assert.deepEqual(r.submitted, ['Open Google and search for One Chance to Grow.']);
  assert.equal(r.ui.autoRunPending, false);
});

test('14b. editing the text, pressing Esc, or clicking the microphone cancels a pending auto-run', async () => {
  for (const how of ['edit', 'escape', 'mic']) {
    const r = rig();
    await speak(r, 'delete everything', '');
    await stop(r);
    assert.equal(r.ui.autoRunPending, true);
    if (how === 'edit') { r.els.textarea.value += ' not'; r.els.textarea.dispatch('input'); }
    else if (how === 'escape') r.doc.dispatch('keydown', { key: 'Escape' });
    else { r.els.mic.click(); await flush(); }
    await r.clock.tick(5000);
    assert.deepEqual(r.submitted, [], how);
  }
});

test('14c. auto-run respects the existing availability of Run: disabled (no agent / task active) means no run', async () => {
  const r = rig({ runDisabled: true });
  await speak(r, 'open google', '');
  await stop(r);
  assert.equal(r.ui.autoRunPending, false);
  assert.match(r.els.status.textContent, /Press Run when the agent is available/);
  await r.clock.tick(5000);
  assert.deepEqual(r.submitted, []);
  assert.equal(r.els.textarea.value, 'open google');
});

test('14d. a manual Run click is not blocked once dictation has finished, and it supersedes the pending auto-run', async () => {
  const r = rig();
  await speak(r, 'open google', '');
  await stop(r);
  r.els.run.click();
  assert.deepEqual(r.submitted, ['open google']);
  await r.clock.tick(5000);
  assert.deepEqual(r.submitted, ['open google'], 'no second submission from the timer');
});

test('14e. Run mid-sentence is refused (the transcript is not final); nothing reaches the Run handler', async () => {
  const r = rig({ autorun: false, text: 'x' });
  await speak(r, '', 'open goo');
  r.els.run.click();
  assert.deepEqual(r.submitted, []);
  assert.match(r.els.status.textContent, /Finish speaking first/);
  assert.equal(r.els.mic.dataset.state, 'listening', 'dictation continues');
});

test('one task at a time: a running task disables the microphone and abandons a dictation in progress', async () => {
  const r = rig({ text: 'before' });
  await speak(r, 'half a sent', '');
  r.ui.sync({ active: true });
  assert.equal(r.els.mic.disabled, true);
  assert.equal(r.els.mic.dataset.state, 'idle');
  assert.equal(r.els.textarea.value, 'before', 'the unfinished dictation does not pollute the Task box');
  assert.equal(r.engine.aborts, 1);
  r.els.mic.click();
  await flush();
  assert.equal(r.engine.starts.length, 1, 'the microphone does not start while a task runs');
  r.ui.sync({ active: false });
  assert.equal(r.els.mic.disabled, false);
});

test('a pending auto-run is cancelled if a task becomes active first (never two tasks)', async () => {
  const r = rig();
  await speak(r, 'open google', '');
  await stop(r);
  r.ui.sync({ active: true });
  await r.clock.tick(5000);
  assert.deepEqual(r.submitted, []);
});

test('the language comes from configuration (navigator.language), not from the code', async () => {
  const r = rig({ language: 'hi-IN', autorun: false });
  r.els.mic.click();
  await flush();
  assert.equal(r.engine.lastLang, 'hi-IN');
});

test('the engine can be swapped (e.g. for a local Whisper service) without touching the UI', async () => {
  const r = rig({ autorun: false });
  const other = new FakeEngine();
  r.ui.setEngine(other);
  r.els.mic.click();
  await flush();
  assert.equal(other.starts.length, 1);
  assert.equal(r.engine.starts.length, 0);
});

// ------------------------------------------------------------------ architecture and privacy guards (static)
const src = (f) => fs.readFileSync(path.join(SIDEPANEL, f), 'utf8');

test('architecture: voice never submits a task itself; the panel has exactly one task-submission site and voice only clicks Run', () => {
  const panel = src('panel.js'), voice = src('voice_ui.js'), stt = src('stt.js');
  assert.equal((panel.match(/'start_task'/g) || []).length, 1, 'one start_task site in the panel: the Run handler');
  for (const [name, text] of [['voice_ui.js', voice], ['stt.js', stt]]) {
    for (const banned of ['start_task', 'chrome.runtime.connect', 'chrome.runtime.sendMessage', 'port.postMessage', 'WebSocket', 'fetch(', 'XMLHttpRequest', 'octg_agent', '/api/']) {
      assert.ok(!text.includes(banned), `${name} must not use ${banned}`);
    }
  }
  assert.ok(voice.includes('els.run.click()'), 'auto-run goes through the existing Run button');
  assert.ok(panel.includes("window.octgVoice.sync({ active })"), 'voice follows the panel\'s one-task-at-a-time state');
  const html = src('panel.html');
  assert.ok(html.indexOf('voice_ui.js') < html.indexOf('panel.js') && html.indexOf('stt.js') < html.indexOf('voice_ui.js'), 'load order: stt.js, voice_ui.js, panel.js');
});

test('privacy: no audio capture or storage, no persisted or logged transcripts, no new manifest permissions', () => {
  for (const f of ['stt.js', 'voice_ui.js', 'mic_permission.js']) {
    const text = src(f);
    for (const banned of ['MediaRecorder', 'AudioContext', 'sendBeacon', 'indexedDB', 'sessionStorage', 'chrome.storage', 'console.', 'Blob(', 'FileReader']) {
      assert.ok(!text.includes(banned), `${f} must not use ${banned}`);
    }
  }
  const ui = src('voice_ui.js');
  assert.ok(!/localStorage\.setItem|storage\.setItem/.test(ui), 'the transcript is never persisted by voice code');
  const manifest = JSON.parse(fs.readFileSync(path.join(SIDEPANEL, '..', 'manifest.json'), 'utf8'));
  assert.deepEqual(manifest.permissions.slice().sort(), ['alarms', 'downloads', 'scripting', 'sidePanel', 'storage', 'tabs', 'webNavigation'], 'voice added no permission');
  assert.deepEqual(manifest.host_permissions, ['<all_urls>']);
  assert.ok(!('optional_permissions' in manifest) && !('content_security_policy' in manifest));
});
