"""`python -m octg_agent business ...`: the local, human-only way to manage the Business Brain (works with the agent stopped).

Everything that WIDENS what the agent can use (creating clients, enrolling folders, importing or entering values, lowering a sensitivity, switching the feature on)
lives here and nowhere a model or a web page can reach. Values for HIGH fields are read with a hidden prompt, never from the command line history.

Two kinds of owner, kept apart: your OWN business (no flag) and your CLIENTS (`--client <id or exact name>`). A client's values never mix with yours or another client's.

  business status | enable | disable | fields [--client C] | list [--client C] | show <key> | set <key> [value] | onboard | missing | confirm <fact_id> | reject <fact_id>
  business resolve <key> <fact_id> | delete <fact_id> | reveal <fact_id> | import <file.json> [--assume-verified] | export | audit | backup | prune
  business template [path]                      write the blank OCTG workbook (xlsx) you fill in with your business and client information
  business import <workbook.xlsx | file.csv | folder> [--dry-run] [--map ...] [--skip ...] [--decide ID=choice] [--on-conflict keep] [--assume-verified] [--yes]
                                                Excel/CSV import: validate -> map -> preview -> you confirm -> business.db (primary way to enter data in bulk)
  business imports | import-verify <import id> | source <key> [--client C] | records --client C [--kind services|locations|seo]
  business client create "<name>" | list | show <client> | rename <client> "<new name>" | archive <client> | restore <client>
  business docs enroll <folder> [--client C] | revoke <folder> | scan | index [doc_id] | list [--client C] | approve <doc_id> | exclude <doc_id> | type <doc_id> <type>
  business research list --client C       (observations only; the research engine itself does not exist yet)
"""
from __future__ import annotations

import getpass
import json
import sys
import time
from pathlib import Path

from ..business.documents import DOC_TYPES
from ..business.onboard import missing_fields, run_onboarding
from ..business.research import ResearchLog
from ..business.schema import CATALOGS, ME, KeyError_, parse_key
from ..business.service import BusinessConfig
from ..business.store import BusinessStore, StoreError
from ..config import load_settings
from ..files.authorization import AuthorizedFolders


def _open():
    from ..business.documents import DocumentRegistry

    s = load_settings()
    store = BusinessStore(s.data_dir / "business.db")
    return s, store, DocumentRegistry(store, AuthorizedFolders(s.folders_path))


def _brain_json(settings) -> Path:
    return settings.data_dir / "brain.json"


def _set_enabled(on: bool) -> None:
    s = load_settings()
    p = _brain_json(s)
    d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    d.setdefault("business", {})["enabled"] = on
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _enabled(settings) -> bool:
    p = _brain_json(settings)
    try:
        return BusinessConfig.from_dict(json.loads(p.read_text(encoding="utf-8")).get("business")).enabled if p.is_file() else False
    except (OSError, ValueError):
        return False


def _scope(store: BusinessStore, args) -> str:
    """--client <id or exact name> -> the client's id; no flag -> your own business. An unknown or ambiguous client is an error, never a guess."""
    c = getattr(args, "client", None)
    if not c:
        return ME
    cl = store.find_client(c)
    if cl["archived"]:
        raise StoreError("CLIENT_ARCHIVED", f"{cl['name']} is archived (business client restore {cl['client_id']})")
    return cl["client_id"]


def _who(store: BusinessStore, scope: str) -> str:
    return "your own business" if scope == ME else f"client {store.get_client(scope)['name']} ({scope})"


def _fmt_field(f) -> str:
    live = [x for x in f.facts if x.state == "LIVE"]
    src = ", ".join(sorted({(x.source_type.lower().replace("_", " ")) for x in live})) or "-"
    mask = live[0].mask if len(live) == 1 else (f"{len(live)} different values" if live else "")
    return f"{f.key:<38} {f.status:<11} {f.sensitivity:<6} {mask:<22} {src}"


def _ask_value(prompt: str, hidden: bool) -> str:
    return getpass.getpass(prompt + " (input hidden): ") if hidden else input(prompt + ": ")


def _template(args) -> int:
    from ..business.spreadsheet import write_template

    target = Path(args.path) if args.path else Path.cwd()
    try:
        out = write_template(target, force=args.force)
    except FileExistsError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 1
    print(f"Blank template written: {out}")
    print("It contains headers and guidance only (no business data). Save a COPY under a new name, fill it in, then:")
    print('  python -m octg_agent business import "<your workbook>.xlsx" --dry-run     (preview only)')
    print('  python -m octg_agent business import "<your workbook>.xlsx"               (preview, then confirm)')
    return 0


def _spreadsheet_import(args, store: BusinessStore) -> int:
    from .business_import_cli import ImportOptions, run_import

    opts = ImportOptions(sheet=args.sheet, dry_run=args.dry_run, maps=args.map or [], skips=args.skip or [], decide=args.decide or [], on_conflict=args.on_conflict, yes=args.yes,
                         accept_errors=args.accept_errors, confirm_sensitive=args.confirm_sensitive, assume_verified=args.assume_verified, interactive=sys.stdin.isatty())
    return run_import(args.file, store, opts, _ask_value, print)


def _imports(args, store: BusinessStore) -> int:
    from ..business.spreadsheet import SpreadsheetImporter

    imp = SpreadsheetImporter(store)
    if args.biz_cmd == "import-verify":
        n = imp.verify_session(args.session_id)
        print(f"Marked VERIFIED: {n['facts']} value(s) and {n['records']} row(s) from import {args.session_id}. Fields that were in conflict stay in conflict until you resolve them.")
        return 0
    rows = imp.sessions()
    if not rows:
        print("No spreadsheet imports yet.")
    for r in rows:
        c = r["counts"]
        print(f"{r['session_id']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created_at']))}  {r['status']:<10} {r['workbook']:<34} new values {c.get('facts_new', 0)}, rows {c.get('records_new', 0)}, "
              f"clients {c.get('clients_created', 0)}  {'(vouched: verified)' if r['assume_verified'] else '(unverified until confirmed)'}")
    return 0


def _source(args, store: BusinessStore, scope: str) -> int:
    rows = store.sources_of(args.key, scope)
    print(f"{_who(store, scope)}: {args.key}")
    if not rows:
        print("  no value has ever been stored for this field")
    for r in rows:
        src = r["imported"]
        where = (f"spreadsheet '{src['workbook']}', sheet '{src['sheet']}', row {src['row']}, column '{src['column']}', import {src['session']}, "
                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(src['at']))}") if src else (f"document {r['doc_id']}" if r["doc_id"] else r["source_type"].lower().replace("_", " "))
        print(f"  {r['fact_id']}  {r['state']:<10} {r['verification']:<10} {r['sensitivity']:<6} {r['mask']:<22} {where}")
    return 0


def _records(args, store: BusinessStore) -> int:
    cl = store.find_client(args.client)
    kinds = {"services": "service", "locations": "location", "seo": "seo"}
    print(f"{cl['name']} ({cl['client_id']}{', ' + cl['ref'] if cl['ref'] else ''})")
    for label, kind in kinds.items():
        if args.kind and args.kind != label:
            continue
        rows = store.records(kind, cl["client_id"])
        print(f"{label.upper()}: {len(rows)}")
        for r in rows:
            main = {"service": lambda: [r["service"], r["category"], r["priority"]], "location": lambda: [r["location"] or r["city"], r["state_region"], r["location_type"], r["priority"]],
                    "seo": lambda: [r["keyword"], r["location"], r["priority"], r["service"]]}[kind]()
            src = f"{r['sheet']} row {r['row_num']}, import {r['session_id']}" if r["session_id"] else "entered directly"
            print("   " + " | ".join(str(x) for x in main if x) + f"   [{r['verification']}; {src}]")
    return 0


def cmd_business(args) -> int:
    if args.biz_cmd == "template":     # nothing to open: writing the blank template must not create or touch business.db
        return _template(args)
    settings, store, docs = _open()
    c = args.biz_cmd
    try:
        if c == "status":
            n = store.counts()
            print(f"Business Brain: {'ON' if _enabled(settings) else 'OFF (the feature flag is off: tasks do not use it)'}")
            print(f"  protection: {n['cipher']}{'' if n['cipher_strong'] else ' (WEAK: key stored beside the data)'}  | live facts {n['live_facts']} ({n['encrypted_facts']} encrypted)")
            print(f"  database healthy: {store.integrity_ok()}  | clients: {n['clients']}  | enrolled folders: {len(docs.enrolled())}  | documents: {len(docs.list())}")
            return 0
        if c == "enable":
            _set_enabled(True)
            print("Business Brain enabled for new tasks (restart the agent: python -m octg_agent serve).")
            return 0
        if c == "disable":
            _set_enabled(False)
            print("Business Brain disabled. business.db is untouched.")
            return 0
        if c == "client":
            return _client(args, store, docs)
        if c == "docs":
            return _docs(args, store, docs)
        if c == "research":
            return _research(args, store)
        if c == "prune":
            print(f"Removed {store.prune_audit(args.days)} audit row(s) older than {args.days} days.")
            return 0
        if c == "backup":
            print("Backup written:", store.backup())
            return 0
        if c in ("imports", "import-verify"):
            return _imports(args, store)
        if c == "records":
            return _records(args, store)
        scope = _scope(store, args)
        kind = store.kind_of(scope)
        if c == "source":
            return _source(args, store, scope)
        if c == "fields":
            for f in CATALOGS[kind][0]:
                print(f"{f.key:<38} {f.sensitivity:<6} {f.label}" + (f"   [{', '.join(p.name for p in f.parts)}]" if f.parts else ""))
            return 0
        if c == "list":
            print(f"{_who(store, scope)}\n{'FIELD':<38} {'STATUS':<11} {'SENS':<6} {'VALUE (masked)':<22} SOURCE")
            for f in store.list_fields(include_missing=args.all, scope=scope):
                print(_fmt_field(f))
            return 0
        if c == "missing":
            rows = missing_fields(store, scope)
            print(f"{_who(store, scope)}: {len(rows)} field(s) have no value yet (nothing is guessed or filled in for you):")
            for r in rows:
                print(f"  {r['key']:<38} {r['sensitivity']:<6} {r['label']}")
            return 0
        if c == "onboard":
            print(f"Entering information for {_who(store, scope)}. Press Enter to skip anything you don't have; nothing is guessed. HIGH fields are hidden as you type.")
            res = run_onboarding(store, scope, _ask_value, print, only=args.only)
            print(f"Done: {res['stored']} stored, {res['kept']} kept, {res['skipped']} skipped, {res['conflicts']} conflicting field(s) to resolve.")
            return 0
        if c == "show":
            f = store.status(args.key, scope)
            print(f"{_who(store, scope)}\n{f.key}  ({f.label})  {f.status}  {f.sensitivity}")
            for x in f.facts:
                print(f"  {x.fact_id}  {x.state:<10} {x.verification:<10} {x.source_type:<14} {x.mask:<22} {'doc ' + x.doc_id if x.doc_id else x.source_ref}  {'format warning: ' + x.format_note if not x.format_ok else ''}")
            return 0
        if c == "set":
            pk = parse_key(args.key, kind)
            if pk.field.many and "[" not in args.key:  # "add another": a repeatable field written without an index goes into the first free slot, never over (or in conflict with) an existing entry
                i = 0
                while store.status(pk.field.key.replace("[]", f"[{i}]") + (f".{pk.part}" if pk.part not in (None, "full") else ""), scope).live and i < 50:
                    i += 1
                args.key = pk.field.key.replace("[]", f"[{i}]")
                pk = parse_key(args.key, kind)
                print(f"(repeatable field: using entry #{i + 1}; use {args.key} to change a specific entry)")
            if pk.field.compound:
                value = {}
                for part in pk.field.parts:
                    v = input(f"{part.label} (blank to skip): ").strip()
                    if v:
                        value[part.name] = v
            elif args.value is not None:
                value = args.value
            else:
                value = _ask_value(f"Value for {pk.label}", hidden=pk.sensitivity == "HIGH")
            if args.replace:
                ans = input(f"This will REPLACE the current value of {pk.label} for {_who(store, scope)}. Type YES to confirm: ")
                if ans.strip() != "YES":
                    print("Cancelled.")
                    return 1
            r = store.put_candidate(args.key, value, source_type="USER_ENTERED", replace=args.replace, sensitivity=args.sensitivity, allow_lower=args.allow_lower, actor="cli", scope=scope)
            print(f"{'Stored' if r.created else 'Not stored'}: {pk.label} ({_who(store, scope)}) is now {r.status}." + (f" ({r.note})" if r.note else ""))
            for w in r.warnings:
                print("  warning:", w)
            return 0
        if c == "confirm":
            f = store.confirm(args.fact_id)
            print(f"Confirmed. {f.key} is now {f.status}.")
            return 0
        if c == "reject":
            f = store.reject(args.fact_id)
            print(f"Rejected. {f.key} is now {f.status}.")
            return 0
        if c == "resolve":
            f = store.resolve(args.key, args.fact_id, scope=scope)
            print(f"Resolved. {f.key} is now {f.status}.")
            return 0
        if c == "delete":
            if input(f"Delete {args.fact_id} permanently? Type YES: ").strip() != "YES":
                print("Cancelled.")
                return 1
            store.delete_fact(args.fact_id)
            print("Deleted.")
            return 0
        if c == "reveal":
            if input("Show the value in this terminal? Type YES: ").strip() != "YES":
                return 1
            print(store.reveal(args.fact_id))
            return 0
        if c == "import" and Path(args.file).suffix.lower() != ".json":
            if getattr(args, "client", None):
                print("Refused: a workbook names its own clients (the client_id column); --client is only for .json imports.", file=sys.stderr)
                return 1
            return _spreadsheet_import(args, store)
        if c == "import":
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            n = bad = 0
            for key, value in (data.get("facts") if isinstance(data, dict) and "facts" in data else data).items():
                try:
                    r = store.put_candidate(key, value, source_type="IMPORT", source_ref=Path(args.file).name[:40], assume_verified=args.assume_verified, actor="cli", scope=scope)
                    n += 1 if r.created else 0
                except StoreError as e:
                    bad += 1
                    print(f"  skipped {key}: {e.message}")
            print(f"Imported {n} value(s) for {_who(store, scope)} as {'VERIFIED (you vouched for them)' if args.assume_verified else 'UNVERIFIED (confirm them with: business confirm <fact_id>)'}; {bad} skipped.")
            return 0
        if c == "export":
            print(json.dumps(store.export_masked(scope), indent=2, ensure_ascii=False))
            return 0
        if c == "audit":
            for r in store.audit_tail(args.tail, task_id=args.task, scope=(scope if getattr(args, "client", None) else None)):
                who = r["scope"] or "me"
                print(f"{r['seq']:>5} {r['event']:<22} {who:<13} {r['task_id'] or '-':<16} {r['field_key'] or r['doc_id'] or '-':<26} {r['outcome'] or '':<18} {r['provider'] or ''}{'/' + r['locality'] if r['locality'] else ''} {r['detail'] or ''}")
            return 0
    except StoreError as e:
        print(f"Refused: {e.message}", file=sys.stderr)
        return 1
    except KeyError_ as e:
        print(f"Refused: {e} (see: business fields)", file=sys.stderr)
        return 1
    return 1


def _client(args, store: BusinessStore, docs) -> int:
    d = args.client_cmd
    if d == "create":
        cl = store.create_client(args.name, allow_duplicate=args.allow_duplicate)
        print(f"Created client {cl['name']!r}: id {cl['client_id']}")
        print(f"  add its details:  python -m octg_agent business onboard --client {cl['client_id']}")
        print(f"  use it in a task: python -m octg_agent task start \"...\" --client {cl['client_id']}")
        return 0
    if d == "list":
        rows = store.list_clients(include_archived=args.all)
        if not rows:
            print("No clients yet. Create one with: python -m octg_agent business client create \"<name>\"")
        for c in rows:
            n = store.counts(c["client_id"])
            print(f"{c['client_id']}  {c['name']:<36} {n['live_facts']:>3} fact(s)  {len(docs.list(scope=c['client_id'])):>2} document(s){'  [archived]' if c['archived'] else ''}")
        return 0
    cl = store.find_client(args.client_ref)
    if d == "show":
        print(f"{cl['client_id']}  {cl['name']}{'  [archived]' if cl['archived'] else ''}")
        for f in store.list_fields(include_missing=False, scope=cl["client_id"]):
            print(" ", _fmt_field(f))
        print(f"  missing fields: {len(missing_fields(store, cl['client_id']))} (see: business missing --client {cl['client_id']})")
    elif d == "rename":
        print(store.rename_client(cl["client_id"], args.new_name))
    elif d == "archive":
        print(store.set_client_archived(cl["client_id"], True))
    elif d == "restore":
        print(store.set_client_archived(cl["client_id"], False))
    return 0


def _research(args, store: BusinessStore) -> int:
    cl = store.find_client(args.client)
    log = ResearchLog(store)
    print(f"{cl['name']}: {log.summary(cl['client_id'])}")
    for o in log.observations(cl["client_id"], limit=args.limit):
        print(" ", log.describe(o))
    return 0


def _docs(args, store: BusinessStore, docs) -> int:
    d = args.docs_cmd
    scope = _scope(store, args) if hasattr(args, "client") else ME
    if d == "enroll":
        e = docs.enroll(args.path, args.label, scope=scope)
        print(f"Enrolled for {_who(store, scope)}: {e['path']}  (nothing is read until you run: business docs scan)")
    elif d == "revoke":
        print("Revoked." if docs.revoke(args.path) else "That folder was not enrolled.")
    elif d == "scan":
        print(json.dumps(docs.scan().to_dict(), indent=2))
    elif d == "index":
        print(json.dumps(docs.index(args.doc_id), indent=2))
    elif d == "list":
        for x in docs.list(search=args.search, scope=scope if getattr(args, "client", None) else None):
            print(f"{x['doc_id']}  {x['client_id'] or 'me':<13} {x['state']:<10} {x['verification']:<10} {x['sensitivity']:<6} {x['doc_type']:<26} {x['name']}  {('— ' + x['note']) if x['note'] else ''}")
    elif d == "approve":
        print(docs.approve(args.doc_id))
    elif d == "exclude":
        print(docs.exclude(args.doc_id))
    elif d == "type":
        print(docs.set_type(args.doc_id, args.doc_type))
    elif d == "types":
        print("\n".join(f"{k:<26} {v[0]}" for k, v in DOC_TYPES.items()))
    return 0


def add_parser(sub) -> None:
    b = sub.add_parser("business", help="Business Brain: your verified business facts, your clients and authorized documents (local only)")
    bs = b.add_subparsers(dest="biz_cmd", required=True)

    def with_client(p):
        p.add_argument("--client", help="a client's id or exact name (default: your own business)")
        return p

    for name in ("status", "enable", "disable", "backup"):
        bs.add_parser(name)
    with_client(bs.add_parser("fields"))
    with_client(bs.add_parser("export"))
    with_client(bs.add_parser("missing", help="list the fields that have no value yet"))
    x = with_client(bs.add_parser("onboard", help="guided entry: asks field by field, blank = skip, nothing is guessed"))
    x.add_argument("--only", nargs="*", help="only these field keys")
    bs.add_parser("prune", help="delete audit rows older than --days (the only way audit rows are ever removed)").add_argument("--days", type=int, default=365)
    x = with_client(bs.add_parser("list"))
    x.add_argument("--all", action="store_true", help="include fields with no value")
    x = with_client(bs.add_parser("show"))
    x.add_argument("key")
    x = with_client(bs.add_parser("set"))
    x.add_argument("key")
    x.add_argument("value", nargs="?")
    x.add_argument("--replace", action="store_true", help="replace the current value (asks for confirmation)")
    x.add_argument("--sensitivity", choices=["LOW", "MEDIUM", "HIGH"])
    x.add_argument("--allow-lower", action="store_true", help="allow a LOWER sensitivity than the field's default")
    for name in ("confirm", "reject", "delete", "reveal"):
        bs.add_parser(name).add_argument("fact_id")
    x = with_client(bs.add_parser("resolve"))
    x.add_argument("key")
    x.add_argument("fact_id")
    x = with_client(bs.add_parser("import", help="import a workbook (.xlsx, primary), a CSV file/folder, or a masked-JSON fact file"))
    x.add_argument("file", help="the workbook (.xlsx), a .csv file or a folder of CSV files; a .json file imports facts for --client or your own business")
    x.add_argument("--assume-verified", action="store_true", help="you vouch that every value in the file is correct")
    x.add_argument("--dry-run", action="store_true", help="show the preview and stop: nothing is written")
    x.add_argument("--sheet", help="for a single .csv file: which sheet it is (e.g. Clients)")
    x.add_argument("--map", action="append", metavar='"Sheet:Header=column"', help="confirm a column mapping (repeatable)")
    x.add_argument("--skip", action="append", metavar='"Sheet:Header"', help="do not import this column (repeatable)")
    x.add_argument("--decide", action="append", metavar="ID=choice", help="answer a question non-interactively: a conflict (keep|sheet) or duplicate client (same|separate) id shown in the preview")
    x.add_argument("--on-conflict", choices=["ask", "keep"], default="ask", help="keep = never replace an existing value (safe default for unattended runs)")
    x.add_argument("--accept-errors", action="store_true", help="leave out the rows/cells with problems and import the rest")
    x.add_argument("--confirm-sensitive", action="store_true", help="explicitly allow protected (HIGH) values to be stored")
    x.add_argument("--yes", action="store_true", help="skip the final typed confirmation (decisions are still required)")
    bs.add_parser("imports", help="list spreadsheet imports (what was imported, when, verified or not)")
    bs.add_parser("import-verify", help="vouch for everything a past import added").add_argument("session_id")
    x = with_client(bs.add_parser("source", help="where did this value come from? (workbook, sheet, row, import) - never shows the value"))
    x.add_argument("key")
    x = bs.add_parser("records", help="a client's services, locations and SEO targets with where each came from")
    x.add_argument("--client", required=True)
    x.add_argument("--kind", choices=["services", "locations", "seo"])
    x = bs.add_parser("template", help="write the blank OCTG workbook to fill in")
    x.add_argument("path", nargs="?", help="a file name or a folder (default: the current folder)")
    x.add_argument("--force", action="store_true", help="overwrite an existing file (careful: it may be your filled-in workbook)")
    x = with_client(bs.add_parser("audit"))
    x.add_argument("--task")
    x.add_argument("--tail", type=int, default=40)

    cc = bs.add_parser("client", help="your clients (each one separate from you and from each other)")
    cs = cc.add_subparsers(dest="client_cmd", required=True)
    x = cs.add_parser("create")
    x.add_argument("name")
    x.add_argument("--allow-duplicate", action="store_true")
    x = cs.add_parser("list")
    x.add_argument("--all", action="store_true", help="include archived clients")
    for name in ("show", "archive", "restore"):
        cs.add_parser(name).add_argument("client_ref", metavar="client")
    x = cs.add_parser("rename")
    x.add_argument("client_ref", metavar="client")
    x.add_argument("new_name")

    rr = bs.add_parser("research", help="research observations (storage only; no research engine yet)")
    rs = rr.add_subparsers(dest="res_cmd", required=True)
    x = rs.add_parser("list")
    x.add_argument("--client", required=True)
    x.add_argument("--limit", type=int, default=50)

    dd = bs.add_parser("docs", help="authorized documents (explicit enrolment; one scope per folder)")
    ds = dd.add_subparsers(dest="docs_cmd", required=True)
    x = with_client(ds.add_parser("enroll"))
    x.add_argument("path")
    x.add_argument("--label")
    ds.add_parser("revoke").add_argument("path")
    ds.add_parser("scan")
    ds.add_parser("types")
    x = ds.add_parser("index")
    x.add_argument("doc_id", nargs="?")
    x = with_client(ds.add_parser("list"))
    x.add_argument("--search")
    for name in ("approve", "exclude"):
        ds.add_parser(name).add_argument("doc_id")
    x = ds.add_parser("type")
    x.add_argument("doc_id")
    x.add_argument("doc_type")
    b.set_defaults(fn=cmd_business)
