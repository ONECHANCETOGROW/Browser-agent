"""Read-only work in the agent's real browser: rendered pages and search-results pages.

Each job opens its OWN background tab, reads, and closes it; the owner's tabs are never touched. No clicking, typing or form submission is possible
through these capabilities. A CAPTCHA or consent wall is reported as a block, never solved or accepted.
"""
from __future__ import annotations

import time

from ...seo import serp as SERP
from ..capabilities import A, B, I, S, capability
from ..store import CollabError

BROWSER_LIMITS = ("Needs the Chrome extension connected to the agent.", "Runs in the owner's browser profile: results can be personalised by sign-in, history and location.")


def _ok(resp: dict, what: str) -> dict:
    if not resp.get("success"):
        err = resp.get("error") or {}
        code = err.get("code") or "BROWSER_ERROR"
        raise CollabError(code, f"{what}: {err.get('message') or code}", retryable=code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED", "ACTION_TIMEOUT", "NAVIGATION_FAILED"))
    return resp.get("result") or {}


def _need_browser(ctx) -> None:
    if ctx.rt.session is None:
        raise CollabError("EXTENSION_NOT_CONNECTED", "the Chrome extension is not connected to the agent (open the side panel and press Connect)", retryable=True)


@capability("browser.inspect_page", "Open a URL in the agent's own background tab, let it render, and return the rendered title, headings, visible text excerpt, "
            "optional find-in-page results for given phrases and an optional screenshot. The tab is closed afterwards.",
            props={"url": S("http(s) URL to open.", minLength=1, maxLength=2000), "find": A("Phrases to look for on the rendered page.", maxItems=20),
                   "text_chars": I("How much page text to return (default 4000).", minimum=200, maximum=30000), "screenshot": B("Also capture a screenshot (saved as evidence).")},
            required=("url",), output={"url": "final URL", "title": "", "headings": "[{level,text}]", "text": "excerpt", "find": "{phrase: {found, total_matches, contexts[]}}",
                                       "screenshot_evidence": "evidence id"},
            permissions=("browser:read",), category="browser", needs_browser=True, timeout_s=90, max_timeout_s=240, limitations=BROWSER_LIMITS)
async def inspect_page(ctx, p: dict) -> dict:
    _need_browser(ctx)
    url = p["url"] if "://" in p["url"] else "https://" + p["url"]
    if not url.lower().startswith(("http://", "https://")):
        raise CollabError("BAD_URL", "only http(s) pages can be opened")
    tab = await ctx.open_tab(url)
    ctx.check_cancel()
    st = _ok(await ctx.browser("get_page_state", {"tab_id": tab}), "reading the page state")
    rd = _ok(await ctx.browser("read_page", {"tab_id": tab, "scope": "page", "max_chars": p.get("text_chars", 4000)}), "reading the page text")
    page = st.get("page") or st
    out = {"url": rd.get("url") or page.get("url"), "title": rd.get("title") or page.get("title"), "headings": page.get("headings") or [], "text": rd.get("text", ""),
           "total_chars": rd.get("total_chars"), "find": {}}
    for phrase in (p.get("find") or []):
        ctx.check_cancel()
        f = await ctx.browser("find_text", {"tab_id": tab, "text": phrase, "scroll": False, "max_matches": 5, "context_chars": 80})
        if f.get("success"):
            r = f.get("result") or {}
            out["find"][phrase] = {"found": bool(r.get("found")), "total_matches": r.get("total_matches", 0),
                                   "contexts": [m.get("context") for m in (r.get("matches") or [])][:5]}
        else:
            out["find"][phrase] = {"error": (f.get("error") or {}).get("code")}
    ctx.evidence("rendered_page", {"url": out["url"], "title": out["title"], "headings": out["headings"], "text": out["text"], "find": out["find"]}, source_url=out["url"] or url,
                 summary=f"rendered {out['title']!r}")
    if p.get("screenshot"):
        sh = await ctx.browser("screenshot", {"tab_id": tab, "format": "png"})
        if sh.get("success") and (sh.get("result") or {}).get("path"):
            from pathlib import Path
            raw = Path(sh["result"]["path"]).read_bytes()
            out["screenshot_evidence"] = ctx.evidence("screenshot", raw, source_url=out["url"] or url, summary="viewport screenshot", ext="png")
        else:
            ctx.limit("the screenshot could not be captured")
    ctx.source("browser", out["url"] or url)
    return out


async def run_search(ctx, engine: str, query: str, *, country: str = "us", language: str = "en", location: str = "", max_results: int = 10, screenshot: bool = False) -> dict:
    _need_browser(ctx)
    url, notes = SERP.build_url(engine, query, country=country, language=language, location=location)
    tab = await ctx.open_tab(url)
    await ctx.browser("wait", {"tab_id": tab, "ms": 1200})
    st = _ok(await ctx.browser("get_page_state", {"tab_id": tab}), "reading the results page")
    page = st.get("page") or st
    rd = _ok(await ctx.browser("read_page", {"tab_id": tab, "scope": "page", "max_chars": 12000}), "reading the results text")
    ins = _ok(await ctx.browser("inspect_page", {"tab_id": tab, "max_elements": 600, "viewport_only": False}), "listing result links")
    observed_at = time.time()
    parsed = SERP.parse(engine, ins.get("elements") or [], page.get("headings") or [], rd.get("text", ""), rd.get("url") or url, max_results=max_results)
    ev = ctx.evidence("serp", {"engine": engine, "query": query, "location": location, "country": country, "language": language, "request_url": url, "page_url": rd.get("url"),
                               "observed_at": observed_at, "headings": page.get("headings"), "links": [{k: e.get(k) for k in ("name", "href")} for e in (ins.get("elements") or []) if e.get("href")][:400],
                               "text": rd.get("text", ""), "parsed": parsed}, source_url=rd.get("url") or url, summary=f"{engine} '{query}'")
    shot = None
    if screenshot and not parsed["blocked"]:
        sh = await ctx.browser("screenshot", {"tab_id": tab, "format": "jpeg", "quality": 70})
        if sh.get("success") and (sh.get("result") or {}).get("path"):
            from pathlib import Path
            shot = ctx.evidence("screenshot", Path(sh["result"]["path"]).read_bytes(), source_url=url, summary=f"{engine} '{query}'", ext="jpg")
    await ctx.close_tabs()
    for n in notes:
        ctx.limit(n)
    ctx.source(engine, url, observed_at)
    return {"engine": engine, "query": query, "location": location, "request_url": url, "observed_at": observed_at, "evidence_id": ev, "screenshot_evidence": shot, **parsed}


@capability("search.web", "Run ONE web search (Google or Bing) in the agent's own browser tab and return the organic results in the order shown, whether a local/map pack "
            "appeared, and whether ads were present. CAPTCHA/consent walls are reported as blocked.",
            props={"query": S("Search query.", minLength=1, maxLength=300), "engine": S("google (default) or bing.", enum=["google", "bing"]),
                   "country": S("Two-letter country (default us).", maxLength=2), "language": S("Language code (default en).", maxLength=5),
                   "location": S("Canonical place to emulate where supported, e.g. 'Portland,Oregon,United States'.", maxLength=120),
                   "max_results": I("Organic results to return (default 10).", minimum=1, maximum=20), "screenshot": B("Save a screenshot as evidence.")},
            required=("query",), output={"organic": "[{position, title, url, domain}]", "local_pack": "{detected, names[]}", "ads_detected": "bool", "blocked": "null | CAPTCHA | CONSENT_REQUIRED",
                                         "method": "heading-match | fallback-external-links"},
            permissions=("search:read",), category="search", needs_browser=True, timeout_s=90, max_timeout_s=180,
            limitations=BROWSER_LIMITS + ("A position is what this browser was shown at that moment, not a universal ranking.",
                                          "Map/local-pack membership is detected from map links only; local-pack ORDER is not measured."))
async def search_web(ctx, p: dict) -> dict:
    res = await run_search(ctx, p.get("engine", "google"), p["query"], country=p.get("country", "us"), language=p.get("language", "en"), location=p.get("location", ""),
                           max_results=p.get("max_results", 10), screenshot=p.get("screenshot", False))
    if res["blocked"]:
        raise CollabError("SEARCH_BLOCKED", f"the search engine showed a {res['blocked']} page; it was not bypassed. Try later, use the other engine, or ask the owner to clear it.",
                          {"evidence_id": res["evidence_id"]}, retryable=True)
    if res["method"] != "heading-match":
        ctx.outcome.confidence = "low"
        ctx.limit("results were identified by the fallback method (external links), so positions are approximate")
    else:
        ctx.outcome.confidence = "medium"
    return res
