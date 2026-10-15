"""SEO research capabilities: campaigns, search observations, site audit, competitor review, report. Built on the generic web/browser capabilities and stored in
the Knowledge Hub (campaigns, research sessions/observations) and collab.db (evidence, finding sets, reports)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from urllib.parse import urlsplit

from ...business.research import ResearchLog
from ...connectors import LABEL
from ...business.store import StoreError
from ...seo import audit as AUDIT
from ...seo import fetch as F
from ...business.campaigns import Campaigns
from ...seo.report import build_report
from ...seo import serp as SERP
from ...seo.serp import classify
from ...seo.signals import brief, extract, host_key
from ..capabilities import A, B, I, O, S, capability
from ..store import CollabError
from .browser import BROWSER_LIMITS, run_search
from .core import client_context, flat, require_client
from .web import HTTP_LIMITS, run_crawl


def _camps(ctx) -> Campaigns:
    return Campaigns(ctx.admin().store)


def _research(ctx) -> ResearchLog:
    return ctx.admin().research


def _store_err(e: StoreError) -> CollabError:
    return CollabError(e.code, e.message)


def valid_serp_sessions(ctx, camp: dict | None) -> list[str]:
    """Every SERP session of the campaign that finished with data and was not invalidated, newest first."""
    rlog = _research(ctx)
    out = []
    for r in (camp or {}).get("runs", []):
        if r["kind"] != "serp" or not r["research_session_id"] or r["status"] != "finished":
            continue
        try:
            if rlog.get_session(r["research_session_id"])["status"] != "abandoned":
                out.append(r["research_session_id"])
        except StoreError:
            continue
    return out


def merged_serp(ctx, cid: str, sessions: list[str]) -> list[dict]:
    """Per (query, engine) keep the observations of the NEWEST valid session that observed it (rank + its organic rows + map pack)."""
    rlog = _research(ctx)
    chosen: dict[tuple, str] = {}
    rows_by_session = {s: rlog.observations(cid, session_id=s, limit=2000) for s in sessions}
    for s in sessions:   # newest first
        for o in rows_by_session[s]:
            if o["kind"] == "keyword_rank":
                chosen.setdefault((o["query"].lower(), o["search_engine"]), s)
    return [o for s in sessions for o in rows_by_session[s] if chosen.get((o["query"].lower(), o["search_engine"])) == s]


def latest_serp_session(ctx, camp: dict | None) -> str | None:
    """The newest SERP session of the campaign that is still valid (not invalidated/abandoned) and produced observations."""
    rlog = _research(ctx)
    for r in (camp or {}).get("runs", []):
        if r["kind"] != "serp" or not r["research_session_id"] or r["status"] not in ("finished",):
            continue
        try:
            if rlog.get_session(r["research_session_id"])["status"] == "abandoned":
                continue
        except StoreError:
            continue
        return r["research_session_id"]
    return None


KW_ITEM = {"type": ["string", "object"], "description": "'keyword', 'keyword | location | service', or {keyword, location?, service?}"}


@capability("seo.campaign_upsert", "Create or update ONE client's SEO campaign: website, objective, language/country, target services and locations, and bulk keywords "
            "(duplicates are reported, not added; keyword locations are checked against the client's known service areas).",
            props={"campaign_id": S("Existing campaign to update (omit to create)."), "name": S("Campaign name.", maxLength=200), "website": S("Website URL.", maxLength=300),
                   "objective": S("What the campaign should achieve.", maxLength=1000), "language": S("Language code (default en).", maxLength=10), "country": S("Country code (default us).", maxLength=10),
                   "services": A("Target services.", maxItems=50), "locations": A("Target locations.", maxItems=50), "keywords": {"type": "array", "items": KW_ITEM, "maxItems": 300,
                                                                                                                                  "description": "Keywords to add (bulk)."},
                   "status": S("draft | researching | baseline_set | active | paused | closed")},
            output={"campaign": "{campaign_id, ..., keywords[], runs[]}", "added": "[]", "duplicates": "[]", "warnings": "[]"},
            permissions=("research:write", "client:read"), scope="client", read_only=False, category="seo", timeout_s=30)
async def campaign_upsert(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    website = p.get("website") or flat(client_context(ctx.admin(), cid))["website"]
    try:
        res = _camps(ctx).upsert(cid, campaign_id=p.get("campaign_id"), name=p.get("name", ""), website=website, objective=p.get("objective", ""), language=p.get("language", "en"),
                                 country=p.get("country", "us"), services=p.get("services"), locations=p.get("locations"), keywords=p.get("keywords"), status=p.get("status"),
                                 source=f"collab:{ctx.caller}")
    except StoreError as e:
        raise _store_err(e) from None
    if res["warnings"]:
        ctx.outcome.completeness = "partial"
        for w in res["warnings"]:
            ctx.limit(w)
    return res


@capability("seo.campaign_get", "Read ONE client's SEO campaign(s): settings, keywords (with service-area status), runs, research sessions and observation counts.",
            props={"campaign_id": S("A specific campaign (default: all of the client's campaigns).")},
            output={"campaigns": "[...]", "research": "{sessions, observations}"},
            permissions=("client:read",), scope="client", category="seo", timeout_s=20)
async def campaign_get(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    try:
        cs = [_camps(ctx).get(cid, p["campaign_id"])] if p.get("campaign_id") else _camps(ctx).list(cid)
    except StoreError as e:
        raise _store_err(e) from None
    return {"campaigns": cs, "research": _research(ctx).summary(cid)}


@capability("seo.serp_observe", "For ONE client's campaign keywords, run searches in the agent's browser and record dated, traceable observations: the client's organic "
            "position (or absence) per keyword, every organic result with client/competitor/directory classification, and map-pack presence. Starts a research session.",
            props={"campaign_id": S("Campaign (default: the client's only campaign)."), "keywords": A("Subset of campaign keywords to check (default: all, capped).", maxItems=30),
                   "engine": S("google (default) or bing.", enum=["google", "bing"]), "max_keywords": I("Cap (default 8).", minimum=1, maximum=30),
                   "location": S("Canonical location to emulate for Google, e.g. 'Portland,Oregon,United States' (default: none).", maxLength=120),
                   "screenshot": B("Save a screenshot per search as evidence.")},
            output={"session_id": "research session", "keywords": "[{keyword, location, client_position|null, client_url, top: [...], local_pack, blocked}]"},
            permissions=("search:read", "research:write", "client:read"), scope="client", read_only=False, category="seo", needs_browser=True, timeout_s=600, max_timeout_s=1800,
            limitations=BROWSER_LIMITS + ("Positions are observations from this browser at this time, not universal rankings.",
                                          "Local-pack order is not measured; only whether a map pack appeared and which business names its links carried."))
async def serp_observe(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    camps = _camps(ctx)
    try:
        camp = camps.find_for_client(cid, p.get("campaign_id"))
    except StoreError as e:
        raise _store_err(e) from None
    info = flat(client_context(ctx.admin(), cid))
    client_domains = [host_key(u) for u in (camp.get("website"), info["website"]) if u]
    kws = camp["keywords"]
    if p.get("keywords"):
        wanted = {k.lower().strip() for k in p["keywords"]}
        kws = [k for k in kws if k["keyword"].lower() in wanted]
    skipped_outside = [k for k in kws if k.get("area_status") == "outside"]
    kws = [k for k in kws if k.get("area_status") != "outside"][: p.get("max_keywords", 8)]
    if not kws:
        raise CollabError("NO_KEYWORDS", "no campaign keywords to check (keywords outside the client's service areas are skipped until the owner confirms them)")
    engine = p.get("engine", "google")
    rlog = _research(ctx)
    sess = rlog.start_session(cid, f"SERP check for campaign {camp['campaign_id']}", search_engine=engine, actor=f"collab:{ctx.caller}")
    run = camps.start_run(cid, camp["campaign_id"], "serp", research_session_id=sess["session_id"], request_id=ctx.request_id)
    out = []
    blocked = unparsed = 0
    for k in kws:
        ctx.check_cancel()
        q = k["keyword"] if not k.get("location") or k["location"].split(",")[0].lower() in k["keyword"].lower() else f"{k['keyword']} {k['location']}"
        loc_label = k.get("location") or p.get("location") or "(none)"
        ctx.progress(f"searching '{q}'")
        try:
            res = await run_search(ctx, engine, q, country=camp.get("country") or "us", language=camp.get("language") or "en", location=p.get("location", ""), screenshot=p.get("screenshot", False))
        except CollabError as e:
            out.append({"keyword": k["keyword"], "query": q, "location": loc_label, "error": e.code})
            if e.code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED"):
                raise
            continue
        row = {"keyword": k["keyword"], "query": q, "location": loc_label, "observed_at": res["observed_at"], "evidence_id": res["evidence_id"], "blocked": res["blocked"],
               "method": res.get("method"), "client_position": None, "client_url": None, "top": [], "local_pack": res.get("local_pack")}
        if res["blocked"]:
            blocked += 1
            out.append(row)
            continue
        if not res["organic"]:   # nothing could be read: that is a failed observation, never "the client is not ranking"
            row["error"] = "NO_RESULTS_PARSED"
            unparsed += 1
            out.append(row)
            continue
        for o in res["organic"]:
            cls = classify(o["domain"], client_domains)
            row["top"].append({**o, "class": cls})
            rlog.add_observation(sess["session_id"], "serp_result", query=q, location=loc_label, search_engine=engine, result_type="organic", position=o["position"], url=o["url"],
                                 domain=o["domain"], business=o["title"][:200], evidence=f"collab evidence {res['evidence_id']}", source=f"browser:{engine}",
                                 data={"class": cls, "method": res.get("method"), "request_id": ctx.request_id}, observed_at=res["observed_at"], actor=f"collab:{ctx.caller}")
            if cls == "client" and row["client_position"] is None:
                row["client_position"], row["client_url"] = o["position"], o["url"]
        rlog.add_observation(sess["session_id"], "keyword_rank", query=q, location=loc_label, search_engine=engine, result_type="organic", position=row["client_position"],
                             url=row["client_url"] or "", domain=client_domains[0] if client_domains else "", business=info["name"],
                             evidence=f"collab evidence {res['evidence_id']}; " + (f"client at organic #{row['client_position']}" if row["client_position"] else f"client not in the top {len(res['organic'])} organic results shown"),
                             source=f"browser:{engine}", data={"checked_results": len(res["organic"]), "method": res.get("method"), "request_id": ctx.request_id}, observed_at=res["observed_at"],
                             actor=f"collab:{ctx.caller}")
        lp = res.get("local_pack") or {}
        if lp.get("detected"):
            in_pack = SERP.in_pack(info["name"], lp.get("names", []))
            rlog.add_observation(sess["session_id"], "local_pack_result", query=q, location=loc_label, search_engine=engine, result_type="local_pack",
                                 position=(next((i for i, n in enumerate(lp.get("names", []), 1) if SERP.in_pack(info["name"], [n])), None)),
                                 business=info["name"] if in_pack else "", evidence=f"map pack shown; businesses in link order: {', '.join(lp.get('names', [])[:5]) or 'none parsed'}",
                                 source=f"browser:{engine}", data={"client_in_pack": in_pack, "names": lp.get("names", []), "request_id": ctx.request_id}, observed_at=res["observed_at"],
                                 actor=f"collab:{ctx.caller}")
            row["client_in_local_pack"] = in_pack
        out.append(row)
    good = len(out) - blocked - unparsed
    rlog.finish_session(sess["session_id"], findings=f"{len(out)} keyword(s) on {engine}: {good} observed, {blocked} blocked, {unparsed} unreadable", actor=f"collab:{ctx.caller}")
    camps.finish_run(run, status="finished" if good else "no_data", summary=f"{len(out)} keywords: {good} observed, {blocked} blocked, {unparsed} unreadable")
    if good:
        camps.set_baseline(cid, camp["campaign_id"], sess["session_id"])
    if blocked or unparsed:
        ctx.outcome.completeness = "partial" if good else "none"
    if blocked:
        ctx.limit(f"{blocked} search(es) hit a CAPTCHA/consent wall and were not bypassed")
    if unparsed:
        ctx.limit(f"{unparsed} results page(s) could not be read into organic results; no ranking was recorded for them")
    if any(t.get("url_inferred") for r in out for t in r.get("top", [])):
        ctx.limit("some result URLs were wrapped by the engine; only their domain is known (url_inferred)")
    if skipped_outside:
        ctx.limit(f"{len(skipped_outside)} keyword(s) outside the known service areas were skipped until the owner confirms them")
    ctx.outcome.confidence = "medium"
    return {"session_id": sess["session_id"], "campaign_id": camp["campaign_id"], "engine": engine, "keywords": out}


async def _variants(site_url: str) -> dict:
    """http->https and www/apex consistency for the site's canonical host."""
    host = urlsplit(site_url).hostname or ""
    apex = host[4:] if host.startswith("www.") else host
    canonical_https = f"https://{host}/"
    checks = []
    for u in {f"http://{apex}/", f"http://www.{apex}/", f"https://{apex}/", f"https://www.{apex}/"}:
        if u == canonical_https:
            continue
        try:
            r = await F.fetch(u, max_bytes=50_000)
            if r.challenge:
                checks.append({"url": u, "ok": None, "detail": f"bot-protection page (HTTP {r.status}): redirect behaviour not measurable from here", "title": "", "recommendation": ""})
                continue
        except F.FetchError as e:
            checks.append({"url": u, "ok": None, "detail": f"{e.code}: could not be measured from this machine (network/DNS), not counted as a defect", "title": "", "recommendation": ""})
            continue
        final_host = urlsplit(r.final_url).hostname
        ok = r.final_url.startswith("https://") and final_host == host and len(r.chain) <= 2 and r.status == 200
        checks.append({"url": u, "ok": ok, "detail": " -> ".join([h.url for h in r.chain] + [r.final_url]) + f" ({r.status})",
                       "title": f"{u} does not end on {canonical_https} cleanly", "recommendation": f"301-redirect {u} straight to {canonical_https} (one hop).",
                       "severity": "high" if u.startswith("http://") and not r.final_url.startswith("https://") else "medium"})
    return {"canonical_host": host, "checks": checks}


@capability("seo.audit_site", "Audit ONE client's website: bounded crawl + robots/sitemap + host variants, then deterministic rules that produce findings with evidence, severity, "
            "confidence, recommendation, verification method and priority. Uses the client's Knowledge Hub context (phone, target locations) and campaign keywords. "
            "Stores the finding set as an artifact and technical observations in a research session.",
            props={"url": S("Start URL (default: campaign website, else the client's website)."), "max_pages": I("Crawl cap (default 30).", minimum=1, maximum=80),
                   "campaign_id": S("Campaign whose keywords to check coverage for."),
                   "exclude": A("Extra path fragments never to request (risky-looking paths are always skipped).", maxItems=50),
                   "mode": S("auto (default: HTTP, falling back to the real browser when the site serves bot-protection pages to scripts) | http | browser",
                             enum=["auto", "http", "browser"])},
            output={"artifact_id": "finding set", "session_id": "research session", "pages_crawled": "int", "findings": "[{finding_id, rule, title, category, type, severity, confidence, pages, evidence, recommendation, verification, effort, value, priority_score}]",
                    "summary": "{by_severity, by_type}"},
            permissions=("net:fetch", "client:read", "research:write", "collab:write"), scope="client", read_only=False, category="seo", timeout_s=600, max_timeout_s=1800,
            limitations=HTTP_LIMITS + ("Rules read raw HTML; JavaScript-only content is not seen.", "Findings of type hypothesis need human review before acting."))
async def audit_site(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    cdoc = client_context(ctx.admin(), cid)
    info = flat(cdoc)
    camp = None
    try:
        camp = _camps(ctx).find_for_client(cid, p.get("campaign_id"))
    except StoreError:
        camp = None
    url = p.get("url") or (camp or {}).get("website") or info["website"]
    if not url:
        raise CollabError("NO_WEBSITE", "no URL given and the client has no website in the Knowledge Hub")
    run = _camps(ctx).start_run(cid, camp["campaign_id"], "audit", request_id=ctx.request_id) if camp else None
    mode = p.get("mode", "auto")
    res = await run_crawl(ctx, url, p.get("max_pages", 30), True, p.get("exclude")) if mode != "browser" else {"blocked": {"reason": "BROWSER_MODE_REQUESTED"}, "pages": []}
    full = res.pop("_full", {})
    http_block = res.get("blocked")
    if http_block and mode == "http":
        raise CollabError("SITE_CHALLENGE", "the site answered the agent's HTTP client with a bot-protection page; nothing was audited. Use mode=auto/browser, "
                          "or ask the owner to allow the agent in the hosting/CDN security settings.", {"blocked": http_block})
    if http_block:
        from .browser_crawl import NOT_MEASURED, browser_crawl
        if http_block.get("reason") == "BOT_CHALLENGE":
            ctx.limit(f"the site served a bot-protection page to plain HTTP (HTTP {http_block.get('status')}, server {http_block.get('server') or '?'}); pages were read through the real browser instead")
        async with ctx.svc._browser_lock:
            res = await browser_crawl(ctx, url, p.get("max_pages", 30), p.get("exclude"))
        full = res.pop("_full", {})
        if res.get("blocked"):
            raise CollabError("SITE_CHALLENGE", "the real browser was also shown a bot-protection challenge; it was not bypassed and nothing was audited", {"blocked": res["blocked"]})
        ctx.limit("browser mode: not measured: " + ", ".join(NOT_MEASURED))
        ctx.outcome.completeness = "partial"
    start = res["pages"][0]["final_url"] if res["pages"] else url
    variants = await _variants(start) if res.get("mode", "http") == "http" else {"canonical_host": urlsplit(start).hostname, "checks": [], "note": "not measured in browser mode"}
    ctx.evidence("host_variants", variants, source_url=start, summary="http/https/www checks")
    locs = list(dict.fromkeys((camp or {}).get("locations", []) + info["target_locations"] + [k["location"] for k in (camp or {}).get("keywords", []) if k.get("location")]))
    findings = AUDIT.run(res, full=full, context={**info, "target_locations": locs}, variants=variants, keywords=(camp or {}).get("keywords") or [{"keyword": k} for k in info["keywords"]])
    rlog = _research(ctx)
    sess = rlog.start_session(cid, f"Site audit of {start}", actor=f"collab:{ctx.caller}")
    for f in findings[:60]:
        rlog.add_observation(sess["session_id"], "technical" if f["category"] in ("technical", "indexability", "structured_data", "metadata", "duplication") else "content",
                             url=(f["pages"] or [""])[0], domain=host_key(start), evidence=f"{f['finding_id']} {f['title']}", source="http-crawl",
                             data={"rule": f["rule"], "severity": f["severity"], "confidence": f["confidence"], "type": f["type"], "request_id": ctx.request_id}, actor=f"collab:{ctx.caller}")
    rlog.finish_session(sess["session_id"], findings=f"{len(findings)} finding(s) over {len(res['pages'])} page(s)", actor=f"collab:{ctx.caller}")
    by_sev, by_type = {}, {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    pages_brief = [{k: pg.get(k) for k in ("url", "final_url", "status", "in_sitemap", "inbound_internal_links", "evidence_id")} | {"title": (pg.get("signals") or {}).get("title"),
                    "h1": (pg.get("signals") or {}).get("h1"), "words": (pg.get("signals") or {}).get("word_count")} for pg in res["pages"]]
    body = {"site": start, "crawled_at": time.time(), "mode": res.get("mode", "http"), "measured": res.get("measured"), "not_measured": res.get("not_measured"),
            "http_block": http_block if mode != "browser" else None, "skipped": res.get("skipped", [])[:100], "errors": res.get("errors", [])[:100], "pages": pages_brief, "robots": {k: v for k, v in (res.get("robots") or {}).items() if k != "body"},
            "sitemap": {k: v for k, v in (res.get("sitemap") or {}).items() if k != "urls"}, "variants": variants, "findings": findings, "summary": {"by_severity": by_sev, "by_type": by_type},
            "research_session_id": sess["session_id"], "request_id": ctx.request_id, "campaign_id": (camp or {}).get("campaign_id"), "limitations": ctx.outcome.limitations}
    art = ctx.svc.store.add_artifact(task_id=ctx.task_id, client_id=cid, kind="finding_set", stage="research", title=f"Site audit: {host_key(start)} ({len(findings)} findings)", body=body,
                                     links={"request_id": ctx.request_id, "evidence": ctx.evidence_ids[-200:]}, created_by=f"agent:{ctx.caller}")
    if run:
        _camps(ctx).finish_run(run, summary=f"{len(findings)} findings", artifact_id=art["artifact_id"])
    return {"artifact_id": art["artifact_id"], "session_id": sess["session_id"], "site": start, "mode": body["mode"], "measured": body["measured"], "not_measured": body["not_measured"],
            "pages_crawled": len(res["pages"]), "summary": body["summary"], "findings": findings,
            "robots": body["robots"], "sitemap": body["sitemap"], "variants": variants, "pages": pages_brief}


@capability("seo.competitors", "Review the competing pages actually observed in a SERP research session for ONE client: fetch each competitor landing page and record its title, "
            "headings, services/locations mentioned, structure, internal links, schema and other signals, compared with the client's own best page for that keyword. "
            "Records competitor observations. Does not copy competitor content.",
            props={"session_id": S("SERP research session (default: the latest SERP session of the campaign)."), "campaign_id": S("Campaign."),
                   "max_competitors": I("Distinct competitor pages to fetch (default 6).", minimum=1, maximum=15)},
            output={"competitors": "[{domain, url, keywords[], best_position, title, h1, h2_sample, word_count, schema, mentions{services,locations}, internal_links}]",
                    "client_pages": "[...]", "opportunities": "[evidence-backed observations]"},
            permissions=("net:fetch", "client:read", "research:write", "collab:write"), scope="client", read_only=False, category="seo", timeout_s=300, max_timeout_s=900,
            limitations=HTTP_LIMITS + ("Correlation only: a competitor's characteristic is not proof of why it ranks.",))
async def competitors(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    rlog = _research(ctx)
    camps = _camps(ctx)
    try:
        camp = camps.find_for_client(cid, p.get("campaign_id"))
    except StoreError as e:
        raise _store_err(e) from None
    sid = p.get("session_id")
    if not sid:
        sids = valid_serp_sessions(ctx, camp)
        if not sids:
            raise CollabError("NO_SERP_DATA", "no valid SERP observations yet: run seo.serp_observe first")
        sid = ",".join(sids)
        obs = [o for o in merged_serp(ctx, cid, sids) if o["kind"] == "serp_result"]
    else:
        obs = rlog.observations(cid, session_id=sid, kind="serp_result", limit=1000)
    if not obs:
        raise CollabError("NO_SERP_DATA", "that session has no organic results")
    comp: dict[str, dict] = {}
    for o in obs:
        d = json.loads(o["data"] or "{}")
        if d.get("class") != "competitor":
            continue
        key = o["url"].split("#")[0]
        e = comp.setdefault(key, {"domain": o["domain"], "url": key, "keywords": [], "best_position": o["position"] or 99})
        if o["query"] not in e["keywords"]:
            e["keywords"].append(o["query"])
        e["best_position"] = min(e["best_position"], o["position"] or 99)
    ranked = sorted(comp.values(), key=lambda e: (-len(e["keywords"]), e["best_position"]))[: p.get("max_competitors", 6)]
    info = flat(client_context(ctx.admin(), cid))
    svc_terms = [s.lower() for s in (camp.get("services") or info["services"])][:20]
    loc_terms = [l.split(",")[0].lower() for l in (camp.get("locations") or info["target_locations"])][:20]
    out = []
    sess = rlog.start_session(cid, f"Competitor review from {sid}", actor=f"collab:{ctx.caller}")
    for e in ranked:
        ctx.check_cancel()
        ctx.progress(f"fetching {e['url']}")
        from .competitors import page_row, read_page
        pr = await read_page(ctx, e["url"], "auto")     # HTTP first; the owner's real browser when the site refuses scripts or cannot be reached by HTTP
        if not pr["ok"]:
            out.append({**e, "error": pr["error"], "tried": pr["via"]})
            continue
        sig = pr["sig"]
        row = page_row(pr, e, svc_terms, loc_terms)
        row["evidence_id"] = ctx.evidence("competitor_page", {k: v for k, v in sig.items() if k != "text"} | {"via": pr["via"]}, source_url=pr["final_url"], summary=f"{e['domain']} {sig['title'][:60]!r}")
        rlog.add_observation(sess["session_id"], "competitor", url=pr["final_url"], domain=e["domain"], business=sig["title"][:200], evidence=f"collab evidence {row['evidence_id']}",
                             source=pr["via"], data={k: row[k] for k in ("keywords", "best_position", "word_count", "schema", "internal_links")} | {"request_id": ctx.request_id},
                             actor=f"collab:{ctx.caller}")
        out.append(row)
    rlog.finish_session(sess["session_id"], findings=f"{len(out)} competitor page(s) reviewed", actor=f"collab:{ctx.caller}")
    good = [c for c in out if not c.get("error")]
    failed = [c for c in out if c.get("error")]
    if failed:
        ctx.outcome.completeness = "partial" if good else "none"
        ctx.limit(f"{len(failed)} of {len(out)} competitor page(s) could not be fetched ({', '.join(sorted({c['error'] for c in failed}))}): "
                  + ", ".join(c["domain"] for c in failed[:8]))
    if 0 < len(good) < 3:
        ctx.outcome.confidence = "low"
        ctx.limit(f"only {len(good)} competitor page(s) were reviewed: the comparisons below are anecdotal")
    opp = []
    if good:
        avg_words = sum(c["word_count"] for c in good) / len(good)
        with_schema = [c for c in good if any("LocalBusiness" in t or "Organization" in t or "Store" in t for t in (c["schema"] or []))]
        loc_in_title = [c for c in good if any(l in (c["title"] or "").lower() for l in loc_terms)]
        if loc_terms:
            opp.append({"observation": f"{len(loc_in_title)}/{len(good)} observed competitor pages name a target location in their title", "evidence": [c["evidence_id"] for c in loc_in_title]})
        else:
            opp.append({"observation": "location names in competitor titles were not counted: no target locations are on file", "evidence": []})
        opp.append({"observation": f"{len(with_schema)}/{len(good)} competitor pages carry LocalBusiness/Organization JSON-LD", "evidence": [c["evidence_id"] for c in with_schema]})
        opp.append({"observation": f"competitor pages average about {round(avg_words)} words of HTML text", "evidence": [c["evidence_id"] for c in good]})
    body = {"serp_session_id": sid, "competitor_session_id": sess["session_id"], "competitors": out, "observations": opp,
            "caveat": "Correlations from a small sample of observed pages; not proof of ranking causes. Do not copy competitor wording."}
    art = ctx.svc.store.add_artifact(task_id=ctx.task_id, client_id=cid, kind="analysis", stage="research", title=f"Competitor review ({len(good)} pages)", body=body,
                                     links={"request_id": ctx.request_id}, created_by=f"agent:{ctx.caller}")
    run = camps.start_run(cid, camp["campaign_id"], "competitors", research_session_id=sess["session_id"], request_id=ctx.request_id)
    camps.finish_run(run, summary=f"{len(good)} competitor pages", artifact_id=art["artifact_id"])
    if ctx.outcome.confidence == "high":
        ctx.outcome.confidence = "medium"
    return {"artifact_id": art["artifact_id"], "session_id": sess["session_id"], "competitors": out, "observations": opp}


def _performance(ctx, cid: str) -> dict:
    """The newest first-party search-performance retrieval per source (GSC, Bing) for this client, from the research log."""
    rlog = _research(ctx)
    out = {}
    for src, label in ((k, LABEL[k]) for k in LABEL):
        rows = [o for o in rlog.observations(cid, kind="search_performance", limit=800) if o["source"] == src]
        if not rows:
            continue
        sess = rows[0]["session_id"]          # newest first
        rows = [o for o in rows if o["session_id"] == sess]
        total = next((json.loads(o["data"] or "{}") for o in rows if o["query"] == "(site total)"), {})
        qs = []
        for o in rows:
            if o["query"] in ("(site total)", ""):
                continue
            d = json.loads(o["data"] or "{}")
            qs.append({"query": o["query"], "clicks": d.get("clicks", 0), "impressions": d.get("impressions", 0), "position": d.get("position", d.get("avg_impression_position"))})
        qs.sort(key=lambda q: (-q["impressions"], -q["clicks"]))
        out[src] = {"label": label, "property": total.get("property", ""), "window": total.get("window", ""), "totals": {"clicks": total.get("clicks", 0), "impressions": total.get("impressions", 0),
                    "position": total.get("position", total.get("avg_impression_position")), "ctr": total.get("ctr")}, "queries": qs, "session_id": sess}
    return out


def _failed_searches(st, cid: str) -> list[dict]:
    """Searches that were tried for this client and gave no usable result (CAPTCHA wall, unreadable page), from the recorded seo.serp_observe results.
    They are context for 'unavailable' in the report, never observations."""
    out = []
    for j in st.jobs(limit=500):
        if j["capability"] != "seo.serp_observe" or j["client_id"] != cid or j["status"] != "succeeded" or not j["result"]:
            continue
        try:
            rows = json.loads(j["result"]).get("keywords") or []
        except (ValueError, AttributeError):
            continue
        for r in rows:
            if r.get("blocked"):
                why = "hit a CAPTCHA/consent wall and was not bypassed"
            elif r.get("error"):
                why = f"returned a results page that could not be read ({r['error']})"
            else:
                continue
            out.append({"query": r.get("query") or r.get("keyword", ""), "at": r.get("observed_at") or j["finished_at"] or j["created_at"], "outcome": why})
    return out


@capability("seo.report", "Build the SEO report for ONE client from stored data only (campaign, SERP observations, latest audit finding set, competitor review, data-source "
            "status): executive summary, baseline, keyword observations, competitor findings, technical issues, prioritised recommendations, missing data, implementation "
            "tasks and success criteria. Returns Markdown and structured JSON and stores both as a report artifact.",
            props={"campaign_id": S("Campaign."), "audit_artifact_id": S("Finding set to use (default: latest for the client)."),
                   "competitor_artifact_id": S("Competitor review to use (default: latest)."), "serp_session_id": S("SERP session (default: the campaign's latest)."),
                   "connectors": O("Data-source status to include, e.g. the result of connectors.status.")},
            output={"artifact_id": "report artifact", "markdown_path": "file", "json_path": "file", "report": "structured report", "markdown": "text"},
            permissions=("client:read", "collab:write"), scope="client", read_only=False, category="seo", timeout_s=60)
async def report(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    st = ctx.svc.store
    cdoc = client_context(ctx.admin(), cid)
    try:
        camp = _camps(ctx).find_for_client(cid, p.get("campaign_id"))
    except StoreError:
        camp = None

    def latest(kind: str, aid: str | None, pred=lambda a: True):
        if aid:
            a = st.get_artifact(aid)
            if a["client_id"] != cid:
                raise CollabError("SCOPE_MISMATCH", "that artifact belongs to another client", status=403)
            return a
        for row in st.artifacts(client_id=cid, kind=kind, limit=50):
            a = st.get_artifact(row["artifact_id"])
            if pred(a):
                return a
        return None
    audit_a = latest("finding_set", p.get("audit_artifact_id"), lambda a: a["status"] != "invalidated")
    comp_a = latest("analysis", p.get("competitor_artifact_id"), lambda a: "competitors" in (a["body"] or {}) and a["status"] != "invalidated")
    sids = [p["serp_session_id"]] if p.get("serp_session_id") else valid_serp_sessions(ctx, camp)
    serp = merged_serp(ctx, cid, sids) if sids else []
    sid = ",".join(sids)
    attempts = _failed_searches(st, cid)
    previous = next(iter(st.artifacts(client_id=cid, kind="report", limit=1)), None)
    rep = build_report(client=cdoc, summary=flat(cdoc), campaign=camp, audit=audit_a, competitors=comp_a, serp=serp, connectors=p.get("connectors"),
                       generated_at=time.time(), request_id=ctx.request_id, performance=_performance(ctx, cid), attempts=attempts)
    folder = st.reports_dir / cid
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    mdp, jsp = folder / f"seo-report-{stamp}.md", folder / f"seo-report-{stamp}.json"
    mdp.write_text(rep["markdown"], encoding="utf-8")
    jsp.write_text(json.dumps(rep["data"], indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    art = st.add_artifact(task_id=ctx.task_id, client_id=cid, kind="report", stage="reasoning", title=f"SEO report: {cdoc['client']['name']} ({stamp})", body=rep["data"], body_md=rep["markdown"],
                          links={"request_id": ctx.request_id, "audit": audit_a["artifact_id"] if audit_a else None, "competitors": comp_a["artifact_id"] if comp_a else None, "serp_session": sid,
                                 "supersedes": previous["artifact_id"] if previous else None, "files": [str(mdp), str(jsp)]}, created_by=f"agent:{ctx.caller}")
    if camp:
        r = _camps(ctx).start_run(cid, camp["campaign_id"], "report", research_session_id=sid or "", request_id=ctx.request_id)
        _camps(ctx).finish_run(r, summary="report built", artifact_id=art["artifact_id"])
    for m in rep["data"]["missing_data"]:
        ctx.limit(m)
    if rep["data"]["missing_data"]:
        ctx.outcome.completeness = "partial"
    return {"artifact_id": art["artifact_id"], "markdown_path": str(mdp), "json_path": str(jsp), "report": rep["data"], "markdown": rep["markdown"]}


@capability("research.invalidate_session", "Mark ONE client's research session as invalid (status abandoned, reason recorded) when its observations turned out not to describe "
            "reality (wrong location, unreadable pages, blocked crawl). Observations stay as history; reports stop using the session.",
            props={"session_id": S("Research session id.", minLength=1), "reason": S("Why, with the evidence.", minLength=10, maxLength=2000)},
            required=("session_id", "reason"), output={"session": "{session_id, status: abandoned, findings}"}, permissions=("research:write",), scope="client", read_only=False,
            category="seo", timeout_s=15)
async def research_invalidate(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    rlog = _research(ctx)
    s = rlog.get_session(p["session_id"])
    if s["client_id"] != cid:
        raise CollabError("SCOPE_MISMATCH", "that session belongs to another client", status=403)
    return {"session": rlog.invalidate_session(p["session_id"], p["reason"], actor=f"collab:{ctx.caller}")}
