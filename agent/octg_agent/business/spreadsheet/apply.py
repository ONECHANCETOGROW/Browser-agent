"""Write a confirmed plan. Everything below runs inside ONE transaction opened by the caller: if anything raises, nothing at all is kept.

Verification: values enter as UNVERIFIED unless the user vouched for the workbook (`assume_verified`); a value the user explicitly chose at a conflict ("use spreadsheet value") is VERIFIED
(it is the user's own decision). A value is never copied between clients or from the owner: each change names exactly one scope.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .. import records as R
from ..store import BusinessStore, StoreError
from .diff import CONFLICT, NEW, UNCHANGED, UPDATE, Change, ClientPlan, Diff, RecChange
from .parse import Parsed

SOURCE_TYPE = "IMPORT"


def _src(session_id: str, sheet: str, row: int) -> str:
    return f"{session_id} spreadsheet {sheet} row {row}"


def record_label(rec) -> tuple[str, str]:
    """(label, summary) of a structured row for the import's item list: the main text (a service / keyword / place is LOW) and its priority; never the notes."""
    kind = rec.kind
    main = {"service": rec.rec.get("service"), "location": rec.rec.get("location") or rec.rec.get("city"), "seo": rec.rec.get("keyword")}[kind]
    label = {"service": "Service", "location": "Location", "seo": "SEO target"}[kind]
    extra = " · ".join(str(x) for x in (rec.rec.get("location") if kind == "seo" else None, rec.rec.get("priority")) if x)
    return f"{label}", f"{main}" + (f" ({extra})" if extra else "")


def apply_plan(store: BusinessStore, c: sqlite3.Connection, parsed: Parsed, diff: Diff, *, session_id: str, assume_verified: bool, actor: str = "import",
               rejected: list[dict] | None = None) -> dict:
    now = store._clock()
    workbook = parsed.workbook[:80]
    default_v = "VERIFIED" if assume_verified else "UNVERIFIED"
    n = {"clients_created": 0, "clients_linked": 0, "clients_renamed": 0, "facts_new": 0, "facts_updated": 0, "facts_chosen": 0, "facts_kept": 0, "facts_unchanged": 0, "facts_skipped": 0,
         "records_new": 0, "records_updated": 0, "records_chosen": 0, "records_kept": 0, "records_unchanged": 0, "items_rejected": 0}
    c.execute("INSERT INTO import_sessions (session_id,workbook,file_sha256,created_at,status,assume_verified,actor,kind,scope,verification,last_verified) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (session_id, workbook, parsed.sha256, now, "committing", int(assume_verified), actor, parsed.kind, parsed.scope, "VERIFIED" if assume_verified else "UNVERIFIED",
               now if assume_verified else None))

    def item(kind: str, scope: str, label: str, key: str, sens: str, mask: str, decision: str, sheet: str, row: int, detail: str = "") -> None:
        c.execute("INSERT INTO import_items (session_id,item_kind,scope,label,field_key,sensitivity,mask,decision,source_sheet,source_row,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (session_id, kind, "" if scope == "me" else scope, label[:120], key[:80], sens, mask[:120], decision, sheet[:80], row, detail[:200]))

    for r in rejected or []:
        item(r.get("kind", "value"), r.get("scope", ""), r.get("label", ""), r.get("key", ""), r.get("sensitivity", "LOW"), r.get("mask", ""), "rejected", r.get("sheet", ""), r.get("row", 0), r.get("detail", "left out by the reviewer"))
    n["items_rejected"] = len(rejected or [])

    def fact(ch: Change, scope: str) -> None:
        lab, sh, rw = ch.fact.prep.pk.label, ch.fact.sheet, ch.fact.row
        if ch.action == UNCHANGED:
            n["facts_unchanged"] += 1
            item("value", scope, lab, ch.key, ch.sens, ch.prep.mask, "unchanged", sh, rw)
            return
        if ch.action == "SKIPPED":
            n["facts_skipped"] += 1
            item("value", scope, lab, ch.key, ch.sens, ch.prep.mask, "skipped", sh, rw, "rejected or replaced earlier: not added again")
            return
        verification, supersede = default_v, None
        if ch.action == CONFLICT:
            if diff.decision(ch.cid) != "sheet":
                store.audit("import_kept_existing", field_key=ch.key, scope=scope, actor=actor, outcome="kept", detail=session_id, c=c)
                n["facts_kept"] += 1
                item("value", scope, lab, ch.key, ch.sens, ch.prep.mask, "kept existing", sh, rw)
                return
            verification, supersede = "VERIFIED", ch.existing_ids
            n["facts_chosen"] += 1
            item("value", scope, lab, ch.key, ch.sens, ch.prep.mask, "replaced", sh, rw, "you chose the imported value over the existing one")
        elif ch.action == UPDATE:
            supersede = ch.existing_ids
            n["facts_updated"] += 1
            item("value", scope, lab, ch.key, ch.sens, ch.prep.mask, "updated", sh, rw)
        else:
            n["facts_new"] += 1
            item("value", scope, lab, ch.key, ch.sens, ch.prep.mask, "added", sh, rw)
        fid = store.insert_fact(c, ch.prep, scope=scope, source_type=SOURCE_TYPE, source_ref=_src(session_id, ch.fact.sheet, ch.fact.row), verification=verification,
                                supersede=supersede, actor=actor, detail=f"{session_id} {ch.fact.sheet} row {ch.fact.row}")
        c.execute("INSERT INTO fact_sources (fact_id,session_id,workbook,sheet,row_num,column_name,imported_at) VALUES (?,?,?,?,?,?,?)",
                  (fid, session_id, workbook, ch.fact.sheet, ch.fact.row, ch.fact.col, now))

    for ch in diff.owner:
        fact(ch, "me")

    ids: dict[str, str] = {}
    for pl in diff.plans:
        if pl.action == "PENDING":
            raise StoreError("UNRESOLVED", "a duplicate-client decision is still needed")
        if pl.action == "NEW":
            cid = store.insert_client(c, pl.row.name, pl.row.ref or None, actor=actor)
            n["clients_created"] += 1
        else:
            cid = pl.client_id or ""
            if pl.link_ref and pl.row.ref:
                store.update_client(c, cid, ref=pl.row.ref, actor=actor)
                n["clients_linked"] += 1
            if pl.new_name:
                store.update_client(c, cid, name=pl.new_name, actor=actor)
                n["clients_renamed"] += 1
        ids[pl.row.ident] = cid
        if pl.action == "NEW":
            item("client", cid, "Client", "client", "MEDIUM", pl.row.name[:2] + "•••", "added", "Clients", pl.row.row)
        for ch in pl.changes:
            fact(ch, cid)

    for rc in diff.records:
        rlabel, rsum = record_label(rc.rec)
        if rc.action in ("PENDING", UNCHANGED):
            n["records_unchanged"] += 1 if rc.action == UNCHANGED else 0
            if rc.action == UNCHANGED:
                item("record", rc.client_id or ids.get(rc.rec.ref_key, ""), rlabel, rc.rec.kind, "LOW", rsum, "unchanged", rc.rec.sheet, rc.rec.row)
            continue
        cid = rc.client_id or ids.get(rc.rec.ref_key)
        if not cid:
            raise StoreError("UNRESOLVED", "a row names a client that was not created")
        verification, supersede = default_v, None
        if rc.action == CONFLICT:
            if diff.decision(rc.cid) != "sheet":
                store.audit("import_kept_existing", scope=cid, actor=actor, outcome="kept", detail=f"{rc.rec.kind} {session_id}", c=c)
                n["records_kept"] += 1
                item("record", cid, rlabel, rc.rec.kind, "LOW", rsum, "kept existing", rc.rec.sheet, rc.rec.row)
                continue
            verification, supersede = "VERIFIED", rc.existing_id
            n["records_chosen"] += 1
            item("record", cid, rlabel, rc.rec.kind, "LOW", rsum, "replaced", rc.rec.sheet, rc.rec.row, "you chose the imported row over the existing one")
        elif rc.action == UPDATE:
            supersede = rc.existing_id
            n["records_updated"] += 1
            item("record", cid, rlabel, rc.rec.kind, "LOW", rsum, "updated", rc.rec.sheet, rc.rec.row)
        else:
            n["records_new"] += 1
            item("record", cid, rlabel, rc.rec.kind, "LOW", rsum, "added", rc.rec.sheet, rc.rec.row)
        R.insert(c, rc.rec.kind, cid, rc.rec.rec, verification=verification, sheet=rc.rec.sheet, workbook=workbook, session_id=session_id, row_num=rc.rec.row, now=now, supersede=supersede)
        store.audit("record_" + ("added" if rc.action == NEW else "replaced"), scope=cid, actor=actor, outcome=verification, detail=f"{rc.rec.kind} {session_id} {rc.rec.sheet} row {rc.rec.row}", c=c)

    c.execute("UPDATE import_sessions SET status='committed', committed_at=?, counts=? WHERE session_id=?", (store._clock(), json.dumps(n), session_id))
    store.audit("import_committed", actor=actor, outcome="committed", detail=f"{session_id} " + " ".join(f"{k}={v}" for k, v in n.items() if v), c=c)
    return n
