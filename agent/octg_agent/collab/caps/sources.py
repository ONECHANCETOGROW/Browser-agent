"""Data-source capabilities: connector status/verification/mapping suggestions, GSC (Google), Bing Webmaster.

Every data capability here takes the CLIENT (explicit, server-checked) and uses ONLY the property the owner mapped to that client: a caller can never name a property, list the credential's
other properties, or reach another client's data. Credentials are never returned. Results are returned as raw rows (capped) plus `observations` in the loop controller's standard form,
and the full response is stored as evidence. Missing credentials, an unmapped client, an unauthorized property, an API failure and "no data in the window" are different, named outcomes.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from ...connectors import LABEL, SHORT, SOURCES, ConnectorError, domain_of
from ...seo.signals import host_key
from ..capabilities import A, B, I, S, capability
from ..store import CollabError
from .core import client_context, flat, require_client

GSC, BING = SOURCES
SC = SHORT[GSC]      # the provider's product name comes from connectors/providers.json
SRC_ENUM = list(SOURCES)


def _err(e: ConnectorError) -> CollabError:
    return CollabError(e.code, e.message, e.details or None, status=409, retryable=e.retryable)


def _domains(ctx, cid: str) -> list[str]:
    info = flat(client_context(ctx.admin(), cid))
    out = [info["website"]] if info.get("website") else []
    try:
        from ...business.campaigns import Campaigns
        out += [c["website"] for c in Campaigns(ctx.admin().store).list(cid) if c.get("website")]
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(out))


def _obs(key, label, metric, value, status, cmp_key, request_id, limits=None) -> dict:
    return {"key": key, "label": label, "metric": metric, "value": value, "status": status, "comparable": status in ("found", "not_found"), "cmp_key": cmp_key, "observed_at": time.time(),
            "source": "", "request_id": request_id, "limits": list(limits or [])}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


# =================================================================================================================  status / verify / mapping suggestions
@capability("connectors.status", "Which external data sources are actually connected for research (GSC, Bing Webmaster Tools, Google Analytics) and why not. Offline: reads which credential "
            "files exist and the result of the last live verification; never contacts a provider and never returns credentials. With a client, also whether a property is mapped to it. "
            "Never implies an unconnected source was queried.",
            output={"sources": "[{source, connected, credentials_present, how, missing, verified_at, mapped_clients, client_mapping?}]"}, permissions=("agent:read",), scope="client_optional", category="seo", timeout_s=10)
async def connectors_status(ctx, p: dict) -> dict:
    hub = ctx.svc.connectors
    rows = hub.status()
    for r in rows:
        if ctx.client_id:
            maps = hub.db.mappings(client_id=ctx.client_id, source=r["source"])
            r["client_mapping"] = {"mapped": bool(maps), "properties": [m["property"] for m in maps], "ready": bool(maps) and r["connected"],
                                   "why_not": "" if maps and r["connected"] else (r["missing"] if not r["credentials_present"] else ("no property is mapped to this client (owner: Hub -> AI & Approvals -> Data sources)" if not maps else r["missing"]))}
        r.pop("identity", None) if not ctx.client_id else None
        r.pop("mappings", None)
    rows.append({"source": "google_analytics_4", "label": "Google Analytics 4", "connected": False, "credentials_present": False, "how": "GA4 Data API (service account with Viewer on the property)",
                 "missing": "not implemented in the agent yet", "mapped_clients": 0})
    rows.append({"source": "public_web_http", "label": "Public web (HTTP)", "connected": True, "how": "web.* capabilities", "missing": ""})
    rows.append({"source": "browser_search_results", "label": "Search results in the owner's Chrome", "connected": ctx.rt.session is not None, "how": "search.web / seo.serp_observe in the owner's Chrome",
                 "missing": "" if ctx.rt.session is not None else "the Chrome extension is not connected"})
    return {"sources": rows, "checked_dir": str(ctx.svc.connectors.folder)}


@capability("connectors.verify", "Check, with ONE live read-only call, that the stored credentials of a data source work, and record the result. Returns how many properties the credential can see "
            "(a count, never their names), never the credentials.",
            props={"source": S("google_search_console | bing_webmaster_tools", enum=SRC_ENUM)}, required=("source",), output={"ok": "bool", "properties_visible": "int", "credential_kind": "string"},
            permissions=("connector:read",), read_only=False, category="seo", timeout_s=90)
async def connectors_verify(ctx, p: dict) -> dict:
    try:
        return await ctx.svc.connectors.verify(p["source"])
    except ConnectorError as e:
        raise _err(e) from None


@capability("connectors.suggest_mapping", "For ONE client: which of the credential's properties look like this client's website (domain match) and whether each is usable or already mapped, plus what is "
            "mapped now. Only the client's own domain matches are shown, never the credential's other properties. The OWNER makes the mapping (Hub, PIN); this cannot.",
            props={"source": S("google_search_console | bing_webmaster_tools", enum=SRC_ENUM)}, required=("source",), output={"candidates": "[{property, permission, usable, domain}]", "mapped": "[property]"},
            permissions=("connector:read", "client:read"), scope="client", category="seo", timeout_s=90)
async def connectors_suggest(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    try:
        return await ctx.svc.connectors.suggest(cid, p["source"], _domains(ctx, cid))
    except ConnectorError as e:
        raise _err(e) from None


# =================================================================================================================  GSC (Google)
def _gsc_window(p: dict) -> tuple[str, str, int]:
    days = int(p.get("days", 28))
    s, e = ctx_gsc_window(days, int(p.get("end_offset_days", 3)))
    return s, e, days


def ctx_gsc_window(days: int, offset: int) -> tuple[str, str]:
    end = date.today() - timedelta(days=offset)
    return (end - timedelta(days=max(1, days) - 1)).isoformat(), end.isoformat()


def _record(ctx, cid: str, source: str, label: str, rows: list[dict], total: dict | None) -> str:
    """Dated, traceable observations in the Knowledge Hub's research log (kind search_performance)."""
    rlog = ctx.admin().research
    sess = rlog.start_session(cid, label, search_engine="google" if source == GSC else "bing", actor=f"collab:{ctx.caller}")
    if total is not None:
        rlog.add_observation(sess["session_id"], "search_performance", query="(site total)", search_engine="google" if source == GSC else "bing", evidence=f"collab request {ctx.request_id}", source=source,
                             data={**total, "request_id": ctx.request_id}, actor=f"collab:{ctx.caller}")
    for r in rows[:60]:
        rlog.add_observation(sess["session_id"], "search_performance", query=str(r.get("query") or r.get("page") or "")[:200], search_engine="google" if source == GSC else "bing",
                             url=str(r.get("page") or "")[:500], evidence=f"collab request {ctx.request_id}", source=source, data={**r, "request_id": ctx.request_id}, actor=f"collab:{ctx.caller}")
    rlog.finish_session(sess["session_id"], findings=f"{len(rows)} row(s)", actor=f"collab:{ctx.caller}")
    return sess["session_id"]


@capability("gsc.search_analytics", "Read ONE client's Google search performance (GSC) (clicks, impressions, CTR, average position) from the property the owner mapped to that client: site totals, the "
            "top queries, and the figures for specific keywords. GSC's own data: averaged over many users, locations and devices, delayed by ~2 days, with rare queries omitted.",
            props={"days": I("Window length (default 28).", minimum=1, maximum=90), "end_offset_days": I("The window ends this many days ago (default 3: recent days are incomplete).", minimum=0, maximum=10),
                   "keywords": A("Queries to report figures for (exact match, case-insensitive).", maxItems=30), "dimensions": A("Row dimensions: query | page | date | country | device (default query).", maxItems=3),
                   "pages": A("Restrict to these exact page URLs.", maxItems=10), "row_limit": I("Rows to fetch (default 250, max 1000).", minimum=1, maximum=1000)},
            output={"property": "string", "window": "{start, end, days}", "totals": "{clicks, impressions, ctr, position}", "rows": "[{keys, clicks, impressions, ctr, position}]",
                    "keywords": "[{keyword, found, clicks, impressions, ctr, position}]", "observations": "[standard observations for loops]"},
            permissions=("connector:read", "client:read", "research:write"), scope="client", read_only=False, category="seo", timeout_s=120, max_timeout_s=300,
            limitations=("Average position is over all searches in the window, all locations and devices: it can differ a lot from one browser's observation.",
                         "GSC omits rare (anonymised) queries: a keyword with no row is NOT proof it had no impressions.", "Data for the last ~2 days is incomplete."))
async def gsc_search_analytics(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    hub = ctx.svc.connectors
    try:
        res = await hub.resolve(cid, GSC)
        cli = hub.client(GSC)
        start, end, days = _gsc_window(p)
        dims = [d for d in (p.get("dimensions") or ["query"])]
        totals_res = await cli.query(res.property, start=start, end=end, dimensions=[], row_limit=1, pages=p.get("pages"))
        rows_res = await cli.query(res.property, start=start, end=end, dimensions=dims, row_limit=p.get("row_limit", 250), pages=p.get("pages"))
    except ConnectorError as e:
        raise _err(e) from None
    trow = (totals_res["rows"] or [{}])[0]
    totals = {"clicks": trow.get("clicks", 0), "impressions": trow.get("impressions", 0), "ctr": trow.get("ctr", 0.0), "position": trow.get("position") if trow.get("impressions") else None}
    rows = [{"keys": r.get("keys", []), "query": (r.get("keys") or [""])[dims.index("query")] if "query" in dims and r.get("keys") else None,
             "page": (r.get("keys") or [""])[dims.index("page")] if "page" in dims and r.get("keys") else None, "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
             "ctr": r.get("ctr", 0.0), "position": r.get("position")} for r in rows_res["rows"]]
    ev = ctx.evidence("gsc_search_analytics", {"property": res.property, "window": {"start": start, "end": end}, "dimensions": dims, "totals": totals, "rows": rows}, source_url=res.property,
                      summary=f"{SC} {start}..{end}: {totals['clicks']} clicks, {totals['impressions']} impressions")
    wanted = [_norm(k) for k in (p.get("keywords") or [])]
    by_q = {_norm(r["query"] or ""): r for r in rows if "query" in dims}
    kw_out, cmpk = [], f"gsc|{days}d|{res.property}"
    obs = []
    win = f"{start}..{end}"
    lim_site = [f"{SC} {win}; averaged over all locations and devices"]
    obs += [_obs("gsc|site|clicks", f"{SC}: clicks", "clicks", float(totals["clicks"]), "found", cmpk + "|site", ctx.request_id, lim_site),
            _obs("gsc|site|impressions", f"{SC}: impressions", "impressions", float(totals["impressions"]), "found", cmpk + "|site", ctx.request_id, lim_site)]
    if totals["impressions"]:
        obs.append(_obs("gsc|site|position", f"{SC}: average position", "avg_position", float(totals["position"]), "found", cmpk + "|site", ctx.request_id, lim_site))
    else:
        obs.append(_obs("gsc|site|position", f"{SC}: average position", "avg_position", None, "unavailable", cmpk + "|site", ctx.request_id, ["no impressions were recorded in this window"]))
    for k in wanted:
        r = by_q.get(k) if "query" in dims else None
        label = next((x for x in p.get("keywords") if _norm(x) == k), k)
        if r:
            kw_out.append({"keyword": label, "found": True, "clicks": r["clicks"], "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"]})
            obs += [_obs(f"gsc|query|{k}|position", f"{SC} position: {label}", "avg_position", float(r["position"]), "found", cmpk + "|query", ctx.request_id, lim_site),
                    _obs(f"gsc|query|{k}|clicks", f"{SC} clicks: {label}", "clicks", float(r["clicks"]), "found", cmpk + "|query", ctx.request_id, lim_site),
                    _obs(f"gsc|query|{k}|impressions", f"{SC} impressions: {label}", "impressions", float(r["impressions"]), "found", cmpk + "|query", ctx.request_id, lim_site)]
        else:
            kw_out.append({"keyword": label, "found": False})
            obs.append(_obs(f"gsc|query|{k}|position", f"{SC} position: {label}", "avg_position", None, "unavailable", cmpk + "|query", ctx.request_id,
                            ["no row for this query: either it had no impressions in the window or it is too rare to be reported (not proof of absence)"]))
    for o in obs:
        o["source"] = "google_search_console"
        o["evidence_id"] = ev
    sid = _record(ctx, cid, GSC, f"{SC} performance {win}", [r for r in rows if r["query"] or r["page"]], {**totals, "window": win, "property": res.property})
    ctx.source("google_search_console", res.property)
    if not totals["impressions"]:
        ctx.limit("no impressions recorded in this window (a new, unverified-for-long or very low-traffic property looks like this)")
    ctx.limit(f"{SC}'s average position is not comparable with a single browser's observation")
    ctx.outcome.confidence = "high"
    return {"property": res.property, "permission": res.permission, "window": {"start": start, "end": end, "days": days}, "totals": totals, "rows": rows[:100], "rows_total": len(rows),
            "keywords": kw_out, "observations": obs, "evidence_id": ev, "research_session_id": sid}


@capability("gsc.sitemaps", "List the sitemaps GSC knows for ONE client's mapped property: when each was last read, errors/warnings and submitted vs indexed counts.",
            output={"property": "string", "sitemaps": "[{path, last_downloaded, errors, warnings, contents[{type, submitted, indexed}]}]"}, permissions=("connector:read", "client:read"), scope="client",
            category="seo", timeout_s=90)
async def gsc_sitemaps(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    try:
        res = await ctx.svc.connectors.resolve(cid, GSC)
        sm = await ctx.svc.connectors.client(GSC).sitemaps(res.property)
    except ConnectorError as e:
        raise _err(e) from None
    ev = ctx.evidence("gsc_sitemaps", {"property": res.property, "sitemaps": sm}, source_url=res.property, summary=f"{len(sm)} sitemap(s)")
    ctx.source("google_search_console", res.property)
    return {"property": res.property, "sitemaps": sm, "evidence_id": ev}


@capability("gsc.inspect_url", "Ask Google's URL Inspection what it knows about up to 10 URLs of ONE client's mapped property: index status, coverage state, last crawl, Google's chosen canonical. "
            "URLs outside the mapped property are refused.",
            props={"urls": A("URLs to inspect (must be on the client's mapped property).", minItems=1, maxItems=10)}, required=("urls",),
            output={"property": "string", "results": "[{url, verdict, coverage_state, last_crawl_time, google_canonical, user_canonical}]"},
            permissions=("connector:read", "client:read"), scope="client", category="seo", timeout_s=180, max_timeout_s=300,
            limitations=("GSC's URL Inspection API has a daily quota per property.",))
async def gsc_inspect(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    hub = ctx.svc.connectors
    try:
        res = await hub.resolve(cid, GSC)
    except ConnectorError as e:
        raise _err(e) from None
    dom = domain_of(res.property)
    out, errors = [], []
    for u in p["urls"]:
        ctx.check_cancel()
        if domain_of(u) != dom and not domain_of(u).endswith("." + dom):
            errors.append({"url": u, "error": "URL_NOT_IN_PROPERTY"})
            continue
        try:
            out.append(await hub.client(GSC).inspect(res.property, u))
        except ConnectorError as e:
            errors.append({"url": u, "error": e.code})
            if e.code in ("RATE_LIMITED", "AUTH_FAILED", "API_DISABLED", "UNAUTHORIZED_PROPERTY"):
                break
    if errors and not out:
        raise CollabError(errors[0]["error"], f"no URL could be inspected: {errors[0]['error']}", {"errors": errors}, retryable=errors[0]["error"] == "RATE_LIMITED")
    ev = ctx.evidence("gsc_url_inspection", {"property": res.property, "results": out, "errors": errors}, source_url=res.property, summary=f"{len(out)} URL(s) inspected")
    ctx.source("google_search_console", res.property)
    if errors:
        ctx.outcome.completeness = "partial"
        ctx.limit(f"{len(errors)} URL(s) were not inspected: " + ", ".join(sorted({e['error'] for e in errors})))
    return {"property": res.property, "results": out, "errors": errors, "evidence_id": ev}


# =================================================================================================================  Bing Webmaster Tools
def _weighted_pos(rows: list[dict], key: str) -> float | None:
    imp = sum(r["impressions"] for r in rows if r.get(key) not in (None, -1))
    if not imp:
        return None
    return round(sum(r[key] * r["impressions"] for r in rows if r.get(key) not in (None, -1)) / imp, 2)


@capability("bing.webmaster_stats", "Read ONE client's Bing Webmaster Tools data from the property the owner mapped to that client: clicks and impressions over a window, the top queries with their "
            "average positions, and the figures for specific keywords.",
            props={"days": I("Window length (default 28).", minimum=1, maximum=180), "keywords": A("Queries to report figures for (exact match, case-insensitive).", maxItems=30)},
            output={"property": "string", "window": "{start, end, days}", "totals": "{clicks, impressions, avg_impression_position}", "top_queries": "[...]", "keywords": "[...]",
                    "observations": "[standard observations for loops]"},
            permissions=("connector:read", "client:read", "research:write"), scope="client", read_only=False, category="seo", timeout_s=120, max_timeout_s=300,
            limitations=("Bing reports its own rolling windows: figures are filtered to the requested days from the dated rows Bing returns.", "Bing omits rare queries."))
async def bing_webmaster_stats(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    hub = ctx.svc.connectors
    days = int(p.get("days", 28))
    try:
        res = await hub.resolve(cid, BING)
        cli = hub.client(BING)
        qs = await cli.query_stats(res.property)
        tr = await cli.traffic_stats(res.property)
    except ConnectorError as e:
        raise _err(e) from None
    end = date.today()
    start = end - timedelta(days=days - 1)
    inwin = lambda r: r.get("date") and start.isoformat() <= r["date"] <= end.isoformat()
    qrows, trows = [r for r in qs if inwin(r)], [r for r in tr if inwin(r)]
    agg: dict[str, dict] = {}
    for r in qrows:
        a = agg.setdefault(_norm(r["query"] or ""), {"query": r["query"], "clicks": 0, "impressions": 0, "rows": []})
        a["clicks"] += r["clicks"]
        a["impressions"] += r["impressions"]
        a["rows"].append(r)
    top = sorted(({"query": a["query"], "clicks": a["clicks"], "impressions": a["impressions"], "avg_impression_position": _weighted_pos(a["rows"], "avg_impression_position")} for a in agg.values()),
                 key=lambda x: (-x["impressions"], -x["clicks"]))
    totals = {"clicks": sum(r["clicks"] for r in trows), "impressions": sum(r["impressions"] for r in trows), "avg_impression_position": _weighted_pos(qrows, "avg_impression_position")}
    ev = ctx.evidence("bing_webmaster_stats", {"property": res.property, "window": {"start": start.isoformat(), "end": end.isoformat()}, "totals": totals, "queries": top[:200], "traffic_rows": len(trows)},
                      source_url=res.property, summary=f"Bing {start}..{end}: {totals['clicks']} clicks, {totals['impressions']} impressions")
    cmpk = f"bing|{days}d|{res.property}"
    lim = [f"Bing Webmaster {start}..{end}"]
    obs = [_obs("bing|site|clicks", "Bing: clicks", "clicks", float(totals["clicks"]), "found", cmpk + "|site", ctx.request_id, lim),
           _obs("bing|site|impressions", "Bing: impressions", "impressions", float(totals["impressions"]), "found", cmpk + "|site", ctx.request_id, lim)]
    kw_out = []
    for k in [_norm(x) for x in (p.get("keywords") or [])]:
        a = agg.get(k)
        label = next((x for x in p.get("keywords") if _norm(x) == k), k)
        pos = _weighted_pos(a["rows"], "avg_impression_position") if a else None
        if a and pos is not None:
            kw_out.append({"keyword": label, "found": True, "clicks": a["clicks"], "impressions": a["impressions"], "avg_impression_position": pos})
            obs.append(_obs(f"bing|query|{k}|position", f"Bing position: {label}", "avg_position", pos, "found", cmpk + "|query", ctx.request_id, lim))
        else:
            kw_out.append({"keyword": label, "found": False})
            obs.append(_obs(f"bing|query|{k}|position", f"Bing position: {label}", "avg_position", None, "unavailable", cmpk + "|query", ctx.request_id,
                            ["no row for this query in the window: no impressions or too rare to be reported (not proof of absence)"]))
    for o in obs:
        o["source"] = "bing_webmaster_tools"
        o["evidence_id"] = ev
    sid = _record(ctx, cid, BING, f"Bing Webmaster performance {start}..{end}", [{"query": t["query"], **t} for t in top[:60]], {**totals, "window": f"{start}..{end}", "property": res.property})
    ctx.source("bing_webmaster_tools", res.property)
    if not trows and not qrows:
        ctx.limit("Bing returned no rows inside this window")
    return {"property": res.property, "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days}, "totals": totals, "top_queries": top[:50], "keywords": kw_out,
            "observations": obs, "evidence_id": ev, "research_session_id": sid}
