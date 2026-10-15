"""Competitor research as a reusable capability.

`research.competitors` reads competitor pages (optionally a bounded read of each whole site) and returns structured profiles: what the business appears to be, which services and places it
names, how its pages are structured, its schema/contact/conversion signals, and how that compares with the client's own page. It works for any list of URLs or domains, for a client or
without one, and is the same reader `seo.competitors` (SERP-derived competitors) uses.

Reading rules: plain HTTP first; if the site serves a bot-protection page to scripts or cannot be reached by the agent's HTTP client, the page is read in the owner's real Chrome (own tab, read
only, nothing clicked or solved). A browser read cannot see <head> metadata/JSON-LD, so those fields are reported as NOT MEASURED, never guessed. Competitor wording is never stored or returned
beyond titles, headings and counts. A competitor's characteristic is a correlation, not proof of why it ranks.
"""
from __future__ import annotations

import json
import re
from statistics import median
from urllib.parse import urlsplit

from ...seo import fetch as F
from ...seo.signals import extract, host_key
from ..capabilities import A, B, I, S, capability
from ..store import CollabError
from .core import client_context, flat
from .web import HTTP_LIMITS, run_crawl

NOT_MEASURED_IN_BROWSER = ["meta description", "JSON-LD / schema", "image count and alt text", "map embed", "HTTP status/redirects"]
PAGE_TYPES = (("contact", r"contact|get-in-touch|quote|appointment|book"), ("about", r"about|our-story|who-we-are|team"), ("blog", r"blog|news|article|post|guide|tips"),
              ("faq", r"faq|questions"), ("reviews", r"review|testimonial"), ("pricing", r"pricing|price|cost|rates"))


def _browser_sig(r: dict, url: str) -> dict:
    text = r.get("text", "")
    hrefs = r.get("hrefs", [])
    site = host_key(url)
    internal = {h.split("#")[0] for h in hrefs if h.startswith("http") and host_key(h) == site}
    heads = r.get("headings") or []
    return {"url": r["final_url"], "title": r.get("title", ""), "title_length": len(r.get("title", "")), "meta_description": None, "headings": heads, "h1": [h["text"] for h in heads if h.get("level") == 1],
            "word_count": len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", text)), "jsonld_types": None, "internal_link_count": len(internal), "tel_links": sorted({h[4:] for h in hrefs if h.lower().startswith("tel:")}),
            "images": None, "map_embed": None, "text": text[:20000], "links_internal": [{"url": u, "text": ""} for u in sorted(internal)[:300]]}


async def read_page(ctx, url: str, mode: str = "auto") -> dict:
    """{ok, via, final_url, status, sig, error, measured_not}: one public page, HTTP first, real browser when the site refuses scripts."""
    fail = None
    if mode != "browser":
        try:
            r = await F.fetch(url)
        except F.FetchError as ex:
            fail = ex.code
            if mode == "http" or ex.code not in ("FETCH_FAILED", "FETCH_TIMEOUT", "DNS_FAILED"):
                return {"ok": False, "via": "http", "error": ex.code}
        else:
            if r.is_html and r.status == 200 and not r.challenge:
                return {"ok": True, "via": "http", "final_url": r.final_url, "status": 200, "sig": extract(r.text(), r.final_url), "not_measured": []}
            if not (r.challenge or r.status in (403, 429, 503)):
                return {"ok": False, "via": "http", "status": r.status, "error": "NOT_HTML_OR_NOT_200"}
            fail = "BOT_CHALLENGE" if r.challenge else f"HTTP_{r.status}"
            if mode == "http":
                return {"ok": False, "via": "http", "status": r.status, "error": fail}
    if ctx.rt.session is None:
        return {"ok": False, "via": "browser", "error": "EXTENSION_NOT_CONNECTED", "http_error": fail}
    from .browser_crawl import _read
    sp = urlsplit(url if "://" in url else "https://" + url)
    try:
        async with ctx.svc._browser_lock:
            tab = await ctx.open_tab(f"{sp.scheme}://{sp.netloc}/")
            rd = await _read(ctx, tab, url if "://" in url else "https://" + url)
            await ctx.close_tabs()
    except CollabError as e:
        return {"ok": False, "via": "browser", "error": e.code, "http_error": fail}
    if not rd or rd.get("error"):
        return {"ok": False, "via": "browser", "error": (rd or {}).get("error", "READ_FAILED"), "http_error": fail}
    if rd.get("challenge"):
        return {"ok": False, "via": "browser", "error": "BOT_CHALLENGE_IN_BROWSER", "http_error": fail}
    return {"ok": True, "via": "browser", "final_url": rd["final_url"], "status": 200, "sig": _browser_sig(rd, url), "not_measured": NOT_MEASURED_IN_BROWSER, "http_error": fail}


def mentions(sig: dict, svc_terms: list[str], loc_terms: list[str]) -> dict:
    text = (sig.get("text") or "").lower()
    return {"services": [s for s in svc_terms if s and s in text], "locations": [l for l in loc_terms if l and l in text]}


def page_row(pr: dict, base: dict, svc_terms: list[str], loc_terms: list[str]) -> dict:
    """The flat per-page record shared by seo.competitors and research.competitors."""
    sig = pr["sig"]
    return {**base, "final_url": pr["final_url"], "status": pr["status"], "via": pr["via"], "title": sig["title"], "meta_description": sig.get("meta_description"), "h1": sig["h1"],
            "h2_sample": [h["text"] for h in sig["headings"] if h.get("level") == 2][:12], "word_count": sig["word_count"], "schema": sig.get("jsonld_types"),
            "internal_links": sig["internal_link_count"], "tel_links": len(sig["tel_links"]), "map_embed": sig.get("map_embed"), "images": sig.get("images"),
            "mentions": mentions(sig, svc_terms, loc_terms), "not_measured": pr.get("not_measured") or []}


def _page_type(url: str, title: str, svc_terms: list[str], loc_terms: list[str]) -> str:
    path = (urlsplit(url).path or "/").lower()
    if path in ("", "/"):
        return "home"
    blob = path + " " + (title or "").lower()
    for name, rx in PAGE_TYPES:
        if re.search(rx, path):
            return name
    if any(l and l in blob for l in loc_terms):
        return "location"
    if any(s and s in blob for s in svc_terms):
        return "service"
    return "other"


async def site_profile(ctx, url: str, *, max_pages: int, mode: str, svc_terms: list[str], loc_terms: list[str]) -> dict:
    """A bounded read of one competitor's site: what kind of business it looks like and how its pages are organised."""
    res = {"blocked": {"reason": "BROWSER_MODE_REQUESTED"}, "pages": []} if mode == "browser" else await run_crawl(ctx, url, max_pages, True, None)
    res.pop("_full", None)
    via = "http"
    if res.get("blocked"):
        if mode == "http":
            return {"ok": False, "error": "BOT_CHALLENGE", "via": "http"}
        if ctx.rt.session is None:
            return {"ok": False, "error": "EXTENSION_NOT_CONNECTED", "via": "browser"}
        from .browser_crawl import browser_crawl
        async with ctx.svc._browser_lock:
            res = await browser_crawl(ctx, url, max_pages, None)
        res.pop("_full", None)
        via = "browser"
        if res.get("blocked"):
            return {"ok": False, "error": "BOT_CHALLENGE_IN_BROWSER", "via": "browser"}
    pages = res.get("pages") or []
    if not pages:
        return {"ok": False, "error": "NO_PAGES_READ", "via": via}
    types: dict[str, int] = {}
    topics: dict[str, int] = {}
    svc_hit: dict[str, int] = {}
    loc_hit: dict[str, int] = {}
    schema: set[str] = set()
    tel = 0
    for pg in pages:
        sg = pg.get("signals") or {}
        title = sg.get("title") or ""
        t = _page_type(pg.get("final_url") or pg.get("url") or "", title, svc_terms, loc_terms)
        types[t] = types.get(t, 0) + 1
        blob = (title + " " + " ".join(sg.get("h1") or [])).lower()
        for s in svc_terms:
            if s and s in blob:
                svc_hit[s] = svc_hit.get(s, 0) + 1
        for l in loc_terms:
            if l and l in blob:
                loc_hit[l] = loc_hit.get(l, 0) + 1
        for h in (sg.get("h1") or [])[:1]:
            topics[h[:80]] = topics.get(h[:80], 0) + 1
        schema.update(sg.get("jsonld_types") or [])
        tel += 1 if sg.get("tel_links") else 0
    home = pages[0].get("signals") or {}
    ev = ctx.evidence("competitor_site", {"url": url, "via": via, "pages": [{"url": p.get("final_url"), "title": (p.get("signals") or {}).get("title"), "h1": (p.get("signals") or {}).get("h1"),
                                                                           "words": (p.get("signals") or {}).get("word_count")} for p in pages]}, source_url=url, summary=f"{host_key(url)} {len(pages)} pages")
    return {"ok": True, "via": via, "pages_read": len(pages), "page_types": types, "service_terms_in_titles": svc_hit, "location_terms_in_titles": loc_hit, "schema_types": sorted(schema) if via == "http" else None,
            "pages_with_phone_link": tel, "homepage_internal_links": home.get("internal_link_count"), "sitemap_urls": ((res.get("sitemap") or {}).get("url_count")),
            "page_h1_samples": list(topics)[:15], "evidence_id": ev, "sampled": f"a bounded sample of at most {max_pages} page(s), not the whole site"}


def _target(item: str) -> tuple[str, str]:
    u = item.strip()
    if "://" not in u:
        u = "https://" + u
    return u, host_key(u)


def default_targets(serp_rows: list[dict], limit: int = 8) -> list[str]:
    """The competitors to profile when no URLs are given: the ones that appear in the MOST recorded searches, then by best position (never just storage order)."""
    seen: dict[str, dict] = {}
    for o in serp_rows:
        if o["kind"] == "serp_result" and json.loads(o["data"] or "{}").get("class") == "competitor":
            e = seen.setdefault(o["domain"], {"url": o["url"], "queries": set(), "best": 99})
            e["queries"].add(o["query"].lower())
            e["best"] = min(e["best"], o["position"] or 99)
    ranked = sorted(seen.values(), key=lambda e: (-len(e["queries"]), e["best"], e["url"]))
    return [e["url"] for e in ranked[:limit]]


@capability("research.competitors", "Profile competitor websites: read each given page (and optionally a bounded sample of the whole site) and return what the business appears to be, the services and places it "
            "names, page structure, schema/phone/conversion signals and how that compares with the client's own page. Reads over HTTP, or in the owner's real Chrome when the site refuses scripts. "
            "Works for any list of URLs/domains, with or without a client. Does not copy competitor wording.",
            props={"urls": A("Competitor pages or domains to read (default: competitors seen in the client's latest valid search results).", maxItems=12), "depth": S("page (default) | site (also a bounded read of the whole site)",
                   enum=["page", "site"]), "max_pages_per_site": I("Pages to read per site when depth=site (default 6).", minimum=2, maximum=15), "mode": S("auto (default) | http | browser", enum=["auto", "http", "browser"]),
                   "compare_with_client": B("Also read the client's own website and compare (default true when a client is given).")},
            output={"profiles": "[{domain, url, via, title, h1, h2_sample, word_count, schema, mentions, not_measured, site: {...}}]", "client_page": "{...}", "comparison": "[evidence-backed observations]",
                    "unreadable": "[{domain, error}]"},
            permissions=("net:fetch", "research:write"), scope="client_optional", read_only=False, category="seo", timeout_s=600, max_timeout_s=1500,
            limitations=HTTP_LIMITS + ("Correlation only: what a competitor does is not proof of why it ranks.", "A browser read cannot see meta descriptions or JSON-LD: those are reported as not measured.",
                                        "A site read is a bounded sample of pages."))
async def research_competitors(ctx, p: dict) -> dict:
    cid = ctx.client_id
    info = flat(client_context(ctx.admin(), cid)) if cid else {"services": [], "target_locations": [], "website": "", "name": ""}
    svc_terms = [s.lower() for s in info["services"]][:20]
    loc_terms = [l.split(",")[0].lower() for l in info["target_locations"]][:20]
    items = list(p.get("urls") or [])
    if not items:
        if not cid:
            raise CollabError("NO_TARGETS", "give urls (competitor pages or domains), or a client whose search results have been observed")
        from ...business.store import StoreError
        from .seo import _camps, merged_serp, valid_serp_sessions
        try:
            camp = _camps(ctx).find_for_client(cid, None)
        except StoreError as e:
            raise CollabError(e.code, e.message) from None
        sids = valid_serp_sessions(ctx, camp)
        if not sids:
            raise CollabError("NO_SERP_DATA", "no valid search observations for this client yet, and no urls were given")
        items = default_targets(merged_serp(ctx, cid, sids))
    client_domains = {host_key(info["website"])} if info.get("website") else set()
    targets, seen_hosts = [], set()
    for it in items:
        u, h = _target(it)
        if h in client_domains:
            raise CollabError("NOT_A_COMPETITOR", f"{h} is the client's own site")
        if h not in seen_hosts:
            seen_hosts.add(h)
            targets.append((u, h))
    mode, depth = p.get("mode", "auto"), p.get("depth", "page")
    profiles, unreadable = [], []
    for u, h in targets:
        ctx.check_cancel()
        ctx.progress(f"reading {h}")
        pr = await read_page(ctx, u, mode)
        if not pr["ok"]:
            unreadable.append({"domain": h, "url": u, "error": pr["error"], "tried": pr["via"]})
            continue
        row = page_row(pr, {"domain": h, "url": u}, svc_terms, loc_terms)
        row["evidence_id"] = ctx.evidence("competitor_page", {k: v for k, v in pr["sig"].items() if k != "text"} | {"via": pr["via"]}, source_url=pr["final_url"], summary=f"{h} {pr['sig']['title'][:60]!r}")
        if depth == "site":
            row["site"] = await site_profile(ctx, pr["final_url"], max_pages=p.get("max_pages_per_site", 6), mode=mode, svc_terms=svc_terms, loc_terms=loc_terms)
        profiles.append(row)
    client_page = None
    if cid and info.get("website") and p.get("compare_with_client", True):
        cp = await read_page(ctx, info["website"], mode)
        if cp["ok"]:
            client_page = page_row(cp, {"domain": host_key(info["website"]), "url": info["website"]}, svc_terms, loc_terms)
    comparison = _compare(profiles, client_page, loc_terms)
    if unreadable:
        ctx.outcome.completeness = "partial" if profiles else "none"
        ctx.limit(f"{len(unreadable)} of {len(targets)} site(s) could not be read ({', '.join(sorted({u['error'] for u in unreadable}))}): " + ", ".join(u["domain"] for u in unreadable[:8]))
    if any(r["via"] == "browser" for r in profiles):
        ctx.limit("some pages were read in the real browser: " + ", ".join(NOT_MEASURED_IN_BROWSER) + " were not measured for them")
    if 0 < len(profiles) < 3:
        ctx.outcome.confidence = "low"
        ctx.limit(f"only {len(profiles)} competitor(s) were read: comparisons are anecdotal")
    elif profiles:
        ctx.outcome.confidence = "medium"
    for h in (r["domain"] for r in profiles):
        ctx.source("competitor_site", h)
    body = {"profiles": profiles, "client_page": client_page, "comparison": comparison, "unreadable": unreadable, "depth": depth, "mode": mode,
            "caveat": "Correlations from a small sample; not proof of ranking causes. Do not copy competitor wording."}
    art = ctx.svc.store.add_artifact(task_id=ctx.task_id, client_id=cid, kind="analysis", stage="research", title=f"Competitor profiles ({len(profiles)} sites, depth={depth})", body={**body, "competitors": profiles},
                                     links={"request_id": ctx.request_id}, created_by=f"agent:{ctx.caller}")
    if cid and profiles:
        rlog = ctx.admin().research
        sess = rlog.start_session(cid, "Competitor profiles (research.competitors)", actor=f"collab:{ctx.caller}")
        for r in profiles:
            rlog.add_observation(sess["session_id"], "competitor", url=r["final_url"], domain=r["domain"], business=(r["title"] or "")[:200], evidence=f"collab evidence {r['evidence_id']}", source=r["via"],
                                 data={"word_count": r["word_count"], "schema": r["schema"], "internal_links": r["internal_links"], "request_id": ctx.request_id}, actor=f"collab:{ctx.caller}")
        rlog.finish_session(sess["session_id"], findings=f"{len(profiles)} competitor site(s) profiled", actor=f"collab:{ctx.caller}")
    return {"artifact_id": art["artifact_id"], "profiles": profiles, "client_page": client_page, "comparison": comparison, "unreadable": unreadable}


def _compare(profiles: list[dict], client: dict | None, loc_terms: list[str]) -> list[dict]:
    out = []
    if not profiles:
        return out
    n = len(profiles)
    words = [r["word_count"] for r in profiles]
    loc_title = [r for r in profiles if any(l and l in (r["title"] or "").lower() for l in loc_terms)]
    if loc_terms:
        out.append({"observation": f"{len(loc_title)} of {n} competitor pages name a target location in their title", "evidence": [r["evidence_id"] for r in loc_title]})
    else:   # counting against an empty list would report a misleading "0 of n"
        out.append({"observation": "location names in competitor titles were not counted: the client has no target locations on file", "evidence": [], "kind": "not_measured"})
    measured = [r for r in profiles if r["schema"] is not None]
    if measured:
        ls = [r for r in measured if any(t in ("LocalBusiness", "Organization", "Store", "AutoRepair", "Plumber", "Locksmith") or t.endswith("Business") for t in r["schema"])]
        out.append({"observation": f"{len(ls)} of {len(measured)} measurable competitor pages carry LocalBusiness/Organization-type JSON-LD", "evidence": [r["evidence_id"] for r in ls]})
    out.append({"observation": f"competitor pages carry a median of {round(median(words))} words (range {min(words)}-{max(words)})", "evidence": [r["evidence_id"] for r in profiles]})
    with_phone = [r for r in profiles if r["tel_links"]]
    out.append({"observation": f"{len(with_phone)} of {n} competitor pages have a click-to-call phone link", "evidence": [r["evidence_id"] for r in with_phone]})
    if client:
        gaps = []
        if client["word_count"] < median(words) * 0.6:
            gaps.append(f"the client's page has {client['word_count']} words against a competitor median of {round(median(words))}")
        if loc_title and not any(l and l in (client["title"] or "").lower() for l in loc_terms):
            gaps.append("most competitors name a location in the title and the client's page title does not")
        if with_phone and not client["tel_links"]:
            gaps.append("competitors offer click-to-call and the client's page has no phone link")
        out.append({"observation": "client page vs competitors: " + ("; ".join(gaps) if gaps else "no clear gap on these measures") + ("" if loc_terms else " (location in title not compared: no target locations on file)"),
                    "evidence": [], "kind": "gap" if gaps else "no_gap"})
    return out
