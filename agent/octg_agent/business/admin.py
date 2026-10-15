"""The management service behind the OCTG Knowledge Hub: everything a person does to the Business Brain / Client Brain through a screen, as plain Python calls.

There is ONE security implementation and it is the existing one: every value goes through `BusinessStore.prepare` (validation, secrets refused, sensitivity, hashing), is written by the same primitives the
spreadsheet importer and the CLI use (encrypted if HIGH, audited without the value, scope-checked), and a change to something already stored needs an explicit confirmation that THIS layer enforces (the web page
only asks for it). This module knows nothing about HTTP, cookies or JavaScript, and a model has no path to it.

Nothing here is deleted for good: removing a value rejects it, removing a row retires it, removing a client archives it; the history stays.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import hubconfig
from . import records as R
from .hubconfig import HubConfig
from .schema import CATALOGS, CONSTITUTIONS, ME, KeyError_, parse_key
from .service import BusinessBrain, REVEAL_SECONDS
from .spreadsheet import SpreadsheetImporter
from .spreadsheet import layout as L
from .spreadsheet.xlsx import SheetSpec, write_xlsx
from .store import BusinessStore, StoreError

SRC = "Knowledge Hub"
OWNER_NAME = "One Chance to Grow"

# ---- which catalog fields each screen shows (template keys; "[]" = a repeatable list)
ME_SECTIONS: list[tuple[str, str, list[str]]] = [
    ("identity", "Identity", ["identity.legal_name", "identity.trade_name", "identity.constitution", "registration.incorporation_date"]),
    ("contact", "Contact", ["contact.email", "contact.support_email", "contact.phone", "contact.website"]),
    ("address", "Address", ["addresses.principal", "addresses.additional[]"]),
    ("services", "Services", ["services.primary[]", "services.additional[]", "services.description", "services.hsn_sac[]"]),
    ("description", "Description", ["identity.description"]),
    ("registration", "Registration", ["registration.udyam", "registration.cin", "registration.llpin", "registration.registration_number"]),
    ("tax", "Tax / Business IDs", ["taxation.pan", "taxation.gstin", "taxation.tan"]),
    ("people", "Owners and partners", ["owners[].name", "owners[].role", "owners[].email", "owners[].phone", "owners[].pan", "owners[].dob"]),
    ("social", "Social profiles", ["social.profile[]"]),
]
CLIENT_SECTIONS: dict[str, tuple[str, list[str]]] = {
    "business": ("Business information", ["identity.business_name", "identity.legal_name", "identity.trade_name", "identity.description", "account.status", "account.onboarding_date", "account.plan",
                                          "registration.cin", "registration.udyam", "taxation.pan", "taxation.gstin", "taxation.tan"]),
    "contact": ("Contact", ["contact.website", "contact.email", "contact.phone", "contact.person"]),
    "locations": ("Locations", ["location.address", "service_area.cities[]", "service_area.towns[]", "service_area.counties[]", "service_area.regions[]", "service_area.other[]"]),
    "services": ("Services", ["services.primary[]", "services.secondary[]", "services.categories[]"]),
    "seo": ("SEO targets", ["seo.target_keywords[]", "seo.target_services[]", "seo.target_locations[]", "seo.goals[]", "seo.current_status", "local_seo.primary_category",
                            "local_seo.secondary_categories[]", "local_seo.gbp_reference", "local_seo.target_locations[]"]),
    "notes": ("Notes", ["account.internal_notes", "marketing.content_notes", "account.authorized_users[]", "marketing.social_profiles[]", "marketing.directories[]"]),
}
ME_CORE = ["identity.legal_name", "contact.email", "contact.phone", "contact.website", "addresses.principal", "identity.description", "services.primary[]"]
CLIENT_CORE = ["identity.business_name", "contact.website", "contact.phone", "location.address"]

EVENT_TEXT = {
    "fact_added": "Value saved", "fact_superseded": "Value replaced", "fact_confirmed": "Value confirmed", "fact_rejected": "Value removed / rejected", "conflict_created": "Conflict detected",
    "conflict_resolved": "Conflict resolved", "client_created": "Client created", "client_renamed": "Client renamed", "client_archived": "Client archived", "client_restored": "Client restored",
    "client_linked": "Client linked to its workbook id", "import_committed": "Import completed", "import_failed": "Import failed (nothing changed)", "import_rejected": "Import rejected",
    "import_verified": "Import marked verified", "import_previewed": "Import previewed", "import_kept_existing": "Existing value kept", "record_added": "Row added", "record_replaced": "Row changed",
    "record_retired": "Row removed", "reveal": "Value revealed", "export_created": "Data exported", "backup_created": "Backup made", "settings_changed": "Setting changed", "hub_login": "Signed in to the Hub",
    "hub_logout": "Signed out of the Hub", "scope_default": "Default client for tasks changed", "document_approved": "Document approved", "document_excluded": "Document excluded",
    "rejected_secret": "A credential was refused", "corroborated": "Same value found again", "skipped_known": "Previously rejected value not re-added",
}
CHANGE_EVENTS = {"fact_added", "fact_superseded", "fact_confirmed", "fact_rejected", "conflict_resolved", "client_created", "client_renamed", "client_archived", "client_restored", "client_linked",
                 "import_committed", "import_failed", "import_rejected", "import_verified", "record_added", "record_replaced", "record_retired", "export_created", "backup_created", "settings_changed",
                 "scope_default", "document_approved", "document_excluded"}
SOURCE_TEXT = {"USER_ENTERED": "Entered by you", "USER_CONFIRMED": "Confirmed by you", "DOCUMENT": "From a document", "IMPORT": "Imported"}


class AdminError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").lower() + "%"


class Admin:
    def __init__(self, brain: BusinessBrain, settings=None):
        self.brain = brain
        self.store: BusinessStore = brain.store
        self.docs = brain.docs
        self.research = brain.research
        self.importer = SpreadsheetImporter(self.store)
        self.hubcfg = HubConfig(brain.data_dir)
        self.settings = settings            # the agent's Settings (host / port / data dir), for the Settings and Security pages

    # ================================================================== scopes
    def owner_name(self) -> str:
        for key in ("identity.trade_name", "identity.legal_name"):
            try:
                return self.store.get_value(key, ME).value
            except Exception:  # noqa: BLE001 - not set / unverified: the generic name
                continue
        return OWNER_NAME

    def scope_name(self, scope: str) -> str:
        if scope == ME:
            return self.owner_name()
        if scope == "new":
            return "A new client (created from this file)"
        try:
            return self.store.get_client(scope)["name"]
        except StoreError:
            return scope

    def scopes(self) -> dict:
        clients = self.store.list_clients(include_archived=True)
        return {"owner": {"id": ME, "name": self.owner_name()}, "clients": [{"id": c["client_id"], "name": c["name"], "ref": c["ref"], "archived": c["archived"]} for c in clients],
                "default_scope": self.hubcfg.active_scope()}

    def _scope(self, scope: str, *, writing: bool = False) -> str:
        scope = scope or ME
        if scope != ME:
            c = self.store.get_client(scope)
            if writing and c["archived"]:
                raise AdminError("CLIENT_ARCHIVED", "that client is archived: restore it before changing anything")
        return scope

    # ================================================================== fields (view)
    @staticmethod
    def _catalog(scope: str):
        return CATALOGS["me" if scope == ME else "client"]

    def _entry_from(self, pk, fs, plain: dict[str, str]) -> dict:
        live = [f for f in fs.facts if f.state == "LIVE"]
        cands = []
        for f in live:
            v = plain.get(f.fact_id)
            if v is not None and pk.field.compound:
                try:
                    v = json.loads(v)
                except ValueError:
                    v = {}
            cands.append({"fact_id": f.fact_id, "mask": f.mask, "value": v, "verification": f.verification, "source": SOURCE_TEXT.get(f.source_type, f.source_type), "source_ref": f.source_ref,
                          "doc_id": f.doc_id, "format_ok": f.format_ok, "format_note": f.format_note, "updated_at": f.updated_at})
        single = cands[0] if len(cands) == 1 else None
        return {"instance": pk.instance, "display_key": fs.key, "status": fs.status, "sensitivity": fs.sensitivity, "candidates": cands,
                "fact_id": single["fact_id"] if single else None, "value": single["value"] if single else None, "mask": single["mask"] if single else "",
                "verification": single["verification"] if single else None, "source": single["source"] if single else None, "source_ref": single["source_ref"] if single else ""}

    def fields(self, scope: str, keys: list[str] | None = None) -> list[dict]:
        scope = self._scope(scope)
        kind = self.store.kind_of(scope)
        catalog, by_key = self._catalog(scope)
        plain = self.store.plain_values(scope)
        grouped: dict[str, list[tuple]] = {}
        for fs in self.store.list_fields(include_missing=True, scope=scope):
            try:
                pk = parse_key(fs.key, kind)
            except KeyError_:
                continue
            grouped.setdefault(pk.field.key, []).append((pk, fs))
        out = []
        for key in keys or [f.key for f in catalog]:
            f = by_key.get(key)
            if f is None:
                continue
            rows = grouped.get(key, [])
            entries = [self._entry_from(pk, fs, plain) for pk, fs in rows if not f.many or fs.status != "MISSING"]
            if not f.many and not entries:
                entries = [{"instance": None, "display_key": f.key, "status": "MISSING", "sensitivity": f.sensitivity, "candidates": [], "fact_id": None, "value": None, "mask": "",
                            "verification": None, "source": None, "source_ref": ""}]
            out.append({"key": f.key, "label": f.label, "group": f.group, "sensitivity": f.sensitivity, "kind": f.kind, "many": f.many, "compound": f.compound,
                        "parts": [{"name": p.name, "label": p.label, "kind": p.kind} for p in f.parts], "enum": list(CONSTITUTIONS) if f.key == "identity.constitution" else None,
                        "entries": sorted(entries, key=lambda e: (e["instance"] if e["instance"] is not None else -1)),
                        "status": "MISSING" if not any(e["status"] != "MISSING" for e in entries) else ("CONFLICTING" if any(e["status"] == "CONFLICTING" for e in entries) else
                                                                                                         "UNVERIFIED" if any(e["status"] == "UNVERIFIED" for e in entries) else "VERIFIED")})
        return out

    def me_profile(self) -> dict:
        wanted = [k for _, _, ks in ME_SECTIONS for k in ks]
        fields = {f["key"]: f for f in self.fields(ME, wanted)}
        sections = [{"id": sid, "title": title, "fields": [fields[k] for k in ks if k in fields]} for sid, title, ks in ME_SECTIONS]
        return {"scope": ME, "name": self.owner_name(), "sections": sections, "completion": self._completion(ME), "sources": self.sources_of_scope(ME)}

    def client_profile(self, client_id: str, section: str) -> dict:
        client_id = self._scope(client_id)
        if client_id == ME or section not in CLIENT_SECTIONS:
            raise AdminError("BAD_SECTION", "unknown client section")
        title, keys = CLIENT_SECTIONS[section]
        return {"scope": client_id, "section": section, "title": title, "fields": self.fields(client_id, keys)}

    # ================================================================== fields (change)
    def _free_instance(self, scope: str, pk, claimed: set[int] | None = None) -> int:
        with self.store.conn() as c:
            used = {r["instance"] for r in c.execute("SELECT instance FROM facts WHERE scope=? AND field_key=? AND state='LIVE'", (scope, pk.field.key)).fetchall()} | (claimed or set())
        i = 0
        while i in used:
            i += 1
        return i

    def save_fields(self, scope: str, items: list[dict], *, confirm_replace: bool = False, allow_format: bool = False) -> dict:
        """Validate every item; if anything is invalid nothing is written. If something already stored would be REPLACED (or a value does not look right) the call returns what needs confirming and writes
        nothing until it is repeated with the confirmation. Then everything is written in ONE transaction. Typed by the user -> VERIFIED."""
        scope = self._scope(scope, writing=True)
        kind = self.store.kind_of(scope)
        problems, needs, plan, unchanged = [], [], [], []
        seen: set[str] = set()
        claimed: dict[str, set[int]] = {}
        for it in items or []:
            key, value = str(it.get("key", "")), it.get("value")
            try:
                pk0 = parse_key(key, kind)
                if pk0.field.many and not re.search(r"\[\d+\]", key):
                    n = self._free_instance(scope, pk0, claimed.setdefault(pk0.field.key, set()))
                    claimed[pk0.field.key].add(n)
                    key = pk0.field.key.replace("[]", f"[{n}]")
                prep = self.store.prepare(key, value, kind=kind)
            except KeyError_ as e:
                problems.append({"key": key, "code": "UNKNOWN_FIELD", "message": str(e)})
                continue
            except StoreError as e:
                problems.append({"key": key, "code": e.code, "message": e.message})
                continue
            if prep.pk.display() in seen:
                problems.append({"key": key, "code": "DUPLICATE", "message": "the same field appears twice in this save"})
                continue
            seen.add(prep.pk.display())
            with self.store.conn() as c:
                rows = self.store.fact_rows(c, scope, prep.pk)
            live = [r for r in rows if r["state"] == "LIVE"]
            if any(r["value_hash"] == prep.vhash for r in live) and len(live) == 1:
                unchanged.append({"key": key, "label": prep.pk.label})
                continue
            if live and not confirm_replace:
                needs.append({"key": key, "label": prep.pk.label, "reason": "replace", "sensitivity": prep.sens, "existing": live[0]["display_mask"] if len(live) == 1 else f"{len(live)} different values",
                              "new": prep.mask, "message": "This replaces the value you have now (it is kept as history)."})
            if not prep.ok and not allow_format:
                needs.append({"key": key, "label": prep.pk.label, "reason": "format", "sensitivity": prep.sens, "message": prep.note + ". Save it anyway?", "new": prep.mask})
            plan.append((prep, [r["fact_id"] for r in live]))
        if problems:
            return {"ok": False, "problems": problems}
        if needs:
            return {"ok": False, "needs_confirmation": needs}
        saved = []
        if plan:
            with self.store.conn() as c:
                c.execute("BEGIN IMMEDIATE")
                for prep, live_ids in plan:
                    fid = self.store.insert_fact(c, prep, scope=scope, source_type="USER_ENTERED", source_ref=SRC, verification="VERIFIED", supersede=live_ids, actor="hub")
                    saved.append({"key": prep.pk.display(), "label": prep.pk.label, "fact_id": fid, "mask": prep.mask, "replaced": bool(live_ids)})
        return {"ok": True, "saved": saved, "unchanged": unchanged}

    def remove_value(self, scope: str, key: str) -> dict:
        """Remove a value from use (every current candidate of that field/entry is REJECTED and stays as history)."""
        scope = self._scope(scope, writing=True)
        pk = parse_key(key, self.store.kind_of(scope))
        st = self.store.status(pk.display(), scope)
        ids = [f.fact_id for f in st.facts if f.state == "LIVE"]
        if not ids:
            raise AdminError("NOTHING_TO_REMOVE", "there is no value to remove")
        for fid in ids:
            self.store.reject(fid, actor="hub")
        return {"ok": True, "removed": len(ids)}

    def confirm(self, fact_id: str) -> dict:
        return {"field": self.store.confirm(fact_id, actor="hub").to_dict()}

    def reject(self, fact_id: str) -> dict:
        return {"field": self.store.reject(fact_id, actor="hub").to_dict()}

    def resolve(self, scope: str, key: str, fact_id: str) -> dict:
        scope = self._scope(scope, writing=True)
        return {"field": self.store.resolve(key, fact_id, actor="hub", scope=scope).to_dict()}

    def reveal(self, fact_id: str) -> dict:
        """A protected value, shown to the person who asked for it (audited without the value). The page hides it again after a few seconds."""
        return {"fact_id": fact_id, "value": self.store.reveal(fact_id, actor="hub"), "hide_after_s": REVEAL_SECONDS}

    # ================================================================== clients
    def _client_name_from(self, d: dict) -> str:
        return " ".join(str(d.get("business_name") or d.get("trade_name") or d.get("legal_name") or "").split())

    def create_client(self, data: dict, *, allow_format: bool = False) -> dict:
        """One transaction: the client, every value the user typed (VERIFIED), and its services / service areas / SEO targets. Nothing is required except a name."""
        name = self._client_name_from(data)
        if not name:
            raise AdminError("NAME_MISSING", "give the client a name (business name, trade name or legal name)")
        kind = "client"
        facts: list[tuple[str, Any]] = []
        mapping = [("business_name", "identity.business_name"), ("legal_name", "identity.legal_name"), ("trade_name", "identity.trade_name"), ("website", "contact.website"), ("email", "contact.email"),
                   ("phone", "contact.phone"), ("contact_person", "contact.person"), ("description", "identity.description"), ("status", "account.status"), ("plan", "account.plan"),
                   ("internal_notes", "account.internal_notes"), ("onboarding_date", "account.onboarding_date")]
        for field, key in mapping:
            v = data.get(field)
            if isinstance(v, str) and v.strip():
                facts.append((key, v.strip()))
        addr = {k: str(v).strip() for k, v in (data.get("address") or {}).items() if str(v or "").strip()}
        if addr:
            facts.append(("location.address", addr))
        problems, needs, preps = [], [], []
        for key, value in facts:
            try:
                prep = self.store.prepare(key, value, kind=kind)
            except StoreError as e:
                problems.append({"key": key, "code": e.code, "message": e.message})
                continue
            if not prep.ok and not allow_format:
                needs.append({"key": key, "label": prep.pk.label, "reason": "format", "message": prep.note + ". Save it anyway?", "new": prep.mask})
            preps.append(prep)
        recs: list[tuple[str, dict]] = []
        for s in (data.get("services") or []):
            try:
                if str(s).strip():
                    recs.append(("service", R.clean_record("service", {"service": s})))
            except R.RecordError as e:
                problems.append({"key": "services", "code": e.code, "message": e.message})
        for a in (data.get("service_areas") or []):
            try:
                if str(a).strip():
                    recs.append(("location", R.clean_record("location", {"location": a, "location_type": "service_area"})))
            except R.RecordError as e:
                problems.append({"key": "service_areas", "code": e.code, "message": e.message})
        for t in (data.get("seo_targets") or []):
            try:
                if isinstance(t, dict) and str(t.get("keyword", "")).strip():
                    recs.append(("seo", R.clean_record("seo", {k: t.get(k) for k in ("keyword", "location", "priority", "service", "notes") if t.get(k)})))
            except R.RecordError as e:
                problems.append({"key": "seo_targets", "code": e.code, "message": e.message})
        if problems:
            return {"ok": False, "problems": problems}
        if needs:
            return {"ok": False, "needs_confirmation": needs}
        now = self.store._clock()
        try:
            with self.store.conn() as c:
                c.execute("BEGIN IMMEDIATE")
                cid = self.store.insert_client(c, name, data.get("ref") or None, actor="hub")
                for prep in preps:
                    self.store.insert_fact(c, prep, scope=cid, source_type="USER_ENTERED", source_ref=SRC, verification="VERIFIED", actor="hub")
                seen: set[str] = set()
                for n, (k, rec) in enumerate(recs, 1):   # the order typed is the order kept
                    key = f"{k}|{R.rec_key(k, rec)}"
                    if key in seen:
                        continue
                    seen.add(key)
                    R.add_manual(c, k, cid, rec, now=now, order=n)
        except StoreError as e:
            raise AdminError(e.code, e.message) from None
        return {"ok": True, "client_id": cid, "name": name}

    def update_client(self, client_id: str, *, name: str | None = None, ref: str | None = None) -> dict:
        client_id = self._scope(client_id, writing=True)
        try:
            with self.store.conn() as c:
                self.store.update_client(c, client_id, name=name, ref=ref, actor="hub")
        except StoreError as e:
            raise AdminError(e.code, e.message) from None
        return {"client": self.store.get_client(client_id)}

    def set_archived(self, client_id: str, archived: bool) -> dict:
        self._scope(client_id)
        return {"client": self.store.set_client_archived(client_id, archived, actor="hub")}

    def clients_table(self, include_archived: bool = True) -> list[dict]:
        rows = []
        with self.store.conn() as c:
            changed = {r["scope"]: r["t"] for r in c.execute("SELECT scope, MAX(updated_at) t FROM facts GROUP BY scope")}
        for cl in self.store.list_clients(include_archived=include_archived):
            cid = cl["client_id"]
            fv = {f["key"]: f for f in self.fields(cid, ["contact.website", "location.address", "account.status", "identity.business_name", "contact.phone"])}
            addr = fv["location.address"]["entries"][0]
            loc = ", ".join(x for x in ((addr["value"] or {}).get("city"), (addr["value"] or {}).get("state")) if x) if isinstance(addr["value"], dict) else ""
            services = self.store.records("service", cid)
            seo = self.store.records("seo", cid)
            locs = self.store.records("location", cid)
            if not loc:
                prim = next((r for r in locs if r["location_type"] == "primary"), None)
                loc = (prim["location"] or prim["city"]) if prim else ""
            core = self._completion(cid)
            rows.append({"client_id": cid, "name": cl["name"], "ref": cl["ref"], "archived": cl["archived"], "website": fv["contact.website"]["entries"][0]["value"] or "",
                         "location": loc, "services": [r["service"] for r in services][:4], "services_count": len(services), "seo_targets": len(seo), "locations": len(locs),
                         "status": (fv["account.status"]["entries"][0]["value"] or ("Archived" if cl["archived"] else "")), "completion": core["percent"],
                         "seo_status": ("Ready" if seo else "No targets yet"), "updated_at": max(changed.get(cid) or 0, cl.get("updated_at") or 0, cl["created_at"] or 0), "pending": core["unverified"]})
        return rows

    def client_overview(self, client_id: str) -> dict:
        client_id = self._scope(client_id)
        if client_id == ME:
            raise AdminError("BAD_CLIENT", "that is your own business, not a client")
        cl = self.store.get_client(client_id)
        comp = self._completion(client_id)
        return {"client": cl, "completion": comp, "counts": {"services": len(self.store.records("service", client_id)), "locations": len(self.store.records("location", client_id)),
                                                            "seo_targets": len(self.store.records("seo", client_id)), "documents": len(self.docs.list(scope=client_id)),
                                                            "sources": len(self.sources_of_scope(client_id)), **self.research.summary(client_id)},
                "is_default": self.hubcfg.active_scope() == client_id}

    def _completion(self, scope: str) -> dict:
        core = ME_CORE if scope == ME else CLIENT_CORE
        fv = {f["key"]: f for f in self.fields(scope, core)}
        done = missing = unverified = conflicts = 0
        todo = []
        for k in core:
            f = fv.get(k)
            st = f["status"] if f else "MISSING"
            if st == "VERIFIED":
                done += 1
            else:
                todo.append({"key": k, "label": f["label"] if f else k, "status": st})
                missing += st == "MISSING"
                unverified += st == "UNVERIFIED"
                conflicts += st == "CONFLICTING"
        total = len(core)
        return {"percent": round(100 * done / total) if total else 0, "done": done, "total": total, "missing": missing, "unverified": unverified, "conflicts": conflicts, "todo": todo}

    # ================================================================== rows: services / locations / SEO targets
    def records(self, client_id: str, kind: str) -> list[dict]:
        client_id = self._scope(client_id)
        if client_id == ME:
            raise AdminError("BAD_CLIENT", "services, locations and SEO targets belong to a client")
        rows = self.store.records(kind, client_id)
        return [{**r, "source": self._row_source(r)} for r in rows]

    @staticmethod
    def _row_source(r: dict) -> str:
        if r.get("session_id"):
            return f"Imported: {r.get('workbook') or ''} / {r.get('sheet') or ''} row {r.get('row_num')}"
        return "Entered in the Knowledge Hub"

    def save_record(self, client_id: str, kind: str, rec: dict, rec_id: str | None = None) -> dict:
        client_id = self._scope(client_id, writing=True)
        if client_id == ME or kind not in R.KINDS:
            raise AdminError("BAD_RECORD", "unknown row type or scope")
        try:
            clean = R.clean_record(kind, rec)
        except R.RecordError as e:
            raise AdminError(e.code, e.message) from None
        now = self.store._clock()
        with self.store.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            key = R.rec_key(kind, clean)
            existing = R.find_live(c, kind, client_id, key)
            if rec_id:
                old = R.get_row(c, kind, rec_id)
                if old is None or old["client_id"] != client_id or old["state"] != "LIVE":
                    raise AdminError("NOT_FOUND", "that row does not exist (or is no longer current)")
                if existing is not None and existing["rec_id"] != rec_id:
                    raise AdminError("DUPLICATE", "another row already describes exactly this")
                rid = R.add_manual(c, kind, client_id, clean, now=now, supersede=rec_id)   # a blank box in the form means "cleared": the new row is exactly what was typed; the old row stays as history
                self.store.audit("record_replaced", scope=client_id, actor="hub", outcome="VERIFIED", detail=kind, c=c)
            else:
                if existing is not None:
                    raise AdminError("DUPLICATE", "that is already in the list: edit the existing row instead")
                rid = R.add_manual(c, kind, client_id, clean, now=now)
                self.store.audit("record_added", scope=client_id, actor="hub", outcome="VERIFIED", detail=kind, c=c)
        return {"ok": True, "rec_id": rid}

    def retire_record(self, client_id: str, kind: str, rec_id: str) -> dict:
        client_id = self._scope(client_id, writing=True)
        with self.store.conn() as c:
            if not R.retire(c, kind, rec_id, client_id, self.store._clock()):
                raise AdminError("NOT_FOUND", "that row does not exist (or is already removed)")
            self.store.audit("record_retired", scope=client_id, actor="hub", outcome="retired", detail=kind, c=c)
        return {"ok": True}

    # ================================================================== SEO workspace
    def seo(self, client_id: str | None = None) -> dict:
        clients = [c for c in self.store.list_clients() if client_id in (None, "", c["client_id"])]
        out = []
        for c in clients:
            cid = c["client_id"]
            fv = self.fields(cid, ["seo.target_keywords[]", "seo.target_services[]", "seo.target_locations[]", "seo.goals[]", "seo.current_status", "local_seo.target_locations[]"])
            sessions = self.research.sessions(cid, 20)
            obs = self.research.observations(cid, limit=30)
            try:
                from .campaigns import Campaigns
                camps = Campaigns(self.store).list(cid)
            except Exception:  # noqa: BLE001 - the campaign tables are optional for this view
                camps = []
            out.append({"client_id": cid, "name": c["name"], "targets": self.records(cid, "seo"), "fields": fv, "sessions": sessions,
                        "observations": [{**o, "text": self.research.describe(o)} for o in obs], "summary": self.research.summary(cid), "campaigns": camps})
        return {"clients": out, "engine": "Research runs through the collaboration layer (your AI collaborator asks this agent; see COLLAB.md): campaigns, search observations, audits and reports appear here. "
                                          "Every observation is dated and tied to its evidence; a position is what one browser was shown at that time, not a guarantee."}

    # ================================================================== dashboard, conflicts, pending
    def _all_scopes(self) -> list[tuple[str, str]]:
        return [(ME, self.owner_name())] + [(c["client_id"], c["name"]) for c in self.store.list_clients(include_archived=True)]

    def conflicts(self) -> list[dict]:
        out = []
        plain: dict[str, dict] = {}
        for scope, name in self._all_scopes():
            plain[scope] = self.store.plain_values(scope)
            for fs in self.store.list_fields(include_missing=False, scope=scope):
                if fs.status != "CONFLICTING":
                    continue
                pk = parse_key(fs.key, self.store.kind_of(scope))
                cands = []
                for f in fs.facts:
                    if f.state != "LIVE":
                        continue
                    v = plain[scope].get(f.fact_id)
                    if v is not None and pk.field.compound:
                        try:
                            v = ", ".join(str(json.loads(v).get(p.name)) for p in pk.field.parts if json.loads(v).get(p.name))
                        except ValueError:
                            pass
                    cands.append({"fact_id": f.fact_id, "mask": f.mask, "value": v, "verification": f.verification, "source": SOURCE_TEXT.get(f.source_type, f.source_type), "source_ref": f.source_ref})
                out.append({"scope": scope, "scope_name": name, "key": fs.key, "label": fs.label, "sensitivity": fs.sensitivity, "candidates": cands})
        return out

    def pending(self, limit: int = 100) -> list[dict]:
        out = []
        for scope, name in self._all_scopes():
            for fs in self.store.list_fields(include_missing=False, scope=scope):
                if fs.status == "UNVERIFIED":
                    live = [f for f in fs.facts if f.state == "LIVE"]
                    out.append({"scope": scope, "scope_name": name, "key": fs.key, "label": fs.label, "sensitivity": fs.sensitivity, "fact_id": live[0].fact_id if live else None,
                                "source": SOURCE_TEXT.get(live[0].source_type, live[0].source_type) if live else "", "mask": live[0].mask if live else ""})
                if len(out) >= limit:
                    return out
        return out

    def dashboard(self) -> dict:
        clients = self.store.list_clients(include_archived=True)
        pend = self.pending(200)
        confs = self.conflicts()
        sessions = self.importer.sessions(6)
        docs = self.docs.list()
        n = self.store.counts()
        return {
            "owner": {"name": self.owner_name(), "completion": self._completion(ME)},
            "counts": {"clients": sum(1 for c in clients if not c["archived"]), "archived_clients": sum(1 for c in clients if c["archived"]), "documents": len(docs),
                       "sources": len(self.sources()["items"]), "values": n["live_facts"], "protected_values": n["encrypted_facts"]},
            "pending": {"count": len(pend), "items": pend[:8]}, "conflicts": {"count": len(confs), "items": confs[:5]},
            "imports": sessions, "changes": self.activity(None, 12, changes_only=True)["events"], "security": self.security(brief=True),
        }

    # ================================================================== activity
    def activity(self, scope: str | None = None, n: int = 60, *, changes_only: bool = False, event: str | None = None) -> dict:
        n = max(1, min(int(n or 60), 300))
        rows = self.store.audit_tail(n * 4 if changes_only else n, scope=(None if scope in (None, "") else scope), event=event)
        names = {s: nm for s, nm in self._all_scopes()}
        out = []
        for r in reversed(rows):
            if changes_only and r["event"] not in CHANGE_EVENTS:
                continue
            label = self._label_of(r["field_key"])
            out.append({"seq": r["seq"], "ts": r["ts"], "event": r["event"], "text": EVENT_TEXT.get(r["event"], r["event"].replace("_", " ").capitalize()), "scope": r["scope"] or ME,
                        "scope_name": names.get(r["scope"] or ME, r["scope"] or OWNER_NAME), "field": label, "actor": r["actor"], "outcome": r["outcome"] or "", "detail": r["detail"] or ""})
            if len(out) >= n:
                break
        return {"events": out}

    @staticmethod
    def _label_of(field_key: str) -> str:
        if not field_key:
            return ""
        base = re.sub(r"\[\d+\]", "[]", field_key)
        f = CATALOGS["me"][1].get(base.split(".full")[0]) or CATALOGS["client"][1].get(base.split(".full")[0])
        return f.label if f else field_key

    # ================================================================== sources and imports
    def sources_of_scope(self, scope: str) -> list[dict]:
        return [s for s in self.sources()["items"] if s["scope"] == (scope if scope != ME else "me")]

    def sources(self) -> dict:
        """Where information came from: imported workbooks and Markdown files (with their hash and verification), values entered by hand, and authorized documents."""
        items = []
        names = {s: nm for s, nm in self._all_scopes()}
        for s in self.importer.sessions(500):
            if s["status"] not in ("committed",):
                continue
            sc = "me" if s["kind"] == "markdown" and s["scope"] in ("", "me") else (s["scope"] or "multiple")
            items.append({"source_id": s["session_id"], "kind": s["kind"], "name": s["workbook"], "scope": sc, "scope_name": "Several (see the import)" if sc == "multiple" else names.get(ME if sc == "me" else sc, sc),
                          "sha256": s["sha256"], "imported_at": s["created_at"], "last_verified": s["last_verified"], "verification": s["verification"], "state": "Imported"})
        with self.store.conn() as c:
            manual = c.execute("SELECT scope, COUNT(*) n, MAX(created_at) t FROM facts WHERE source_type IN ('USER_ENTERED','USER_CONFIRMED') AND state='LIVE' GROUP BY scope").fetchall()
            doc_n = c.execute("SELECT client_id, COUNT(*) n, MAX(updated_at) t FROM documents WHERE state<>'EXCLUDED' GROUP BY client_id").fetchall()
        for r in manual:
            sc = r["scope"]
            items.append({"source_id": "manual:" + sc, "kind": "manual", "name": f"Entered by you ({r['n']} value(s))", "scope": sc, "scope_name": names.get(sc, sc), "sha256": "", "imported_at": r["t"],
                          "last_verified": r["t"], "verification": "VERIFIED", "state": "Manual"})
        for r in doc_n:
            sc = r["client_id"] or "me"
            items.append({"source_id": "documents:" + sc, "kind": "document", "name": f"Authorized documents ({r['n']})", "scope": sc, "scope_name": names.get(sc, sc), "sha256": "", "imported_at": r["t"],
                          "last_verified": None, "verification": "UNVERIFIED", "state": "Documents"})
        items.sort(key=lambda x: x["imported_at"] or 0, reverse=True)
        from .spreadsheet.connectors import describe_connectors
        return {"items": items, "kinds": describe_connectors()}

    def imports_list(self) -> list[dict]:
        return self.importer.sessions(100)

    def verify_import(self, session_id: str) -> dict:
        return self.importer.verify_session(session_id, actor="hub")

    def import_detail(self, session_id: str) -> dict:
        d = self.importer.session(session_id)
        names = {s: nm for s, nm in self._all_scopes()}
        for it in d["items"]:
            it["scope_name"] = "" if it["scope"] in ("multiple", "unknown") else names.get(it["scope"] or ME, it["scope"] or OWNER_NAME)
        d["scope_name"] = names.get(d["scope"] or ME, "") if d["kind"] == "markdown" else ""
        return d

    # ================================================================== documents (guided; the existing authorization model is untouched)
    def documents(self, scope: str | None = None) -> dict:
        docs = self.docs.list(scope=(None if scope in (None, "") else scope))
        return {"documents": docs, "enrolled": self.docs.enrolled(None), "authorized": self.brain.folders.roots(),
                "steps": [{"n": 1, "title": "Authorize a folder", "command": "python -m octg_agent fs allow \"<folder>\"", "done": bool(self.brain.folders.roots()), "who": "terminal only: this decides what the agent may read"},
                          {"n": 2, "title": "Enroll it for the Business Brain (one owner per folder)", "command": "python -m octg_agent business docs enroll \"<folder>\" [--client \"<client>\"]", "done": bool(self.docs.enrolled(None)),
                           "who": "terminal only"},
                          {"n": 3, "title": "Scan it", "command": "", "done": bool(docs), "who": "button below"},
                          {"n": 4, "title": "Index it (read the text; candidates only)", "command": "", "done": any(d["state"] in ("INDEXED", "APPROVED") for d in docs), "who": "button below"},
                          {"n": 5, "title": "Approve the documents you want the agent to use", "command": "", "done": any(d["state"] == "APPROVED" for d in docs), "who": "button on each document"}]}

    def documents_scan(self) -> dict:
        return {"report": self.docs.scan().to_dict()}

    def documents_index(self, doc_id: str | None = None) -> dict:
        return {"report": self.docs.index(doc_id)}

    def document_approve(self, doc_id: str) -> dict:
        return {"document": self.docs.approve(doc_id, actor="hub")}

    def document_exclude(self, doc_id: str) -> dict:
        return {"document": self.docs.exclude(doc_id, actor="hub")}

    # ================================================================== search (no protected value is ever matched or shown)
    def search(self, q: str, limit: int = 40) -> dict:
        q = " ".join((q or "").split())
        if len(q) < 2:
            return {"results": [], "query": q}
        q = q[:80]
        pat = _like(q)
        names = {s: nm for s, nm in self._all_scopes()}
        res: list[dict] = []

        def add(kind, title, snippet, scope, link):
            if len(res) < limit:
                res.append({"type": kind, "title": title, "snippet": snippet, "scope": scope, "scope_name": names.get(scope, scope), "link": link})
        with self.store.conn() as c:
            for r in c.execute("SELECT client_id, name, ref FROM clients WHERE lower(name) LIKE ? ESCAPE '\\' OR lower(ifnull(ref,'')) LIKE ? ESCAPE '\\' LIMIT 20", (pat, pat)):
                add("client", r["name"], r["ref"] or "", r["client_id"], f"#/clients/{r['client_id']}")
            for r in c.execute("SELECT scope, field_key, instance, value_plain FROM facts WHERE state='LIVE' AND sensitivity<>'HIGH' AND value_plain IS NOT NULL AND lower(value_plain) LIKE ? ESCAPE '\\' LIMIT 40", (pat,)):
                f = self._label_of(r["field_key"])
                sc = r["scope"]
                snippet = r["value_plain"]
                if snippet.startswith("{"):
                    try:
                        snippet = ", ".join(str(v) for v in json.loads(snippet).values())
                    except ValueError:
                        pass
                add("value", f.strip() or r["field_key"], snippet[:140], sc, "#/my-business" if sc == ME else f"#/clients/{sc}")
            for kind, table, cols in (("service", "client_services", ("service", "category")), ("location", "client_locations", ("location", "city")), ("seo", "client_seo_targets", ("keyword", "location"))):
                cond = " OR ".join(f"lower(ifnull({c_},'')) LIKE ? ESCAPE '\\'" for c_ in cols)
                for r in c.execute(f"SELECT client_id, {','.join(cols)} FROM {table} WHERE state='LIVE' AND ({cond}) LIMIT 15", [pat] * len(cols)):
                    label = {"service": "Service", "location": "Location", "seo": "SEO target"}[kind]
                    add(kind, f"{label}: {r[cols[0]] or r[cols[1]] or ''}", " · ".join(str(r[x]) for x in cols[1:] if r[x]), r["client_id"], f"#/clients/{r['client_id']}/" + {"service": "services", "location": "locations", "seo": "seo"}[kind])
            for r in c.execute("SELECT session_id, workbook, kind, scope FROM import_sessions WHERE status='committed' AND lower(workbook) LIKE ? ESCAPE '\\' LIMIT 10", (pat,)):
                add("source", r["workbook"], f"{r['kind']} import", r["scope"] or ME, "#/imports/" + r["session_id"])
        for d in self.docs.list(search=q)[:10]:
            add("document", d["name"], d["state"], d["client_id"] or ME, "#/documents")
        return {"results": res, "query": q}

    # ================================================================== export (HIGH values are never exported)
    def _value_for(self, scope: str, col: L.Col, plain: dict[str, str], fields: dict[str, dict]) -> str:
        t = col.target
        if t.kind not in ("fact", "multi", "addr"):
            return ""
        f = fields.get(t.key)
        if f is None or f["sensitivity"] == "HIGH":
            return ""
        ents = [e for e in f["entries"] if e["status"] in ("VERIFIED", "UNVERIFIED") and e["value"] is not None]
        if not ents:
            return ""
        if t.kind == "multi":
            return "; ".join(str(e["value"]) for e in ents)
        v = ents[0]["value"]
        if t.kind == "addr":
            return str(v.get(t.part, "")) if isinstance(v, dict) else ""
        return str(v)

    def export(self, kind: str, scope: str | None = None) -> tuple[str, bytes]:
        """An .xlsx in the same layout the importer reads. Protected (HIGH) values are never written: their cells are blank, so re-importing the file can never change them. Audited."""
        day = time.strftime("%Y%m%d")
        sheets: list[SheetSpec] = []
        if kind == "business":
            fields = {f["key"]: f for f in self.fields(ME)}
            rows = [["Field", "Value", "Guidance"]]
            for col in L.ME.cols:
                f = fields.get(col.target.key)
                protected = bool(f and f["sensitivity"] == "HIGH")
                rows.append([col.name, self._value_for(ME, col, {}, fields), "protected value: not exported" if protected else ""])
            sheets.append(SheetSpec("My Business", rows, [28, 48, 50], "table", [], "FF1F4E79", True))
            name = f"OCTG_My_Business_{day}.xlsx"
        elif kind in ("clients", "seo"):
            targets = [c for c in self.store.list_clients(include_archived=False) if scope in (None, "", "all", c["client_id"])]
            if scope not in (None, "", "all") and not targets:
                raise AdminError("NO_SUCH_CLIENT", "no such (active) client")
            head = [c.name for c in L.CLIENTS.cols]
            crow, srv, loc, seo = [head], [[c.name for c in L.SERVICES.cols]], [[c.name for c in L.LOCATIONS.cols]], [[c.name for c in L.SEO.cols]]
            for cl in targets:
                cid = cl["client_id"]
                ref = cl["ref"] or cid
                fields = {f["key"]: f for f in self.fields(cid)}
                if kind == "clients":
                    row = [ref if col.target.kind == "ref" else self._value_for(cid, col, {}, fields) for col in L.CLIENTS.cols]
                    if not row[head.index("business_name")]:
                        row[head.index("business_name")] = cl["name"]
                    crow.append(row)
                    for r in self.store.records("service", cid):
                        srv.append([ref, r["service"] or "", r["category"] or "", r["priority"] or "", r["notes"] or ""])
                    for r in self.store.records("location", cid):
                        loc.append([ref, r["location"] or "", r["city"] or "", r["state_region"] or "", r["country"] or "", r["postal_code"] or "", r["location_type"] or "", r["priority"] or "", r["notes"] or ""])
                for r in self.store.records("seo", cid):
                    seo.append([ref, r["keyword"] or "", r["location"] or "", r["priority"] or "", r["service"] or "", r["notes"] or ""])
            if kind == "clients":
                sheets += [SheetSpec("Clients", crow), SheetSpec("Client Services", srv), SheetSpec("Client Locations", loc)]
            sheets.append(SheetSpec("Client SEO", seo))
            name = f"OCTG_{'Client_Data' if kind == 'clients' else 'SEO_Targets'}_{day}.xlsx"
        else:
            raise AdminError("BAD_EXPORT", "export 'business', 'clients' or 'seo'")
        data = write_xlsx(None, sheets)
        self.store.audit("export_created", actor="hub", outcome="exported", detail=f"{kind} {len(data)} bytes; protected values excluded", scope=(scope if scope not in (None, "", "all", ME) else ""))
        return name, data

    # ================================================================== backups, settings, security
    def backup(self) -> dict:
        p = self.store.backup(keep=30, tag="hub")
        self.store.audit("backup_created", actor="hub", outcome="made", detail=p.name)
        return {"name": p.name, "size": p.stat().st_size}

    def backups(self) -> list[dict]:
        base = self.store.path
        files = sorted(base.parent.glob(base.name + ".bak-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime, "kind": ("before an import" if "pre-import" in p.name else "before an upgrade" if "-pre-v" in p.name else "backup")}
                for p in files[:30]]

    def settings_view(self) -> dict:
        flags = hubconfig.read_flags(self.brain.data_dir)
        s = self.settings
        return {"business_in_tasks": flags["enabled"], "intent_detection": flags["intent_detection"], "default_scope": self.hubcfg.active_scope(),
                "agent": {"host": getattr(s, "host", "127.0.0.1"), "port": getattr(s, "port", None)}, "data_dir": str(self.brain.data_dir), "note_restart": "Changing 'use in tasks' takes effect when the agent restarts."}

    def save_settings(self, patch: dict) -> dict:
        changed = []
        flags = {}
        if isinstance(patch.get("business_in_tasks"), bool):
            flags["enabled"] = patch["business_in_tasks"]
        if isinstance(patch.get("intent_detection"), bool):
            flags["intent_detection"] = patch["intent_detection"]
        if flags:
            hubconfig.write_flags(self.brain.data_dir, **flags)
            changed += [{"enabled": "business_in_tasks"}.get(k, k) for k in flags]
        if "default_scope" in patch:
            v = patch["default_scope"] or None
            if v and v != ME:
                c = self.store.get_client(v)
                if c["archived"]:
                    raise AdminError("CLIENT_ARCHIVED", "an archived client cannot be the default")
            self.hubcfg.set_active_scope(v)
            self.store.audit("scope_default", actor="hub", outcome="set", scope=(v if v and v != ME else ""), detail=("my business" if v == ME else "client" if v else "none"))
            changed.append("default_scope")
        if flags:
            self.store.audit("settings_changed", actor="hub", outcome="set", detail=",".join(f"{k}={v}" for k, v in flags.items()))
        return {**self.settings_view(), "changed": changed}

    def security(self, brief: bool = False) -> dict:
        n = self.store.counts()
        s = self.settings
        last_backup = next(iter(self.backups()), None)
        d = {"protection": n["cipher"], "strong": n["cipher_strong"], "healthy": self.brain.healthy and self.store.integrity_ok(), "protected_values": n["encrypted_facts"],
             "local_only": getattr(s, "host", "127.0.0.1") in ("127.0.0.1", "localhost", "::1"), "last_backup": last_backup["modified"] if last_backup else None}
        if brief:
            return d
        d.update({"statements": [
            "Everything on this page runs on this computer: the Hub only answers requests from this computer.",
            "Protected values (tax / registration ids, internal notes) are encrypted with your Windows account and are never shown to an AI model.",
            "AI models only ever see which fields exist and whether a value is available. They never receive a value from here.",
            "Saving, importing and editing never call an AI model.",
            "The browser agent cannot open or operate this Hub."], "folders": {"authorized": len(self.brain.folders.roots()), "enrolled": len(self.docs.enrolled(None))}})
        return d
