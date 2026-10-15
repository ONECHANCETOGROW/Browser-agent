"""Turn a plain sentence like "go to Google and uh, search for painters in Alabama and search for best painters in Alabama. Tell me what you find." into the list of searches to run.

Deliberately rule-based and conservative: it only returns what the owner actually asked to search for, and when it cannot find any it says so (the Hub then tells the owner how to phrase it) rather than guessing.
"""
from __future__ import annotations

import re

MAX_QUERIES = 10
FILLER = re.compile(r"\b(?:uh+|um+|erm+|er|hmm+|you know)\b[,.]?", re.I)
QUOTED = re.compile(r"[\"“”‘'](?=\S)([^\"“”]{3,120}?)(?<=\S)[\"“”’']")
# "search for X", "search X", "look up X", "google X" (but not "go to Google and ...")
VERB = re.compile(r"(?:\bsearch(?:ing|es)?(?:\s+(?:for|up))?|\blook(?:ing)?\s*up|(?<!\bto )(?<!\bon )(?<!\bin )(?<!\bat )\bgoogle(?!\s+(?:and|then|for|search|to)\b))\s+", re.I)
REPORT_WORDS = r"(?:search|google|look\s*up|tell|report|let|give|come|show|check|also|then|send|write|summar|list|compare|and\s+then)"
STOP = re.compile(r"\s*(?:[.;!?\n]|,\s+(?:and\s+)?" + REPORT_WORDS + r"\b|\s+and\s+(?:then\s+)?" + REPORT_WORDS + r"\b|\s+then\b|\s+and\s+(?:what|how|where|which)\b)", re.I)
TRAIL = re.compile(r"\s+(?:on|in|using|with|via)\s+google(?:\s+chrome)?\s*$|\s+google\s*$", re.I)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")


def _clean(q: str) -> str:
    q = re.sub(r"\s+", " ", q).strip(" \t\"“”‘’'.,;:!?-–")
    q = re.sub(r"^(?:for|up|about)\s+", "", q, flags=re.I)
    q = TRAIL.sub("", q).strip(" .,;:")
    return q


def parse_search_request(text: str) -> dict:
    """{"queries": [...], "notes": [...]}: the searches the sentence asks for, in order, de-duplicated, at most MAX_QUERIES."""
    raw = (text or "").strip()
    notes: list[str] = []
    if not raw:
        return {"queries": [], "notes": ["nothing was written"]}
    clean = FILLER.sub(" ", raw)
    found: list[str] = []
    has_verb = bool(VERB.search(clean))
    if has_verb:
        bullets = [m.group(1) for line in clean.splitlines() if (m := BULLET.match(line))]
        if bullets:
            found += bullets
        else:
            quoted = QUOTED.findall(clean)
            if quoted:
                found += quoted
            else:
                for m in VERB.finditer(clean):
                    rest = clean[m.end():]
                    stop = STOP.search(rest)
                    found.append(rest[:stop.start()] if stop else rest)
    queries: list[str] = []
    for q in (_clean(x) for x in found):
        if 3 <= len(q) <= 120 and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)
    if len(queries) > MAX_QUERIES:
        notes.append(f"only the first {MAX_QUERIES} searches are run at a time ({len(queries) - MAX_QUERIES} left out)")
        queries = queries[:MAX_QUERIES]
    if not queries:
        notes.append("I could not find what to search for. Write it like: search for painters in Alabama" if has_verb or raw else "nothing was written")
    return {"queries": queries, "notes": notes}
