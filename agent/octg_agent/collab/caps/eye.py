"""eye.open / eye.look / eye.act / eye.table / eye.close / eye.status: the AI collaborator's eyes and (careful) hands in the owner's real, logged-in Chrome.

One dedicated tab per session; compact answers (a digest, then only what changed); a gate that refuses anything that looks like it changes something. See collab/eye.py for the rules. Typical use:

    eye.open url=https://search.google.com/search-console/performance/search-analytics?resource_id=...   -> digest (tables as rows, a short controls list with refs)
    eye.act do=click target="Average position"                                                         -> what changed
    eye.act do=click target="Impressions"                                                              -> the table re-sorted
    eye.table all_pages=true max_rows=25                                                               -> the rows, exactly as shown
    eye.close
"""
from __future__ import annotations

import asyncio
import re
import time

from ...engine import risk as risk_mod
from ...engine.registry import ACTIONS
from ..capabilities import A, B, I, S, capability
from ..store import CollabError
from ..eye import (MAX_SESSIONS_PER_CALLER, MAX_STEPS, PAGE_INFO, EyeError, clean_lines, compact_controls, diff_lines, gate_action, human_check, next_button, parse_tables, resolve_target)
from .browser import _need_browser

EXTRA_MUTATING = re.compile(r"\b(save|save changes|update|apply changes|create|add (?:user|owner|property)|request indexing|validate fix|start new|disavow|mark as|resend|reply|comment|upload|turn (?:on|off)|enable|disable)\b", re.I)
ENGINE_ACTION = {"click": "click", "type": "type", "select": "select", "check": "check", "uncheck": "uncheck", "scroll": "scroll", "key": "press_key", "back": "back", "reload": "reload"}
DIGEST_NOTE = "refs (el_###) belong to the latest look/act of this session; they change after every action, so use the label or the newest ref."


def _fail(e: EyeError) -> CollabError:
    return CollabError(e.code, e.message, e.details or None)


async def _call(ctx, action: str, params: dict, timeout_s: float | None = None) -> dict:
    resp = await ctx.svc.rt.dispatcher.execute(action, params, source=f"eye:{ctx.caller}", timeout_s=timeout_s)
    if not resp.get("success"):
        err = resp.get("error") or {}
        code = err.get("code") or "BROWSER_ERROR"
        raise CollabError(code, f"{action}: {err.get('message') or code}", {"result": resp.get("result")} if resp.get("result") else None,
                          retryable=code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED", "ACTION_TIMEOUT"))
    return resp


def _eyes(ctx):
    _need_browser(ctx)
    return ctx.svc.eye


def _instance(ctx) -> str:
    """Which Chrome copy (profile/window) is connected right now. Tab ids only mean something inside the browser that made them."""
    sess = getattr(ctx.svc.rt, "session", None)
    return str((getattr(sess, "hello", None) or {}).get("instance") or "")


def _forget_if_other_browser(ctx, name: str) -> bool:
    """A different browser connected since this session's tab was opened: the tab is not ours any more. Drop the session (its tab belongs to the other browser) and say so."""
    s = ctx.svc.eye.get(ctx.caller, name)
    if s is not None and s.get("instance") != _instance(ctx):
        ctx.svc.eye.items.pop(ctx.svc.eye.key(ctx.caller, name), None)
        return True
    return False


def _session(ctx, name: str, *, must_exist: bool = True) -> dict | None:
    if _forget_if_other_browser(ctx, name):
        raise CollabError("BROWSER_CHANGED", "a different Chrome (another profile or window) connected since this session was opened, so its tab is gone: open the page again with eye.open")
    s = _eyes(ctx).get(ctx.caller, name)
    if s is None and must_exist:
        raise CollabError("NO_SESSION", f"there is no eye session {name!r}: open one with eye.open url=... (sessions end after 10 idle minutes or an agent restart)")
    return s


def _check_url(ctx, url: str) -> None:
    try:
        ctx.svc.eye.policy.check(url)
    except EyeError as e:
        raise _fail(e) from None


async def _refresh(ctx, s: dict) -> None:
    """Read the tab again: cleaned text, the elements with fresh refs, the url. If the page is now on a blocked site, go back and say so."""
    tab = s["tab_id"]
    rd = (await _call(ctx, "read_page", {"tab_id": tab, "scope": "page", "max_chars": 60000}))["result"]
    ins = (await _call(ctx, "inspect_page", {"tab_id": tab, "max_elements": 500}))["result"]
    s.update(url=rd.get("url") or s["url"], title=rd.get("title") or "", lines=clean_lines(rd.get("text", "")), snapshot_id=ins.get("snapshot_id"), elements={e["id"]: e for e in ins.get("elements", [])})
    try:
        ctx.svc.eye.policy.check(s["url"])
    except EyeError as e:
        if e.code == "SITE_BLOCKED":
            try:
                await _call(ctx, "back", {"tab_id": tab})
            except CollabError:
                pass
            raise _fail(e) from None


def _digest(s: dict, *, controls: bool = True, max_controls: int = 30, text_chars: int = 2500, table_rows: int = 30, links: bool = False) -> dict:
    lines = s["lines"]
    tables = sorted(parse_tables(lines), key=lambda t: -len(t["rows"]))[:3]
    skip = set()
    for t in tables:
        skip.update(range(t["start"], t["end"]))
    text = "\n".join(l for i, l in enumerate(lines) if i not in skip)
    more = len(text) - text_chars
    if more > 0:
        text = text[:text_chars] + f"\n…(+{more} more characters: raise text_chars, or use find)"
    page_info = PAGE_INFO.search("\n".join(lines))
    out = {"url": s["url"], "title": s["title"], "text": text,
           "tables": [{"title": t["title"], "header": t["header"], "rows": t["rows"][:table_rows], "row_count": len(t["rows"])} for t in tables]}
    if page_info:
        out["page_info"] = page_info.group(0)
    check = human_check(s["title"], lines)
    if check:
        out["human_check"] = check
        out["hint"] = ("the site refused this connection (a block page): passing a check will not help; try another source or ask the owner" if check == "blocked"
                       else "a check that only a person can pass: call eye.wait_human (it asks the owner and waits). Never try to get past it yourself")
    if controls:
        out["controls"] = compact_controls(list(s["elements"].values()), max_controls, links=links)
        out["note"] = DIGEST_NOTE
    out["steps_used"] = s["steps"]
    return out


def _touch(s: dict) -> None:
    s["last_used"] = time.time()


# ---------------------------------------------------------------------------------------------- open / look
@capability("eye.open", "Open a page in YOUR dedicated tab in the owner's real, logged-in Chrome (one tab per session, never the owner's own tabs) and return a compact digest: cleaned text, tables as rows, and a short "
            "list of controls with refs. Reuses the session's tab if it exists, so sorts and filters you set stay set. Refused on the owner's blocked sites (banks, payments, e-mail, passwords, account security).",
            props={"url": S("http(s) page to open.", minLength=4, maxLength=2000), "session": S("Session name (default 'main'; up to 3 sessions).", maxLength=30), "wait_ms": I("Extra time for the page to finish drawing (default 1500).", minimum=0, maximum=15000),
                   "max_controls": I("How many controls to list (default 30).", minimum=0, maximum=100), "text_chars": I("How much page text to return (default 2500).", minimum=200, maximum=12000),
                   "links": B("Also list links (menus are left out by default).")},
            required=("url",), output={"url": "", "title": "", "text": "cleaned visible text", "tables": "[{header, rows, row_count}]", "controls": "[{ref, role, name}]"},
            permissions=("browser:act",), scope="client_optional", read_only=False, category="eye", needs_browser=True, timeout_s=90, max_timeout_s=180,
            limitations=("Needs the Chrome extension connected.", "Runs in the owner's Chrome profile (their sign-ins): results can differ from what a customer sees.", "Pages the extension cannot script (chrome://, the Web Store) cannot be read."))
async def eye_open(ctx, p: dict) -> dict:
    eyes = _eyes(ctx)
    name = p.get("session", "main")
    url = p["url"] if "://" in p["url"] else "https://" + p["url"]
    _check_url(ctx, url)
    _forget_if_other_browser(ctx, name)
    s = eyes.get(ctx.caller, name)
    load_unconfirmed = False
    if s is None:
        if eyes.count(ctx.caller) >= MAX_SESSIONS_PER_CALLER:
            raise CollabError("TOO_MANY_SESSIONS", f"at most {MAX_SESSIONS_PER_CALLER} eye sessions at once: close one with eye.close")
        try:
            r = await _call(ctx, "new_tab", {"url": url, "active": False, "timeout_ms": 30000})
        except CollabError as e:
            if e.code != "POSTCONDITION_FAILED" or not (e.details or {}).get("result"):
                raise
            r = {"result": e.details["result"]}      # the tab opened but Chrome did not report 'finished loading' in time (common on app pages): carry on, the text is read below
            load_unconfirmed = True
        tid = ((r.get("result") or {}).get("tab") or {}).get("tab_id") or (r.get("result") or {}).get("tab_id")
        if not isinstance(tid, int):
            raise CollabError("BROWSER_ERROR", "the extension did not report the new tab's id")
        s = eyes.add(ctx.caller, name, tid, ctx.client_id)
        s["instance"] = _instance(ctx)
    else:
        try:
            await _call(ctx, "navigate", {"url": url, "tab_id": s["tab_id"], "timeout_ms": 30000})
        except CollabError as e:
            if e.code != "POSTCONDITION_FAILED":
                raise
            load_unconfirmed = True
    s["steps"] += 1
    s["last_was_type"] = False
    await _call(ctx, "wait", {"tab_id": s["tab_id"], "ms": int(p.get("wait_ms", 1500))})
    await _refresh(ctx, s)
    _touch(s)
    ctx.source("chrome", s["url"])
    out = _digest(s, max_controls=p.get("max_controls", 30), text_chars=p.get("text_chars", 2500), links=bool(p.get("links")))
    if load_unconfirmed:
        out["load_confirmed"] = False
        out["load_note"] = "Chrome did not report the page as fully loaded in time; the text above is what was readable. eye.look wait_ms=3000 to read again."
    return out


@capability("eye.look", "Look at the session's current page again without changing anything: the compact digest (cleaned text, tables as rows, controls with refs). Optionally find words in the page and get the lines around them.",
            props={"session": S("Session name (default 'main').", maxLength=30), "controls": B("Include the controls list (default true)."), "max_controls": I("Controls to list (default 30).", minimum=0, maximum=100), "links": B("Also list links (menus are left out by default)."),
                   "text_chars": I("Text to return (default 2500).", minimum=200, maximum=12000), "table_rows": I("Rows per table (default 30).", minimum=0, maximum=300),
                   "find": A("Words to look for: returns the matching lines (max 3 each).", maxItems=5), "wait_ms": I("Wait this long first, e.g. for a page still loading.", minimum=0, maximum=15000)},
            output={"url": "", "text": "", "tables": "[]", "controls": "[]", "found": "{word: [lines]}"}, permissions=("browser:act",), scope="client_optional", read_only=False, category="eye",
            needs_browser=True, timeout_s=60)
async def eye_look(ctx, p: dict) -> dict:
    s = _session(ctx, p.get("session", "main"))
    if p.get("wait_ms"):
        await _call(ctx, "wait", {"tab_id": s["tab_id"], "ms": int(p["wait_ms"])})
    await _refresh(ctx, s)
    _touch(s)
    out = _digest(s, controls=p.get("controls", True), max_controls=p.get("max_controls", 30), text_chars=p.get("text_chars", 2500), table_rows=p.get("table_rows", 30), links=bool(p.get("links")))
    if p.get("find"):
        out["found"] = {w: [l[:200] for l in s["lines"] if w.lower() in l.lower()][:3] for w in p["find"]}
    return out


# ---------------------------------------------------------------------------------------------- act
async def _act(ctx, s: dict, do: str, p: dict) -> dict:
    """Do ONE gated action in the session tab and return what changed. Raises CollabError for refusals and failures."""
    limit = s.get("max_steps", MAX_STEPS)
    if s["steps"] >= limit:
        raise CollabError("STEP_LIMIT", f"this session has used its {limit} actions: close it and open a fresh one (this stops runaway loops)")
    tab, el = s["tab_id"], None
    params: dict = {"tab_id": tab}
    if do in ("click", "type", "select", "check", "uncheck") or (do in ("scroll", "key") and p.get("target")):
        el, cands = resolve_target(s["elements"], str(p.get("target", "")), loose=bool(p.get("loose")))
        if el is None:
            return {"acted": False, "problem": "ambiguous" if cands else "not_found", "target": p.get("target"), "candidates": cands,
                    "hint": "no control has exactly that label: these are partial matches or lookalikes. Pass the right ref (el_###), the full label, or loose=true to accept a single partial match" if cands else "no control has that label: call eye.look (controls) to see what is on the page, or scroll"}
        params.update(element_id=el["id"], snapshot_id=s["snapshot_id"])
    if do == "type":
        if p.get("text") is None:
            raise CollabError("INVALID_PARAMS", "do=type needs text")
        params.update(text=str(p["text"]), clear=True)
    elif do == "select":
        if p.get("option") is None:
            raise CollabError("INVALID_PARAMS", "do=select needs option")
        params["option"] = p["option"]
    elif do == "scroll":
        if p.get("to"):
            params["to"] = p["to"]
        elif p.get("direction"):
            params["direction"] = p["direction"]
        elif not el:
            params["direction"] = "down"
    elif do == "key":
        if not p.get("key"):
            raise CollabError("INVALID_PARAMS", "do=key needs key")
        params["key"] = str(p["key"])
    engine = ENGINE_ACTION[do]
    risk = risk_mod.assess(ACTIONS[engine], params, ctx.svc.rt.state)
    label = f"{(el or {}).get('name') or ''} {(el or {}).get('href') or ''}"
    if do in ("click", "check", "uncheck", "select", "key") and (risk_mod.IRREVERSIBLE.search(label) or EXTRA_MUTATING.search(str((el or {}).get('name') or ''))):
        risk = {**risk, "approval": "required", "reasons": list(risk.get("reasons") or []) + [f"the control is labelled {str((el or {}).get('name') or '')[:50]!r}, which sounds like it changes something"]}
    try:
        gate_action(do, el, risk, params=params, last_was_type=s["last_was_type"])
    except EyeError as e:
        raise _fail(e) from None
    before, url0 = list(s["lines"]), s["url"]
    try:
        resp = await _call(ctx, engine, params, timeout_s=45)
    except CollabError as e:
        if e.code == "STALE_ELEMENT" and el is not None and not p.get("_retried"):      # the page redrew between my look and my click: look again, find the same control by its label, try once more
            await _refresh(ctx, s)
            return await _act(ctx, s, do, {**p, "target": str(el.get("name") or p.get("target")), "_retried": True})
        raise
    s["steps"] += 1
    s["last_was_type"] = do == "type"
    await _call(ctx, "wait", {"tab_id": tab, "ms": int(p.get("wait_ms", 700))})
    await _refresh(ctx, s)
    _touch(s)
    added, gone = diff_lines(before, s["lines"])
    new_text = "\n".join(added)
    if len(new_text) > 1500:
        new_text = new_text[:1500] + f"\n…(+{len(new_text) - 1500} more characters: eye.look to read them)"
    out = {"acted": True, "did": f"{do}" + (f" {str(el.get('name') or el['id'])[:50]!r}" if el else "") + (f" {str(p.get('text'))[:40]!r}" if do == "type" else ""),
           "changed": bool(added or gone or s["url"] != url0), "new_lines": new_text, "lines_gone": gone}
    if s["url"] != url0:
        out["url"], out["title"] = s["url"], s["title"]
    if not out["changed"]:
        out["note"] = "nothing visible changed (the control may need a different target, or the page is still loading: eye.look wait_ms=1500)"
    big = max(parse_tables(s["lines"]), key=lambda t: len(t["rows"]), default=None)
    if big and (added or gone):
        out["table"] = {"header": big["header"], "first_rows": big["rows"][:5], "row_count": len(big["rows"])}
    ctx.evidence("eye_step", {"do": do, "target": (el or {}).get("name"), "url": s["url"], "added": added[:60], "gone": gone, "risk": risk.get("level")}, source_url=s["url"], summary=out["did"])
    return out


@capability("eye.act", "Do ONE careful action in the session's tab and get back only what changed. do = click | type | select | check | uncheck | scroll | key | back | reload. target = a ref from the latest digest (el_###) "
            "or, easier, the control's exact label (e.g. 'Average position', 'Impressions', 'Next page'). Refused, with the reason, when it looks like it changes something (delete, send, pay, publish, save, approve, log out ...), "
            "touches a password/personal field, types anywhere except a search/filter box, or leads to a blocked site. If it is refused and truly needed, ask the owner with owner.ask.",
            props={"session": S("Session name (default 'main').", maxLength=30), "do": S("click | type | select | check | uncheck | scroll | key | back | reload", enum=list(ENGINE_ACTION)),
                   "target": S("Ref (el_###) or the control's label.", maxLength=120), "text": S("Text to type (search/filter boxes only).", maxLength=300), "option": S("Option label or value for do=select.", maxLength=120),
                   "direction": S("For do=scroll: up | down | left | right", enum=["up", "down", "left", "right"]), "to": S("For do=scroll: top | bottom", enum=["top", "bottom"]),
                   "key": S("For do=key: Enter (only right after typing in a search box), Tab, Escape, arrows, PageDown, ...", maxLength=20),
                   "loose": B("Accept ONE partial label match (default false: a partial match is only offered as a candidate, never pressed)."), "wait_ms": I("Wait after the action (default 700).", minimum=0, maximum=10000)},
            required=("do",), output={"acted": "bool", "did": "", "changed": "bool", "new_lines": "text that appeared", "table": "{header, first_rows, row_count} when a table changed", "candidates": "when the label was ambiguous"},
            permissions=("browser:act",), scope="client_optional", read_only=False, category="eye", needs_browser=True, timeout_s=90, max_timeout_s=180,
            limitations=("One action per call; each session allows 80 actions.", "It never submits forms, signs in, buys, sends, deletes or types into personal fields."))
async def eye_act(ctx, p: dict) -> dict:
    s = _session(ctx, p.get("session", "main"))
    return await _act(ctx, s, p["do"], p)


# ---------------------------------------------------------------------------------------------- table
@capability("eye.table", "Read the biggest table on the session's page as rows, exactly as shown (header + cells). With all_pages=true it presses the page's own 'Next page' button until the rows are enough or the pages run out. "
            "Typical use: after sorting/toggling columns with eye.act, take the top 25 rows.",
            props={"session": S("Session name (default 'main').", maxLength=30), "all_pages": B("Keep pressing 'Next page' (default false: only the rows on screen)."),
                   "max_rows": I("Stop at this many rows (default 100, max 1000).", minimum=1, maximum=1000), "max_pages": I("Never more than this many pages (default 30).", minimum=1, maximum=100)},
            output={"header": "[...]", "rows": "[[cell,...]]", "row_count": "int", "pages_read": "int", "page_info": "e.g. 1-10 of 211", "complete": "bool"},
            permissions=("browser:act",), scope="client_optional", read_only=False, category="eye", needs_browser=True, timeout_s=240, max_timeout_s=600,
            limitations=("Tables are read from the page text: cells are separated by tabs on pages that draw real tables or grids.", "Pagination uses a button named Next / Next page."))
async def eye_table(ctx, p: dict) -> dict:
    s = _session(ctx, p.get("session", "main"))
    await _refresh(ctx, s)
    _touch(s)
    big = max(parse_tables(s["lines"]), key=lambda t: len(t["rows"]), default=None)
    if big is None:
        raise CollabError("NO_TABLE", "no table found on this page (cells separated by tabs): eye.look to see the page, or the table may need scrolling into view")
    cap, pages_cap = int(p.get("max_rows", 100)), int(p.get("max_pages", 30))
    header, rows, pages, last = big["header"], list(big["rows"]), 1, big["rows"]
    info = PAGE_INFO.search("\n".join(s["lines"]))
    reached_end = True           # False when it stopped because of the row or page limit rather than because the pages ran out
    if p.get("all_pages"):
        while True:
            nb = next_button(s["elements"])
            if nb is None:                      # the pages ran out
                break
            if len(rows) >= cap or pages >= pages_cap:
                reached_end = False             # there is more, but the limit was reached first
                break
            await _act(ctx, s, "click", {"target": nb["id"], "wait_ms": 900})
            cur = max(parse_tables(s["lines"]), key=lambda t: len(t["rows"]), default=None)
            if cur is None or cur["rows"] == last:
                break
            rows += cur["rows"]
            last, pages = cur["rows"], pages + 1
            info = PAGE_INFO.search("\n".join(s["lines"])) or info
            ctx.progress(f"table: {len(rows)} rows, page {pages}")
    return {"header": header, "rows": rows[:cap], "row_count": min(len(rows), cap), "pages_read": pages, "page_info": info.group(0) if info else None,
            "complete": bool(reached_end and len(rows) <= cap and (p.get("all_pages") or not next_button(s["elements"])))}


# ---------------------------------------------------------------------------------------------- a person must pass a check
HUMAN_POLL_S = 3.0


@capability("eye.wait_human", "The session's page is a CAPTCHA / 'verify you are human' check that only a person can pass (eye.open / eye.look show human_check). This brings the tab to the front, asks the OWNER in their Chrome side "
            "panel to pass it themselves, then waits and carries on as soon as the page is clear (or the owner says they cannot, or time runs out). It NEVER tries to pass a check itself. A 'blocked' page "
            "(the site refused this connection) cannot be cleared by the owner: it returns at once.",
            props={"session": S("Session name (default 'main').", maxLength=30), "max_wait_s": I("How long to wait for the owner (default 240, max 540).", minimum=10, maximum=540)},
            output={"cleared": "bool", "reason": "why not, when cleared is false", "url": "", "text": "the page once it is clear"}, permissions=("browser:act", "owner:message"), scope="client_optional", read_only=False,
            category="eye", needs_browser=True, timeout_s=300, max_timeout_s=600,
            limitations=("Needs the owner to be at the computer: the request shows in the side panel and the Hub Inbox.", "The owner passes the check in the Chrome tab itself; nothing is typed or clicked for them."))
async def eye_wait_human(ctx, p: dict) -> dict:
    s = _session(ctx, p.get("session", "main"))
    await _refresh(ctx, s)
    label = human_check(s["title"], s["lines"])
    if label is None:
        return {"cleared": True, "note": "there is no check on this page", **_digest(s, max_controls=15)}
    if label == "blocked":
        return {"cleared": False, "reason": "blocked", "url": s["url"], "note": "the site refused this connection (a block page). Passing a check will not help: use another source, or ask the owner to open it themselves and send what they see."}
    host = re.sub(r"^https?://", "", s["url"]).split("/")[0]
    await _call(ctx, "switch_tab", {"tab_id": s["tab_id"]})
    req = ctx.svc.inbox.create(caller=ctx.caller, title=f"Please pass the check on {host}", client_id=ctx.client_id, task_id=ctx.task_id, related="eye", want_page=False,
                               steps=["Open Chrome: the tab for this site is now in front.", "Pass the check on the page yourself (tick the box or pick the pictures). Do not close the tab.",
                                      "I will carry on by myself as soon as the page loads. If you cannot pass it, press 'I can't pass it'."],
                               question="", choices=["Done", "I can't pass it"])
    ctx.svc.notify_inbox()
    deadline, why = time.time() + float(p.get("max_wait_s", 240)), "timed out waiting for the owner"
    try:
        while time.time() < deadline:
            await asyncio.sleep(HUMAN_POLL_S)
            await _refresh(ctx, s)
            _touch(s)
            if human_check(s["title"], s["lines"]) is None:
                return {"cleared": True, "waited_s": round(time.time() - (deadline - float(p.get("max_wait_s", 240)))), **_digest(s, max_controls=15)}
            row = ctx.svc.inbox.get(req["or_id"])
            if row["status"] == "answered" and row.get("reply_choice") == "I can't pass it":
                why = "the owner could not pass the check"
                break
    finally:
        try:
            ctx.svc.inbox.cancel(req["or_id"], caller=ctx.caller)
            ctx.svc.notify_inbox()
        except Exception:      # already answered or closed
            pass
    return {"cleared": False, "reason": why, "url": s["url"], "note": "do not retry in a loop: tell the owner what is blocked and ask them how to continue"}


# ---------------------------------------------------------------------------------------------- close / status
@capability("eye.close", "Close the session's tab (do this when you are done: it is the owner's browser).", props={"session": S("Session name (default 'main').", maxLength=30)},
            output={"closed": "bool"}, permissions=("browser:act",), scope="client_optional", read_only=False, category="eye", timeout_s=30)
async def eye_close(ctx, p: dict) -> dict:
    return {"closed": await ctx.svc.eye.close(ctx.caller, p.get("session", "main"))}


@capability("eye.status", "Whether the owner allows AI browsing, the blocked-site list, and this caller's open sessions.", output={"enabled": "bool", "blocked_sites": "[...]", "sessions": "[...]"},
            permissions=("browser:act",), scope="client_optional", category="eye", timeout_s=15)
async def eye_status(ctx, p: dict) -> dict:
    eyes = ctx.svc.eye
    pol = eyes.policy.load()
    mine = [{"session": k.split(":", 1)[1], "url": v["url"], "steps": v["steps"], "idle_s": int(time.time() - v["last_used"])} for k, v in eyes.items.items() if k.startswith(ctx.caller + ":")]
    return {"enabled": pol["enabled"], "blocked_sites": pol["deny"], "allowed_overrides": pol["allow_override"], "sessions": mine, "max_actions_per_session": MAX_STEPS,
            "browser_copy": _instance(ctx)[:8] or None}
