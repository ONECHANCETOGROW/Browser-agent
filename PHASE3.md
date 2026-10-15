# Phase 3 — Business Brain (implemented v1, feature flag OFF by default)

Design and rationale: [PHASE3_DESIGN.md](PHASE3_DESIGN.md) (approved with the amendments listed at the end of this file). This file describes what exists.

**What it is:** a structured, user-approved source of business facts and authorized documents that the task loop can use while filling forms — without the model ever
seeing a business value. **What it is not:** conversational memory, a browser action, a second agent or a second approval system.

> **Stage 2 (real-data onboarding + Client Brain):** clients are a first-class scope next to the owner's own business, the Windows junction gap in the authorized-folder model is closed, and the Chrome 155
> test flakiness is diagnosed. See [PHASE3_CLIENTS.md](PHASE3_CLIENTS.md). Everything below still holds; `{{client:KEY}}` tokens, `--client`, the Clients tab and `business onboard` are the additions.

## Turn it on
```
python -m octg_agent business status
python -m octg_agent business set identity.legal_name "Example Growth LLC"       # HIGH fields are read with a hidden prompt
python -m octg_agent business set addresses.principal                              # asks for each part
python -m octg_agent business enable                                               # writes {"business": {"enabled": true}} into data/brain.json
python -m octg_agent serve                                                         # restart; reload nothing in Chrome unless the extension changed
```
Env switch for tests: `OCTG_BUSINESS=1`. With the flag off, `business.db` is never opened by the agent, the panel section and per-task switch stay hidden, tool
descriptions carry no token hint, and a model that types `{{biz:...}}` gets a refusal instead of the literal text reaching a page.

## Architecture
```
voice/text ─► task engine (brain/loop.py) ─► CONTEXT RESOLVER (business/resolver.py: BrainReader, read-only)
                                                 ├─ task context   TaskState (memory)
                                                 ├─ business profile   business.db › facts
                                                 └─ authorized documents  business.db › documents  (+ files in enrolled folders)
   the model sees:  field KEYS + statuses (never values)      the browser gets:  exact values, substituted by code in `_act`
```
* `business/` is a leaf package (imports only stdlib, `octg_agent.sensitive`, `files.authorization`); `engine/ bridge/ files/ reasoning/` never import it, and `brain/` imports only
  `business.resolver` (all enforced in `tests/test_business_ui.py › ArchitectureGuardTests`).
* `brain/business_glue.py` (`TaskBusiness`) is the only place the loop meets it: ledger, per-site approvals, observation sanitising, routing flag.
* Not touched: the browser action registry (hash-pinned by a test), `page_agent.js`, routing policies/weights/retry, the Gemini/Ollama/FreeLLMAPI adapters and their config, the disabled FreeLLMAPI routes, voice/STT, the task lifecycle, `tasks.db`'s schema, the extension manifest.

## Data model (`data/business.db`, SQLite, WAL)
| table | holds |
|---|---|
| `facts` | one row per CANDIDATE value: field key, instance, value (plain for LOW/MEDIUM, DPAPI blob for HIGH), display mask, keyed equality hash, sensitivity, state (LIVE/SUPERSEDED/REJECTED), verification (VERIFIED/UNVERIFIED), source_type (USER_ENTERED/USER_CONFIRMED/DOCUMENT/IMPORT), source_ref, doc_id, format_ok/note, confirmed_at, created/updated, supersedes |
| `documents` | doc_id, folder, relative path, ext, size, SHA-256, mtime, type, label, sensitivity, state (DISCOVERED/INDEXED/APPROVED/EXCLUDED/BLOCKED/MISSING), verification, source, extraction note, fields found |
| `enrolled_folders` | folders enrolled for the Business Brain (a subset of `authorized_folders.json`) |
| `audit` | append-only (SQLite triggers refuse UPDATE/DELETE); event, task, field/doc id, provider, locality, actor, outcome, short scrubbed detail — never a value |
| `schema_meta` | schema/catalog version, cipher name, DPAPI-protected keying material for the equality hashes |

Field status is derived from the candidates: `MISSING`, `VERIFIED` (agreeing candidates, one user-entered/confirmed), `UNVERIFIED`, `CONFLICTING` (never resolved automatically).
The catalog (`business/schema.py`) holds identity, contact, addresses (compound, stored as one unit), registration identifiers, tax identifiers, owners, services, social links and
`other.*` custom fields (HIGH by default). **No banking, no Aadhaar, no passwords/OTPs/keys/payment credentials** — those are refused, not stored.

Import format (`business import file.json [--assume-verified]`): `{"identity.legal_name": "Example Growth LLC", "addresses.principal": {"line1": "...", "pin": "..."}}` (or the same under a `"facts"` key).
Imported values are UNVERIFIED unless you vouch for them with `--assume-verified`; secrets are refused per entry.

## Token architecture
The model writes `{{biz:KEY}}` as the *entire* text of `type`, the option of `select`, and `{{doc:ID}}` as the file of `upload_file`. In `TaskRunner._act`, after the model's reply and before
risk assessment, `BrainReader.prepare()` turns the call into the real one: the exact stored string (never re-cased, trimmed or re-encoded), or a validated file path — or stops.
Composite text, tokens in any other parameter or in expectations, unknown keys, and tokens when the Business Brain is not active for the task are refused with feedback. A literal
identifier shaped like a PAN/GSTIN/IFSC/card that the user never gave the model is blocked. The engine's own read-back (`value_matches`) verifies what the page kept; a mismatch is handed
to the user, never "fixed" by the model. Missing / unverified / conflicting values stop the task with `WAITING_FOR_USER` (`missing_info`, `unverified`, `conflict`) and an
explanation that contains labels and source names only.

## What reaches a model
* The **manifest**: keys, labels and statuses for the fields the *current page* asks about (deterministic label matching, ≤ 12 entries); nothing for fields the page does not ask about.
* HIGH values: **never** — not to cloud, not to local. MEDIUM/LOW values: never (tokens). The one exception: the value of an **enum** fact (`identity.constitution`) may be shown to a **local**
  model, so it can answer a radio question; that call is `local_only` (router), and if no local model can answer, the value is withheld rather than sent to a cloud model.
* Screenshots: while the task has filled business details, an image goes only to a local vision model, otherwise it is dropped.
* Document **labels and counts**, never filenames or paths.
* Outbound filter on every prompt of a Business-Brain task (defence in depth): known resolved values become tokens, sensitive-labelled/shaped field values are hidden, identifier shapes masked.
* `provider_is_local()` (router) counts a provider as local only if it declares so **and** its address is loopback; FreeLLMAPI is cloud.
* Every model call of such a task writes a `prompt_context` audit row: provider, local/cloud, field keys offered, whether an enum value or image was included.

## DPAPI protection
`business/cipher.py`: `DpapiCipher` (Windows `CryptProtectData`, current user, application entropy, `pywin32`). Values with sensitivity ≥ HIGH are stored only as DPAPI blobs; masks are
stored in clear so lists never decrypt; the equality hashes are HMAC-keyed with a DPAPI-protected random pepper. A blob that cannot be decrypted (different Windows user, damage) is
reported as unavailable and the user is asked — nothing is guessed. Fallback where DPAPI is missing: `FernetFileCipher` (key file beside the data; reported as WEAK). Honest limit: it
stops a copied database, not malware running as you. MEDIUM/LOW values are stored in plain text by design (D1).

## General logging protection (not only Business Brain)
`octg_agent/sensitive.py` is a neutral module used by the dispatcher/log, task activity, history and error text:
* **Field-based**: a field whose own label/type/autocomplete says password, OTP, card/payment, PAN, GSTIN/tax id, bank/account, identity number or secret → the typed text and the page's `value_after` are never written to the action log.
* **Shape-based**: PAN, GSTIN, IFSC, card (Luhn) and 12-digit ID shapes are masked in any logged string, including error messages, task activity, history (`instruction`, `result`, `error`, step args/outcomes) and audit text.
* **Registry-based**: exact values the agent resolved from the Business Brain are registered per task and redacted wherever they would be written, whatever their shape (a synthetic `TEST-GSTIN-123` included); cleared when the task ends.
* `Dispatcher.execute(..., sensitive_call=True)` lets a caller declare a call private. Ordinary non-sensitive typed text is logged exactly as before.
* `TaskState.error/result/waiting_message/current_step` are scrubbed in every summary sent to the panel.

## Documents
Two-step consent, both CLI-only: `fs allow <dir>` (existing) then `business docs enroll <dir>`. Nothing is read until `business docs scan`; `index` extracts text in a
separate stdlib-only process (timeout, output cap, zip-ratio/entry caps, DOCTYPE/entity refusal, no network, secrets stripped from its environment) and derives **candidates only**
(format-valid PAN/GSTIN/CIN/TAN and a few labelled lines) — always `UNVERIFIED`. Supported: `.txt .csv .json .docx .xlsx`; `.pdf` text layer needs the optional `pypdf` (not installed);
images are registered but not read (no OCR). Banking/identity-card/credential files are `BLOCKED`; unknown documents default to HIGH. A changed file resets to DISCOVERED/UNVERIFIED and retires the facts
it produced. **At use** (`{{doc:ID}}`): the document must be APPROVED by the user; unique per type (several → ask); its folder must still be enrolled and authorized (`AuthorizedFolders.resolve_file`);
the real path must be inside the enrolled folder; the SHA-256 must equal the approved one; the size limit applies; then the existing `upload_file` (high-risk → approval) runs. The model never supplies a path.

## Context resolver
`BrainReader` (per task): `scope` (deterministic intent detection on the wording, or the panel/CLI switch `--business/--no-business`), `plan` (manifest), `prepare` (tokens),
`redact`/`hides_value` (outbound filter), audit helpers. It has no write methods; `TaskRunner` holds no handle to the store.

## Approval (the existing gate)
Added to `ApprovalPolicy`: a HIGH business value asks once per (task, site origin, field) — the card names the field labels and the site, warns if the field was not mentioned in the
task, the page is not HTTPS, the host is look-alike/non-Latin, or the field is in an embedded frame; a form-submitting action after business details were entered always asks and lists
what was entered; uploads keep the existing high-risk approval with the document label. Brain writes (confirm/reject/resolve, enrol, import, lower sensitivity) are not task actions.

## UI (side panel, hidden unless the agent reports the flag ON)
`sidepanel/biz_ui.js` + a collapsible **Business Brain** section: **Profile** (masked values, status and sensitivity chips, source, Confirm / Reject / Use this one, **Reveal** for 10 s then wiped), **Documents**
(Approve / Exclude), **Sources** (read-only; shows the terminal commands), **Security** (plain statement, protection, local/cloud models), **Activity** (audit). A per-task *Auto / On / Off* switch and a chip
(“Business Brain: on — your task mentions your business information · 2 field(s) filled”) sit by the Task box. Commands are `biz_*` over the existing authenticated socket; the service worker relays
results once, in memory (never in its state, storage or log). The panel cannot enrol, import, set values or lower sensitivity.

## Audit
Events: retrieve, use, blocked, missing, conflict_surfaced, prompt_context, fact_added/confirmed/rejected/superseded/deleted, conflict_resolved, reveal, rejected_secret, doc_registered/changed/indexed/approved/excluded,
folder_enrolled/revoked, profile_changed_during_task, error. `business audit [--task T]`, `business prune --days N` (the only way rows are removed).

## Tests (new)
`test_sensitive` 21 · `test_business_store` 42 · `test_business_documents` 40 (1 skipped: file symlinks need privileges) · `test_business_resolver` 29 · `test_business_loop` 39 · `test_business_ui` 18 · JS `biz_ui` 20 ·
AI e2e 13–16 (real Chrome) · live local-model case L3. See the final report for totals and regression results.

## Failure and rollback
* Database damaged at start → the agent still starts; Business Brain is off with a message; tokens are refused. Failed integrity check → readers inactive. Decrypt failure → ask the user. Audit-write failure → never breaks a task.
* Rollback: `business disable` (or remove the `business` block) restores Phase 2 behaviour; delete `data/business.db` to remove the data; no other file references it.
* Code touch points: `engine/dispatcher.py` (`sensitive_call`, sensitive-field log redaction), `reasoning/router.py` (`local_only`, `provider_is_local`), `brain/{loop,context,approval,tools,prompts,task,history,manager,settings}.py`, `app.py`, `api/{cli,task_routes}.py`, `logging_setup.py`, extension `sidepanel/*` + `background/{bridge,service_worker}.js` (UI only).

## Deliberately disabled / absent in v1
Banking, Aadhaar, passwords/OTPs/keys/payment credentials, OCR, PDF text (needs `pypdf`), vector search, panel value entry, a text-reply box for missing values, trusted-origin lists, any real portal or real document.

## Amendments to the design made at approval
Approved all of D1–D14 with: HIGH values never in any prompt incl. local (strengthened); banking and Aadhaar not implemented; a **general** logging layer (not only Business Brain) before go-live; feature flag OFF by default.
