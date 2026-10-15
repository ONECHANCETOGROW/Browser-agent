"""Authorized Documents: a registry of files inside ENROLLED folders. Nothing is read from anywhere else, and nothing is scanned unless asked.

Consent is two steps, both CLI-only (so neither the extension, a web page nor a model can widen what is read):
  1. `fs allow <dir>`                 (existing) the agent may read/write files in this folder for uploads/downloads
  2. `business docs enroll <dir>`     this folder is for the Business Brain; it must already be inside an authorized folder

A scan registers metadata (id, path, hash, size, type, sensitivity, source). Content is extracted by a sandboxed worker process, deterministically,
and only yields CANDIDATE facts (UNVERIFIED). A document is usable for upload only after the user APPROVES it, and every use re-validates
authorization, the real path, enrolment, the file hash and the size limit.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..files.authorization import AuthorizationError, AuthorizedFolders, safe_walk
from .extract import extract_text
from .extract.fields import extract_candidates
from .schema import ME
from .store import BusinessStore, StoreError

ALLOWED_EXT = {".txt", ".csv", ".json", ".docx", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
_MAGIC = {".pdf": (b"%PDF",), ".docx": (b"PK",), ".xlsx": (b"PK",), ".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8",), ".jpeg": (b"\xff\xd8",),
          ".tif": (b"II*\x00", b"MM\x00*"), ".tiff": (b"II*\x00", b"MM\x00*"), ".webp": (b"RIFF",)}
_BIDI = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩", "‎", "‏"}

# doc_type -> (label, filename keywords, text keywords, sensitivity)
DOC_TYPES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], str]] = {
    "gst_certificate": ("GST registration certificate", ("gst", "gstin", "reg-06", "reg06"), ("certificate of registration", "goods and services tax", "gstin"), "HIGH"),
    "pan_card": ("PAN card", ("pan",), ("permanent account number", "income tax department"), "HIGH"),
    "incorporation_certificate": ("Certificate of incorporation", ("incorporation", "coi", "moa", "aoa"), ("certificate of incorporation", "corporate identity number"), "HIGH"),
    "address_proof": ("Address proof", ("electricity", "utility", "rent", "lease", "address"), ("electricity bill", "rent agreement", "lease deed"), "HIGH"),
    "agreement": ("Agreement", ("agreement", "contract", "deed", "mou"), ("this agreement", "memorandum of understanding"), "HIGH"),
    "invoice": ("Invoice", ("invoice", "inv-"), ("tax invoice", "invoice no"), "MEDIUM"),
    "other": ("Other document", (), (), "HIGH"),
}
# never registered as usable: banking and identity-card material is out of scope in v1; secrets are never stored at all
_BLOCKED_NAME = re.compile(r"bank|passbook|cheque|statement|aadhaar|aadhar|passport|password|passwd|credential|secret|api[_ -]?key|\.env|id_rsa|wallet|seed ?phrase|otp", re.I)
_BLOCKED_REASON = "banking, identity-card and credential files are not supported in this version"
_MAX_NAME = 160


def classify_name(filename: str) -> str:
    n = filename.lower()
    for t, (_, kws, _, _) in DOC_TYPES.items():
        if any(re.search(r"(^|[^a-z])" + re.escape(k) + r"([^a-z]|$)", n) for k in kws):
            return t
    return "other"


def classify_text(text: str, fallback: str) -> str:
    low = text.lower()
    for t, (_, _, kws, _) in DOC_TYPES.items():
        if kws and any(k in low for k in kws):
            return t
    return fallback


@dataclass
class ScanReport:
    registered: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    blocked: int = 0
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DocNotAvailable(Exception):
    """`status`: MISSING / AMBIGUOUS / CHANGED / UNAUTHORIZED / UNAPPROVED."""

    def __init__(self, token: str, status: str, label: str = "", message: str = ""):
        super().__init__(f"{token}: {status}")
        self.token, self.status, self.label, self.message = token, status, label, message


def _sha256(path: str, limit: int) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            n += len(chunk)
            if n > limit:
                raise ValueError("too large")
            h.update(chunk)
    return h.hexdigest(), n


def _contained(real_child: str, real_root: str) -> bool:
    try:
        return os.path.commonpath([real_child, real_root]) == real_root
    except ValueError:
        return False


class DocumentRegistry:
    def __init__(self, store: BusinessStore, folders: AuthorizedFolders, *, max_bytes: int = 15 * 1024 * 1024, extract_timeout_s: float = 20.0):
        self.store, self.folders, self.max_bytes, self.timeout = store, folders, max_bytes, extract_timeout_s

    # ---------------------------------------------------------------- enrolment (CLI-only)
    @staticmethod
    def _cid(scope: str) -> str:
        return "" if scope == ME else scope

    def enrolled(self, scope: str | None = None) -> list[dict]:
        """scope None = every enrolled folder; 'me' = the owner's own business; a client id = that client's folders."""
        with self.store.conn() as c:
            if scope is None:
                return [dict(r) for r in c.execute("SELECT * FROM enrolled_folders ORDER BY added_at").fetchall()]
            return [dict(r) for r in c.execute("SELECT * FROM enrolled_folders WHERE client_id=? ORDER BY added_at", (self._cid(scope),)).fetchall()]

    def enroll(self, path: str, label: str | None = None, scope: str = ME) -> dict:
        with self.store.conn() as c0:
            self.store._check_scope(c0, scope, writing=True)  # a client must exist and not be archived
        try:
            real = str(self.folders.resolve_dir(path))  # must be inside an authorized folder, real path, no ADS/escape
        except AuthorizationError as e:
            raise StoreError(e.code, f"{e.message} Authorize it first with: python -m octg_agent fs allow <folder>") from None
        norm = os.path.normcase(os.path.realpath(real))
        with self.store.conn() as c:
            for r in c.execute("SELECT * FROM enrolled_folders").fetchall():
                if os.path.normcase(r["path"]) == norm:
                    if r["client_id"] != self._cid(scope):
                        raise StoreError("FOLDER_SCOPE", "that folder is already enrolled for a different owner (the owner's business or another client). A folder belongs to exactly one scope: "
                                                         "revoke it first, or use a separate folder.")
                    return dict(r)
            fid = "fld_" + hashlib.sha256(norm.encode()).hexdigest()[:8]
            c.execute("INSERT INTO enrolled_folders (folder_id,path,label,added_at,client_id) VALUES (?,?,?,?,?)", (fid, os.path.realpath(real), label, time.time(), self._cid(scope)))
        self.store.audit("folder_enrolled", scope=scope, outcome="enrolled", detail=label or "", actor="cli")
        return {"folder_id": fid, "path": os.path.realpath(real), "label": label, "client_id": self._cid(scope)}

    def revoke(self, path: str) -> bool:
        norm = os.path.normcase(os.path.realpath(path))
        with self.store.conn() as c:
            row = next((r for r in c.execute("SELECT * FROM enrolled_folders").fetchall() if os.path.normcase(r["path"]) == norm), None)
            if not row:
                return False
            c.execute("DELETE FROM enrolled_folders WHERE folder_id=?", (row["folder_id"],))
            n = c.execute("UPDATE documents SET state='MISSING', verification='UNVERIFIED', updated_at=? WHERE folder_id=?", (time.time(), row["folder_id"])).rowcount
        self.store.audit("folder_revoked", scope=row["client_id"] or ME, outcome="revoked", detail=f"{n} documents deactivated", actor="cli")
        return True

    # ---------------------------------------------------------------- scan (explicit command only)
    @staticmethod
    def _doc_id(folder_id: str, rel: str) -> str:
        return "doc_" + hashlib.sha256(f"{folder_id}|{os.path.normcase(rel)}".encode()).hexdigest()[:8]

    def scan(self, folder_path: str | None = None) -> ScanReport:
        rep = ScanReport()
        folders = self.enrolled()
        if folder_path:
            norm = os.path.normcase(os.path.realpath(folder_path))
            folders = [f for f in folders if os.path.normcase(f["path"]) == norm]
        for f in folders:
            self._scan_folder(f, rep)
        self.store.audit("docs_scanned", outcome="done", detail=f"{rep.registered} new, {rep.changed} changed, {rep.missing} missing, {len(rep.skipped)} skipped", actor="cli")
        return rep

    def _scan_folder(self, folder: dict, rep: ScanReport) -> None:
        root = os.path.realpath(folder["path"])
        if not os.path.isdir(root):
            with self.store.conn() as c:
                n = c.execute("UPDATE documents SET state='MISSING', updated_at=? WHERE folder_id=?", (time.time(), folder["folder_id"])).rowcount
            rep.missing += n
            return
        try:
            self.folders.resolve_dir(root)  # authorization may have been revoked since enrolment
        except AuthorizationError:
            rep.skipped.append(f"{folder['folder_id']}: no longer inside an authorized folder")
            return
        seen: set[str] = set()
        rn = os.path.normcase(root)
        # safe_walk never enters (or yields) a junction/symlink/reparse point, whether or not its target is inside the folder
        for dirpath, dirnames, filenames in safe_walk(root, max_depth=12, on_skip=lambda p, why: rep.skipped.append(f"{os.path.basename(p)[:30]}: {why}")):
            for fn in filenames:
                self._scan_file(folder, root, rn, dirpath, fn, rep, seen)
        with self.store.conn() as c:
            for r in c.execute("SELECT doc_id, state FROM documents WHERE folder_id=?", (folder["folder_id"],)).fetchall():
                if r["doc_id"] not in seen and r["state"] != "MISSING":
                    c.execute("UPDATE documents SET state='MISSING', verification='UNVERIFIED', updated_at=? WHERE doc_id=?", (time.time(), r["doc_id"]))
                    rep.missing += 1

    def _scan_file(self, folder: dict, root: str, rn: str, dirpath: str, fn: str, rep: ScanReport, seen: set[str]) -> None:
        ext = os.path.splitext(fn)[1].lower()
        full = os.path.join(dirpath, fn)
        if fn.startswith(("~$", ".")) or ext not in ALLOWED_EXT:
            return
        if len(fn) > _MAX_NAME or any(ch in _BIDI or unicodedata.category(ch) == "Cc" for ch in fn):
            rep.skipped.append("a file with an unsafe name was skipped")
            return
        real = os.path.realpath(full)
        if not _contained(os.path.normcase(real), rn) or os.path.islink(full):
            rep.skipped.append(f"{fn[:30]}: leads outside the enrolled folder (skipped)")
            return
        try:
            self.folders.resolve_file(real)
            sha, size = _sha256(real, self.max_bytes)
        except (AuthorizationError, OSError, ValueError):
            rep.skipped.append(f"{fn[:30]}: not readable, too large or not authorized")
            return
        rel = os.path.relpath(real, root)
        did = self._doc_id(folder["folder_id"], rel)
        seen.add(did)
        now = time.time()
        state, note = "DISCOVERED", ""
        if _BLOCKED_NAME.search(fn):
            state, note = "BLOCKED", _BLOCKED_REASON
        else:
            with open(real, "rb") as f:
                head = f.read(8)
            magic = _MAGIC.get(ext)
            if magic and not any(head.startswith(m) for m in magic):
                state, note = "BLOCKED", "the file's contents do not match its extension"
        dtype = classify_name(fn)
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM documents WHERE doc_id=?", (did,)).fetchone()
            if row is None:
                c.execute("INSERT INTO documents (doc_id,folder_id,rel_path,ext,size,sha256,mtime,doc_type,label,sensitivity,state,verification,source,extract_note,created_at,updated_at,client_id)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (did, folder["folder_id"], rel, ext, size, sha, os.path.getmtime(real), dtype, DOC_TYPES[dtype][0], DOC_TYPES[dtype][3], state, "UNVERIFIED",
                           f"enrolled folder {folder.get('label') or folder['folder_id']}", note, now, now, folder["client_id"]))
                rep.registered += 1
                self.store.audit("doc_registered", doc_id=did, scope=folder["client_id"] or ME, outcome=state, detail=dtype, actor="scanner", c=c)
            elif row["sha256"] != sha:
                c.execute("UPDATE documents SET size=?, sha256=?, mtime=?, state=?, verification='UNVERIFIED', extract_note=?, indexed_at=NULL, fields_found=0, updated_at=? WHERE doc_id=?",
                          (size, sha, os.path.getmtime(real), state, note or "changed since it was last registered: index and approve it again", now, did))
                rep.changed += 1
                self.store.audit("doc_changed", doc_id=did, outcome="reset", actor="scanner", c=c)
            else:
                rep.unchanged += 1
                if row["state"] == "MISSING":
                    c.execute("UPDATE documents SET state='DISCOVERED', updated_at=? WHERE doc_id=?", (now, did))
                return
        if row is not None and row["sha256"] != sha:
            self.store.supersede_document_facts(did)
        if state == "BLOCKED":
            rep.blocked += 1

    # ---------------------------------------------------------------- reading
    def _row(self, doc_id: str) -> sqlite3.Row:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if not r:
            raise StoreError("NO_SUCH_DOC", f"no document {doc_id}")
        return r

    def _abs(self, row: sqlite3.Row) -> str:
        with self.store.conn() as c:
            f = c.execute("SELECT * FROM enrolled_folders WHERE folder_id=?", (row["folder_id"],)).fetchone()
        if not f:
            raise DocNotAvailable(row["doc_id"], "UNAUTHORIZED", row["label"], "its folder is no longer enrolled")
        return os.path.join(os.path.realpath(f["path"]), row["rel_path"])

    def list(self, state: str | None = None, search: str | None = None, scope: str | None = None) -> list[dict]:
        """scope None = every document (CLI overview); 'me' = the owner's own; a client id = that client's only."""
        q, args = "SELECT * FROM documents", []
        cond = []
        if scope is not None:
            cond.append("client_id=?")
            args.append(self._cid(scope))
        if state:
            cond.append("state=?")
            args.append(state)
        if search:
            cond.append("(lower(rel_path) LIKE ? OR lower(label) LIKE ? OR lower(doc_type) LIKE ?)")
            args += [f"%{search.lower()}%"] * 3
        if cond:
            q += " WHERE " + " AND ".join(cond)
        with self.store.conn() as c:
            rows = c.execute(q + " ORDER BY created_at", args).fetchall()
        return [{"doc_id": r["doc_id"], "name": os.path.basename(r["rel_path"]), "doc_type": r["doc_type"], "label": r["label"], "sensitivity": r["sensitivity"], "state": r["state"],
                 "verification": r["verification"], "source": r["source"], "sha256": r["sha256"][:12], "size": r["size"], "fields_found": r["fields_found"], "note": r["extract_note"] or "", "client_id": r["client_id"]}
                for r in rows]

    # ---------------------------------------------------------------- indexing: sandboxed extraction + deterministic candidates
    def index(self, doc_id: str | None = None) -> dict:
        with self.store.conn() as c:
            ids = [doc_id] if doc_id else [r["doc_id"] for r in c.execute("SELECT doc_id FROM documents WHERE state='DISCOVERED'").fetchall()]
        done = {"indexed": 0, "candidates": 0, "failed": 0}
        for did in ids:
            row = self._row(did)
            if row["state"] in ("BLOCKED", "EXCLUDED", "MISSING"):
                continue
            try:
                path = self._abs(row)
                real = str(self.folders.resolve_file(path))
                sha, _ = _sha256(real, self.max_bytes)
            except (AuthorizationError, DocNotAvailable, OSError, ValueError):
                done["failed"] += 1
                continue
            if sha != row["sha256"]:
                self.store.audit("doc_changed", doc_id=did, outcome="hash_mismatch_at_index", actor="indexer")
                done["failed"] += 1
                continue
            res = extract_text(real, row["ext"], self.timeout)
            dtype, note, n = row["doc_type"], res.note, 0
            if res.ok:
                dtype = classify_text(res.text, row["doc_type"])
                for key, val in extract_candidates(res.text):
                    try:
                        # what a document says belongs to the owner of its folder: the owner's own profile, or that one client
                        r = self.store.put_candidate(key, val, source_type="DOCUMENT", source_ref=DOC_TYPES[dtype][0][:60], doc_id=did, actor="indexer", scope=row["client_id"] or ME)
                        n += 1 if r.created else 0
                    except StoreError:
                        continue
            else:
                done["failed"] += 1
            new_state = "APPROVED" if row["state"] == "APPROVED" else "INDEXED"
            with self.store.conn() as c:
                c.execute("UPDATE documents SET doc_type=?, label=?, sensitivity=?, state=?, indexed_at=?, extractor=?, extract_note=?, fields_found=fields_found+?, updated_at=? WHERE doc_id=?",
                          (dtype, DOC_TYPES[dtype][0], DOC_TYPES[dtype][3], new_state, time.time(), res.extractor, note[:200], n, time.time(), did))
            self.store.audit("doc_indexed", doc_id=did, scope=row["client_id"] or ME, outcome="ok" if res.ok else "no_text", detail=f"{dtype}; {n} candidate fact(s)", actor="indexer")
            done["indexed"] += 1
            done["candidates"] += n
        return done

    # ---------------------------------------------------------------- user decisions
    def approve(self, doc_id: str, *, actor: str = "cli") -> dict:
        row = self._row(doc_id)
        if row["state"] in ("BLOCKED", "EXCLUDED", "MISSING"):
            raise StoreError("NOT_APPROVABLE", f"a {row['state'].lower()} document cannot be approved")
        real = self._revalidate(row)
        sha, _ = _sha256(real, self.max_bytes)
        if sha != row["sha256"]:
            raise StoreError("CHANGED", "the file changed since it was registered: scan again, then approve")
        with self.store.conn() as c:
            c.execute("UPDATE documents SET state='APPROVED', verification='VERIFIED', updated_at=? WHERE doc_id=?", (time.time(), doc_id))
        self.store.audit("doc_approved", doc_id=doc_id, outcome="approved", actor=actor, detail=row["doc_type"])
        return self.list_one(doc_id)

    def exclude(self, doc_id: str, *, actor: str = "cli") -> dict:
        self._row(doc_id)
        with self.store.conn() as c:
            c.execute("UPDATE documents SET state='EXCLUDED', verification='UNVERIFIED', updated_at=? WHERE doc_id=?", (time.time(), doc_id))
        self.store.supersede_document_facts(doc_id, actor=actor)
        self.store.audit("doc_excluded", doc_id=doc_id, outcome="excluded", actor=actor)
        return self.list_one(doc_id)

    def list_one(self, doc_id: str) -> dict:
        return next(d for d in self.list() if d["doc_id"] == doc_id)

    def set_type(self, doc_id: str, doc_type: str, *, actor: str = "cli") -> dict:
        if doc_type not in DOC_TYPES:
            raise StoreError("BAD_TYPE", f"document type must be one of {sorted(DOC_TYPES)}")
        self._row(doc_id)
        with self.store.conn() as c:
            c.execute("UPDATE documents SET doc_type=?, label=?, sensitivity=?, updated_at=? WHERE doc_id=?", (doc_type, DOC_TYPES[doc_type][0], DOC_TYPES[doc_type][3], time.time(), doc_id))
        self.store.audit("doc_retyped", doc_id=doc_id, outcome=doc_type, actor=actor)
        return self.list_one(doc_id)

    # ---------------------------------------------------------------- use (upload): every check, every time
    def _revalidate(self, row: sqlite3.Row) -> str:
        path = self._abs(row)
        real = os.path.realpath(path)
        with self.store.conn() as c:
            f = c.execute("SELECT * FROM enrolled_folders WHERE folder_id=?", (row["folder_id"],)).fetchone()
        if not f or not _contained(os.path.normcase(real), os.path.normcase(os.path.realpath(f["path"]))):
            raise DocNotAvailable(row["doc_id"], "UNAUTHORIZED", row["label"], "the file is no longer inside its enrolled folder")
        try:
            return str(self.folders.resolve_file(real))  # existing authorization: allow-list, real path, no ADS
        except AuthorizationError as e:
            code = "MISSING" if e.code == "FILE_NOT_FOUND" else "UNAUTHORIZED"
            raise DocNotAvailable(row["doc_id"], code, row["label"], e.message) from None

    def approved_manifest(self, scope: str = ME) -> list[dict]:
        """[{token, label, count}] for the approved documents OF THIS SCOPE, grouped by type. No filenames, no paths."""
        with self.store.conn() as c:
            rows = c.execute("SELECT doc_type, label, COUNT(*) n FROM documents WHERE state='APPROVED' AND client_id=? GROUP BY doc_type, label", (self._cid(scope),)).fetchall()
        return [{"token": r["doc_type"], "label": r["label"], "count": r["n"]} for r in rows]

    def resolve_for_upload(self, token_id: str, scope: str = ME) -> tuple[str, dict]:
        """token id (a document type like gst_certificate, or a document id) -> (real path, info), WITHIN THIS SCOPE ONLY: a document of client A is never
        available to client B or to the owner's own tasks, even by its exact id. Raises DocNotAvailable."""
        cid = self._cid(scope)
        with self.store.conn() as c:
            if token_id.startswith("doc_"):
                rows = c.execute("SELECT * FROM documents WHERE doc_id=? AND client_id=?", (token_id, cid)).fetchall()
            else:
                rows = c.execute("SELECT * FROM documents WHERE doc_type=? AND state='APPROVED' AND client_id=?", (token_id, cid)).fetchall()
        if not rows:
            raise DocNotAvailable(token_id, "MISSING", DOC_TYPES.get(token_id, ("",))[0], "no approved document of that kind")
        if len(rows) > 1:
            raise DocNotAvailable(token_id, "AMBIGUOUS", rows[0]["label"], f"{len(rows)} approved documents match; the user must say which one")
        row = rows[0]
        if row["state"] != "APPROVED":
            raise DocNotAvailable(token_id, "UNAPPROVED", row["label"], "the document has not been approved by the user")
        real = self._revalidate(row)
        try:
            sha, size = _sha256(real, self.max_bytes)
        except (OSError, ValueError):
            raise DocNotAvailable(token_id, "MISSING", row["label"], "the file cannot be read or exceeds the upload limit") from None
        if sha != row["sha256"]:
            raise DocNotAvailable(token_id, "CHANGED", row["label"], "the file changed after it was approved")
        return real, {"doc_id": row["doc_id"], "label": row["label"], "sensitivity": row["sensitivity"], "name": os.path.basename(real), "doc_type": row["doc_type"]}
