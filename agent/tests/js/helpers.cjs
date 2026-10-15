// Fakes for the voice-input unit tests: a scripted speech engine, a fake microphone, a fake clock and tiny fake DOM elements.
'use strict';

const flush = () => new Promise((r) => setImmediate(r));

class FakeClock {
  constructor() { this.now = 0; this.timers = []; this.seq = 0; }
  setTimeout = (fn, ms) => { const id = ++this.seq; this.timers.push({ id, at: this.now + ms, fn }); return id; };
  clearTimeout = (id) => { this.timers = this.timers.filter((t) => t.id !== id); };
  async tick(ms) {
    const end = this.now + ms;
    for (;;) {
      const due = this.timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      this.timers = this.timers.filter((t) => t !== due);
      this.now = due.at;
      due.fn();
      await flush();
    }
    this.now = end;
  }
}

class FakeTrack {
  constructor(id) { this.id = id; this.stopped = false; this.listeners = {}; }
  stop() { this.stopped = true; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  getSettings() { return { echoCancellation: true, noiseSuppression: true, autoGainControl: true }; }
  fire(t) { for (const f of this.listeners[t] || []) f({ type: t }); }
}

class FakeMedia {
  constructor() { this.requests = []; this.streams = []; this.failWith = null; this.delay = null; }
  async getUserMedia(constraints) {
    this.requests.push(constraints);
    if (this.delay) await this.delay;
    if (this.failWith) { const e = new Error(this.failWith); e.name = this.failWith; throw e; }
    const track = new FakeTrack('t' + (this.streams.length + 1));
    const stream = { track, getAudioTracks: () => [track], getTracks: () => [track] };
    this.streams.push(stream);
    return stream;
  }
}

/** A speech engine the tests drive by hand. `autoEndOnStop` mimics real engines, which answer stop() with their last results and an end event. */
class FakeEngine {
  constructor(av) {
    this.av = av || { status: 'available', local: true };
    this.starts = []; this.stops = 0; this.aborts = 0; this.installs = []; this.installResult = true;
    this.autoEndOnStop = true; this.cur = null; this.throwOnStart = null;
  }
  async availability(lang) { this.lastLang = lang; return this.av; }
  async install(lang) { this.installs.push(lang); return this.installResult; }
  start(o) {
    if (this.throwOnStart) throw this.throwOnStart;
    this.cur = o;
    this.starts.push({ lang: o.lang, track: o.track, local: o.local });
    return { stop: () => { this.stops++; if (this.autoEndOnStop) setImmediate(() => o.onEnd()); }, abort: () => { this.aborts++; } };
  }
  result(final, interim) { this.cur.onResult({ final: final || '', interim: interim || '' }); }
  error(code) { this.cur.onError(code); }
  end() { this.cur.onEnd(); }
}

class El {
  constructor(extra) {
    Object.assign(this, { listeners: {}, dataset: {}, attrs: {}, hidden: false, disabled: false, value: '', textContent: '', checked: true, title: '' }, extra || {});
  }
  addEventListener(type, fn, capture) { (this.listeners[type] ||= []).push({ fn, capture: !!capture }); }
  setAttribute(k, v) { this.attrs[k] = v; }
  click() {
    if (this.disabled) return;
    const ev = { type: 'click', stopped: false, prevented: false, preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
    for (const { fn } of this.listeners.click || []) { fn(ev); if (ev.stopped) break; }
  }
  dispatch(type, extra) { for (const { fn } of this.listeners[type] || []) fn({ type, ...(extra || {}) }); }
}

module.exports = { flush, FakeClock, FakeTrack, FakeMedia, FakeEngine, El };
