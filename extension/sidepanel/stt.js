// OCTG voice input (Phase 2.1): speech -> TEXT. Nothing else.
//
// This file is the speech-to-text controller. It knows nothing about tasks, the agent, the model router or the browser actions: its only output is a
// transcript string handed to a callback. The side panel puts that text into the Task box, and the EXISTING Run path takes it from there.
//
// Engines are adapters (see webSpeechEngine below), so Chrome's Web Speech API can later be replaced by a local Whisper-style service without touching
// the controller or the UI. Audio is never recorded, stored, logged or sent anywhere by this file: it only asks the browser for a live microphone
// track (with echo cancellation / noise suppression / auto gain control requested) and hands that track to the engine.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OCTG_STT = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const STATE = Object.freeze({
    IDLE: 'idle', STARTING: 'starting', LISTENING: 'listening', PROCESSING: 'processing',
    ERROR: 'error', PERMISSION_DENIED: 'permission_denied', UNSUPPORTED: 'unsupported',
  });
  // What we ask the browser's capture pipeline to do. The browser applies what the device/OS allows; `applied` reports what it actually granted.
  const AUDIO_CONSTRAINTS = Object.freeze({ echoCancellation: true, noiseSuppression: true, autoGainControl: true });
  const FINALIZE_WAIT_MS = 5000;   // after stop(), how long to wait for the engine's last words
  const MAX_LISTEN_MS = 120000;    // safety stop; the engine/browser decides when speech ended, we add no silence timer of our own

  const join = (a, b) => [a, b].map((s) => (s || '').trim()).filter(Boolean).join(' ');

  // ------------------------------------------------------------------------------------------------ engine: Chrome Web Speech API
  // Chrome's DEFAULT recognizer streams audio to Google's servers. To keep audio on this PC the engine insists on on-device recognition
  // (`processLocally`) and reports 'unavailable' / 'needs_install' instead of silently falling back to the cloud. Cloud is opt-in (allowCloud).
  function webSpeechEngine(opts) {
    opts = opts || {};
    const g = opts.global || (typeof globalThis !== 'undefined' ? globalThis : {});
    const SR = g.SpeechRecognition || g.webkitSpeechRecognition;
    const allowCloud = opts.allowCloud === true;
    const localCapable = !!(SR && typeof SR.available === 'function' && SR.prototype && 'processLocally' in SR.prototype);

    return {
      id: 'web-speech',
      /** -> {status: 'available'|'needs_install'|'installing'|'unavailable', local: bool, reason?, canInstall?} */
      async availability(lang) {
        if (!SR) return { status: 'unavailable', reason: 'no_api', local: false };
        if (localCapable) {
          let st;
          try { st = await SR.available({ langs: [lang], processLocally: true }); } catch (_) { st = 'unavailable'; }
          if (st === 'available') return { status: 'available', local: true };
          if (st === 'downloadable') return { status: 'needs_install', reason: 'needs_install', local: true, canInstall: typeof SR.install === 'function' };
          if (st === 'downloading') return { status: 'installing', reason: 'installing', local: true };
          if (allowCloud) return { status: 'available', local: false };
          return { status: 'unavailable', reason: 'language_not_local', local: true };
        }
        return allowCloud ? { status: 'available', local: false } : { status: 'unavailable', reason: 'no_local_engine', local: false };
      },
      async install(lang) {
        if (!SR || typeof SR.install !== 'function') return false;
        return !!(await SR.install({ langs: [lang], processLocally: true }));
      },
      /** handlers: onResult({final, interim}), onError(code), onEnd(), onStart(). Returns {stop(), abort()}. */
      start({ lang, track, local, onResult, onError, onEnd, onStart }) {
        const make = () => {
          const rec = new SR();
          rec.lang = lang;
          rec.continuous = true;       // keep listening through normal pauses
          rec.interimResults = true;
          rec.maxAlternatives = 1;
          if (local && 'processLocally' in rec) rec.processLocally = true;
          rec.onstart = () => onStart && onStart();
          rec.onresult = (e) => {
            let fin = '', interim = '';
            for (let i = 0; i < e.results.length; i++) {
              const r = e.results[i];
              const alt = r[0] ? r[0].transcript : '';
              if (r.isFinal) fin = join(fin, alt); else interim = join(interim, alt);
            }
            onResult({ final: fin, interim });
          };
          rec.onerror = (e) => onError(e && e.error ? e.error : 'unknown');
          rec.onend = () => onEnd();
          return rec;
        };
        const describe = (e) => ((e && e.name) || 'Error') + ': ' + String((e && e.message) || e || '').slice(0, 120);
        let rec = null, usedTrack = false;
        // First choice: recognise from OUR constrained microphone track. If this engine/context refuses it for any reason other than permission, retry once on a
        // fresh recogniser with the engine's own capture (so voice still works), and if that fails too, report BOTH causes.
        const attempt = (withTrack) => {
          const r = make();
          try { if (withTrack) r.start(track); else r.start(); rec = r; usedTrack = withTrack; return null; } catch (e) { try { r.abort(); } catch (_) { /* never started */ } return e; }
        };
        let err = attempt(!!track);
        if (err && track && err.name !== 'NotAllowedError') {
          const first = err;
          err = attempt(false);
          if (err) err.detail = 'start(track): ' + describe(first) + '; start(): ' + describe(err);
        }
        if (err) throw err;
        return { usedTrack, stop: () => { try { rec.stop(); } catch (_) { /* already stopped */ } }, abort: () => { try { rec.abort(); } catch (_) { /* already stopped */ } } };
      },
    };
  }

  // ------------------------------------------------------------------------------------------------ controller
  /**
   * opts: engine, mediaDevices (navigator.mediaDevices), getLang(), onState(state, info), onTranscript({text, final, interim}), onFinal({text, empty}),
   *       setTimeout/clearTimeout (injectable for tests).
   */
  function createVoiceInput(opts) {
    const engine = opts.engine;
    const mediaDevices = opts.mediaDevices;
    const getLang = opts.getLang || (() => 'en-US');
    const st = opts.setTimeout || ((f, ms) => setTimeout(f, ms));
    const ct = opts.clearTimeout || ((h) => clearTimeout(h));
    const emit = (fn, ...a) => { try { if (fn) fn(...a); } catch (_) { /* a UI callback must never break speech handling */ } };

    let state = STATE.IDLE;
    let info = {};
    let session = null; // {stream, track, handle, finalText, interimText, stopped, done, timers: []}
    let busy = false;   // true from start() until the session exists: blocks duplicate starts while the mic/engine are being acquired
    let epoch = 0;      // bumped by cancel(): a start() that was still acquiring the microphone notices and gives it back

    function setState(next, extra) {
      state = next;
      info = extra || {};
      emit(opts.onState, state, info);
    }

    function release(s) {
      if (!s) return;
      s.done = true;
      for (const t of s.timers) ct(t);
      s.timers.length = 0;
      try { if (s.track && s.onTrackEnded) s.track.removeEventListener('ended', s.onTrackEnded); } catch (_) { /* ignore */ }
      try { if (s.stream) for (const t of s.stream.getTracks()) t.stop(); } catch (_) { /* ignore */ }
      s.stream = s.track = s.handle = null; // no audio object is kept after the session
      if (session === s) session = null;
    }

    const textOf = (s) => join(s.finalText, s.interimText);

    function finish(s, { error, code, message, permission } = {}) {
      if (s.done) return;
      const text = textOf(s);
      release(s);
      if (text) { emit(opts.onTranscript, { text, final: true, interim: false }); } // never lose recognised words, even when something went wrong afterwards
      if (error) {
        setState(permission ? STATE.PERMISSION_DENIED : STATE.ERROR, { code, message, kept: !!text });
        if (text) emit(opts.onFinal, { text, empty: false, error: true });
        return;
      }
      setState(STATE.IDLE, {});
      emit(opts.onFinal, { text, empty: !text });
    }

    function mapMicError(e) {
      const name = (e && e.name) || '';
      if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') return { permission: true, code: 'permission_denied', message: 'Microphone permission is required.' };
      if (name === 'NotFoundError' || name === 'DevicesNotFoundError') return { code: 'no_microphone', message: 'No microphone was found.' };
      if (name === 'NotReadableError' || name === 'AbortError') return { code: 'mic_busy', message: 'The microphone is in use by another application or could not be opened.' };
      if (name === 'OverconstrainedError') return { code: 'no_microphone', message: 'No microphone matches the requested settings.' };
      return { code: 'mic_error', message: 'The microphone could not be started.' };
    }

    function mapEngineError(code) {
      switch (code) {
        case 'not-allowed': case 'service-not-allowed': return { permission: true, code: 'permission_denied', message: 'Microphone permission is required.' };
        case 'audio-capture': return { code: 'mic_disconnected', message: 'The microphone was disconnected or is unavailable.' };
        case 'network': return { code: 'network', message: 'Speech recognition lost its connection.' };
        case 'language-not-supported': return { code: 'language_not_supported', message: 'This language is not supported by the speech engine.' };
        case 'bad-grammar': case 'phrases-not-supported': return { code: code, message: 'The speech engine rejected its settings.' };
        default: return { code: code || 'unknown', message: 'Speech recognition failed (' + (code || 'unknown') + ').' };
      }
    }

    async function start() {
      if (busy || state === STATE.STARTING || state === STATE.LISTENING || state === STATE.PROCESSING) return false; // duplicate-start prevention
      busy = true;
      const mine = epoch;
      setState(STATE.STARTING, {});
      try {
        const lang = getLang();
        let av;
        try { av = await engine.availability(lang); } catch (e) { av = { status: 'unavailable', reason: 'no_api' }; }
        if (mine !== epoch) return false; // cancelled while checking the engine
        if (!av || av.status !== 'available') {
          const reason = (av && av.reason) || 'unavailable';
          setState(STATE.UNSUPPORTED, { reason, canInstall: !!(av && av.canInstall), local: av ? av.local : false, installing: reason === 'installing' });
          return false;
        }
        if (!mediaDevices || typeof mediaDevices.getUserMedia !== 'function') { setState(STATE.UNSUPPORTED, { reason: 'no_microphone_api' }); return false; }
        let stream;
        try { stream = await mediaDevices.getUserMedia({ audio: { ...AUDIO_CONSTRAINTS } }); } catch (e) {
          const m = mapMicError(e);
          setState(m.permission ? STATE.PERMISSION_DENIED : STATE.ERROR, { code: m.code, message: m.message });
          return false;
        }
        if (mine !== epoch) { try { for (const t of stream.getTracks()) t.stop(); } catch (_) { /* ignore */ } return false; } // cancelled while waiting for the microphone
        const track = stream.getAudioTracks && stream.getAudioTracks()[0];
        if (!track) { try { for (const t of stream.getTracks()) t.stop(); } catch (_) { /* ignore */ } setState(STATE.ERROR, { code: 'no_microphone', message: 'No microphone was found.' }); return false; }

        const s = { stream, track, handle: null, finalText: '', interimText: '', stopped: false, done: false, timers: [], onTrackEnded: null };
        session = s;
        s.onTrackEnded = () => { if (!s.done) { try { if (s.handle) s.handle.abort(); } catch (_) { /* ignore */ } finish(s, { error: true, code: 'mic_disconnected', message: 'The microphone was disconnected.' }); } };
        track.addEventListener('ended', s.onTrackEnded);
        let applied = {};
        try { applied = track.getSettings ? track.getSettings() : {}; } catch (_) { applied = {}; }
        try {
          s.handle = engine.start({
            lang, track, local: av.local,
            onStart: () => {},
            onResult: ({ final, interim }) => {
              if (s.done) return;
              s.finalText = final || '';
              s.interimText = interim || '';
              emit(opts.onTranscript, { text: textOf(s), final: false, interim: !!s.interimText });
            },
            onError: (code) => {
              if (s.done) return;
              if (code === 'aborted' && s.stopped) return;      // we asked for it
              if (code === 'no-speech') return finish(s);       // silence: finishes as an empty/short result, not an error
              const m = mapEngineError(code);
              finish(s, { error: true, code: m.code, message: m.message, permission: m.permission });
            },
            onEnd: () => { if (!s.done) finish(s); },           // the engine decided speech ended (or answered our stop)
          });
        } catch (e) {
          if (e && e.name === 'NotAllowedError') {
            const m = mapEngineError('not-allowed');
            finish(s, { error: true, code: m.code, message: m.message, permission: true });
          } else {
            // show the real cause (an exception name/message, never audio or page content) so a failure can be diagnosed from the panel itself
            const detail = (e && e.detail) || (((e && e.name) || 'Error') + ': ' + String((e && e.message) || e || '').slice(0, 160));
            finish(s, { error: true, code: 'start_failed', message: 'Speech recognition could not start (' + detail + ').' });
          }
          return false;
        }
        if (s.handle && s.handle.usedTrack === false) {
          // the engine is capturing audio itself: give our own microphone back so two captures never compete for the device
          try { s.track.removeEventListener('ended', s.onTrackEnded); } catch (_) { /* ignore */ }
          try { for (const t of s.stream.getTracks()) t.stop(); } catch (_) { /* ignore */ }
        }
        s.timers.push(st(() => stop(), MAX_LISTEN_MS));
        setState(STATE.LISTENING, { local: av.local, applied, usedTrack: !(s.handle && s.handle.usedTrack === false) });
        return true;
      } finally {
        busy = false;
      }
    }

    function stop() {
      const s = session;
      if (!s || s.done || s.stopped || state !== STATE.LISTENING) return false;
      s.stopped = true;
      setState(STATE.PROCESSING, {});
      try { if (s.handle) s.handle.stop(); } catch (_) { /* engine already ended */ }
      s.timers.push(st(() => finish(s), FINALIZE_WAIT_MS)); // never get stuck waiting for the engine
      return true;
    }

    /** Abort without producing a result (task started, panel closing). Whatever was recognised is discarded. */
    function cancel() {
      epoch++;
      const s = session;
      if (s && !s.done) {
        s.stopped = true;
        try { if (s.handle) s.handle.abort(); } catch (_) { /* ignore */ }
        release(s);
      }
      if (state !== STATE.IDLE) setState(STATE.IDLE, { cancelled: true });
    }

    function toggle() {
      if (state === STATE.LISTENING) return Promise.resolve(stop());
      return start();
    }

    async function install() {
      if (!engine.install) return false;
      const lang = getLang();
      setState(STATE.UNSUPPORTED, { reason: 'installing', canInstall: false, installing: true });
      let ok = false;
      try { ok = await engine.install(lang); } catch (_) { ok = false; }
      if (ok) setState(STATE.IDLE, { installed: true });
      else setState(STATE.UNSUPPORTED, { reason: 'install_failed', canInstall: true });
      return ok;
    }

    return { start, stop, cancel, toggle, install, get state() { return state; }, get info() { return info; } };
  }

  return { STATE, AUDIO_CONSTRAINTS, FINALIZE_WAIT_MS, MAX_LISTEN_MS, webSpeechEngine, createVoiceInput };
});
