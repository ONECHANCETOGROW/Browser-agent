"""Business Profile storage (SQLite) with source tracking, verification status, conflict detection and per-field protection of HIGH values.

One row in `facts` is one CANDIDATE value for a field. The status of a FIELD is derived from its candidates, so it cannot drift:
  MISSING      no live candidate
  VERIFIED     live candidates all agree and at least one was entered/confirmed by the user
  UNVERIFIED   live candidates all agree but none has been confirmed by the user (document / import)
  CONFLICTING  live candidates disagree (never resolved automatically)

Nothing here accepts a value from a model: there is no source type for one. Verification is a human act (`USER_ENTERED`, `USER_CONFIRMED`, or an
import the user explicitly vouches for with assume_verified).
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..sensitive import scrub_text
from . import records
from .cipher import Cipher, CipherError, default_cipher
from .schema import CATALOGS, CATALOG_VERSION, CLIENT, LEVEL_RANK, LEVELS, ME, KeyError_, ParsedKey, parse_key
from .validators import VALIDATORS, normalize_for_compare, secret_reason

SCHEMA_VERSION = 4   # v2: facts/documents/folders/audit carry a SCOPE (the owner's own business, or one client); clients and research observations exist
                     # v4: import sessions carry a kind (spreadsheet / markdown), a scope and a verification state; import_items records what each import did with each value (masks only)
                     # v3: clients may carry a reference (the id used in the owner's spreadsheet); services/locations/SEO-target rows, import sessions and per-value sources exist
MISSING, VERIFIED, UNVERIFIED, CONFLICTING = "MISSING", "VERIFIED", "UNVERIFIED", "CONFLICTING"
SOURCES = ("USER_ENTERED", "USER_CONFIRMED", "DOCUMENT", "IMPORT")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS facts (
  fact_id TEXT PRIMARY KEY, field_key TEXT NOT NULL, instance INTEGER,
  value_plain TEXT, value_enc BLOB, display_mask TEXT, value_hash TEXT,
  sensitivity TEXT NOT NULL, state TEXT NOT NULL, verification TEXT NOT NULL,
  source_type TEXT NOT NULL, source_ref TEXT, doc_id TEXT,
  format_ok INTEGER, format_note TEXT,
  confirmed_at REAL, created_at REAL, updated_at REAL, supersedes TEXT,
  scope TEXT NOT NULL DEFAULT 'me'
);
CREATE TABLE IF NOT EXISTS clients (
  client_id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, created_at REAL, updated_at REAL, ref TEXT
);
CREATE TABLE IF NOT EXISTS enrolled_folders (folder_id TEXT PRIMARY KEY, path TEXT NOT NULL, label TEXT, added_at REAL, client_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, folder_id TEXT NOT NULL, rel_path TEXT NOT NULL, ext TEXT, size INTEGER, sha256 TEXT, mtime REAL,
  doc_type TEXT, label TEXT, sensitivity TEXT, state TEXT, verification TEXT, source TEXT,
  indexed_at REAL, extractor TEXT, extract_note TEXT, fields_found INTEGER DEFAULT 0, created_at REAL, updated_at REAL,
  client_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event TEXT, task_id TEXT, field_key TEXT, fact_id TEXT, doc_id TEXT,
  provider TEXT, locality TEXT, actor TEXT, outcome TEXT, detail TEXT, scope TEXT NOT NULL DEFAULT ''
);
-- Research OBSERVATIONS (architecture for the future SEO Intelligence phase). Append-only, timestamped, never permanent facts.
CREATE TABLE IF NOT EXISTS research_sessions (
  session_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, objective TEXT, query TEXT, location TEXT, search_engine TEXT,
  started_at REAL, finished_at REAL, status TEXT, findings TEXT, recommendations TEXT
);
CREATE TABLE IF NOT EXISTS research_observations (
  obs_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, client_id TEXT NOT NULL, kind TEXT NOT NULL,
  query TEXT, location TEXT, search_engine TEXT, result_type TEXT, position INTEGER, url TEXT, domain TEXT, business TEXT,
  observed_at REAL NOT NULL, evidence TEXT, source TEXT, data TEXT
);
""" + records.DDL + """
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS obs_no_update BEFORE UPDATE ON research_observations BEGIN SELECT RAISE(ABORT, 'research observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS obs_no_delete BEFORE DELETE ON research_observations BEGIN SELECT RAISE(ABORT, 'research observations are append-only'); END;
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS facts_field ON facts(scope, field_key, instance, state);
CREATE INDEX IF NOT EXISTS docs_client ON documents(client_id, state);
CREATE INDEX IF NOT EXISTS obs_client ON research_observations(client_id, kind, observed_at);
""" + records.INDEXES
# columns a v1 database lacks; added in place (after an automatic backup) by BusinessStore._migrate
V2_COLUMNS = [("facts", "scope", "TEXT NOT NULL DEFAULT 'me'"), ("enrolled_folders", "client_id", "TEXT NOT NULL DEFAULT ''"),
              ("documents", "client_id", "TEXT NOT NULL DEFAULT ''"), ("audit", "scope", "TEXT NOT NULL DEFAULT ''"), ("clients", "ref", "TEXT"),
              ("import_sessions", "kind", "TEXT NOT NULL DEFAULT 'spreadsheet'"), ("import_sessions", "scope", "TEXT NOT NULL DEFAULT ''"), ("import_sessions", "last_verified", "REAL"),
              ("import_sessions", "verification", "TEXT")]


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class FactInfo:
    """A candidate as shown to humans: NEVER contains the value, only its mask."""
    fact_id: str
    mask: str
    sensitivity: str
    verification: str
    state: str
    source_type: str
    source_ref: str
    doc_id: str | None
    format_ok: bool
    format_note: str
    updated_at: float
    confirmed_at: float | None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class FieldStatus:
    key: str                # display key, e.g. owners[0].name
    label: str
    group: str
    sensitivity: str
    status: str
    facts: list[FactInfo] = field(default_factory=list)
    scope: str = "me"

    @property
    def live(self) -> list[FactInfo]:
        return [f for f in self.facts if f.state == "LIVE"]

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "group": self.group, "sensitivity": self.sensitivity, "status": self.status, "scope": self.scope, "facts": [f.to_dict() for f in self.facts]}


@dataclass
class Resolved:
    """An exact value, for application code only. Never logged, never put in a prompt."""
    value: str
    fact_id: str
    sensitivity: str
    source_type: str
    key: str


class NotAvailable(Exception):
    """The field has no usable value. `status` is MISSING / UNVERIFIED / CONFLICTING / UNDECRYPTABLE / EXCLUDED."""

    def __init__(self, key: str, status: str, label: str = "", sources: list[str] | None = None):
        super().__init__(f"{key}: {status}")
        self.key, self.status, self.label, self.sources = key, status, label, sources or []


@dataclass
class PutResult:
    fact_id: str | None
    created: bool
    status: str
    warnings: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Prepared:
    """A value that passed validation and is ready to store (application code only: `canon` is the exact value and must never be logged or shown)."""
    pk: ParsedKey
    canon: str
    ok: bool
    note: str
    sens: str
    vhash: str
    mask: str


def mask_value(value: str, sensitivity: str, kind: str = "text") -> str:
    """A short masked form for lists and audit-free displays. Reveals as little as is useful."""
    v = value.strip()
    if kind == "address":
        try:
            d = json.loads(v)
            line = (d.get("line1") or "")[:5]
            pin = (d.get("pin") or "")[:2]
            return f"{line}•••" + (f", PIN {pin}••••" if pin else "")
        except ValueError:
            pass
    if kind == "email" and "@" in v:
        user, _, dom = v.partition("@")
        return f"{user[:1]}•••@{dom}"
    if kind == "phone":
        d = "".join(c for c in v if c.isdigit())
        return "•••••" + d[-3:] if len(d) >= 6 else "•••"
    if sensitivity == "LOW":
        return v if len(v) <= 40 else v[:37] + "…"
    n = len(v)
    if sensitivity == "HIGH":
        return "•" * n if n < 8 else v[:2] + "•" * (n - 4) + v[-2:]
    return v[:1] + "•" * max(2, min(n - 1, 8)) if n < 8 else v[:2] + "•" * min(n - 4, 8) + v[-2:]


class BusinessStore:
    def __init__(self, path: Path | str, cipher: Cipher | None = None, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher or default_cipher(self.path.parent)
        self._clock = clock
        self.migrated_from: int | None = None
        had_db = self.path.is_file() and self.path.stat().st_size > 0
        old_version = self._schema_version() if had_db else 0
        if had_db and 0 < old_version < SCHEMA_VERSION:
            self.migrated_from = old_version
            self.backup(tag=f"pre-v{SCHEMA_VERSION}")  # a forward migration always starts from a copy
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.executescript(INDEXES)
            self._meta_set(c, "schema_version", str(SCHEMA_VERSION))
            self._meta_set(c, "catalog_version", str(CATALOG_VERSION))
            if c.execute("SELECT 1 FROM schema_meta WHERE key='pepper'").fetchone() is None:
                self._meta_set(c, "pepper", base64.b64encode(self.cipher.encrypt(os.urandom(32))).decode())
            self._meta_set(c, "cipher", self.cipher.name, keep=True)
        self._pepper_cache: bytes | None = None

    def _schema_version(self) -> int:
        try:
            with self.conn() as c:
                r = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            return int(r["value"]) if r else 0
        except (sqlite3.DatabaseError, ValueError):
            return 0

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Forward-only, additive: existing rows keep their meaning (every v1 fact is the owner's own business, scope 'me')."""
        for table, col, ddl in V2_COLUMNS:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # ---------------------------------------------------------------- plumbing
    @contextlib.contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _meta_set(c: sqlite3.Connection, k: str, v: str, keep: bool = False) -> None:
        if keep and c.execute("SELECT 1 FROM schema_meta WHERE key=?", (k,)).fetchone():
            return
        c.execute("INSERT OR REPLACE INTO schema_meta VALUES (?,?)", (k, v))

    def meta(self, k: str) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT value FROM schema_meta WHERE key=?", (k,)).fetchone()
        return r["value"] if r else None

    def _pepper(self) -> bytes:
        if self._pepper_cache is None:
            raw = self.meta("pepper")
            try:
                self._pepper_cache = self.cipher.decrypt(base64.b64decode(raw))
            except (CipherError, TypeError, ValueError) as e:
                raise StoreError("UNDECRYPTABLE", "the Business Brain key material cannot be read by this Windows user") from e
        return self._pepper_cache

    def _hash(self, kind: str, value: str) -> str:
        return hmac.new(self._pepper(), (kind + ":" + normalize_for_compare(kind, value)).encode("utf-8"), hashlib.sha256).hexdigest()

    # ---------------------------------------------------------------- audit (same database; append-only by trigger)
    def audit(self, event: str, *, task_id: str = "", field_key: str = "", fact_id: str = "", doc_id: str = "", provider: str = "", locality: str = "",
              actor: str = "", outcome: str = "", detail: str = "", scope: str = "", c: sqlite3.Connection | None = None) -> None:
        """Record WHAT happened and WHY, never a value. `detail` is scrubbed again defensively (registered values, identifier shapes).
        Pass `c` to write inside a transaction that is already open (SQLite allows one writer at a time)."""
        with (contextlib.nullcontext(c) if c is not None else self.conn()) as c:
            c.execute("INSERT INTO audit(ts,event,task_id,field_key,fact_id,doc_id,provider,locality,actor,outcome,detail,scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self._clock(), event, task_id, field_key, fact_id, doc_id, provider, locality, actor, outcome, scrub_text(str(detail))[:300], "" if scope == ME else scope))

    def audit_tail(self, n: int = 50, task_id: str | None = None, event: str | None = None, scope: str | None = None) -> list[dict]:
        """scope: None = every scope; 'me' = the owner's own business; a client id = that client only."""
        q, args = "SELECT * FROM audit", []
        conds = []
        if scope is not None:
            conds.append("scope=?")
            args.append("" if scope == ME else scope)
        if task_id:
            conds.append("task_id=?")
            args.append(task_id)
        if event:
            conds.append("event=?")
            args.append(event)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY seq DESC LIMIT ?"
        with self.conn() as c:
            rows = c.execute(q, (*args, n)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_audit(self, older_than_days: int) -> int:
        cutoff = self._clock() - older_than_days * 86400
        with self.conn() as c:
            c.execute("DROP TRIGGER IF EXISTS audit_no_delete")
            n = c.execute("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
            c.execute("CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;")
        return n

    # ---------------------------------------------------------------- scopes and clients
    @staticmethod
    def kind_of(scope: str) -> str:
        return ME if scope == ME else CLIENT

    def _check_scope(self, c: sqlite3.Connection, scope: str, *, writing: bool) -> None:
        if scope == ME:
            return
        r = c.execute("SELECT archived FROM clients WHERE client_id=?", (scope,)).fetchone()
        if r is None:
            raise StoreError("NO_SUCH_CLIENT", f"no client {scope!r}")
        if writing and r["archived"]:
            raise StoreError("CLIENT_ARCHIVED", "that client is archived: restore it before changing anything")

    @staticmethod
    def clean_ref(ref: str | None) -> str | None:
        r = " ".join((ref or "").split())
        if not r:
            return None
        if len(r) > 60 or secret_reason("", r):
            raise StoreError("BAD_REF", "a client reference is a short label such as CLIENT-001")
        return r

    def create_client(self, name: str, *, actor: str = "cli", allow_duplicate: bool = False, ref: str | None = None) -> dict:
        name = " ".join((name or "").split())
        if not name or len(name) > 120:
            raise StoreError("BAD_NAME", "a client needs a name (up to 120 characters)")
        why = secret_reason("", name)
        if why:
            raise StoreError("SECRET_NOT_STORABLE", f"that {why}")
        with self.conn() as c:
            if not allow_duplicate and c.execute("SELECT 1 FROM clients WHERE lower(name)=lower(?) AND archived=0", (name,)).fetchone():
                raise StoreError("DUPLICATE_CLIENT", f"a client called {name!r} already exists (they would be impossible to tell apart when you say which client a task is for)")
            cid = "cli_" + uuid.uuid4().hex[:8]
            now = self._clock()
            ref = self.clean_ref(ref)
            if ref and c.execute("SELECT 1 FROM clients WHERE lower(ref)=lower(?)", (ref,)).fetchone():
                raise StoreError("DUPLICATE_REF", "another client already has that reference")
            c.execute("INSERT INTO clients (client_id,name,archived,created_at,updated_at,ref) VALUES (?,?,?,?,?,?)", (cid, name, 0, now, now, ref))
        self.audit("client_created", scope=cid, actor=actor, outcome="created", detail=name[:60])
        return self.get_client(cid)

    def get_client(self, client_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not r:
            raise StoreError("NO_SUCH_CLIENT", f"no client {client_id!r}")
        return {"client_id": r["client_id"], "name": r["name"], "archived": bool(r["archived"]), "created_at": r["created_at"], "updated_at": r["updated_at"], "ref": r["ref"] or ""}

    def list_clients(self, include_archived: bool = False) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM clients" + ("" if include_archived else " WHERE archived=0") + " ORDER BY lower(name)").fetchall()
        return [{"client_id": r["client_id"], "name": r["name"], "archived": bool(r["archived"]), "created_at": r["created_at"], "updated_at": r["updated_at"], "ref": r["ref"] or ""} for r in rows]

    def find_client(self, text: str) -> dict:
        """An exact id, a spreadsheet reference (CLIENT-001) or a case-insensitive exact name; ambiguous or unknown raises (a task must never guess which client you meant)."""
        t = (text or "").strip()
        rows = [c for c in self.list_clients(include_archived=True) if c["client_id"] == t or c["name"].lower() == t.lower() or (c["ref"] and c["ref"].lower() == t.lower())]
        if not rows:
            raise StoreError("NO_SUCH_CLIENT", f"no client matches {t!r}")
        if len(rows) > 1:
            raise StoreError("AMBIGUOUS_CLIENT", f"{len(rows)} clients are called {t!r}: use the client id")
        return rows[0]

    def rename_client(self, client_id: str, name: str, *, actor: str = "cli") -> dict:
        name = " ".join((name or "").split())
        if not name:
            raise StoreError("BAD_NAME", "a client needs a name")
        with self.conn() as c:
            self._check_scope(c, client_id, writing=True)
            c.execute("UPDATE clients SET name=?, updated_at=? WHERE client_id=?", (name, self._clock(), client_id))
        self.audit("client_renamed", scope=client_id, actor=actor, outcome="renamed")
        return self.get_client(client_id)

    def set_client_archived(self, client_id: str, archived: bool, *, actor: str = "cli") -> dict:
        with self.conn() as c:
            self._check_scope(c, client_id, writing=False)
            c.execute("UPDATE clients SET archived=?, updated_at=? WHERE client_id=?", (int(archived), self._clock(), client_id))
        self.audit("client_archived" if archived else "client_restored", scope=client_id, actor=actor, outcome="ok")
        return self.get_client(client_id)

    # ---------------------------------------------------------------- value (de)serialisation
    @staticmethod
    def _kind(pk: ParsedKey) -> str:
        return "address" if pk.field.compound else pk.field.kind

    def _canon_value(self, pk: ParsedKey, value: Any) -> str:
        if pk.field.compound:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise StoreError("BAD_VALUE", f"{pk.field.key} is an address: give its parts ({', '.join(p.name for p in pk.field.parts)}) as a JSON object") from None
            if not isinstance(value, dict):
                raise StoreError("BAD_VALUE", f"{pk.field.key} needs an object with its parts")
            unknown = [k for k in value if not pk.field.part(k)]
            if unknown:
                raise StoreError("BAD_VALUE", f"unknown part(s) {unknown}; parts are {[p.name for p in pk.field.parts]}")
            clean = {k: str(v).strip() for k, v in value.items() if str(v).strip()}
            if not clean:
                raise StoreError("BAD_VALUE", "an address needs at least one part")
            return json.dumps(clean, ensure_ascii=False, sort_keys=True)
        if not isinstance(value, str):
            value = str(value)
        v = value.strip()
        if not v:
            raise StoreError("BAD_VALUE", "the value is empty")
        if len(v) > 2000:
            raise StoreError("BAD_VALUE", "the value is too long")
        return v

    def _validate(self, pk: ParsedKey, canon: str) -> tuple[bool, str]:
        if pk.field.compound:
            d = json.loads(canon)
            notes = []
            for name, v in d.items():
                ok, note = VALIDATORS[pk.field.part(name).kind](v)
                if not ok:
                    notes.append(f"{name}: {note}")
            return not notes, "; ".join(notes)
        return VALIDATORS.get(pk.field.kind, VALIDATORS["none"])(canon)

    def _compare_hash(self, pk: ParsedKey, canon: str) -> str:
        if pk.field.compound:
            d = json.loads(canon)
            norm = json.dumps({k: normalize_for_compare("pin" if k == "pin" else "text", v) for k, v in sorted(d.items())}, ensure_ascii=False)
            return self._hash("address", norm)
        return self._hash(pk.field.kind, canon)

    def _read_value(self, row: sqlite3.Row) -> str:
        if row["value_enc"] is not None:
            return self.cipher.decrypt(bytes(row["value_enc"])).decode("utf-8")
        return row["value_plain"]

    @staticmethod
    def _inst(pk: ParsedKey) -> int:
        return pk.instance if pk.instance is not None else -1

    # ---------------------------------------------------------------- writes (humans only: CLI / authenticated panel)
    def put_candidate(self, key: str, value: Any, *, source_type: str, source_ref: str = "", doc_id: str | None = None, assume_verified: bool = False,
                      replace: bool = False, sensitivity: str | None = None, allow_lower: bool = False, actor: str = "cli", scope: str = ME) -> PutResult:
        if source_type not in SOURCES or source_type == "USER_CONFIRMED":
            raise StoreError("BAD_SOURCE", "a value can only enter as USER_ENTERED, DOCUMENT or IMPORT; confirmation is a separate user action")
        try:
            pk = parse_key(key, self.kind_of(scope))
        except KeyError_ as e:
            raise StoreError("UNKNOWN_FIELD", str(e)) from None
        with self.conn() as c0:
            self._check_scope(c0, scope, writing=True)
        if pk.part not in (None, "full"):
            raise StoreError("BAD_VALUE", f"set the whole {pk.field.key}, not one part of it")
        canon = self._canon_value(pk, value)
        why = secret_reason(pk.field.key.split(".", 1)[-1] if pk.field.custom else "", canon if not pk.field.compound else " ".join(json.loads(canon).values()))
        if why:
            self.audit("rejected_secret", field_key=pk.display(), scope=scope, actor=actor, outcome="refused", detail=why)
            raise StoreError("SECRET_NOT_STORABLE", f"this {why}; passwords, OTPs, keys and payment credentials are never stored here. Enter it yourself in Chrome when asked.")
        sens = pk.field.sensitivity
        if sensitivity:
            if sensitivity not in LEVELS:
                raise StoreError("BAD_SENSITIVITY", f"sensitivity must be one of {LEVELS}")
            if LEVEL_RANK[sensitivity] < LEVEL_RANK[sens] and not allow_lower:
                raise StoreError("LOWERING_REFUSED", "lowering a field's sensitivity needs an explicit confirmation (CLI --allow-lower)")
            sens = sensitivity
        ok, note = self._validate(pk, canon)
        vhash = self._compare_hash(pk, canon)
        inst = self._inst(pk)
        verification = "VERIFIED" if source_type == "USER_ENTERED" or (source_type == "IMPORT" and assume_verified) else "UNVERIFIED"
        now = self._clock()
        warnings = [] if ok else [f"format: {note}"]
        with self.conn() as c:
            rows = c.execute("SELECT * FROM facts WHERE scope=? AND field_key=? AND instance=?", (scope, pk.storage_key, inst)).fetchall()
            same = [r for r in rows if r["value_hash"] == vhash]
            live_same = [r for r in same if r["state"] == "LIVE"]
            if live_same:
                self.audit("corroborated", field_key=pk.display(), fact_id=live_same[0]["fact_id"], actor=actor, outcome="duplicate", detail=f"same value from {source_type}", scope=scope, c=c)
                return PutResult(live_same[0]["fact_id"], False, self._derive(rows)[0], warnings, "the same value is already stored")
            if source_type != "USER_ENTERED" and same:
                self.audit("skipped_known", field_key=pk.display(), actor=actor, outcome="rejected_or_superseded_before", detail=source_type, scope=scope, c=c)
                return PutResult(None, False, self._derive(rows)[0], warnings, "this value was rejected or replaced earlier and is not re-added automatically")
            sup = None
            if replace:
                if source_type != "USER_ENTERED":
                    raise StoreError("REPLACE_REFUSED", "only a value the user enters directly can replace the current one")
                for r in rows:
                    if r["state"] == "LIVE":
                        c.execute("UPDATE facts SET state='SUPERSEDED', updated_at=? WHERE fact_id=?", (now, r["fact_id"]))
                        self.audit("fact_superseded", field_key=pk.display(), fact_id=r["fact_id"], actor=actor, outcome="replaced", scope=scope, c=c)
                        sup = r["fact_id"]
            fid = "fact_" + uuid.uuid4().hex[:10]
            enc = LEVEL_RANK[sens] >= LEVEL_RANK["HIGH"]
            c.execute("INSERT INTO facts (fact_id,field_key,instance,value_plain,value_enc,display_mask,value_hash,sensitivity,state,verification,source_type,source_ref,doc_id,"
                      "format_ok,format_note,confirmed_at,created_at,updated_at,supersedes,scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (fid, pk.storage_key, inst, None if enc else canon, self.cipher.encrypt(canon.encode("utf-8")) if enc else None,
                       mask_value(canon, sens, self._kind(pk)), vhash, sens, "LIVE", verification, source_type, source_ref[:120], doc_id, int(ok), note[:200],
                       now if verification == "VERIFIED" else None, now, now, sup, scope))
            rows = c.execute("SELECT * FROM facts WHERE scope=? AND field_key=? AND instance=?", (scope, pk.storage_key, inst)).fetchall()
        status = self._derive(rows)[0]
        self.audit("fact_added", field_key=pk.display(), fact_id=fid, scope=scope, actor=actor, outcome=verification, detail=f"{source_type} {sens}" + (f" doc={doc_id}" if doc_id else ""))
        if status == CONFLICTING:
            self.audit("conflict_created", field_key=pk.display(), fact_id=fid, scope=scope, actor=actor, outcome="needs_user")
            warnings.append("this value differs from another stored value: the field is now CONFLICTING until you resolve it")
        return PutResult(fid, True, status, warnings)

    def _fact(self, c: sqlite3.Connection, fact_id: str) -> sqlite3.Row:
        r = c.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        if not r:
            raise StoreError("NO_SUCH_FACT", f"no fact {fact_id}")
        return r

    def confirm(self, fact_id: str, *, actor: str = "cli") -> FieldStatus:
        """The user states: this value is correct. UNVERIFIED -> VERIFIED (source becomes USER_CONFIRMED). Other differing candidates stay: the field
        remains CONFLICTING until resolve()."""
        with self.conn() as c:
            r = self._fact(c, fact_id)
            if r["state"] != "LIVE":
                raise StoreError("NOT_LIVE", "only a current candidate can be confirmed")
            if r["verification"] != "VERIFIED":
                now = self._clock()
                c.execute("UPDATE facts SET verification='VERIFIED', source_type='USER_CONFIRMED', confirmed_at=?, updated_at=? WHERE fact_id=?", (now, now, fact_id))
        self.audit("fact_confirmed", field_key=r["field_key"], fact_id=fact_id, scope=r["scope"], actor=actor, outcome="verified")
        return self.status(self._display(r), r["scope"])

    def reject(self, fact_id: str, *, actor: str = "cli") -> FieldStatus:
        with self.conn() as c:
            r = self._fact(c, fact_id)
            c.execute("UPDATE facts SET state='REJECTED', updated_at=? WHERE fact_id=?", (self._clock(), fact_id))
        self.audit("fact_rejected", field_key=r["field_key"], fact_id=fact_id, scope=r["scope"], actor=actor, outcome="rejected")
        return self.status(self._display(r), r["scope"])

    def resolve(self, key: str, fact_id: str, *, actor: str = "cli", scope: str = ME) -> FieldStatus:
        """Resolve a conflict: the chosen candidate becomes USER_CONFIRMED / VERIFIED, every other live candidate is superseded."""
        pk = parse_key(key, self.kind_of(scope))
        inst = self._inst(pk)
        with self.conn() as c:
            r = self._fact(c, fact_id)
            if r["scope"] != scope or r["field_key"] != pk.storage_key or r["instance"] != inst or r["state"] != "LIVE":
                raise StoreError("WRONG_FACT", "that candidate does not belong to this field or is no longer current")
            now = self._clock()
            for o in c.execute("SELECT fact_id FROM facts WHERE scope=? AND field_key=? AND instance=? AND state='LIVE' AND fact_id<>?", (scope, pk.storage_key, inst, fact_id)).fetchall():
                c.execute("UPDATE facts SET state='SUPERSEDED', updated_at=? WHERE fact_id=?", (now, o["fact_id"]))
                self.audit("fact_superseded", field_key=pk.display(), fact_id=o["fact_id"], scope=scope, actor=actor, outcome="conflict_resolved", c=c)
            c.execute("UPDATE facts SET verification='VERIFIED', source_type='USER_CONFIRMED', confirmed_at=?, updated_at=? WHERE fact_id=?", (now, now, fact_id))
        self.audit("conflict_resolved", field_key=pk.display(), fact_id=fact_id, scope=scope, actor=actor, outcome="chosen")
        return self.status(key, scope)

    def delete_fact(self, fact_id: str, *, actor: str = "cli") -> None:
        with self.conn() as c:
            r = self._fact(c, fact_id)
            c.execute("DELETE FROM facts WHERE fact_id=?", (fact_id,))
        self.audit("fact_deleted", field_key=r["field_key"], fact_id=fact_id, scope=r["scope"], actor=actor, outcome="deleted")

    # ---------------------------------------------------------------- primitives for a multi-row import (every one takes the caller's open transaction `c`)
    def prepare(self, key: str, value: Any, *, kind: str, min_sensitivity: str | None = None) -> "Prepared":
        """Validate a value for a catalog key WITHOUT writing: the canonical text, its format check, its sensitivity (never lower than the field's own) and its comparison hash.
        Raises StoreError for an unknown field, an unusable value or something that must never be stored (password / OTP / key / card). Error text never contains the value."""
        try:
            pk = parse_key(key, kind)
        except KeyError_ as e:
            raise StoreError("UNKNOWN_FIELD", str(e)) from None
        if pk.part not in (None, "full"):
            raise StoreError("BAD_VALUE", f"set the whole {pk.field.key}, not one part of it")
        canon = self._canon_value(pk, value)
        why = secret_reason(pk.field.key.split(".", 1)[-1] if pk.field.custom else "", canon if not pk.field.compound else " ".join(json.loads(canon).values()))
        if why:
            raise StoreError("SECRET_NOT_STORABLE", f"this {why}; passwords, OTPs, keys and payment credentials are never stored here")
        sens = pk.field.sensitivity
        if min_sensitivity and LEVEL_RANK.get(min_sensitivity, 0) > LEVEL_RANK[sens]:
            sens = min_sensitivity
        ok, note = self._validate(pk, canon)
        return Prepared(pk, canon, ok, note, sens, self._compare_hash(pk, canon), mask_value(canon, sens, self._kind(pk)))

    def fact_rows(self, c: sqlite3.Connection, scope: str, pk: ParsedKey) -> list[sqlite3.Row]:
        return c.execute("SELECT * FROM facts WHERE scope=? AND field_key=? AND instance=? ORDER BY created_at", (scope, pk.storage_key, self._inst(pk))).fetchall()

    def insert_fact(self, c: sqlite3.Connection, prep: "Prepared", *, scope: str, source_type: str, source_ref: str, verification: str, supersede: list[str] | None = None,
                    actor: str = "import", detail: str = "") -> str:
        """Add one LIVE candidate inside the caller's transaction; the listed live candidates (if any) become SUPERSEDED. Audited without the value."""
        pk, now = prep.pk, self._clock()
        for fid in supersede or []:
            c.execute("UPDATE facts SET state='SUPERSEDED', updated_at=? WHERE fact_id=? AND state='LIVE'", (now, fid))
            self.audit("fact_superseded", field_key=pk.display(), fact_id=fid, scope=scope, actor=actor, outcome="replaced_by_import", c=c)
        fid = "fact_" + uuid.uuid4().hex[:10]
        enc = LEVEL_RANK[prep.sens] >= LEVEL_RANK["HIGH"]
        c.execute("INSERT INTO facts (fact_id,field_key,instance,value_plain,value_enc,display_mask,value_hash,sensitivity,state,verification,source_type,source_ref,doc_id,"
                  "format_ok,format_note,confirmed_at,created_at,updated_at,supersedes,scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (fid, pk.storage_key, self._inst(pk), None if enc else prep.canon, self.cipher.encrypt(prep.canon.encode("utf-8")) if enc else None, prep.mask, prep.vhash,
                   prep.sens, "LIVE", verification, source_type, source_ref[:120], None, int(prep.ok), prep.note[:200], now if verification == "VERIFIED" else None, now, now,
                   (supersede or [None])[0], scope))
        self.audit("fact_added", field_key=pk.display(), fact_id=fid, scope=scope, actor=actor, outcome=verification, detail=f"{source_type} {prep.sens}" + (f" {detail}" if detail else ""), c=c)
        return fid

    def insert_client(self, c: sqlite3.Connection, name: str, ref: str | None, *, actor: str = "import") -> str:
        name = " ".join((name or "").split())
        if not name or len(name) > 120:
            raise StoreError("BAD_NAME", "a client needs a name (up to 120 characters)")
        why = secret_reason("", name)
        if why:
            raise StoreError("SECRET_NOT_STORABLE", f"that {why}")
        if c.execute("SELECT 1 FROM clients WHERE lower(name)=lower(?) AND archived=0", (name,)).fetchone():
            raise StoreError("DUPLICATE_CLIENT", "a client with that exact name already exists")
        ref = self.clean_ref(ref)
        if ref and c.execute("SELECT 1 FROM clients WHERE lower(ref)=lower(?)", (ref,)).fetchone():
            raise StoreError("DUPLICATE_REF", "another client already has that reference")
        cid, now = "cli_" + uuid.uuid4().hex[:8], self._clock()
        c.execute("INSERT INTO clients (client_id,name,archived,created_at,updated_at,ref) VALUES (?,?,?,?,?,?)", (cid, name, 0, now, now, ref))
        self.audit("client_created", scope=cid, actor=actor, outcome="created", detail="by spreadsheet import", c=c)
        return cid

    def update_client(self, c: sqlite3.Connection, client_id: str, *, name: str | None = None, ref: str | None = None, actor: str = "import") -> None:
        self._check_scope(c, client_id, writing=True)
        if name is not None:
            name = " ".join(name.split())
            if not name or len(name) > 120:
                raise StoreError("BAD_NAME", "a client needs a name (up to 120 characters)")
            if c.execute("SELECT 1 FROM clients WHERE lower(name)=lower(?) AND archived=0 AND client_id<>?", (name, client_id)).fetchone():
                raise StoreError("DUPLICATE_CLIENT", "a client with that exact name already exists")
            c.execute("UPDATE clients SET name=?, updated_at=? WHERE client_id=?", (name, self._clock(), client_id))
            self.audit("client_renamed", scope=client_id, actor=actor, outcome="renamed_by_import", c=c)
        if ref is not None:
            ref = self.clean_ref(ref)
            if ref and c.execute("SELECT 1 FROM clients WHERE lower(ref)=lower(?) AND client_id<>?", (ref, client_id)).fetchone():
                raise StoreError("DUPLICATE_REF", "another client already has that reference")
            c.execute("UPDATE clients SET ref=?, updated_at=? WHERE client_id=?", (ref, self._clock(), client_id))
            self.audit("client_linked", scope=client_id, actor=actor, outcome="reference_set", detail="by spreadsheet import", c=c)

    def supersede_document_facts(self, doc_id: str, *, actor: str = "scanner") -> int:
        """A document changed on disk: what it used to say is no longer evidence."""
        with self.conn() as c:
            rows = c.execute("SELECT fact_id, field_key, scope FROM facts WHERE doc_id=? AND state='LIVE' AND source_type='DOCUMENT'", (doc_id,)).fetchall()
            for r in rows:
                c.execute("UPDATE facts SET state='SUPERSEDED', updated_at=? WHERE fact_id=?", (self._clock(), r["fact_id"]))
        for r in rows:
            self.audit("fact_superseded", field_key=r["field_key"], fact_id=r["fact_id"], doc_id=doc_id, scope=r["scope"], actor=actor, outcome="document_changed")
        return len(rows)

    def reveal(self, fact_id: str, *, actor: str = "cli") -> str:
        """The plain value, for the human who asked to see it. Audited (without the value)."""
        with self.conn() as c:
            r = self._fact(c, fact_id)
        try:
            v = self._read_value(r)
        except CipherError as e:
            raise StoreError("UNDECRYPTABLE", str(e)) from None
        self.audit("reveal", field_key=r["field_key"], fact_id=fact_id, scope=r["scope"], actor=actor, outcome="shown")
        return v

    # ---------------------------------------------------------------- reads
    @staticmethod
    def _display(r: sqlite3.Row) -> str:
        key = r["field_key"]
        return key.replace("[]", f"[{r['instance']}]") if r["instance"] is not None and r["instance"] >= 0 else key

    @staticmethod
    def _info(r: sqlite3.Row) -> FactInfo:
        return FactInfo(r["fact_id"], r["display_mask"] or "", r["sensitivity"], r["verification"], r["state"], r["source_type"], r["source_ref"] or "", r["doc_id"],
                        bool(r["format_ok"]), r["format_note"] or "", r["updated_at"], r["confirmed_at"])

    @staticmethod
    def _effective(default: str, rows: list[sqlite3.Row]) -> str:
        """A field is as sensitive as its most sensitive current value (the user may have raised it)."""
        levels = [r["sensitivity"] for r in rows if r["state"] == "LIVE"] or [default]
        return max(levels, key=lambda x: LEVEL_RANK.get(x, 0))

    @staticmethod
    def _derive(rows: list[sqlite3.Row]) -> tuple[str, list[sqlite3.Row]]:
        live = [r for r in rows if r["state"] == "LIVE"]
        if not live:
            return MISSING, live
        if len({r["value_hash"] for r in live}) > 1:
            return CONFLICTING, live
        return (VERIFIED if any(r["verification"] == "VERIFIED" for r in live) else UNVERIFIED), live

    def status(self, key: str, scope: str = ME) -> FieldStatus:
        pk = parse_key(key, self.kind_of(scope))
        inst = self._inst(pk)
        with self.conn() as c:
            self._check_scope(c, scope, writing=False)
            rows = c.execute("SELECT * FROM facts WHERE scope=? AND field_key=? AND instance=? ORDER BY created_at", (scope, pk.storage_key, inst)).fetchall()
        st, _ = self._derive(rows)
        return FieldStatus(pk.display(), pk.label, pk.field.group, self._effective(pk.sensitivity, rows), st, [self._info(r) for r in rows], scope)

    def get_value(self, key: str, scope: str = ME) -> Resolved:
        """The exact stored value for a field (or a part of a compound field) IN THIS SCOPE, only if the field is VERIFIED. Raises NotAvailable otherwise.
        There is no call that reads across scopes: a client's value can never be returned for the owner's field or for another client."""
        pk = parse_key(key, self.kind_of(scope))
        inst = self._inst(pk)
        with self.conn() as c:
            self._check_scope(c, scope, writing=False)
            rows = c.execute("SELECT * FROM facts WHERE scope=? AND field_key=? AND instance=?", (scope, pk.storage_key, inst)).fetchall()
        st, live = self._derive(rows)
        if st == MISSING:
            raise NotAvailable(pk.display(), MISSING, pk.label)
        if st == CONFLICTING:
            raise NotAvailable(pk.display(), CONFLICTING, pk.label, sorted({self._source_name(r) for r in live}))
        if st == UNVERIFIED:
            raise NotAvailable(pk.display(), UNVERIFIED, pk.label, sorted({self._source_name(r) for r in live}))
        row = next(r for r in live if r["verification"] == "VERIFIED")
        try:
            val = self._read_value(row)
        except CipherError:
            raise NotAvailable(pk.display(), "UNDECRYPTABLE", pk.label) from None
        if pk.field.compound:
            parts = json.loads(val)
            if pk.part == "full":
                order = [p.name for p in pk.field.parts]
                val = ", ".join(parts[n] for n in order if parts.get(n))
            else:
                if not parts.get(pk.part):
                    raise NotAvailable(pk.display(), MISSING, pk.label)
                val = parts[pk.part]
        return Resolved(val, row["fact_id"], row["sensitivity"], row["source_type"], pk.display())

    @staticmethod
    def _source_name(r: sqlite3.Row) -> str:
        return (f"document {r['doc_id']}" if r["doc_id"] else r["source_type"].lower().replace("_", " ")) + (f" ({r['source_ref']})" if r["source_ref"] and not r["doc_id"] else "")

    def list_fields(self, include_missing: bool = True, scope: str = ME) -> list[FieldStatus]:
        """Every catalog field of this scope (and stored custom fields) with its derived status and masked candidates. For the CLI and the panel."""
        with self.conn() as c:
            self._check_scope(c, scope, writing=False)
            rows = c.execute("SELECT * FROM facts WHERE scope=? ORDER BY created_at", (scope,)).fetchall()
        by: dict[tuple, list[sqlite3.Row]] = {}
        for r in rows:
            by.setdefault((r["field_key"], r["instance"]), []).append(r)
        out: list[FieldStatus] = []
        seen: set[tuple] = set()
        for f in CATALOGS[self.kind_of(scope)][0]:
            insts = sorted({i for (k, i) in by if k == f.key}) or ([0] if f.many else [-1])
            for i in insts:
                rs = by.get((f.key, i), [])
                seen.add((f.key, i))
                if rs or include_missing:
                    st, _ = self._derive(rs)
                    disp = f.key.replace("[]", f"[{i}]") if f.many else f.key
                    out.append(FieldStatus(disp, f.label, f.group, self._effective(f.sensitivity, rs), st, [self._info(r) for r in rs], scope))
        for (k, i), rs in by.items():
            if (k, i) in seen:
                continue
            st, _ = self._derive(rs)
            out.append(FieldStatus(k, k.split(".", 1)[-1].replace("_", " ").capitalize(), "other", rs[0]["sensitivity"], st, [self._info(r) for r in rs], scope))
        return out

    def plain_values(self, scope: str) -> dict[str, str]:
        """fact_id -> exact value, for the CURRENT LOW / MEDIUM values of one scope (HIGH values are stored encrypted and are never in this map). For the owner's own editing screens."""
        with self.conn() as c:
            self._check_scope(c, scope, writing=False)
            rows = c.execute("SELECT fact_id, value_plain FROM facts WHERE scope=? AND state='LIVE' AND value_plain IS NOT NULL AND sensitivity<>'HIGH'", (scope,)).fetchall()
        return {r["fact_id"]: r["value_plain"] for r in rows}

    def counts(self, scope: str | None = None) -> dict:
        """scope None = everything (all clients and the owner's own business)."""
        w, a = ("", ()) if scope is None else (" AND scope=?", (scope,))
        with self.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM facts WHERE state='LIVE'" + w, a).fetchone()[0]
            enc = c.execute("SELECT COUNT(*) FROM facts WHERE value_enc IS NOT NULL" + w, a).fetchone()[0]
            clients = c.execute("SELECT COUNT(*) FROM clients WHERE archived=0").fetchone()[0]
        return {"live_facts": n, "encrypted_facts": enc, "clients": clients, "cipher": self.cipher.name, "cipher_strong": self.cipher.strong}

    # ---------------------------------------------------------------- structured client records and where values came from (no values: masks and metadata only)
    def records(self, kind: str, client_id: str, *, include_history: bool = False) -> list[dict]:
        """Services ('service'), locations ('location') or SEO targets ('seo') of ONE client, with where each row came from."""
        if kind not in records.KINDS:
            raise StoreError("BAD_KIND", f"kind must be one of {sorted(records.KINDS)}")
        table = records.KINDS[kind]["table"]
        with self.conn() as c:
            self._check_scope(c, client_id, writing=False)
            rows = c.execute(f"SELECT * FROM {table} WHERE client_id=?" + ("" if include_history else " AND state='LIVE'") + " ORDER BY created_at, row_num, rec_id", (client_id,)).fetchall()
        return [records.as_dict(r) for r in rows]

    def seo_targets(self, client_id: str) -> list[dict]:
        """The structured input for the future research phase: one client's keyword + location targets (live rows only)."""
        return self.records("seo", client_id)

    def sources_of(self, key: str, scope: str = ME) -> list[dict]:
        """Every candidate of one field in one scope with its source: kind, verification, mask, and for spreadsheet imports the workbook / sheet / row / import session. Never the value."""
        pk = parse_key(key, self.kind_of(scope))
        with self.conn() as c:
            self._check_scope(c, scope, writing=False)
            rows = c.execute("SELECT f.*, s.session_id AS s_session, s.workbook AS s_workbook, s.sheet AS s_sheet, s.row_num AS s_row, s.column_name AS s_col, s.imported_at AS s_at "
                             "FROM facts f LEFT JOIN fact_sources s ON s.fact_id=f.fact_id WHERE f.scope=? AND f.field_key=? AND f.instance=? ORDER BY f.created_at", (scope, pk.storage_key, self._inst(pk))).fetchall()
        out = []
        for r in rows:
            out.append({"fact_id": r["fact_id"], "state": r["state"], "verification": r["verification"], "source_type": r["source_type"], "sensitivity": r["sensitivity"], "mask": r["display_mask"] or "",
                        "imported": ({"session": r["s_session"], "workbook": r["s_workbook"], "sheet": r["s_sheet"], "row": r["s_row"], "column": r["s_col"], "at": r["s_at"]} if r["s_session"] else None),
                        "doc_id": r["doc_id"], "created_at": r["created_at"]})
        return out

    # ---------------------------------------------------------------- health / backup
    def integrity_ok(self) -> bool:
        try:
            with self.conn() as c:
                return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    def backup(self, keep: int = 7, tag: str = "") -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock())) + (f"-{tag}" if tag else "")
        dest = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        with self.conn() as src:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        old = sorted(self.path.parent.glob(self.path.name + ".bak-*"))
        for p in old[:-keep]:
            with contextlib.suppress(OSError):
                p.unlink()
        return dest

    def export_masked(self, scope: str = ME) -> list[dict]:
        return [f.to_dict() for f in self.list_fields(include_missing=False, scope=scope)]
