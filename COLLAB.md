# OCTG AI Collaboration System (Cloudy ⇄ local agent)

Cloudy (the coding/reasoning AI, today Claude Code) delegates work to this local agent through a **capability-based, authenticated, persistent request/response
protocol**, gets structured results with evidence back, and continues. SEO research is the first application; the protocol knows nothing about SEO.

```
 Owner ──natural language──► Cloudy (plans, reasons, codes, deploys via its own hosting tools)
                               │  python -m octg_agent collab ... / CollabClient (HTTP, loopback, Bearer collaborator token)
                               ▼
              /api/collab/*   (agent :8765)  ── capability registry ── job runner ── collab.db (jobs, evidence, artifacts, approvals, audit)
                               │                         │
                 Knowledge Hub (business.db: clients,    ├─ web.*      guarded HTTP fetcher (public addresses only)
                 facts, campaigns, research sessions)    ├─ browser.*  own background tab in the owner's Chrome (read-only)
                               ▲                         ├─ search.*   search-results pages, CAPTCHA/consent reported not bypassed
 Owner ──Hub "AI & Approvals"──┘ (only place, with the   ├─ seo.*      campaigns, SERP observations, audit, competitors, report
          TTY CLI, where approvals are decided)          └─ handoff.*, change.*, verify.page, approval.request (coordination)
```

## 1. Why this transport (verified, not assumed)

| Question | Verified answer (2026-10-09) |
|---|---|
| Where does Cloudy run? | Claude Code desktop app on the same Windows machine; its Bash tool executes locally. |
| Can it reach `127.0.0.1:8765`? | Yes: `curl http://127.0.0.1:8765/healthz` returned `{"ok": true}` from Cloudy's shell. |
| Is a public tunnel needed? | No. Nothing new listens beyond loopback; the agent still refuses non-loopback binds. |

So the chosen design is option 2 of the brief: **a restricted, authenticated loopback API** plus a stdlib client/CLI. If Cloudy ever runs remotely (cloud session),
the same protocol works over an *outbound* bridge: the agent would poll a queue for `submit` payloads and post `response` objects back. The job runner, registry,
store and response format do not change (only the transport adapter), because requests are already persisted jobs addressed by `request_id`.

## 2. Protocol `octg-collab/1`

**Request** (`POST /api/collab/requests`):

| Field | Meaning |
|---|---|
| `request_id` | optional, caller-chosen (4-64 chars). Re-sending the same id returns the existing job: **never a second execution**. |
| `task_id` | parent collaboration task (`POST /api/collab/tasks`). Jobs of a task may only touch that task's client. |
| `client` | client id or exact name. Required for client-scoped capabilities unless the task has a client. Never a default client. |
| `capability` | registered name, e.g. `web.fetch_page`. |
| `parameters` | object, validated against the capability's JSON schema (`additionalProperties: false`). |
| `required_permissions` | optional declaration; must cover the capability's permissions and be within the caller's grants (no silent escalation). |
| `expected_format` | `json` (default), `markdown`, `json+markdown`. |
| `timeout_s` | capped by the capability's `max_timeout_s`. |
| `idempotency_key` | per caller; same key + same payload = same job; different payload = `409 IDEMPOTENCY_CONFLICT`. |
| `priority` | 1 (most urgent) – 9. |
| `created_at`, `protocol` | recorded / checked. |

**Response** (every key always present): `protocol, request_id, task_id, capability, client_id, status (queued | awaiting_approval | running | succeeded | failed |
timed_out | cancelled), result, evidence[] {evidence_id, kind, source_url, collected_at, sha256, bytes, summary}, sources[] {source, url, collected_at},
completeness (complete | partial | none), confidence, limitations[], error {code, message, retryable, details}, retryable, approval_id, progress, attempts,
created_at, started_at, completed_at, duration_ms`.

**Long-running jobs**: `GET /api/collab/requests/{id}?wait=N` long-polls (≤ 55 s per call); `progress` is persisted; `POST .../cancel` cancels queued or running
jobs. **Restart recovery**: on start, interrupted *read-only* jobs are re-queued (max 2 attempts); interrupted jobs that may have side effects are failed with
`INTERRUPTED` (never blindly retried). Queued jobs resume.

Other endpoints: `GET /whoami`, `GET /capabilities[/{name}]`, `POST/GET /tasks`, `GET /tasks/{id}` (jobs, artifacts, approvals), `POST /tasks/{id}/finish`,
`GET /evidence/{id}` (content + hash check), `GET /artifacts/{id}`, `GET /approvals`, `GET /audit?task_id=|request_id=`.

## 3. Capabilities (discoverable at `GET /api/collab/capabilities`)

Each describes purpose, input schema, output, permissions, client scope, read-only, external effects, approval rule, timeouts, cancellation, browser need and
known limitations. Adding a tool = one `@capability(...)` handler in `collab/caps/` (and its module in `caps/__init__.py`); nothing else changes.

| Group | Capabilities |
|---|---|
| agent / clients | `agent.status`, `clients.list`, `client.context` (verified + flagged-unverified LOW/MEDIUM facts, services, locations, SEO targets; HIGH fields withheld by key) |
| web (HTTP) | `web.fetch_page`, `web.robots`, `web.sitemap`, `web.crawl_site` |
| browser (read-only, own tab) | `browser.inspect_page` (rendered title/headings/text, find-in-page, optional screenshot), `search.web` |
| SEO | `seo.campaign_upsert`, `seo.campaign_get`, `seo.serp_observe`, `seo.audit_site`, `seo.competitors`, `seo.report`, `connectors.status` |
| coordination | `artifact.record`, `artifact.get`, `target.register`, `handoff.create`, `handoff.get`, `approval.request`, `approval.status`, `change.record`, `change.mark_deployed`, `verify.page` |
| not granted by default | `agent.browser_task` (hand an instruction to the agent's own LLM loop; its approval gate still applies) |

Deliberately **not** exposed: raw engine actions (click/type/upload/close arbitrary tabs), file-system paths, shell, Python.

## 4. Security model

* **Authentication**: per-caller random token (`data/collab_<caller>.token`); only its SHA-256 is stored (`data/collab.json`). The agent's own API token is *not*
  accepted on `/api/collab/*`, and a collaborator token is *not* accepted on any other `/api` route (so it can never drive the raw browser API).
* **Loopback + anti-browser**: same guard as the rest of `/api` (loopback Host, any `Origin` header refused).
* **Permissions**: caller grants (`collab token list`) vs. capability permissions; requests can only narrow, never widen.
* **Scope**: client resolution is exact (id/exact name/ref); ambiguous → `CLIENT_AMBIGUOUS` with candidates; a task's jobs are locked to its client
  (`SCOPE_MISMATCH`); callers see only their own tasks, jobs, evidence, artifacts.
* **Secrets**: requests containing credential-, card-, PAN/GSTIN- or ID-shaped strings are refused (`SECRET_IN_REQUEST`); stored text is scrubbed; HIGH facts never leave.
* **SSRF**: the HTTP fetcher refuses non-http(s), embedded credentials, non-web ports and any host resolving to loopback/private/link-local/reserved addresses —
  checked before the request **and** inside aiohttp's resolver at connect time (closes DNS rebinding), and again on every redirect hop.
* **Untrusted content**: page text, search results and documents are data. Capabilities never follow instructions found in them; nothing a page says can change
  permissions or approvals.
* **Approvals** (`approval.request` → owner decides in Hub → `change.mark_deployed` → `verify.page`): the protocol has **no** route that approves. The owner decides in
  the Knowledge Hub ("AI & Approvals", session + CSRF protected) or with `python -m octg_agent collab approve <id>` at an interactive terminal (typed `YES`,
  refused when stdin is not a TTY). Honest boundary: Cloudy runs with shell access on the same machine, so the ultimate guard is Claude Code's own permission
  prompts plus Cloudy's operating rule never to approve its own requests; the protocol guarantees the AI-driven path cannot self-approve.
* **States are distinct**: PROPOSED (approval requested / handoff) → APPROVED (owner) → EXECUTED (deploy reported with hosting result; refused unless approved)
  → VERIFIED (this agent fetched the live page and every check passed).

## 5. SEO workflow on top of the protocol

1. `client.context` → verified business context (no re-explaining).
2. `seo.campaign_upsert` → campaign in business.db (`seo_campaigns`, `seo_campaign_keywords`, `seo_campaign_runs`): objective, language/country, services,
   locations, bulk keywords (deduplicated case/space-insensitively; locations checked against the client's known service areas → `outside` needs owner confirmation).
3. `seo.serp_observe` → one research session; per keyword: `keyword_rank` (client position or absence), every organic `serp_result` classified
   client/competitor/directory, `local_pack_result` when a map pack appears. First successful run sets the campaign baseline. Observations are append-only.
4. `seo.audit_site` → crawl + robots/sitemap + http/https/www variants + deterministic rules (`seo/audit.py`) → findings typed defect / opportunity / hypothesis,
   with evidence, severity, confidence, recommendation, verification method, effort, value and priority score → `finding_set` artifact.
5. `seo.competitors` → competitor landing pages actually observed in the SERP session, fetched and compared (correlation only, no copying).
6. `connectors.status` + `seo.report` → Markdown + JSON report (executive summary, client/campaign, data sources, baseline, keyword observations, competitor
   findings, technical issues, prioritised recommendations, missing data, implementation tasks, success criteria) stored as a `report` artifact and as files
   under `data/collab/reports/<client>/`.

**Data sources**: Google Search Console, Bing Webmaster Tools and GA4 are **not connected** (no credentials, no API client yet). `connectors.status` says so and the
report lists it under Missing data. Nothing implies they were queried.

## 6. Research → code → deploy → verify (Phases C–E)

`target.register` (client + domain + environment + local source folder + hosting + deploy/rollback method; domain cross-checked with the Knowledge Hub) →
`handoff.create` (package: client/domain/target, verified context, selected findings with evidence ids, pages, candidate source files, proposed changes,
constraints, acceptance criteria, test requirements, approval requirements, rollback) → Cloudy implements and tests → `change.record` (files, diff, tests;
failing tests can't be `ready_to_deploy`) → `approval.request production_deploy` → owner approves → Cloudy deploys through its hosting integration →
`change.mark_deployed` (refused unless approved; records the hosting result) → `verify.page` (live checks; records a `verification` artifact, marks the change
verified/failed, moves the approval to VERIFIED, sets a follow-up date, default +28 days). A passing technical check is never reported as a ranking improvement.

## 7. Using it (Cloudy)

```
python -m octg_agent collab whoami
python -m octg_agent collab caps
python -m octg_agent collab task new "SEO audit" --client "Example Cigar Co."
python -m octg_agent collab run client.context --task ct_xxx --wait 60
python -m octg_agent collab run seo.audit_site max_pages=30 --task ct_xxx --wait 600 --out audit.json
python -m octg_agent collab artifact ar_xxx --markdown --out report.md
python -m octg_agent collab task finish ct_xxx --status completed --outcome "..."
```
Python: `from octg_agent.collab.client import CollabClient; c = CollabClient(); r = c.run("web.fetch_page", {"url": "..."})`.

Owner: `python -m octg_agent collab approvals`, `... approve <id>`, `... deny <id>`, `... token list|rotate|disable`.

## 8. Tradeoffs and known limits

* Loopback-only transport (no remote Cloudy without the outbound bridge adapter described in §1).
* SERP parsing uses only the generic engine output (headings + links). Positions are "what this browser was shown"; Google may ignore the `uule` location;
  local-pack *order* is not measured. CAPTCHA/consent → `SEARCH_BLOCKED`, never bypassed.
* The audit reads raw HTML (crawler view); JavaScript-only content is not seen by `web.*` (use `browser.inspect_page`).
* GSC / Bing / GA4 connectors: status only; API clients are a future capability (credentials → `data/connectors/`).
* Scheduled follow-ups: the follow-up date is recorded on each verification; running them on a schedule is a future capability, not a protocol change.

## 9. What live use changed (2026-10-09) — honesty rules the code now enforces

| Found in live use | Rule now in code |
|---|---|
| A Hostinger CDN answered the agent's HTTP client with a 403 "Checking your browser" page; the first audit turned that into 17 fake defects. | `FetchResult.challenge` detects bot-protection pages; a challenged crawl produces **no findings**. `seo.audit_site` / `verify.page` `mode=auto` fall back to reading the site in the owner's real Chrome (ordinary browsing, nothing solved or bypassed); what a browser cannot see (meta description, canonical, robots meta, JSON-LD, alt text, status codes) is listed as **not measured**, never failed or guessed. |
| Bing from this browser's location ignored "Buffalo" and its wrapped links were truncated, giving 0 parsed results that were recorded as "client not in top 0". | An empty/unreadable results page is `NO_RESULTS_PARSED`: no ranking observation, no baseline. Wrapped links fall back to the domain label (`url_inferred`). |
| Rules reported three "This Page Does Not Exist" pages as duplicate titles, and 33 long titles as 33 findings. | Soft-404 detection (`broken_internal_links`, with the linking page), per-rule grouping, `weakly_linked_pages` vs. siblings. |
| A competitor run with 7/8 fetch failures called itself "complete". | Completeness/confidence follow failures; small samples are flagged anecdotal. |
| Wrong research must not be silently deleted. | `artifact.invalidate` / `research.invalidate_session`: kept with the reason, excluded from reports and handoffs. |
| Some checks can't run before deploy. | `change.record` accepts `{skipped: true, detail}` (reason required, at least one real test); skipped tests travel with the approval request. |
| Some sites act on GET (reset_password.php, install_db.php). | The crawler never requests reset/install/login/admin/cart/token-like paths, whatever links say. |
| Domain lists must not live in code. | `seo/domains.json` (engine hosts, consent hosts, directories). |

## 10. The loop controller (2026-10-09): research → plan → implement → wait → verify → decide, round after round

`agent/octg_agent/collab/loops.py` (generic controller), `loop_kinds.py` (what each kind measures), `caps/loops.py` (the capabilities), Hub card **AI & Approvals → Improvement loops**
(`hub/static/v_loops.js`, routes `/hub/api/loops`). Tests: `tests/test_collab_loops.py` (30), `tests/test_hub_browser.py::LoopViewTests`.

**What it is.** A durable state machine for ONE client. It is not SEO-specific: a *loop kind* says what to measure, how to turn answers into comparable observations and when the goal is met.
`seo_visibility` is the first kind (organic position and map-pack presence per keyword + the site audit); `page_checks` (a live page must satisfy explicit checks) proves it is general. A new
workflow = a new `LoopKind` + `register_kind`; the store, protocol, scheduler, history and Hub card do not change. External data sources plug in the same way (see below).

**Who does what.**
| | does |
|---|---|
| controller (no model, no browser held open) | baseline + every re-measurement, requested through the existing collab capabilities **with the creator's own grants**; comparison; goal check; scheduling; retries; budget; history |
| collaborator (Cloudy) | reads `loop.get` (it includes `for_planning`), submits a plan (`loop.submit_plan`), implements it with the existing handoff → change → approval → deploy workflow, reports it (`loop.report_implemented`) |
| owner | approves production deploys (**PIN**), may pause / resume / stop / check now in the Hub |

**Phases** (`status` active | paused | blocked | goal_reached | stopped | exhausted; the last three are final): `measuring` → `awaiting_plan` → `awaiting_implementation` → `waiting` (a stored
`next_check_at`; nothing runs, no model or browser session is kept) → `measuring` → … The scheduler tick (`OCTG_LOOP_TICK_S`, default 20 s) resumes what is due, also after an agent restart (all state is in `collab.db`).

**Capabilities.** `loop.kinds`, `loop.create`, `loop.get`, `loop.list`, `loop.advance`, `loop.submit_plan`, `loop.report_implemented`, `loop.control` (pause | resume | stop | check_now). All are scoped to
ONE explicit client and to loops the calling identity created (another client's or caller's loop is "no such loop"). There is deliberately no approve/deploy capability.

**Goal and stop conditions** (`goal`): `targets[]` (`keyword`, `location?`, `search_type` organic | local_pack, `max_position` 10 = first page), `confirmations` (default 2), `require` all | any, and at least one of
`max_rounds`, `deadline`, `max_requests` (a request budget, checked *before* each measurement). A manual stop is always possible.
*Reaching page one is never a guess:* a target counts only with `confirmations` consecutive **comparable** observations (same engine, search type, country, language, location) at least `min_gap_hours` apart;
one good observation only schedules a confirmation check; a comparable miss resets the streak; `volatile` is flagged when positions swing (> 5) or flip present/absent repeatedly.

**Honest data.** Each observation is `found` (a value), `not_found` (measured and absent: a negative RESULT, e.g. "not among the 10 results read") or `unavailable` (CAPTCHA, unreadable page, failed job, browser off: NOT a result,
never a ranking, never a change, kept as history). No usable data → bounded retries with back-off (`max_attempts`, `retry_backoff_s`) → status `blocked` with the exact blocker; the owner resumes it. A move of ≤ 2 positions is labelled
"within ordinary variation". Data sources that are not connected (Search Console, Bing Webmaster: no API client exists yet) are reported as **not connected** on every round; a capability named in `PLUGGABLE_SOURCES`
(`gsc.search_analytics`, `bing.webmaster_stats`) returning `{"observations": [...]}` is requested automatically once it is registered.

**Learning from rounds.** Each round records its plan, the changes reported, a before/after comparison against the baseline AND the previous observation, and the effect of each planned item
(improved / no_improvement / worse / unknown). A plan item identical to one already implemented (same action, pages and finding) is refused with `REPEAT_CHANGE` unless an `override_reason` (20+ characters) is given;
wait-only rounds are allowed but limited (`max_waits`); an empty plan is refused.

**Approvals are never bypassed.** `loop.report_implemented` only accepts changes recorded as `deployed`/`verified`, and a production change additionally needs an `executed` owner approval
(`change.mark_deployed` only works after the PIN-approved `production_deploy`). The controller approves nothing and has no way to.

**History.** Append-only `loop_events` (triggers forbid update/delete) + `loop_rounds` + `loop_observations`, linked to a parent collab task, plus the normal collab audit. Hub: state, who/what it waits for, goal progress,
before/after table, rounds, data sources, history, and the owner's Pause / Resume / Check now / Stop.

**Limits (honest).** A loop's research runs in the owner's Chrome (positions are what that browser was shown, not universal rankings). `observed_at` comes from the measurement, so scheduling and the confirmation gap assume
real time. Competitor review is not a loop step yet (use `seo.competitors`, then plan). GSC/Bing/GA4 are not connected.

## 11. Data sources, competitor research and the read-only audit (2026-10-09)

Code: `agent/octg_agent/connectors/` (`http.py`, `gsc.py`, `bing.py`, `hub.py`, `oauth.py`, `providers.json`), capabilities `caps/sources.py`, `caps/competitors.py`, `caps/audit.py`; Hub card **AI & Approvals → Data sources**
(`hub/static/v_sources.js`); terminal `python -m octg_agent connectors status | verify | gsc-login | map`. Tests: `tests/test_connectors.py` (68), `tests/connector_fakes.py`. Owner runbook: `FIRST_AUDIT.md`.

**Credentials** are files the owner puts in `agent/data/connectors/`: `gsc_service_account.json` (service-account key) or `gsc_oauth_token.json` (from `connectors gsc-login`), and `bing_webmaster_api_key.txt`.
They are read from disk when needed, never stored in a database, never returned, scrubbed from every error, never in a recorded URL. Provider names, API hosts and the owner's setup steps are data (`providers.json`).

**Who may use what.** A new permission `connector:read` (the standard collaborator gets it once; a deliberately restricted caller does not). The collaborator can only ask for "this client's" data: it cannot name a property,
list the credential's other properties, or create a mapping. `connectors.suggest_mapping` shows only the properties whose domain matches the client's own website.

**Client-to-property mapping** is the owner's decision (Hub with PIN, or the terminal with a typed YES). A property belongs to exactly one client (a mapped property can't be given to a second client, not even with the owner's
"domain differs" override); a property whose domain differs from the client's websites needs that explicit override. Before EVERY use the credential's current view of its properties is checked (cached 5 min): a revoked or
downgraded permission is `UNAUTHORIZED_PROPERTY`, recorded on the mapping, never ignored.

**Named outcomes** (never "no data" or "not ranking"): `NOT_CONNECTED` (no/invalid credentials: says exactly which file goes where), `NOT_MAPPED`, `UNAUTHORIZED_PROPERTY`, `AUTH_FAILED` (revoked/expired), `API_DISABLED`,
`RATE_LIMITED` / `API_ERROR` / `API_UNREACHABLE` (retryable), `BAD_REQUEST`, `PROPERTY_NOT_FOUND`. The HTTP layer (fixed https hosts only, no redirects, size cap, bounded retries with Retry-After honoured and capped) is
tested for real against a local server. A query that Search Console/Bing returns no row for is `unavailable` ("too rare to be reported, not proof of absence"); an empty window is reported as such.

**Capabilities.** `connectors.status` (offline; per client with `client`), `connectors.verify` (one live call; proves the stored credentials, not a cached token), `connectors.suggest_mapping`,
`gsc.search_analytics` (totals, rows, per-keyword figures, standard observations, evidence, research-log records), `gsc.sitemaps`, `gsc.inspect_url` (only URLs on the mapped property), `bing.webmaster_stats`,
`research.competitors` (any URLs/domains, with or without a client; HTTP first, the owner's real Chrome when a site refuses scripts or can't be reached; optional bounded whole-site read; never returns competitor wording;
"not measured" fields for what a browser read cannot see), `seo.audit_preflight`, `seo.start_baseline_audit`.

**In the loop controller (nothing duplicated).** `seo_visibility` requests `gsc.search_analytics` / `bing.webmaster_stats` only when the source is ready FOR THIS CLIENT (credentials + mapping) and its creator holds
`connector:read`; competitor research and the report run as follow-up stages after the search observations (a small additive `followups` hook; scheduling, approvals, tasks and history are the existing ones). Higher-is-better
metrics (clicks, impressions) compare in the right direction; where a first-party average and one browser's observation disagree by more than 5 positions, the loop says so. A goal's measurement is usable only if a goal TARGET
was measured: supporting data alone never advances a loop. `goal.baseline_only` ends the loop as **completed** after the baseline (the read-only audit).

**The read-only audit.** `seo.audit_preflight` lists what is ready and the exact owner actions; `seo.start_baseline_audit` refuses on a blocking problem and otherwise starts the loop: search observations, site audit,
GSC/Bing data where ready, competitor profiles, prioritised report; then it ends by itself. It writes research records only.

**Report evidence labels (2026-10-10).** `seo.report` labels every campaign keyword: **fresh** (observed within 12 h of the report), **older** (valid and dated, shown with its own date and age) or **unavailable**
(no valid observation: a CAPTCHA wall that was not bypassed, an unreadable page, or never searched; the reason comes from the recorded `seo.serp_observe` results). It states keyword coverage as complete only when every keyword
is fresh, and otherwise adds a "Search coverage is incomplete" entry to Missing data. Observations are never overwritten: per keyword the report uses the newest valid session, older sessions stay as history, and a new
report links the one it supersedes. Competitor positions and query counts come from the recorded search results; a competitor the search data does not show is "unavailable", never `#None` or 0. Findings are labelled
"measured on site" or "inferred hypothesis". `research.competitors` with no URLs profiles the competitors seen in the MOST searches (then best position). With no target locations on file the location-in-title count is
"not counted", not "0 of n". Tests: `tests/test_seo_report_coverage.py`.

**`seo.rank_check` (2026-10-13; spec `RANKCHECK-BUILD-SPEC.md`, code `collab/caps/rank.py`, tests `tests/test_rank_check.py`).** A read-only, client-agnostic rank check from a keyword file (`octg.rankcheck.input/1`: site, brand_name,
locale.uule, per keyword a web_url and a map_url). Run it with `collab run seo.rank_check --json FILE --client NAME --wait 1800 --timeout 2400` (add `only=[...]` to finish a stopped run). Rules enforced in code: every URL is
opened byte for byte and is validated first (https://www.google.com/search, carries `locale.uule` verbatim, `tbm=lcl` only on the map URL, `q` equals the keyword; a bad file is refused before anything opens); the page that
actually loaded must still carry the uule, otherwise that page is `location_lost` and not used; 20-45 s random pause between every two pages (a constant, not a parameter); the first CAPTCHA/consent wall stops the run
(`status: blocked`, partial results kept, rest `not_run`, no retry, no other engine); `null` for anything not observed (never 0 or 11); a results page that cannot be read reliably records no position. Output
`octg.rankcheck.result/1` (JSON + Markdown files under `data/collab/reports/<client>/`, an artifact, research observations) with `previous_*` and `*_delta` against the newest earlier run at the SAME uule (positive delta = moved up).
No approval is needed (nothing leaves the machine and nothing changes). Local-finder names/ratings/review counts are read from the page text; a count that cannot be read stays null. First live check (Example Painting Co): location
verified, map position and competitor reviews read correctly; the spec's "control" organic #1 was NOT reproduced (the page itself, text and links, contains no example-painting.test), so that expectation needs the owner's confirmation.

**Ask the agent (2026-10-13; Hub page `#/ask`, `hub/static/v_ask.js`; routes `POST/GET /hub/api/asks`, `POST /asks/{id}/cancel`; sentence parser `collab/intent.py`; capability `search.report` in `collab/caps/searchreport.py`; tests `tests/test_ask.py` + `AskViewTests`).**
The owner types a plain sentence ("go to Google and search for X, search for Y ... tell me what you find"), picks the client and presses Send. The Hub parses the searches (rule-based; if none are found it replies with how to phrase it, nothing
is guessed), runs `search.report` as the caller `owner-ui` (grants limited to `search:read, research:write, client:read, collab:write`: nothing that can change a site) and shows the answer in plain language in the feed:
what was searched, whether the client's website is on the first page (position and URL), first results, competitors and directories on the page, map listings with ratings and review counts, and a dated snapshot note. The feed is the
agent's own record (a collab task per question), so it survives a reload. Safeguards: one run at a time (`BUSY`), 20-45 s between searches, the first CAPTCHA/consent wall stops the run, the answer says what to do and the page offers
"Try again" for exactly the searches that were not run. Not built yet: free-text tasks other than Google searches, file upload, and the Chrome side panel (the extension in the owner's Chrome is a stale build).

**AI -> owner: "go here, do this, tell me what you see" (2026-10-13; `collab/ownerio.py`, `collab/caps/owner.py`, `collab/panel_api.py`; Hub page `#/inbox`; Chrome side panel tabs Ask + Inbox in `extension/sidepanel/ask_ui.js`; tests `test_owner_inbox.py`, `test_panel_browser.py`, `InboxViewTests`, `tests/js/ask_ui.test.cjs`).**
The collaborator posts an owner request with `owner.ask` (title, 1-12 steps, what to report back, optional quick-answer buttons, optional "attach the tab" prompt, optional label). Permission `owner:message` (granted once to the standard collaborator).
The owner sees it as a card in the side panel's **Inbox** tab (a red count on the tab and on the toolbar icon, pushed by the agent as `{"type":"inbox","open":n}` and on every connect) and in the Hub **Inbox**. The owner answers with a quick answer
and/or text; in the side panel they may also press **Attach this tab's text and a screenshot** (runs only on that press, on the tab they are on; the text is scrubbed of card numbers, secrets and identifiers; the screenshot is not edited; what
was attached is shown before sending). The collaborator reads the answer with `owner.replies` (`id=...`, optionally `wait_s` up to 540 to wait for it; attachments carry the page text and the path of the screenshot to read), or withdraws with `owner.cancel`.
Rules: an answer is final (follow-up = a new request); only the asking caller can read the answer; replies are scrubbed of secret-looking values before they are stored; unanswered requests close after 14 days; nothing here can approve, deploy or change anything.
Side-panel channel: `ui` commands `ask_clients/ask_send/ask_list/ask_cancel` and `inbox_list/inbox_reply/inbox_dismiss/inbox_attach` -> `ui_result` with a `panel` payload (relayed once by the service worker, never stored). `inbox_attach` talks to the browser, so it
answers later from the background (the bridge loop must keep reading the extension's replies: a handler returning None means "I will answer later"). The panel cannot approve or deny anything (approvals need the PIN and stay in the Hub).
Ask the agent is in the side panel too (same code as the Hub page, `collab/asks.py`). **After pulling this change the owner must reload the extension once** (chrome://extensions -> OCTG Browser Agent -> reload); no new permissions are needed.

**The eye (2026-10-14; `collab/eye.py`, `collab/caps/eye.py`, permission `browser:act`, owner guide `EYE.md`, tests `tests/test_eye.py`).** The collaborator's way of looking at, and carefully using, the owner's Chrome without burning tokens: `eye.open / look / act / table / close / status`.
One dedicated tab per session; digests (cleaned text, tables as rows, a short controls list) and diffs instead of whole pages; a gate in the agent (refuses change-looking clicks, personal fields, non-search typing, blocked sites, >80 actions); the owner's switch and
site list in `data/eye.json` (`python -m octg_agent eye status|on|off|deny|allow`). Live result: all 211 Search Console queries read in 34 s through three calls. A partial label match is never pressed (it is offered as a candidate) because "Impressions" also matches the
"Total impressions" card.

**Honest limits.** Everything against Google and Bing in the tests is MOCKED (`tests/connector_fakes.py`); live connectivity is unproven until the owner adds credentials. Bing's error names are mapped from its documented
list; unknown ones become `API_ERROR` with Bing's own scrubbed message. GA4 is not implemented.
