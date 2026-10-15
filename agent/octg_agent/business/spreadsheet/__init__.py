"""Spreadsheet import: Excel (primary) / CSV  ->  validate -> map columns -> preview -> detect conflicts, duplicates, missing and sensitive data -> user confirms -> Business / Client Brain.

    importer = SpreadsheetImporter(store)
    preview  = importer.analyze("OCTG_Business_Data.xlsx")        # reads the file, writes NOTHING
    preview.to_dict()                                              # masked: what the CLI / panel shows
    preview.decide(conflict_id, "keep" | "sheet")                  # the user's choices
    importer.commit(preview, assume_verified=False, confirm_sensitive=True)   # one transaction, after an automatic backup

The workbook is an INPUT, never a model's output: cells are data, never instructions, and nothing parsed is ever sent to a model. Raw HIGH values live only in the in-memory plan
(and are registered with the log scrubber while it exists); every preview, audit row, log line and error shows masks only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...sensitive import SECRETS
from .. import records as R
from ..store import BusinessStore, StoreError
from . import layout as L
from .apply import apply_plan
from .csvio import read_csv_source
from .diff import CONFLICT, NEW, UNCHANGED, UPDATE, Differ, Diff
from .parse import Issue, Parsed, Parser
from .template import TEMPLATE_NAME, write_template
from .xlsx import Limits, Workbook, XlsxError, read_xlsx

__all__ = ["SpreadsheetImporter", "Preview", "ImportRefused", "write_template", "TEMPLATE_NAME", "Limits", "XlsxError"]


class ImportRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def parse_map_arg(text: str) -> tuple[str, str, str]:
    """'Clients:Business Name=business_name' -> (layout key, hnorm(header), column)."""
    try:
        left, col = text.rsplit("=", 1)
        sheet, header = left.split(":", 1)
    except ValueError:
        raise ImportRefused("BAD_MAP", f"--map expects \"Sheet:Header=column\", got {text!r}") from None
    d = L.sheet_for_name(sheet.strip())
    if d is None:
        raise ImportRefused("BAD_MAP", f"unknown sheet {sheet.strip()!r} in --map")
    if d.col(col.strip()) is None:
        raise ImportRefused("BAD_MAP", f"{col.strip()!r} is not a column of '{d.name}' (columns: {', '.join(c.name for c in d.cols)})")
    return d.key, L.hnorm(header), col.strip()


def parse_skip_arg(text: str) -> tuple[str, str]:
    try:
        sheet, header = text.split(":", 1)
    except ValueError:
        raise ImportRefused("BAD_SKIP", f"--skip expects \"Sheet:Header\", got {text!r}") from None
    d = L.sheet_for_name(sheet.strip())
    if d is None:
        raise ImportRefused("BAD_SKIP", f"unknown sheet {sheet.strip()!r} in --skip")
    return d.key, L.hnorm(header)


@dataclass
class Preview:
    importer: "SpreadsheetImporter"
    parsed: Parsed
    diff: Diff
    workbook: Workbook
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    skips: dict[str, set[str]] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    excluded: set[str] = field(default_factory=set)      # values / rows the reviewer left out (by uid); they are not compared, not written, and recorded as rejected

    # ------------------------------------------------------------------ what is actually imported
    def effective(self) -> Parsed:
        """The plan minus what the reviewer excluded. Everything (comparison, conflicts, commit) works on this."""
        if not self.excluded:
            return self.parsed
        p, ex = self.parsed, self.excluded
        return dataclasses.replace(p, owner=[f for f in p.owner if f.uid not in ex],
                                   clients=[dataclasses.replace(c, facts=[f for f in c.facts if f.uid not in ex]) for c in p.clients],
                                   records=[r for r in p.records if r.uid not in ex])

    def _all_uids(self) -> set[str]:
        p = self.parsed
        return {f.uid for f in p.owner} | {f.uid for c in p.clients for f in c.facts} | {r.uid for r in p.records}

    def exclude(self, uid: str) -> "Preview":
        if uid not in self._all_uids():
            raise ImportRefused("UNKNOWN_ITEM", "there is no such item in this import")
        self.excluded.add(uid)
        self.diff = self.importer._diff(self.effective(), self.decisions)
        return self

    def include(self, uid: str) -> "Preview":
        self.excluded.discard(uid)
        self.diff = self.importer._diff(self.effective(), self.decisions)
        return self

    # ------------------------------------------------------------------ decisions
    def decide(self, cid: str, choice: str) -> "Preview":
        ok = {d.cid: d.choices for d in self.diff.duplicates}
        ok.update({c.cid: c.choices for c in self.diff.conflicts})
        if cid not in ok:
            raise ImportRefused("UNKNOWN_DECISION", f"there is nothing to decide for {cid!r}")
        if choice != "cancel" and choice not in ok[cid]:
            raise ImportRefused("BAD_CHOICE", f"for {cid} choose one of: {', '.join(list(ok[cid]) + ['cancel'])}")
        self.decisions[cid] = choice
        self.diff = self.importer._diff(self.effective(), self.decisions)   # a decision (a duplicate "same client") can change what is compared
        return self

    def map_column(self, sheet: str, header: str, column: str) -> "Preview":
        d = L.sheet_for_name(sheet)
        if d is None or d.col(column) is None:
            raise ImportRefused("BAD_MAP", f"cannot map {header!r} to {column!r} on {sheet!r}")
        self.overrides.setdefault(d.key, {})[L.hnorm(header)] = column
        return self.importer.refresh(self)

    def skip_column(self, sheet: str, header: str) -> "Preview":
        d = L.sheet_for_name(sheet)
        if d is None:
            raise ImportRefused("BAD_SKIP", f"unknown sheet {sheet!r}")
        self.skips.setdefault(d.key, set()).add(L.hnorm(header))
        return self.importer.refresh(self)

    def keep_all_existing(self) -> "Preview":
        """Resolve every conflict as 'keep the existing value' (the safe default); duplicate-client questions stay open."""
        for c in self.diff.conflicts:
            self.decisions.setdefault(c.cid, "keep")
        self.diff = self.importer._diff(self.effective(), self.decisions)
        return self

    def close(self) -> None:
        SECRETS.clear(self.parsed.secrets_owner)

    # ------------------------------------------------------------------ state
    def unresolved(self) -> list[str]:
        return self.diff.unresolved()

    def errors(self) -> list[Issue]:
        return self.parsed.errors() + [i for i in self.diff.issues if i.level == "error"]

    def actionable(self) -> int:
        n = sum(1 for ch in self.diff.owner if ch.action in (NEW, UPDATE) or (ch.action == CONFLICT and self.decisions.get(ch.cid) == "sheet"))
        for pl in self.diff.plans:
            if pl.action == "PENDING":
                continue
            n += 1 if (pl.action == "NEW" or pl.link_ref or pl.new_name) else 0
            n += sum(1 for ch in pl.changes if ch.action in (NEW, UPDATE) or (ch.action == CONFLICT and self.decisions.get(ch.cid) == "sheet"))
        n += sum(1 for r in self.diff.records if r.action in (NEW, UPDATE) or (r.action == CONFLICT and self.decisions.get(r.cid) == "sheet"))
        return n

    def blocking(self, accept_errors: bool = False) -> list[str]:
        why: list[str] = []
        if any(v == "cancel" for v in self.decisions.values()):
            why.append("you chose to cancel the import")
        pend = self.parsed.pending_maps()
        if pend:
            why.append(f"{len(pend)} column(s) need your confirmation (map or skip them): " + ", ".join(f"{m.sheet}: '{m.header}'" for m in pend[:6]))
        un = self.unresolved()
        if un:
            why.append(f"{len(un)} decision(s) are needed: " + ", ".join(un[:8]))
        errs = self.errors()
        if errs and not accept_errors:
            why.append(f"{len(errs)} problem(s) were found (rows or cells that cannot be imported); fix the workbook, or confirm that they may be left out")
        if not why and self.actionable() == 0:
            why.append("there is nothing to change: everything in the workbook is already stored or was left out")
        return why

    # ------------------------------------------------------------------ item-by-item review (the Knowledge Hub's review table)
    @staticmethod
    def _rec_text(rec) -> str:
        from .apply import record_label
        label, summary = record_label(rec)
        return f"{label}: {summary}"

    def items(self, reveal: str = "MEDIUM") -> list[dict]:
        """Every value / row of the plan as one reviewable item: what it is, its scope, where it came from, what would happen to it, and whether it is included. HIGH is always a mask."""
        d, out = self.diff, []

        def show(text, mask, sens):
            return text if text is not None and (sens == "LOW" or (reveal == "MEDIUM" and sens == "MEDIUM")) else mask

        def fact(ch, who):
            out.append({"uid": ch.fact.uid, "kind": "value", "scope": who, "field": ch.fact.prep.pk.label, "key": ch.key, "sensitivity": ch.sens, "action": ch.action,
                        "new": show(ch.sheet_text, ch.prep.mask, ch.sens),
                        "existing": (show(ch.sheet_text, ch.prep.mask, ch.sens) if ch.action == UNCHANGED else show(ch.existing_text, ch.existing_mask, ch.sens)) if ch.existing_ids else None,
                        "conflict_id": ch.cid or None, "decision": self.decisions.get(ch.cid) if ch.cid else None, "included": True, "section": ch.fact.sheet, "row": ch.fact.row,
                        "label_in_source": ch.fact.col, "note": ch.note})
        for ch in d.owner:
            fact(ch, "My Business")
        for pl in d.plans:
            for ch in pl.changes:
                fact(ch, pl.row.name)
        for rc in d.records:
            label, summary = self.rec_label_summary(rc.rec)
            out.append({"uid": rc.rec.uid, "kind": "record", "scope": rc.client_label, "field": label, "key": rc.rec.kind, "sensitivity": "LOW", "action": rc.action, "new": summary,
                        "existing": ", ".join(f"{a}: {o}" for a, o, _ in rc.changes) or None, "conflict_id": rc.cid or None, "decision": self.decisions.get(rc.cid) if rc.cid else None,
                        "included": True, "section": rc.rec.sheet, "row": rc.rec.row, "label_in_source": "", "note": ""})
        p = self.parsed
        for f in p.owner + [f for c in p.clients for f in c.facts]:
            if f.uid in self.excluded:
                who = "My Business" if f.scope_ref == "me" else next((c.name for c in p.clients if c.ident == f.scope_ref), "")
                out.append({"uid": f.uid, "kind": "value", "scope": who, "field": f.prep.pk.label, "key": f.key, "sensitivity": f.prep.sens, "action": None, "new": f.prep.mask,
                            "existing": None, "conflict_id": None, "decision": None, "included": False, "section": f.sheet, "row": f.row, "label_in_source": f.col, "note": ""})
        for r in p.records:
            if r.uid in self.excluded:
                label, summary = self.rec_label_summary(r)
                out.append({"uid": r.uid, "kind": "record", "scope": next((c.name for c in p.clients if c.ident == r.ref_key), r.ref), "field": label, "key": r.kind, "sensitivity": "LOW",
                            "action": None, "new": summary, "existing": None, "conflict_id": None, "decision": None, "included": False, "section": r.sheet, "row": r.row, "label_in_source": "", "note": ""})
        return out

    @staticmethod
    def rec_label_summary(rec) -> tuple[str, str]:
        from .apply import record_label
        return record_label(rec)

    def rejected_items(self) -> list[dict]:
        """What the reviewer left out, as masks, for the import's permanent record."""
        return [{"kind": i["kind"], "scope": "unknown", "label": i["field"], "key": i["key"], "sensitivity": i["sensitivity"], "mask": str(i["new"])[:120], "sheet": i["section"], "row": i["row"],
                 "detail": f"{i['scope']}: left out by the reviewer"}
                for i in self.items("LOW") if not i["included"]]

    # ------------------------------------------------------------------ the masked view
    def to_dict(self, reveal: str = "LOW") -> dict:
        """What may be shown. `reveal` = "LOW": only LOW values appear in clear (panel / API); "MEDIUM": LOW and MEDIUM (the local terminal); HIGH values are NEVER shown, only masks."""
        d, p = self.diff, self.parsed
        facts = {"new": 0, "updated": 0, "unchanged": 0, "conflict": 0, "skipped": 0}
        every = d.owner + [c for pl in d.plans for c in pl.changes]
        for ch in every:
            facts[{NEW: "new", UPDATE: "updated", UNCHANGED: "unchanged", CONFLICT: "conflict"}.get(ch.action, "skipped")] += 1

        def tally(items) -> dict:
            t = {"new": 0, "updated": 0, "unchanged": 0, "conflict": 0, "pending": 0}
            for st in items:
                t[{"NEW": "new", "UPDATED": "updated", UPDATE: "updated", UNCHANGED: "unchanged", "CONFLICT": "conflict", "PENDING": "pending"}[st]] += 1
            return t

        owner_state = "CONFLICT" if any(c.action == CONFLICT for c in d.owner) else "UPDATED" if any(c.action in (NEW, UPDATE) for c in d.owner) else "UNCHANGED"
        records = {"clients": tally([pl.state for pl in d.plans])}
        for kind, label in (("service", "services"), ("location", "locations"), ("seo", "seo_targets")):
            records[label] = tally([r.action for r in d.records if r.rec.kind == kind])
        sensitive = [{"scope": ch.client_label or "your own business", "field": ch.fact.prep.pk.label, "sheet": ch.fact.sheet, "row": ch.fact.row, "action": ch.action,
                      "value": ch.prep.mask, "existing": ch.existing_mask or None} for ch in every if ch.sens == "HIGH"]
        return {
            "workbook": p.workbook, "sha256": p.sha256[:12], "kind": p.kind, "scope": p.scope, "excluded": len(self.excluded), "items_total": len(self._all_uids()),
            "sheets": [s.to_dict() for s in p.sheets],
            "detected": p.counts(),
            "records": records, "facts": facts,
            "owner": {"state": owner_state, "facts": len(d.owner)} if p.owner or d.owner else None,
            "clients": [{"name": pl.row.name, "ref": pl.row.ref, "row": pl.row.row, "state": pl.state,
                         "new": sum(1 for c in pl.changes if c.action == NEW), "updated": sum(1 for c in pl.changes if c.action == UPDATE),
                         "conflicts": sum(1 for c in pl.changes if c.action == CONFLICT) + (1 if pl.name_conflict else 0), "unchanged": sum(1 for c in pl.changes if c.action == UNCHANGED)}
                        for pl in d.plans],
            "conflicts": [c.to_dict(reveal, d.decision(c.cid)) for c in d.conflicts],
            "duplicates": [x.to_dict(d.decision(x.cid)) for x in d.duplicates],
            "mapping_needed": [m.to_dict() for m in p.pending_maps()],
            "missing": d.missing,
            "sensitive": {"count": len(sensitive), "items": sensitive},
            "issues": [i.to_dict() for i in (p.issues + d.issues)],
            "warnings": list(p.warnings) + list(self.workbook.warnings),
            "unresolved": self.unresolved(),
            "blocking": self.blocking(),
            "high_writes": d.high_writes(),
        }


class SpreadsheetImporter:
    def __init__(self, store: BusinessStore, limits: Limits = Limits()):
        self.store, self.limits = store, limits

    # ------------------------------------------------------------------ read + plan
    def load(self, source: str | Path | bytes, name: str | None = None, sheet: str | None = None) -> tuple[Workbook, str, str]:
        if isinstance(source, (bytes, bytearray)):
            wb = read_xlsx(bytes(source), self.limits)
            return wb, (name or "uploaded workbook")[:80], hashlib.sha256(bytes(source)).hexdigest()
        path = Path(source)
        if not path.exists():
            raise ImportRefused("NO_FILE", "that file or folder does not exist")
        if path.is_dir() or path.suffix.lower() == ".csv":
            wb = read_csv_source(path, sheet, self.limits)
            h = hashlib.sha256()
            for f in sorted(path.glob("*.csv")) if path.is_dir() else [path]:
                h.update(f.read_bytes())
            return wb, path.name[:80], h.hexdigest()
        if path.suffix.lower() in (".xlsm", ".xlsb", ".xls"):
            raise ImportRefused("UNSUPPORTED", f"{path.suffix} files are not imported (macros / old formats): save an .xlsx copy")
        if path.suffix.lower() != ".xlsx":
            raise ImportRefused("UNSUPPORTED", "give an .xlsx workbook (or a .csv file / folder of CSV files)")
        try:
            data = path.read_bytes() if path.stat().st_size <= self.limits.max_file_bytes else None
        except OSError:
            raise ImportRefused("CANNOT_READ", "the file could not be read (is it open in Excel? close it, or save a copy and import the copy)") from None
        if data is None:
            raise ImportRefused("TOO_LARGE", f"the file is larger than {self.limits.max_file_bytes // (1024 * 1024)} MB")
        return read_xlsx(data, self.limits), path.name[:80], hashlib.sha256(data).hexdigest()

    def analyze(self, source: str | Path | bytes, *, name: str | None = None, sheet: str | None = None, overrides: dict | None = None, skips: dict | None = None,
                decisions: dict | None = None) -> Preview:
        try:
            wb, wname, sha = self.load(source, name, sheet)
        except XlsxError as e:
            raise ImportRefused(e.code, e.message) from None
        ov = {k: dict(v) for k, v in (overrides or {}).items()}
        sk = {k: set(v) for k, v in (skips or {}).items()}
        parsed = Parser(self.store).parse(wb, wname, sha, ov, sk)
        pv = Preview(self, parsed, Diff(), wb, ov, sk, dict(decisions or {}))
        pv.diff = self._diff(pv.effective(), pv.decisions)
        return pv

    def analyze_markdown(self, text: str, *, name: str, scope: str, client_name: str = "") -> Preview:
        """A Markdown knowledge file for ONE scope the user chose ("me" or a client id): deterministic extraction of CANDIDATE values and rows, compared with what is stored.
        Nothing here is read by a model, and the text is data, never instructions."""
        from ..markdown_import import parse_markdown
        parsed = parse_markdown(self.store, text, name=name, scope=scope, client_name=client_name)
        pv = Preview(self, parsed, Diff(), Workbook([], []))
        pv.diff = self._diff(pv.effective(), pv.decisions)
        return pv

    def refresh(self, pv: Preview) -> Preview:
        """Re-read the plan after a column mapping changed (the workbook itself is kept in memory; the file is not read again)."""
        if pv.parsed.kind != "spreadsheet":
            return pv
        SECRETS.clear(pv.parsed.secrets_owner)
        pv.parsed = Parser(self.store).parse(pv.workbook, pv.parsed.workbook, pv.parsed.sha256, pv.overrides, pv.skips)
        pv.diff = self._diff(pv.effective(), pv.decisions)
        return pv

    def _diff(self, parsed: Parsed, decisions: dict) -> Diff:
        with self.store.conn() as c:
            return Differ(self.store, parsed, decisions).run(c)

    # ------------------------------------------------------------------ commit
    def commit(self, pv: Preview, *, assume_verified: bool = False, confirm_sensitive: bool = False, accept_errors: bool = False, actor: str = "cli") -> dict:
        why = pv.blocking(accept_errors)
        if why:
            raise ImportRefused("BLOCKED", "; ".join(why))
        if pv.diff.high_writes() and not confirm_sensitive:
            raise ImportRefused("SENSITIVE_UNCONFIRMED", f"this import would store {pv.diff.high_writes()} protected (HIGH sensitivity) value(s): confirm that explicitly")
        session_id = "imp_" + uuid.uuid4().hex[:8]
        backup = self.store.backup(tag=f"pre-import-{session_id}", keep=30)
        try:
            with self.store.conn() as c:
                c.execute("BEGIN IMMEDIATE")
                fresh = Differ(self.store, pv.effective(), pv.decisions).run(c)
                if fresh.signature() != pv.diff.signature():
                    raise ImportRefused("STALE", "the Business Brain changed since the preview was made: nothing was imported. Run the import again")
                counts = apply_plan(self.store, c, pv.effective(), fresh, session_id=session_id, assume_verified=assume_verified, actor=actor, rejected=pv.rejected_items())
        except ImportRefused:
            raise
        except (StoreError, Exception) as e:  # noqa: BLE001 - whatever failed, the transaction was rolled back: say so, never echo a value
            code = getattr(e, "code", type(e).__name__)
            try:
                self.store.audit("import_failed", actor=actor, outcome="rolled_back", detail=f"{session_id} {code}")
                self.record_outcome(pv, "failed", actor=actor, note=str(code))
            except Exception:  # noqa: BLE001
                pass
            raise ImportRefused("COMMIT_FAILED", f"the import failed ({code}) and NOTHING was changed; a backup was made first: {backup.name}") from None
        finally:
            SECRETS.clear(pv.parsed.secrets_owner)
        return {"session_id": session_id, "backup": str(backup), "counts": counts, "assume_verified": assume_verified}

    def record_outcome(self, pv: Preview, status: str, *, actor: str = "cli", note: str = "") -> str:
        """Remember an import that did NOT happen (rejected by the user, or failed): a row in the Import Center with counts only, never a value."""
        session_id = "imp_" + uuid.uuid4().hex[:8]
        p = pv.parsed
        now = self.store._clock()
        counts = {"items_total": len(pv._all_uids()), "excluded": len(pv.excluded)}
        with self.store.conn() as c:
            c.execute("INSERT INTO import_sessions (session_id,workbook,file_sha256,created_at,status,assume_verified,counts,actor,note,kind,scope,verification) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (session_id, p.workbook[:80], p.sha256, now, status, 0, json.dumps(counts), actor, note[:200], p.kind, p.scope, "UNVERIFIED"))
            self.store.audit("import_" + status, actor=actor, outcome=status, detail=f"{session_id} {p.kind}", scope="" if p.scope in ("", "me") else p.scope, c=c)
        return session_id

    # ------------------------------------------------------------------ afterwards
    def verify_session(self, session_id: str, *, actor: str = "cli") -> dict:
        """The user vouches for everything a past import added (UNVERIFIED -> VERIFIED). Conflicting fields stay conflicting until resolved."""
        now = self.store._clock()
        with self.store.conn() as c:
            s = c.execute("SELECT * FROM import_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not s or s["status"] != "committed":
                raise StoreError("NO_SUCH_IMPORT", f"no committed import {session_id!r}")
            ids = [r["fact_id"] for r in c.execute("SELECT f.fact_id FROM fact_sources s JOIN facts f ON f.fact_id=s.fact_id WHERE s.session_id=? AND f.state='LIVE' AND f.verification<>'VERIFIED'", (session_id,))]
            for fid in ids:
                c.execute("UPDATE facts SET verification='VERIFIED', source_type='USER_CONFIRMED', confirmed_at=?, updated_at=? WHERE fact_id=?", (now, now, fid))
            recs = R.verify_session(c, session_id, now)
            c.execute("UPDATE import_sessions SET verification='VERIFIED', last_verified=?, assume_verified=1 WHERE session_id=?", (now, session_id))
            self.store.audit("import_verified", actor=actor, outcome="verified", detail=f"{session_id} facts={len(ids)} records={recs}", c=c)
        return {"facts": len(ids), "records": recs}

    def sessions(self, limit: int = 20, scope: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM import_sessions", []
        if scope is not None:
            q += " WHERE scope=?"
            args.append("" if scope == "me" else scope)
        with self.store.conn() as c:
            rows = c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._session_dict(r) for r in rows]

    @staticmethod
    def _session_dict(r) -> dict:
        return {"session_id": r["session_id"], "workbook": r["workbook"], "status": r["status"], "created_at": r["created_at"], "committed_at": r["committed_at"],
                "assume_verified": bool(r["assume_verified"]), "counts": json.loads(r["counts"] or "{}"), "sha256": (r["file_sha256"] or "")[:12], "kind": r["kind"] or "spreadsheet",
                "scope": r["scope"] or "", "verification": r["verification"] or "UNVERIFIED", "last_verified": r["last_verified"], "note": r["note"] or ""}

    def session(self, session_id: str) -> dict:
        """One import with every item it handled (masks only)."""
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM import_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not r:
                raise StoreError("NO_SUCH_IMPORT", f"no import {session_id!r}")
            items = [dict(x) for x in c.execute("SELECT item_kind, scope, label, field_key, sensitivity, mask, decision, source_sheet, source_row, detail FROM import_items WHERE session_id=? ORDER BY item_id", (session_id,))]
        return {**self._session_dict(r), "items": items}
