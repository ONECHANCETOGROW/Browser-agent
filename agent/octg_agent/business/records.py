"""Structured client records that are not single facts: SERVICES, LOCATIONS and SEO TARGETS (keyword + location), plus import sessions and the source of every imported value.

Why separate tables instead of facts: a service, a place or a target "keyword in a location" is a ROW with several attributes that belong together (priority, category,
the location of a keyword...). The facts store keeps one value per field; these tables keep rows. Same rules as facts, though:
  * every row belongs to exactly ONE client (client_id is NOT NULL; nothing here reads across clients);
  * a row is never overwritten: a changed row is SUPERSEDED and a new LIVE row is inserted, so the history stays;
  * every row remembers where it came from (import session, workbook, sheet, row number) and its verification state;
  * nothing here is ever offered to a model (these tables are read by the local CLI/panel and, later, by the research phase through the same scoped reader).
The SQL takes a connection `c` so that an import writes everything in ONE transaction.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from ..sensitive import value_category
from .validators import secret_reason

DDL = """
CREATE TABLE IF NOT EXISTS import_sessions (
  session_id TEXT PRIMARY KEY, workbook TEXT, file_sha256 TEXT, created_at REAL, committed_at REAL, status TEXT,
  assume_verified INTEGER NOT NULL DEFAULT 0, counts TEXT, actor TEXT, note TEXT,
  kind TEXT NOT NULL DEFAULT 'spreadsheet', scope TEXT NOT NULL DEFAULT '', last_verified REAL, verification TEXT
);
-- what an import did with each value (masks only, never a value): the Import Center's "accepted / rejected / kept" lists
CREATE TABLE IF NOT EXISTS import_items (
  item_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, item_kind TEXT, scope TEXT, label TEXT, field_key TEXT, sensitivity TEXT, mask TEXT,
  decision TEXT, source_sheet TEXT, source_row INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS fact_sources (
  fact_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, workbook TEXT, sheet TEXT, row_num INTEGER, column_name TEXT, imported_at REAL
);
CREATE TABLE IF NOT EXISTS client_services (
  rec_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, service TEXT NOT NULL, rec_key TEXT NOT NULL, category TEXT, priority TEXT, notes TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'LOW', verification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'LIVE',
  session_id TEXT, workbook TEXT, sheet TEXT, row_num INTEGER, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS client_locations (
  rec_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, location TEXT, city TEXT, state_region TEXT, country TEXT, postal_code TEXT, rec_key TEXT NOT NULL,
  location_type TEXT, priority TEXT, notes TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'LOW', verification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'LIVE',
  session_id TEXT, workbook TEXT, sheet TEXT, row_num INTEGER, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS client_seo_targets (
  rec_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, keyword TEXT NOT NULL, location TEXT, service TEXT, rec_key TEXT NOT NULL, priority TEXT, notes TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'LOW', verification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'LIVE',
  session_id TEXT, workbook TEXT, sheet TEXT, row_num INTEGER, created_at REAL, updated_at REAL
);
"""
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS clients_ref ON clients(lower(ref)) WHERE ref IS NOT NULL AND ref <> '';
CREATE UNIQUE INDEX IF NOT EXISTS svc_live ON client_services(client_id, rec_key) WHERE state = 'LIVE';
CREATE UNIQUE INDEX IF NOT EXISTS loc_live ON client_locations(client_id, rec_key) WHERE state = 'LIVE';
CREATE UNIQUE INDEX IF NOT EXISTS seo_live ON client_seo_targets(client_id, rec_key) WHERE state = 'LIVE';
CREATE INDEX IF NOT EXISTS fact_sources_session ON fact_sources(session_id);
CREATE INDEX IF NOT EXISTS import_items_session ON import_items(session_id);
"""

# kind -> (table, identity columns (what makes it "the same record"), attribute columns (what may change), the column holding the first identity text)
KINDS: dict[str, dict[str, Any]] = {
    "service": {"table": "client_services", "attrs": ("service", "category", "priority", "notes")},
    "location": {"table": "client_locations", "attrs": ("location", "city", "state_region", "country", "postal_code", "location_type", "priority", "notes")},
    "seo": {"table": "client_seo_targets", "attrs": ("keyword", "location", "service", "priority", "notes")},
}
# the attributes that are part of a record's IDENTITY (changing them makes it a different record, not a changed one); the rest may change
IDENTITY = {"service": ("service",), "location": ("location", "city", "state_region", "country", "postal_code"), "seo": ("keyword", "location")}

PRIORITIES = ("High", "Medium", "Low")
LOCATION_TYPES = ("primary", "service_area", "target_area", "other")
MAX_ATTR = 300
MAX_NOTES = 2000

_WS = re.compile(r"\s+")


class RecordError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def clean_attr(kind: str, name: str, value) -> str:
    """One attribute of a service / location / SEO target, validated: length, no credential, no identifier-shaped value, priority and location type from their lists.
    Raises RecordError (the message never contains the value)."""
    v = " ".join(str(value).split()) if name != "notes" else str(value).strip()
    if not v:
        return ""
    limit = MAX_NOTES if name == "notes" else MAX_ATTR
    if len(v) > limit:
        raise RecordError("TOO_LONG", f"{name} is longer than {limit} characters")
    why = secret_reason("", v)
    if why:
        raise RecordError("SECRET_NOT_STORABLE", f"{name}: this {why}; it is never stored")
    cat = value_category(v)
    if cat:
        raise RecordError("REFUSED_VALUE", f"{name}: this looks like a {cat} identifier, which does not belong here")
    if name == "priority":
        m = next((x for x in PRIORITIES if x.lower() == v.lower()), None)
        if not m:
            raise RecordError("BAD_VALUE", "priority must be High, Medium or Low")
        return m
    if name == "location_type":
        m = next((x for x in LOCATION_TYPES if x == v.lower().replace(" ", "_").replace("-", "_")), None)
        if not m:
            raise RecordError("BAD_VALUE", "location type must be primary, service_area, target_area or other")
        return m
    return v


def clean_record(kind: str, rec: dict) -> dict:
    """A whole record: only the attributes of this kind, each validated, blanks dropped; the main text (service / keyword / location or city) is required."""
    attrs = KINDS[kind]["attrs"]
    unknown = [k for k in rec if k not in attrs]
    if unknown:
        raise RecordError("UNKNOWN_FIELD", f"unknown attribute(s) {unknown} for a {kind}")
    out = {a: clean_attr(kind, a, rec[a]) for a in attrs if rec.get(a) not in (None, "")}
    out = {k: v for k, v in out.items() if v}
    main = {"service": ("service",), "location": ("location", "city"), "seo": ("keyword",)}[kind]
    if not any(out.get(k) for k in main):
        raise RecordError("MISSING_VALUE", "this needs " + " or ".join(main))
    return out


def norm(text: str | None) -> str:
    """Comparison form: case-insensitive, whitespace-collapsed, punctuation at the ends dropped. The stored text is never altered."""
    return _WS.sub(" ", (text or "").strip()).strip(" .,;:-").casefold()


def rec_key(kind: str, rec: dict) -> str:
    return "|".join(norm(rec.get(k)) for k in IDENTITY[kind])


def _cols(kind: str) -> tuple[str, tuple[str, ...]]:
    k = KINDS[kind]
    return k["table"], k["attrs"]


def live_rows(c: sqlite3.Connection, kind: str, client_id: str) -> list[sqlite3.Row]:
    table, _ = _cols(kind)
    return c.execute(f"SELECT * FROM {table} WHERE client_id=? AND state='LIVE' ORDER BY created_at, row_num, rec_id", (client_id,)).fetchall()


def find_live(c: sqlite3.Connection, kind: str, client_id: str, key: str) -> sqlite3.Row | None:
    table, _ = _cols(kind)
    return c.execute(f"SELECT * FROM {table} WHERE client_id=? AND rec_key=? AND state='LIVE'", (client_id, key)).fetchone()


def any_row(c: sqlite3.Connection, kind: str, client_id: str, key: str) -> bool:
    table, _ = _cols(kind)
    return c.execute(f"SELECT 1 FROM {table} WHERE client_id=? AND rec_key=?", (client_id, key)).fetchone() is not None


def attrs_equal(kind: str, existing: sqlite3.Row, rec: dict) -> bool:
    """Same record content? Compared case-/space-insensitively on every attribute the SHEET supplies; a blank cell never counts as a difference (blank = no information)."""
    for a in _cols(kind)[1]:
        new = rec.get(a)
        if new in (None, ""):
            continue
        if norm(existing[a]) != norm(new):
            return False
    return True


def insert(c: sqlite3.Connection, kind: str, client_id: str, rec: dict, *, verification: str, sheet: str, workbook: str, session_id: str, row_num: int, now: float,
           supersede: str | None = None, merge_blank: bool = True) -> str:
    """Insert a LIVE record. If `supersede` names the current LIVE row it is marked SUPERSEDED first. Blank attributes of the sheet keep the existing row's value (blank = no information)."""
    table, attrs = _cols(kind)
    merged = dict(rec)
    if supersede:
        old = c.execute(f"SELECT * FROM {table} WHERE rec_id=?", (supersede,)).fetchone()
        for a in attrs:
            if merge_blank and merged.get(a) in (None, "") and old is not None and old[a]:
                merged[a] = old[a]
        c.execute(f"UPDATE {table} SET state='SUPERSEDED', updated_at=? WHERE rec_id=?", (now, supersede))
    rid = {"service": "svc_", "location": "loc_", "seo": "seo_"}[kind] + uuid.uuid4().hex[:10]
    sens = "MEDIUM" if merged.get("notes") else "LOW"
    cols = ["rec_id", "client_id", "rec_key", *attrs, "sensitivity", "verification", "state", "session_id", "workbook", "sheet", "row_num", "created_at", "updated_at"]
    vals = [rid, client_id, rec_key(kind, merged), *[merged.get(a) or None for a in attrs], sens, verification, "LIVE", session_id, workbook, sheet, row_num, now, now]
    c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    return rid


def add_manual(c: sqlite3.Connection, kind: str, client_id: str, rec: dict, *, now: float, supersede: str | None = None, sheet: str = "Knowledge Hub", order: int = 0) -> str:
    """A row the user typed (VERIFIED: it is their own entry). Same shape as an imported row, with the sheet naming where it came from."""
    return insert(c, kind, client_id, rec, verification="VERIFIED", sheet=sheet, workbook="", session_id="", row_num=order, now=now, supersede=supersede, merge_blank=False)


def retire(c: sqlite3.Connection, kind: str, rec_id: str, client_id: str, now: float) -> bool:
    """Take a row out of use without deleting it (state RETIRED stays as history)."""
    table, _ = _cols(kind)
    return c.execute(f"UPDATE {table} SET state='RETIRED', updated_at=? WHERE rec_id=? AND client_id=? AND state='LIVE'", (now, rec_id, client_id)).rowcount > 0


def get_row(c: sqlite3.Connection, kind: str, rec_id: str) -> sqlite3.Row | None:
    table, _ = _cols(kind)
    return c.execute(f"SELECT * FROM {table} WHERE rec_id=?", (rec_id,)).fetchone()


def verify_session(c: sqlite3.Connection, session_id: str, now: float) -> int:
    n = 0
    for k in KINDS:
        table, _ = _cols(k)
        n += c.execute(f"UPDATE {table} SET verification='VERIFIED', updated_at=? WHERE session_id=? AND state='LIVE' AND verification<>'VERIFIED'", (now, session_id)).rowcount
    return n


def as_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}
