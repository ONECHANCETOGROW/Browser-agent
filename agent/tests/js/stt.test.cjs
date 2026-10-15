'use strict';
// Unit tests for extension/sidepanel/stt.js: the speech-to-text controller and the Web Speech engine adapter.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { flush, FakeClock, FakeMedia, FakeEngine } = require('./helpers.cjs');

const STT = require(path.join(__dirname, '..', '..', '..', 'extension', 'sidepanel', 'stt.js'));
const { STATE } = STT;

function rig(opts) {
  opts = opts || {};
  const clock = new FakeClock();
  const engine = opts.engine || new FakeEngine();
  const media = opts.media || new FakeMedia();
  const log = { states: [], transcripts: [], finals: [] };
  const vi = STT.createVoiceInput({
    engine, mediaDevices: media, getLang: () => opts.lang || 'en-US', setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    onState: (s, i) => log.states.push([s, i]), onTranscript: (t) => log.transcripts.push(t), onFinal: (f) => log.finals.push(f),
  });
  return { vi, engine, media, clock, log };
}
const started = async (r) => { assert.equal(await r.vi.start(), true); assert.equal(r.vi.state, STATE.LISTENING); };

test('1. the microphone starts idle with nothing acquired', () => {
  const r = rig();
  assert.equal(r.vi.state, STATE.IDLE);
  assert.equal(r.media.requests.length, 0);
  assert.equal(r.engine.starts.length, 0);
});

test('2. start listening: asks for echo cancellation, noise suppression and auto gain, hands the engine the live track', async () => {
  const r = rig({ lang: 'en-IN' });
  await started(r);
  assert.deepEqual(r.media.requests[0], { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  assert.equal(r.engine.starts.length, 1);
  assert.equal(r.engine.starts[0].lang, 'en-IN', 'language comes from configuration, never hard-coded');
  assert.equal(r.engine.starts[0].track, r.media.streams[0].track);
  assert.equal(r.engine.starts[0].local, true);
  assert.deepEqual(r.log.states.map((s) => s[0]), [STATE.STARTING, STATE.LISTENING]);
  assert.deepEqual(r.log.states[1][1].applied, { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, 'what the browser actually granted is reported');
});

test('3. stop listening: processing, then idle once the engine delivers its last words; the microphone is released', async () => {
  const r = rig();
  await started(r);
  r.engine.result('', 'open google');
  assert.equal(r.vi.stop(), true);
  assert.equal(r.vi.state, STATE.PROCESSING);
  assert.equal(r.engine.stops, 1);
  await flush();
  assert.equal(r.vi.state, STATE.IDLE);
  assert.equal(r.media.streams[0].track.stopped, true, 'the microphone track is stopped');
  assert.equal(r.log.finals.length, 1);
});

test('4. interim transcript is reported while speaking and replaced, not accumulated', async () => {
  const r = rig();
  await started(r);
  r.engine.result('', 'open');
  r.engine.result('', 'open google');
  r.engine.result('open google', 'and search');
  assert.deepEqual(r.log.transcripts.map((t) => [t.text, t.interim, t.final]), [
    ['open', true, false], ['open google', true, false], ['open google and search', true, false]]);
});

test('5. final transcript: reported once, as final text', async () => {
  const r = rig();
  await started(r);
  r.engine.result('Open Google and search for One Chance to Grow.', '');
  r.vi.stop();
  await flush();
  assert.equal(r.log.finals.length, 1);
  assert.deepEqual(r.log.finals[0], { text: 'Open Google and search for One Chance to Grow.', empty: false });
  assert.equal(r.log.transcripts.at(-1).final, true);
  assert.equal(r.vi.state, STATE.IDLE);
});

test('6. empty speech: nothing is produced and the result says so (the UI never submits an empty task)', async () => {
  const r = rig();
  await started(r);
  r.vi.stop();
  await flush();
  assert.deepEqual(r.log.finals, [{ text: '', empty: true }]);
  assert.equal(r.vi.state, STATE.IDLE);
  const r2 = rig();
  await started(r2);
  r2.engine.error('no-speech');
  assert.deepEqual(r2.log.finals, [{ text: '', empty: true }], 'a no-speech timeout is an empty result, not an error');
  assert.equal(r2.vi.state, STATE.IDLE);
});

test('7. recognition error: reported, resources released, recognised words are not lost', async () => {
  const r = rig();
  await started(r);
  r.engine.result('open', 'goo');
  r.engine.error('network');
  assert.equal(r.vi.state, STATE.ERROR);
  assert.equal(r.vi.info.code, 'network');
  assert.equal(r.vi.info.kept, true);
  assert.equal(r.log.transcripts.at(-1).text, 'open goo', 'interim text is kept rather than discarded');
  assert.deepEqual(r.log.finals[0], { text: 'open goo', empty: false, error: true });
  assert.equal(r.media.streams[0].track.stopped, true);
});

test('7b. the microphone disappearing mid-session is an error, not a hang', async () => {
  const r = rig();
  await started(r);
  r.engine.result('open the', '');
  r.media.streams[0].track.fire('ended');
  assert.equal(r.vi.state, STATE.ERROR);
  assert.equal(r.vi.info.code, 'mic_disconnected');
  assert.equal(r.engine.aborts, 1);
  assert.equal(r.log.transcripts.at(-1).text, 'open the');
});

test('8. permission denied: its own state, the engine is never started, retry works once allowed', async () => {
  const media = new FakeMedia();
  media.failWith = 'NotAllowedError';
  const r = rig({ media });
  assert.equal(await r.vi.start(), false);
  assert.equal(r.vi.state, STATE.PERMISSION_DENIED);
  assert.equal(r.vi.info.code, 'permission_denied');
  assert.equal(r.engine.starts.length, 0);
  media.failWith = null; // the user allowed the microphone
  assert.equal(await r.vi.start(), true);
  assert.equal(r.vi.state, STATE.LISTENING);
});

test('8b. the engine itself reporting not-allowed is also PERMISSION_DENIED', async () => {
  const r = rig();
  await started(r);
  r.engine.error('not-allowed');
  assert.equal(r.vi.state, STATE.PERMISSION_DENIED);
});

test('9. unsupported: no API, no on-device engine, or a missing language pack are explained, and nothing is acquired', async () => {
  for (const [av, reason, canInstall] of [
    [{ status: 'unavailable', reason: 'no_api', local: false }, 'no_api', false],
    [{ status: 'unavailable', reason: 'no_local_engine', local: false }, 'no_local_engine', false],
    [{ status: 'needs_install', reason: 'needs_install', local: true, canInstall: true }, 'needs_install', true],
  ]) {
    const r = rig({ engine: new FakeEngine(av) });
    assert.equal(await r.vi.start(), false);
    assert.equal(r.vi.state, STATE.UNSUPPORTED);
    assert.equal(r.vi.info.reason, reason);
    assert.equal(r.vi.info.canInstall, canInstall);
    assert.equal(r.media.requests.length, 0, 'the microphone is not even opened');
  }
  const noMedia = rig();
  const vi = STT.createVoiceInput({ engine: new FakeEngine(), mediaDevices: undefined });
  assert.equal(await vi.start(), false);
  assert.equal(vi.state, STATE.UNSUPPORTED);
  assert.equal(noMedia.vi.state, STATE.IDLE);
});

test('9b. install: explicit action, then the microphone can be used', async () => {
  const engine = new FakeEngine({ status: 'needs_install', reason: 'needs_install', local: true, canInstall: true });
  const r = rig({ engine });
  await r.vi.start();
  assert.equal(engine.installs.length, 0, 'nothing is downloaded until the user asks');
  assert.equal(await r.vi.install(), true);
  assert.deepEqual(engine.installs, ['en-US']);
  assert.equal(r.vi.state, STATE.IDLE);
  engine.installResult = false;
  assert.equal(await r.vi.install(), false);
  assert.equal(r.vi.info.reason, 'install_failed');
});

test('10. duplicate starts are prevented: while acquiring, while listening and while processing', async () => {
  let release;
  const media = new FakeMedia();
  media.delay = new Promise((res) => { release = res; });
  const r = rig({ media });
  const first = r.vi.start();
  const second = r.vi.start();
  assert.equal(await second, false, 'a second start while the first is still acquiring the microphone');
  release();
  assert.equal(await first, true);
  assert.equal(await r.vi.start(), false, 'a start while listening');
  assert.equal(r.media.requests.length, 1);
  assert.equal(r.engine.starts.length, 1);
  r.engine.autoEndOnStop = false;
  r.vi.stop();
  assert.equal(r.vi.state, STATE.PROCESSING);
  assert.equal(await r.vi.start(), false, 'a start while processing');
  assert.equal(r.engine.starts.length, 1);
});

test('12. finalisation: interim words are promoted when the engine ends on its own; the final result is delivered exactly once', async () => {
  const r = rig();
  await started(r);
  r.engine.result('open google', 'and search for one chance');
  r.engine.end(); // engine decided speech ended (no explicit stop, no is-final flag)
  r.engine.end(); // a repeated end event must not duplicate anything
  r.vi.stop();
  assert.equal(r.log.finals.length, 1);
  assert.equal(r.log.finals[0].text, 'open google and search for one chance');
  assert.equal(r.vi.state, STATE.IDLE);
});

test('12b. a stop that the engine never answers cannot leave the microphone stuck in PROCESSING', async () => {
  const r = rig();
  r.engine.autoEndOnStop = false;
  await started(r);
  r.engine.result('hello', '');
  r.vi.stop();
  assert.equal(r.vi.state, STATE.PROCESSING);
  await r.clock.tick(STT.FINALIZE_WAIT_MS + 1);
  assert.equal(r.vi.state, STATE.IDLE);
  assert.equal(r.log.finals[0].text, 'hello');
  assert.equal(r.media.streams[0].track.stopped, true);
});

test('13. after an error the state resets: retry starts a clean session and nothing from the failed one leaks in', async () => {
  const r = rig();
  await started(r);
  r.engine.result('stale words', '');
  r.engine.error('audio-capture');
  assert.equal(r.vi.state, STATE.ERROR);
  assert.equal(await r.vi.start(), true);
  assert.equal(r.vi.state, STATE.LISTENING);
  assert.equal(r.engine.starts.length, 2);
  r.engine.result('', 'fresh');
  assert.equal(r.log.transcripts.at(-1).text, 'fresh', 'no text from the failed session');
  assert.equal(r.media.streams[0].track.stopped, true, 'the first session released its microphone');
  assert.equal(r.media.streams[1].track.stopped, false);
});

test('cancel: aborts, releases the microphone, discards the text and delivers no result', async () => {
  const r = rig();
  await started(r);
  r.engine.result('half a sentence', '');
  r.vi.cancel();
  assert.equal(r.vi.state, STATE.IDLE);
  assert.equal(r.engine.aborts, 1);
  assert.equal(r.media.streams[0].track.stopped, true);
  assert.equal(r.log.finals.length, 0);
});

test('cancel while the microphone is still being acquired gives it straight back', async () => {
  let release;
  const media = new FakeMedia();
  media.delay = new Promise((res) => { release = res; });
  const r = rig({ media });
  const p = r.vi.start();
  await flush(); // past the engine check; now waiting for the microphone
  assert.equal(media.requests.length, 1);
  r.vi.cancel();
  release();
  assert.equal(await p, false);
  assert.equal(r.engine.starts.length, 0, 'the engine is never started');
  assert.equal(media.streams[0].track.stopped, true, 'the microphone that arrived late is released immediately');
  assert.equal(r.vi.state, STATE.IDLE);
});

test('cancel while the engine is still being checked never even opens the microphone', async () => {
  const r = rig();
  const p = r.vi.start();
  r.vi.cancel();
  assert.equal(await p, false);
  assert.equal(r.media.requests.length, 0);
  assert.equal(r.engine.starts.length, 0);
  assert.equal(r.vi.state, STATE.IDLE);
});

test('the controller adds no silence timer of its own (normal pauses must not end a command)', async () => {
  const r = rig();
  await started(r);
  r.engine.result('open google', '');
  await r.clock.tick(60000);
  assert.equal(r.vi.state, STATE.LISTENING, 'a minute of silence does not stop it; only the engine or the user does');
  await r.clock.tick(STT.MAX_LISTEN_MS);
  assert.notEqual(r.vi.state, STATE.LISTENING, 'only the long safety limit ends a forgotten session');
});

// ------------------------------------------------------------------ the Web Speech engine adapter
function fakeSpeechGlobal(opts) {
  opts = opts || {};
  const created = [];
  class SR {
    static async available(o) { SR.lastAvailable = o; return opts.availability || 'available'; }
    static async install(o) { SR.lastInstall = o; return opts.installOk !== false; }
    constructor() { created.push(this); this._pl = false; }
    get processLocally() { return this._pl; }
    set processLocally(v) { this._pl = v; }
    start(track) {
      const boom = (spec) => Object.assign(new Error(spec.message), { name: spec.name });
      if (opts.rejectTrack && track) throw new TypeError('start() takes no arguments');
      if (opts.throwWithTrack && track) throw boom(opts.throwWithTrack);
      if (opts.throwAlways) throw boom(opts.throwAlways);
      this.started = { track }; this.startCalls = (this.startCalls || 0) + 1;
    }
    stop() { this.stopped = true; }
    abort() { this.aborted = true; }
  }
  if (opts.noLocal) { delete SR.available; delete SR.install; Object.defineProperty(SR.prototype, 'processLocally', { value: undefined, configurable: true }); delete SR.prototype.processLocally; }
  return { global: opts.webkitOnly ? { webkitSpeechRecognition: SR } : { SpeechRecognition: SR }, SR, created };
}

test('engine: only on-device recognition is used by default; cloud is never a silent fallback', async () => {
  let f = fakeSpeechGlobal({ availability: 'available' });
  let e = STT.webSpeechEngine({ global: f.global });
  assert.deepEqual(await e.availability('en-US'), { status: 'available', local: true });
  assert.deepEqual(f.SR.lastAvailable, { langs: ['en-US'], processLocally: true });
  f = fakeSpeechGlobal({ availability: 'downloadable' });
  assert.deepEqual(await STT.webSpeechEngine({ global: f.global }).availability('en-US'), { status: 'needs_install', reason: 'needs_install', local: true, canInstall: true });
  f = fakeSpeechGlobal({ availability: 'downloading' });
  assert.equal((await STT.webSpeechEngine({ global: f.global }).availability('en-US')).status, 'installing');
  f = fakeSpeechGlobal({ availability: 'unavailable' });
  assert.deepEqual(await STT.webSpeechEngine({ global: f.global }).availability('xx'), { status: 'unavailable', reason: 'language_not_local', local: true });
  assert.deepEqual(await STT.webSpeechEngine({ global: {} }).availability('en-US'), { status: 'unavailable', reason: 'no_api', local: false });
  assert.deepEqual(await STT.webSpeechEngine({ global: f.global, allowCloud: true }).availability('xx'), { status: 'available', local: false }, 'cloud only when explicitly allowed');
});

test('engine: prefixed API is found, and an engine without on-device support is refused', async () => {
  const f = fakeSpeechGlobal({ webkitOnly: true });
  assert.equal((await STT.webSpeechEngine({ global: f.global }).availability('en-US')).status, 'available');
  const old = fakeSpeechGlobal({ noLocal: true });
  assert.deepEqual(await STT.webSpeechEngine({ global: old.global }).availability('en-US'), { status: 'unavailable', reason: 'no_local_engine', local: false });
});

test('engine: install asks for the on-device pack only', async () => {
  const f = fakeSpeechGlobal();
  assert.equal(await STT.webSpeechEngine({ global: f.global }).install('en-US'), true);
  assert.deepEqual(f.SR.lastInstall, { langs: ['en-US'], processLocally: true });
});

test('engine: starts continuous + interim, on-device, with our microphone track; results are snapshots', () => {
  const f = fakeSpeechGlobal();
  const e = STT.webSpeechEngine({ global: f.global });
  const seen = [];
  const track = { id: 'x' };
  const h = e.start({ lang: 'en-US', track, local: true, onResult: (r) => seen.push(r), onError() {}, onEnd() {}, onStart() {} });
  const rec = f.created[0];
  assert.equal(rec.lang, 'en-US');
  assert.equal(rec.continuous, true);
  assert.equal(rec.interimResults, true);
  assert.equal(rec.processLocally, true);
  assert.equal(rec.started.track, track);
  const res = (t, fin) => Object.assign([{ transcript: t }], { isFinal: fin });
  rec.onresult({ results: [res('open google', true), res('and search', false)] });
  assert.deepEqual(seen[0], { final: 'open google', interim: 'and search' });
  h.stop(); h.abort();
  assert.equal(rec.stopped && rec.aborted, true);
});

test('engine: a recogniser that does not take a track falls back to its own capture', () => {
  const f = fakeSpeechGlobal({ rejectTrack: true });
  STT.webSpeechEngine({ global: f.global }).start({ lang: 'en-US', track: {}, local: true, onResult() {}, onError() {}, onEnd() {} });
  const live = f.created[f.created.length - 1]; // the retry runs on a fresh recogniser
  assert.equal(live.startCalls, 1);
  assert.equal(live.started.track, undefined);
});

test('engine: if start(track) is refused for any non-permission reason, a fresh recogniser starts on the engine\'s own capture', () => {
  const f = fakeSpeechGlobal({ throwWithTrack: { name: 'InvalidStateError', message: 'recognition cannot take this track here' } });
  const h = STT.webSpeechEngine({ global: f.global }).start({ lang: 'en-US', track: { id: 'x' }, local: true, onResult() {}, onError() {}, onEnd() {} });
  assert.equal(f.created.length, 2, 'a clean recogniser for the retry');
  assert.equal(f.created[0].aborted, true, 'the failed one is discarded');
  assert.equal(f.created[1].started.track, undefined);
  assert.equal(f.created[1].processLocally, true, 'the retry is still on-device');
  assert.equal(h.usedTrack, false);
});

test('engine: when both attempts fail the error names both causes; a permission refusal is never retried', () => {
  const f = fakeSpeechGlobal({ throwAlways: { name: 'InvalidStateError', message: 'nope' } });
  assert.throws(() => STT.webSpeechEngine({ global: f.global }).start({ lang: 'en-US', track: {}, local: true, onResult() {}, onError() {}, onEnd() {} }),
    (e) => /start\(track\): InvalidStateError: nope; start\(\): InvalidStateError: nope/.test(e.detail));
  const g = fakeSpeechGlobal({ throwWithTrack: { name: 'NotAllowedError', message: 'blocked' } });
  assert.throws(() => STT.webSpeechEngine({ global: g.global }).start({ lang: 'en-US', track: {}, local: true, onResult() {}, onError() {}, onEnd() {} }), (e) => e.name === 'NotAllowedError');
  assert.equal(g.created.length, 1, 'no fallback after a permission error');
});

test('controller: an engine that cannot start reports the REAL cause in the message, releases the microphone and allows a retry', async () => {
  const r = rig();
  r.engine.throwOnStart = Object.assign(new Error('this document cannot start recognition'), { name: 'InvalidStateError' });
  assert.equal(await r.vi.start(), false);
  assert.equal(r.vi.state, STATE.ERROR);
  assert.equal(r.vi.info.code, 'start_failed');
  assert.match(r.vi.info.message, /could not start \(InvalidStateError: this document cannot start recognition\)/);
  assert.equal(r.media.streams[0].track.stopped, true);
  r.engine.throwOnStart = Object.assign(new Error('x'), { detail: 'start(track): A: a; start(): B: b' });
  assert.equal(await r.vi.start(), false);
  assert.match(r.vi.info.message, /start\(track\): A: a; start\(\): B: b/);
  r.engine.throwOnStart = null;
  assert.equal(await r.vi.start(), true);
});

test('controller: when the engine captures audio itself our own microphone is released at once (no two captures)', async () => {
  const r = rig();
  r.engine.start = function (o) { this.cur = o; this.starts.push({ lang: o.lang, track: o.track }); return { usedTrack: false, stop() {}, abort() {} }; };
  assert.equal(await r.vi.start(), true);
  assert.equal(r.vi.state, STATE.LISTENING);
  assert.equal(r.vi.info.usedTrack, false);
  assert.equal(r.media.streams[0].track.stopped, true);
  r.media.streams[0].track.fire('ended'); // nothing listens any more: not an error
  assert.equal(r.vi.state, STATE.LISTENING);
});

test('privacy: stt.js never records, stores, logs or transmits audio or text', () => {
  const src = require('node:fs').readFileSync(path.join(__dirname, '..', '..', '..', 'extension', 'sidepanel', 'stt.js'), 'utf8');
  for (const banned of ['MediaRecorder', 'AudioContext', 'AudioWorklet', 'ScriptProcessor', 'fetch(', 'XMLHttpRequest', 'WebSocket', 'sendBeacon', 'localStorage', 'sessionStorage',
    'indexedDB', 'chrome.', 'console.', 'start_task', 'postMessage', 'Blob(']) {
    assert.ok(!src.includes(banned), `stt.js must not use ${banned}`);
  }
});
