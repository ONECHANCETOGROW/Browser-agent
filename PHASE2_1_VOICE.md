# Phase 2.1 — Voice command interface

Voice is **only an input method**. It turns speech into text in the side panel's Task box; the **existing Phase 2 pipeline** does everything after that.
It is not a new agent, not a new browser engine and not a new task pipeline. Phase 2 (engine, router, providers, approval/safety) is unchanged.

```
 microphone ──▶ stt.js (STT controller + engine adapter) ──▶ TEXT ──▶ #taskInput ──▶ the existing Run button ──▶ existing task engine
                                                                                      (start_task)                 → router → agent → actions → Chrome
```

## What it does

Click the microphone, speak, click it again (or let the engine decide speech ended). Words appear in the Task box as you speak (interim results), then the
final text stays there. By default the panel then runs the task automatically after a short review window (below); typing works exactly as before.

### UI states (the button next to the Task box)

| State | Looks like | Meaning |
|---|---|---|
| `idle` | microphone icon | Ready. |
| `starting` | amber, disabled | Checking the speech engine / opening the microphone. |
| `listening` | red, pulsing (still when reduced-motion is on), `aria-pressed=true`; "Listening…" | Recording is live. Click to stop. |
| `processing` | amber, disabled; "Transcribing…" | Stopped; waiting for the engine's last words. Duplicate starts are blocked. |
| `error` | red outline; concise message | Safe to retry. Words recognised so far stay in the Task box. |
| `permission_denied` | red outline; "Microphone permission is required…" + **Allow microphone…** | Opens a small extension page (`mic_permission.html`) that triggers Chrome's prompt in a normal tab. |
| `unsupported` | red outline; reason | No speech API, no on-device engine (cloud is off), or the on-device model is missing (**Install on-device speech model** button, an explicit click). |

Empty speech (nothing recognised) restores the Task box, shows "I didn't catch anything…" and **never submits an empty task**.

### Auto-run, and how it reaches the existing pipeline

"Run automatically after speaking" (checkbox, on by default). After a non-empty final transcript the panel shows "Running in 2 s. Edit the text, press Esc, or click
the microphone to cancel." and then calls the **existing Run button's `click()`**, the same handler a typed task uses (`start_task`). Editing the text, Esc, or the
microphone cancels it; if Run is disabled (agent not connected, or a task is running) nothing runs and the text waits for you. A dictation that ended in an error is
never auto-run. Pressing Run in the middle of a sentence is refused ("Finish speaking first") because the transcript is not final.

One task at a time: while a task is active the Task box is disabled and so is the microphone; a dictation in progress is abandoned (its unfinished text is discarded).
`voice_ui.js` never talks to the agent, the service worker or the bridge, and a test asserts the panel still has exactly one `start_task` call site.

## Speech-to-text implementation

`extension/sidepanel/stt.js` is a small controller (`start / stop / cancel / toggle / install`, callbacks for state, interim/final transcript and the final result) in front of an
**engine adapter**. The first adapter wraps Chrome's Web Speech API (`SpeechRecognition`, `continuous`, `interimResults`). Swapping in a local Whisper-style service later means
writing another adapter (`availability / install / start`) and calling `octgVoice.setEngine(...)`; the controller and UI do not change. The language comes from configuration
(`localStorage octg.voice.lang`, else `navigator.language`), never from code; the agent only ever sees text.

**On-device only.** Chrome's *default* recogniser streams audio to Google's servers. The adapter therefore insists on on-device recognition (`processLocally`) and reports
`unsupported` instead of silently falling back to the cloud. A cloud fallback exists in the adapter only behind an explicit `allowCloud` option that the UI does not expose.
On-device recognition needs Chrome's speech model for the language, a **one-time download that Chrome performs only when you click "Install on-device speech model"**
(measured on Chrome 155 / en-US: about **535 MB** added to the profile, 22 s on this PC). After that, recognition is local.

**Audio quality.** The microphone is opened with `echoCancellation`, `noiseSuppression` and `autoGainControl` requested and that same live track is handed to the recogniser
(`SpeechRecognition.start(track)`), so Chrome's capture processing applies; the controller reports what the browser actually granted (`getSettings()`). That is Chrome's
built-in processing, not professional noise cancellation, and no DSP of our own was added. There is no silence timer of our own: normal pauses do not end a command
(the engine or the user ends it; a 2-minute safety stop covers a forgotten session).

## Permissions and privacy

* **Manifest: no new permissions** (a test pins the list). `getUserMedia` works in an extension page without a manifest entry; Chrome shows its normal prompt for the extension.
  If a side panel cannot display that prompt, **Allow microphone…** opens `mic_permission.html` (a normal tab) where it can; the grant applies to the whole extension origin.
* **Audio is never recorded, stored, logged or sent by OCTG.** No `MediaRecorder`, `AudioContext`, buffers, files, `fetch`, WebSocket or storage in the voice code (tests grep for each).
  The microphone track is stopped as soon as a session ends, errors or is cancelled.
* **Audio is never sent to Gemini, FreeLLMAPI, Ollama or Cloudy.** STT and the reasoning models are separate; only the *text* of the task reaches the agent, exactly as if typed.
* The transcript is held only in the Task box. It is saved nowhere by the voice code (task history stores the task text as it does for typed tasks, with the existing redaction).
  No telemetry was added.
* Voice is **not authorisation**: a spoken "delete this" meets the same approval gate as a typed one (e2e case 12 proves it), and risk tagging, verification,
  restricted-page handling and the password/OTP/card protections are untouched.

## Tests

* `tests/js/stt.test.cjs` and `tests/js/voice_ui.test.cjs` (Node's built-in test runner, 51 tests) cover idle state, start/stop, interim and final transcripts, empty speech, recognition errors,
  permission denied, unsupported/needs-install, duplicate-start prevention, insertion into the Task box (appending to typed text), finalisation, reset after errors, cancel while starting,
  auto-run and its cancellation, one-task-at-a-time, the Run guard, the engine adapter, and static architecture/privacy/permission guards.
  `tests/test_voice_js.py` runs them inside `python -m unittest discover`.
* `tests/e2e/run_ai_e2e.py` case 12 drives the real panel in real Chrome with a scripted recogniser and Chrome's fake audio device: the track really carries echo cancellation / noise suppression /
  auto gain; interim then final text appears; the task reaches the engine through the normal Run path with exactly one task created; empty speech creates none; a spoken delete stops at the approval card.
* Opt-in live check: `python -m tests.e2e.make_test_speech "<sentence>" speech.wav` then `python -m tests.e2e.run_voice_live speech.wav "<sentence>"` runs Chrome's REAL on-device recogniser
  on a synthetic-voice WAV (via the fake microphone) with only the local model configured.

## Live test (2026-10-07, Chrome 155.0.8059.39, fresh profile, synthetic voice)

"Open Google and search for One Chance to Grow." → transcript "Open Google and search for one chance to grow" (word similarity 1.0), interim text appeared word by word (first
word about 0.7 s after speech began), final text 0.12 s after the stop click, the task started through the normal Run path with the local model, navigated to google.com, typed the query
and clicked Search. **Google then showed a CAPTCHA** (clean automation profile) and the agent correctly paused for the user, so the run did not reach the results page.

## Known limitations

* Verified with a synthetic voice and Chrome's fake microphone, not a human voice in a real room: background-noise robustness and real-world accuracy are not measured.
* Chrome's real microphone **permission prompt inside the actual side panel** was not observed (the test opens the same panel page in a tab and a fake device auto-grants).
  The permission page is the fallback if the panel cannot show it.
* **First real side-panel run (user's Chrome): "Speech recognition failed (start-failed)".** The engine threw while starting in the real side panel, a context the automated tests
  cannot open (Chrome requires a real click to open a side panel) and which the tab-based tests did not reproduce (checked: microphone format is not the cause; Chrome normalises
  the track to 48 kHz mono). Response: the engine now retries once on a fresh recogniser with Chrome's own capture if `start(track)` is refused for any non-permission reason,
  releases our own microphone in that case, and the panel shows the real exception (`start(track): <name>: <message>; start(): …`) instead of a generic code.
* On-device recognition needs a large one-time model download per language; without it voice is unavailable by design (cloud is off).
* Chrome ends a continuous recognition on its own after a long silence; the controller then finalises what it has.
* English was tested; other languages depend on Chrome's on-device packs and on `octg.voice.lang`, and have no UI selector yet.
* Spoken commands can be misrecognised; the 2-second review window and the approval gate are the safety nets. Turn auto-run off to always review first.
