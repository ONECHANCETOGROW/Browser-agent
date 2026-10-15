"""Read a site through the agent's real browser when plain HTTP is refused (for example a CDN serving bot-protection interstitials to scripts).

This is ordinary browsing in the owner's Chrome, one page at a time, in the job's own tab. If the browser itself is shown an interactive challenge, the
crawl stops and reports it: nothing is clicked, solved or bypassed. A browser read cannot see <head> metadata (meta description, canonical, robots meta),
alt attributes or JSON-LD, so the result declares exactly what was MEASURED and the audit skips rules about anything else.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit

from ...seo.crawl import ASSET, UNSAFE_PATH, canon_url
from ...seo.fetch import CHALLENGE_RE
from ...seo.signals import host_key
from ..store import CollabError

MEASURED = ("title", "h1", "tel", "links", "words")
NOT_MEASURED = ("meta description", "canonical", "robots meta / X-Robots-Tag", "structured data (JSON-LD)", "image alt text", "HTTP status codes and redirects", "viewport/lang")


async def _read(ctx, tab: int, url: str) -> dict | None:
    nav = await ctx.browser("navigate", {"tab_id": tab, "url": url, "timeout_ms": 30000})
    if not nav.get("success"):
        return {"error": (nav.get("error") or {}).get("code") or "NAVIGATION_FAILED"}
    st = await ctx.browser("get_page_state", {"tab_id": tab})
    page = ((st.get("result") or {}).get("page")) or {}
    title = page.get("title") or ""
    if CHALLENGE_RE.search(title.lower()):          # a JavaScript check usually finishes by itself in a real browser: wait once, never interact
        await ctx.browser("wait", {"tab_id": tab, "ms": 6000})
        st = await ctx.browser("get_page_state", {"tab_id": tab})
        page = ((st.get("result") or {}).get("page")) or {}
        title = page.get("title") or ""
        if CHALLENGE_RE.search(title.lower()):
            return {"challenge": True}
    rd = await ctx.browser("read_page", {"tab_id": tab, "scope": "page", "max_chars": 40000})
    text = (rd.get("result") or {}).get("text", "") if rd.get("success") else ""
    ins = await ctx.browser("inspect_page", {"tab_id": tab, "max_elements": 800, "viewport_only": False})
    els = ((ins.get("result") or {}).get("elements")) or [] if ins.get("success") else []
    final = page.get("url") or url
    hrefs = [e.get("href") or "" for e in els if e.get("tag") == "a" and e.get("href")]
    return {"final_url": final, "title": title, "headings": page.get("headings") or [], "text": text, "hrefs": hrefs}


async def browser_crawl(ctx, start_url: str, max_pages: int = 25, exclude: list[str] | None = None) -> dict:
    if ctx.rt.session is None:
        raise CollabError("EXTENSION_NOT_CONNECTED", "the site refuses plain HTTP and the Chrome extension is not connected, so it cannot be read", retryable=True)
    exclude = [x.lower() for x in (exclude or []) if x]
    sp = urlsplit(start_url if "://" in start_url else "https://" + start_url)
    site = host_key(start_url)
    out = {"start_url": start_url, "site": site, "mode": "browser", "measured": list(MEASURED), "not_measured": list(NOT_MEASURED), "robots": None, "sitemap": None,
           "pages": [], "skipped": [], "errors": [], "blocked": None, "limits": {"max_pages": max_pages}}
    full: dict[str, dict] = {}
    tab = await ctx.open_tab(f"{sp.scheme}://{sp.netloc}/")
    # robots.txt and sitemap as the browser shows them (plain text / XML tree): best effort
    sm_urls: list[str] = []
    r = await _read(ctx, tab, f"{sp.scheme}://{sp.netloc}/robots.txt")
    if r and not r.get("error") and not r.get("challenge"):
        out["robots"] = {"url": r["final_url"], "status": None, "via": "browser", "sitemaps": re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r["text"]), "body": r["text"][:5000],
                         "disallow_all": bool(re.search(r"(?im)^\s*disallow:\s*/\s*$", r["text"])) and bool(re.search(r"(?im)^\s*user-agent:\s*\*", r["text"]))}
    sm_url = ((out["robots"] or {}).get("sitemaps") or [f"{sp.scheme}://{sp.netloc}/sitemap.xml"])[0]
    r = await _read(ctx, tab, sm_url)
    if r and not r.get("error") and not r.get("challenge"):
        sm_urls = [u for u in re.findall(r"https?://[^\s<>\"]+", r["text"]) if host_key(u) == site and not ASSET.search(urlsplit(u).path)]
        out["sitemap"] = {"checked": [{"url": sm_url, "valid": True if sm_urls else None, "via": "browser", "note": "" if sm_urls else "no URLs visible in the browser: unknown, not proof of absence"}],
                          "url_count": len(sm_urls), "urls": sm_urls[:2000]}
    sm_set = {canon_url(u) for u in sm_urls}
    queue = [canon_url(f"{sp.scheme}://{sp.netloc}{sp.path or '/'}")] + [canon_url(u) for u in sm_urls]
    seen: set[str] = set()
    inbound: dict[str, int] = {}
    linkers: dict[str, list[str]] = {}
    while queue and len(out["pages"]) < max_pages:
        ctx.check_cancel()
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        path = urlsplit(u).path or "/"
        pq = path + (("?" + urlsplit(u).query) if urlsplit(u).query else "")
        if host_key(u) != site or ASSET.search(path):
            continue
        if path != "/" and (UNSAFE_PATH.search(pq) or any(x in pq.lower() for x in exclude)):
            out["skipped"].append({"url": u, "reason": "unsafe-looking or excluded path: never requested"})
            continue
        ctx.progress(f"browser read {len(out['pages']) + 1}/{max_pages}: {u}")
        t0 = time.perf_counter()
        r = await _read(ctx, tab, u)
        if r is None or r.get("error"):
            out["errors"].append({"url": u, "error": (r or {}).get("error", "READ_FAILED")})
            continue
        if r.get("challenge"):
            if not out["pages"]:
                out["blocked"] = {"reason": "BOT_CHALLENGE_IN_BROWSER", "url": u}
                break
            out["errors"].append({"url": u, "error": "BOT_CHALLENGE_IN_BROWSER"})
            continue
        internal = sorted({canon_url(h) for h in r["hrefs"] if h.startswith("http") and host_key(h) == site})
        tel = sorted({h[4:] for h in r["hrefs"] if h.lower().startswith("tel:")})
        words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", r["text"]))
        sig = {"url": r["final_url"], "title": r["title"], "title_length": len(r["title"]), "headings": r["headings"], "h1": [h["text"] for h in r["headings"] if h.get("level") == 1],
               "word_count": words, "tel_links": tel, "internal_link_count": len(internal), "text_excerpt": r["text"][:600], "noindex": False}
        page = {"url": u, "final_url": r["final_url"], "status": 200, "content_type": "text/html", "elapsed_ms": int((time.perf_counter() - t0) * 1000), "redirects": [], "x_robots_tag": "",
                "in_sitemap": u in sm_set or canon_url(r["final_url"]) in sm_set, "signals": sig, "links": internal, "via": "browser"}
        full[r["final_url"]] = {**sig, "text": r["text"], "jsonld": []}
        page["evidence_id"] = ctx.evidence("rendered_page", {**sig, "text": r["text"][:20000], "links": internal}, source_url=r["final_url"], summary=f"browser read {r['title'][:60]!r}")
        for l in internal:
            if l != canon_url(r["final_url"]):
                inbound[l] = inbound.get(l, 0) + 1
                linkers.setdefault(l, []).append(r["final_url"])
            if l not in seen:
                queue.append(l)
        out["pages"].append(page)
        await asyncio.sleep(0.4)
    for p in out["pages"]:
        p["inbound_internal_links"] = inbound.get(canon_url(p["final_url"]), 0)
        p["linked_from"] = sorted(set(linkers.get(p["url"], []) + linkers.get(canon_url(p["final_url"]), [])))[:20]
    out["unvisited_in_queue"] = len([u for u in queue if u not in seen])
    await ctx.close_tabs()
    out["_full"] = full
    return out
