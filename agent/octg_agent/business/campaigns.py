"""SEO campaigns, stored in the Knowledge Hub's own database (business.db) next to the client they belong to.

A campaign = one client + website + objective + language/country + keyword targets (each optionally with a location and a service) + a research status
and a baseline. Keywords are de-duplicated (case/space-insensitive, per location). Every keyword location is checked against what the Knowledge Hub
knows about the client's service areas: "in_service_area", "outside" (needs the owner's confirmation before it is worked on) or "unknown" (no area data).
Runs (SERP checks, audits, competitor reviews, reports) are recorded per campaign and point at their research session and collaboration request.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from .store import BusinessStore, StoreError
from ..sensitive import scrub_text

DDL = """
CREATE TABLE IF NOT EXISTS seo_campaigns (
  campaign_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT NOT NULL, website TEXT, objective TEXT, language TEXT, country TEXT,
  services TEXT, locations TEXT, status TEXT NOT NULL, research_status TEXT, baseline_date REAL, baseline_session_id TEXT,
  created_at REAL, updated_at REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS seo_campaign_keywords (
  kw_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, client_id TEXT NOT NULL, keyword TEXT NOT NULL, keyword_norm TEXT NOT NULL, location TEXT, location_norm TEXT NOT NULL DEFAULT '',
  service TEXT, area_status TEXT, state TEXT NOT NULL DEFAULT 'LIVE', source TEXT, added_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS seo_kw_live ON seo_campaign_keywords(campaign_id, keyword_norm, location_norm) WHERE state = 'LIVE';
CREATE TABLE IF NOT EXISTS seo_campaign_runs (
  run_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, client_id TEXT NOT NULL, kind TEXT NOT NULL, research_session_id TEXT, request_id TEXT, artifact_id TEXT,
  status TEXT, summary TEXT, started_at REAL, finished_at REAL
);
"""
STATUSES = ("draft", "researching", "baseline_set", "active", "paused", "closed")
RUN_KINDS = ("serp", "audit", "competitors", "report", "connectors", "verification")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s,&'-]", " ", (s or "").lower())).strip()


def place(s: str) -> str:
    return norm((s or "").split(",")[0]).replace("'", "")


class Campaigns:
    def __init__(self, store: BusinessStore):
        self.store = store
        with store.conn() as c:
            c.executescript(DDL)

    # ------------------------------------------------------------------ service areas known for the client (for validation)
    def service_areas(self, client_id: str) -> set[str]:
        areas: set[str] = set()
        with self.store.conn() as c:
            self.store._check_scope(c, client_id, writing=False)
            for r in c.execute("SELECT location, city FROM client_locations WHERE client_id=? AND state='LIVE'", (client_id,)):
                for v in (r["location"], r["city"]):
                    if v:
                        areas.add(place(v))
            for r in c.execute("SELECT field_key, value_plain FROM facts WHERE scope=? AND state='LIVE' AND value_plain IS NOT NULL AND (field_key LIKE 'service_area.%' "
                               "OR field_key LIKE 'local_seo.target_locations%' OR field_key LIKE 'seo.target_locations%' OR field_key='location.address')", (client_id,)):
                v = r["value_plain"]
                if r["field_key"] == "location.address":
                    try:
                        v = (json.loads(v) or {}).get("city", "")
                    except ValueError:
                        v = ""
                if v:
                    areas.add(place(v))
        return {a for a in areas if a}

    def _area_status(self, areas: set[str], location: str) -> str:
        if not location:
            return "n/a"
        if not areas:
            return "unknown"
        return "in_service_area" if place(location) in areas else "outside"

    # ------------------------------------------------------------------ campaigns
    def upsert(self, client_id: str, *, campaign_id: str | None = None, name: str = "", website: str = "", objective: str = "", language: str = "en", country: str = "us",
               services: list[str] | None = None, locations: list[str] | None = None, keywords: list | None = None, status: str | None = None, notes: str = "", source: str = "collab") -> dict:
        now = time.time()
        if status and status not in STATUSES:
            raise StoreError("BAD_STATUS", f"campaign status must be one of {STATUSES}")
        with self.store.conn() as c:
            self.store._check_scope(c, client_id, writing=True)
            if campaign_id:
                row = c.execute("SELECT * FROM seo_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
                if row is None or row["client_id"] != client_id:
                    raise StoreError("NO_SUCH_CAMPAIGN", "no such campaign for this client")
                sets = {"updated_at": now}
                for k, v in (("name", name), ("website", website), ("objective", objective), ("language", language), ("country", country), ("notes", notes)):
                    if v:
                        sets[k] = scrub_text(v)[:1000]
                if services is not None:
                    sets["services"] = json.dumps(sorted({s.strip() for s in services if s.strip()}, key=str.lower))
                if locations is not None:
                    sets["locations"] = json.dumps(sorted({s.strip() for s in locations if s.strip()}, key=str.lower))
                if status:
                    sets["status"] = status
                c.execute(f"UPDATE seo_campaigns SET {','.join(f'{k}=?' for k in sets)} WHERE campaign_id=?", (*sets.values(), campaign_id))
            else:
                campaign_id = "cmp_" + uuid.uuid4().hex[:10]
                c.execute("INSERT INTO seo_campaigns (campaign_id,client_id,name,website,objective,language,country,services,locations,status,research_status,created_at,updated_at,notes) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (campaign_id, client_id, scrub_text(name or "SEO campaign")[:200], website[:300], scrub_text(objective)[:1000], language[:10], country[:10],
                                                                 json.dumps(sorted({s.strip() for s in (services or []) if s.strip()}, key=str.lower)),
                                                                 json.dumps(sorted({s.strip() for s in (locations or []) if s.strip()}, key=str.lower)), status or "draft", "not_started", now, now, scrub_text(notes)[:2000]))
        self.store.audit("seo_campaign_saved", scope=client_id, actor=source, outcome="ok", detail=campaign_id)
        added = self.add_keywords(client_id, campaign_id, keywords or [], source=source) if keywords else {"added": [], "duplicates": [], "warnings": []}
        return {"campaign": self.get(client_id, campaign_id), **added}

    def add_keywords(self, client_id: str, campaign_id: str, keywords: list, *, source: str = "collab") -> dict:
        """Bulk entry: strings ('keyword' or 'keyword | location | service') or objects {keyword, location?, service?}. Duplicates are reported, not inserted."""
        areas = self.service_areas(client_id)
        added, dups, warns = [], [], []
        now = time.time()
        with self.store.conn() as c:
            self.store._check_scope(c, client_id, writing=True)
            camp = c.execute("SELECT client_id FROM seo_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if camp is None or camp["client_id"] != client_id:
                raise StoreError("NO_SUCH_CAMPAIGN", "no such campaign for this client")
            for k in keywords:
                if isinstance(k, str):
                    parts = [x.strip() for x in k.split("|")]
                    k = {"keyword": parts[0], "location": parts[1] if len(parts) > 1 else "", "service": parts[2] if len(parts) > 2 else ""}
                if not isinstance(k, dict) or not str(k.get("keyword", "")).strip():
                    continue
                kw, loc, svc = " ".join(str(k["keyword"]).split())[:200], " ".join(str(k.get("location") or "").split())[:120], " ".join(str(k.get("service") or "").split())[:120]
                kn, ln = norm(kw), norm(loc)
                if c.execute("SELECT 1 FROM seo_campaign_keywords WHERE campaign_id=? AND keyword_norm=? AND location_norm=? AND state='LIVE'", (campaign_id, kn, ln)).fetchone():
                    dups.append(kw + (f" | {loc}" if loc else ""))
                    continue
                st = self._area_status(areas, loc)
                if st == "outside":
                    warns.append(f"'{loc}' (for '{kw}') is not among the client's known service areas: confirm with the owner before working on it")
                c.execute("INSERT INTO seo_campaign_keywords (kw_id,campaign_id,client_id,keyword,keyword_norm,location,location_norm,service,area_status,state,source,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          ("kw_" + uuid.uuid4().hex[:10], campaign_id, client_id, scrub_text(kw), kn, scrub_text(loc), ln, scrub_text(svc), st, "LIVE", source, now))
                added.append(kw + (f" | {loc}" if loc else ""))
            c.execute("UPDATE seo_campaigns SET updated_at=? WHERE campaign_id=?", (now, campaign_id))
        if added:
            self.store.audit("seo_keywords_added", scope=client_id, actor=source, outcome=str(len(added)), detail=campaign_id)
        if not areas and any(isinstance(k, dict) and k.get("location") or isinstance(k, str) and "|" in k for k in keywords):
            warns.append("the Knowledge Hub has no service areas for this client yet, so keyword locations could not be checked")
        return {"added": added, "duplicates": dups, "warnings": warns}

    def remove_keyword(self, client_id: str, kw_id: str) -> None:
        with self.store.conn() as c:
            n = c.execute("UPDATE seo_campaign_keywords SET state='REMOVED' WHERE kw_id=? AND client_id=? AND state='LIVE'", (kw_id, client_id)).rowcount
        if not n:
            raise StoreError("NOT_FOUND", "no such keyword")

    def get(self, client_id: str, campaign_id: str) -> dict:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM seo_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if r is None or r["client_id"] != client_id:
                raise StoreError("NO_SUCH_CAMPAIGN", "no such campaign for this client")
            kws = [dict(k) for k in c.execute("SELECT kw_id,keyword,location,service,area_status,source,added_at FROM seo_campaign_keywords WHERE campaign_id=? AND state='LIVE' ORDER BY added_at, keyword", (campaign_id,))]
            runs = [dict(x) for x in c.execute("SELECT * FROM seo_campaign_runs WHERE campaign_id=? ORDER BY started_at DESC LIMIT 50", (campaign_id,))]
        d = dict(r)
        d["services"] = json.loads(d["services"] or "[]")
        d["locations"] = json.loads(d["locations"] or "[]")
        d["keywords"] = kws
        d["runs"] = runs
        return d

    def list(self, client_id: str | None = None) -> list[dict]:
        with self.store.conn() as c:
            q = "SELECT campaign_id, client_id FROM seo_campaigns" + (" WHERE client_id=?" if client_id else "") + " ORDER BY created_at DESC"
            rows = c.execute(q, (client_id,) if client_id else ()).fetchall()
        return [self.get(r["client_id"], r["campaign_id"]) for r in rows]

    def find_for_client(self, client_id: str, campaign_id: str | None) -> dict:
        if campaign_id:
            return self.get(client_id, campaign_id)
        cs = self.list(client_id)
        if not cs:
            raise StoreError("NO_CAMPAIGN", "this client has no SEO campaign yet: create one with seo.campaign_upsert")
        if len(cs) > 1:
            raise StoreError("AMBIGUOUS_CAMPAIGN", "this client has several campaigns: pass campaign_id")
        return cs[0]

    # ------------------------------------------------------------------ runs and research status
    def start_run(self, client_id: str, campaign_id: str, kind: str, *, research_session_id: str = "", request_id: str = "") -> str:
        if kind not in RUN_KINDS:
            raise StoreError("BAD_KIND", f"run kind must be one of {RUN_KINDS}")
        rid = "run_" + uuid.uuid4().hex[:10]
        with self.store.conn() as c:
            c.execute("INSERT INTO seo_campaign_runs (run_id,campaign_id,client_id,kind,research_session_id,request_id,status,started_at) VALUES (?,?,?,?,?,?,?,?)",
                      (rid, campaign_id, client_id, kind, research_session_id, request_id, "running", time.time()))
            c.execute("UPDATE seo_campaigns SET research_status='in_progress', status=CASE WHEN status='draft' THEN 'researching' ELSE status END, updated_at=? WHERE campaign_id=?",
                      (time.time(), campaign_id))
        return rid

    def finish_run(self, run_id: str, *, status: str = "finished", summary: str = "", artifact_id: str = "") -> None:
        with self.store.conn() as c:
            r = c.execute("SELECT campaign_id FROM seo_campaign_runs WHERE run_id=?", (run_id,)).fetchone()
            c.execute("UPDATE seo_campaign_runs SET status=?, summary=?, artifact_id=?, finished_at=? WHERE run_id=?", (status, scrub_text(summary)[:2000], artifact_id, time.time(), run_id))
            if r:
                c.execute("UPDATE seo_campaigns SET research_status=?, updated_at=? WHERE campaign_id=?", ("collected" if status == "finished" else status, time.time(), r["campaign_id"]))

    def set_baseline(self, client_id: str, campaign_id: str, session_id: str) -> None:
        with self.store.conn() as c:
            n = c.execute("UPDATE seo_campaigns SET baseline_date=?, baseline_session_id=?, status=CASE WHEN status IN ('draft','researching') THEN 'baseline_set' ELSE status END, updated_at=? "
                          "WHERE campaign_id=? AND client_id=? AND baseline_date IS NULL", (time.time(), session_id, time.time(), campaign_id, client_id)).rowcount
        if n:
            self.store.audit("seo_baseline_set", scope=client_id, actor="collab", outcome="ok", detail=f"{campaign_id} {session_id}")
