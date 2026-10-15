# OCTG Browser Agent

A **generic, website-agnostic AI browser agent**, built in layers:

* **Phase 1 — the engine**: a Python agent on your PC plus a Manifest V3 Chrome extension that observe and operate whatever Chrome is showing
  (this file).
* **Phase 2 — the brain** (`PHASE2.md`): task state, an observe → reason → act → verify loop, a model router (local Ollama / Gemini / FreeLLMAPI /
  any OpenAI-compatible endpoint), an approval gate, and a side-panel task UI, all built on top of the engine without changing it.
* **Phase 2.1 — voice input** (`PHASE2_1_VOICE.md`): a microphone button next to the Task box. Speech becomes text in that box (on-device recognition, nothing recorded or
  uploaded) and the existing Run path takes it from there; it is only an input method.
* **Phase 3 — Business Brain** (`PHASE3.md`, design in `PHASE3_DESIGN.md`; **off by default**): structured, user-approved business facts and authorized documents. The model refers to
  them by token (`{{biz:identity.legal_name}}`), application code supplies the exact value, and no business value reaches a model, a log, the history or the audit trail.
  `python -m octg_agent business --help`. It also holds **clients** (`PHASE3_CLIENTS.md`): each client is a separate scope (`--client`, `{{client:KEY}}`), never mixed with your own business or another client. **The Knowledge Hub** (`PHASE3_HUB.md`; desktop shortcut *OCTG Knowledge Hub*, or `python -m octg_agent hub open`) is now the normal way to manage it: a local web page for typing, editing, importing and reviewing. **Spreadsheet import** (`PHASE3_SPREADSHEET.md`) remains the bulk path (also available inside the Hub): `business template` makes the blank workbook, `business import <workbook.xlsx> --dry-run` previews, `business import <workbook.xlsx>` imports after you confirm.

Neither layer knows any website; tasks are plain language. Everything is local.

```
YOU / CLI ──HTTP──▶ LOCAL AGENT (Python) ──WebSocket──▶ CHROME EXTENSION (MV3) ──▶ REAL CHROME
                         │
                         └──▶ authorized local folders only
```

Everything is local. The engine talks to no server and no model; only the Phase 2 brain calls a model provider you configure (a local one needs no internet).

## Install the extension

1. Open `chrome://extensions`, switch on **Developer mode**.
2. **Load unpacked** → choose `D:\Tool\browser agent\extension`.
3. The extension ID must read `kfofbldlpmoobgiafcbohiiebmfggecg` (it is pinned by the key in `manifest.json`; the agent only accepts that origin).
4. Pin it, then click its icon: the side panel opens.
5. Optional: *Details → Allow access to file URLs* if you want to control `file://` pages.

## Start the agent

```powershell
cd "D:\Tool\browser agent\agent"
python -m pip install aiohttp          # the only dependency
python -m octg_agent serve             # or double-click start_agent.bat
```

It listens on `127.0.0.1:8765` only and prints a **pairing token**. Keep the window open.

## Connect

Side panel → **Connection** → paste the token (`python -m octg_agent token` prints it again) → **Connect**.
The badge turns green (`connected`) and the panel shows the current site, tab, URL, title, task, action and an activity log.
Reconnection after an agent restart is automatic.

## Drive it (CLI test driver)

```powershell
python -m octg_agent tools                         # every action with its risk level
python -m octg_agent call get_tabs
python -m octg_agent call inspect_page             # elements get ids like el_004, bound to a snapshot_id
python -m octg_agent call type element_id=el_004 text="hello"      # snapshot_id auto-filled by the CLI from the latest inspect
python -m octg_agent call click element_id=el_007
python -m octg_agent call verify checks='[{"type":"text_present","value":"Thanks"}]'
python -m octg_agent repl                          # interactive
python -m octg_agent tools --json                  # machine-readable tool schema for a future LLM
```

Every response has the same shape: `request_id, success, action, result, error, verification, risk, duration_ms` (see `PROTOCOL.md`).
Actions that change the page carry a `verification` block (URL/title change, new/closed tabs, DOM/text/scroll/focus/dialog evidence).
The agent never says "done" just because Chrome accepted a command.

## Local files

Nothing on disk is readable or writable until **you** authorize a folder, locally:

```powershell
python -m octg_agent fs allow "D:\Client Assets"
python -m octg_agent fs list
python -m octg_agent fs revoke "D:\Client Assets"
```

There is no HTTP endpoint, action or extension message that can change this list. `upload_file` reads a file from an authorized folder
inside the agent and hands the *bytes* to the extension; the extension never sees a path. `fs_find` and `fs_save_download` are also confined to the list.

## Real-Chrome smoke test

See **`SMOKE_TEST.md`**: load the extension in your own Chrome, `python -m octg_agent serve`, connect, then `python -m tests.smoke_real_chrome`.

## Tests

```powershell
cd "D:\Tool\browser agent\agent"
python -m unittest discover -s tests -t .     # fast tests (engine + AI layer, scripted model, no network): protocol, bridge security, dispatcher, files, HTTP guard, architecture rules
python -m tests.e2e.run_e2e                   # 23 end-to-end cases: real browser + real extension + real agent (≈50 s, headless)
python -m tests.e2e.run_e2e --headed          # same, with a visible browser window
python -m tests.e2e.run_e2e --real-chrome     # same suite on the INSTALLED Google Chrome (fresh temp profile, needs Node)
python -m tests.e2e.run_e2e --real-chrome --resilience   # + MV3 worker idle (75 s) and worker-killed recovery cases
python -m tests.e2e.run_e2e --list
```

The e2e run starts its own agent, a throw-away browser profile and a fixture web server on loopback; it never touches your Chrome profile.
By default it uses a Chromium that accepts `--load-extension` (Playwright's, found automatically; override with `OCTG_CHROME`).
Branded Chrome 137+ ignores that switch, so `--real-chrome` launches the installed Chrome through `tests/e2e/chrome_launcher.js`, which loads
the extension over a DevTools pipe (`Extensions.loadUnpacked`). Results: `agent/tests/e2e/last_run.json` / `last_run_real_chrome.json`.

## Phase 2 quick start

```powershell
python -m octg_agent provider list                      # local (Ollama) should show as available
python -m octg_agent task start "Open example.com and read the main heading" --wait
```
Or reload the extension at `chrome://extensions`, open the side panel, type the task and press **Run**. See `PHASE2.md` for models, keys,
routing, approvals and tests.

## Layout

```
extension/            MV3 extension (vanilla JS, no build step)
  manifest.json       permissions: tabs scripting webNavigation sidePanel storage alarms downloads  (+ <all_urls>); no debugger
  background/         service worker: bridge (WS), tabs/navigation tracker, actions, verification, page injection
  content/page_agent.js   injected on demand: page understanding + generic actions
  sidepanel/          status + task UI; stt.js / voice_ui.js (Phase 2.1 voice input); mic_permission.html
agent/
  octg_agent/         bridge/ (WebSocket server)  engine/ (registry, dispatcher, state, risk)  files/ (allow-list)
                      api/ (HTTP + CLI)           reasoning/ (providers, router, keys, pricing)   brain/ (task loop, observation, context, approval, history)
  tests/              unit tests, fake extension, e2e suite + fixtures
ARCHITECTURE.md  PROTOCOL.md
```

## Security model in one screen

* The agent binds to loopback. The extension dials out, and only to `ws://127.0.0.1|localhost|[::1]`.
* WebSocket: `Origin` must be this extension's ID **and** the first message must carry the pairing token.
* HTTP API: secret header required; requests with an `Origin` header (any web page) or a foreign `Host` are rejected.
* No content scripts run on pages until an action needs one; no cookies, history, passwords or credentials are read or stored.
  Values of password / card-number / one-time-code / secret-looking fields are masked in every output; text typed into them, page text and element values are never logged.
* Every action carries structured **risk tags** (`risk.level / tags / approval`). Phase 1 only tags; `approval_enforced` is always `false`.
  Clicks on elements named like *Delete / Publish / Send / Pay…* and typing into password fields are escalated automatically.

## Limitations (no `debugger` permission in Phase 1)

| Area | Limitation |
|---|---|
| Input realism | Clicks/keys are page-dispatched events (`isTrusted=false`). Sites that insist on trusted input (some payment widgets, drag-and-drop, fullscreen, clipboard) may ignore them. Enter/Tab/Space/Backspace/typing defaults are emulated. |
| Popups | Script popups (`window.open`) are blocked by Chrome without user activation. Plain `target=_blank` links are handled (the extension opens the tab). |
| JS dialogs | `alert/confirm/prompt/beforeunload` block the page and cannot be dismissed; the action ends with `PAGE_UNRESPONSIVE`/`ACTION_TIMEOUT`. A human must dismiss it. |
| File upload | Only an `<input type=file>` that exists in the DOM (even hidden). Pickers opened via `showOpenFilePicker` or inputs created on click can't be driven. Max 15 MB per call (one WebSocket message). |
| Screenshots | Visible viewport only (no full-page), needs the tab to be active and its window not minimized (it activates the tab for you), ~2 captures/second. |
| History | `tabs.goBack` skips entries that never had user activation; the agent falls back to the page's own `history.go()` (reported as `via: history_api`). |
| Frames/shadow DOM | Open shadow roots and same/cross-origin iframes are inspected; closed shadow roots are invisible. Element `rect`s are relative to their own frame's viewport. |
| Canvas/image UIs | Not understandable from the DOM; needs screenshots + vision (later phase). |
| Restricted pages | `chrome://`, Chrome Web Store, other extensions' pages, `about:`, `view-source:`, `data:`/`blob:` documents, Chrome's network-error page, and `file://` unless enabled → `UNSUPPORTED_PAGE`. |
| Incognito | The extension is off in incognito unless you enable it there. |
| Service worker | MV3 may suspend the worker; a 20 s WebSocket ping plus a 30 s alarm keep it connected and reconnect it. A command sent in a reconnect gap fails with `EXTENSION_NOT_CONNECTED`. |
| Logs | `agent/data/logs/*.jsonl` record URLs, titles, timings and text typed into ordinary fields (not sensitive-looking fields). Treat the folder as private. |

Tested on Windows 11, Python 3.11: Chromium 149 and the installed Google Chrome 154.0.8037.98 (fresh profile, headed). Your own profile
(other extensions, policies) is covered by the manual steps in `SMOKE_TEST.md`.

## AI collaboration

See `COLLAB.md` (protocol, capabilities, security, SEO workflow) and `COLLAB_CHECKLIST.md` (status, runbook). Quick start: `python -m octg_agent collab whoami`.
