# Phase 3 — Client Brain foundation (implemented), real-data onboarding, and the SEO-Intelligence extension points

This extends [PHASE3.md](PHASE3.md) (the Business Brain) without changing its architecture or security boundaries. Same store, same DPAPI protection, same tokens, same approval gate, same
logging protection, same document controls; one new dimension: **scope**.

```
BUSINESS BRAIN  (feature flag, off by default)
├── My business            scope "me"        OCTG's own profile            catalog: schema.CATALOG
├── Clients                scope = client id  each client, independently   catalog: schema.CLIENT_CATALOG
└── Documents / Sources / Activity            every document, folder and audit row belongs to exactly one scope
Task context  (TaskState, memory only)  +  research observations (future, per client)  — never merged into the above
```

## Scope: how clients stay apart
* **Storage.** `facts.scope` is `me` or a client id; every read and write names its scope (`store.get_value(key, scope)`, `status`, `list_fields`, `resolve`, `put_candidate(..., scope=)`).
  There is **no call that reads across scopes**, so a client's value can never be returned for another client or for the owner. Documents (`documents.client_id`) and enrolled folders
  (`enrolled_folders.client_id`) carry the same scope; a folder belongs to exactly one scope; what a document says becomes a candidate **for its folder's owner only**.
  Audit rows carry the scope too (`business audit --client C`). Clients live in `clients(client_id, name, archived)`; a client's display name is not a model-visible value.
* **A task is for one owner.** The reader (`BrainReader`) is created with one scope. A client task offers only `{{client:KEY}}` tokens and only that client's keys; the owner's own details and every other
  client are not merely hidden, they are unreachable (the reader has no handle to any other scope). `{{biz:…}}` in a client task and `{{client:…}}` in an owner task are refused with an explanation. The
  model never names a client id: the token has no client in it.
* **Which client?** In order: the user's explicit choice (`task start --client`, the panel's Client box, `POST /api/tasks` `client`); else the wording **only if exactly one client's name, trade name or web
  address matches** (`resolver.pick_client`, deterministic, generic words and company suffixes never decide); else the task **asks** (`WAITING_FOR_USER`, kind `client_needed`) and the user selects
  (panel picker / `python -m octg_agent task select-client <client>` / `POST /api/tasks/{id}/client`) then resumes. Several matches are listed to the user, never to the model. The client of a task cannot be
  changed once something was filled in. Archived clients cannot be picked or written to.
* **Minimum necessary context.** A client task's manifest lists keys and statuses only for what the current page asks about (plus keys the wording names, e.g. “target keyword”). No values; HIGH values never
  enter any prompt (client GSTIN/PAN/Udyam/internal notes are HIGH and DPAPI-protected like the owner's).
* **Approvals.** HIGH client values ask once per (site, field, task) and the card names the client (`… of client "Example Roofing LLC" on https://…`) so a wrong client is caught by a human; submitting after
  filling asks and lists what was entered; uploads keep the existing upload approval with the client named.
* **Logging.** Resolved client values are registered with the same per-task secret registry as the owner's; nothing private appears in action logs, activity, history, audit or errors.

> Bulk entry of the owner's profile and of clients (with services, locations and SEO targets) now goes through the spreadsheet importer: see [PHASE3_SPREADSHEET.md](PHASE3_SPREADSHEET.md).

## Client schema (`schema.CLIENT_CATALOG`, data not code; nothing is required)
| group | fields (repeatable `[]`) | sensitivity |
|---|---|---|
| identity | business_name, legal_name, trade_name, description | MEDIUM / LOW |
| contact | website, email, phone, person | LOW / MEDIUM |
| location | address (street, line 2, city, state/province, country, ZIP/postal code — stored as one unit) | MEDIUM |
| service_area | cities[], towns[], counties[], regions[], other[] | LOW |
| services | primary[], secondary[], categories[] | LOW |
| local_seo | gbp_reference, primary_category, secondary_categories[], target_locations[] | MEDIUM / LOW |
| seo | target_keywords[], target_services[], target_locations[], goals[], current_status | LOW |
| marketing | social_profiles[], directories[] (citations), content_notes | LOW / MEDIUM |
| account | status, onboarding_date, plan, internal_notes (HIGH), authorized_users[] | LOW / MEDIUM / HIGH |
| identifiers | taxation.pan / gstin / tan, registration.cin / udyam | HIGH |

Missing information is `MISSING` (never guessed, never copied from another field or scope). Conflicts surface exactly as for the owner. `registration.udyam` was also added to the owner's catalog.
Still absent: banking, Aadhaar, credentials of any kind.

## Using it
```
python -m octg_agent business enable                                      # then restart: python -m octg_agent serve  (and reload the extension at chrome://extensions)
python -m octg_agent business onboard                                     # YOUR business: asks field by field; Enter = skip; HIGH fields hidden; nothing guessed
python -m octg_agent business missing                                     # what is still empty
python -m octg_agent business client create "Client Name"                 # prints the client id
python -m octg_agent business onboard --client "Client Name"              # that client's details (or: business set <field> --client ...)
python -m octg_agent business docs enroll "D:\Docs\ClientName" --client "Client Name"   # only after: fs allow "D:\Docs\ClientName"; then docs scan / index / approve
python -m octg_agent task start "Fill in the selected client's business information." --client "Client Name"
```
In the side panel: **Business Brain → Clients** (list, profile, documents, sources, SEO, activity — read, confirm, reject, resolve, reveal, approve; creating/editing stays in the terminal), the **Client** box next
to the Task box (Auto / My own business / each client), and a picker on the waiting card when a task asks which client.

## Future SEO Intelligence — architecture only (no research engine exists)
Tables `research_sessions` and `research_observations` exist (empty) with `business/research.py` (`ResearchLog`): sessions belong to exactly one client; observations record **query, location, search engine,
result type, position, URL/domain/business, observed_at, evidence, source** inside a session and are **append-only** (SQLite triggers refuse UPDATE/DELETE), so rankings can be compared over time:
`history(client, query, location, who)` returns the dated observations oldest-first. A ranking observation without query + location + engine is refused. `ResearchLog.describe()` is the only sanctioned
sentence and always reads “*X was observed at position #n for the query 'q' in location 'L' on <engine> during research session S at <time>*” — never “X is #1”. The module imports no network, browser or model code.

How the future workflow plugs in (nothing below is built): (1) select the client (above); (2) a **research context** channel — LOW client fields (keywords, target locations, services) offered to a **local** model,
the same Class-1 `local_only` mechanism used for the owner's enum facts; (3) the generic browser actions search and read pages; their results are written as observations in a session; (4) competitor evidence
is more observations (`kind=competitor|citation|review|technical|content`); (5) findings/recommendations go in the session record; (6) comparisons use `history()`. No SEO-specific browser action and no site-specific
logic is added: the agent stays website-agnostic.

## Junction / reparse-point fix (`files/authorization.py`)
Gap found: on Python 3.11, `os.walk(followlinks=False)` **enters Windows junctions**, so `find()` (and anything built on it) listed file names that live outside the authorized folder through a junction placed
inside it (demonstrated: a junction to a folder outside the root made `secret.txt` appear). Fix: `is_reparse_point()` (symlinks and every Windows reparse point) and `safe_walk()` (never enters or yields a reparse point,
never leaves the real root, reports what it skipped); `find()` and the document scanner use it; `read_for_upload` re-resolves after reading and refuses if the real path changed; direct paths are still accepted only
when their final real path is inside a root (covers nested links, chains, `..`, sibling-prefix names, mixed case). 18 deterministic tests (`tests/test_files_reparse.py`) build real junctions.
Remaining limit: hard links to outside files cannot be detected from a path.

## Chrome 155 Phase 1 flakiness: cause and handling
Symptom: on the installed Chrome (headed, fresh profile) the screenshot case (12) and the find_text scroll-event case (26) failed intermittently (~40% of full runs), never on Chromium.
Instrumented runs (probe of `document.visibilityState` and animation-frame rate in the test tab before each case): every failing run had `visibilityState: "hidden"` and **0 animation frames** in the test tab
(`hasFocus` still true); every passing run had `visible` and ~38 frames per 600 ms. A hidden page produces no frames, so `captureVisibleTab` cannot read back an image and frame-driven page events (the inner box's
`scroll` event) never fire — the product code is not involved (the page is already hidden when the case starts). Chrome marks a headed window hidden when native window-occlusion tracking decides other windows cover it
(this machine is in daily use). Disabling that tracking removed the failures: without it roughly a third of full runs failed (2 of 5 and 1 of 8 in two instrumented series, plus 4 of the 6 manual runs that looked at it); with
`--disable-features=CalculateNativeWinOcclusion` 0 failures in 12 experiment runs (4 with that flag alone, 4 with it plus `--disable-backgrounding-occluded-windows`, 4 with the renderer-backgrounding/timer-throttling flags), and 5 of 5
full Phase 1 runs on Chrome 155 were 24/24 after the harness change (plus 26/26 with the MV3 resilience cases). Headless-new Chrome failed too (3 of 5), consistent with hidden-page throttling rather than a window effect only. **Classification: environmental (window visibility/compositing), not a timing bug in the
tests and not a product regression.** Handling: the real-Chrome test launcher now passes `--disable-features=CalculateNativeWinOcclusion` (test harness only, `tests/e2e/env.py`); no assertion was weakened and no
product code changed. Product note: an agent action that needs rendering (screenshot, frame-driven page effects) cannot succeed in a hidden window; a clearer error for that case is a possible later improvement.

## Tests added in this stage
`test_business_clients` 61 (clients, schema, isolation, resolver scope, name→client picking, client documents, onboarding, v1→v2 migration, research observations) · `test_business_clients_loop` 19
(client tasks through the real loop: exact values per client, no cross-client/owner data in prompts, logs, history, audit; asking which client; HIGH approvals naming the client; documents) ·
`test_files_reparse` 18 · `test_business_ui` +8 (client panel commands, scoped views, no create/edit commands) · JS `biz_ui` +7 (Clients tab) · `test_sensitive` (Udyam) · AI e2e 17–18 (real Chrome).
