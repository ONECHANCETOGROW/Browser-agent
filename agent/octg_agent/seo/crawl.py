"""A small, polite, bounded crawler for ONE site: robots.txt, sitemap(s), then breadth-first over same-site links.

Bounded by max_pages and a per-request delay; never leaves the start host (www and apex count as the same site); respects robots.txt for '*';
skips obvious binary assets. Each fetched page keeps its HTTP facts and extracted signals so findings can point at exact evidence.
"""
from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from .fetch import FetchError, FetchResult, fetch as _fetch, new_session
from .signals import brief, extract, host_key, parse_robots, parse_sitemap, robots_allows

# GET requests are supposed to be safe, but many small sites have URLs that ACT when merely loaded (reset a password, install a database, log out,
# delete, add to cart, confirm, unsubscribe). The crawler never requests a path that looks like one, whatever links or sitemaps say.
UNSAFE_PATH = re.compile(r"(?i)(reset|passw|install|setup|migrat|seed|cron|logout|log-out|signout|sign-out|delete|remove|destroy|unsubscribe|confirm|activate|verify|token|"
                         r"admin|wp-admin|wp-login|login|signin|sign-in|register|signup|account|checkout|cart|order|payment|api/|ajax|webhook|callback|debug|phpinfo|\.env|backup|dump|export)")
ASSET = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|ico|pdf|zip|mp4|webm|mov|mp3|wav|css|js|json|xml|txt|woff2?|ttf|eot)(\?|$)", re.I)


def canon_url(u: str) -> str:
    p = urlsplit(u)
    path = p.path or "/"
    return urlunsplit((p.scheme, (p.hostname or "").lower() + (f":{p.port}" if p.port else ""), path, p.query, ""))


async def crawl(start_url: str, *, max_pages: int = 30, delay_s: float = 0.3, fetcher: Callable[..., Awaitable[FetchResult]] | None = None, include_sitemap: bool = True,
                on_page: Callable[[dict, FetchResult | None], None] | None = None, cancelled: Callable[[], bool] | None = None, allow_private: bool = False,
                exclude: list[str] | None = None) -> dict:
    session = None
    if fetcher is None:
        session = new_session(25.0, allow_private)

        async def fetcher(u, **kw):  # noqa: E306
            return await _fetch(u, session=session, allow_private=allow_private, **kw)
    try:
        return await _crawl(start_url, max_pages=max_pages, delay_s=delay_s, fetcher=fetcher, include_sitemap=include_sitemap, on_page=on_page, cancelled=cancelled,
                            exclude=[x.lower() for x in (exclude or []) if x])
    finally:
        if session is not None:
            await session.close()


async def _crawl(start_url, *, max_pages, delay_s, fetcher, include_sitemap, on_page, cancelled, exclude=()) -> dict:
    sp = urlsplit(start_url if "://" in start_url else "https://" + start_url)
    origin = f"{sp.scheme}://{sp.netloc}"
    site = host_key(origin)
    out: dict = {"start_url": start_url, "site": site, "robots": None, "sitemap": None, "pages": [], "skipped": [], "errors": [], "limits": {"max_pages": max_pages},
                 "mode": "http", "blocked": None}

    # robots.txt
    rules: list = []
    try:
        r = await fetcher(origin + "/robots.txt", max_bytes=300_000)
        if r.challenge:
            raise FetchError("BOT_CHALLENGE", "robots.txt was answered with a bot-protection page")
        txt = r.text() if r.status == 200 else ""
        pr = parse_robots(txt) if txt else {"sitemaps": [], "star": [], "googlebot": [], "groups": 0}
        rules = pr["star"]
        out["robots"] = {"url": r.final_url, "status": r.status, "sitemaps": pr["sitemaps"], "disallow_all": any(k == "disallow" and v == "/" for k, v in rules)
                         and not any(k == "allow" and v in ("/", "/*") for k, v in rules), "rules_star": [f"{k}: {v}" for k, v in rules][:40], "body": txt[:5000]}
    except FetchError as e:
        out["robots"] = {"error": e.code, "message": e.message, **({"challenge": True} if e.code == "BOT_CHALLENGE" else {})}

    # sitemap(s)
    sm_urls: list[str] = []
    if include_sitemap:
        cands = (out["robots"] or {}).get("sitemaps") or [origin + "/sitemap.xml"]
        seen_sm: set[str] = set()
        queue = list(cands)[:5]
        sm_info = {"checked": [], "url_count": 0}
        while queue and len(seen_sm) < 8:
            u = queue.pop(0)
            if u in seen_sm:
                continue
            seen_sm.add(u)
            try:
                r = await fetcher(u, max_bytes=5_000_000)
                if r.challenge:
                    sm_info["checked"].append({"url": u, "error": "BOT_CHALLENGE", "challenge": True})
                    continue
                ok = r.status == 200 and ("xml" in r.content_type or r.text().lstrip().startswith("<?xml") or "<urlset" in r.text()[:500])
                sm_info["checked"].append({"url": u, "final_url": r.final_url, "status": r.status, "content_type": r.content_type, "valid": ok})
                if ok:
                    p = parse_sitemap(r.text())
                    if p["index"]:
                        queue += p["locs"][:6]
                    else:
                        sm_urls += p["locs"]
            except FetchError as e:
                sm_info["checked"].append({"url": u, "error": e.code})
        sm_info["url_count"] = len(sm_urls)
        sm_info["urls"] = sm_urls[:2000]
        out["sitemap"] = sm_info
    sm_set = {canon_url(u) for u in sm_urls}

    # breadth-first crawl
    queue_: deque[str] = deque([canon_url(origin + (sp.path or "/"))])
    for u in sm_urls:
        if host_key(u) == site:
            queue_.append(canon_url(u))
    seen: set[str] = set()
    inbound: dict[str, int] = {}
    linkers: dict[str, list[str]] = {}
    while queue_ and len(out["pages"]) < max_pages:
        if cancelled and cancelled():
            out["errors"].append({"url": "", "error": "CANCELLED"})
            break
        u = queue_.popleft()
        if u in seen:
            continue
        seen.add(u)
        if host_key(u) != site or ASSET.search(urlsplit(u).path):
            continue
        path = urlsplit(u).path or "/"
        pq = path + (("?" + urlsplit(u).query) if urlsplit(u).query else "")
        if path != "/" and (UNSAFE_PATH.search(pq) or any(x in pq.lower() for x in exclude)):
            out["skipped"].append({"url": u, "reason": "unsafe-looking or excluded path: never requested"})
            continue
        if rules and not robots_allows(rules, path + (("?" + urlsplit(u).query) if urlsplit(u).query else "")):
            out["skipped"].append({"url": u, "reason": "disallowed by robots.txt"})
            continue
        try:
            r = await fetcher(u, max_bytes=3_000_000)
        except FetchError as e:
            out["errors"].append({"url": u, "error": e.code, "message": e.message})
            continue
        if r.challenge:
            if not out["pages"]:   # the start page itself is a bot-protection interstitial: nothing this client sees is the site
                out["blocked"] = {"reason": "BOT_CHALLENGE", "url": u, "status": r.status, "server": r.headers.get("server", "")}
                break
            out["errors"].append({"url": u, "error": "BOT_CHALLENGE", "message": f"HTTP {r.status} bot-protection page"})
            continue
        page = {"url": u, "final_url": r.final_url, "status": r.status, "content_type": r.content_type, "elapsed_ms": r.elapsed_ms, "bytes": len(r.body),
                "redirects": [{"url": h.url, "status": h.status, "location": h.location} for h in r.chain], "x_robots_tag": r.headers.get("x-robots-tag", ""),
                "in_sitemap": u in sm_set or canon_url(r.final_url) in sm_set, "signals": None, "links": []}
        if r.status == 200 and r.is_html:
            sig = extract(r.text(), r.final_url)
            page["signals"] = brief(sig)
            page["links"] = sorted({canon_url(l["url"]) for l in sig["links_internal"]})
            for l in page["links"]:
                if l != canon_url(r.final_url):
                    inbound[l] = inbound.get(l, 0) + 1
                linkers.setdefault(l, []).append(r.final_url)
                if l not in seen:
                    queue_.append(l)
            page["_full"] = sig
        if r.final_url != u:
            seen.add(canon_url(r.final_url))
        if on_page:
            on_page(page, r)
        page.pop("_full", None)
        out["pages"].append(page)
        if delay_s:
            await asyncio.sleep(delay_s)
    for p in out["pages"]:
        p["inbound_internal_links"] = inbound.get(canon_url(p["final_url"]), 0) + (inbound.get(p["url"], 0) if canon_url(p["final_url"]) != p["url"] else 0)
        p["linked_from"] = sorted(set(linkers.get(p["url"], []) + linkers.get(canon_url(p["final_url"]), [])))[:20]
    out["unvisited_in_queue"] = len([u for u in queue_ if u not in seen])
    return out
