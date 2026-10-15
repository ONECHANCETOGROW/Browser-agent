"""Public-web reading over HTTP from this machine (no browser): one page, robots.txt, sitemaps, a bounded crawl."""
from __future__ import annotations

from urllib.parse import urlsplit

from ...seo import fetch as F
from ...seo.crawl import crawl
from ...seo.signals import brief, extract, parse_robots, parse_sitemap
from ..capabilities import A, B, I, S, capability
from ..store import CollabError

HTTP_LIMITS = ("Raw HTML as served (no JavaScript is run); use browser.inspect_page for rendered content.",
               "Private, loopback and LAN addresses are refused; only ports 80/443/8080/8443.")


def fetch_error(e: F.FetchError) -> CollabError:
    return CollabError(e.code, e.message, retryable=e.retryable)


@capability("web.fetch_page", "Fetch one public URL over HTTP(S) and return its status, redirect chain, key headers and extracted page signals "
            "(title, meta description, robots, canonical, headings, links, images/alt, JSON-LD types, word count). Raw HTML and full signals are saved as evidence.",
            props={"url": S("Public http(s) URL.", minLength=1, maxLength=2000), "include_text": B("Also return the page text (up to 20k chars).")}, required=("url",),
            output={"http": "{requested_url, final_url, status, redirects[], headers{}, elapsed_ms}", "signals": "page signals (brief)", "text": "optional"},
            permissions=("net:fetch",), category="web", timeout_s=40, max_timeout_s=120, limitations=HTTP_LIMITS)
async def fetch_page(ctx, p: dict) -> dict:
    try:
        r = await F.fetch(p["url"])
    except F.FetchError as e:
        raise fetch_error(e) from None
    out = {"http": r.summary(), "signals": None}
    ctx.source("http", r.final_url, r.fetched_at)
    if r.is_html:
        sig = extract(r.text(), r.final_url)
        ctx.evidence("http_html", r.body, source_url=r.final_url, summary=f"HTTP {r.status} {len(r.body)} bytes", ext="html")
        ctx.evidence("page_signals", sig, source_url=r.final_url, summary=f"title={sig['title'][:80]!r}")
        out["signals"] = brief(sig)
        if p.get("include_text"):
            out["text"] = sig["text"]
    else:
        ctx.evidence("http_body", r.body[:200_000], source_url=r.final_url, summary=f"HTTP {r.status} {r.content_type}", ext="txt")
        ctx.limit(f"not an HTML page ({r.content_type or 'unknown type'})")
    if r.truncated:
        ctx.outcome.completeness = "partial"
        ctx.limit("the body was larger than the size cap and was truncated")
    return out


@capability("web.robots", "Fetch and parse a site's robots.txt: rules for all crawlers, sitemap URLs, and whether given paths are allowed.",
            props={"site": S("Domain or URL of the site.", minLength=1, maxLength=300), "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to test (e.g. /, /services/).", "maxItems": 50}},
            required=("site",), output={"status": "HTTP status", "sitemaps": "[]", "rules": "['disallow: /x', ...]", "allowed": "{path: bool}"},
            permissions=("net:fetch",), category="web", timeout_s=30, limitations=HTTP_LIMITS)
async def robots(ctx, p: dict) -> dict:
    from ...seo.signals import robots_allows
    s = p["site"] if "://" in p["site"] else "https://" + p["site"]
    origin = f"{urlsplit(s).scheme}://{urlsplit(s).netloc}"
    try:
        r = await F.fetch(origin + "/robots.txt", max_bytes=300_000)
    except F.FetchError as e:
        raise fetch_error(e) from None
    txt = r.text() if r.status == 200 else ""
    pr = parse_robots(txt)
    ctx.evidence("robots_txt", txt or f"(HTTP {r.status})", source_url=r.final_url, summary=f"HTTP {r.status}", ext="txt")
    ctx.source("http", r.final_url, r.fetched_at)
    return {"url": r.final_url, "status": r.status, "sitemaps": pr["sitemaps"], "rules": [f"{k}: {v}" for k, v in pr["star"]],
            "allowed": {path: robots_allows(pr["star"], path) for path in (p.get("paths") or ["/"])}}


@capability("web.sitemap", "Fetch an XML sitemap (following a sitemap index) and return its URLs.",
            props={"url": S("Sitemap URL, or a site/domain to try /sitemap.xml.", minLength=1, maxLength=2000), "max_urls": I("Cap (default 2000).", minimum=1, maximum=20000)},
            required=("url",), output={"sitemaps": "[{url, status, valid}]", "urls": "[...]", "count": "int"},
            permissions=("net:fetch",), category="web", timeout_s=60, limitations=HTTP_LIMITS)
async def sitemap(ctx, p: dict) -> dict:
    u = p["url"] if "://" in p["url"] else "https://" + p["url"]
    if not urlsplit(u).path.endswith(".xml"):
        u = f"{urlsplit(u).scheme}://{urlsplit(u).netloc}/sitemap.xml"
    queue, seen, urls, checked = [u], set(), [], []
    cap = p.get("max_urls", 2000)
    while queue and len(seen) < 10 and len(urls) < cap:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            r = await F.fetch(cur, max_bytes=8_000_000)
        except F.FetchError as e:
            checked.append({"url": cur, "error": e.code})
            continue
        t = r.text()
        valid = r.status == 200 and ("<urlset" in t[:2000] or "<sitemapindex" in t[:2000])
        checked.append({"url": cur, "final_url": r.final_url, "status": r.status, "valid": valid})
        if valid:
            ctx.evidence("sitemap_xml", t, source_url=r.final_url, summary=f"HTTP {r.status}", ext="xml")
            ps = parse_sitemap(t)
            (queue.extend(ps["locs"]) if ps["index"] else urls.extend(ps["locs"]))
        ctx.source("http", r.final_url, r.fetched_at)
    if not any(c.get("valid") for c in checked):
        ctx.outcome.completeness = "none"
        ctx.limit("no valid sitemap was found at the given location")
    return {"sitemaps": checked, "urls": urls[:cap], "count": len(urls)}


@capability("web.crawl_site", "Crawl ONE site politely (robots.txt respected, same host only, bounded) and return per-page HTTP facts and signals. "
            "Every page's full signals are saved as evidence.",
            props={"url": S("Start URL (usually the homepage).", minLength=1, maxLength=2000), "max_pages": I("Page cap (default 25, max 80).", minimum=1, maximum=80),
                   "include_sitemap": B("Seed from the sitemap too (default true)."),
                   "exclude": A("Extra path fragments never to request (risky-looking paths such as reset/install/admin/login/cart are always skipped).", maxItems=50)},
            required=("url",), output={"site": "host", "robots": "{...}", "sitemap": "{checked[], url_count}", "pages": "[{url, final_url, status, signals, in_sitemap, inbound_internal_links}]",
                                       "errors": "[]", "skipped": "[]"},
            permissions=("net:fetch",), category="web", timeout_s=300, max_timeout_s=900, limitations=HTTP_LIMITS + ("Bounded by max_pages: a large site is sampled, not fully crawled.",))
async def crawl_site(ctx, p: dict) -> dict:
    res = await run_crawl(ctx, p["url"], p.get("max_pages", 25), p.get("include_sitemap", True), p.get("exclude"))
    res.pop("_full", None)
    return res


async def run_crawl(ctx, url: str, max_pages: int, include_sitemap: bool = True, exclude: list[str] | None = None) -> dict:
    full: dict[str, dict] = {}

    def on_page(page, r):
        sig = page.get("_full")
        if sig is not None:
            full[page["final_url"]] = sig
            page["evidence_id"] = ctx.evidence("page_signals", sig, source_url=page["final_url"], summary=f"HTTP {page['status']} title={sig['title'][:60]!r}")
        ctx.progress(f"crawled {len(full)} page(s)")

    try:
        F.normalise_url(url)
    except F.FetchError as e:
        raise fetch_error(e) from None
    res = await crawl(url, max_pages=max_pages, include_sitemap=include_sitemap, on_page=on_page, exclude=exclude)
    ctx.evidence("crawl_summary", {k: v for k, v in res.items() if k != "pages"} | {"pages": [{k: v for k, v in pg.items() if k != "links"} for pg in res["pages"]]},
                 source_url=url, summary=f"{len(res['pages'])} page(s)")
    ctx.source("http-crawl", url)
    if res["errors"]:
        ctx.limit(f"{len(res['errors'])} URL(s) could not be fetched")
    if res.get("unvisited_in_queue"):
        ctx.outcome.completeness = "partial"
        ctx.limit(f"stopped at max_pages; {res['unvisited_in_queue']} discovered URL(s) were not visited")
    res["_full"] = full
    return res
