# Phase 3 — Spreadsheet import (the bulk way to enter business and client data)

> The everyday way to manage this information is now the **Knowledge Hub** ([PHASE3_HUB.md](PHASE3_HUB.md)): a local web page that also imports workbooks (same pipeline, same review) and Markdown files. This document describes the importer behind it and the command-line route, which both still work.

Excel (or CSV) is now the primary way to give the agent your One Chance to Grow profile and your clients. The `business onboard` / `set` / `confirm` / `resolve` / `missing` / `list` commands are unchanged and stay the
tool for one-off changes, corrections, protected fields and troubleshooting.

```
Excel / CSV  ->  read + validate  ->  map columns  ->  PREVIEW  ->  conflicts / duplicates / missing / sensitive  ->  YOU confirm  ->  Business Brain / Client Brain  (business.db)
                    (data only)       (ask if unclear)   (writes nothing)                                          (typed words)        (one transaction, backup first)
```

The security architecture is untouched: a workbook is an **input**, never a model's output. No model can start, change or confirm an import; no panel command can write one; the importer imports nothing
from the browser engine, the bridge, the model layer or the task loop (test-enforced).

## The files and commands
| what | where |
|---|---|
| blank template (headers and guidance only, no data) | `D:\Tool\browser agent\templates\OCTG_Business_Data_TEMPLATE.xlsx` |
| make a fresh blank template | `python -m octg_agent business template [path]` (never overwrites an existing file unless `--force`; does not touch `business.db`) |
| preview only (writes nothing) | `python -m octg_agent business import "<workbook>.xlsx" --dry-run` |
| import (preview, questions, typed confirmation) | `python -m octg_agent business import "<workbook>.xlsx"` |
| CSV | `business import Clients.csv` (the file name says which sheet, or `--sheet "Clients"`) or a folder of CSV files named after the sheets |
| past imports / vouch later | `business imports`, `business import-verify <import id>` |
| where did a value come from? | `business source <field> [--client C]` (workbook, sheet, row, column, import id, time; never the value) |
| a client's services / locations / SEO targets | `business records --client C [--kind services\|locations\|seo]` |

Side panel (**Business Brain → Imports**): lists past imports and **previews** a workbook you choose (masked, nothing saved, preview dropped when you leave the tab). Importing itself stays in the terminal, like every
other action that adds data or widens access (the panel has no commit command at all; tested).

## The workbook (sheet names and columns are exact)
* **My Business** — rows of `Field | Value | Guidance` (Guidance is ignored): `legal_name, trade_name, website, email, support_email, phone, address_line_1, address_line_2, city, state, country, postal_code, description, primary_services, additional_services`, and optionally `registration.udyam, registration.pan, registration.gstin, registration.tan, registration.cin` (stored as `registration.udyam`, `taxation.pan`, `taxation.gstin`, `taxation.tan`, `registration.cin`). Any catalog key may be typed in the Field column (e.g. `owners[0].name`). Several services in one cell: separate with `;` or new lines.
* **Clients** — `client_id, business_name, legal_name, trade_name, website, email, phone, contact_person, description, primary_address_line_1, primary_address_line_2, city, state, country, postal_code, status, onboarding_date, plan, internal_notes, pan, gstin, tan, cin, udyam`. `client_id` is *your* id (CLIENT-001); it links the other sheets to the client and is how a later import recognises the same client. A client needs a name (`business_name`, else `trade_name`, else `legal_name`).
* **Client Services** — `client_id, service, service_category, priority, notes` (one row per client/service).
* **Client Locations** — `client_id, location, city, state, country, postal_code, location_type, priority, notes` (`location_type`: primary, service_area, target_area, other).
* **Client SEO** — `client_id, keyword, location, priority, service, notes` (structured input for a future research feature; nothing is researched or ranked by importing it).
* **README** — instructions and clearly-marked examples; never imported. Unknown sheets are reported and ignored; hidden sheets are ignored with a note.
`priority` is High / Medium / Low. Every field is optional except what links rows (client_id) and names them. Blank means missing.

## Preview and confirmation
`--dry-run` (or the first part of a normal run) prints: workbook and fingerprint; counts detected; every sheet with recognised / unknown / needs-confirmation / refused columns and the template columns it lacks;
**NEW / UPDATED / UNCHANGED** (clients and rows), per-value totals, **CONFLICTS**, **DUPLICATES**, **MISSING** (fields that have no value anywhere), **SENSITIVE** (every protected value, masked), problems and notes.
Nothing is written until you have (1) settled every question, (2) typed `IMPORT`, (3) typed `PROTECT` if protected values are involved, and (4) said whether you vouch for the workbook. Anything else cancels. Non-interactive runs need
`--yes` (and `--confirm-sensitive` for protected values) and never decide for you (`--on-conflict keep` is the only blanket choice offered; it never replaces anything).

## Column mapping (ask, never guess)
Exact template names and a short list of known alternatives (`Company Name`, `Web Site`, `ZIP Code`, `Phone Number` …) are CONFIDENT. Wording that could mean materially different fields is **NEEDS CONFIRMATION** and
blocks the import until you choose: `Name` (business / legal / trade), `Contact`, `Notes` (internal notes / description), `Address`, and — in *My Business* — `Business Name` / `Company Name` (legal or trade name). Two columns
that would fill the same field also ask. Unknown columns are reported and ignored (a closest-name hint is shown, never applied). Columns that look like a password, OTP, API key, card, bank account or Aadhaar number are **refused**
and never read. Resolve with the questions, or `--map "Clients:Name=business_name"` / `--skip "Clients:Notes"`.

## Conflicts (never overwritten silently)
* A different value for a field whose current value is **VERIFIED** (or came from a document, or is already conflicting) is a **CONFLICT**: *keep existing* (default) / *use spreadsheet value* / *cancel*. Existing and sheet values are shown in the terminal for LOW and MEDIUM fields and **masked for HIGH**; the panel masks MEDIUM too. Replacing a HIGH value needs the extra typed word `REPLACE`, and the commit needs `PROTECT`.
* An earlier, still-unverified *import* is simply replaced by a newer workbook (UPDATE). A value you rejected or replaced earlier is **not re-added**.
* Addresses merge by part: a part the sheet leaves blank keeps its stored value; a differing part, or a part you did not have, is a conflict if the stored address is verified.
* Repeatable fields (services, social links, owners…) only **add** entries; they never replace. Rows are never deleted by an import.
* Services / locations / SEO targets: a changed row (priority, notes …) updates an unverified one and conflicts with a verified one; the old row stays as history (superseded, never overwritten).
* A client whose name in the sheet differs from the stored name is a conflict on the name.
* Your explicit "use spreadsheet value" is your confirmation: that value becomes VERIFIED.

## Duplicate clients
A sheet client is matched to an existing one **only** by reference (`client_id` equal to the stored reference, case-insensitive) or by *your answer*. Otherwise, if the exact name, a similar name (company words such as LLC / Inc / Services ignored),
or the web address (http/www ignored) matches an existing client — or another row in the same workbook — the preview shows **POSSIBLE DUPLICATE** with the reasons and asks: *same client* (links them, stores the reference, updates the existing client) / *different client* /
*cancel*. Same *exact* name cannot be a second client (clients are told apart by name when you pick one for a task), so *different client* is not offered; a different stored id makes *same client* unavailable. Nothing is ever merged automatically, and the rows of a client waiting for a decision wait too.

## Sensitive data
* PAN, GSTIN, Udyam, TAN, CIN, internal notes (and any HIGH catalog field) get the existing HIGH treatment: encrypted with Windows DPAPI at rest, never in a model prompt, never in logs / history / audit / import records / error text (all show masks; tested by scanning every table, log and view).
* While a plan exists its protected values are registered with the log scrubber; the registry is released when the preview closes or the commit ends.
* Passwords, OTPs, API keys, payment cards are refused wherever they appear (cell values *and* column names); bank-account / IFSC / Aadhaar-shaped values in ordinary columns are refused; banking and Aadhaar stay disabled.
* A value that is going to a field a *local model* may be shown (a state name; the constitution) must have the shape of a short place name / a known constitution, so spreadsheet text cannot ride into a prompt through those two fields.
* Spreadsheet text is **data**: it is never an instruction, a formula, a path or a command. `Ignore previous instructions…` is stored as ordinary text (and flagged to you as an FYI); formulas are never evaluated; macros, password-protected files, DOCTYPE/entity declarations and zip bombs are refused; strict size, row, column and cell limits apply.
* Error messages and the preview name the sheet, row and column — never the cell's value.

## Source tracking
Every imported fact has a `fact_sources` row (import session, workbook name, sheet, row number, column, time) and `source_ref` text `imp_xxxxxxxx spreadsheet <sheet> row <n>`; every structured row carries the same fields on the row itself; `import_sessions` records
workbook name, content fingerprint, time, whether you vouched, and counts. Verification state and sensitivity live on the fact / row. None of it contains a value. `business source taxation.pan` answers "where did this come from?" and
`business source location.address --client CLIENT-001` the same for a client address.

## Verification
Values enter **UNVERIFIED** (tasks refuse them) unless you vouch for the workbook (`y` at the last question, or `--assume-verified`). Vouch later with `business import-verify <import id>`, or confirm field by field as before.

## Safety: backup, one transaction, rollback
Before any write: an automatic backup (`business.db.bak-<time>-pre-import-<id>`); the whole plan is re-checked inside the write transaction (if the database changed since the preview the import stops, `STALE`); then clients, facts, rows, sources, the session and audit rows
are written in **one** SQLite transaction. Any failure — a bug, a unique-constraint clash, an I/O error — rolls everything back (only the failure itself is audited), the message says nothing was changed and names the backup. The schema moved to v3 (additive: `clients.ref`, the
structured tables, import sessions, sources) after an automatic `pre-v3` backup; the v1→v2 migration is unchanged and also ends at v3.

## Client scoping
Every imported client row, service, location and SEO target belongs to exactly one client; a row whose `client_id` matches no client in the workbook and none stored is refused. The owner's own profile stays in the owner scope. Nothing is copied between clients or from the owner
(blank stays missing; test-enforced), and the task loop still reads exactly one scope.

## For the future SEO Intelligence phase (nothing of it is built)
`store.seo_targets(client_id)` returns the live keyword + location targets of one client (with priority, service, notes, provenance); `store.records("service"|"location", client_id)` the services and locations. Future flow: *Client → SEO target → keyword + location → SERP research → competitor analysis → dated observation (`research_observations`) → report.* No scraping, ranking, crawling or publishing was added.

## Not done on purpose
No automatic import of anything (a workbook is read only when you run the command or pick it in the panel); no document ingestion through the spreadsheet (`fs allow → docs enroll → scan → index → approve` is the only path); no credentials, banking, Aadhaar, vector search, SEO research; no change to the browser actions, provider routing or voice.

## Making your real workbook
1. `python -m octg_agent business template D:\OCTG_Data\` (or copy `templates\OCTG_Business_Data_TEMPLATE.xlsx`) — keep the working file **outside** the agent folder, under a new name such as `OCTG_Business_Data.xlsx`.
2. Fill **My Business** (Value column), **Clients** (one row each, your own `client_id` like CLIENT-001), then the three client sheets with the same `client_id`. Leave anything unknown blank. Read the README sheet once.
3. `python -m octg_agent business import "D:\OCTG_Data\OCTG_Business_Data.xlsx" --dry-run` — read the preview; fix the workbook for any PROBLEMS; re-run until you are happy.
4. `python -m octg_agent business import "D:\OCTG_Data\OCTG_Business_Data.xlsx"` — answer the questions, type `IMPORT`, `PROTECT`, then `y` only if you vouch for it.
5. `python -m octg_agent business enable`, restart `python -m octg_agent serve`, reload the extension; check Business Brain → Clients / Imports.
