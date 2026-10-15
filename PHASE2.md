# Phase 2 — the generic AI agent brain

Phase 1 is the hands and eyes (browser engine). Phase 2 adds the reasoning layer **on top of it**, without changing it: the model chooses
among the same registry tools, every call goes through `Dispatcher.execute()`, and nothing in `engine/`, `bridge/`, `files/` or the
extension's browser code knows a model exists. The agent is website-agnostic: there is no per-site code anywhere (a test scans for it).

```
 you ── side panel / CLI / HTTP ──▶ TaskManager ──▶ TaskRunner (one task)
                                                     │  observe → reason → validate → approve → act → observe → verify → update state
        ObservationBuilder ◀───────── Dispatcher ◀───┤        (the ONLY way to touch the browser)
        ContextManager ──▶ ModelRouter ──▶ local (Ollama) │ Gemini │ FreeLLMAPI │ any OpenAI-compatible
        ApprovalPolicy · TaskState · TaskHistory (SQLite) · usage/cost tracking
```

## Use it

**Side panel** (reload the extension at `chrome://extensions` once after updating): pick a model (Auto = routed by policy), type a task,
press **Run**. The panel shows status, current task/website/tab/action, the approval card (**Approve / Deny**), *Waiting for you* (**continue**),
**Pause / Resume / Stop**, the result, and the activity log.

**CLI** (development/debugging; same conventions as Phase 1):

```powershell
python -m octg_agent provider list                  # what is available, what is missing
python -m octg_agent provider test local            # one tiny tool-call round trip
python -m octg_agent task start "Open example.com and read the main heading" --wait
python -m octg_agent task start "..." --provider gemini        # force one provider
python -m octg_agent task start "..." --policy GEMINI_FIRST    # LOCAL_FIRST | GEMINI_FIRST | FALLBACK | MANUAL
python -m octg_agent task status | approve | deny | pause | resume | stop | list | show <id>
python -m octg_agent provider usage                 # model calls / tokens / est. cost per provider across all tasks
```

The agent works on the **active tab of the focused window** (and switches tabs itself when the task needs it). Open a blank tab first if you
do not want it to touch the page you are looking at.

## Models

| Provider | How to enable | Notes |
|---|---|---|
| **local** (Ollama) | Install/run Ollama (`ollama serve`) and pull a model; default `qwen3:8b`. Nothing is installed or downloaded for you. | Native API, thinking off, exact token counts, JSON fallback for models without tool support. LM Studio / llama.cpp: set `providers.local.runtime = "openai"` and its URL. |
| **gemini** | `python -m octg_agent provider set-key gemini` (hidden prompt) **or** env `GEMINI_API_KEY` / `GOOGLE_API_KEY`. | `google-genai` SDK (`pip install google-genai`), `generate_content`, tools as JSON Schema, automatic function calling **disabled** (the dispatcher runs tools), mode `ANY`. Default model `gemini-3.5-flash-lite`; set `providers.gemini.model` (e.g. `gemini-3.8-flash`, `gemini-3.1-pro-preview`). Docs checked 2026-10-07. |
| **freellmapi** | Run [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (default `http://localhost:3001/v1`), then `provider set-key freellmapi` or env `FREELLMAPI_API_KEY`; URL via `FREELLMAPI_BASE_URL` or `brain.json`. | OpenAI-compatible; `model: "auto"`. Optional: if it is down the others carry on. |
| any OpenAI-compatible | `providers.openai_compat = {"enabled": true, "url": ..., "model": ...}` + key via `OCTG_OPENAI_COMPAT_API_KEY`. | |

**Keys never leave the agent process.** They are read from the environment or `data/provider_keys.json` (local file, git-ignored), are never
returned by any API route, never sent to the extension/side panel, never logged (errors are scrubbed), and there is no HTTP route that sets
them. `provider list` shows only *whether* a key exists and where from.

### Routing (`data/brain.json` → `router`)

| Policy | Behaviour |
|---|---|
| `LOCAL_FIRST` (default) | Simple steps → local. Complex steps (first step, failed actions, unusable replies, stale recovery, big pages, text-heavy, vision, CAPTCHA/login signals, long task text) → `strong_order` (gemini, freellmapi), local as last resort. A provider that fails is put on cooldown; repeated failure escalates and stays escalated for `sticky_steps`. |
| `GEMINI_FIRST` | gemini → freellmapi → local |
| `FALLBACK` | `order` (default local → gemini → freellmapi) |
| `MANUAL` | exactly one provider, no fallback |

Score weights and the `complexity_threshold` are configuration (`router.weights`, `router.complexity_threshold`), not code. Providers that are
not configured or unreachable are skipped automatically.

## How a step works

1. **Observe** — `get_tabs` (cheap) every step; `inspect_page` only when the page changed (action had effect, URL changed, error, or first sight).
2. **Compact view for the model** — tabs, URL/title, headings, dialogs/alerts, scroll, signals (`captcha`, `login_form`, `dialog_open`,
   `error_page`, `restricted_page`), a *diff* of what changed, visible text only when the URL changed, and the interactive elements **ranked** by
   role, on-screen position and overlap with the task, de-duplicated and capped (with an "omitted" count). Screenshots only if the model asks.
3. **Context** — one compact state document per call (task, step, notes, last 8 steps, last result + verification, observation), trimmed to a token
   budget (elements first, then history, then text). Not a growing transcript. Task-scoped: nothing carries over between tasks.
4. **Reason** — the router picks a provider; the model must call exactly one tool. The tool list is the registry in a compact form
   (~2.4k tokens). `snapshot_id` is hidden: the loop binds element ids to the snapshot it showed and pins the call to that tab. A model-supplied
   snapshot id is ignored, and ids from an old snapshot fail with `STALE_ELEMENT`.
5. **Validate** — unknown tools/parameters and bad types are fed back to the model (counted, escalated, bounded by `max_tool_errors`).
6. **Approve** — see below.
7. **Act** — `Dispatcher.execute()`; the engine's `verification` evidence comes back. If the call carried `expect_url_contains` / `expect_text`, the
   loop checks them through the engine's `wait` action.
8. **Update state, recover, repeat** — see *Recovery*.

Limits: `max_steps` (40 browser actions), `max_model_calls` (100), `max_seconds` (900, excluding time waiting for you), `max_tool_errors`,
`max_repeat_actions`.

The model is told to use `find_text` to locate specific content within an already-loaded page (a registry action, no second mechanism) and **not** to run a search: after typing into a
search field it submits (Enter or the site's submit control). The loop guard treats `find_text(text)` and `find_text(text, index=0)` (and case/spacing variants) as the same
repeated attempt, while a different `index` or different text is a different attempt. `find_text` jumps straight to a word or section, scrolls it into
view and returns the surrounding text, instead of scrolling blindly. It is a browser **primitive**: matches come back in document order and each says what
it is (`match_type` heading / link / body_text / other, `is_heading`, `heading_level`, `is_link`, `nearest_heading`, `visibility` — see `PROTOCOL.md`), but nothing is
ranked, skipped or preferred. When a phrase appears in a navigation link, a section heading and a paragraph, the model sees all of them (the step outcome lists
every match's type) and picks the occurrence it wants with `index`. Its outcome is shown to the model but only a count is kept in history; logs hold counts and
positions, never match text or headings.

## Safety

* **Approval gate** (uses the registry risk tags + the engine's dynamic escalation): `high` or `approval=required` → **WAITING_FOR_APPROVAL**
  (e.g. clicking *Delete / Publish / Send / Pay…*, uploading files); `medium` auto unless `approve_medium`; low auto. The model can also call
  `request_approval` before an irreversible step. Deny → the action never runs and the model is told. Closing a tab the task did not open needs approval.
* **Credentials never pass through a model**: typing into password / card / OTP-looking fields is refused and the task waits for you
  (**WAITING_FOR_USER**); you type it yourself and press continue.
* **CAPTCHA** → pause immediately (no model call while it blocks); **login / 2FA** → the model calls `ask_user`; **restricted Chrome pages** →
  the model is told it can only navigate away, and insisting stops the task with an explanation.
* **Loop guard**: the same action repeated with no effect gets a hint at 3 and a hand-over to you at `max_repeat_actions`.

## Recovery (generic, no site rules)

`STALE_ELEMENT` / `ELEMENT_NOT_FOUND` → fresh `inspect_page`, new snapshot, the model re-identifies; after `max_stale_retries` → asks you ·
`ELEMENT_NOT_INTERACTABLE` → re-inspect + "something may cover it" · `PAGE_NAVIGATED` / `PAGE_NOT_READY` → re-observe ·
`ACTION_TIMEOUT` / `NAVIGATION_FAILED` / `PAGE_UNRESPONSIVE` → re-observe the *actual* state; nothing is assumed · extension disconnected → waits for
the reconnect, then asks you · provider failure/rate limit → router falls back (cooldown) · dangerous actions are never retried automatically.

## Model-call timing (instrumentation)

Every model call (every provider attempt, failed ones included) is timed: the router stamps each attempt with `model`, `started_at`, `duration_ms`, outcome and any adapter retry reasons
(`http_502`, `tool_negotiation`); the loop turns them into one `ModelCallRecord` per call with the 1-based call number, the `step` (browser actions done so far), whether it was a fallback
after an earlier failure, and **the action the model chose** (a tool name, or `no_tool_call` / `invalid_call`). They appear as `model` lines in the activity feed
(`call 3: gemini/gemini-3.5-flash-lite 1.8s ok → find_text`, `call 1: local/qwen3:8b 0.4s FAILED (timeout)`), in the task's full view as `model_call_timing`, and in the history DB
(`model_calls` table). `python -m octg_agent provider usage` adds a `latency` block per provider (timed calls, average, median, max, calls over 5 s, failed, retried), so slow
calls can be seen in real data before any timeout is tuned. `python -m tests.e2e.task_timing_report [task_id]` prints one task's report (every call, per-provider averages, slow Gemini
calls, FreeLLMAPI use, `find_text` count, what followed typing, repeated steps). This is measurement only: no routing, timeout, provider or retry behaviour changed, and no prompt, reply, page text,
field value or error text is recorded (only the provider's error *kind*).

## Task state, history, accounting

`TaskState` holds: `task_id, user_instruction, status, goal, current_step, completed_steps, failed_steps, current_tab, known_tabs, current_url,
current_page_title, latest_observation, available_actions, last_action, last_action_result, verification_result, pending_approval, error,
started_at, updated_at` (+ notes, created tabs, usage, routing). Statuses: `PENDING, PLANNING, RUNNING, PAUSED*, WAITING_FOR_APPROVAL,
WAITING_FOR_USER, COMPLETED, FAILED, CANCELLED` (*`PAUSED` added so the UI can show it). **Pause** stops new model calls and browser actions;
**Resume** continues from the same state; **Stop** cancels (an in-flight model call is interrupted).

`data/tasks.db` (SQLite) stores per task: id, time, instruction, status, providers used, model calls, browser actions, duration, result, error, and
per-provider tokens + estimated cost, plus compact steps. It never stores passwords, cookies, tokens, field values (typed text is length-only for
sensitive/long-digit input) or page text. Costs are estimates from `reasoning/pricing.py` (override in `brain.json` → `pricing`); local = $0;
unknown price or missing token counts are reported as unknown, never invented. `provider usage` aggregates across tasks.

## Config (`data/brain.json`, optional — see `agent/brain.example.json`)

Limits, approval policy, observation/context budgets, `router`, `providers`, `pricing`. Env shortcuts: `OCTG_LOCAL_MODEL`, `OCTG_GEMINI_MODEL`,
`OCTG_ROUTER_POLICY`, `OCTG_MAX_STEPS`, `FREELLMAPI_BASE_URL`.

## Tests

```powershell
cd "D:\Tool\browser agent\agent"
python -m unittest discover -s tests -t .                 # unit + integration, scripted model, no network, no real LLM
python -m tests.e2e.run_ai_e2e --real-chrome              # scripted model, REAL extension in installed Chrome (10 cases)
python -m tests.e2e.run_ai_e2e --real-chrome --live-only  # a REAL local model drives generic fixture pages (needs Ollama running)
python -m tests.e2e.run_e2e --real-chrome                 # Phase 1 regression
```

## Limits to know

* Gemini and FreeLLMAPI are verified against faithful fakes (request shape, parsing, errors, key handling) and the current SDK's real types, but a
  live call needs your key / a running FreeLLMAPI.
* An 8B local model is capable but not perfect: expect occasional wrong-id attempts (the loop recovers) and slower steps (~2–8 s each); hard or
  long tasks are where `LOCAL_FIRST` escalates to Gemini once a key exists.
* The agent acts through untrusted page events (Phase 1 limits still apply: no trusted-input-only widgets, JS dialogs need you, etc.).
* One task at a time. Long-term memory, accounts, and site-specific workflows are intentionally absent.

> Phase 2.1 (voice input in the side panel, documented in `PHASE2_1_VOICE.md`) was added afterwards as an input method only; nothing in this document changed for it.

## Status at the close of Phase 2

**Validated live (2026-10-07):** local Ollama (`qwen3:8b`), Gemini (`gemini-3.5-flash-lite`), and FreeLLMAPI (source install in `C:\Tools\freellmapi`, loopback only;
upstreams Groq + NVIDIA). A real-Chrome task under the default `LOCAL_FIRST` policy ("Marie Curie's Nobel Prizes", no provider pinned) completed in 21.7 s with 7 model
calls: local 5, Gemini 2, FreeLLMAPI 0, no failures or retries, answer correct.

**Tests:** 208 unit tests; Phase 1 end-to-end 24/24 (plus the 2 MV3 idle/kill resilience cases); AI end-to-end 11/11 — each on Playwright Chromium and on the installed
Chrome 155 (fresh profile, extension loaded through the CDP pipe). Opt-in live-model cases: `python -m tests.e2e.run_ai_e2e --real-chrome --live-only`.

**Operating notes learned on the way**
* FreeLLMAPI's *free* limits are much lower than its catalog states (Groq's `qwen3.8-27b` enforced ~7k input tokens/min; one OCTG step is ~3–6k tokens), and several upstream
  routes are unreliable for tool calls (`gpt-oss-20b` emitted malformed calls; some NVIDIA reasoning models spent the whole output cap or hung). Bad routes were disabled in
  the dashboard (they live in the Default profile; list-page toggles need *Save changes*). A task **pinned** to FreeLLMAPI therefore cannot sustain ten-plus calls per
  minute; the default `LOCAL_FIRST` policy is the intended way to use it (Gemini sits ahead of FreeLLMAPI in `strong_order`, so FreeLLMAPI is the overflow).
* The OpenAI-compatible adapter retries **once** for FreeLLMAPI only, on HTTP 429/5xx or a malformed-tool-call 400 (never on other 4xx, never after a reply was produced, so a browser
  action can never be repeated); the retry is logged by reason, without prompts or keys.
* After changing extension code, reload it at `chrome://extensions`; after changing agent code, restart `python -m octg_agent serve`.

**Deliberately deferred:** a loop-level wait/back-off when every free route is rate-limited, extra upstreams, `brain.json` tuning, long-term/cross-task memory, site- or
account-specific skills, multi-task scheduling, and everything in Phase 3.
