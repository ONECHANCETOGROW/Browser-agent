"""BusinessBrain: the facade that owns the store and the document registry.

Two faces, deliberately separate:
  * `reader(task_id, instruction)`  -> BrainReader   (read-only; the ONLY thing handed to the task loop / a model-driven path)
  * `ui(command, msg)`              -> side-panel commands (confirm / reject / resolve / reveal / approve / exclude / views); authenticated like every panel command
Writes that WIDEN access (enrolling folders, lowering a sensitivity, importing values) exist only in the local CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from ..files.authorization import AuthorizedFolders
from .cipher import Cipher
from .documents import DocumentRegistry
from .hubconfig import HubConfig
from .research import ResearchLog
from .resolver import CLIENT_INTENT, BrainReader, pick_client
from .schema import ME
from .spreadsheet import ImportRefused, SpreadsheetImporter
from .store import BusinessStore, StoreError

REVEAL_SECONDS = 10
PANEL_IMPORT_MAX_BYTES = 3 * 1024 * 1024   # a workbook the panel may preview (the base64 text travels over the local socket)
IMPORT_CLI = 'python -m octg_agent business import "<your workbook>.xlsx"'


@dataclass
class BusinessConfig:
    enabled: bool = False                 # feature flag: OFF until the owner turns it on
    intent_detection: bool = True         # activate for a task whose instruction mentions the user's business information
    max_upload_bytes: int = 15 * 1024 * 1024
    extract_timeout_s: float = 20.0
    audit_retention_days: int = 365

    @classmethod
    def from_dict(cls, d: dict | None) -> "BusinessConfig":
        c = cls()
        known = {f.name for f in fields(cls)}
        for k, v in (d or {}).items():
            if k in known:
                setattr(c, k, v)
        return c


class BusinessBrain:
    def __init__(self, data_dir: Path | str, folders: AuthorizedFolders, config: BusinessConfig | None = None, cipher: Cipher | None = None):
        self.config = config or BusinessConfig()
        self.data_dir = Path(data_dir)
        self.store = BusinessStore(self.data_dir / "business.db", cipher)
        self.docs = DocumentRegistry(self.store, folders, max_bytes=self.config.max_upload_bytes, extract_timeout_s=self.config.extract_timeout_s)
        self.folders = folders
        self.research = ResearchLog(self.store)
        self.healthy = self.store.integrity_ok()

    def reader(self, task_id: str, instruction: str, override: bool | None = None, client: str | None = None) -> BrainReader:
        """`client`: None = decide from the wording (a client is chosen only if exactly one client's NAME is in it, else the task asks you);
        "me" = the owner's own business; otherwise a client id or exact client name chosen by the user."""
        ok = self.healthy and override is not False
        cid = cname = creason = None
        cands: list[str] = []
        client_intent = bool(self.healthy and client != ME and CLIENT_INTENT.search(instruction or ""))
        if ok and client and client != ME:
            c = self.store.find_client(client)
            if c["archived"]:
                raise StoreError("CLIENT_ARCHIVED", "that client is archived")
            cid, cname, creason = c["client_id"], c["name"], "selected by you"
        elif ok and client_intent:
            pick = pick_client(self.store, instruction)
            if pick.client_id:
                cid, cname, creason = pick.client_id, pick.name, pick.reason
            cands = pick.candidates
            if not cid and not cands:   # the wording names no client: the client you chose as the DEFAULT in the Knowledge Hub (if any), instead of asking
                default = HubConfig(self.data_dir).active_scope()
                if default and default != ME:
                    try:
                        c = self.store.find_client(default)
                    except StoreError:
                        c = None
                    if c and not c["archived"]:
                        cid, cname, creason = c["client_id"], c["name"], "your default client in the Knowledge Hub"
        r = BrainReader(self.store, self.docs, task_id, instruction, intent=self.config.intent_detection, override=override if self.healthy else False,
                        client_id=cid, client_name=cname or "", client_reason=creason or "", client_intent=client_intent and cid is None)
        r.client_candidates = cands
        if not self.healthy:  # the database failed its integrity check: never trust it for a task; the user is told why and can still type things themselves
            r.reason = "the Business Brain database failed its integrity check"
        return r

    # ---------------------------------------------------------------- side-panel commands
    def ui(self, cmd: str, msg: dict, providers: list[dict] | None = None) -> dict:
        try:
            return {"ok": True, "biz": self._ui(cmd, msg, providers or [])}
        except StoreError as e:
            return {"ok": False, "error": e.message, "code": e.code}
        except ImportRefused as e:
            return {"ok": False, "error": e.message, "code": e.code}
        except Exception as e:  # noqa: BLE001 - a panel command must never take the agent down or echo a value
            return {"ok": False, "error": f"Business Brain error: {type(e).__name__}", "code": "BIZ_ERROR"}

    def _ui(self, cmd: str, m: dict, providers: list[dict]) -> Any:
        s = self.store
        if cmd == "biz_status":
            return {"enabled": True, "healthy": self.healthy, **s.counts(ME), "providers": providers, "folders": {"authorized": len(self.folders.roots()), "enrolled": len(self.docs.enrolled())},
                    "reveal_seconds": REVEAL_SECONDS}
        if cmd == "biz_profile":
            return {"fields": [f.to_dict() for f in s.list_fields(include_missing=True)]}
        if cmd == "biz_confirm":
            return {"field": s.confirm(str(m.get("fact_id", "")), actor="panel").to_dict()}
        if cmd == "biz_reject":
            return {"field": s.reject(str(m.get("fact_id", "")), actor="panel").to_dict()}
        if cmd == "biz_resolve":
            return {"field": s.resolve(str(m.get("key", "")), str(m.get("fact_id", "")), actor="panel", scope=str(m.get("client_id") or ME)).to_dict()}
        if cmd == "biz_reveal":
            return {"fact_id": str(m.get("fact_id", "")), "value": s.reveal(str(m.get("fact_id", "")), actor="panel"), "hide_after_s": REVEAL_SECONDS}
        if cmd == "biz_docs":
            return {"documents": self.docs.list(scope=ME)}
        if cmd == "biz_doc_approve":
            return {"document": self.docs.approve(str(m.get("doc_id", "")), actor="panel")}
        if cmd == "biz_doc_exclude":
            return {"document": self.docs.exclude(str(m.get("doc_id", "")), actor="panel")}
        if cmd == "biz_sources":
            return {"authorized": self.folders.roots(), "enrolled": self.docs.enrolled(ME), "cli": "python -m octg_agent business docs enroll <folder>"}
        if cmd == "biz_audit":
            return {"events": s.audit_tail(min(int(m.get("n", 40) or 40), 200), scope=ME)}
        # ---- spreadsheet import: the panel can PREVIEW a workbook (nothing is written, values are masked, the raw plan is dropped at once); committing an import stays in the terminal
        if cmd == "biz_imports":
            return {"imports": SpreadsheetImporter(s).sessions(20), "cli": IMPORT_CLI, "template_cli": "python -m octg_agent business template", "max_bytes": PANEL_IMPORT_MAX_BYTES}
        if cmd == "biz_import_preview":
            import base64
            import binascii
            name = str(m.get("name", "") or "workbook")[-80:]
            b64 = m.get("data_b64")
            if not isinstance(b64, str) or not b64 or len(b64) > PANEL_IMPORT_MAX_BYTES * 4 // 3 + 16:
                raise StoreError("BAD_UPLOAD", "choose an .xlsx workbook of at most 3 MB (larger files: use the terminal import)")
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                raise StoreError("BAD_UPLOAD", "that file could not be read") from None
            if name.lower().endswith(".csv"):
                raise StoreError("BAD_UPLOAD", "the panel previews .xlsx workbooks; CSV files are imported from the terminal")
            pv = SpreadsheetImporter(s).analyze(data, name=name)
            try:
                view = pv.to_dict("LOW")
            finally:
                pv.close()   # the raw plan (and the log scrubber's copy of protected values) exists only for this call
            s.audit("import_previewed", actor="panel", outcome="preview", detail=name)
            return {"preview": view, "cli": IMPORT_CLI}
        # ---- clients (read and decide only: creating or editing a client is done in the terminal)
        if cmd == "biz_clients":
            out = []
            for c in s.list_clients():
                n = s.counts(c["client_id"])
                out.append({**c, "facts": n["live_facts"], "documents": len(self.docs.list(scope=c["client_id"])), **self.research.summary(c["client_id"])})
            return {"clients": out}
        if cmd in ("biz_client", "biz_client_docs", "biz_client_sources", "biz_client_seo", "biz_client_audit"):
            cid = str(m.get("client_id", ""))
            client = s.get_client(cid)
            if cmd == "biz_client":
                return {"client": client, "fields": [f.to_dict() for f in s.list_fields(include_missing=True, scope=cid)]}
            if cmd == "biz_client_docs":
                return {"client": client, "documents": self.docs.list(scope=cid)}
            if cmd == "biz_client_sources":
                return {"client": client, "enrolled": self.docs.enrolled(cid), "cli": f"python -m octg_agent business docs enroll <folder> --client {cid}"}
            if cmd == "biz_client_seo":
                groups = ("seo", "local_seo", "service_area", "services", "marketing")
                return {"client": client, "fields": [f.to_dict() for f in s.list_fields(include_missing=True, scope=cid) if f.group in groups], **self.research.summary(cid)}
            return {"client": client, "events": s.audit_tail(min(int(m.get("n", 40) or 40), 200), scope=cid)}
        raise StoreError("UNKNOWN_COMMAND", f"unknown Business Brain command {cmd!r}")
