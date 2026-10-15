# OCTG protocol (version 1)

Two layers share one vocabulary: the **local API** you call, and the **extension bridge** the agent uses to reach Chrome.

## 1. Local API (loopback HTTP)

Every `/api/*` call needs the header `X-OCTG-Token: <api token>` (`python -m octg_agent token --api`) and must not carry an `Origin` header.

| Method & path | Purpose |
|---|---|
| `POST /api/action` | Run one action. Body: `{"action": "click", "parameters": {...}, "request_id": "optional", "timeout_s": optional}` |
| `GET /api/tools` | Machine-readable tool schema (see §4) |
| `GET /api/state` | Agent's live browser model: connection, tabs, active tab, latest snapshot per tab, task, current action |
| `GET /api/events?since=N&limit=M` | Browser events (tab created/removed/activated/updated, navigations, downloads) |
| `GET /api/logs?tail=N` | Last N structured action-log entries |
| `GET /healthz` | `{"ok": true}` (unauthenticated, reveals nothing else) |

### Response (always these keys)

```json
{
  "request_id": "req_3f2a91c0d4e1",
  "success": true,
  "action": "click",
  "result":  { "...action specific..." },
  "error":   null,
  "verification": { "...evidence, see §3..." },
  "risk": { "level": "medium", "tags": ["interaction"], "approval": "contextual", "approval_enforced": false, "reasons": [] },
  "duration_ms": 412
}
```

On failure `success` is `false`, `result` is usually `null`, and `error` is `{ "code": "...", "message": "...", "details": {...} }`:

```json
{ "success": false, "error": { "code": "UNSUPPORTED_PAGE", "message": "This Chrome page cannot be controlled by the extension.",
   "details": { "url": "chrome://version/", "reason": "Chrome internal pages (chrome://) block extension scripts." } } }
```

HTTP status is 200 for every executed action, success or not. 400 = malformed body, 401/403 = guard rejection.

### Error codes

| Code | Meaning |
|---|---|
| `UNKNOWN_ACTION`, `INVALID_PARAMS` | Rejected before reaching the browser (details list every problem) |
| `EXTENSION_NOT_CONNECTED`, `EXTENSION_DISCONNECTED`, `ACTION_TIMEOUT`, `BAD_EXTENSION_RESPONSE` | Bridge problems |
| `UNSUPPORTED_PAGE` | Chrome does not let the extension control this page (never reported as success) |
| `STALE_ELEMENT` | Element id/snapshot no longer valid. `details.reason`: `snapshot_superseded`, `detached`, `changed`, `document_changed`, `no_snapshot` |
| `ELEMENT_NOT_FOUND`, `ELEMENT_NOT_INTERACTABLE` (`details.obscured_by`), `NOT_EDITABLE`, `NOT_A_SELECT`, `OPTION_NOT_FOUND`, `OPTION_DISABLED`, `NOT_CHECKABLE`, `NOT_A_FILE_INPUT` | The target can't do what was asked |
| `POSTCONDITION_FAILED` | The action ran but its own check failed (typed value differs, state didn't change, `verify` failed, wait timed out…). `details.failed` lists the checks |
| `TAB_NOT_FOUND`, `NO_ACTIVE_TAB`, `LAST_TAB`, `NO_HISTORY`, `NAVIGATION_FAILED`, `PAGE_NAVIGATED`, `PAGE_NOT_READY`, `PAGE_UNRESPONSIVE` | Tab/navigation/page state |
| `DOWNLOAD_FAILED`, `DOWNLOAD_NOT_FOUND` | Downloads |
| `FILE_NOT_AUTHORIZED`, `FILE_NOT_FOUND`, `FILE_TOO_LARGE`, `FILE_EXISTS` | Authorized-folder rules |
| `INTERNAL_ERROR` | Bug; reported, never swallowed |

## 2. Extension bridge (WebSocket `ws://127.0.0.1:<port>/ext`)

The extension connects out. Handshake: the browser sends `Origin: chrome-extension://<pinned id>`; the first message must be

```json
{"type": "hello", "token": "<pairing token>", "extension_id": "...", "version": "0.1.0", "protocol": 1}
```

Agent answers `{"type": "hello_ack"}` or `{"type": "hello_error", "code": "BAD_TOKEN|PROTOCOL_MISMATCH|BAD_HELLO"}` and closes.

| Direction | Message |
|---|---|
| agent → ext | **command** `{"request_id": "abc123", "action": "click", "parameters": {"element_id": "el_004", "snapshot_id": "snap_..."}}` (no `type` field) |
| ext → agent | **response** `{"request_id": "abc123", "success": true, "action": "click", "result": {...}, "error": null, "verification": {...}}` |
| ext → agent | `{"type": "event", "event": "tab_created|tab_removed|tab_activated|tab_updated|navigation_started|navigation_committed|navigation_completed|navigation_error|navigation_spa|navigation_fragment|download_created|download_changed|window_focus|...", "seq": n, "ts": ms, "data": {...}}` |
| ext → agent | `{"type": "tabs_snapshot", "tabs": [...]}` after every (re)connect |
| agent → ext | `{"type": "status", "task": "...", "current_action": "..."}` for the side panel |
| both | `{"type": "ping"}` / `{"type": "pong"}` |

One command is in flight at a time (the agent serialises actions). The agent adds `risk`, `duration_ms` and post-processing
(e.g. screenshots are written to `data/screenshots/` and replaced by a path; uploads are read from disk by the agent and sent as bytes).

## 3. Verification block

Present on every action that changes browser state. It reports evidence, not opinions:

```json
{
  "tab_id": 123, "tab_closed": false, "settle_ms": 340, "observed_change": true,
  "url":   {"before": "...", "after": "...", "changed": true},
  "title": {"before": "...", "after": "...", "changed": true},
  "navigated": true, "navigations": ["https://..."],
  "new_tabs": [{"tab_id": 124, "url": "...", "title": "..."}], "closed_tabs": [],
  "active_tab_changed": null, "downloads_started": [],
  "page": {"text_changed": true, "dom_mutations": 12, "scroll_changed": false, "dialogs_opened": ["Are you sure?"], "focus_changed": true, "ready_state": "complete"},
  "checks": [{"name": "value_matches", "passed": true}]
}
```

`page.comparable=false` appears instead when a new document replaced the old one. `checks` are the action's own postconditions; any failed
check turns the response into `success:false / POSTCONDITION_FAILED`.

## 4. Snapshots and element ids

`inspect_page` returns a `snapshot_id` and elements `el_001…`. Ids are meaningful **only with that snapshot_id**; every element action
requires both. If the page has been re-inspected, navigated, re-rendered or the element was removed/changed, the answer is `STALE_ELEMENT`
— never a click on whatever now sits at that position. (The CLI auto-fills the latest `snapshot_id` as a convenience; the engine does not.)

## 5. Tool schema for a future LLM

`GET /api/tools` (or `python -m octg_agent tools --json`):

```json
{"schema_version": 1, "protocol_version": 1, "tools": [
  {"name": "click", "description": "...", "category": "interact",
    "input_schema": {"type": "object", "properties": {...}, "required": [...], "additionalProperties": false},
    "risk": {"level": "medium", "tags": ["interaction", "may_trigger_irreversible_action"], "approval": "contextual"},
    "target": "extension", "mutating": true} ]}
```

JSON-Schema inputs, so any provider's function-calling format can be generated from it. `risk.approval` says what a later approval gate should do
(`never` / `contextual` / `required`); Phase 1 never blocks.

## 5b. AI task API and side-panel messages (Phase 2)

HTTP (same token/loopback guard): `POST /api/tasks {instruction, provider?, policy?, max_steps?, approve_medium?}`, `GET /api/tasks`,
`GET /api/tasks/{id|current}`, `POST /api/tasks/{id|current}/{approve|deny|pause|resume|stop|cancel}`, `GET /api/providers`,
`POST /api/providers/{name}/test`, `GET /api/providers/detect-local`. Responses are `{"ok": bool, "task"|"error": ...}`. No route reads or writes API keys.

Extension socket (authenticated by the pairing token/origin): panel -> agent `{"type":"ui","command":"start_task|approve|deny|pause|resume|stop|get_status","request_id",...}`;
agent -> panel `{"type":"ui_result",...}`, `{"type":"task_update","task":{summary}}` (no page text / field values), `{"type":"providers","providers":[{name,model,available,local}],"policy"}`
(never keys).

Phase 3 additions (Business Brain; all absent/refused while its feature flag is off): `start_task` and `POST /api/tasks` accept an optional boolean `business` (force it on/off for this task);
task summaries carry `business: {active, reason, fills}` (counts only). Panel commands `biz_status|biz_profile|biz_confirm|biz_reject|biz_resolve|biz_reveal|biz_docs|biz_doc_approve|biz_doc_exclude|biz_sources|biz_audit`
are answered by a `ui_result` carrying a `biz` payload (masked values; `biz_reveal` returns one value on explicit request). Client Brain additions: `start_task` / `POST /api/tasks` accept an optional string `client` (an id or an exact client name); `POST /api/tasks/{id}/client {"client": ...}` and the panel command `select_client {client_id}`
choose the client of a running task that asked which one (`waiting_kind: "client_needed"`); task summaries carry `business.client: {id, name, pending, reason, candidates}`. Panel commands `biz_clients|biz_client|biz_client_docs|biz_client_sources|biz_client_seo|biz_client_audit`
(each takes `client_id`) return masked, single-client views; `biz_resolve` takes an optional `client_id`. There is no command that creates or edits a client.
Knowledge Hub (browser UI, not the extension protocol): `GET /hub/` and `/hub/static/*` are public static files; `/hub/api/*` needs the `octg_hub` session cookie and the `X-Hub-CSRF` header (obtained from `POST /hub/api/login` with the access token or a one-time code from `python -m octg_agent hub open`), a loopback `Host`, and for POSTs the Hub's own `Origin`. See PHASE3_HUB.md.
Spreadsheet import: `biz_imports` lists past imports; `biz_import_preview {name, data_b64}` (an .xlsx of at most 3 MB) returns a masked preview and writes nothing. There is no panel command that imports, decides, verifies or creates anything: `business import ...` runs only in the local terminal.
There is deliberately no HTTP route and no panel command that enrols folders,
imports or sets values, or lowers a sensitivity. `Dispatcher.execute(..., sensitive_call=True)` (agent-internal, not an API field) keeps a call's text and the page's reported value out of the action log.

## 6. Action reference

`*` = required. All browser actions also accept `tab_id` (default: active tab of the focused window).

| Action | Category | Risk (level / approval) | Parameters | What it does |
|---|---|---|---|---|
| `ping` | observe | none / never | — | Round-trip check with the Chrome extension; returns extension version and protocol. |
| `get_browser_state` | observe | none / never | — | Overview of Chrome: all windows and their tabs, the active tab, the current website, and the latest page snapshot. |
| `get_tabs` | observe | none / never | — | List every open tab (id, window, title, url, active, status, whether the extension can control it). |
| `get_active_tab` | observe | none / never | — | The active tab of the focused window. |
| `get_page_state` | observe | none / never | `tab_id` | Lightweight state of a page: url, title, scroll position, headings, open dialogs/alerts, focused element. No element list. |
| `inspect_page` | observe | none / never | `tab_id`, `viewport_only`, `max_elements`, `include_selectors`, `include_text`, `text_chars` | Inspect the page DOM/ARIA structure and list the interactive elements a human could use, with ids bound to a new snapshot_id. Prefer this over screenshots. |
| `read_page` | observe | none / never | `tab_id`, `scope`, `max_chars` | Read the visible text of a page (viewport) or all of its text (page). |
| `find_text` | observe | none / never | `text`*, `tab_id`, `case_sensitive`, `whole_word`, `index`, `max_matches`, `context_chars`, `scroll` | Find a word or phrase in the page text (DOM text, never screenshots; form-field contents are never searched) and scroll the match into view. Returns whether it was found, the number of matches, and for each match its surrounding text, nearest heading and element (with element_id when the current inspect_page snapshot covers it). Case-insensitive by default. Use index to move to the next match. See *find_text* below. |
| `screenshot` | observe | low / never | `tab_id`, `format`, `quality`, `include_base64` | Capture the visible viewport of a tab (PNG/JPEG). The image is saved locally and its path returned. Activates the tab if needed. |
| `list_downloads` | observe | none / never | `limit` | Recent Chrome downloads with state and local file path. |
| `verify` | observe | none / never | `tab_id`, `checks`* | Check conditions about the current state (url/title/text/elements/tabs). Succeeds only if every check passes. |
| `wait` | observe | none / never | `tab_id`, `ms`, `until`, `timeout_ms`, `poll_ms` | Pause for a fixed time (ms) or poll until conditions hold (until) up to timeout_ms. |
| `navigate` | navigation | low / never | `url`*, `tab_id`, `timeout_ms` | Load a URL in a tab and wait for the navigation to finish. Scheme-less input gets https://. |
| `new_tab` | tabs | low / never | `url`, `active`, `window_id`, `timeout_ms` | Open a new tab, optionally at a URL. |
| `switch_tab` | tabs | low / never | `tab_id`, `match` | Focus a tab by tab_id, or by match on url/title. |
| `close_tab` | tabs | medium / contextual | `tab_id`* | Close a tab (tab_id required). Refuses to close the last tab of the last window. |
| `reload` | navigation | low / never | `tab_id`, `bypass_cache`, `timeout_ms` | Reload a tab and wait for it to finish. |
| `back` | navigation | low / never | `tab_id`, `timeout_ms` | Go back in the tab's history. |
| `forward` | navigation | low / never | `tab_id`, `timeout_ms` | Go forward in the tab's history. |
| `click` | interact | medium / contextual | `element_id`, `snapshot_id`, `x`, `y`, `force`, `tab_id` | Click an element (element_id + snapshot_id) or a viewport coordinate (x, y). Reports what changed. |
| `type` | interact | low / never | `element_id`*, `snapshot_id`*, `text`*, `clear`, `mode`, `tab_id` | Type text into an input, textarea or contenteditable. Replaces existing content unless clear=false. Reads the value back to confirm. |
| `clear` | interact | low / never | `element_id`*, `snapshot_id`*, `tab_id` | Empty an input/textarea/contenteditable. |
| `select` | interact | low / never | `element_id`*, `snapshot_id`*, `option`, `values`, `tab_id` | Choose an option in a native <select> by value, label or index. |
| `check` | interact | low / never | `element_id`*, `snapshot_id`*, `force`, `tab_id` | Tick a checkbox/radio/switch and confirm its state. |
| `uncheck` | interact | low / never | `element_id`*, `snapshot_id`*, `force`, `tab_id` | Untick a checkbox/switch and confirm its state. |
| `scroll` | interact | low / never | `direction`, `amount`, `to`, `element_id`, `snapshot_id`, `tab_id` | Scroll the page (or the element's scroll container) by direction/amount or to top/bottom, or bring an element into view. |
| `press_key` | interact | medium / contextual | `key`*, `modifiers`, `element_id`, `snapshot_id`, `tab_id` | Send a key (Enter, Tab, Escape, ArrowDown, a, …) to the focused or given element. Page-synthesised events are untrusted; common defaults are emulated. |
| `upload_file` | files | high / required | `element_id`*, `snapshot_id`*, `local_file_path`, `local_file_paths`, `tab_id` | Attach a local file from an AUTHORIZED folder to an <input type=file>. The agent reads the file; the extension never sees a path. |
| `download_file` | files | medium / contextual | `url`*, `filename`, `timeout_ms` | Download an http(s) URL through Chrome (uses the browser's logged-in session) into Chrome's download folder. |
| `get_agent_state` | agent | none / never | — | Local agent status: extension connection, tab model, latest snapshots, authorized-folder count. |
| `get_events` | agent | none / never | `since`, `limit` | Browser events seen by the agent (tab opened/closed/activated, navigations, downloads). |
| `set_task` | agent | none / never | `task`* | Set the human-readable current task label shown in the side panel. |
| `fs_list_roots` | files | low / never | — | List the authorized local folders (managed only via the local CLI). |
| `fs_find` | files | low / never | `pattern`*, `root`, `max_results` | Find files by name pattern (e.g. *.png) inside authorized folders. |
| `fs_save_download` | files | medium / contextual | `download_id`*, `dest_dir`*, `filename`, `overwrite` | Copy a completed Chrome download into an authorized folder. |

### find_text

Find-in-page for long pages, so a caller reaches a section directly instead of scrolling blindly. It is website-agnostic: it searches the rendered
**DOM text** (text nodes, open shadow roots, and every iframe the extension can script), never screenshots, and never the contents of form controls
(`input`, `textarea`, `select`), so a typed password, OTP or card number cannot come back through it. Restricted Chrome pages fail with `UNSUPPORTED_PAGE`
like every other page action.

* Matching: case-insensitive by default (`case_sensitive`), optional `whole_word`; runs of whitespace are normalised and a phrase may span inline
  elements (`<b>emergency</b> <i>rollback</i>` matches `emergency rollback`). Text in hidden or collapsed content (`display:none`, closed `<details>`)
  is **counted separately** (`hidden_matches`) and never scrolled to.
* Result: `found`, `total_matches`, `hidden_matches`, `matches[]` (up to `max_matches`), `focused` (the match brought into view, or `null`),
  `scroll {before, after, max_y}`, `frames_searched`, `notes`. Not finding the text is a normal result (`found: false`), not an error.
* Each match describes **what it is**, so the caller can choose the occurrence it wants. `find_text` is a primitive: it never ranks, prefers, filters or
  reorders matches (document order, always), and it contains no site-specific logic. All fields come from the DOM/ARIA only:

  | Field | Meaning |
  |---|---|
  | `match_index` | Position in document order; pass it as `index` to scroll to that occurrence. |
  | `text`, `context` | The matched text and the text around it, the match marked `[[like this]]`. |
  | `match_type` | `heading` (inside `h1`–`h6`/`role=heading`), `link` (inside `a[href]`/`role=link`), `other` (a control: button, label, summary, tab, menu item, option …) or `body_text`. A match that is both a heading and a link is `heading`; `is_link` still says `true`. |
  | `is_heading`, `heading_level` | Heading flag, and its level 1–6 (`aria-level`, default 2 for `role=heading`); `heading_level` only on headings. |
  | `is_link` | The match sits inside a link. |
  | `element_tag`, `role` | The semantic element (heading, else link, else control, else the text's parent) and its role (`null` when none). |
  | `nearest_heading` | Text of the closest heading at or before the match in document order (`null` if none). |
  | `visibility`, `in_viewport` | Where the match was when found: `in_view`, `above`, `below` or `beside` the viewport (`in_viewport` is the boolean form; on `focused` it is measured after scrolling). |
  | `page_y`, `frame` | Document y of the match; frame id when it is inside an iframe. |
  | `element_id`, `snapshot_id` | Only when the current `inspect_page` snapshot covers the interactive element. |
* Scrolling: `scroll` (default true) centres match number `index` (0-based; use `index=1,2,…` to step through matches), including inside scrollable
  containers and iframes. `scroll: false` only counts and describes. The page itself is not modified (no highlight, no selection).
* Snapshots: `element_id` + `snapshot_id` appear on a match only when the tab's **current** `inspect_page` snapshot covers the matched interactive
  element, so the ids can be used with `click` etc. `find_text` never creates or replaces a snapshot, and scrolling does not invalidate one; navigation or a
  newer `inspect_page` supersedes ids exactly as before (`STALE_ELEMENT`).
* Verification: it is a mutating action (it moves the viewport), so the usual before/after `verification` block is attached, with a `match_in_view` check.
* Logs keep only counts and positions; match text, context, headings and the semantic fields are never written to disk.
