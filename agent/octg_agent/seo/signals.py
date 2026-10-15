"""Turn an HTML document into structured, model-neutral page signals (what a crawler sees in the raw HTML).

Stdlib only (html.parser). The page is DATA: nothing in it is ever executed or followed as an instruction; text is only measured and excerpted.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

SKIP_TEXT = {"script", "style", "noscript", "template", "svg", "head", "title"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
WS = re.compile(r"\s+")


def host_key(url_or_host: str) -> str:
    h = urlsplit(url_or_host if "://" in url_or_host else "//" + url_or_host).hostname or ""
    h = h.lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


class _P(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.lang = ""
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta: dict[str, list[str]] = {}
        self.links_head: list[dict] = []
        self.headings: list[dict] = []
        self._h: list | None = None
        self.anchors: list[dict] = []
        self._a: dict | None = None
        self.images: list[dict] = []
        self.jsonld_raw: list[str] = []
        self._script: dict | None = None
        self.text_parts: list[str] = []
        self.skip_depth = 0
        self.stack: list[str] = []
        self.forms = 0
        self.iframes: list[str] = []
        self.itemtypes: list[str] = []
        self.base_href = ""

    # ---- tags
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag not in VOID:
            self.stack.append(tag)
        if tag in SKIP_TEXT:
            self.skip_depth += 1
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"].strip()
        elif tag == "base" and a.get("href") and not self.base_href:
            self.base_href = urljoin(self.base, a["href"])
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("http-equiv") or ("charset" if "charset" in a else "")).lower().strip()
            if key:
                self.meta.setdefault(key, []).append((a.get("content") if key != "charset" else a.get("charset")) or "")
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if a.get("href"):
                self.links_head.append({"rel": rel, "href": urljoin(self.base_href or self.base, a["href"]), "hreflang": a.get("hreflang", ""), "type": a.get("type", "")})
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._h = [int(tag[1]), []]
        elif tag == "a":
            self._a = {"href": a.get("href", ""), "rel": a.get("rel", "").lower(), "text": [], "title": a.get("title", ""), "img_alt": []}
        elif tag == "img":
            alt = a["alt"] if "alt" in a else None
            self.images.append({"src": urljoin(self.base_href or self.base, a.get("src") or a.get("data-src") or ""), "alt": alt, "loading": a.get("loading", ""),
                                "width": a.get("width", ""), "height": a.get("height", "")})
            if self._a is not None and alt:
                self._a["img_alt"].append(alt)
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._script = {"buf": []}
        elif tag == "form":
            self.forms += 1
        elif tag == "iframe":
            self.iframes.append(a.get("src", "")[:300])
        if a.get("itemtype"):
            self.itemtypes.append(a["itemtype"][:200])

    def handle_endtag(self, tag):
        if tag in SKIP_TEXT and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._h is not None:
            self.headings.append({"level": self._h[0], "text": WS.sub(" ", " ".join(self._h[1])).strip()[:300]})
            self._h = None
        elif tag == "a" and self._a is not None:
            self.anchors.append(self._a)
            self._a = None
        elif tag == "script" and self._script is not None:
            self.jsonld_raw.append("".join(self._script["buf"]))
            self._script = None
        if self.stack and tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._script is not None:
            self._script["buf"].append(data)
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if self._h is not None:
            self._h[1].append(data)
        if self._a is not None:
            self._a["text"].append(data)
        if not self.skip_depth:
            self.text_parts.append(data)


def _types(obj, out: list[str], objs: list[dict], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, list):
        for x in obj:
            _types(x, out, objs, depth + 1)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t:
            for x in (t if isinstance(t, list) else [t]):
                out.append(str(x))
            objs.append(obj)
        for k, v in obj.items():
            if k != "@type" and isinstance(v, (dict, list)):
                _types(v, out, objs, depth + 1)


def extract(html: str, url: str, *, text_limit: int = 20000) -> dict:
    p = _P(url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML still yields what was parsed so far
        pass
    base = p.base_href or url
    site = host_key(url)
    meta = {k: v[0] for k, v in p.meta.items()}
    canon = [l["href"] for l in p.links_head if "canonical" in l["rel"]]
    alternates = [{"hreflang": l["hreflang"], "href": l["href"]} for l in p.links_head if "alternate" in l["rel"] and l["hreflang"]]
    links, tel, mail = [], [], []
    for a in p.anchors:
        href = (a["href"] or "").strip()
        text = WS.sub(" ", " ".join(a["text"])).strip() or " ".join(a["img_alt"]).strip() or a["title"]
        if href.lower().startswith("tel:"):
            tel.append(href[4:].strip())
            continue
        if href.lower().startswith("mailto:"):
            mail.append(href[7:].split("?")[0].strip())
            continue
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "data:")):
            continue
        absu = urldefrag(urljoin(base, href))[0]
        if urlsplit(absu).scheme not in ("http", "https"):
            continue
        links.append({"url": absu, "text": text[:160], "internal": host_key(absu) == site, "nofollow": "nofollow" in a["rel"]})
    jsonld_types: list[str] = []
    jsonld_objs: list[dict] = []
    jsonld_errors = 0
    for raw in p.jsonld_raw:
        try:
            _types(json.loads(raw.strip()), jsonld_types, jsonld_objs)
        except ValueError:
            jsonld_errors += 1
    text = WS.sub(" ", " ".join(p.text_parts)).strip()
    words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", text))
    imgs = p.images
    title = WS.sub(" ", "".join(p.title_parts)).strip()
    robots = ",".join(x.lower() for x in (p.meta.get("robots", []) + p.meta.get("googlebot", [])))
    return {
        "url": url,
        "lang": p.lang,
        "title": title,
        "title_length": len(title),
        "meta_description": (meta.get("description") or "").strip(),
        "meta_description_length": len((meta.get("description") or "").strip()),
        "meta_robots": robots,
        "noindex": "noindex" in robots or "none" in robots.split(","),
        "nofollow_meta": "nofollow" in robots,
        "viewport": meta.get("viewport", ""),
        "canonical": canon[0] if canon else "",
        "canonical_count": len(canon),
        "og": {k[3:]: v for k, v in meta.items() if k.startswith("og:")},
        "hreflang": alternates,
        "headings": p.headings[:200],
        "h1": [h["text"] for h in p.headings if h["level"] == 1],
        "links_internal": [l for l in links if l["internal"]][:600],
        "links_external": [l for l in links if not l["internal"]][:300],
        "internal_link_count": sum(1 for l in links if l["internal"]),
        "external_link_count": sum(1 for l in links if not l["internal"]),
        "tel_links": sorted(set(tel))[:20],
        "mailto_links": sorted(set(mail))[:20],
        "images": len(imgs),
        "images_missing_alt": sum(1 for i in imgs if i["alt"] is None),
        "images_empty_alt": sum(1 for i in imgs if i["alt"] == ""),
        "images_missing_alt_src": [i["src"] for i in imgs if i["alt"] is None][:30],
        "jsonld_types": sorted(set(jsonld_types)),
        "jsonld_errors": jsonld_errors,
        "jsonld": jsonld_objs[:20],
        "microdata_types": sorted(set(p.itemtypes))[:20],
        "forms": p.forms,
        "iframes": p.iframes[:20],
        "map_embed": any("google.com/maps" in s or "maps.google" in s for s in p.iframes),
        "word_count": words,
        "text_excerpt": text[:600],
        "text": text[:text_limit],
    }


def brief(sig: dict) -> dict:
    """The signals without bulky lists: what a caller needs to reason about one page."""
    drop = {"text", "links_internal", "links_external", "jsonld", "images_missing_alt_src"}
    return {k: v for k, v in sig.items() if k not in drop}


def parse_robots(text: str) -> dict:
    """robots.txt -> rules for '*' (and Googlebot), plus sitemaps. Minimal, enough to answer "is this path blocked for a general crawler"."""
    groups: list[dict] = []
    cur: dict | None = None
    sitemaps: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "sitemap":
            sitemaps.append(v)
        elif k == "user-agent":
            if cur is None or cur["rules"]:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(v.lower())
        elif k in ("allow", "disallow") and cur is not None:
            cur["rules"].append((k, v))
    def rules_for(agent: str) -> list:
        for g in groups:
            if agent in g["agents"]:
                return g["rules"]
        for g in groups:
            if "*" in g["agents"]:
                return g["rules"]
        return []
    return {"sitemaps": sitemaps, "star": rules_for("*"), "googlebot": rules_for("googlebot"), "groups": len(groups)}


def robots_allows(rules: list, path: str) -> bool:
    """Longest-match wins; Allow wins ties (Google's documented behaviour). '*' and '$' wildcards supported."""
    best, allow = -1, True
    for kind, pat in rules:
        if kind == "disallow" and pat == "":
            continue
        rx = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\$", "$")
        if re.match(rx, path):
            ln = len(pat)
            if ln > best or (ln == best and kind == "allow"):
                best, allow = ln, kind == "allow"
    return allow


def parse_sitemap(xml: str) -> dict:
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml, flags=re.I)
    is_index = bool(re.search(r"<sitemapindex", xml, flags=re.I))
    lastmods = re.findall(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", xml, flags=re.I)
    return {"index": is_index, "locs": [l.replace("&amp;", "&") for l in locs], "lastmod_count": len(lastmods)}
