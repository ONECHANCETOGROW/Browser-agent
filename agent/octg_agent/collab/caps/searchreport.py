"""search.report: run a handful of Google searches for ONE client and answer in plain language (what was searched, whether the client's site is on the first page, who is, the map listings).

This is what the Hub's "Ask" page runs. It reuses the rank-check reading code (a real tab, the page read as-is, a CAPTCHA/consent wall stops the run and is never bypassed, 20-45 s between searches)
but needs no keyword file: the searches come from a sentence. Nothing is clicked, typed or changed anywhere.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from ...seo import serp as SERP
from ..capabilities import A, S, capability
from ..store import CollabError
from . import rank
from .browser import BROWSER_LIMITS, _need_browser
from .core import client_context, flat, require_client

MAX_QUERIES = 10
_PACK_END = re.compile(r"\n(?:Web results|People also ask|Videos|Images|Short videos|Related searches)\b")


def pack_text(text: str) -> str:
    """The part of a results page that sits above the web results: the map-pack block. (Ratings further down belong to web results, not to businesses.)"""
    t = "\n" + (text or "")
    m = _PACK_END.search(t)
    return (t[:m.start()] if m else t[:3000]).strip()


def _short(s: str, n: int = 60) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _biz(b: dict) -> str:
    bits = []
    if b.get("rating") is not None:
        bits.append(f"{b['rating']:.1f}")
    if b.get("review_count") is not None:
        bits.append(f"{b['review_count']} reviews")
    return b["name"] + (f" ({', '.join(bits)})" if bits else "")


def describe(i: int, r: dict, *, site: str, brand: str) -> list[str]:
    """The plain-language block for one search."""
    L = [f"{i}. “{r['query']}”"]
    if r["status"] == "not_run":
        return L + ["   Not run: the run stopped before this search."]
    if r["status"] == "blocked":
        return L + ["   Google asked for a human check on this search, so I stopped here. I did not try to get around it."]
    if r["status"] == "error":
        return L + [f"   I could not open this search ({r.get('error') or 'browser error'})."]
    if r["status"] != "read":
        return L + ["   I opened the page but could not read the results reliably, so I am not reporting anything for it."]
    n = len(r["results"])
    if not site:
        L.append(f"   Your website: I can't say, because no website is on file for this client (I read {n} results).")
    elif r["client_position"]:
        L.append(f"   Your website: yes, it came up at #{r['client_position']} on the first page ({r['client_url']}).")
    else:
        L.append(f"   Your website: not on the first page (I read {n} web results and it is not among them).")
    if r["results"]:
        L.append("   First results: " + "; ".join(f"{x['position']}) {x['domain']}" + (f" ({x['class']})" if x["class"] != "competitor" else "") + f" — {_short(x['title'], 46)}" for x in r["results"][:5]))
    comps = r["competitors"]
    if comps:
        L.append("   Competitors on the page: " + ", ".join(comps[:8]) + (f" (+{len(comps) - 8} more)" if len(comps) > 8 else "") + ".")
    dirs = r["directories"]
    if dirs:
        L.append("   Directories on the page: " + ", ".join(dirs) + ".")
    if r["map"]:
        L.append("   Map listings: " + "; ".join(f"{b['position']}) {_biz(b)}" for b in r["map"][:5]) + ("." if len(r["map"]) <= 5 else f" (+{len(r['map']) - 5} more)."))
        if r["client_in_map"]:
            L.append(f"   Your business ({brand}) is in the map listings at #{r['client_in_map']}.")
        else:
            L.append(f"   Your business ({brand}) was not among the map listings I could read.")
    else:
        L.append("   Map listings: none were shown (or I could not read them).")
    extra = [x for x in r["features"] if x != "map_pack"]
    if extra:
        L.append("   Also on the page: " + ", ".join(x.replace("_", " ") for x in extra) + ".")
    return L


def compose(rows: list[dict], *, site: str, brand: str, location: str | None, ran_at: float, stopped: str | None, remaining: list[str]) -> str:
    read = [r for r in rows if r["status"] == "read"]
    out = []
    n = len(rows)
    if stopped:
        out.append(f"I ran {len(read)} of {n} searches on Google, then stopped: {stopped}")
    else:
        out.append(f"I searched Google for {n} phrase{'s' if n != 1 else ''} and read the results.")
    if read and site:
        hit = [r for r in read if r["client_position"]]
        if hit:
            out.append(f"Your website ({site}) was on the first page for {len(hit)} of {len(read)}: " + "; ".join(f"“{r['query']}” (#{r['client_position']})" for r in hit) + ".")
        else:
            out.append(f"Your website ({site}) was not on the first page for any of the {len(read)} search{'es' if len(read) != 1 else ''} I could read.")
    elif read:
        out.append("No website is on file for this client, so I cannot say whether it showed up; the results are listed below.")
    counts: dict[str, int] = {}
    for r in read:
        for d in r["competitors"]:
            counts[d] = counts.get(d, 0) + 1
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        out.append("Competitors that showed up most: " + ", ".join(f"{d} ({c} of {len(read)})" for d, c in top) + ".")
    pk = [r for r in read if r["map"]]
    if pk:
        mine = [r for r in pk if r["client_in_map"]]
        out.append(f"Your business was in the map listings for {len(mine)} of the {len(pk)} searches that showed one.")
    out.append("")
    for i, r in enumerate(rows, 1):
        out += describe(i, r, site=site, brand=brand)
        out.append("")
    if stopped and remaining:
        out += ["What to do: open Google in Chrome and complete its check yourself (I never do that), then press “Try again” to run the remaining searches: " + "; ".join(f"“{q}”" for q in remaining) + ".", ""]
    stamp = datetime.fromtimestamp(ran_at).strftime("%d %b %Y, %H:%M")
    out.append(f"This is what Google showed in your Chrome on {stamp}" + (f", asked to answer as if searching from {location}" if location else "") + ". Google can show different results by place, device and sign-in, so treat positions as a snapshot.")
    return "\n".join(out).strip()


@capability("search.report", "Run Google searches for ONE client in the agent's browser (one tab, paced, a CAPTCHA/consent wall stops the run and is never bypassed) and answer in plain language: for each search "
            "whether the client's website is on the first page, the first results, the competitors and directories on the page and the map listings with their ratings and review counts. "
            "This is what the Hub's Ask page runs.",
            props={"queries": A("The phrases to search for (1-10).", minItems=1, maxItems=MAX_QUERIES), "location": S("Optional place to search as, e.g. 'Alabama,United States' (Google's canonical name).", maxLength=120)},
            required=("queries",), output={"report": "the plain-language answer", "status": "complete | partial | blocked", "queries": "[per-search detail]", "remaining": "[searches not run]"},
            permissions=("search:read", "research:write", "client:read", "collab:write"), scope="client", read_only=False, category="seo", needs_browser=True, timeout_s=900, max_timeout_s=1800,
            limitations=BROWSER_LIMITS + ("Positions are what this browser was shown at that moment, not universal rankings.", "Map listings are read from the results page text; a figure that cannot be read is left out."))
async def search_report(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    queries = list(dict.fromkeys(q.strip() for q in p["queries"] if isinstance(q, str) and q.strip()))
    if not queries or any(len(q) > 160 for q in queries):
        raise CollabError("BAD_INPUT", "give 1-10 search phrases of at most 160 characters")
    info = flat(client_context(ctx.admin(), cid))
    site = rank.registrable(info["website"]) if info["website"] else ""
    brand = info["name"]
    loc = (p.get("location") or "").strip()
    code = SERP.uule(loc) if loc else ""
    if loc and not code:
        raise CollabError("BAD_INPUT", "that location name is too long to use")
    _need_browser(ctx)
    rows: list[dict] = []
    waits: list[float] = []
    stopped = None
    for i, q in enumerate(queries):
        row = {"query": q, "status": "not_run", "observed_at": None, "client_position": None, "client_url": None, "results": [], "competitors": [], "directories": [], "map": [], "client_in_map": None,
               "features": [], "evidence": {"page": None, "screenshot": None}, "error": None}
        rows.append(row)
        if stopped:
            continue
        if i:
            await rank._pace(ctx, waits, "before the next search")
        ctx.check_cancel()
        ctx.progress(f"search {i + 1} of {len(queries)}: {q}")
        url, _notes = SERP.build_url("google", q, country="us", language="en", location=loc)
        try:
            pg = await rank._read(ctx, url, code or None)
        except CollabError as e:
            if e.code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED"):
                stopped = "the Chrome extension is not connected."
                row["status"], row["error"] = "error", e.code
                continue
            await ctx.close_tabs()
            row["status"], row["error"] = "error", e.code
            continue
        ev = ctx.evidence("serp", {"requested_url": pg["requested_url"], "final_url": pg["final_url"], "observed_at": pg["observed_at"], "headings": pg["headings"],
                                   "links": [{x: e.get(x) for x in ("name", "href")} for e in pg["elements"] if e.get("href")][:400], "text": pg["text"]}, source_url=pg["final_url"] or url, summary=f"google '{q}'")
        ctx.source("google", url, pg["observed_at"])
        row["observed_at"] = pg["observed_at"]
        row["evidence"] = {"page": ev, "screenshot": pg["screenshot"]}
        if pg["blocked"]:
            row["status"] = "blocked"
            stopped = f"Google showed a {pg['blocked']} page on search {i + 1}."
            continue
        if not pg["location_kept"]:
            row["status"] = "unreadable"
            continue
        parsed = SERP.parse("google", pg["elements"], pg["headings"], pg["text"], pg["final_url"], max_results=rank.TOP_N)
        if parsed["method"] != "heading-match" or not parsed["organic"]:
            row["status"] = "unreadable"
            continue
        row["status"] = "read"
        for o in parsed["organic"][:rank.TOP_N]:
            cls = SERP.classify(rank.registrable(o["domain"]), [site] if site else [])
            row["results"].append({"position": o["position"], "title": o["title"], "domain": rank.registrable(o["domain"]), "url": o["url"], "class": cls})
        row["client_position"] = next((x["position"] for x in row["results"] if x["class"] == "client"), None)
        row["client_url"] = next((x["url"] for x in row["results"] if x["class"] == "client"), None)
        row["competitors"] = list(dict.fromkeys(x["domain"] for x in row["results"] if x["class"] == "competitor"))
        row["directories"] = list(dict.fromkeys(x["domain"] for x in row["results"] if x["class"] == "directory"))
        row["map"] = [{"position": b["position"], "name": b["name"], "rating": b["rating"], "review_count": b["review_count"]} for b in rank.parse_local_finder(pg["headings"], pack_text(pg["text"])) if b["position"]]
        row["client_in_map"] = next((b["position"] for b in row["map"] if SERP.in_pack(brand, [b["name"]])), None)
        row["features"] = rank._features(parsed, pg["text"], pg["headings"])
        if row["map"] and "map_pack" not in row["features"]:
            row["features"].insert(0, "map_pack")
    await ctx.close_tabs()
    ran = [r for r in rows if r["status"] != "not_run"]
    remaining = [r["query"] for r in rows if r["status"] in ("not_run", "blocked")]
    status = "blocked" if any(r["status"] == "blocked" for r in rows) else ("complete" if all(r["status"] == "read" for r in rows) else "partial")
    when = time.time()
    report = compose(rows, site=site, brand=brand, location=loc or None, ran_at=when, stopped=stopped, remaining=remaining)
    if site:
        rlog = ctx.admin().research
        sess = rlog.start_session(cid, "Ask: Google search report", search_engine="google", actor=f"collab:{ctx.caller}")
        for r in rows:
            if r["status"] == "read":
                rlog.add_observation(sess["session_id"], "keyword_rank", query=r["query"], location=loc or "(browser location)", search_engine="google", result_type="organic", position=r["client_position"],
                                     url=r["client_url"] or "", domain=site, business=brand, evidence=f"search report; " + (f"organic #{r['client_position']}" if r["client_position"] else f"not in the {len(r['results'])} organic results read"),
                                     source="browser:google", data={"request_id": ctx.request_id, "competitors": r["competitors"][:10]}, observed_at=r["observed_at"], actor=f"collab:{ctx.caller}")
        rlog.finish_session(sess["session_id"], findings=f"{len([r for r in rows if r['status'] == 'read'])} of {len(rows)} searches read", actor=f"collab:{ctx.caller}")
    if status != "complete":
        ctx.outcome.completeness = "partial" if any(r["status"] == "read" for r in rows) else "none"
    if stopped:
        ctx.limit(stopped)
    ctx.outcome.confidence = "medium"
    return {"report": report, "status": status, "searched": len([r for r in ran if r["status"] == "read"]), "queries": rows, "remaining": remaining, "site": site, "brand": brand, "location": loc or None,
            "ran_at": when, "pacing_waits_s": waits}
