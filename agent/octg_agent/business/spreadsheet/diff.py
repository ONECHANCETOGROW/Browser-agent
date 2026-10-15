"""Parsed rows + the current database -> a reviewable plan. Reads only; nothing is written here.

Rules (the importer NEVER blindly overwrites):
  * a blank cell is "no information": it never deletes, never overwrites, and is reported as MISSING when the field has no value anywhere;
  * a value for a field that has none  -> NEW;   the same value already stored -> UNCHANGED;   a value the user rejected or replaced earlier is not re-added -> SKIPPED;
  * a different value for a field whose current value is VERIFIED (or came from a document, or is already CONFLICTING) -> CONFLICT: the user chooses keep / use spreadsheet;
  * a different value for a field whose only current value is an earlier, still unverified import -> UPDATE (the newer sheet replaces the older sheet);
  * addresses merge by part: parts the sheet leaves blank keep their stored value; a part that differs from a VERIFIED address is a conflict;
  * repeatable fields (services, social links ...) only ever ADD an entry that is not stored yet;
  * a sheet row whose client looks like an existing client (same reference, or the same name / web address) is never merged unless the user says "same client".
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field, replace

from .. import records as R
from ..schema import KeyError_, parse_key
from ..store import BusinessStore, Prepared
from . import layout as L
from .parse import ClientRow, Issue, ParsedFact, ParsedRecord, Parsed

NEW, UPDATE, UNCHANGED, CONFLICT, SKIPPED = "NEW", "UPDATE", "UNCHANGED", "CONFLICT", "SKIPPED"
GENERIC_NAME_WORDS = set("llc inc ltd pvt co corp corporation company services service the and group solutions studio shop store".split())
_WORD = re.compile(r"[a-z0-9]+")


def sid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:6]


def name_key(name: str) -> str:
    words = [w for w in _WORD.findall((name or "").lower()) if w not in GENERIC_NAME_WORDS]
    return " ".join(words) or " ".join(_WORD.findall((name or "").lower()))


def exact_name(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def site_key(url: str) -> str:
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z]+://", "", u)
    u = u.split("#")[0].split("?")[0]
    u = u.removeprefix("www.").rstrip("/")
    return u


@dataclass
class Change:
    scope_ref: str
    client_label: str
    key: str                       # display key (services.primary[2])
    action: str
    sens: str
    fact: ParsedFact
    prep: Prepared                 # what would be written (an address is merged with the stored one)
    existing_ids: list[str] = field(default_factory=list)
    existing_mask: str = ""
    existing_text: str | None = None
    sheet_text: str | None = None
    existing_verification: str = ""
    cid: str = ""
    note: str = ""


@dataclass
class RecChange:
    rec: ParsedRecord
    client_label: str
    client_id: str | None
    action: str
    existing_id: str | None = None
    changes: list[tuple[str, str, str]] = field(default_factory=list)   # (attribute, existing, sheet)
    cid: str = ""


@dataclass
class Conflict:
    cid: str
    kind: str                      # fact | record | name
    label: str
    client: str
    sens: str
    existing_mask: str
    sheet_mask: str
    existing_text: str | None
    sheet_text: str | None
    detail: list[tuple[str, str, str]] = field(default_factory=list)
    choices: tuple[str, ...] = ("keep", "sheet")

    def to_dict(self, reveal: str, decision: str | None) -> dict:
        show = lambda text, mask: text if (text is not None and (self.sens == "LOW" or (reveal == "MEDIUM" and self.sens == "MEDIUM"))) else mask   # noqa: E731
        d = {"id": self.cid, "kind": self.kind, "field": self.label, "client": self.client, "sensitivity": self.sens, "choices": list(self.choices), "decision": decision,
             "existing": show(self.existing_text, self.existing_mask), "spreadsheet": show(self.sheet_text, self.sheet_mask)}
        if self.detail:
            d["differences"] = [{"attribute": a, "existing": o, "spreadsheet": n} for a, o, n in self.detail]
        return d


@dataclass
class Duplicate:
    cid: str
    sheet_ref: str
    sheet_name: str
    sheet_row: int
    existing_id: str | None
    existing_name: str
    existing_ref: str
    other_row: int | None          # for a duplicate inside the workbook
    signals: list[str]
    choices: tuple[str, ...]
    problem: str = ""              # why no safe choice exists (the sheet must be fixed)

    def to_dict(self, decision: str | None) -> dict:
        return {"id": self.cid, "spreadsheet_client": self.sheet_name, "spreadsheet_id": self.sheet_ref, "row": self.sheet_row, "possible_duplicate_of": self.existing_name,
                "existing_client_id": self.existing_id, "existing_ref": self.existing_ref, "other_row": self.other_row, "because": self.signals, "choices": list(self.choices),
                "decision": decision, "problem": self.problem}


@dataclass
class ClientPlan:
    row: ClientRow
    action: str                    # NEW | EXISTING | PENDING (a duplicate decision is needed)
    client_id: str | None = None
    link_ref: bool = False         # the matched client had no reference: store the sheet's
    changes: list[Change] = field(default_factory=list)
    new_name: str | None = None    # rename the client (only after the user chose "use spreadsheet")
    name_conflict: Conflict | None = None
    dup: Duplicate | None = None
    state: str = ""                # NEW | UPDATED | UNCHANGED | CONFLICT | PENDING


@dataclass
class Diff:
    owner: list[Change] = field(default_factory=list)
    plans: list[ClientPlan] = field(default_factory=list)
    records: list[RecChange] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    missing: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)

    def decision(self, cid: str) -> str | None:
        return self.decisions.get(cid)

    def unresolved(self) -> list[str]:
        out = [d.cid for d in self.duplicates if self.decision(d.cid) not in d.choices]
        out += [c.cid for c in self.conflicts if self.decision(c.cid) not in c.choices]
        return out

    def cancelled(self) -> bool:
        return any(v == "cancel" for v in self.decisions.values())

    def signature(self) -> str:
        """What this plan is based on (the stored facts/records it compared with and what it decided to do): re-checked inside the commit transaction."""
        parts = []
        for ch in self.owner + [c for p in self.plans for c in p.changes]:
            parts.append(f"{ch.scope_ref}|{ch.key}|{ch.action}|{','.join(ch.existing_ids)}")
        for r in self.records:
            parts.append(f"{r.rec.kind}|{r.rec.ref_key}|{r.cid or r.rec.row}|{r.action}|{r.existing_id}")
        for p in self.plans:
            parts.append(f"client|{p.row.ident}|{p.action}|{p.client_id}|{p.link_ref}|{p.new_name}")
        return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()

    def high_writes(self, decisions: dict | None = None) -> int:
        n = 0
        for ch in self.owner + [c for p in self.plans for c in p.changes]:
            if ch.sens == "HIGH" and (ch.action in (NEW, UPDATE) or (ch.action == CONFLICT and self.decision(ch.cid) == "sheet")):
                n += 1
        return n


class Differ:
    def __init__(self, store: BusinessStore, parsed: Parsed, decisions: dict[str, str] | None = None):
        self.store, self.parsed, self.decisions = store, parsed, dict(decisions or {})
        self._taken: dict[tuple[str, str], set[int]] = {}

    # ------------------------------------------------------------------ main
    def run(self, c: sqlite3.Connection) -> Diff:
        d = Diff(decisions=self.decisions)
        self.d, self.c = d, c
        d.issues = []
        # the owner's own business
        for f in self.parsed.owner:
            ch = self._classify(None, "me", "", f, "me")
            d.owner.append(ch)
            if ch.action == CONFLICT:
                d.conflicts.append(self._fact_conflict(ch, "your own business"))
        # clients
        db = self._db_clients()
        self._plans(db)
        for pl in d.plans:
            if pl.action == "PENDING":
                continue
            for f in pl.row.facts:
                ch = self._classify(pl.client_id, pl.row.ident, pl.row.name, f, "client")
                pl.changes.append(ch)
                if ch.action == CONFLICT:
                    d.conflicts.append(self._fact_conflict(ch, pl.row.name))
            if pl.action == "EXISTING" and not pl.row.forced_id:
                self._name_and_ref(pl, db)
            pl.state = self._client_state(pl)
        self._records(db)
        d.missing = self._missing()
        return d

    # ------------------------------------------------------------------ database clients
    def _db_clients(self) -> dict[str, dict]:
        c = self.c
        out: dict[str, dict] = {}
        for r in c.execute("SELECT * FROM clients").fetchall():
            out[r["client_id"]] = {"id": r["client_id"], "name": r["name"], "ref": r["ref"] or "", "archived": bool(r["archived"]), "names": {name_key(r["name"])}, "exact": {exact_name(r["name"])},
                                   "sites": set(), "site_keys": set()}
        for r in c.execute("SELECT scope, field_key, value_plain FROM facts WHERE scope<>'me' AND state='LIVE' AND value_plain IS NOT NULL AND field_key IN "
                           "('contact.website','identity.business_name','identity.trade_name','identity.legal_name')").fetchall():
            e = out.get(r["scope"])
            if not e:
                continue
            if r["field_key"] == "contact.website":
                e["sites"].add(r["value_plain"].strip().casefold())
                e["site_keys"].add(site_key(r["value_plain"]))
            else:
                e["names"].add(name_key(r["value_plain"]))
                e["exact"].add(exact_name(r["value_plain"]))
        return out

    # ------------------------------------------------------------------ client plans and duplicates
    def _plans(self, db: dict[str, dict]) -> None:
        d = self.d
        by_ref = {v["ref"].casefold(): v for v in db.values() if v["ref"]}
        rows = self.parsed.clients
        sites = {r.ident: next((f.prep.canon for f in r.facts if f.key == "contact.website"), "") for r in rows}
        # duplicates INSIDE the workbook (different references, similar client)
        in_book: dict[str, Duplicate] = {}
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                sig = []
                if exact_name(a.name) == exact_name(b.name):
                    sig.append("same name")
                elif name_key(a.name) and name_key(a.name) == name_key(b.name):
                    sig.append("similar name")
                if sites[a.ident] and sites[b.ident] and site_key(sites[a.ident]) == site_key(sites[b.ident]):
                    sig.append("same web address")
                if not sig:
                    continue
                exact = "same name" in sig
                cid = "D" + sid("book", a.ident, b.ident)
                in_book[b.ident] = Duplicate(cid, b.ref, b.name, b.row, None, a.name, a.ref, a.row, sig,
                                             () if exact else ("separate", "cancel"),
                                             "two rows have exactly the same client name: rename one of them in the workbook (clients are told apart by name when you pick one for a task)" if exact else "")
        for r in rows:
            if r.forced_id:   # the user chose this client explicitly (e.g. a Markdown file imported "for Example Pest Pro")
                e = db.get(r.forced_id)
                if e is None or e["archived"]:
                    d.issues.append(Issue("error", self.parsed.workbook, 0, "", "CLIENT_ARCHIVED" if e else "NO_SUCH_CLIENT", "that client is archived or does not exist: nothing was imported"))
                    continue
                d.plans.append(ClientPlan(r, "EXISTING", client_id=r.forced_id))
                continue
            existing = by_ref.get(r.ref.casefold()) if r.ref else None
            plan = ClientPlan(r, "NEW")
            if existing:
                if existing["archived"]:
                    d.issues.append(Issue("error", "Clients", r.row, "client_id", "CLIENT_ARCHIVED", "this client is archived: restore it (business client restore) before importing into it"))
                    continue
                plan.action, plan.client_id = "EXISTING", existing["id"]
            else:
                dup = self._possible_duplicate(r, sites[r.ident], db)
                if dup:
                    plan.dup = dup
                    d.duplicates.append(dup)
                    choice = d.decision(dup.cid)
                    if choice == "same":
                        plan.action, plan.client_id, plan.link_ref = "EXISTING", dup.existing_id, True
                    elif choice == "separate":
                        plan.action = "NEW"
                    else:
                        plan.action, plan.state = "PENDING", "PENDING"
                elif r.ident in in_book:
                    dup = in_book[r.ident]
                    plan.dup = dup
                    d.duplicates.append(dup)
                    choice = d.decision(dup.cid)
                    if choice == "separate":
                        plan.action = "NEW"
                    else:
                        plan.action, plan.state = "PENDING", "PENDING"
            d.plans.append(plan)

    def _possible_duplicate(self, r: ClientRow, site: str, db: dict[str, dict]) -> Duplicate | None:
        best: tuple[int, dict, list[str]] | None = None
        for e in db.values():
            sig: list[str] = []
            exact = exact_name(r.name) in e["exact"]
            if exact:
                sig.append("same name")
            elif name_key(r.name) and name_key(r.name) in e["names"]:
                sig.append("similar name")
            if site and site.strip().casefold() in e["sites"]:
                sig.append("same web address")
            elif site and site_key(site) in e["site_keys"]:
                sig.append("same web address (after ignoring http/www)")
            if not sig:
                continue
            score = len(sig) + (1 if exact else 0)
            if best is None or score > best[0]:
                best = (score, e, sig)
        if best is None:
            return None
        _, e, sig = best
        exact = "same name" in sig
        refs_differ = bool(e["ref"] and r.ref and e["ref"].casefold() != r.ref.casefold())
        choices: list[str] = []
        problem = ""
        if e["archived"]:
            problem = "the matching client is archived: restore it first (business client restore), or change the workbook"
        else:
            if refs_differ:
                problem = f"the existing client already has the id '{e['ref']}', so it cannot be the same client under the id '{r.ref}': use one id in the workbook"
            else:
                choices.append("same")
            if not exact:
                choices.append("separate")
            elif not choices:
                problem = problem or "a client with exactly this name already exists"
        if choices:
            choices.append("cancel")
        return Duplicate("D" + sid("db", r.ident, e["id"]), r.ref, r.name, r.row, e["id"], e["name"], e["ref"], None, sig, tuple(choices), problem)

    def _name_and_ref(self, pl: ClientPlan, db: dict[str, dict]) -> None:
        e = db[pl.client_id]
        if exact_name(pl.row.name) != exact_name(e["name"]):
            cid = "C" + sid("name", pl.row.ident)
            conf = Conflict(cid, "name", "Client name", pl.row.name, "MEDIUM", self._mask_name(e["name"]), self._mask_name(pl.row.name), e["name"], pl.row.name)
            pl.name_conflict = conf
            self.d.conflicts.append(conf)
            if self.d.decision(cid) == "sheet":
                pl.new_name = pl.row.name

    @staticmethod
    def _mask_name(n: str) -> str:
        return n[:1] + "•" * max(2, min(len(n) - 1, 8)) if len(n) < 8 else n[:2] + "•" * min(len(n) - 4, 8) + n[-2:]

    @staticmethod
    def _client_state(pl: ClientPlan) -> str:
        if pl.action == "PENDING":
            return "PENDING"
        if pl.action == "NEW":
            return "NEW"
        if pl.name_conflict or any(ch.action == CONFLICT for ch in pl.changes):
            return "CONFLICT"
        if pl.link_ref or any(ch.action in (NEW, UPDATE) for ch in pl.changes):
            return "UPDATED"
        return "UNCHANGED"

    # ------------------------------------------------------------------ one fact
    def _clear(self, canon: str, compound: bool, pk=None) -> str:
        if not compound:
            return canon
        try:
            d = json.loads(canon)
            order = [p.name for p in pk.field.parts] if pk is not None else sorted(d)
            return ", ".join(str(d[n]) for n in order if d.get(n))
        except ValueError:
            return canon

    def _classify(self, scope_id: str | None, scope_ref: str, label: str, f: ParsedFact, kind: str) -> Change:
        store, c = self.store, self.c
        prep = f.prep
        pk = prep.pk
        scope = scope_id or ("me" if scope_ref == "me" else "")
        sens = prep.sens
        compound = pk.field.compound
        sheet_text = None if sens == "HIGH" else self._clear(prep.canon, compound, pk)
        mk = lambda action, **kw: Change(scope_ref, label, kw.pop("key", pk.display().removesuffix(".full")), action, sens, f, kw.pop("prep", prep), sheet_text=sheet_text, **kw)   # noqa: E731
        if f.many:
            rows = c.execute("SELECT * FROM facts WHERE scope=? AND field_key=?", (scope, pk.storage_key)).fetchall() if scope else []
            for r in rows:
                if r["value_hash"] == prep.vhash:
                    if r["state"] == "LIVE":
                        return mk(UNCHANGED, existing_ids=[r["fact_id"]], existing_mask=r["display_mask"] or "", existing_verification=r["verification"])
                    return mk(SKIPPED, note="this value was rejected or replaced earlier: not added again")
            taken = self._taken.setdefault((scope_ref, f.key), set())
            used = {r["instance"] for r in rows} | taken
            n = 0
            while n in used:
                n += 1
            taken.add(n)
            key = f.key.replace("[]", f"[{n}]")
            return mk(NEW, key=key, prep=replace(prep, pk=parse_key(key, "me" if scope_ref == "me" else "client")))
        rows = store.fact_rows(c, scope, pk) if scope else []
        live = [r for r in rows if r["state"] == "LIVE"]
        if not live:
            if any(r["value_hash"] == prep.vhash for r in rows):
                return mk(SKIPPED, note="this value was rejected or replaced earlier: not added again")
            return mk(NEW)
        if any(r["value_hash"] == prep.vhash for r in live):
            ok = next(r for r in live if r["value_hash"] == prep.vhash)
            return mk(UNCHANGED, existing_ids=[r["fact_id"] for r in live], existing_mask=ok["display_mask"] or "", existing_verification=ok["verification"])
        ids = [r["fact_id"] for r in live]
        verified = any(r["verification"] == "VERIFIED" for r in live)
        mask = live[0]["display_mask"] or "" if len(live) == 1 else f"{len(live)} different values"
        ev = "VERIFIED" if verified else "UNVERIFIED"
        ex_text = None
        if len(live) == 1 and live[0]["sensitivity"] != "HIGH":
            try:
                ex_text = self._clear(store._read_value(live[0]), compound, pk)
            except Exception:  # noqa: BLE001
                ex_text = None
        final = prep
        if compound and len(live) == 1:
            try:
                have = json.loads(store._read_value(live[0]))
                new = json.loads(prep.canon)
            except Exception:  # noqa: BLE001
                have, new = {}, json.loads(prep.canon)
            same = lambda a, b: " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()   # noqa: E731
            if all(k in have and same(have[k], v) for k, v in new.items()):
                return mk(UNCHANGED, existing_ids=ids, existing_mask=mask, existing_verification=ev, note="the stored address already contains these parts")
            merged = {**have, **new}
            final = store.prepare(pk.display(), merged, kind="me" if scope_ref == "me" else "client")
            sheet_text = self._clear(final.canon, True, pk) if sens != "HIGH" else None
        updatable = len(live) == 1 and live[0]["source_type"] == "IMPORT" and not verified
        action = UPDATE if updatable else CONFLICT
        ch = Change(scope_ref, label, pk.display().removesuffix(".full"), action, sens, f, final, ids, mask, ex_text, sheet_text, ev)
        if action == CONFLICT:
            ch.cid = "C" + sid("fact", scope_ref, ch.key)
        return ch

    def _fact_conflict(self, ch: Change, who: str) -> Conflict:
        label = ch.fact.prep.pk.label
        multiple = len(ch.existing_ids) > 1
        return Conflict(ch.cid, "fact", label + (" (already conflicting)" if multiple else ""), who, ch.sens, ch.existing_mask, ch.prep.mask, ch.existing_text, ch.sheet_text)

    # ------------------------------------------------------------------ structured rows
    def _records(self, db: dict[str, dict]) -> None:
        d, c = self.d, self.c
        plans = {p.row.ident: p for p in d.plans}
        by_ref = {v["ref"].casefold(): v for v in db.values() if v["ref"]}
        for rec in self.parsed.records:
            plan = plans.get(rec.ref_key)
            cid_db: str | None = None
            label = rec.ref
            if plan is not None:
                if plan.action == "PENDING":
                    d.records.append(RecChange(rec, plan.row.name, None, "PENDING"))
                    continue
                cid_db, label = plan.client_id, plan.row.name
            else:
                e = by_ref.get(rec.ref_key) or db.get(rec.ref)
                if e is None:
                    d.issues.append(Issue("error", {"service": "Client Services", "location": "Client Locations", "seo": "Client SEO"}[rec.kind], rec.row, "client_id", "UNKNOWN_CLIENT",
                                          "this client_id matches no client in the workbook's Clients sheet and no client already stored: row not imported"))
                    continue
                if e["archived"]:
                    d.issues.append(Issue("error", rec.sheet, rec.row, "client_id", "CLIENT_ARCHIVED", "this client is archived: restore it before importing into it"))
                    continue
                cid_db, label = e["id"], e["name"]
            key = R.rec_key(rec.kind, rec.rec)
            ex = R.find_live(c, rec.kind, cid_db, key) if cid_db else None
            if ex is None:
                d.records.append(RecChange(rec, label, cid_db, NEW))
                continue
            if R.attrs_equal(rec.kind, ex, rec.rec):
                d.records.append(RecChange(rec, label, cid_db, UNCHANGED, ex["rec_id"]))
                continue
            diffs = [(a, ex[a] or "", str(rec.rec[a])) for a in R.KINDS[rec.kind]["attrs"] if rec.rec.get(a) and R.norm(ex[a]) != R.norm(rec.rec[a])]
            rc = RecChange(rec, label, cid_db, CONFLICT if ex["verification"] == "VERIFIED" else UPDATE, ex["rec_id"], diffs)
            if rc.action == CONFLICT:
                rc.cid = "C" + sid("rec", rec.kind, rec.ref_key, key)
                kind_label = {"service": "service", "location": "location", "seo": "SEO target"}[rec.kind]
                d.conflicts.append(Conflict(rc.cid, "record", f"{kind_label}: {rec.rec.get(R.IDENTITY[rec.kind][0]) or ''}", label, "LOW", "", "", None, None,
                                            [(a, (o if a != "notes" else "(notes)"), (n if a != "notes" else "(notes)")) for a, o, n in diffs]))
            d.records.append(rc)

    # ------------------------------------------------------------------ missing
    def _db_has(self, scope: str | None, t: L.Target) -> bool:
        if not scope:
            return False
        c = self.c
        key = t.key
        rows = c.execute("SELECT * FROM facts WHERE scope=? AND field_key=? AND state='LIVE'", (scope, key)).fetchall()
        if not rows:
            return False
        if t.kind == "addr":
            for r in rows:
                try:
                    parts = json.loads(self.store._read_value(r))
                except Exception:  # noqa: BLE001
                    return True
                if parts.get(t.part):
                    return True
            return False
        return True

    def _missing(self) -> dict:
        d = self.d
        by_field: dict[str, int] = {}
        total = 0
        # the owner's profile
        if any(s.kind == "me" and s.status == "read" for s in self.parsed.sheets):
            for col in L.ME.cols:
                if col.name in self.parsed.owner_provided:
                    continue
                if not self._db_has("me", col.target):
                    by_field[f"My Business.{col.name}"] = by_field.get(f"My Business.{col.name}", 0) + 1
                    total += 1
        clients_sheet_read = any(s.kind == "clients" and s.status == "read" for s in self.parsed.sheets)
        if clients_sheet_read:
            for pl in d.plans:
                if pl.action == "PENDING":
                    continue
                for col in L.CLIENTS.cols:
                    if col.target.kind == "ref" or col.name in pl.row.provided:
                        continue
                    if not self._db_has(pl.client_id, col.target):
                        by_field[f"Clients.{col.name}"] = by_field.get(f"Clients.{col.name}", 0) + 1
                        total += 1
            # clients with no service / location / SEO rows anywhere (workbook or database)
            have = {k: {r.rec.ref_key for r in d.records if r.rec.kind == k and r.action != "PENDING"} for k in ("service", "location", "seo")}
            for pl in d.plans:
                if pl.action == "PENDING":
                    continue
                for kind, label in (("service", "services"), ("location", "locations"), ("seo", "SEO targets")):
                    if pl.row.ident in have[kind]:
                        continue
                    if pl.client_id and R.live_rows(self.c, kind, pl.client_id):
                        continue
                    by_field[f"(no {label})"] = by_field.get(f"(no {label})", 0) + 1
        return {"fields": len(by_field), "values": total, "by_field": dict(sorted(by_field.items()))}
