"""gbp.reviews / gbp.reply_check / gbp.reply_post: Google Business Profile reviews through the eye (collab/caps/eye.py), in the owner's real, logged-in Chrome.

gbp.reviews     READ ONLY. Pages through business.google.com/reviews (every business on the account, newest first) and returns the counts and the list of reviews that have no owner reply.
gbp.reply_check Opens the Reply box of ONE named review, verifies it is the right one and the box is the right box, then closes it again WITHOUT writing or posting anything.
gbp.reply_post  Posts an approved public reply to ONE review (external effect: the job waits for the owner to approve the exact text).
"""
from __future__ import annotations

import asyncio
import re

from ..capabilities import A, B, I, S, capability
from ..eye import diff_lines, next_button
from ..gbp import REVIEWS_URL, find_review, page_range, parse_reviews, review_key, summarize
from ..store import CollabError
from .eye import _act, _call, _refresh, _session, eye_open

REPLY_BTN = re.compile(r"^(?:reply\s+)?reply$", re.I)
COMPOSER, POST_BTN, CANCEL_BTN = "Your reply", "Post reply", "Cancel"
SESSION_MAX_STEPS = 400                      # a long review list is one click per page; the global 80-action limit would cut it short
LIST_LIMIT = 200


def _id_num(el: dict) -> int:
    m = re.search(r"(\d+)$", str(el.get("id") or ""))
    return int(m.group(1)) if m else 0


async def _open(ctx, name: str) -> dict:
    await eye_open(ctx, {"url": REVIEWS_URL, "session": name, "max_controls": 0, "text_chars": 200, "wait_ms": 2500})
    s = _session(ctx, name)
    s["max_steps"] = SESSION_MAX_STEPS
    return s


async def _next_page(ctx, s: dict) -> bool:
    """Press the page's own Next and wait until the list really changed. False = no more pages (or it would not advance)."""
    nb = next_button(s["elements"])
    if nb is None:
        return False
    before = page_range(s["lines"])
    res = await _act(ctx, s, "click", {"target": nb["id"], "wait_ms": 1200})
    if not res.get("acted"):
        return False
    for _ in range(5):
        if page_range(s["lines"]) != before:
            return True
        await _call(ctx, "wait", {"tab_id": s["tab_id"], "ms": 1000})
        await _refresh(ctx, s)
    return False


@capability("gbp.reviews", "READ ONLY. Read the owner's Google Business Profile reviews page (all businesses on the account, newest first) in their Chrome and return how many reviews have NO owner reply, "
            "per business, with the unreplied reviews listed (business, reviewer, when, text). It only reads and turns pages: it never replies, edits or deletes anything.",
            props={"max_pages": I("Stop after this many pages of 10 reviews (default 60, max 300).", minimum=1, maximum=300), "business": S("Only list reviews of businesses whose name contains this text.", maxLength=120),
                   "only_unreplied": B("List only the unreplied reviews (default true)."), "text_chars": I("Characters of each review to return (default 200).", minimum=40, maximum=1000)},
            output={"reviews_read": "int", "unreplied": "int", "unreplied_by_business": "{business: n}", "unreplied_reviews": "[{business, reviewer, when, text}]", "complete": "bool", "pages_read": "int"},
            permissions=("browser:act",), scope="client_optional", read_only=False, category="eye", needs_browser=True, timeout_s=600, max_timeout_s=1800,
            limitations=("Reads the page as text; reviews on pages it did not reach are not counted (complete=false says so).", "Needs the Chrome extension connected and the owner signed in to Business Profile Manager."))
async def gbp_reviews(ctx, p: dict) -> dict:
    name = "gbp-reviews"
    s = await _open(ctx, name)
    seen: dict[tuple, dict] = {}
    pages, complete, note = 0, False, ""
    try:
        while pages < int(p.get("max_pages", 60)):
            await _refresh(ctx, s)
            rows = parse_reviews(s["lines"])
            if not rows and pages == 0:
                raise CollabError("NO_REVIEWS_PAGE", "the reviews page showed no reviews: is the owner signed in to Business Profile Manager?", {"url": s["url"], "title": s["title"]})
            for r in rows:
                seen.setdefault(review_key(r), r)
            pages += 1
            ctx.progress(f"page {pages}: {len(seen)} reviews, {sum(1 for r in seen.values() if not r['replied'])} unreplied")
            try:
                if not await _next_page(ctx, s):
                    complete = next_button(s["elements"]) is None
                    note = "" if complete else "the Next button did not advance the list"
                    break
            except CollabError as e:
                note = f"stopped: {e.message}"
                break
    finally:
        await ctx.svc.eye.close(ctx.caller, name)
    reviews = list(seen.values())
    want = (p.get("business") or "").casefold()
    if want:
        reviews = [r for r in reviews if want in r["biz"].casefold()]
    sm = summarize(reviews)
    tc = int(p.get("text_chars", 200))
    listing = [{"business": r["biz"], "reviewer": r["reviewer"], "when": r["when"], "text": r["text"][:tc]} for r in reviews if p.get("only_unreplied", True) is False or not r["replied"]]
    ctx.source("chrome", REVIEWS_URL)
    out = {"reviews_read": sm["reviews"], "unreplied": sm["unreplied"], "unreplied_by_business": sm["unreplied_by_business"], "unreplied_reviews": listing[:LIST_LIMIT], "complete": complete, "pages_read": pages}
    if len(listing) > LIST_LIMIT:
        out["list_truncated_at"] = LIST_LIMIT
    if not complete:
        out["note"] = (note or f"stopped at the max_pages limit ({pages} pages)") + ": reviews further back are not counted"
    return out


# ---------------------------------------------------------------------------------------------- finding one review and its Reply box
async def locate_review(ctx, s: dict, *, business: str, reviewer: str, excerpt: str, max_pages: int = 80) -> tuple[dict, dict]:
    """Walk the pages until exactly one review matches business + reviewer + excerpt; leave the session ON that page. Returns (review, its Reply button element)."""
    for page in range(max_pages):
        await _refresh(ctx, s)
        rows = parse_reviews(s["lines"])
        hits = find_review(rows, business=business, reviewer=reviewer, excerpt=excerpt)
        if len(hits) > 1:
            raise CollabError("AMBIGUOUS_REVIEW", f"{len(hits)} reviews on one page match that business, reviewer and excerpt: give a longer excerpt", {"page": page_range(s["lines"])})
        if hits:
            rev = rows[hits[0]]
            if rev["replied"]:
                raise CollabError("ALREADY_REPLIED", "that review already has an owner reply", {"owner_reply": rev["owner_reply"][:200]})
            buttons = sorted((e for e in s["elements"].values() if REPLY_BTN.match(str(e.get("name") or "").strip()) and "button" in (e.get("role"), e.get("tag"))), key=_id_num)
            unreplied = [r for r in rows if not r["replied"]]
            if len(buttons) != len(unreplied):
                raise CollabError("PAGE_MISMATCH", f"the page shows {len(unreplied)} unreplied reviews but {len(buttons)} Reply buttons: refusing to guess which button belongs to which review", {"page": page_range(s["lines"])})
            return rev, buttons[[id(r) for r in unreplied].index(id(rev))]
        if not await _next_page(ctx, s):
            break
    raise CollabError("REVIEW_NOT_FOUND", "no review matched that business, reviewer and excerpt in the pages read (check the spelling, or it may be older than max_pages)")


async def _click_el(ctx, s: dict, el: dict) -> None:
    """A deliberate click on ONE known element of the session's page (the caller has already checked it is the right one). Site rules still apply after the page changes."""
    await _call(ctx, "click", {"tab_id": s["tab_id"], "element_id": el["id"], "snapshot_id": s["snapshot_id"]}, timeout_s=45)
    s["steps"] += 1
    await _call(ctx, "wait", {"tab_id": s["tab_id"], "ms": 1200})
    await _refresh(ctx, s)


def _by_name(s: dict, name: str, *, roles: tuple[str, ...]) -> dict | None:
    for e in s["elements"].values():
        if str(e.get("name") or "").strip() == name and any(r in roles for r in (e.get("role"), e.get("tag"))):
            return e
    return None


def _composer(s: dict) -> dict:
    return {"box": _by_name(s, COMPOSER, roles=("textbox", "textarea")), "post": _by_name(s, POST_BTN, roles=("button",)), "cancel": _by_name(s, CANCEL_BTN, roles=("button",))}


def _norm(t: str) -> str:
    return re.sub(r"\W+", "", t.casefold())


def _composer_belongs_to(s: dict, rev: dict) -> bool:
    """True when exactly one Reply box is open and the lines just above it are THIS review (reviewer's name and the start of the review text)."""
    idx = [i for i, l in enumerate(s["lines"]) if l.strip() == COMPOSER]
    if len(idx) != 1:
        return False
    above = _norm(" ".join(s["lines"][max(0, idx[0] - 14):idx[0]]))
    return _norm(rev["reviewer"]) in above and _norm(rev["text"])[:25] in above


async def _open_composer(ctx, s: dict, rev: dict, btn: dict) -> dict:
    await _click_el(ctx, s, btn)
    c = _composer(s)
    if not (c["box"] and c["post"] and c["cancel"]):
        raise CollabError("NO_REPLY_BOX", "pressing Reply did not open the expected Reply box (a 'Your reply' field with Post reply and Cancel)", {"controls": [e.get("name") for e in list(s["elements"].values())[-12:]]})
    if not _composer_belongs_to(s, rev):
        await _close_composer(ctx, s)
        raise CollabError("WRONG_REVIEW", "the Reply box that opened is not directly under the review that was asked for: nothing was typed or posted")
    return c


async def _close_composer(ctx, s: dict) -> bool:
    c = _composer(s)
    if c["cancel"]:
        try:
            await _click_el(ctx, s, c["cancel"])
        except CollabError:
            return False
    return _composer(s)["box"] is None


async def _type_into(ctx, s: dict, box: dict, text: str) -> dict:
    await _call(ctx, "type", {"tab_id": s["tab_id"], "element_id": box["id"], "snapshot_id": s["snapshot_id"], "text": text, "clear": True}, timeout_s=45)
    s["steps"] += 1
    await _call(ctx, "wait", {"tab_id": s["tab_id"], "ms": 600})
    await _refresh(ctx, s)
    return _composer(s)


def _typed_text(s: dict, c: dict) -> str | None:
    """What the Reply box now holds, if the page lets us read it back (the element's value, or the box's text on the page)."""
    box = c["box"] or {}
    for k in ("value", "text", "content"):
        if isinstance(box.get(k), str):
            return box[k]
    return None


@capability("gbp.reply_check", "Open the Reply box of ONE named review on the owner's Google Business Profile, prove it is the right review's box, then close it with Cancel WITHOUT posting anything. Use it to prove you have the "
            "right review before asking the owner to approve a reply. Needs business, reviewer and the first words of the review text. Optionally types a harmless test string to prove typing works, then cancels (nothing is posted).",
            props={"business": S("Business name as on the page (a contiguous part is enough).", minLength=2, maxLength=120), "reviewer": S("Reviewer's name exactly as shown.", minLength=1, maxLength=120),
                   "review_excerpt": S("The first words of the review text as shown (at least 12 characters), or \"The user didn't write a review\" for a rating-only review.", minLength=12, maxLength=300),
                   "type_test": B("Also type a short test string into the box and read it back before cancelling (default false)."), "max_pages": I("Pages to search (default 80).", minimum=1, maximum=300)},
            required=("business", "reviewer", "review_excerpt"), output={"found": "bool", "review": "{business, reviewer, when, text}", "box_is_under_this_review": "bool", "closed": "bool"},
            permissions=("browser:act",), scope="client_optional", read_only=False, category="eye", needs_browser=True, timeout_s=300, max_timeout_s=900,
            limitations=("It opens and closes the Reply box: nothing is saved or posted.",))
async def gbp_reply_check(ctx, p: dict) -> dict:
    name = "gbp-reply"
    s = await _open(ctx, name)
    try:
        rev, btn = await locate_review(ctx, s, business=p["business"], reviewer=p["reviewer"], excerpt=p["review_excerpt"], max_pages=int(p.get("max_pages", 80)))
        c = await _open_composer(ctx, s, rev, btn)
        out = {"found": True, "review": {"business": rev["biz"], "reviewer": rev["reviewer"], "when": rev["when"], "text": rev["text"][:300]}, "box_is_under_this_review": True}
        try:
            if p.get("type_test"):
                c = await _type_into(ctx, s, c["box"], "(test - not posted)")
                out["typed_back"] = _typed_text(s, c)
                out["box_element_fields"] = sorted((c["box"] or {}).keys())
        finally:
            out["closed"] = await _close_composer(ctx, s)
        return out
    finally:
        await ctx.svc.eye.close(ctx.caller, name)


# ---------------------------------------------------------------------------------------------- posting (owner approval required)
@capability("gbp.reply_post", "POST a public owner reply to ONE named Google review (it is shown publicly on Google under the business). Waits for the owner to approve this exact text in the Knowledge Hub. "
            "Finds the review by business + reviewer + excerpt, refuses if more than one matches or it already has a reply, checks the Reply box that opens is directly under that review, types the approved text, "
            "presses Post reply and then checks that the reply now shows under the review. Run gbp.reply_check first.",
            props={"business": S("Business name as on the page.", minLength=2, maxLength=120), "reviewer": S("Reviewer's name exactly as shown.", minLength=1, maxLength=120),
                   "review_excerpt": S("The first words of the review text as shown (at least 12 characters).", minLength=12, maxLength=300),
                   "reply_text": S("The exact reply to post (plain text, no more than 4000 characters).", minLength=5, maxLength=4000), "max_pages": I("Pages to search (default 80).", minimum=1, maximum=300)},
            required=("business", "reviewer", "review_excerpt", "reply_text"), output={"posted": "bool", "verified": "bool", "review": "{...}", "reply_seen_on_page": "str"},
            permissions=("browser:act", "web:post"), scope="client_optional", read_only=False, external_effects=True, approval="required", approval_kind="gbp_reply",
            category="eye", needs_browser=True, timeout_s=300, max_timeout_s=900,
            limitations=("A posted reply is public. It can be edited or deleted afterwards on Google, but not unseen.", "Reads the result back from the page: if the page does not show the reply, verified=false and nothing is retried."))
async def gbp_reply_post(ctx, p: dict) -> dict:
    name, text = "gbp-reply", p["reply_text"].strip()
    s = await _open(ctx, name)
    try:
        rev, btn = await locate_review(ctx, s, business=p["business"], reviewer=p["reviewer"], excerpt=p["review_excerpt"], max_pages=int(p.get("max_pages", 80)))
        c = await _open_composer(ctx, s, rev, btn)
        try:
            c = await _type_into(ctx, s, c["box"], text)
            back = _typed_text(s, c)
            if back is not None and _norm(back) != _norm(text):
                raise CollabError("TYPED_TEXT_DIFFERS", "the Reply box does not hold exactly the approved text: nothing was posted", {"box_holds": back[:300]})
            if not c["post"] or not _composer_belongs_to(s, rev):
                raise CollabError("WRONG_REVIEW", "the Reply box moved or is no longer under the requested review: nothing was posted")
        except CollabError:
            await _close_composer(ctx, s)
            raise
        await _click_el(ctx, s, c["post"])                  # the one irreversible click: the owner approved exactly this text for exactly this review
        await _call(ctx, "wait", {"tab_id": s["tab_id"], "ms": 2000})
        await _refresh(ctx, s)
        seen, verified = "", False
        for r in parse_reviews(s["lines"]):
            if find_review([r], business=p["business"], reviewer=p["reviewer"], excerpt=p["review_excerpt"]) and r["replied"]:
                seen = r["owner_reply"]
                verified = _norm(text)[:25] in _norm(seen)
        ctx.evidence("gbp_reply", {"business": rev["biz"], "reviewer": rev["reviewer"], "review": rev["text"][:300], "reply": text, "verified": verified, "page": page_range(s["lines"])}, source_url=s["url"],
                     summary=f"replied to {rev['reviewer']} on {rev['biz']}" + ("" if verified else " (NOT confirmed on the page)"))
        out = {"posted": True, "verified": verified, "review": {"business": rev["biz"], "reviewer": rev["reviewer"], "when": rev["when"], "text": rev["text"][:300]}, "reply_seen_on_page": seen[:400]}
        if not verified:
            out["warning"] = "Post reply was pressed but the reply was not found under the review afterwards: check it on Google before doing anything else. It was NOT retried."
        return out
    finally:
        await ctx.svc.eye.close(ctx.caller, name)
