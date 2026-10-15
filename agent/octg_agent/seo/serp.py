"""Search-results pages read through the agent's browser: URL building and a conservative parser.

The parser uses only what the generic engine already returns: the page's visible headings (get_page_state) and its links with names and hrefs
(inspect_page). An ORGANIC result is a link whose accessible name begins with one of the page's result headings and whose target is outside the
search engine; sitelinks, ads (engine-hosted click URLs), maps and engine navigation are excluded. Positions are therefore "the Nth organic result
this browser was shown", never a universal ranking. Blocks (CAPTCHA / consent) are reported, never bypassed.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from .signals import host_key

_LISTS = json.loads((Path(__file__).with_name("domains.json")).read_text(encoding="utf-8"))
ENGINE_HOSTS = tuple(_LISTS["engine_hosts"])
CONSENT_HOSTS = tuple(_LISTS["consent_hosts"])
DIRECTORIES = tuple(_LISTS["directories"])
BLOCK_TEXT = re.compile(r"unusual traffic|not a robot|are you a robot|captcha|verify you are human|detected unusual", re.I)
CONSENT_TEXT = re.compile(r"before you continue to google|we use cookies and data to", re.I)


def uule(location: str) -> str:
    """Google's location parameter for a canonical place name (e.g. 'Portland,Oregon,United States'). Google may ignore it; results record that."""
    raw = location.encode("utf-8")
    keys = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if len(raw) >= len(keys):
        return ""
    return "w+CAIQICI" + keys[len(raw)] + base64.b64encode(raw).decode("ascii")


def build_url(engine: str, query: str, *, country: str = "us", language: str = "en", location: str = "", count: int = 10) -> tuple[str, list[str]]:
    notes: list[str] = []
    if engine == "google":
        u = f"https://www.google.com/search?q={quote_plus(query)}&hl={quote_plus(language)}&gl={quote_plus(country)}&pws=0"
        if location:
            code = uule(location)
            if code:
                u += "&uule=" + quote_plus(code, safe="+")
                notes.append(f"location requested via Google's uule parameter ({location}); Google may still use the browser's own location")
        notes.append("results are what this browser session was shown (signed-in state, history and IP location can personalise them)")
        return u, notes
    if engine == "bing":
        u = f"https://www.bing.com/search?q={quote_plus(query)}&setlang={quote_plus(language)}&cc={quote_plus(country)}&count={count}"
        if location:
            notes.append("Bing has no URL location parameter: the location is only implied by the words of the query")
        notes.append("results are what this browser session was shown")
        return u, notes
    raise ValueError("engine must be google or bing")


def _real_target(href: str) -> str:
    """Unwrap engine redirect links (Google /url?q=, Bing /ck/a?u=a1<base64url>)."""
    try:
        p = urlsplit(href)
    except ValueError:
        return href
    q = parse_qs(p.query)
    if (p.hostname or "").endswith("google.com") and p.path == "/url":
        return unquote((q.get("q") or q.get("url") or [href])[0])
    if (p.hostname or "").endswith("bing.com") and p.path.startswith("/ck/"):
        u = (q.get("u") or [""])[0]
        if u.startswith("a1"):
            b = u[2:] + "=" * (-len(u[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(b).decode("utf-8", "replace")
            except ValueError:
                return href
    return href


def _engine_url(u: str) -> bool:
    low = u.lower()
    h = (urlsplit(low).hostname or "")
    return any(e in h or e in low[:60] for e in ENGINE_HOSTS)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def detect_block(page_url: str, text: str) -> str | None:
    low = page_url.lower()
    if "/sorry/" in low or "captcha" in low:
        return "CAPTCHA"
    if any(h in low for h in CONSENT_HOSTS) or CONSENT_TEXT.search(text[:3000] or ""):
        return "CONSENT_REQUIRED"
    if BLOCK_TEXT.search((text or "")[:4000]):
        return "CAPTCHA"
    return None


def parse(engine: str, elements: list[dict], headings: list[dict], text: str, page_url: str, *, max_results: int = 10) -> dict:
    block = detect_block(page_url, text)
    if block:
        return {"blocked": block, "organic": [], "local_pack": {"detected": False}, "ads_detected": False, "method": "none"}
    heads = [_norm(h.get("text", "")) for h in headings if h.get("level") in (2, 3) and len(_norm(h.get("text", ""))) >= 6]
    organic: list[dict] = []
    seen: set[str] = set()
    method = "heading-match"
    # engines that wrap result links (and whose wrapped URL is cut off in the element list) show the site's domain as a separate link with the SAME href:
    # that label is the only trustworthy target we have, recorded as an inferred domain-only URL
    domain_label = {}
    for el in elements:
        n = (el.get("name") or "").strip().lower()
        if el.get("tag") == "a" and el.get("href") and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", n):
            domain_label.setdefault(el["href"], n)
    inferred = 0
    for el in elements:
        href = el.get("href") or ""
        if not href or el.get("tag") != "a":
            continue
        target = _real_target(href)
        if _engine_url(target) and href in domain_label and not _engine_url("https://" + domain_label[href]):
            target = "https://" + domain_label[href] + "/"
            inferred += 1
        if _engine_url(target) or not target.startswith("http"):
            continue
        name = _norm(el.get("name", ""))
        title = next((h for h in heads if name.startswith(h[:60])), None)
        if title is None:
            continue
        key = target.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        organic.append({"position": len(organic) + 1, "title": next((h.get("text") for h in headings if _norm(h.get("text", "")) == title), title)[:200], "url": target[:500],
                        "domain": host_key(target), **({"url_inferred": True} if href in domain_label and target.endswith(domain_label[href] + "/") else {})})
        if len(organic) >= max_results:
            break
    if not organic:   # fallback: external links with substantial names, one per host, flagged low-confidence
        method = "fallback-external-links"
        hosts: set[str] = set()
        for el in elements:
            target = _real_target(el.get("href") or "")
            if el.get("tag") != "a" or not target.startswith("http") or _engine_url(target) or len(el.get("name", "")) < 15:
                continue
            h = host_key(target)
            if h in hosts:
                continue
            hosts.add(h)
            organic.append({"position": len(organic) + 1, "title": el.get("name", "")[:200], "url": target[:500], "domain": h})
            if len(organic) >= max_results:
                break
    # map pack: per-business links (place pages, directions, local-finder parameters). The generic "Maps" tab link is NOT evidence of a pack.
    lp_links = [el for el in elements if el.get("tag") == "a" and re.search(r"(/maps/place/|/maps/dir/|[?&]tbm=lcl|[?&]lrd=|rllag=|ludocid=|[?&]lqi=)", el.get("href") or "")]
    names: list[str] = []
    for el in lp_links:
        href = el.get("href") or ""
        m = re.search(r"/maps/dir//([^/?]+)", href)       # directions links carry "Business Name, street, city": the most reliable name source, in pack order
        n = unquote(m.group(1).replace("+", " ")).split(",")[0].strip() if m else (el.get("name") or "").strip()
        if 3 <= len(n) <= 80 and n not in names and not re.match(r"(?i)^(more (places|businesses)|directions|website|call|view all|maps?|reviews?|address|hours|phone|menu|order|map of .*)$", n):
            names.append(n)
    ads = bool(re.search(r"\bsponsored\b|\bad\b ·", (text or "")[:6000], re.I)) or any("googleadservices" in (el.get("href") or "") or "/aclk" in (el.get("href") or "") for el in elements)
    return {"blocked": None, "organic": organic, "local_pack": {"detected": bool(lp_links), "names": names[:10], "order_source": "directions links (document order)" if names else ""},
            "ads_detected": ads, "method": method}


def in_pack(business: str, names: list[str]) -> bool:
    """Loose name match (punctuation/case-insensitive prefix): 'Example Towing & Lockout Service' ~ 'Examples Towing & Lockout Service Inc'."""
    key = re.sub(r"[^a-z0-9]", "", (business or "").lower())[:16]
    return bool(key) and any(re.sub(r"[^a-z0-9]", "", n.lower()).startswith(key) for n in names)


def classify(domain: str, client_domains: list[str], directories: tuple[str, ...] = DIRECTORIES) -> str:
    d = (domain or "").lower()
    if any(d == c or d.endswith("." + c) for c in client_domains if c):
        return "client"
    if any(x in d for x in directories):
        return "directory"
    return "competitor"
