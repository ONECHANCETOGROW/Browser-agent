# OCTG Knowledge Hub — the normal way to manage the Business Brain and the Client Brain

A private web page on your own computer where you type, edit, import, review and organise everything the agent may know about **your business** and **your clients**. No terminal, no Excel round-trip, no database editing.

```
Chrome  →  Knowledge Hub page (HTML / CSS / vanilla JS, served by the agent)  →  /hub/api (JSON, same agent process)  →  Business Brain / Client Brain code  →  SQLite (business.db)
```
The database stays the single source of truth. The page is only a way to *ask Python to do things*: every validation, every refusal, every encryption and every "this replaces something" check is done by the same Python code the command line uses (`business/admin.py` → `BusinessStore`, the spreadsheet pipeline). There is no second security implementation in JavaScript.

## Open it
* Desktop shortcut **OCTG Knowledge Hub** (or double-click `open_knowledge_hub.bat` in the agent folder): starts the agent if it is not running, makes a one-time sign-in link and opens your browser already signed in.
* Or: `python -m octg_agent hub open`. The address is always `http://127.0.0.1:8765/hub/` (`python -m octg_agent hub url`).
* If you open the address by hand it asks for your PIN (or, before a PIN is set, the access code printed by `python -m octg_agent token --api`; the extension's pairing token also works).
The agent starts by itself at Windows login (Startup shortcut), so the Hub is normally just there.

## What is in it
| page | what you do there |
|---|---|
| **Dashboard** | how complete your profile is; clients, documents, sources; values waiting for your confirmation; conflicts; recent imports and changes; security status. Masks and counts only. |
| **My Business** | tabs: Identity, Contact, Address, Services, Description, Registration, Tax / Business IDs, Owners and partners, Social profiles, **Sources**. Type, **Save**, **Edit**, **Remove**, **Confirm**, **Resolve**. |
| **Clients** | a table (Client, Website, Primary location, Services, SEO status, Status, Last updated) with filter, **Add client**, View / Edit / **Archive**; the client page has tabs Overview, Business information, Contact, Locations, Services, SEO targets, Documents, Sources, Research, Notes, Activity. |
| **Documents** | the five authorization steps with their exact commands; **Scan** and **Read** buttons; Approve / Exclude. |
| **SEO** | each client's SEO targets (keyword + location + priority + service), target lists and goals, and (later) research sessions and observations. |
| **Sources** | every import (workbook / Markdown file) with its fingerprint, scope, time and verification, plus values entered by hand and authorized documents; and the connectors that exist or are planned. |
| **Imports** | the Import Center: upload a workbook or a Markdown file, review it value by value, confirm; history with status and per-value results. |
| **Conflicts** | stored fields with two different values and values that wait for your confirmation. |
| **Activity** | what happened, when, to which scope; never a value. |
| **Security** | protection status, what is and is not shared, backups, end all Hub sessions. |
| **Settings** | let tasks use the Business Brain; default client for tasks. |
The **scope bar** at the top always says `CURRENT SCOPE: <your business>` or `CURRENT CLIENT: <name>` and lets you switch. Search (top right) looks through clients, services, locations, keywords and sources.

## How to …
1. **Add your business.** My Business → fill the boxes of a tab → *Save changes*. Empty boxes stay empty; nothing is guessed. Protected fields (tax / registration ids, internal notes) use a password box and are stored encrypted.
2. **Add a client.** Clients → *+ Add client* → fill what you know (only a name is needed): business details, address, service areas, services, SEO targets, account → *Add client*. All of it is saved in one step or not at all.
3. **Import Markdown.** Imports → *Import a Markdown knowledge file* → choose **whose knowledge it is** (required: your business or one client; never guessed) → choose the `.md` file (or one from an authorized folder) → *Read and analyse* → untick what you do not want → *Review and confirm import…*. Nothing is saved before that last step.
3b. **Add a NEW client from a Markdown file.** Imports → *Import a Markdown knowledge file* → *This file is about* → **＋ A NEW client (create it from this file)** → choose the `.md` file → *Read and analyse* → review → *Review and confirm import…*. The client is told apart by name, so the file needs a `Business name: …` line (or type the name into the optional box; the file's own line wins). Nothing exists until you confirm; if a client with the same name already exists you are asked, never silently duplicated. One file = one client. Blank starting points: the *Client template (.md)* and *My business template (.md)* buttons on that page, or `templates\OCTG_Client_Profile_TEMPLATE.md` / `OCTG_Business_Profile_TEMPLATE.md`. Leave a line's value empty to skip it; values such as NOT PROVIDED / CONFLICTING / VERIFY / “leave blank” are left blank, not imported.
4. **Import Excel.** Imports → *Import a spreadsheet* → choose the workbook (template: `templates\OCTG_Business_Data_TEMPLATE.xlsx`) → *Read and preview* → answer the questions (unclear columns, possible duplicate clients, conflicts), untick anything unwanted → *Review and confirm import…*.
5. **Review and approve.** Every value shows what would happen (New / Update / Already stored / Conflict) and where it was found (sheet and row, or section and line). Values that are not yet confirmed appear under *Conflicts* with *Confirm*.
6. **Manage conflicts.** In an import: *Keep existing* or *Accept new* per conflict. Stored conflicts: *Conflicts* page → *Keep this one* (the other is kept as history) or *Reject this one*, or *Edit by hand*.
7. **Add SEO targets.** Client → *SEO targets* → *+ Add SEO target* (keyword, location, priority, service, notes); or when adding a client. SEO → pick a client for the workspace view.
8. **Export safe data.** *Export to Excel* on My Business, a client, Clients (all) or SEO. Protected values are **never** exported (their cells are blank, so re-importing the file cannot change them).
9. **Back up.** Security → *Back up now*. A backup is also made automatically before every import and every upgrade.
10. **See where something came from.** Sources, or the line under each value (“Imported · imp_… spreadsheet Clients row 2”); an import's page lists every value it handled.

## Markdown: what is read (and what is not)
Markdown is **data**, read deterministically — no model reads it, and no line is ever an instruction ("Ignore all previous instructions…" is flagged as an FYI and kept as plain text). Read: `Label: value` lines (also as bullets, bold or in a front-matter block), two-column `Field | Value` tables, tables with Service / Location / Keyword columns, bullet lists under headings such as *Services*, *Additional services*, *Service areas*, *Target locations*, *Keywords*, and **one** *About / Description* section as a candidate description. Not read: free prose, code blocks, comments, nested bullets, tables it does not recognise, single-line addresses (it will not split them for you), ambiguous labels (`Name`, `Notes`, `Business name` for your own business). Passwords, OTPs, keys, cards and bank / Aadhaar labels are refused and their values never read. Everything is a **candidate** until you confirm the import; the original file's name, fingerprint, section and line stay attached to every value. The file's content itself is not stored.

## Security model
* **Local only.** Bound to `127.0.0.1`; answers only loopback `Host` names (no DNS-rebinding), and works with no internet. Nothing is loaded from the internet (no fonts, scripts or images).
* **A web page cannot use it.** Every API call needs a session cookie (HttpOnly, SameSite=Strict, scoped to `/hub`, gone when the browser closes) **and** a per-session CSRF header only the Hub's own script can read; state-changing calls must come from the Hub's own origin; `Sec-Fetch-Site` must be same-origin; no CORS grant exists; search is a POST so words never sit in an address. Strict CSP (no inline script, no eval, no frames).
* **Signing in.** Your **PIN** (4 to 12 digits; set with `python -m octg_agent hub pin set`, or on the Security page; only a salted hash is stored, in `data/hub_pin.json`), the long access token (the way back in if the PIN is forgotten), or a one-time code (valid 5 minutes, used once, stored only as a hash). A short PIN is easy to guess, so five wrong guesses lock signing in for 1 minute, the next five for 2, then 4, 8 … up to 60 minutes, until a correct sign-in. Changing or removing the PIN needs the current PIN. `hub pin status` / `hub pin clear` work from the terminal. Sessions expire after 8 hours idle / 24 hours.
* **The browser agent cannot operate the Hub.** The extension classifies every `/hub` page on a loopback address as one it may not script: `inspect_page`, `screenshot`, `click`, `type`, `scroll`, `find_text` all fail with UNSUPPORTED_PAGE, so a model can neither read it nor click in it (real-Chrome tested). The Hub API also refuses any request without its own session and CSRF header.
* **Values.** Ordinary values are shown to you (it is your screen). Protected (HIGH) values are encrypted with Windows DPAPI, shown masked, revealed only on a click for ten seconds (audited without the value), never exported, never searched, never in a log, audit row, import record or error message. Saving, importing, searching and exporting **never call an AI model**.
* **Nothing is ever permanently deleted:** removing a value rejects it, removing a row retires it, removing a client archives it; history stays. There is no delete route.
* **Every change goes through the same checks as the CLI:** credentials / keys / cards refused anywhere, identifier-shaped values refused in ordinary fields, format checks, scope checks (a value saved for one client can only land on that client), a replacement of something you already have needs your explicit yes (enforced by the server), and a protected-value replacement or import needs its own confirmation.
* Folders can still only be **authorized and enrolled in the terminal** (that is what stops a web page or model from widening what is read). The Hub's document buttons only scan, read and approve inside folders you already enrolled.

## Imports and sources
* A preview lives in memory only (two hours, cleared on restart); raw protected values in it are registered with the log scrubber while it exists and released when it ends.
* Statuses: **Preview** (something is still unresolved), **Waiting for confirmation** (ready), **Imported**, **Partially accepted** (some values were left out or the existing value was kept), **Rejected**, **Failed** (rolled back; a backup was made first). Each import has an ID, type, source, scope, date, record count and a per-value result list (masks only).
* One transaction, a backup first, the plan re-checked inside the transaction (`STALE` if the database changed), full rollback on any failure. Schema v4 (additive; `pre-v4` backup before upgrading).
* **Future connectors** (Google Sheets, PDF): `business/spreadsheet/connectors.py` is the extension point. A connector only has to produce a `Workbook` (rows of text) or a Markdown-like text; the same parser, comparison, review and commit apply. No separate database.

## The default client for tasks
Settings → *Default client for tasks* (or the button in the scope bar). It is used **only** for a task that is about "the client" and whose wording names none, instead of pausing to ask you. A client named in the wording always wins; tasks about your own business are never affected; with no default the task still asks. The agent needs a restart for *use in tasks* (Settings) to take effect.

## Limits and honest notes
* Workbooks up to 10 MB, Markdown up to 1 MB / 20,000 lines; previews are kept two hours.
* The Hub shows ordinary values in clear on your screen; if someone else can see your screen, lock the Hub (sidebar) or end all sessions (Security).
* Restoring a backup is a manual step (stop the agent, copy the backup over `business.db`, start the agent).
* The research engine does not exist yet: the SEO workspace is the place its targets live and where its observations will appear.

## Command-line fallback (unchanged)
`business onboard / set / confirm / resolve / import / template / source / records …` still work and use the same code. `python -m octg_agent hub open | url` opens the Hub.

## Tests
`tests/test_hub.py` (API, security, isolation, masking, imports, Markdown, export, settings, SEO, CLI, assets), `tests/test_markdown_import.py`, `tests/test_hub_browser.py` (26 flows in real headless Chromium: unlock, edit, reveal, add client, table, rows, Markdown review, workbook review with conflicts, reject, conflicts, search, export, accessibility, phone layout), `tests/js/classify.test.cjs`, and real-Chrome AI e2e case 21 (the browser agent cannot operate the Hub).
