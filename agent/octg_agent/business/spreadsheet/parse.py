"""Workbook -> validated, typed rows. Pure: it reads cells, never the database, and never writes.

Cell contents are DATA. They are validated as values for catalog fields / row attributes and nothing else: no cell is ever interpreted as an instruction, a formula, a path or a
command, and nothing parsed here is ever sent to a model. Problems are reported as Issues that name the sheet, row and column but NEVER contain the cell's value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ...sensitive import SECRETS, value_category
from .. import records as R
from ..schema import CONSTITUTIONS, KeyError_, parse_key
from ..store import BusinessStore, Prepared, StoreError
from ..validators import secret_reason
from . import layout as L
from .layout import SheetDef, Target
from .xlsx import Row, Sheet, Workbook

MAX_TEXT = 2000
MAX_NOTES = 2000
MAX_ATTR = 300
SPLIT = re.compile(r"[;|\n]+")
# wording that looks like an attempt to instruct an AI. It is still just data (nothing here is ever read by a model as an instruction); the user is told so they can look at the cell.
INSTRUCTION_LIKE = re.compile(r"ignore (all |any |the )?(previous|prior|above|earlier) (instruction|prompt|message)|disregard (the )?(previous|above|system)|system prompt|you are now |"
                              r"act as (an? )?(ai|assistant|admin)|reveal (your|the) (prompt|instructions|password|key)|do not tell the user|new instructions?:", re.I)
SAFE_ENUMS = {"state": re.compile(r"[A-Za-z][A-Za-z .&'()-]{1,39}")}   # parts a local model may be shown (prompt_ok): only a short place-name shape ever reaches them
PROMPT_TEXT_PARTS = ("state",)


@dataclass
class Issue:
    level: str                     # error | warning | info
    sheet: str
    row: int
    col: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"level": self.level, "sheet": self.sheet, "row": self.row, "column": self.col, "code": self.code, "message": self.message}


@dataclass
class ParsedFact:
    scope_ref: str                 # "me" or a ClientRow.ident
    key: str                       # catalog key; for repeatable facts the template ("services.primary[]") and many=True
    many: bool
    prep: Prepared                 # validated exact value (application code only: never serialised, logged or shown)
    sheet: str
    row: int
    col: str
    uid: str = ""                  # identifies this value inside one plan (so a reviewer can exclude it)


@dataclass
class ClientRow:
    ident: str                     # the reference (case-folded) or "~<row>" when the row has none
    ref: str                       # as written ("" if none)
    name: str                      # display name: business_name, else trade_name, else legal_name
    row: int
    facts: list[ParsedFact] = field(default_factory=list)
    provided: set[str] = field(default_factory=set)       # template columns this row gave a value for
    forced_id: str | None = None   # the client is chosen by the USER (a Markdown file imported for a named client): no matching, no duplicate detection


@dataclass
class ParsedRecord:
    kind: str                      # service | location | seo
    ref_key: str                   # the client reference this row names (case-folded)
    ref: str
    rec: dict                      # validated attributes
    sheet: str
    row: int
    uid: str = ""


@dataclass
class SheetReport:
    name: str
    status: str                    # "read" | "documentation sheet: ignored" | "unknown sheet: ignored" | "hidden sheet: ignored" | "empty" | "not read"
    kind: str = ""                 # the layout key
    data_rows: int = 0
    maps: list[L.ColMap] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    formula_cells: int = 0

    def recognized(self) -> list[L.ColMap]:
        return [m for m in self.maps if m.status in (L.CONFIDENT, L.CONFIRMED)]

    def to_dict(self) -> dict:
        return {"sheet": self.name, "status": self.status, "kind": self.kind, "rows": self.data_rows, "columns": [m.to_dict() for m in self.maps],
                "recognized": [m.header for m in self.recognized()], "unknown": [m.header for m in self.maps if m.status == L.UNKNOWN],
                "needs_confirmation": [m.header for m in self.maps if m.status == L.NEEDS], "refused": [m.header for m in self.maps if m.status == L.REFUSED],
                "missing_columns": self.missing_columns, "formula_cells": self.formula_cells}


@dataclass
class Parsed:
    workbook: str
    sha256: str
    sheets: list[SheetReport] = field(default_factory=list)
    owner: list[ParsedFact] = field(default_factory=list)
    owner_provided: set[str] = field(default_factory=set)  # template rows of My Business that have a value
    clients: list[ClientRow] = field(default_factory=list)
    records: list[ParsedRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    secrets_owner: str = ""
    kind: str = "spreadsheet"      # spreadsheet | markdown (the source type recorded with the import)
    scope: str = ""                # a Markdown import is for ONE scope chosen by the user: "me" or a client id
    info: dict = field(default_factory=dict)   # what a text source contained (headings, tables, lists ...), for the review

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def pending_maps(self) -> list[L.ColMap]:
        return [m for s in self.sheets for m in s.maps if m.status == L.NEEDS]

    def counts(self) -> dict:
        n = {"business_profile": 1 if self.owner else 0, "clients": len(self.clients)}
        for k, label in (("service", "services"), ("location", "locations"), ("seo", "seo_targets")):
            n[label] = sum(1 for r in self.records if r.kind == k)
        return n


class Parser:
    def __init__(self, store: BusinessStore):
        self.store = store
        self._reset()

    def _reset(self) -> None:
        self.p: Parsed
        self._seen_clients: dict[str, ClientRow] = {}
        self._rowvals: dict[str, dict] = {}
        self._ap: dict[tuple[str, str], dict] = {}                # (scope, compound key) -> {part: (value, sheet, row, header)}
        self._extra_cols: dict[str, L.Col] = {}
        self._uid = 0

    def new_uid(self) -> str:
        self._uid += 1
        return f"i{self._uid}"

    def begin(self, name: str, sha256: str, kind: str = "spreadsheet", scope: str = "") -> Parsed:
        """Start a plan that is not read from a workbook (the Markdown reader builds one with the same validators)."""
        self._reset()
        self.p = Parsed(name, sha256, kind=kind, scope=scope)
        self.p.secrets_owner = "import:" + sha256[:12]
        return self.p

    # ------------------------------------------------------------------ entry
    def parse(self, wb: Workbook, name: str, sha256: str, overrides: dict[str, dict[str, str]] | None = None, skips: dict[str, set[str]] | None = None) -> Parsed:
        """overrides / skips: {layout key: {hnorm(header): column}} / {layout key: {hnorm(header)}}."""
        p = self.begin(name, sha256)
        self.overrides, self.skips = overrides or {}, skips or {}
        used: set[str] = set()
        for sh in wb.sheets:
            d = L.sheet_for_name(sh.name)
            if sh.hidden:
                p.sheets.append(SheetReport(sh.name, "hidden sheet: ignored"))
                p.warnings.append(f"sheet {sh.name!r} is hidden and was ignored")
                continue
            if d is None:
                p.sheets.append(SheetReport(sh.name, "documentation sheet: ignored" if L.is_doc_sheet(sh.name) else "unknown sheet: ignored"))
                continue
            if d.key in used:
                p.sheets.append(SheetReport(sh.name, "not read", d.key))
                self._issue("error", sh.name, 0, "", "DUPLICATE_SHEET", f"two sheets are both '{d.name}': only one is allowed")
                continue
            used.add(d.key)
            rep = SheetReport(sh.name, "read", d.key, formula_cells=sh.formula_cells)
            p.sheets.append(rep)
            if sh.formula_cells:
                p.warnings.append(f"sheet {sh.name!r} has {sh.formula_cells} formula cell(s): formulas are never evaluated, only the value stored in the file is read")
            if sh.long_cells:
                self._issue("error", sh.name, 0, "", "TOO_LONG", f"{sh.long_cells} cell(s) are longer than {5000} characters and were refused")
            if sh.error_cells:
                p.warnings.append(f"sheet {sh.name!r} has {sh.error_cells} cell(s) showing an Excel error: treated as blank")
            if not sh.rows:
                rep.status = "empty"
                continue
            if d.layout == "kv":
                self._kv(sh, d, rep)
            else:
                self._table(sh, d, rep)
        if not p.owner and not p.clients and not p.records and not any(s.status == "read" for s in p.sheets):
            self._issue("error", "", 0, "", "NO_SHEETS", "none of the sheets My Business, Clients, Client Services, Client Locations or Client SEO was found in this workbook")
        return p

    def _issue(self, level: str, sheet: str, row: int, col: str, code: str, message: str) -> None:
        self.p.issues.append(Issue(level, sheet, row, col, code, message))

    issue = _issue

    # ------------------------------------------------------------------ cell checks (shared)
    def _text_ok(self, sheet: str, row: int, col: str, value: str, limit: int, *, allow_identifier: bool = False) -> bool:
        if len(value) > limit:
            self._issue("error", sheet, row, col, "TOO_LONG", f"the value is longer than {limit} characters: not imported")
            return False
        if INSTRUCTION_LIKE.search(value):
            self._issue("info", sheet, row, col, "INSTRUCTION_LIKE", "this cell reads like an instruction to an AI. It is stored as plain text only and is never treated as an instruction")
        why = secret_reason("", value)
        if why:
            self._issue("error", sheet, row, col, "SECRET_NOT_STORABLE", f"this {why}; it was NOT imported (passwords, OTPs, keys and payment credentials are never stored)")
            return False
        if not allow_identifier:
            cat = value_category(value)
            if cat:
                self._issue("error", sheet, row, col, "REFUSED_VALUE", f"this looks like a {cat} identifier, which does not belong in this column: it was NOT imported")
                return False
        return True

    def _make_fact(self, scope_ref: str, kind: str, key: str, value, sheet: str, row: int, col: str, *, many: bool = False) -> ParsedFact | None:
        """Validate one value for a catalog key. Returns None (and records an Issue) if it must not be imported."""
        probe = key.replace("[]", "[0]")
        try:
            pk0 = parse_key(probe, kind)
        except KeyError_:
            self._issue("error", sheet, row, col, "UNKNOWN_FIELD", f"'{key}' is not a field of the Business Brain")
            return None
        if isinstance(value, dict):
            ok = all(self._text_ok(sheet, row, col, v, MAX_ATTR, allow_identifier=False) for v in value.values())
        else:
            ok = self._text_ok(sheet, row, col, value, MAX_TEXT, allow_identifier=pk0.sensitivity == "HIGH" or pk0.field.kind in ("phone", "email", "url", "date", "pin", "digits"))
        if not ok:
            return None
        if pk0.field.key == "identity.constitution":
            typed = {"llc": "Limited Liability Company (LLC)", "l.l.c.": "Limited Liability Company (LLC)", "c-corp": "Corporation (C-Corp)", "c corp": "Corporation (C-Corp)",
                     "s-corp": "S Corporation", "s corp": "S Corporation"}.get(value.strip().lower(), value)
            match = next((c for c in CONSTITUTIONS if c.lower() == typed.strip().lower()), None)
            if not match:
                self._issue("error", sheet, row, col, "BAD_VALUE", "not one of: " + ", ".join(CONSTITUTIONS))
                return None
            value = match
        if isinstance(value, dict):
            for part in PROMPT_TEXT_PARTS:
                if part in value and pk0.field.part(part) is not None and pk0.field.part(part).prompt_ok and not SAFE_ENUMS[part].fullmatch(value[part]):
                    self._issue("error", sheet, row, col, "BAD_VALUE", f"{part} must be a short place name (letters, spaces and . & ' ( ) - only)")
                    return None
        try:
            prep = self.store.prepare(probe, value, kind=kind)
        except StoreError as e:
            self._issue("error", sheet, row, col, e.code, e.message)
            return None
        if not prep.ok:
            self._issue("error", sheet, row, col, "BAD_VALUE", f"{prep.note}: not imported (a typo here would be stored as if it were right; fix it in the workbook, or enter it with `business set`)")
            return None
        if prep.sens == "HIGH":
            for v in (value.values() if isinstance(value, dict) else [value]):
                SECRETS.register(str(v), self.p.secrets_owner)
            SECRETS.register(prep.canon, self.p.secrets_owner)
        return ParsedFact(scope_ref, key, many, prep, sheet, row, col, self.new_uid())

    # ------------------------------------------------------------------ My Business: Field | Value
    def _kv(self, sh: Sheet, d: SheetDef, rep: SheetReport) -> None:
        p = self.p
        head = sh.rows[0]
        cells = [L.hnorm(c) for c in head.cells]
        vi = next((i for i, c in enumerate(cells) if c in ("value", "answer", "entry")), -1)
        if not cells or cells[0] not in ("field", "key", "item") or vi < 1:
            rep.status = "not read"
            self._issue("error", sh.name, head.num, "", "NO_HEADER", "the first row of 'My Business' must be the header: Field | Value")
            return
        rows = sh.rows[1:]
        rep.data_rows = len(rows)
        headers = [(0, r.cells[0] if r.cells else "", r.num) for r in rows if r.cells and r.cells[0]]
        for r in rows:
            if (not r.cells or not r.cells[0]) and any(r.cells):
                self._issue("warning", sh.name, r.num, "", "NO_FIELD_NAME", "a value without a field name: ignored")
        ov, sk = self.overrides.get("me", {}), self.skips.get("me", set())
        maps = L.map_headers(d, headers, ov, sk, extra=self._me_extra)
        rep.maps = maps
        by_row = {r.num: r for r in rows}
        seen: dict[str, ParsedFact] = {}
        for m in maps:
            if m.status in (L.NEEDS, L.REFUSED, L.UNKNOWN, L.SKIPPED):
                continue
            row = by_row[m.row]
            value = row.cells[vi].strip() if len(row.cells) > vi else ""
            col = d.col(m.target)
            if col is None:                      # an extra catalog key typed in the Field column
                col = self._extra_cols[L.hnorm(m.header)]
            if not value:
                continue
            p.owner_provided.add(col.name)
            for f in self._facts_for(col, value, "me", "me", sh.name, m.row, m.header):
                key = f.prep.pk.display() if not f.many else f"{f.key}:{f.prep.vhash}"
                if key in seen:
                    if seen[key].prep.vhash == f.prep.vhash:
                        self._issue("warning", sh.name, m.row, m.header, "DUPLICATE_ROW", "the same value was already given for this field in this sheet: ignored")
                    else:
                        self._issue("error", sh.name, m.row, m.header, "ROW_CONFLICT", "two rows give different values for the same field: this one was not imported")
                    continue
                seen[key] = f
                p.owner.append(f)
        self._assemble_addresses("me", "me", p.owner)
        missing = [c.name for c in d.cols if c.name not in {m.target for m in maps}]
        rep.missing_columns = missing

    def _me_extra(self, text: str):
        """Any catalog key typed in the Field column (owners[0].name, social.profile[], identity.constitution ...) is accepted when it names a plain or repeatable field."""
        raw = (text or "").strip()
        if not raw or "." not in raw:
            return None
        try:
            pk = parse_key(raw, "me")
        except KeyError_:
            return None
        if pk.field.compound or pk.field.custom:
            return None
        many = pk.field.many and "[" not in raw
        col = L.Col(raw, Target("multi" if many else "fact", pk.field.key if many else pk.display()))
        self._extra_cols[L.hnorm(raw)] = col
        return col

    def _facts_for(self, col: L.Col, value: str, kind: str, scope_ref: str, sheet: str, row: int, header: str) -> list[ParsedFact]:
        t = col.target
        out: list[ParsedFact] = []
        if t.kind == "fact":
            f = self._make_fact(scope_ref, kind, t.key, value, sheet, row, header)
            if f:
                out.append(f)
        elif t.kind == "multi":
            seen: set[str] = set()
            for item in (s.strip() for s in SPLIT.split(value)):
                if not item or item.casefold() in seen:
                    continue
                seen.add(item.casefold())
                f = self._make_fact(scope_ref, kind, t.key, item, sheet, row, header, many=True)
                if f:
                    out.append(f)
        elif t.kind == "addr":
            self._ap.setdefault((scope_ref, t.key), {})[t.part] = (value, sheet, row, header)
        return out

    def _assemble_addresses(self, scope_ref: str, kind: str, into: list[ParsedFact]) -> None:
        """Assemble the address columns collected for this scope into ONE compound value (the parts of one address always travel together)."""
        for (sref, key), parts in list(self._ap.items()):
            if sref != scope_ref:
                continue
            del self._ap[(sref, key)]
            first = next(iter(parts.values()))
            f = self._make_fact(scope_ref, kind, key, {name: v[0] for name, v in parts.items()}, first[1], first[2], "address columns")
            if f:
                into.append(f)

    # ------------------------------------------------------------------ tables
    def _table(self, sh: Sheet, d: SheetDef, rep: SheetReport) -> None:
        p = self.p
        head = sh.rows[0]
        headers = [(i, t, head.num) for i, t in enumerate(head.cells) if t]
        maps = L.map_headers(d, headers, self.overrides.get(d.key, {}), self.skips.get(d.key, set()))
        rep.maps = maps
        mapped = {m.target: m for m in maps if m.status in (L.CONFIDENT, L.CONFIRMED)}
        rep.missing_columns = [c.name for c in d.cols if c.name not in mapped]
        rows = sh.rows[1:]
        rep.data_rows = len(rows)
        for c in d.cols:
            if c.required and c.name not in mapped:
                pending = any(m.status == L.NEEDS and c.name in (m.candidates or ()) for m in maps)
                if not pending:
                    self._issue("error", sh.name, head.num, c.name, "MISSING_COLUMN", f"the required column '{c.name}' was not found in '{d.name}': the whole sheet was not imported")
                rep.status = "not read"
                return
        if d.key == "clients":
            self._clients(sh, d, rows, mapped)
        else:
            self._records(sh, d, rows, mapped)
        for m in maps:
            if m.status == L.REFUSED:
                self._issue("warning", sh.name, head.num, m.header, "REFUSED_COLUMN", m.note)

    def _cell(self, row: Row, m: L.ColMap | None) -> str:
        if m is None or m.index >= len(row.cells):
            return ""
        return row.cells[m.index].strip()

    def _clients(self, sh: Sheet, d: SheetDef, rows: list[Row], mapped: dict[str, L.ColMap]) -> None:
        p = self.p
        for r in rows:
            vals = {c.name: self._cell(r, mapped.get(c.name)) for c in d.cols}
            ref = vals.get("client_id", "")
            if ref:
                try:
                    ref = self.store.clean_ref(ref) or ""
                except StoreError as e:
                    self._issue("error", sh.name, r.num, "client_id", e.code, e.message)
                    continue
            name = vals.get("business_name") or vals.get("trade_name") or vals.get("legal_name")
            ident = ref.casefold() if ref else f"~{r.num}"
            if not name:
                self._issue("error", sh.name, r.num, "business_name", "NAME_MISSING", "a client needs a name (business_name, trade_name or legal_name): this row was not imported")
                continue
            if ident in self._seen_clients:
                first = self._seen_clients[ident]
                if vals == self._rowvals[ident]:
                    self._issue("warning", sh.name, r.num, "client_id", "DUPLICATE_ROW", f"identical to row {first.row} (same client_id): ignored")
                else:
                    self._issue("error", sh.name, r.num, "client_id", "ROW_CONFLICT", f"row {first.row} already defines this client_id with different details: this row was not imported")
                continue
            cr = ClientRow(ident, ref, name, r.num)
            for c in d.cols:
                if c.target.kind == "ref":
                    continue
                value = vals.get(c.name, "")
                if c.name not in mapped:
                    continue
                if not value:
                    continue
                cr.provided.add(c.name)
                cr.facts.extend(self._facts_for(c, value, "client", ident, sh.name, r.num, c.name))
            self._assemble_addresses(ident, "client", cr.facts)
            self._seen_clients[ident] = cr
            self._rowvals[ident] = vals
            p.clients.append(cr)

    # ------------------------------------------------------------------ services / locations / SEO targets
    def _records(self, sh: Sheet, d: SheetDef, rows: list[Row], mapped: dict[str, L.ColMap]) -> None:
        kind = {"services": "service", "locations": "location", "seo": "seo"}[d.key]
        seen: dict[str, tuple[int, dict]] = {}
        for r in rows:
            vals = {c.name: self._cell(r, mapped.get(c.name)) for c in d.cols}
            ref = vals.get("client_id", "")
            if not ref:
                self._issue("error", sh.name, r.num, "client_id", "NO_CLIENT", "this row does not say which client it belongs to (client_id is empty): it was not imported")
                continue
            try:
                ref = self.store.clean_ref(ref) or ""
            except StoreError as e:
                self._issue("error", sh.name, r.num, "client_id", e.code, e.message)
                continue
            rec: dict = {}
            bad = False
            for c in d.cols:
                if c.target.kind != "attr" or c.name not in mapped:
                    continue
                v = vals[c.name]
                if not v:
                    continue
                v = self._attr_value(sh.name, r.num, c, v)
                if v is None:
                    bad = True
                    continue
                rec[c.target.key] = v
            if bad:
                continue
            main = {"service": ("service",), "location": ("location", "city"), "seo": ("keyword",)}[kind]
            if not any(rec.get(k) for k in main):
                self._issue("error", sh.name, r.num, " or ".join(main), "MISSING_VALUE", f"this row has no {' or '.join(main)}: it was not imported")
                continue
            key = f"{ref.casefold()}|{R.rec_key(kind, rec)}"
            if key in seen:
                first_row, first = seen[key]
                if all(R.norm(first.get(a)) == R.norm(rec.get(a)) for a in R.KINDS[kind]["attrs"]):
                    self._issue("warning", sh.name, r.num, "", "DUPLICATE_ROW", f"identical to row {first_row}: ignored")
                else:
                    self._issue("error", sh.name, r.num, "", "ROW_CONFLICT", f"row {first_row} describes the same {kind} for this client with different details: this row was not imported")
                continue
            seen[key] = (r.num, rec)
            self.p.records.append(ParsedRecord(kind, ref.casefold(), ref, rec, sh.name, r.num, self.new_uid()))

    def _attr_value(self, sheet: str, row: int, c: L.Col, v: str) -> str | None:
        name = c.target.key
        limit = MAX_NOTES if name == "notes" else MAX_ATTR
        if not self._text_ok(sheet, row, c.name, v, limit):
            return None
        if name == "priority":
            m = next((x for x in L.PRIORITIES if x.lower() == v.lower()), None)
            if not m:
                self._issue("error", sheet, row, c.name, "BAD_VALUE", "priority must be High, Medium or Low")
            return m
        if name == "location_type":
            m = next((x for x in L.LOCATION_TYPES if x == v.lower().replace(" ", "_").replace("-", "_")), None)
            if not m:
                self._issue("error", sheet, row, c.name, "BAD_VALUE", "location_type must be primary, service_area, target_area or other")
            return m
        return v


Parser.make_fact = Parser._make_fact      # type: ignore[attr-defined]
Parser.check_text = Parser._text_ok       # type: ignore[attr-defined]
