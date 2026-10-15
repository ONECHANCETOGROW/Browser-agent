# Architecture (Phase 1)

## Principle

Build **capabilities, not workflows.** The engine can observe, navigate, click, type, select, scroll, switch tabs, upload, download and
verify. It never learns what site it is on; a later planner works that out from observations. Nothing in `engine/`, `bridge/`,
`files/`, `api/` or the extension names a website or an AI provider (a test enforces this).

```
                      ┌────────────────────────── LOCAL AGENT (Python, asyncio + aiohttp) ───────────────────────────┐
 CLI / HTTP ─────────▶│ api/      HTTP + CLI driver                                                                  │
 (later: planner)     │ engine/   registry (schemas + risk) → dispatcher (validate, risk, prepare, execute, log)     │
                      │           state (tabs, events, snapshots, downloads)   risk (dynamic tags)                   │
                      │ bridge/   WebSocket server, handshake, request/response correlation, reconnect handling      │
                      │ files/    authorized-folder allow-list (the ONLY route to the local disk)                    │
                      │ reasoning/ ModelProvider interface — unused in Phase 1, never imported by the above          │
                      └────────────────────────────────────┬─────────────────────────────────────────────────────────┘
                                                           │ ws://127.0.0.1:8765/ext   (extension dials out)
                      ┌────────────────────────────────────▼─────────────────────────────────────────────────────────┐
                      │ CHROME EXTENSION (MV3, vanilla JS)                                                            │
                      │ service worker: bridge · tabs/navigation tracker · actions · verification · snapshot store    │
                      │ page_agent.js  (injected per action): DOM/ARIA understanding + generic page actions           │
                      │ side panel: status + activity log                                                             │
                      └────────────────────────────────────┬─────────────────────────────────────────────────────────┘
                                                           ▼
                                                     REAL CHROME
```

## Observe → act → observe

1. **Observe**: `inspect_page` (DOM + ARIA, shadow roots, iframes; ids bound to a `snapshot_id`), `read_page`, `find_text` (find-in-page: DOM text, scrolls the match into view, returns the text around it), `get_page_state`, `get_tabs`.
   Screenshots exist but are on demand, not the default.
2. **Act**: a generic action addressed by `element_id + snapshot_id`.
3. **Observe the result**: the extension captures evidence before and after (URL, title, navigation events, new/closed tabs, DOM
   mutations, text signature, scroll, focus, dialogs, downloads) and attaches it as `verification`. Actions also check their own postcondition
   (typed value read back, checkbox state, selected option, files attached, navigation finished) and fail with `POSTCONDITION_FAILED` otherwise.
4. The caller (you today, a planner later) decides what the evidence means.

## Browser state

The extension pushes events (`tab_created`, `tab_activated`, `navigation_committed`, `download_changed`, …) and a full `tabs_snapshot` on each
(re)connect; the agent folds them into `BrowserState`. `get_tabs`/`get_browser_state` are the authoritative refresh. The extension also keeps a
per-tab navigation tracker (`loading/done/error`, SPA and fragment changes) that powers `navigate`/`back`/`forward` waiting and marks
Chrome's error page as uncontrollable.

## Page understanding

`content/page_agent.js` is injected on demand into the frames involved (no persistent content scripts). It lists visible interactive
elements by tag, ARIA role, `onclick`, contenteditable and a cursor-pointer heuristic; computes accessible names (labelledby, aria-label,
labels, placeholder, alt, text); descends into open shadow roots; and reports value/checked/options/disabled/rect/viewport state. Hidden
file/checkbox/radio inputs are included (flagged `hidden`) because sites commonly style over them. Password values are never returned.
Element references live in the page as `WeakRef`s keyed by snapshot; a fingerprint (tag, role, type, name) is re-checked before every action, which is
what produces `STALE_ELEMENT` instead of acting on the wrong element.

## Multi-tab

Every action takes an optional `tab_id`; otherwise it targets the active tab of the focused window. `switch_tab` focuses tab and window and
confirms both. Tabs opened by clicks are detected from events and surfaced in `verification.new_tabs`. Snapshots are per tab.

## Local file authorization

`files/authorization.py` holds a user-managed allow-list (`python -m octg_agent fs allow …`). Paths are resolved through symlinks/junctions and
must stay inside an authorized root; drive roots, the home directory and Windows directories can't be authorized; NTFS alternate streams, `..`
tricks and sibling-prefix tricks are rejected. Uploads: agent reads the file (size-limited), the extension receives bytes only. Downloads:
Chrome saves to its download folder; `fs_save_download` copies into an authorized folder. The extension has no filesystem access of its own.

## The AI layer (Phase 2)

`brain/` sits on top of the dispatcher and `reasoning/` supplies model adapters; neither is imported by `engine/`, `bridge/` or `files/`
(tests enforce it). The LLM-facing tool list is derived from the same action registry, so there is still exactly one action system. See
`PHASE2.md`.

## Business Brain (Phase 3)

`business/` is a leaf package (`store`, `documents`, `resolver`, `schema`, `cipher`) that `engine/`, `bridge/`, `files/` and `reasoning/` never import; the task loop reaches it only through the
read-only `BrainReader` (`brain/business_glue.py`). The model refers to business details by token; code substitutes the exact stored value just before the browser action; values never enter a prompt,
log, history row or audit row. Documents come only from folders enrolled on top of `fs allow`, and are revalidated (authorization, real path, hash) at every use. Off by default. See `PHASE3.md`.
The **Knowledge Hub** (`hub/`, `business/admin.py`, `business/markdown_import.py`, `PHASE3_HUB.md`) is the human interface: static pages under `/hub/` and a JSON API under `/hub/api/` in the same loopback server, guarded by a session cookie + CSRF header + same-origin checks, calling the same `BusinessStore` and import pipeline as the CLI; the extension refuses to script `/hub` pages so a model cannot operate it.
Bulk data enters through the spreadsheet importer (`business/spreadsheet/`, `PHASE3_SPREADSHEET.md`): a stdlib-only XLSX/CSV reader that treats cells as data, a column mapper that asks instead of guessing, a read-only diff against the database (conflicts, duplicates, missing, sensitive), and a single-transaction commit after an automatic backup. It imports nothing from the engine, bridge, models or task loop, and no model or panel command can write an import.
Every fact, folder, document and audit row belongs to one **scope** — the owner's own business or one client — and a task reads exactly one scope (`PHASE3_CLIENTS.md`). Directory walks never enter a Windows
junction/symlink (`files/authorization.safe_walk`).

## Voice input (Phase 2.1)

Speech-to-text lives entirely in the side panel (`sidepanel/stt.js` + `voice_ui.js`): microphone → STT engine adapter → text in the Task box → the existing Run button → the
existing `start_task` path. It adds no agent, no pipeline, no permission and no Python code, and the audio never leaves the browser's speech engine (on-device by default). See
`PHASE2_1_VOICE.md`.

## Provider independence

`reasoning/provider.py` fixes the interface (`BaseProvider.complete(messages, tools)` with registry-derived tools). Adapters: local Ollama,
Gemini (`google-genai`), FreeLLMAPI and generic OpenAI-compatible, plus a scripted mock for tests; `reasoning/router.py` chooses and falls back.
None of it touches the engine, bridge or extension.

## Communication and reliability

* Extension → agent dial-out WebSocket (MV3 workers can't listen). Origin pinned to the extension ID via the manifest key; first message carries the pairing token.
* One extension session; a reconnect replaces a stale one. In-flight requests fail immediately with `EXTENSION_DISCONNECTED`; they never hang.
* Keep-alive: 20 s application ping + 25 s protocol heartbeat + a 30 s alarm that reconnects; exponential backoff 1 s → 10 s.
* Actions are serialised in the agent and in the extension so evidence from one action can't bleed into another.

## Permissions (and why)

| Permission | Why |
|---|---|
| `tabs` | list/open/switch/close tabs, read URL and title |
| `scripting` | inject the page agent into the frames an action targets |
| `webNavigation` | detect navigations, redirects, errors, SPA/fragment changes |
| `sidePanel` | the status UI |
| `storage` | pairing config (`local`), snapshot map (`session`) |
| `alarms` | reconnect/keep-alive |
| `downloads` | `download_file`, `list_downloads`, download events |
| host `<all_urls>` | the agent must work on any site |

Deliberately absent: `debugger`, `cookies`, `history`, `webRequest`, `nativeMessaging`, `clipboard*`, content scripts, `externally_connectable`.

## AI collaboration (COLLAB.md)

`collab/` exposes registered **capabilities** (not engine actions) to a reasoning/coding AI over `/api/collab/*` with its own token, persistent jobs, evidence,
artifacts, approvals and an append-only audit (`collab.db`). `seo/` holds the guarded fetcher, signal extraction, crawler, SERP parser, audit rules and report builder;
campaigns live in business.db (`business/campaigns.py`). Approvals are decided only in the Knowledge Hub ("AI & Approvals") or the TTY-only CLI. See `COLLAB.md`
and `COLLAB_CHECKLIST.md`.
