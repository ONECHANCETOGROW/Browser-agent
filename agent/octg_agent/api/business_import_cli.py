"""The terminal side of the spreadsheet import: preview, questions, explicit confirmation. Local and human-only (like the rest of `business ...`).

`ask(prompt, hidden)` and `say(text)` are injected so the whole flow is testable. Nothing here prints a HIGH value (masks only) and nothing is written until the user has typed a confirmation
(or passed --yes together with every decision it needs).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..business.spreadsheet import ImportRefused, Preview, SpreadsheetImporter, parse_map_arg, parse_skip_arg
from ..business.store import BusinessStore, StoreError

Ask = Callable[[str, bool], str]
Say = Callable[[str], None]


@dataclass
class ImportOptions:
    sheet: str | None = None
    dry_run: bool = False
    maps: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    decide: list[str] = field(default_factory=list)
    on_conflict: str = "ask"            # ask | keep
    yes: bool = False
    accept_errors: bool = False
    confirm_sensitive: bool = False
    assume_verified: bool = False
    interactive: bool = True


def _n(label: str, count: int, plural: str | None = None) -> str:
    return f"{count} {label if count == 1 else (plural or label + 's')}"


def render_preview(pv: Preview, say: Say, reveal: str = "MEDIUM") -> None:
    r = pv.to_dict(reveal)
    det = r["detected"]
    say("")
    say("IMPORT PREVIEW")
    say(f"Workbook:  {r['workbook']}   (fingerprint {r['sha256']})")
    say("Detected: " + " | ".join([_n("business profile", det["business_profile"])] + [_n("client", det["clients"]), _n("service", det["services"]), _n("location", det["locations"]),
                                                                                        _n("SEO target", det["seo_targets"])]))
    say("")
    say("Sheets and columns:")
    for s in r["sheets"]:
        extra = f" ({s['rows']} row(s))" if s["status"] == "read" else ""
        say(f"  {s['sheet']:<18} {s['status']}{extra}")
        for c in s["columns"]:
            if c["status"] in ("CONFIDENT", "CONFIRMED"):
                continue
            say(f"      {c['status']:<19} '{c['header']}'  {c['note']}")
        if s["status"] == "read" and s["missing_columns"]:
            say(f"      not in this sheet: {', '.join(s['missing_columns'])}")
        if s["status"] == "read":
            say(f"      recognised: {', '.join(s['recognized']) or '-'}")
    for w in r["warnings"]:
        say(f"  note: {w}")
    say("")
    rec = r["records"]
    new = [f"+ {rec['clients']['new']} client(s)"] + [f"+ {rec[k]['new']} {k.replace('_', ' ')}" for k in ("services", "locations", "seo_targets") if rec[k]["new"]]
    say("NEW        " + (", ".join(new) if any(v for v in [rec["clients"]["new"], rec["services"]["new"], rec["locations"]["new"], rec["seo_targets"]["new"]]) else "nothing"))
    upd = rec["clients"]["updated"] + rec["services"]["updated"] + rec["locations"]["updated"] + rec["seo_targets"]["updated"]
    say(f"UPDATED    ~ {rec['clients']['updated']} client record(s), {rec['services']['updated'] + rec['locations']['updated'] + rec['seo_targets']['updated']} other row(s)" if upd else "UPDATED    nothing")
    same = rec["clients"]["unchanged"] + rec["services"]["unchanged"] + rec["locations"]["unchanged"] + rec["seo_targets"]["unchanged"]
    say(f"UNCHANGED  = {rec['clients']['unchanged']} client record(s), {same - rec['clients']['unchanged']} other row(s)" if same else "UNCHANGED  nothing")
    if r["owner"]:
        say(f"MY BUSINESS  {r['owner']['state'].lower()} ({r['owner']['facts']} value(s) in the sheet)")
    f = r["facts"]
    say(f"VALUES     {f['new']} new, {f['updated']} updated, {f['unchanged']} unchanged, {f['conflict']} in conflict, {f['skipped']} not re-added (rejected/replaced earlier)")
    say(f"CONFLICTS  ! {len(r['conflicts'])}")
    for c in r["conflicts"][:20]:
        _show_conflict(c, say)
        say(f"    decision: {c['decision'] or 'not decided yet'}")
    if len(r["conflicts"]) > 20:
        say(f"    ... and {len(r['conflicts']) - 20} more conflict(s)")
    say(f"DUPLICATES ! {len(r['duplicates'])} possible duplicate client(s)")
    for x in r["duplicates"][:20]:
        say(f"    [{x['id']}] '{x['spreadsheet_client']}' may be '{x['possible_duplicate_of']}' ({', '.join(x['because'])}): {x['decision'] or 'not decided yet'}" + (f"  -- {x['problem']}" if x["problem"] else ""))
    m = r["missing"]
    say(f"MISSING    ? {m['fields']} field(s), {m['values']} value(s) have no value anywhere (they stay missing: nothing is guessed)")
    s = r["sensitive"]
    say(f"SENSITIVE  ! {s['count']} protected value(s) in this workbook (encrypted when stored; never shown to a model)")
    for it in s["items"][:12]:
        say(f"      {it['scope']}: {it['field']}  [{it['action']}]  {it['value']}  (sheet {it['sheet']}, row {it['row']})")
    errs = [i for i in r["issues"] if i["level"] == "error"]
    warn = [i for i in r["issues"] if i["level"] != "error"]
    if errs:
        say("")
        say(f"PROBLEMS   {len(errs)} row(s)/cell(s) cannot be imported and would be left out:")
        for i in errs[:25]:
            say(f"      {i['sheet']} row {i['row']} {i['column']}: {i['message']}")
        if len(errs) > 25:
            say(f"      ... and {len(errs) - 25} more")
    if warn:
        say("")
        say(f"NOTES      {len(warn)}")
        for i in warn[:12]:
            say(f"      {i['sheet']} row {i['row']} {i['column']}: {i['message']}")
    say("")


def _show_conflict(c: dict, say: Say) -> None:
    say("")
    say(f"CONFLICT [{c['id']}]  {c['field']}  ({c['client']})")
    if c.get("differences"):
        for d in c["differences"]:
            say(f"    {d['attribute']}: existing '{d['existing']}'  |  spreadsheet '{d['spreadsheet']}'")
    else:
        say(f"    Existing value:     {c['existing']}")
        say(f"    Spreadsheet value:  {c['spreadsheet']}")
    if c["sensitivity"] == "HIGH":
        say("    (protected: masked)")


def _ask_choice(ask: Ask, prompt: str, options: dict[str, str], default: str | None = None) -> str:
    while True:
        a = ask(prompt, False).strip().lower()
        if not a and default:
            return default
        if a in options:
            return options[a]
        if a in options.values():
            return a


def resolve_interactively(pv: Preview, opts: ImportOptions, ask: Ask, say: Say) -> None:
    # 1. column mappings that could mean different things
    for m in list(pv.parsed.pending_maps()):
        if not opts.interactive:
            continue
        cands = list(m.candidates)
        say("")
        say(f"NEEDS CONFIRMATION  sheet '{m.sheet}', column '{m.header}': {m.note}")
        if len(cands) == 1 and m.note.startswith("'"):
            say("   two columns would fill the same field: skip one of them.")
        for i, cn in enumerate(cands, 1):
            say(f"   [{i}] it means: {cn}")
        say("   [s] skip this column (not imported)   [c] cancel the whole import")
        opts_map = {str(i): cn for i, cn in enumerate(cands, 1)}
        opts_map.update({"s": "skip", "c": "cancel"})
        a = _ask_choice(ask, "choose: ", opts_map)
        if a == "cancel":
            raise ImportRefused("CANCELLED", "import cancelled")
        if a == "skip":
            pv.skip_column(m.sheet, m.header)
        else:
            pv.map_column(m.sheet, m.header, a)
    # 2. non-interactive decisions from --decide
    for d in opts.decide:
        cid, _, choice = d.partition("=")
        pv.decide(cid.strip(), choice.strip())
    if opts.on_conflict == "keep":
        pv.keep_all_existing()
    if not opts.interactive:
        return
    # 3. possible duplicate clients (before conflicts: "same client" changes what is compared)
    progressed = True
    while progressed:
        progressed = False
        for dup in pv.diff.duplicates:
            if pv.decisions.get(dup.cid) in dup.choices:
                continue
            say("")
            say(f"POSSIBLE DUPLICATE [{dup.cid}]")
            say(f"    Spreadsheet:  {dup.sheet_name}   (id {dup.sheet_ref or '-'}, row {dup.sheet_row})")
            say(f"    {'Also in the workbook' if dup.other_row else 'Existing'}:     {dup.existing_name}   (id {dup.existing_ref or dup.existing_id or '-'}{', row ' + str(dup.other_row) if dup.other_row else ''})")
            say(f"    Because:      {', '.join(dup.signals)}")
            if dup.problem:
                say(f"    {dup.problem}")
            if not dup.choices:
                continue
            menu = {"same": "[s] the SAME client (link them; update the existing client)", "separate": "[n] a DIFFERENT client (create a separate client)", "cancel": "[c] cancel the import"}
            for ch in dup.choices:
                say("    " + menu[ch])
            a = _ask_choice(ask, "choose: ", {"s": "same", "n": "separate", "c": "cancel"})
            if a not in dup.choices:
                say("    that choice is not available here")
                continue
            pv.decide(dup.cid, a)
            progressed = True
            break
    # 4. conflicts
    for c in list(pv.diff.conflicts):
        if pv.decisions.get(c.cid) in c.choices:
            continue
        cd = c.to_dict("MEDIUM", None)
        _show_conflict(cd, say)
        say("    [k] keep the existing value (default)   [u] use the spreadsheet value   [c] cancel the import")
        a = _ask_choice(ask, "choose: ", {"k": "keep", "u": "sheet", "c": "cancel"}, default="keep")
        if a == "sheet" and c.sens == "HIGH":
            if ask("    This replaces a PROTECTED value. Type REPLACE to confirm: ", False).strip() != "REPLACE":
                say("    kept the existing value")
                a = "keep"
        pv.decide(c.cid, a)


def run_import(path: str, store: BusinessStore, opts: ImportOptions, ask: Ask, say: Say) -> int:
    imp = SpreadsheetImporter(store)
    pv: Preview | None = None
    try:
        ov: dict = {}
        sk: dict = {}
        for m in opts.maps:
            k, h, col = parse_map_arg(m)
            ov.setdefault(k, {})[h] = col
        for s in opts.skips:
            k, h = parse_skip_arg(s)
            sk.setdefault(k, set()).add(h)
        pv = imp.analyze(path, sheet=opts.sheet, overrides=ov, skips=sk)
        try:
            resolve_interactively(pv, opts, ask, say)
        except ImportRefused as e:
            if e.code == "CANCELLED":
                say("Cancelled. Nothing was changed.")
                return 1
            raise
        render_preview(pv, say)
        if any(v == "cancel" for v in pv.decisions.values()):
            say("Cancelled. Nothing was changed.")
            return 1
        if opts.dry_run:
            why = pv.blocking(accept_errors=True)
            say("DRY RUN: nothing was written." + (f" Before a real import you still need to settle: {'; '.join(why)}" if why else " This import could be confirmed as it stands."))
            for cid in pv.unresolved():
                say(f"   to decide: {cid}  (re-run with --decide {cid}=<choice>, or answer the questions in a normal run)")
            return 0
        why = pv.blocking(accept_errors=opts.accept_errors or False)
        errs = pv.errors()
        if errs and not opts.accept_errors:
            if opts.interactive and not [w for w in why if "problem(s)" not in w]:
                if ask(f"{len(errs)} problem(s) above would be LEFT OUT of the import. Type LEAVE OUT to continue without them (anything else cancels): ", False).strip() != "LEAVE OUT":
                    say("Cancelled. Nothing was changed.")
                    return 1
                opts.accept_errors = True
                why = pv.blocking(accept_errors=True)
        if why and pv.actionable() == 0 and not pv.unresolved() and not pv.parsed.pending_maps() and not (errs and not opts.accept_errors):
            say("Nothing to import: everything in the workbook is already stored (or was left out). Nothing was changed.")
            return 0
        if why:
            say("Cannot import yet: " + "; ".join(why))
            say("Nothing was changed.")
            return 2
        high = pv.diff.high_writes()
        if not opts.yes:
            if not opts.interactive:
                say("Not confirmed (non-interactive run): add --yes to confirm. Nothing was changed.")
                return 2
            n = pv.actionable()
            if ask(f"Write these {n} change(s) to the Business Brain? A backup is made first. Type IMPORT to confirm (anything else cancels): ", False).strip() != "IMPORT":
                say("Cancelled. Nothing was changed.")
                return 1
        confirm_sensitive = opts.confirm_sensitive
        if high and not confirm_sensitive:
            if not opts.interactive:
                say("This import stores protected values: add --confirm-sensitive. Nothing was changed.")
                return 2
            if ask(f"{high} protected value(s) will be stored encrypted (masked above). Type PROTECT to confirm: ", False).strip() != "PROTECT":
                say("Cancelled. Nothing was changed.")
                return 1
            confirm_sensitive = True
        verified = opts.assume_verified
        if not verified and opts.interactive and not opts.yes:
            verified = ask("Do you vouch that the values in this workbook are correct, so tasks can use them straight away? (y = mark them VERIFIED, Enter = leave them UNVERIFIED until you confirm them) [y/N]: ", False).strip().lower() in ("y", "yes")
        res = imp.commit(pv, assume_verified=verified, confirm_sensitive=confirm_sensitive, accept_errors=opts.accept_errors)
        c = res["counts"]
        say("")
        say(f"IMPORTED  (import {res['session_id']})")
        say(f"  clients created {c['clients_created']}, linked to an existing client {c['clients_linked']}, renamed {c['clients_renamed']}")
        say(f"  values: {c['facts_new']} new, {c['facts_updated']} updated, {c['facts_chosen']} replaced by your choice, {c['facts_kept']} kept as they were, {c['facts_unchanged']} unchanged")
        say(f"  rows:   {c['records_new']} new, {c['records_updated']} updated, {c['records_chosen']} replaced by your choice, {c['records_kept']} kept")
        say(f"  state:  {'VERIFIED (you vouched for the workbook)' if verified else 'UNVERIFIED: confirm them with  business import-verify ' + res['session_id'] + '  or one by one with  business confirm <fact_id>'}")
        say(f"  backup taken before writing: {res['backup']}")
        say(f"  where did a value come from?   python -m octg_agent business source <field> [--client <client>]")
        return 0
    except ImportRefused as e:
        say(f"Refused: {e.message}")
        return 1
    except StoreError as e:
        say(f"Refused: {e.message}")
        return 1
    finally:
        if pv is not None:
            pv.close()
