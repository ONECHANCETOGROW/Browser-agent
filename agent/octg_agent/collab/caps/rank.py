"""seo.rank_check: a read-only, paced, location-faithful rank check from a keyword file (spec: RANKCHECK-BUILD-SPEC.md, input schema octg.rankcheck.input/1).

What makes it different from seo.serp_observe: every URL is opened BYTE FOR BYTE as supplied (the `uule` parameter is what makes Google answer as if the searcher stood in the client's city), the page
that actually loaded is checked to still carry it, searches are paced 20-45 s apart, the whole run stops at the first CAPTCHA/consent wall (never solved, never retried, never replaced by another engine),
the local finder is read for rank AND review counts, and every run is stored so later runs can be compared. `null` always means "not observed"; 0 or 11 are never used for "not found".
Nothing here is client-specific: domain, brand, location and keywords all come from the input.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlsplit

from ...seo import serp as SERP
from ...seo.signals import host_key
from ..capabilities import A, O, S, capability
from ..store import CollabError
from .browser import BROWSER_LIMITS, _need_browser, _ok
from .core import require_client

SCHEMA_IN = "octg.rankcheck.input/1"
SCHEMA_OUT = "octg.rankcheck.result/1"
PACE = (20.0, 45.0)              # seconds between any two Google pages of one run. Not a parameter: a caller cannot switch the safeguard off.
GOOGLE_HOSTS = ("www.google.com", "google.com")
MAX_KEYWORDS = 30
TOP_N = 10
SECOND_LEVEL = {"co.uk", "org.uk", "com.au", "co.nz", "co.in", "com.br", "co.za", "com.mx", "co.jp"}


# ---------------------------------------------------------------------------------------------- small helpers
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def registrable(host_or_url: str) -> str:
    """example.com for www.example.com / blog.example.com / https://example.com/x (a small second-level-TLD list covers co.uk and friends)."""
    h = host_key(host_or_url or "")
    parts = h.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _uule_of(url: str) -> str | None:
    m = re.search(r"[?&]uule=([^&#]+)", url or "")
    return unquote(m.group(1)).replace(" ", "+") if m else None


# ---------------------------------------------------------------------------------------------- the input
def parse_markdown(text: str) -> list[dict]:
    """FALLBACK ONLY: `## <n>. <keyword>` headings with `- **Web results:** <url>` / `- **Map results:** <url>` bullets beneath."""
    out: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        h = re.match(r"^##\s+\d+\.\s+(.+?)\s*$", line)
        if h:
            cur = {"keyword": h.group(1).strip().strip("`*"), "web_url": "", "map_url": ""}
            out.append(cur)
            continue
        m = re.match(r"^\s*[-*]\s+\*\*(Web|Map) results:\*\*\s*(.+)$", line, re.I)
        if m and cur is not None:
            u = re.search(r"https?://[^\s)>\]`]+", m.group(2))
            if u:
                cur["web_url" if m.group(1).lower() == "web" else "map_url"] = u.group(0)
    return out


def _check_url(url, uule: str, *, want_map: bool, keyword: str, label: str) -> list[str]:
    if not isinstance(url, str) or not url:
        return [f"{label}: missing"]
    bad = []
    if len(url) > 2000 or re.search(r"[\s\x00-\x1f]", url):
        return [f"{label}: not a plain URL (whitespace, control characters or too long)"]
    sp = urlsplit(url)
    if sp.scheme != "https" or (sp.hostname or "") not in GOOGLE_HOSTS or sp.path != "/search" or sp.username or sp.port:
        bad.append(f"{label}: must be an https://www.google.com/search URL")
        return bad
    if not re.search(r"(?:^|&)uule=" + re.escape(uule) + r"(?:&|$)", sp.query):
        bad.append(f"{label}: does not carry the location (uule) given in locale.uule, verbatim")
    q = dict(parse_qsl(sp.query, keep_blank_values=True))
    tbm = q.get("tbm")
    if want_map and tbm != "lcl":
        bad.append(f"{label}: the map URL must have tbm=lcl")
    if not want_map and tbm:
        bad.append(f"{label}: the web URL must not have tbm")
    if _norm(q.get("q", "")) != _norm(keyword):
        bad.append(f"{label}: its q= ({q.get('q', '')!r}) is not the keyword ({keyword!r})")
    return bad


def prepare(p: dict) -> tuple[dict, list[str]]:
    """Validate the input and return the normalised document. The URLs are never rewritten; they are only checked."""
    problems: list[str] = []
    if p.get("schema") not in (None, SCHEMA_IN):
        problems.append(f"schema must be {SCHEMA_IN}")
    raw = p.get("keywords")
    from_md = False
    if not raw and p.get("markdown"):
        raw, from_md = parse_markdown(p["markdown"]), True      # the JSON is authoritative: Markdown is read only when there is no JSON keyword list
    if not isinstance(raw, list) or not raw:
        problems.append("give a keywords list (schema octg.rankcheck.input/1) or, as a fallback, the Markdown text")
        raw = []
    if len(raw) > MAX_KEYWORDS:
        problems.append(f"at most {MAX_KEYWORDS} keywords per run")
    site = registrable(urlsplit(p["site"] if "://" in str(p.get("site") or "") else "//" + str(p.get("site") or "")).hostname or "")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", site):
        problems.append("site must be the client's domain, e.g. example.com")
    brand = str(p.get("brand_name") or "").strip()
    if not brand or len(brand) > 120:
        problems.append("brand_name is required (it is how the map listing is recognised)")
    loc = p.get("locale") if isinstance(p.get("locale"), dict) else {}
    uule = str(loc.get("uule") or "").strip()
    if not uule and from_md and raw and raw[0].get("web_url"):
        uule = _uule_of(raw[0]["web_url"]) or ""
    if not uule:
        problems.append("locale.uule is required: without it Google answers for wherever this computer is, and the whole run would be wrong")
    kws, seen = [], set()
    for i, k in enumerate(raw, 1):
        if not isinstance(k, dict) or not isinstance(k.get("keyword"), str) or not k["keyword"].strip():
            problems.append(f"keyword {i}: needs a keyword text")
            continue
        kw = k["keyword"].strip()
        if _norm(kw) in seen:
            problems.append(f"keyword {i}: {kw!r} appears twice")
        seen.add(_norm(kw))
        if uule:
            problems += _check_url(k.get("web_url"), uule, want_map=False, keyword=kw, label=f"keyword {i} ({kw}) web_url")
            problems += _check_url(k.get("map_url"), uule, want_map=True, keyword=kw, label=f"keyword {i} ({kw}) map_url")
        kws.append({"keyword": kw, "target_page": k.get("target_page") if isinstance(k.get("target_page"), str) else None, "web_url": k.get("web_url"), "map_url": k.get("map_url")})
    only = p.get("only")
    if only:
        want = {_norm(x) for x in only}
        unknown = sorted(want - {_norm(k["keyword"]) for k in kws})
        if unknown:
            problems.append("only: not in the keyword list: " + ", ".join(unknown))
        kws = [k for k in kws if _norm(k["keyword"]) in want]
    return {"client": p.get("client") or "", "site": site, "brand_name": brand, "locale": {k: loc.get(k) for k in ("city", "region", "country") if loc.get(k)} | {"uule": uule}, "keywords": kws,
            "subset": bool(only), "source": "markdown" if from_md else "json"}, problems


# ---------------------------------------------------------------------------------------------- reading the local finder
_RATING_INLINE = re.compile(r"(?<![\d.])([1-5][.,]\d)\s*[\(\[]\s*([\d.,]+\s*[KkMm]?)\s*[\)\]]")
_RATING_ONLY = re.compile(r"^([1-5][.,]\d)$")
_REVIEWS_ONLY = re.compile(r"^[\(\[]\s*([\d.,]+\s*[KkMm]?)\s*[\)\]]$|^([\d.,]+\s*[KkMm]?)\s+(?:google\s+)?reviews?$", re.I)
_NOT_A_NAME = re.compile(r"^(open|closed|opens|closes|open 24 hours|website|directions|call|hours|more|save|share|filters?|sort by|rating|all filters|sponsored|ads?|results?|local results?|"
                         r"search results?|map data.*|no reviews?|dine-in|delivery|takeout|on-site services|online estimates|online appointments|\d+\+? years? in business)$", re.I)
_SPONSORED = re.compile(r"^(sponsored|ads?)$", re.I)


def _count(s: str) -> int | None:
    s = s.strip().replace(" ", "")
    m = re.fullmatch(r"([\d.,]+)([KkMm]?)", s)
    if not m:
        return None
    num, unit = m.groups()
    try:
        if unit:
            return int(round(float(num.replace(",", ".")) * (1000 if unit.lower() == "k" else 1_000_000)))
        return int(num.replace(",", "").replace(".", ""))
    except ValueError:
        return None


def _plain(line: str) -> bool:
    return 3 <= len(line) <= 90 and not _NOT_A_NAME.match(line) and not _RATING_ONLY.match(line) and not _REVIEWS_ONLY.match(line) and " · " not in line and not re.match(r"^[\d(+]", line)


def parse_local_finder(headings: list[dict], text: str) -> list[dict]:
    """The businesses of a Google local-finder page in the order shown: [{position|None (sponsored), name, rating|None, review_count|None, sponsored}]. Text-driven and deliberately conservative:
    a name is only taken from the line (or heading) that sits right before its rating, and a figure that cannot be read stays None."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    heads = {_norm(h.get("text", "")) for h in headings if h.get("level") in (2, 3, 4)}
    found: list[dict] = []
    seen: set[str] = set()

    def previous_name(i: int) -> str | None:
        window = [lines[j] for j in range(i - 1, max(i - 5, -1), -1)]     # nearest line first
        for l in window[:2]:                                              # a heading right next to the rating is certainly the name...
            if _norm(l) in heads and _plain(l):
                return l
        return next((l for l in window if _plain(l)), None)               # ...otherwise the nearest plain line (never a name from further back)

    def emit(i: int, name: str | None, rating: str, reviews: int | None) -> None:
        if not name or _norm(name) in seen:
            return
        seen.add(_norm(name))
        near = lines[max(i - 4, 0):i + 3]
        found.append({"name": name, "rating": float(rating.replace(",", ".")), "review_count": reviews, "sponsored": any(_SPONSORED.match(x) for x in near)})

    for i, line in enumerate(lines):
        m = _RATING_INLINE.search(line)
        if m:
            prefix = line[:m.start()].strip(" ·•⋅-–|")
            emit(i, prefix if _plain(prefix) else previous_name(i), m.group(1), _count(m.group(2)))
            continue
        if _RATING_ONLY.match(line):
            rev = None
            for nxt in lines[i + 1:i + 3]:
                r = _REVIEWS_ONLY.match(nxt)
                if r:
                    rev = _count(r.group(1) or r.group(2))
                    break
            emit(i, previous_name(i), line, rev)
    pos = 0
    for b in found:
        if b["sponsored"]:
            b["position"] = None
        else:
            pos += 1
            b["position"] = pos
    return found


def _features(parsed: dict, text: str, headings: list[dict]) -> list[str]:
    f = []
    if parsed["local_pack"]["detected"]:
        f.append("map_pack")
    t = text or ""
    if re.search(r"People also ask", t, re.I):
        f.append("people_also_ask")
    if re.search(r"Related searches|People also search for", t, re.I):
        f.append("related_searches")
    if re.search(r"\bAI Overview", t, re.I):
        f.append("ai_overview")
    if parsed.get("ads_detected"):
        f.append("ads")
    heads = {_norm(h.get("text", "")) for h in headings}
    if heads & {"videos", "short videos"}:
        f.append("video_carousel")
    if "images" in heads:
        f.append("images")
    return f


# ---------------------------------------------------------------------------------------------- one page
async def _pause(ctx, seconds: float) -> None:
    end = time.monotonic() + seconds
    while True:
        ctx.check_cancel()
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(1.0, left))


async def _pace(ctx, waits: list[float], what: str) -> None:
    s = random.uniform(*PACE)
    waits.append(round(s, 1))
    ctx.progress(f"pacing: waiting {s:.0f}s {what}")
    await _pause(ctx, s)


async def _read(ctx, url: str, uule: str | None) -> dict:
    """Open `url` unchanged in a new background tab, read it, close the tab. Nothing is clicked or typed. With uule=None no location is required of the loaded page."""
    tab = await ctx.open_tab(url)
    await ctx.browser("wait", {"tab_id": tab, "ms": 1500})
    st = _ok(await ctx.browser("get_page_state", {"tab_id": tab}), "reading the results page")
    page = st.get("page") or st
    rd = _ok(await ctx.browser("read_page", {"tab_id": tab, "scope": "page", "max_chars": 20000}), "reading the results text")
    ins = _ok(await ctx.browser("inspect_page", {"tab_id": tab, "max_elements": 800, "viewport_only": False}), "listing result links")
    observed = time.time()
    final_url = rd.get("url") or page.get("url") or ""
    text, headings, elements = rd.get("text", ""), page.get("headings") or [], ins.get("elements") or []
    blocked = SERP.detect_block(final_url, text)
    shot = None
    if not blocked:
        sh = await ctx.browser("screenshot", {"tab_id": tab, "format": "jpeg", "quality": 70})
        if sh.get("success") and (sh.get("result") or {}).get("path"):
            from pathlib import Path
            shot = ctx.evidence("screenshot", Path(sh["result"]["path"]).read_bytes(), source_url=url, summary="results page", ext="jpg")
    await ctx.close_tabs()
    on_google = (urlsplit(final_url).hostname or "") in GOOGLE_HOSTS and urlsplit(final_url).path == "/search"
    kept = uule is None or _uule_of(final_url) == uule
    return {"requested_url": url, "final_url": final_url, "observed_at": observed, "text": text, "headings": headings, "elements": elements, "blocked": blocked, "screenshot": shot,
            "location_kept": bool(on_google and kept), "on_google": on_google}


def _evidence_ref(ctx, eid: str | None) -> str | None:
    return ctx.svc.store.get_evidence(eid)["path"] if eid else None


# ---------------------------------------------------------------------------------------------- history
def _previous(ctx, cid: str, uule: str) -> tuple[dict, dict]:
    """Newest comparable (same location) earlier reading per keyword: web: {kw: (run_id, entry)}, map: {kw: (run_id, entry)}."""
    st = ctx.svc.store
    web, mp = {}, {}
    rows = sorted(st.artifacts(client_id=cid, kind="analysis", limit=80), key=lambda r: r["created_at"], reverse=True)
    for r in rows:
        if not str(r["title"]).startswith("Rank check"):
            continue
        body = st.get_artifact(r["artifact_id"])["body"] or {}
        if body.get("schema") != SCHEMA_OUT or (body.get("locale_used") or {}).get("uule") != uule:
            continue
        for e in body.get("results", []):
            k = _norm(e.get("keyword", ""))
            if e.get("web_status") == "read" and k not in web:
                web[k] = (body["run_id"], e)
            if e.get("map_status") == "read" and k not in mp:
                mp[k] = (body["run_id"], e)
    return web, mp


def _movement(prev, cur) -> str:
    if prev is None and cur is None:
        return "absent then and now"
    if prev is None:
        return "newly ranked"
    if cur is None:
        return "dropped out of the first 10"
    return "up" if cur < prev else "down" if cur > prev else "same"


# ---------------------------------------------------------------------------------------------- report
def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return None if den == 0 else num / den


def _delta(d, present: bool) -> str:
    if not present:
        return "-"
    return "n/a" if d is None else ("=" if d == 0 else f"{d:+d}")


def render(doc: dict, listings: list[tuple[int, int]]) -> str:
    L = [f"# Rank check — {doc['brand_name']} ({doc['site']})", "",
         f"Run `{doc['run_id']}` · {doc['started_at']} to {doc['finished_at']} · **{doc['status']}** · {doc['completed']} of {doc['total']} keywords finished · location verified: {doc['locale_used']['verified']}", ""]
    if doc.get("stopped_reason"):
        L += [f"**Stopped:** {doc['stopped_reason']}. The results below are what was gathered before that; nothing was retried or replaced.", ""]
    L += ["| Keyword | Web pos | Δ | Map pos | Δ | Top competitor | Their reviews |", "|---|---|---|---|---|---|---|"]
    for r in doc["results"]:
        web = "not run" if r["web_status"] == "not_run" else (r["position"] if r["web_status"] == "read" and r["position"] else ("absent (top 10)" if r["web_status"] == "read" else f"unavailable ({r['web_status']})"))
        mp = "not run" if r["map_status"] == "not_run" else (r["map_position"] if r["map_status"] == "read" and r["map_position"] else ("absent (top 10)" if r["map_status"] == "read" else f"unavailable ({r['map_status']})"))
        top = (r.get("top_competitor") or {})
        L.append(f"| {r['keyword']} | {web} | {_delta(r.get('position_delta'), 'previous_position' in r)} | {mp} | {_delta(r.get('map_position_delta'), 'previous_map_position' in r)} | "
                 f"{top.get('name') or '-'} | {top.get('review_count') if top.get('review_count') is not None else '-'} |")
    L += ["", "_Δ = previous position minus current (positive = moved up). `absent (top 10)` is a measured absence from the first 10; `unavailable` means it could not be read._", ""]
    read = [r for r in doc["results"] if r["web_status"] == "read"]
    absent = [r["keyword"] for r in read if r["position"] is None]
    L.append("**Absent from the first 10 organic results:** " + (", ".join(absent) if absent else "none of the keywords read.") if read else "**Absent from the first 10 organic results:** no web results could be read.")
    rho = _spearman([(float(a), float(b)) for a, b in listings]) if len(listings) >= 8 else None
    if rho is None:
        L.append(f"**Map position and reviews:** too few listings with a review count ({len(listings)}) to say whether they go together.")
    else:
        strength = "more reviews go with a better map position" if rho <= -0.3 else "fewer reviews go with a better map position" if rho >= 0.3 else "no clear relationship"
        L.append(f"**Map position and reviews:** Spearman rho {rho:+.2f} over {len(listings)} listings: {strength}. A correlation across these searches, not proof of cause.")
    pages = [f"{r['keyword']} ({r['target_page']})" for r in read if r["position"] is None and r.get("target_page") and "map pack only" not in r["target_page"].lower() and "gbp" not in r["target_page"].lower()]
    L.append("**Target pages absent from the first 10 for their own term:** " + (", ".join(pages) + ". Absence from ten results does not prove a page is unindexed." if pages else "none."))
    notes = [f"- {r['keyword']}: {r['notes']}" for r in doc["results"] if r.get("notes")]
    if notes:
        L += ["", "**Notes**", ""] + notes
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------- the capability
@capability("seo.rank_check", "Read-only rank check for ONE client from a keyword file (octg.rankcheck.input/1: site, brand_name, locale.uule and, per keyword, a web_url and a map_url). Opens every URL "
            "exactly as given in the agent's browser, 20-45 s apart, records the client's organic position, the SERP features present, the local-finder position and the review counts of the "
            "businesses above the client, stores the run, and compares it with earlier runs. Stops at the first CAPTCHA/consent wall (never solved, never retried, no other engine). "
            "Pass the JSON file's content as parameters (collab run seo.rank_check --json FILE --client NAME --wait 1800).",
            props={"schema": S("octg.rankcheck.input/1"), "client": S("A label for the client (informational; the call's client decides whose data it is).", maxLength=200),
                   "site": S("The client's domain, matched on registrable domain.", maxLength=200), "brand_name": S("The business name as it appears in the map listing.", maxLength=120),
                   "locale": O("{city, region, country, uule}: uule is required and must appear verbatim in every URL."),
                   "keywords": A("[{keyword, web_url, map_url, target_page?, rationale?}] (max 30)", items={"type": "object"}, maxItems=MAX_KEYWORDS),
                   "markdown": S("FALLBACK only: the Markdown keyword file's text, used when no keywords list is given.", maxLength=60000),
                   "only": A("Run only these keywords of the list (e.g. to finish a run that was stopped).", maxItems=MAX_KEYWORDS)},
            output={"run": "octg.rankcheck.result/1", "markdown": "report", "artifact_id": "stored run", "json_path": "file", "markdown_path": "file"},
            permissions=("search:read", "research:write", "client:read", "collab:write"), scope="client", read_only=False, category="seo", needs_browser=True, timeout_s=2400, max_timeout_s=3600,
            limitations=BROWSER_LIMITS + ("Positions are what this browser was shown at that moment (location emulated through the supplied uule), not universal rankings.",
                                          "Knowledge panels are not detected; the other SERP features are detected from the page text and headings.",
                                          "Local-finder names, ratings and review counts are read from the page text and are None when they cannot be read."))
async def rank_check(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    doc_in, problems = prepare(p)
    if problems:
        raise CollabError("BAD_INPUT", "the rank-check input is not valid (nothing was opened): " + "; ".join(problems[:6]) + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""), {"problems": problems})
    _need_browser(ctx)
    site, brand, uule = doc_in["site"], doc_in["brand_name"], doc_in["locale"]["uule"]
    prev_web, prev_map = _previous(ctx, cid, uule)
    started = time.time()
    run_id = "rc_" + datetime.fromtimestamp(started, timezone.utc).strftime("%Y%m%d_%H%M")
    folder = ctx.svc.store.reports_dir / cid
    base_id, n = run_id, 1
    while (folder / f"rankcheck-{run_id}.json").exists():     # two runs in one minute never share an id
        n += 1
        run_id = f"{base_id}_{n}"
    results: list[dict] = []
    waits: list[float] = []
    listings: list[tuple[int, int]] = []
    pages_read, location_ok = 0, True
    stopped: str | None = None
    status_override: str | None = None
    todo = doc_in["keywords"]

    def blank(k: dict) -> dict:
        return {"keyword": k["keyword"], "target_page": k["target_page"], "checked_at": None, "web_status": "not_run", "position": None, "found_url": None, "top3": [], "serp_features": [],
                "map_status": "not_run", "map_position": None, "competitors_above": [], "client_map": None, "top_competitor": None,
                "evidence": {"web_screenshot": None, "map_screenshot": None, "web_page": None, "map_page": None}, "notes": None}
    results = [blank(k) for k in todo]
    try:
        first_page = True
        for idx, k in enumerate(todo):
            r = results[idx]
            notes: list[str] = []
            for kind in ("web", "map"):
                if not first_page:
                    await _pace(ctx, waits, "before the next search")
                first_page = False
                ctx.check_cancel()
                ctx.progress(f"{kind} search {idx + 1}/{len(todo)}: {k['keyword']}")
                try:
                    pg = await _read(ctx, k[f"{kind}_url"], uule)
                except CollabError as e:
                    if e.code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED"):
                        raise
                    await ctx.close_tabs()
                    r[f"{kind}_status"] = "error"      # this keyword fails loudly; nothing else is searched in its place
                    notes.append(f"{kind}: the URL could not be opened or read ({e.code}); no substitute search was made")
                    continue
                pages_read += 1
                ev = ctx.evidence("serp", {"kind": kind, "requested_url": pg["requested_url"], "final_url": pg["final_url"], "observed_at": pg["observed_at"], "headings": pg["headings"],
                                           "links": [{x: e.get(x) for x in ("name", "href")} for e in pg["elements"] if e.get("href")][:400], "text": pg["text"]},
                                  source_url=pg["final_url"] or pg["requested_url"], summary=f"google {kind} '{k['keyword']}'")
                ctx.source("google", pg["requested_url"], pg["observed_at"])
                r["checked_at"] = _iso(pg["observed_at"])
                r["evidence"][f"{kind}_page"] = _evidence_ref(ctx, ev)
                r["evidence"][f"{kind}_screenshot"] = _evidence_ref(ctx, pg["screenshot"])
                if pg["blocked"]:
                    r[f"{kind}_status"] = "blocked"
                    notes.append(f"{kind}: Google showed a {pg['blocked']} page; the run was stopped and nothing was retried or bypassed")
                    stopped = f"Google showed a {pg['blocked']} page at keyword {idx + 1} of {len(todo)} ({kind} search for '{k['keyword']}')"
                    status_override = "blocked"
                    break
                if not pg["location_kept"]:
                    location_ok = False
                    r[f"{kind}_status"] = "location_lost"
                    notes.append(f"{kind}: the page that loaded does not carry the supplied location (uule), so its results were not used" + ("" if pg["on_google"] else " (it is not a Google results page)"))
                    continue
                if kind == "web":
                    parsed = SERP.parse("google", pg["elements"], pg["headings"], pg["text"], pg["final_url"], max_results=TOP_N)
                    r["serp_features"] = _features(parsed, pg["text"], pg["headings"])
                    if parsed["method"] != "heading-match" or not parsed["organic"]:
                        r["web_status"] = "unreadable"
                        notes.append("web: the results page could not be read reliably (no organic results identified), so no position is recorded")
                        continue
                    organic = parsed["organic"][:TOP_N]
                    hit = next((o for o in organic if registrable(o["domain"]) == site), None)
                    r.update(web_status="read", position=hit["position"] if hit else None, found_url=hit["url"] if hit else None, top3=[registrable(o["domain"]) for o in organic[:3]])
                    if hit and hit.get("url_inferred"):
                        notes.append("web: the engine wrapped the result link, so only the domain is known (found_url is domain-only)")
                else:
                    biz = parse_local_finder(pg["headings"], pg["text"])[:TOP_N + 3]
                    biz = [b for b in biz if b["position"] is None or b["position"] <= TOP_N]
                    if not biz:
                        r["map_status"] = "unreadable"
                        notes.append("map: no businesses could be read from the local-finder page, so no map position is recorded")
                        continue
                    ranked = [b for b in biz if b["position"] is not None]
                    mine = next((b for b in ranked if SERP.in_pack(brand, [b["name"]])), None)
                    above = [b for b in ranked if mine is None or b["position"] < mine["position"]]
                    r.update(map_status="read", map_position=mine["position"] if mine else None,
                             competitors_above=[{"name": b["name"], "rating": b["rating"], "review_count": b["review_count"]} for b in above],
                             client_map={"name": mine["name"], "rating": mine["rating"], "review_count": mine["review_count"]} if mine else None)
                    r["top_competitor"] = next(({"name": b["name"], "rating": b["rating"], "review_count": b["review_count"]} for b in ranked if b is not mine), None)
                    listings += [(b["position"], b["review_count"]) for b in ranked if b["review_count"] is not None]
                    if any(b["review_count"] is None for b in above):
                        notes.append("map: a review count could not be read for some listings above the client (left null)")
                    if any(b["sponsored"] for b in biz):
                        notes.append("map: sponsored listings were left out of the ranking")
            r["notes"] = "; ".join(notes) or None
            if stopped:
                break
    except CollabError as e:
        if e.code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED"):
            stopped = f"the Chrome extension disconnected ({e.code}); the run stopped with what had been gathered"
        else:
            raise
    finally:
        await ctx.close_tabs()
    finished = time.time()
    for r in results:   # deltas against the newest comparable earlier reading of the same keyword
        k = _norm(r["keyword"])
        if r["web_status"] == "read" and k in prev_web:
            rid, old = prev_web[k]
            r.update(previous_position=old["position"], position_delta=(old["position"] - r["position"]) if old["position"] and r["position"] else None, previous_run_id=rid,
                     movement=_movement(old["position"], r["position"]))
        if r["map_status"] == "read" and k in prev_map:
            rid, old = prev_map[k]
            r.update(previous_map_position=old["map_position"], map_position_delta=(old["map_position"] - r["map_position"]) if old["map_position"] and r["map_position"] else None,
                     previous_map_run_id=rid, map_movement=_movement(old["map_position"], r["map_position"]))
    complete = all(r["web_status"] == "read" and r["map_status"] == "read" for r in results)
    status = status_override or ("complete" if complete and not stopped else "partial")
    doc = {"schema": SCHEMA_OUT, "client": doc_in["client"] or ctx.client_id, "site": site, "brand_name": brand, "run_id": run_id, "started_at": _iso(started), "finished_at": _iso(finished), "status": status,
           "engine": "google", "locale_used": {**{k: v for k, v in doc_in["locale"].items()}, "verified": bool(pages_read and location_ok and not any(r["web_status"] == "location_lost" or r["map_status"] == "location_lost" for r in results))},
           "subset": doc_in["subset"], "total": len(results), "completed": sum(1 for r in results if r["web_status"] == "read" and r["map_status"] == "read"), "stopped_reason": stopped,
           "first_run": not (prev_web or prev_map), "pacing": {"min_s": PACE[0], "max_s": PACE[1], "waits_s": waits}, "results": results}
    md = render(doc, listings)
    folder.mkdir(parents=True, exist_ok=True)
    jp, mp_ = folder / f"rankcheck-{run_id}.json", folder / f"rankcheck-{run_id}.md"
    jp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    mp_.write_text(md, encoding="utf-8")
    art = ctx.svc.store.add_artifact(task_id=ctx.task_id, client_id=cid, kind="analysis", stage="research", title=f"Rank check {run_id}: {site} ({status})", body=doc, body_md=md,
                                     links={"request_id": ctx.request_id, "files": [str(jp), str(mp_)], "evidence": ctx.evidence_ids[-80:]}, created_by=f"agent:{ctx.caller}")
    rlog = ctx.admin().research
    sess = rlog.start_session(cid, f"Rank check {run_id}", search_engine="google", actor=f"collab:{ctx.caller}")
    place = ", ".join(str(doc_in["locale"].get(x)) for x in ("city", "region", "country") if doc_in["locale"].get(x)) or "uule location"
    for r in results:
        when = datetime.strptime(r["checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if r["checked_at"] else finished
        if r["web_status"] == "read":
            rlog.add_observation(sess["session_id"], "keyword_rank", query=r["keyword"], location=place, search_engine="google", result_type="organic", position=r["position"], url=r["found_url"] or "",
                                 domain=site, business=brand, evidence=f"rank check {run_id}; " + (f"organic #{r['position']}" if r["position"] else f"not in the first {TOP_N} organic results"),
                                 source="browser:google", data={"run_id": run_id, "request_id": ctx.request_id, "top3": r["top3"], "serp_features": r["serp_features"]}, observed_at=when, actor=f"collab:{ctx.caller}")
        if r["map_status"] == "read":
            rlog.add_observation(sess["session_id"], "local_pack_result", query=r["keyword"], location=place, search_engine="google", result_type="local_finder", position=r["map_position"],
                                 business=brand, evidence=f"rank check {run_id}; " + (f"local finder #{r['map_position']}" if r["map_position"] else f"not in the first {TOP_N} local-finder results"),
                                 source="browser:google", data={"run_id": run_id, "request_id": ctx.request_id, "competitors_above": r["competitors_above"]}, observed_at=when, actor=f"collab:{ctx.caller}")
    rlog.finish_session(sess["session_id"], findings=f"{doc['completed']} of {doc['total']} keywords read ({status})", actor=f"collab:{ctx.caller}")
    if status != "complete":
        ctx.outcome.completeness = "partial" if doc["completed"] or any(r["web_status"] == "read" or r["map_status"] == "read" for r in results) else "none"
    if stopped:
        ctx.limit(stopped)
    for r in results:
        if r["notes"]:
            ctx.limit(f"{r['keyword']}: {r['notes']}")
    ctx.outcome.confidence = "medium"
    return {"run": doc, "markdown": md, "artifact_id": art["artifact_id"], "json_path": str(jp), "markdown_path": str(mp_)}
