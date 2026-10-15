"""Google Business Profile reviews page (business.google.com/reviews): reading it as text and finding ONE review on it.

The page lists reviews of every business on the account, newest first, 10 per page. A review is a block of lines:

    <business name>
    <address>
    [Store code: ...]
    <reviewer>
     <time>                  (e.g. "5 days ago")
    <review text> | "The user didn't write a review, and has left just a rating."
    [(owner)  <time>  <the owner's reply>  Edit  Delete]        <- replied
    [reply  Reply]                                              <- not replied

Pure functions only (no browser): collab/caps/gbp.py drives the page.
"""
from __future__ import annotations

import re

REVIEWS_URL = "https://business.google.com/reviews"
RANGE = re.compile(r"(\d+)\s*[–-]\s*(\d+) of (\w+)")
_ADDR = re.compile(r", (United States|Canada|India|USA|United Kingdom|Australia|New Zealand|Ireland)$")
_NO_TEXT = "The user didn't write a review"
_ICONS = re.compile(r"[-]")


def page_range(lines: list[str] | str) -> str | None:
    text = lines if isinstance(lines, str) else "\n".join(lines)
    m = RANGE.search(text)
    return m.group(0) if m else None


def parse_reviews(lines: list[str] | str) -> list[dict]:
    """Reviews on the page in page order. Each: {biz, reviewer, when, text, replied, owner_reply, rating_only}."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    lines = [l.rstrip() for l in lines]
    start = next((i + 1 for i, l in enumerate(lines) if l.strip() == "sort"), 0)
    end = next((i for i, l in enumerate(lines) if l.startswith("Rows per page")), len(lines))
    body = lines[start:end]
    starts = [i - 1 for i in range(1, len(body)) if _ADDR.search(body[i].strip()) and body[i - 1].strip() and not body[i - 1].startswith(("Store code", "(owner)"))]
    out = []
    for k, i in enumerate(starts):
        blk = body[i:starts[k + 1] if k + 1 < len(starts) else len(body)]
        rest = blk[2:]
        if rest and rest[0].startswith("Store code"):
            rest = rest[1:]
        if not rest:
            continue
        reviewer, when_raw, content = rest[0].strip(), (rest[1].strip() if len(rest) > 1 else ""), [x.strip() for x in rest[2:] if x.strip()]
        when = _ICONS.sub("", when_raw).strip()
        owner_at = next((j for j, x in enumerate(content) if x == "(owner)"), None)
        review_text = " ".join(content[:owner_at] if owner_at is not None else content)
        review_text = re.sub(r"\s*(\.\.\. )?More\s*(reply Reply)?$|\s*reply Reply$", "", review_text).strip()
        owner_reply = ""
        if owner_at is not None:
            tail = [x for x in content[owner_at + 1:] if x not in ("Edit", "Delete")]
            owner_reply = " ".join(tail[1:]) if len(tail) > 1 else ""
        out.append({"biz": blk[0].strip(), "reviewer": reviewer, "when": when, "text": review_text, "replied": owner_at is not None, "owner_reply": owner_reply,
                    "rating_only": review_text.startswith(_NO_TEXT)})
    return out


def review_key(r: dict) -> tuple:
    return (r["biz"].casefold(), r["reviewer"].casefold(), re.sub(r"\W+", "", r["text"].casefold())[:80])


def find_review(reviews: list[dict], *, business: str, reviewer: str, excerpt: str) -> list[int]:
    """Indexes of the reviews on the page that match all three: business (contains), reviewer (exact), excerpt (the first words of the review text, ignoring case and punctuation)."""
    b, who, ex = business.strip().casefold(), reviewer.strip().casefold(), re.sub(r"\W+", "", excerpt.casefold())
    hits = []
    for i, r in enumerate(reviews):
        if b in r["biz"].casefold() and r["reviewer"].casefold() == who and ex and ex in re.sub(r"\W+", "", r["text"].casefold()):
            hits.append(i)
    return hits


def summarize(reviews: list[dict]) -> dict:
    un = [r for r in reviews if not r["replied"]]
    by: dict[str, int] = {}
    for r in un:
        by[r["biz"]] = by.get(r["biz"], 0) + 1
    return {"reviews": len(reviews), "unreplied": len(un), "unreplied_by_business": dict(sorted(by.items(), key=lambda kv: -kv[1]))}
