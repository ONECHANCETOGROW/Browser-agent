// OCTG voice input (Phase 2.1): the microphone button next to the Task box.
//
// This file is ONLY a view over stt.js. It writes the transcript into #taskInput and, when "run automatically" is on, presses the EXISTING Run button
// (`run.click()`), which is the same handler a typed task goes through. It never talks to the agent, the service worker, the bridge or the model
// router: voice is another way of entering the same task text, and Phase 2's approval/safety rules apply to it unchanged.
//
// Loaded before panel.js, so its Run-button guard runs first. The DOM is injected (els, storage, timers) so the logic is unit-tested without a browser.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; return; }
  root.OCTG_VOICE_UI = api;
  // In the side panel: mount onto the page's own elements. The rest of the panel only ever calls window.octgVoice.sync().
  const d = root.document;
  if (!d || !root.OCTG_STT || !d.getElementById('mic')) return;
  const $ = (id) => d.getElementById(id);
  let storage = null;
  try { storage = root.localStorage; } catch (_) { storage = null; }
  root.octgVoice = api.mountVoiceInput({
    STT: root.OCTG_STT, doc: d, engine: root.OCTG_STT.webSpeechEngine({ global: root }), mediaDevices: root.navigator && root.navigator.mediaDevices,
    language: root.navigator && root.navigator.language, storage,
    els: { textarea: $('taskInput'), mic: $('mic'), status: $('voiceStatus'), install: $('voiceInstall'), permission: $('voicePermission'), autorun: $('voiceAutorun'), run: $('run') },
    openPermissionPage: () => { try { root.chrome.tabs.create({ url: root.chrome.runtime.getURL('sidepanel/mic_permission.html') }); } catch (_) { /* not in an extension */ } },
  });
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const AUTORUN_DELAY_MS = 2000; // a short review window: a misheard command can be edited or cancelled before it runs
  const MESSAGES = {
    starting: 'Starting microphone…',
    listening: 'Listening… click the microphone to stop.',
    processing: 'Transcribing…',
    empty: "I didn't catch anything. Click the microphone and try again.",
    busy: 'Finish speaking first: click the microphone to stop.',
    permission: 'Microphone permission is required. Allow it, then click the microphone again.',
    reasons: {
      no_api: "Voice input isn't supported by this browser.",
      no_microphone_api: "Voice input isn't supported here: this page can't access a microphone.",
      no_local_engine: "On-device speech recognition isn't available in this browser, and cloud recognition is off (it would send your audio to Google).",
      language_not_local: "On-device speech recognition isn't available for this language, and cloud recognition is off (it would send your audio to Google).",
      needs_install: "Voice input needs Chrome's on-device speech model (a one-time download). Nothing is sent to a server while you speak.",
      installing: 'Downloading the on-device speech model… click the microphone again when it has finished.',
      install_failed: 'The on-device speech model could not be installed.',
      unavailable: 'Voice input is not available.',
    },
  };
  const LABEL = { idle: 'Start voice input', starting: 'Starting microphone…', listening: 'Stop listening', processing: 'Transcribing…', error: 'Start voice input', permission_denied: 'Start voice input', unsupported: 'Start voice input' };

  /**
   * deps: STT (the stt.js module), engine, mediaDevices, els {textarea, mic, status, install, permission, autorun, run}, storage ({getItem}),
   *       language (navigator.language), openPermissionPage(), setTimeout/clearTimeout.
   */
  function mountVoiceInput(deps) {
    const { STT, els } = deps;
    const st = deps.setTimeout || ((f, ms) => setTimeout(f, ms));
    const ct = deps.clearTimeout || ((h) => clearTimeout(h));
    const store = (k) => { try { return deps.storage ? deps.storage.getItem(k) : null; } catch (_) { return null; } };

    let engine = deps.engine;
    let controller = null;
    let base = '';            // what was in the Task box before dictation started; dictation is appended to it
    let enabled = true;
    let autoTimer = null;
    let autoText = null;
    let statusTimer = null;
    let lastState = STT.STATE.IDLE;

    const busyState = () => [STT.STATE.STARTING, STT.STATE.LISTENING, STT.STATE.PROCESSING].includes(controller.state);

    function say(text, { sticky = true } = {}) {
      if (statusTimer) { ct(statusTimer); statusTimer = null; }
      els.status.textContent = text || '';
      els.status.hidden = !text;
      if (text && !sticky) statusTimer = st(() => { els.status.textContent = ''; els.status.hidden = true; }, 6000);
    }
    function cancelAutoRun() { if (autoTimer) { ct(autoTimer); autoTimer = null; } autoText = null; }
    function put(text) { els.textarea.value = [base, text].map((s) => (s || '').trim()).filter(Boolean).join(' '); }

    function renderMic(state, info) {
      lastState = state;
      const m = els.mic;
      m.dataset.state = state;
      m.setAttribute('aria-pressed', state === STT.STATE.LISTENING ? 'true' : 'false');
      m.setAttribute('aria-label', LABEL[state] || LABEL.idle);
      m.title = LABEL[state] || LABEL.idle;
      m.disabled = !enabled || state === STT.STATE.STARTING || state === STT.STATE.PROCESSING;
      els.install.hidden = !(state === STT.STATE.UNSUPPORTED && info && info.canInstall);
      els.permission.hidden = state !== STT.STATE.PERMISSION_DENIED;
    }

    function onState(state, info) {
      renderMic(state, info);
      if (state === STT.STATE.STARTING) { base = els.textarea.value; say(MESSAGES.starting); }
      else if (state === STT.STATE.LISTENING) say(MESSAGES.listening);
      else if (state === STT.STATE.PROCESSING) say(MESSAGES.processing);
      else if (state === STT.STATE.PERMISSION_DENIED) say(MESSAGES.permission);
      else if (state === STT.STATE.ERROR) say((info && info.message) || 'Voice input failed. Click the microphone to try again.');
      else if (state === STT.STATE.UNSUPPORTED) say(MESSAGES.reasons[info && info.reason] || MESSAGES.reasons.unavailable);
      else if (state === STT.STATE.IDLE && info && info.cancelled) { els.textarea.value = base; say(''); }
      else if (state === STT.STATE.IDLE && info && info.installed) say('On-device speech model installed. Click the microphone to start.', { sticky: false });
    }

    function onTranscript({ text }) { put(text); }

    function onFinal({ text, empty, error }) {
      if (empty) { els.textarea.value = base; say(MESSAGES.empty, { sticky: false }); return; } // never submit an empty task
      put(text);
      const transcript = els.textarea.value;
      if (error) { say((els.status.textContent || 'Voice input stopped early.') + ' The text so far is in the Task box; check it before running.'); return; }
      if (els.autorun.checked && !els.run.disabled) {
        autoText = transcript;
        say(`Running in ${Math.round(AUTORUN_DELAY_MS / 1000)} s. Edit the text, press Esc, or click the microphone to cancel.`);
        autoTimer = st(() => {
          autoTimer = null;
          const same = els.textarea.value === autoText;
          autoText = null;
          if (!same) return;                       // the user edited it: they are in charge now
          if (els.run.disabled) { say('Transcript is ready. Press Run when the agent is available.'); return; }
          say('');
          els.run.click();                         // the EXISTING Run handler: same path as a typed task
        }, AUTORUN_DELAY_MS);
      } else {
        say(els.run.disabled ? 'Transcript is ready. Press Run when the agent is available.' : 'Transcript is ready. Press Run to start.', { sticky: false });
      }
    }

    function build() {
      controller = STT.createVoiceInput({
        engine, mediaDevices: deps.mediaDevices, setTimeout: st, clearTimeout: ct, onState, onTranscript, onFinal,
        getLang: () => store('octg.voice.lang') || deps.language || 'en-US',
      });
      renderMic(STT.STATE.IDLE, {});
    }
    build();

    // ---- events
    els.mic.addEventListener('click', () => {
      cancelAutoRun();
      if (!enabled) return;
      controller.toggle();
    });
    els.install.addEventListener('click', () => { controller.install(); });
    els.permission.addEventListener('click', () => { if (deps.openPermissionPage) deps.openPermissionPage(); });
    // Run guard: registered before panel.js, so it runs first. Not mid-sentence: the transcript is not final yet.
    els.run.addEventListener('click', (e) => {
      if (busyState()) { e.preventDefault(); e.stopImmediatePropagation(); say(MESSAGES.busy, { sticky: false }); return; }
      cancelAutoRun(); // a manual Run supersedes the pending automatic one
    }, true);
    els.textarea.addEventListener('input', cancelAutoRun);
    if (deps.doc) deps.doc.addEventListener('keydown', (e) => { if (e.key === 'Escape' && autoTimer) { cancelAutoRun(); say('Cancelled. The text is in the Task box.', { sticky: false }); } });

    return {
      /** Called by panel.js on every render: voice follows the existing one-task-at-a-time state. */
      sync({ active }) {
        const was = enabled;
        enabled = !active;
        if (active) { cancelAutoRun(); if (busyState()) controller.cancel(); }
        if (was !== enabled) renderMic(controller.state, controller.info);
      },
      /** Swap the speech engine (a future local Whisper service, or a scripted engine in tests). */
      setEngine(e) { controller.cancel(); cancelAutoRun(); engine = e; build(); },
      get controller() { return controller; },
      get state() { return controller.state; },
      get autoRunPending() { return !!autoTimer; },
    };
  }

  return { mountVoiceInput, AUTORUN_DELAY_MS, MESSAGES };
});
