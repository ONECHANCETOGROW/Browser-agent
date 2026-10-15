"""Loop kinds: what a loop measures and when its goal is met. The controller (loops.py) is the same for all of them.

seo_visibility  the first use case: where does the client's website appear in search for chosen keywords/locations, and what does the site audit say.
page_checks     a deliberately different, non-SEO kind (a live page must satisfy explicit checks): it exists to show the controller is general, and to be reused for deploy-verification loops.

Adding a kind = subclass LoopKind, call register_kind. Nothing else in the protocol, the store, the scheduler or the Hub changes.
External data sources are pluggable the same way: a capability named in PLUGGABLE_SOURCES that returns {"observations": [...]} in the standard form is requested automatically as soon as it
is registered; until then the loop reports the source as NOT connected. It never pretends data it does not have.
"""
from __future__ import annotations

import time

from ..business.campaigns import Campaigns, norm
from ..business.store import StoreError
from .capabilities import REGISTRY
from .loops import VOLATILE_RANGE, LoopKind, register_kind

# Data sources a loop uses when they are READY FOR THIS CLIENT (credentials present, a property mapped to the client by the owner): source -> the capability that delivers its observations.
# A capability that returns {"observations": [...]} in the standard form can be added here (or by a new kind) without touching the controller.
PLUGGABLE_SOURCES = {"google_search_console": "gsc.search_analytics", "bing_webmaster_tools": "bing.webmaster_stats"}
from ..connectors import SHORT as LABEL_OF   # display names come from connectors/providers.json
POSITION_TOLERANCE = 5.0   # first-party average position vs one browser's observation: beyond this the two are reported as disagreeing


def _standard(o: dict, **kw) -> dict:
    return {"key": o["key"], "label": o.get("label", o["key"]), "metric": o.get("metric", ""), "value": o.get("value"), "status": o["status"], "comparable": bool(o.get("comparable", o["status"] != "unavailable")),
            "cmp_key": o.get("cmp_key", ""), "observed_at": o.get("observed_at"), "source": o.get("source", ""), "evidence_id": o.get("evidence_id", ""), "request_id": o.get("request_id", ""),
            "limits": list(o.get("limits") or []), **kw}


def norm_key_tail(key: str) -> str:
    """'google|organic|drain cleaning|springfield, il' -> 'drain cleaning' (the keyword part of a SERP observation key)."""
    parts = key.split("|")
    return parts[2] if len(parts) > 2 else key


def streak_verdict(usable: list[dict], meets, confirmations: int, min_gap_s: float) -> tuple[str, int]:
    """Consecutive comparable observations that meet the target, at least `min_gap_s` apart (two checks minutes apart are one observation, not a confirmation).
    Unavailable observations are not in `usable`: they neither count nor reset. A comparable observation that misses resets the run."""
    streak, last = 0, None
    for o in usable:
        if meets(o):
            if streak == 0:
                streak, last = 1, o["observed_at"]
            elif o["observed_at"] - last >= min_gap_s:
                streak, last = streak + 1, o["observed_at"]
        else:
            streak, last = 0, None
    return ("met_confirmed" if streak >= confirmations else "met_unconfirmed" if streak >= 1 else "not_met"), streak


def is_volatile(recent: list[dict]) -> bool:
    """Unstable rather than improving: the measured positions of the last few observations swing by more than VOLATILE_RANGE, or the target flips between present and absent more than once.
    One move from absent to present (or back) is a change, not volatility."""
    if len(recent) < 3:
        return False
    found = [o["value"] for o in recent if o["status"] == "found" and o["value"] is not None]
    flips = sum(1 for a, b in zip(recent, recent[1:]) if (a["status"] == "found") != (b["status"] == "found"))
    return flips >= 2 or (len(found) >= 2 and max(found) - min(found) > VOLATILE_RANGE)


# ============================================================================================================================ SEO visibility
class SeoVisibility(LoopKind):
    name = "seo_visibility"
    title = "Search visibility"
    description = ("Track where ONE client's website appears in search for chosen keywords (organic position, map pack) plus the technical audit, round after round, until the target positions are reliably reached. "
                   "config: {campaign_id?, engine?, keywords?, location?, audit?, audit_each_round?, max_pages?}. goal: {targets:[{keyword, location?, search_type?: organic|local_pack, max_position?: 10}], "
                   "confirmations?: 2, require?: all|any, max_rounds?, deadline?, max_requests?}.")
    permissions = ("client:read", "research:write", "search:read", "net:fetch", "agent:read", "collab:write")

    @staticmethod
    def _key(engine: str, stype: str, keyword: str, location: str) -> str:
        return f"{engine}|{stype}|{norm(keyword)}|{norm(location) or '-'}"

    def check(self, svc, client_id: str, config: dict, goal: dict) -> list[str]:
        problems: list[str] = []
        try:
            camp = Campaigns(svc.admin().store).find_for_client(client_id, config.get("campaign_id"))
        except StoreError as e:
            return [e.message]
        config["campaign_id"] = camp["campaign_id"]
        config.setdefault("engine", "google")
        if config["engine"] not in ("google", "bing"):
            problems.append("engine must be google or bing")
        config.setdefault("country", camp.get("country") or "us")
        config.setdefault("language", camp.get("language") or "en")
        config.setdefault("location", "")
        config.setdefault("audit", True)
        kws = {norm(k["keyword"]): k for k in camp["keywords"]}
        config["_kw_locations"] = {k: (v.get("location") or "") for k, v in kws.items()}   # where each campaign keyword is searched: part of what makes observations comparable
        targets = goal.get("targets") or []
        if goal.get("baseline_only"):
            config.setdefault("competitors", True)
        if not isinstance(targets, list) or len(targets) > 30:
            problems.append("targets must be a list of at most 30 {keyword, location?, search_type?, max_position?}")
            return problems
        seen = set()
        for i, t in enumerate(targets):
            if not isinstance(t, dict) or not isinstance(t.get("keyword"), str) or norm(t["keyword"]) not in kws:
                problems.append(f"target {i + 1}: '{(t or {}).get('keyword') if isinstance(t, dict) else t}' is not a keyword of campaign {camp['campaign_id']} (add it with seo.campaign_upsert first)")
                continue
            stype = t.get("search_type", "organic")
            if stype not in ("organic", "local_pack"):
                problems.append(f"target {i + 1}: search_type is organic or local_pack")
            mp = t.get("max_position", 10)
            if not isinstance(mp, int) or isinstance(mp, bool) or not 1 <= mp <= 100:
                problems.append(f"target {i + 1}: max_position is a whole number from 1 to 100 (10 = first page)")
            if kws[norm(t["keyword"])].get("area_status") == "outside":
                problems.append(f"target {i + 1}: '{t['keyword']}' is outside the client's known service areas: the owner must confirm it first")
            k = self._key(config["engine"], stype, t["keyword"], t.get("location") or kws[norm(t["keyword"])].get("location") or "")
            if k in seen:
                problems.append(f"target {i + 1} duplicates another target")
            seen.add(k)
        if goal.get("confirmations") is not None and (not isinstance(goal["confirmations"], int) or not 1 <= goal["confirmations"] <= 10):
            problems.append("confirmations is a whole number from 1 to 10")
        return problems

    def targets(self, loop: dict) -> list[dict]:
        cfg, out = loop["config"], []
        locs = loop["config"].get("_kw_locations") or {}
        for t in loop["goal"].get("targets") or []:
            stype = t.get("search_type", "organic")
            loc = t.get("location") or locs.get(norm(t["keyword"])) or cfg.get("location") or ""
            out.append({"key": self._key(cfg.get("engine", "google"), stype, t["keyword"], loc), "label": f"{t['keyword']}" + (f" ({loc})" if loc else "") + (" — map pack" if stype == "local_pack" else ""),
                        "search_type": stype, "max_position": t.get("max_position", 10)})
        return out

    def _ready(self, loop: dict, source: str) -> bool:
        """Credentials present AND the owner mapped a property to THIS client. (Whether the credential still has access is checked by the capability itself, per request.)"""
        hub = getattr(self.svc, "connectors", None)
        if hub is None:
            return False
        grants = next((c["grants"] for c in self.svc.callers.list() if c["name"] == loop["caller"] and not c["disabled"]), [])
        if "connector:read" not in grants:     # the loop never has more rights than its creator: no first-party requests without the permission
            return False
        return hub.client(source).configured()["configured"] and bool(hub.db.mappings(client_id=loop["client_id"], source=source))

    def _kw_list(self, loop: dict) -> list[str]:
        cfg = loop["config"]
        return list(dict.fromkeys([t["keyword"] for t in loop["goal"].get("targets") or []] + list(cfg.get("keywords") or [])))[:30]

    def requests(self, loop: dict, purpose: str) -> list[dict]:
        cfg = loop["config"]
        kws = self._kw_list(loop)
        serp = {"campaign_id": cfg["campaign_id"], "engine": cfg.get("engine", "google")}
        if kws:
            serp["keywords"] = kws
            serp["max_keywords"] = len(kws)
        if cfg.get("location"):
            serp["location"] = cfg["location"]
        reqs = [{"capability": "connectors.status", "parameters": {}, "label": "data sources"},
                {"capability": "seo.serp_observe", "parameters": serp, "label": "search observations"}]
        if cfg.get("audit", True) and (purpose == "baseline" or cfg.get("audit_each_round")):
            reqs.append({"capability": "seo.audit_site", "parameters": {"campaign_id": cfg["campaign_id"], "max_pages": int(cfg.get("max_pages", 25)), "mode": "auto"}, "label": "site audit"})
        if self._ready(loop, "google_search_console") and "gsc.search_analytics" in REGISTRY:
            reqs.append({"capability": "gsc.search_analytics", "parameters": {"keywords": kws, "days": int(cfg.get("gsc_days", 28))} if kws else {"days": int(cfg.get("gsc_days", 28))}, "label": LABEL_OF["google_search_console"]})
        if self._ready(loop, "bing_webmaster_tools") and "bing.webmaster_stats" in REGISTRY:
            reqs.append({"capability": "bing.webmaster_stats", "parameters": {"keywords": kws, "days": int(cfg.get("bing_days", 28))} if kws else {"days": int(cfg.get("bing_days", 28))}, "label": "Bing Webmaster"})
        return reqs

    def followups(self, loop: dict, purpose: str, stage: int, responses: list[dict]) -> list[dict]:
        cfg = loop["config"]
        by_cap = {r["capability"]: r for r in responses}
        if stage == 1 and cfg.get("competitors") and (purpose == "baseline" or cfg.get("competitors_each_round")):
            serp = by_cap.get("seo.serp_observe")
            if serp and serp["status"] == "succeeded" and any(not (k.get("blocked") or k.get("error")) for k in serp["result"].get("keywords") or []):
                return [{"capability": "research.competitors", "parameters": {"depth": cfg.get("competitor_depth", "page"), "max_pages_per_site": int(cfg.get("competitor_pages", 6)), "mode": "auto"},
                         "label": "competitor research"}]
        last = "research.competitors" if "research.competitors" in by_cap else None
        if loop["goal"].get("baseline_only") and purpose == "baseline" and stage in (1, 2) and "seo.report" not in by_cap and (stage == 2 or last is None):
            cs = by_cap.get("connectors.status")
            return [{"capability": "seo.report", "parameters": {"connectors": cs["result"]} if cs and cs["status"] == "succeeded" else {}, "label": "SEO report"}]
        return []

    def observations(self, loop: dict, purpose: str, responses: list[dict]) -> list[dict]:
        cfg = loop["config"]
        engine = cfg.get("engine", "google")
        cmp_base = f"{engine}|{cfg.get('country', 'us')}|{cfg.get('language', 'en')}|{cfg.get('location') or '-'}"
        out: list[dict] = []
        wanted = [(t, self.targets(loop)[i]) for i, t in enumerate(loop["goal"].get("targets") or [])]
        by_cap = {r["capability"]: r for r in responses}
        serp = by_cap.get("seo.serp_observe")
        if serp and serp["status"] == "succeeded":
            for row in serp["result"].get("keywords") or []:
                loc = row.get("location") if row.get("location") not in (None, "(none)") else ""
                base = {"request_id": serp["request_id"], "source": f"browser:{engine}", "observed_at": row.get("observed_at") or serp.get("completed_at"), "evidence_id": row.get("evidence_id", ""),
                        "metric": "organic_position", "label": row["keyword"] + (f" ({loc})" if loc else "")}
                key = self._key(engine, "organic", row["keyword"], loc)
                if row.get("blocked") or row.get("error"):
                    why = "the search hit a CAPTCHA/consent wall and was not bypassed" if row.get("blocked") else f"the results page could not be read ({row.get('error')})"
                    out.append(_standard({**base, "key": key, "status": "unavailable", "limits": [why]}))
                    continue
                pos = row.get("client_position")
                lim = [] if pos else [f"not among the {len(row.get('top') or [])} organic results read (this is a measured absence, not missing data)"]
                out.append(_standard({**base, "key": key, "status": "found" if pos else "not_found", "value": pos, "comparable": True, "cmp_key": cmp_base + "|organic", "limits": lim}))
                lp = row.get("local_pack") or {}
                if any(t.get("search_type") == "local_pack" and norm(t["keyword"]) == norm(row["keyword"]) for t in loop["goal"].get("targets") or []) or lp.get("detected"):
                    present = bool(row.get("client_in_local_pack"))
                    out.append(_standard({**base, "key": self._key(engine, "local_pack", row["keyword"], loc), "metric": "in_map_pack", "label": base["label"] + " — map pack",
                                          "status": "found" if present else "not_found", "value": 1 if present else None, "comparable": True, "cmp_key": cmp_base + "|local_pack",
                                          "limits": [] if lp.get("detected") else ["no map pack was shown for this search"]}))
        # targets whose search could not be run at all are UNAVAILABLE for this round (never "not ranking")
        have = {o["key"] for o in out}
        why = ("the search request failed: " + ((serp or {}).get("error") or {}).get("code", "no response")) if (not serp or serp["status"] != "succeeded") else "this keyword was not part of the search results returned"
        for t, tt in wanted:
            if tt["key"] not in have:
                out.append(_standard({"key": tt["key"], "label": tt["label"], "metric": "organic_position" if tt["search_type"] == "organic" else "in_map_pack", "status": "unavailable",
                                      "observed_at": time.time(), "limits": [why], "source": f"browser:{engine}", "request_id": (serp or {}).get("request_id", "")}))
        audit = by_cap.get("seo.audit_site")
        if audit and audit["status"] == "succeeded":
            res, obs_at = audit["result"], audit.get("completed_at")
            fs = res.get("findings") or []
            for key, label, val in (("audit|findings_total", "Site audit: findings", len(fs)), ("audit|findings_high", "Site audit: high-severity findings", sum(1 for f in fs if f.get("severity") == "high"))):
                out.append(_standard({"key": key, "label": label, "metric": "count", "value": float(val), "status": "found", "comparable": True, "cmp_key": "audit|" + (res.get("mode") or "http"),
                                      "observed_at": obs_at, "source": "site_audit", "request_id": audit["request_id"], "evidence_id": (audit.get("evidence") or [{}])[0].get("evidence_id", ""),
                                      "limits": [] if res.get("measured") is None else [f"mode {res.get('mode')}"]}))
        for src, cap in PLUGGABLE_SOURCES.items():
            r = by_cap.get(cap)
            if not r:
                continue
            if r["status"] == "succeeded":
                out.extend(_standard({**o, "source": o.get("source") or src}) for o in (r["result"].get("observations") or []) if o.get("status") in ("found", "not_found", "unavailable"))
            else:   # a failed source is "unavailable" in the history (with its named error), never silently absent and never a negative result
                code = (r.get("error") or {}).get("code") or r["status"]
                tag = "gsc" if src == "google_search_console" else "bing"
                out.append(_standard({"key": f"{tag}|site|clicks", "label": LABEL_OF[src] + ": clicks", "metric": "clicks", "status": "unavailable", "observed_at": time.time(), "source": src,
                                      "request_id": r["request_id"], "limits": [f"{LABEL_OF[src]} could not be read: {code}"]}))
        return out

    def evaluate_target(self, target: dict, series: list[dict], confirmations: int, min_gap_s: float) -> dict:
        usable = [o for o in series if o["status"] in ("found", "not_found") and o["comparable"]]
        if not usable:
            return {"verdict": "insufficient", "reason": "no comparable observation of this target yet (every attempt so far was unavailable)", "observations": 0, "volatile": False}
        mp = target.get("max_position", 10)
        if target.get("search_type") == "local_pack":
            meets = lambda o: o["status"] == "found"
        else:
            meets = lambda o: o["status"] == "found" and o["value"] <= mp
        verdict, streak = streak_verdict(usable, meets, confirmations, min_gap_s)
        volatile = is_volatile(usable[-4:])
        cur = usable[-1]
        return {"verdict": verdict, "streak": streak, "needed": confirmations, "volatile": volatile, "observations": len(usable), "target": f"position {mp} or better" if target.get("search_type") != "local_pack" else "in the map pack",
                "current": {"status": cur["status"], "value": cur["value"], "observed_at": cur["observed_at"]}, "best": min((o["value"] for o in usable if o["status"] == "found"), default=None)}

    def sources(self, loop: dict, responses: list[dict]) -> list[dict]:
        by_cap = {r["capability"]: r for r in responses}
        rows = []
        cs = by_cap.get("connectors.status")
        if cs and cs["status"] == "succeeded":
            for s_ in cs["result"].get("sources") or []:
                row = {"source": s_["source"], "connected": bool(s_["connected"]), "detail": s_.get("missing") or s_.get("how", "")}
                cm = s_.get("client_mapping")
                if cm is not None:
                    row["connected"] = bool(cm["ready"])
                    row["detail"] = ("property " + ", ".join(cm["properties"])) if cm["ready"] else cm["why_not"]
                    row["used_in_this_loop"] = bool(cm["ready"]) and s_["source"] in PLUGGABLE_SOURCES and PLUGGABLE_SOURCES[s_["source"]] in by_cap
                rows.append(row)
        else:
            rows.append({"source": "connectors", "connected": False, "detail": "the data-source check itself could not run in this round"})
        for src, cap in PLUGGABLE_SOURCES.items():
            r = by_cap.get(cap)
            row = next((x for x in rows if x["source"] == src), None)
            if r and r["status"] != "succeeded" and row is not None:
                row["detail"] = f"requested but failed: {(r.get('error') or {}).get('code') or r['status']}"
                row["connected"] = False
            elif r and r["status"] == "succeeded" and row is not None:     # data actually came back in this round: that is the proof, whatever the status check said first
                row.update(connected=True, used_in_this_loop=True, detail=f"property {r['result'].get('property', '')} (data retrieved)")
        return rows

    def cross_checks(self, loop: dict, latest: dict[str, dict]) -> list[str]:
        """Where independent measurements disagree, say so: a first-party average (many users, all places and devices) is not one browser's observation."""
        notes = []
        for t in self.targets(loop):
            k = norm_key_tail(t["key"])
            serp = latest.get(t["key"])
            for tag, label in (("gsc", LABEL_OF["google_search_console"]), ("bing", LABEL_OF["bing_webmaster_tools"])):
                o = latest.get(f"{tag}|query|{k}|position")
                if not (serp and o and serp["status"] == "found" and o["status"] == "found" and serp["value"] is not None and o["value"] is not None):
                    continue
                if abs(serp["value"] - o["value"]) > POSITION_TOLERANCE:
                    notes.append(f"{t['label']}: this browser saw position {serp['value']:g}, but {label}'s average position is {o['value']:g}. They measure different things (one search from one place vs an average over many users, "
                                 "locations and devices): neither is wrong, and the gap itself is information (for example strong personalisation or location effects).")
        return notes

    def brief(self, svc, loop: dict) -> dict:
        arts = svc.store.artifacts(task_id=loop["task_id"], kind="finding_set", limit=5) if loop.get("task_id") else []
        cfg = loop["config"]
        comp = svc.store.artifacts(task_id=loop["task_id"], kind="analysis", limit=5) if loop.get("task_id") else []
        reps = svc.store.artifacts(task_id=loop["task_id"], kind="report", limit=3) if loop.get("task_id") else []
        return {"campaign_id": cfg.get("campaign_id"), "engine": cfg.get("engine"), "location": cfg.get("location"), "audit_artifact_ids": [a["artifact_id"] for a in arts],
                "competitor_artifact_ids": [a["artifact_id"] for a in comp if "ompetitor" in (a.get("title") or "")], "report_artifact_ids": [a["artifact_id"] for a in reps],
                "how_to_plan": "read the latest audit finding set (artifact.get), the comparison rows below and the competitors (seo.competitors) when allowed; propose changes whose target keywords are listed per item"}


# ============================================================================================================================ page checks (not SEO)
class PageChecks(LoopKind):
    name = "page_checks"
    title = "Live page checks"
    description = ("Keep checking that a live page satisfies explicit expectations until they pass on consecutive checks. config: {url, checks:[{type,value?}], mode?}. "
                   "goal: {confirmations?, max_rounds?, deadline?, max_requests?}. Shows the controller is not SEO-specific.")
    permissions = ("net:fetch", "collab:write")

    def check(self, svc, client_id: str, config: dict, goal: dict) -> list[str]:
        p = []
        if not isinstance(config.get("url"), str) or not config["url"].strip():
            p.append("config.url is required")
        if not isinstance(config.get("checks"), list) or not config["checks"]:
            p.append("config.checks must be a non-empty list of verify.page checks")
        goal.setdefault("targets", [{"keyword": config.get("url", "page")}])
        return p

    def targets(self, loop: dict) -> list[dict]:
        return [{"key": f"page|{loop['config'].get('url', '')}", "label": f"{loop['config'].get('url', '')} passes {len(loop['config'].get('checks') or [])} check(s)"}]

    def requests(self, loop: dict, purpose: str) -> list[dict]:
        c = loop["config"]
        return [{"capability": "verify.page", "parameters": {"url": c["url"], "checks": c["checks"], "mode": c.get("mode", "auto")}, "label": "page checks"}]

    def observations(self, loop: dict, purpose: str, responses: list[dict]) -> list[dict]:
        key, label = self.targets(loop)[0]["key"], self.targets(loop)[0]["label"]
        r = responses[0]
        if r["status"] != "succeeded":
            return [_standard({"key": key, "label": label, "metric": "failing_checks", "status": "unavailable", "observed_at": r.get("completed_at") or time.time(), "request_id": r["request_id"],
                               "limits": [f"the check could not run: {(r.get('error') or {}).get('code', r['status'])}"]})]
        failing = [c for c in r["result"].get("checks") or [] if not c.get("passed")]
        return [_standard({"key": key, "label": label, "metric": "failing_checks", "value": float(len(failing)), "status": "found", "comparable": True, "cmp_key": "page_checks",
                           "observed_at": r.get("completed_at"), "request_id": r["request_id"], "source": "verify.page", "limits": [f"failing: {c.get('type')}" for c in failing][:5]})]

    def evaluate_target(self, target: dict, series: list[dict], confirmations: int, min_gap_s: float) -> dict:
        usable = [o for o in series if o["status"] == "found"]
        if not usable:
            return {"verdict": "insufficient", "reason": "the page could not be checked yet", "observations": 0, "volatile": False}
        verdict, streak = streak_verdict(usable, lambda o: o["value"] == 0, confirmations, min_gap_s)
        return {"verdict": verdict, "streak": streak, "needed": confirmations, "volatile": False, "observations": len(usable), "target": "all checks pass",
                "current": {"status": usable[-1]["status"], "value": usable[-1]["value"], "observed_at": usable[-1]["observed_at"]}}


register_kind(SeoVisibility())
register_kind(PageChecks())
