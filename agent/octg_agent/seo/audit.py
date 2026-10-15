"""Deterministic site-audit rules over crawl data. Each finding carries its evidence, severity, confidence, recommendation, the pages concerned and
how to verify a fix. A finding is a DEFECT (directly observed, e.g. a 404), an OPPORTUNITY (observed gap, value plausible) or a HYPOTHESIS (worth testing,
cause not proven). Nothing here claims a ranking effect; values are expected-value estimates for prioritising, not promises.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

SEV = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
CONF = {"confirmed": 1.0, "likely": 0.75, "hypothesis": 0.45}
VALUE = {"high": 3, "medium": 2, "low": 1}
EFFORT = {"S": 1, "M": 2, "L": 3}
NOT_FOUND_RE = re.compile(r"(?i)\b(page not found|not found|does not exist|doesn.t exist|no longer available|404)\b")
UTILITY_PATHS = re.compile(r"/(privacy|terms|cookie|legal|admin|login|cart|checkout|account|thank|404|search|tag|feed)(/|$|\.)", re.I)


def _fid(rule: str, pages: list[str]) -> str:
    return "F-" + hashlib.sha1((rule + "|" + "|".join(sorted(pages))).encode()).hexdigest()[:8]


def finding(rule: str, title: str, category: str, kind: str, severity: str, confidence: str, pages: list[str], evidence: list[dict], recommendation: str,
            verification: str, effort: str = "S", value: str = "medium", notes: str = "") -> dict:
    score = round(SEV[severity] * CONF[confidence] * VALUE[value] / EFFORT[effort], 2)
    return {"finding_id": _fid(rule, pages), "rule": rule, "title": title, "category": category, "type": kind, "severity": severity, "confidence": confidence,
            "pages": pages, "evidence": evidence[:25], "recommendation": recommendation, "verification": verification, "effort": effort, "value": value,
            "priority_score": score, "notes": notes}


def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _place_name(loc: str) -> str:
    """'Portland, OR' -> 'portland'; 'Warren County, TN' -> 'warren county'."""
    return _norm((loc or "").split(",")[0])


def run(crawl: dict, *, full: dict[str, dict] | None = None, context: dict | None = None, variants: dict | None = None, keywords: list[dict] | None = None) -> list[dict]:
    full = full or {}
    ctx = context or {}
    if crawl.get("blocked"):
        return []   # a blocked crawl measured nothing about the site: no findings, the caller reports the block
    # what this crawl could actually measure (browser mode cannot see <head> metadata, alt attributes or JSON-LD): rules about anything else are skipped, not failed
    measured = set(crawl.get("measured") or ("status", "title", "meta_description", "canonical", "robots_meta", "h1", "viewport", "lang", "images", "jsonld", "tel", "links", "words", "headers"))
    F: list[dict] = []
    pages = crawl.get("pages", [])
    for p in pages:
        s = p.get("signals") or {}
        if s and (NOT_FOUND_RE.search(s.get("title", "")) or any(NOT_FOUND_RE.search(h) for h in s.get("h1", []))):
            p["soft_404"] = True
    html_pages = [p for p in pages if p.get("signals") and not p.get("soft_404")]
    indexable = [p for p in html_pages if not p["signals"].get("noindex") and "noindex" not in (p.get("x_robots_tag") or "").lower()]
    content_pages = [p for p in indexable if not UTILITY_PATHS.search(urlsplit(p["final_url"]).path)]
    home = next((p for p in html_pages if urlsplit(p["final_url"]).path in ("", "/")), html_pages[0] if html_pages else None)

    # ------------------------------------------------------------------ crawlability / indexability
    rb = crawl.get("robots") or {}
    if rb.get("challenge"):
        rb = {}
    if rb.get("disallow_all"):
        F.append(finding("robots_disallow_all", "robots.txt blocks the whole site for all crawlers", "indexability", "defect", "critical", "confirmed", [rb.get("url", "")],
                         [{"url": rb.get("url", ""), "detail": "; ".join(rb.get("rules_star", [])[:5])}], "Remove 'Disallow: /' for User-agent * (keep only private paths disallowed).",
                         "Fetch /robots.txt and confirm '/' is allowed for '*'.", "S", "high"))
    if rb.get("status") not in (None, 200) and not rb.get("error"):
        F.append(finding("robots_missing", f"robots.txt answers HTTP {rb.get('status')}", "indexability", "defect", "low", "confirmed", [rb.get("url", "")],
                         [{"url": rb.get("url", ""), "detail": f"status {rb.get('status')}"}], "Serve a robots.txt (200) that lists the sitemap.", "Fetch /robots.txt: 200 with a Sitemap line.", "S", "low"))
    sm = crawl.get("sitemap") or {}
    if sm and sm.get("checked") and all(c.get("valid") is False for c in sm["checked"]):   # unknown (network error, challenge, browser read) is not "missing"
        F.append(finding("sitemap_missing", "No valid XML sitemap was found", "indexability", "defect", "medium", "confirmed", [c.get("url", "") for c in sm.get("checked", [])][:3],
                         [{"url": c.get("url", ""), "detail": f"status {c.get('status', c.get('error'))}, type {c.get('content_type', '')}"} for c in sm.get("checked", [])],
                         "Publish /sitemap.xml listing every indexable page and reference it in robots.txt.", "Fetch the sitemap: 200, XML, contains the canonical URLs.", "S", "medium"))
    for p in html_pages:
        xr = (p.get("x_robots_tag") or "").lower()
        if p in indexable:
            continue
        sev = "high" if p.get("in_sitemap") else "medium"
        F.append(finding("noindex_page", f"Page is marked noindex{' but is listed in the sitemap' if p.get('in_sitemap') else ''}", "indexability", "defect" if p.get("in_sitemap") else "hypothesis",
                         sev, "confirmed", [p["final_url"]], [{"url": p["final_url"], "detail": f"meta robots='{p['signals'].get('meta_robots')}' x-robots-tag='{xr}'"}],
                         "If the page should rank, remove noindex; if not, drop it from the sitemap.", "Fetch the page: no noindex in meta robots or X-Robots-Tag.", "S",
                         "high" if p.get("in_sitemap") else "low"))
    by_url = {p["url"]: p for p in pages}
    soft = [p for p in pages if p.get("soft_404") and p.get("status", 200) < 400]
    if soft:
        browser = crawl.get("mode") == "browser"
        F.append(finding("broken_internal_links", f"{len(soft)} internal link target(s) show a 'page not found' page", "internal_linking", "defect", "high", "likely" if browser else "confirmed",
                         [p["url"] for p in soft], [{"url": p["url"], "detail": f"title {p['signals'].get('title', '')[:60]!r}; linked from {', '.join(p.get('linked_from', [])[:4]) or 'sitemap/start'}"} for p in soft],
                         "Fix each link at its source page (point it at the existing page for that service/area) or create the page if the service/area is real; "
                         "serve a real 404 status for missing pages" + (" (status code not measurable in browser mode)" if browser else "") + ".",
                         "Load each source page: no link to a not-found page; the old URLs return 404/301.", "S", "high",
                         "Found by the page's own 'not found' wording; HTTP status " + ("not measured (browser mode)." if browser else "was 200 (soft 404).")))
    broken = [p for p in pages if p.get("status", 200) >= 400]
    if broken:
        linkers = {}
        for p in html_pages:
            for l in p.get("links", []):
                if l in by_url and by_url[l].get("status", 200) >= 400:
                    linkers.setdefault(l, []).append(p["final_url"])
        F.append(finding("broken_pages", f"{len(broken)} internal URL(s) return an error status", "technical", "defect", "high", "confirmed", [p["url"] for p in broken],
                         [{"url": p["url"], "detail": f"HTTP {p['status']}; linked from {', '.join(linkers.get(p['url'], [])[:3]) or 'sitemap/start'}"} for p in broken],
                         "Fix or 301-redirect each broken URL and update the links that point to it.", "Re-fetch each URL: 200 (or a single 301 to a live page).", "S", "high"))
    chains = [p for p in pages if len(p.get("redirects") or []) >= 2]
    if chains:
        F.append(finding("redirect_chains", f"{len(chains)} URL(s) go through 2+ redirects", "technical", "defect", "low", "confirmed", [p["url"] for p in chains],
                         [{"url": p["url"], "detail": " -> ".join([h["url"] for h in p["redirects"]] + [p["final_url"]])} for p in chains],
                         "Point links and redirects straight at the final URL.", "Fetch: one hop at most.", "S", "low"))
    if sm.get("urls"):
        bad_sm = [p for p in pages if p.get("in_sitemap") and (p.get("status") != 200 or p.get("redirects"))]
        if bad_sm:
            F.append(finding("sitemap_bad_urls", f"{len(bad_sm)} sitemap URL(s) redirect or fail", "indexability", "defect", "medium", "confirmed", [p["url"] for p in bad_sm],
                             [{"url": p["url"], "detail": f"HTTP {p.get('status')}, redirects {len(p.get('redirects') or [])} -> {p['final_url']}"} for p in bad_sm],
                             "List only final, 200-status canonical URLs in the sitemap.", "Every <loc> answers 200 without redirect.", "S", "medium"))
    # host / protocol variants
    for v in (variants or {}).get("checks", []):
        if v.get("ok") is False:
            F.append(finding("host_variant", v["title"], "technical", "defect", v.get("severity", "medium"), "confirmed", [v["url"]], [{"url": v["url"], "detail": v["detail"]}],
                             v["recommendation"], "Fetch the variant: one 301 to the canonical https host.", "S", "medium"))

    # ------------------------------------------------------------------ canonicals
    for p in (indexable if "canonical" in measured else []):
        s = p["signals"]
        if s.get("canonical_count", 0) > 1:
            F.append(finding("multiple_canonicals", "Page declares more than one canonical URL", "indexability", "defect", "medium", "confirmed", [p["final_url"]],
                             [{"url": p["final_url"], "detail": f"{s['canonical_count']} canonical links"}], "Keep exactly one rel=canonical.", "Fetch the page: one canonical.", "S", "medium"))
        c = s.get("canonical") or ""
        if c and c.rstrip("/") != p["final_url"].rstrip("/"):
            cross = urlsplit(c).hostname != urlsplit(p["final_url"]).hostname
            F.append(finding("canonical_elsewhere", "Canonical points to a different " + ("host" if cross else "URL"), "indexability", "defect" if cross else "hypothesis",
                             "high" if cross else "medium", "confirmed" if cross else "likely", [p["final_url"]], [{"url": p["final_url"], "detail": f"canonical={c}"}],
                             "Make the canonical self-referencing unless this page is a deliberate duplicate.", "Fetch: canonical equals the page's own final URL.", "S", "medium"))
    no_canon = [p["final_url"] for p in content_pages if not p["signals"].get("canonical")] if "canonical" in measured else []
    if no_canon:
        F.append(finding("canonical_missing", f"{len(no_canon)} page(s) have no canonical link", "indexability", "opportunity", "low", "confirmed", no_canon,
                         [{"url": u, "detail": "no <link rel=canonical>"} for u in no_canon], "Add a self-referencing canonical to each indexable page.", "Fetch: canonical present.", "S", "low"))

    # ------------------------------------------------------------------ metadata
    titles: dict[str, list[str]] = {}
    descs: dict[str, list[str]] = {}
    for p in content_pages:
        s = p["signals"]
        u = p["final_url"]
        t = s.get("title", "")
        if "title" not in measured:
            pass
        elif not t:
            F.append(finding("title_missing", "Page has no <title>", "metadata", "defect", "high", "confirmed", [u], [{"url": u, "detail": "empty title"}],
                             "Write a unique title naming the service and place.", "Fetch: non-empty, unique title.", "S", "high"))
        else:
            titles.setdefault(t.strip().lower(), []).append(u)
            if len(t) > 65:
                F.append(finding("title_long", "Title is likely to be truncated in results", "metadata", "opportunity", "low", "likely", [u], [{"url": u, "detail": f"{len(t)} chars: {t[:90]}"}],
                                 "Keep the title near 50-60 characters with the main phrase first.", "Fetch: title <= 60-65 characters.", "S", "low"))
            elif len(t) < 15:
                F.append(finding("title_short", "Title is very short", "metadata", "opportunity", "low", "likely", [u], [{"url": u, "detail": f"{len(t)} chars: {t}"}],
                                 "Use a descriptive title (service + place + brand).", "Fetch: title 30-60 characters.", "S", "low"))
        d = s.get("meta_description", "")
        if "meta_description" not in measured:
            pass
        elif not d:
            F.append(finding("meta_description_missing", "Page has no meta description", "metadata", "opportunity", "medium", "confirmed", [u], [{"url": u, "detail": "no meta description"}],
                             "Write a 140-160 character description with the offer, place and a reason to click.", "Fetch: meta description present, 70-160 characters.", "S", "medium"))
        else:
            descs.setdefault(d.strip().lower(), []).append(u)
            if len(d) > 170:
                F.append(finding("meta_description_long", "Meta description is likely to be truncated", "metadata", "opportunity", "low", "likely", [u], [{"url": u, "detail": f"{len(d)} chars"}],
                                 "Trim to about 150-160 characters.", "Fetch: description <= 160 characters.", "S", "low"))
        h1 = s.get("h1") or []
        if "h1" not in measured:
            pass
        elif not h1:
            F.append(finding("h1_missing", "Page has no H1 heading", "content", "defect", "medium", "confirmed", [u], [{"url": u, "detail": "0 h1"}],
                             "Add one H1 stating what the page is about.", "Fetch: exactly one H1.", "S", "medium"))
        elif len(h1) > 1:
            F.append(finding("h1_multiple", "Page has several H1 headings", "content", "hypothesis", "low", "likely", [u], [{"url": u, "detail": " | ".join(h1[:5])}],
                             "Keep one H1; demote the others to H2.", "Fetch: exactly one H1.", "S", "low"))
        if "viewport" in measured and not s.get("viewport"):
            F.append(finding("viewport_missing", "No mobile viewport meta tag", "technical", "defect", "medium", "confirmed", [u], [{"url": u, "detail": "no meta viewport"}],
                             "Add <meta name=viewport content='width=device-width, initial-scale=1'>.", "Fetch: viewport meta present.", "S", "medium"))
        if "lang" in measured and not s.get("lang"):
            F.append(finding("lang_missing", "No lang attribute on <html>", "technical", "defect", "low", "confirmed", [u], [{"url": u, "detail": "no <html lang>"}],
                             "Set <html lang='en'> (or the page language).", "Fetch: html lang present.", "S", "low"))
    for t, us in titles.items():
        if len(us) > 1:
            F.append(finding("title_duplicate", f"{len(us)} pages share the same title", "duplication", "defect", "medium", "confirmed", us, [{"url": u, "detail": t[:120]} for u in us],
                             "Give each page a unique title matching its own topic; if two pages target the same intent, merge or differentiate them.", "Fetch each: titles differ.", "S", "medium"))
    for d, us in descs.items():
        if len(us) > 1:
            F.append(finding("description_duplicate", f"{len(us)} pages share the same meta description", "duplication", "opportunity", "low", "confirmed", us,
                             [{"url": u, "detail": d[:120]} for u in us], "Write a specific description per page.", "Fetch each: descriptions differ.", "S", "low"))

    # ------------------------------------------------------------------ content
    thin = [p for p in content_pages if p["signals"].get("word_count", 0) < 250] if "words" in measured else []
    if thin:
        F.append(finding("thin_content", f"{len(thin)} page(s) have under 250 words of HTML text", "content", "hypothesis", "low", "hypothesis", [p["final_url"] for p in thin],
                         [{"url": p["final_url"], "detail": f"{p['signals'].get('word_count')} words"} for p in thin],
                         "Where the page targets a search, add useful specifics (what, where, price/time expectations, FAQs). Short pages are fine for utility pages.",
                         "Re-crawl: word count and coverage of the page's topic improved; then watch impressions for that page.", "M", "medium",
                         "Word count is a weak proxy; JavaScript-rendered text is not counted by this check."))
    imgs = [(p["final_url"], p["signals"].get("images_missing_alt", 0)) for p in content_pages if p["signals"].get("images_missing_alt", 0)] if "images" in measured else []
    if imgs:
        F.append(finding("img_alt_missing", f"{sum(n for _, n in imgs)} image(s) without an alt attribute", "content", "defect", "low", "confirmed", [u for u, _ in imgs],
                         [{"url": u, "detail": f"{n} image(s) without alt"} for u, n in imgs], "Add descriptive alt text (or alt='' for decorative images).",
                         "Fetch: every <img> has an alt attribute.", "S", "low"))

    # ------------------------------------------------------------------ structured data / NAP
    if home:
        hs = home["signals"]
        local_types = [t for t in hs.get("jsonld_types", []) if re.search(r"(LocalBusiness|Store|Restaurant|Service|Organization|Shop|Contractor|Locksmith|Plumber|Electrician|HVAC|Towing|AutoRepair|Painter|BarOrPub)", t)]
        if "jsonld" in measured and not local_types:
            F.append(finding("localbusiness_schema_missing", "Homepage has no LocalBusiness/Organization structured data", "structured_data", "opportunity", "medium", "confirmed",
                             [home["final_url"]], [{"url": home["final_url"], "detail": f"JSON-LD types: {hs.get('jsonld_types') or 'none'}"}],
                             "Add LocalBusiness JSON-LD (name, address or areaServed, telephone, url, openingHours, sameAs).", "Rich Results Test / fetch: LocalBusiness present and valid.", "S", "medium"))
        if "tel" in measured and not hs.get("tel_links"):
            F.append(finding("no_click_to_call", "Homepage has no tap-to-call (tel:) link", "conversion", "opportunity", "medium", "confirmed", [home["final_url"]],
                             [{"url": home["final_url"], "detail": "no tel: links"}], "Add a visible tel: link in the header and hero.", "Fetch: at least one tel: link.", "S", "high"))
    errs = [p for p in content_pages if p["signals"].get("jsonld_errors")] if "jsonld" in measured else []
    if errs:
        F.append(finding("jsonld_invalid", "Structured data that does not parse as JSON", "structured_data", "defect", "medium", "confirmed", [p["final_url"] for p in errs],
                         [{"url": p["final_url"], "detail": f"{p['signals']['jsonld_errors']} block(s) failed to parse"} for p in errs], "Fix the JSON-LD syntax.",
                         "Fetch: all ld+json blocks parse.", "S", "medium"))
    phone = _digits(ctx.get("phone", ""))
    if phone and len(phone) == 10 and html_pages:
        seen_on = []
        for p in html_pages:
            fs = full.get(p["final_url"]) or {}
            blob = " ".join([fs.get("text", "")] + p["signals"].get("tel_links", []))
            for o in fs.get("jsonld", []):
                blob += " " + str(o.get("telephone", ""))
            if phone in re.sub(r"\D", "", blob):
                seen_on.append(p["final_url"])
        if not seen_on:
            F.append(finding("nap_phone_absent", "The business phone on file does not appear on the crawled pages", "local", "defect", "high", "likely", [home["final_url"]] if home else [],
                             [{"url": home["final_url"] if home else "", "detail": f"phone ending {phone[-4:]} not found in text, tel: links or JSON-LD of {len(html_pages)} page(s)"}],
                             "Show the same phone number everywhere (header, footer, contact, JSON-LD) as on the Google Business Profile.", "Fetch: phone present in text and JSON-LD.", "S", "high",
                             "Checked against the client's phone in the Knowledge Hub; a tracking number on the site would also trigger this."))
    # ------------------------------------------------------------------ local relevance / keyword coverage (opportunities, not defects)
    corpus = {p["final_url"]: _norm(" ".join([p["signals"].get("title", ""), " ".join(p["signals"].get("h1", [])), urlsplit(p["final_url"]).path.replace("-", " ").replace("/", " ")]))
              for p in content_pages}
    body = {p["final_url"]: _norm((full.get(p["final_url"]) or {}).get("text", "")) for p in content_pages}
    for loc in (ctx.get("target_locations") or [])[:15]:
        name = _place_name(loc)
        if not name or len(name) < 3:
            continue
        in_head = [u for u, t in corpus.items() if name in t]
        in_body = [u for u, t in body.items() if name in t]
        if not in_head:
            F.append(finding("location_not_targeted", f"No page title/H1/URL targets '{loc}'", "local", "opportunity", "medium", "likely" if in_body else "confirmed",
                             in_body[:5] or ([home["final_url"]] if home else []),
                             [{"url": u, "detail": f"'{name}' only in body text" } for u in in_body[:5]] or [{"url": home["final_url"] if home else "", "detail": f"'{name}' not found on any crawled page"}],
                             f"Make '{loc}' explicit where it is genuinely served: title/H1 of the most relevant page, or a dedicated location page with real local detail (no doorway pages).",
                             f"Re-crawl: a page names '{loc}' in its title or H1; later compare impressions for '{loc}' queries in first-party search data (GSC).", "M", "medium"))
    for kw in (keywords or [])[:30]:
        terms = [w for w in _norm(kw.get("keyword", "")).split() if len(w) > 2 and w not in ("near", "the", "and", "for", "with", "best")]
        if not terms:
            continue
        hits = [u for u, t in corpus.items() if all(w in t for w in terms)]
        if not hits:
            part = [u for u, t in corpus.items() if sum(w in t for w in terms) >= max(1, len(terms) - 1)]
            F.append(finding("keyword_no_target_page", f"No page's title/H1/URL contains all of '{kw['keyword']}'", "content", "opportunity", "low" if part else "medium", "hypothesis",
                             part[:5], [{"url": u, "detail": "partial match in title/H1/URL"} for u in part[:5]] or [{"url": "", "detail": "no page matches the phrase"}],
                             f"Decide which page should answer '{kw['keyword']}' and make that explicit in its title/H1/copy (or create one if the service is real and has no page).",
                             "Re-crawl: one page clearly targets the phrase; later check its search impressions.", "M", "medium",
                             "Phrase matching is literal; a page can rank without the exact words. Treat as a prompt for review, not a defect."))
    # ------------------------------------------------------------------ internal linking
    if sm.get("urls"):
        orphans = [p for p in content_pages if p.get("in_sitemap") and p.get("inbound_internal_links", 0) == 0 and home and p["final_url"] != home["final_url"]]
        if orphans:
            F.append(finding("orphan_pages", f"{len(orphans)} sitemap page(s) with no internal links from the crawled pages", "internal_linking", "defect", "medium", "likely",
                             [p["final_url"] for p in orphans], [{"url": p["final_url"], "detail": "0 inbound internal links found"} for p in orphans],
                             "Link to each page from a relevant hub/menu/footer or related pages.", "Re-crawl: each page has at least one inbound internal link.", "S", "medium",
                             "Only links on the crawled pages are counted; raise max_pages for a complete picture."))
    # pages that get far fewer internal links than their siblings (same first path segment)
    groups: dict[str, list[dict]] = {}
    for p in content_pages:
        seg = urlsplit(p["final_url"]).path.strip("/").split("/")[0] if "/" in urlsplit(p["final_url"]).path.strip("/") else ""
        if seg:
            groups.setdefault(seg, []).append(p)
    for seg, ps in groups.items():
        if len(ps) < 4:
            continue
        counts = sorted(p.get("inbound_internal_links", 0) for p in ps)
        median = counts[len(counts) // 2]
        weak = [p for p in ps if median >= 8 and p.get("inbound_internal_links", 0) < median * 0.4]
        if weak:
            F.append(finding("weakly_linked_pages", f"{len(weak)} /{seg}/ page(s) get far fewer internal links than their siblings", "internal_linking", "opportunity", "medium", "likely",
                             [p["final_url"] for p in weak], [{"url": p["final_url"], "detail": f"{p.get('inbound_internal_links', 0)} inbound vs median {median} for /{seg}/"} for p in weak],
                             f"Link these pages wherever their /{seg}/ siblings are linked (menus, footer 'areas'/'services' lists, hub pages).",
                             "Re-crawl: inbound internal links comparable to the siblings.", "S", "medium", "Counted over the crawled pages only."))
    # ------------------------------------------------------------------ performance hint (hypothesis)
    slow = [p for p in html_pages if p.get("elapsed_ms", 0) > 2000] if "headers" in measured else []
    if slow:
        F.append(finding("slow_html", f"{len(slow)} page(s) took over 2 s to return HTML from this machine", "technical", "hypothesis", "low", "hypothesis", [p["final_url"] for p in slow],
                         [{"url": p["final_url"], "detail": f"{p['elapsed_ms']} ms"} for p in slow], "Check server response time / caching; confirm with PageSpeed Insights (field data).",
                         "PageSpeed Insights / CrUX TTFB for the page.", "M", "low", "Measured from one location over one request: noisy."))
    return group(F)


GROUPABLE = {"title_missing", "title_long", "title_short", "meta_description_missing", "meta_description_long", "h1_missing", "h1_multiple", "viewport_missing", "lang_missing",
             "noindex_page", "canonical_elsewhere", "multiple_canonicals", "host_variant"}


def group(F: list[dict]) -> list[dict]:
    """Merge single-page findings of the same rule/severity/confidence/type into one finding listing every page (stable id from the full page set)."""
    out: list[dict] = []
    buckets: dict[tuple, list[dict]] = {}
    for f in F:
        if f["rule"] in GROUPABLE:
            buckets.setdefault((f["rule"], f["severity"], f["confidence"], f["type"]), []).append(f)
        else:
            out.append(f)
    for (rule, sev, conf, kind), fs in buckets.items():
        if len(fs) == 1:
            out.append(fs[0])
            continue
        pages = [u for f in fs for u in f["pages"]]
        g = finding(rule, f"{len(pages)} pages: {fs[0]['title'][0].lower() + fs[0]['title'][1:]}", fs[0]["category"], kind, sev, conf, pages,
                    [e for f in fs for e in f["evidence"]], fs[0]["recommendation"], fs[0]["verification"], fs[0]["effort"] if len(pages) < 10 else "M", fs[0]["value"], fs[0].get("notes", ""))
        g["evidence"] = [e for f in fs for e in f["evidence"]][:60]
        out.append(g)
    out.sort(key=lambda f: (-f["priority_score"], f["rule"]))
    return out
