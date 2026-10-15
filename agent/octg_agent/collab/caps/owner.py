"""owner.ask / owner.replies / owner.cancel: the AI -> owner direction. The collaborator posts "go here, do this, tell me what you see"; the owner answers in the Chrome side panel or the Hub inbox; the
collaborator reads the answer. Nothing here touches a browser, a site or an approval: it is a message channel with a strict shape (see collab/ownerio.py)."""
from __future__ import annotations

import asyncio
import time

from ..capabilities import A, B, I, S, capability
from ..store import CollabError

MAX_WAIT_S = 540
PAGE_TEXT_CHARS = 5000


def _view(svc, r: dict, *, with_page: bool = True) -> dict:
    """What the collaborator sees of a request: its own words, the owner's answer, and (when the owner attached them) the page text and where the screenshot is."""
    atts = []
    for a in r["attachments"]:
        item = {k: a.get(k) for k in ("kind", "evidence_id", "url", "title", "chars")}
        if with_page and a.get("kind") == "page_text":
            try:
                _ev, raw = svc.store.read_evidence(a["evidence_id"], max_bytes=PAGE_TEXT_CHARS * 4)
                item["text"] = raw.decode("utf-8", "replace")[:PAGE_TEXT_CHARS]
            except CollabError:
                item["text"] = None
        if a.get("kind") == "screenshot":
            try:
                item["path"] = svc.store.get_evidence(a["evidence_id"])["path"]
            except CollabError:
                item["path"] = None
        atts.append(item)
    return {"or_id": r["or_id"], "status": r["status"], "title": r["title"], "asked_at": r["created_at"], "answered_at": r["answered_at"], "related": r["related"] or None,
            "reply_text": r["reply_text"] or None, "reply_choice": r["reply_choice"] or None, "answered_via": r["answered_via"], "attachments": atts}


@capability("owner.ask", "Ask the OWNER to do something and report back: a title, 1-12 numbered steps (what to open, click or look at), what to tell you, optional quick-answer buttons, and optionally "
            "to attach the text and a screenshot of the tab they are on. It appears as a card in the owner's Chrome side panel (with a badge) and in the Hub Inbox. The owner's answer is read with "
            "owner.replies. Use it when only the owner can do or see something (a login, a verification screen, an error message). Never put passwords or secrets in it.",
            props={"title": S("What this is about, in a few words.", minLength=3, maxLength=140), "steps": A("Numbered steps for the owner, one action each (max 12).", minItems=1, maxItems=12),
                   "question": S("What you want the owner to tell you, e.g. 'What does the page say after you click Verify?'", maxLength=400),
                   "choices": A("Optional quick answers shown as buttons, e.g. ['It worked', 'I see an error'] (max 6).", maxItems=6),
                   "want_page": B("Ask the owner to attach the text and a screenshot of the tab they are on (they choose whether to)."),
                   "related": S("Optional label tying this to a task, loop or approval (shown to the owner).", maxLength=120)},
            required=("title", "steps"), output={"or_id": "the request id (read the answer with owner.replies id=...)", "status": "open", "shown_in": "where the owner will see it"},
            permissions=("owner:message",), scope="client_optional", read_only=False, category="owner", timeout_s=20,
            limitations=("The owner answers when they next open the side panel or the Hub: there is no deadline and the answer is not instant. Requests nobody answers for 14 days are closed.",))
async def owner_ask(ctx, p: dict) -> dict:
    r = ctx.svc.inbox.create(caller=ctx.caller, title=p["title"], steps=p["steps"], question=p.get("question", ""), choices=p.get("choices"), want_page=bool(p.get("want_page")),
                             related=p.get("related", ""), client_id=ctx.client_id, task_id=ctx.task_id)
    ctx.svc.notify_inbox()
    return {"or_id": r["or_id"], "status": "open", "shown_in": "the Chrome side panel (Inbox tab, with a badge on the toolbar icon) and the Hub's Inbox page"}


@capability("owner.replies", "Read the owner's answers to the requests THIS caller made with owner.ask: one request (id) or every answered one (optionally only since a time). With wait_s the call waits "
            "(up to 9 minutes) until the request is answered, which suits 'I just asked, the owner is doing it now'. Attachments carry the page text the owner chose to share and the path of a screenshot you can read.",
            props={"id": S("One owner request id (or_...). Omit to list.", maxLength=40), "status": S("open | answered | all (default answered)", enum=["open", "answered", "all"]),
                   "since": {"type": "number", "description": "Only requests asked or answered after this time (epoch seconds)."},
                   "wait_s": I("Wait up to this many seconds for an answer (0-540, default 0).", minimum=0, maximum=MAX_WAIT_S)},
            output={"requests": "[{or_id, status, title, reply_text, reply_choice, attachments:[{kind, url, title, text?, path?}], answered_at}]", "waited_s": "number", "still_open": "[or_id]"},
            permissions=("owner:message",), scope="client_optional", read_only=True, category="owner", timeout_s=120, max_timeout_s=MAX_WAIT_S + 60,
            limitations=("Only the requests this caller made are visible.", "Page text is what the owner chose to attach, with anything that looks like a secret removed."))
async def owner_replies(ctx, p: dict) -> dict:
    inbox = ctx.svc.inbox
    wait = float(p.get("wait_s") or 0)
    status = p.get("status", "answered")
    t0 = time.monotonic()

    def collect():
        if p.get("id"):
            r = inbox.get(p["id"])
            if r["created_by"] != ctx.caller:
                raise CollabError("NO_SUCH_REQUEST", f"no owner request {p['id']}", status=404)
            return [r]
        rows = inbox.list(caller=ctx.caller, client_id=ctx.client_id, since=p.get("since"), status=None if status == "all" else status, limit=50)
        return rows
    while True:
        rows = collect()
        done = [r for r in rows if r["status"] != "open"]
        if done or wait <= 0 or time.monotonic() - t0 >= wait:
            break
        ctx.check_cancel()
        ctx.progress(f"waiting for the owner ({int(time.monotonic() - t0)}s)")
        await asyncio.sleep(2.0)
    open_ids = [r["or_id"] for r in inbox.list(caller=ctx.caller, client_id=ctx.client_id, status="open", limit=50)]
    shown = rows if status != "answered" or p.get("id") else done
    if wait > 0 and not done:
        ctx.outcome.completeness = "none"
        ctx.limit("the owner has not answered yet")
    return {"requests": [_view(ctx.svc, r) for r in shown], "waited_s": round(time.monotonic() - t0, 1), "still_open": open_ids}


@capability("owner.cancel", "Withdraw one of your own open owner requests (it disappears from the owner's inbox).",
            props={"id": S("The owner request id (or_...).", minLength=4, maxLength=40)}, required=("id",), output={"status": "cancelled"}, permissions=("owner:message",),
            scope="client_optional", read_only=False, category="owner", timeout_s=15)
async def owner_cancel(ctx, p: dict) -> dict:
    r = ctx.svc.inbox.cancel(p["id"], caller=ctx.caller)
    ctx.svc.notify_inbox()
    return {"status": r["status"]}
