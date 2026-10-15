"""A tiny fake browser behind the FakeExtension protocol, so the AI loop can be tested end to end against the REAL dispatcher.

It models tabs, pages made of elements, snapshot-bound element ids (a stale snapshot really fails with STALE_ELEMENT), navigation, new tabs,
typing, Enter-to-submit, restricted pages and injectable faults. It is test scaffolding only; the loop under test knows nothing about it.
"""
from __future__ import annotations

import copy
import itertools
from typing import Any, Callable

RESTRICTED = ("chrome://", "chrome-extension://", "about:")


def el(role: str, name: str, **kw: Any) -> dict:
    return {"role": role, "name": name, "tag": kw.pop("tag", {"textbox": "input", "searchbox": "input", "button": "button", "link": "a"}.get(role, "div")), **kw}


class FakeBrowser:
    def __init__(self, pages: dict[str, dict], start_url: str = "https://start.test/"):
        self.pages = {u: copy.deepcopy(p) for u, p in pages.items()}
        self._tab_ids = itertools.count(100)
        self._snap = itertools.count(1)
        self.tabs: dict[int, dict] = {}
        self.snap: dict[int, dict] = {}  # tab -> {"id": str, "els": [...]}
        self.log: list[tuple[str, dict]] = []  # executed browser actions (action, params)
        self.faults: dict[str, Any] = {}
        self.window = 1
        self.open_tab(start_url, active=True)

    # ---- model helpers
    @property
    def active(self) -> int:
        return next(i for i, t in self.tabs.items() if t["active"])

    def open_tab(self, url: str, active: bool = True) -> int:
        tid = next(self._tab_ids)
        if active:
            for t in self.tabs.values():
                t["active"] = False
        self.tabs[tid] = {"url": url, "active": active}
        return tid

    def page(self, tab: int) -> dict:
        return self.pages.get(self.tabs[tab]["url"], {"title": "Untitled", "elements": [], "text": ""})

    def tab_view(self, tid: int) -> dict:
        t = self.tabs[tid]
        supported = not t["url"].startswith(RESTRICTED)
        d = {"tab_id": tid, "window_id": self.window, "index": 0, "active": t["active"], "title": self.page(tid)["title"], "url": t["url"], "status": "complete",
             "supported": supported}
        if not supported:
            d["restricted_reason"] = "Chrome internal pages block extension scripts."
        return d

    def _fail(self, code: str, msg: str, **details: Any) -> dict:
        return {"success": False, "result": None, "error": {"code": code, "message": msg, **({"details": details} if details else {})}}

    def _verif(self, before_url: str, tab: int, *, text_changed: bool = False, new_tabs: list | None = None, closed: list | None = None) -> dict:
        after = self.tabs[tab]["url"] if tab in self.tabs else None
        changed = after != before_url
        return {"tab_id": tab, "settle_ms": 5, "observed_change": bool(changed or text_changed or new_tabs or closed), "url": {"before": before_url, "after": after, "changed": changed},
                "navigated": changed, "new_tabs": new_tabs or [], "closed_tabs": closed or [],
                "page": {"text_changed": text_changed, "dom_mutations": 1, "scroll_changed": False, "dialogs_opened": [], "focus_changed": False}, "checks": []}

    def _resolve_tab(self, p: dict) -> int:
        return p.get("tab_id") or self.active

    # ---- handlers (action -> fn(params, cmd) -> response fields)
    def handlers(self) -> dict[str, Callable]:
        return {n: (lambda p, c, n=n: self._h(n, p)) for n in (
            "get_tabs", "inspect_page", "click", "type", "press_key", "navigate", "new_tab", "switch_tab", "close_tab", "scroll", "read_page", "wait", "verify",
            "get_page_state", "screenshot", "select", "check", "clear", "back", "forward", "reload", "ping", "upload_file", "download_file", "uncheck", "find_text")}

    def _h(self, action: str, p: dict) -> dict:
        if action in self.faults:
            f = self.faults[action]
            out = f(p) if callable(f) else f
            if out is not None:
                return out
        if action not in ("get_tabs", "inspect_page", "read_page", "get_page_state", "verify", "wait", "ping", "screenshot"):  # find_text moves the viewport, so it is logged
            self.log.append((action, dict(p)))
        fn = getattr(self, "_a_" + action, None)
        return fn(p) if fn else {"result": {}}

    def _a_ping(self, p):
        return {"result": {"version": "fake"}}

    def _a_get_tabs(self, p):
        tabs = [self.tab_view(t) for t in self.tabs]
        return {"result": {"tabs": tabs, "count": len(tabs), "active_tab_id": self.active, "focused_window_id": self.window}}

    def _a_inspect_page(self, p):
        tab = self._resolve_tab(p)
        if not self.tab_view(tab)["supported"]:
            return self._fail("UNSUPPORTED_PAGE", "This Chrome page cannot be controlled by the extension.", url=self.tabs[tab]["url"])
        sid = f"snap_{next(self._snap)}"
        pg = self.page(tab)
        els = []
        for i, e in enumerate(pg["elements"], 1):
            d = {"id": f"el_{i:03d}", "in_viewport": True, "rect": {"x": 0, "y": i * 20, "w": 100, "h": 20}, **{k: v for k, v in e.items() if k in (
                "role", "name", "tag", "type", "value", "checked", "href", "placeholder", "hidden", "sensitive", "submits_form", "disabled", "options")}}
            els.append(d)
        self.snap[tab] = {"id": sid, "els": els, "defs": pg["elements"]}
        page = {"url": self.tabs[tab]["url"], "title": pg["title"], "headings": [{"level": 1, "text": pg["title"]}], "dialogs": pg.get("dialogs", []), "alerts": pg.get("alerts", []),
                "iframes": pg.get("iframes", []), "scroll": {"x": 0, "y": 0, "max_x": 0, "max_y": 500}, "viewport": {"w": 1000, "h": 700}, "forms": 0}
        result = {"snapshot_id": sid, "tab_id": tab, "url": self.tabs[tab]["url"], "title": pg["title"], "page": page, "elements": els,
                  "counts": {"returned": len(els), "candidates": len(els), "frames": 1}, "truncated": False, "notes": []}
        if p.get("include_text"):
            result["visible_text"] = pg.get("text", "")
        return {"result": result}

    def _elem(self, p: dict):
        tab = self._resolve_tab(p)
        s = self.snap.get(tab)
        if not s:
            return tab, None, self._fail("STALE_ELEMENT", "No inspect_page snapshot exists for this tab.", reason="no_snapshot")
        if p.get("snapshot_id") != s["id"]:
            return tab, None, self._fail("STALE_ELEMENT", f"Element belongs to snapshot {p.get('snapshot_id')}, current is {s['id']}.", reason="snapshot_superseded")
        idx = next((i for i, e in enumerate(s["els"]) if e["id"] == p.get("element_id")), None)
        if idx is None:
            return tab, None, self._fail("ELEMENT_NOT_FOUND", f"No element {p.get('element_id')}.")
        return tab, (s["els"][idx], s["defs"][idx]), None

    def _a_click(self, p):
        tab, found, err = self._elem(p)
        if err:
            return err
        e, d = found
        before = self.tabs[tab]["url"]
        if d.get("opens_tab"):
            nt = self.open_tab(d["opens_tab"], active=True)
            return {"result": {"element": {"name": e["name"]}}, "verification": self._verif(before, tab, new_tabs=[{"tab_id": nt, "url": d["opens_tab"], "title": ""}])}
        if d.get("goto"):
            self.tabs[tab]["url"] = d["goto"]
        if d.get("set_text"):
            self.pages[self.tabs[tab]["url"]]["text"] = d["set_text"]
        return {"result": {"element": {"name": e["name"]}}, "verification": self._verif(before, tab, text_changed=bool(d.get("set_text") or d.get("goto")))}

    def _a_type(self, p):
        tab, found, err = self._elem(p)
        if err:
            return err
        e, d = found
        if e["role"] not in ("textbox", "searchbox"):
            return self._fail("NOT_EDITABLE", "The element does not accept text input.")
        d["value"] = p["text"] if p.get("clear", True) else d.get("value", "") + p["text"]
        masked = d.get("sensitive") or d.get("type") == "password"
        return {"result": {"element": {"name": e["name"]}, "value_after": "[hidden: %d chars]" % len(d["value"]) if masked else d["value"], "value_matches": True},
                "verification": self._verif(self.tabs[tab]["url"], tab, text_changed=True)}

    def _a_press_key(self, p):
        tab = self._resolve_tab(p)
        before = self.tabs[tab]["url"]
        action = None
        if p.get("element_id"):
            _, found, err = self._elem(p)
            if err:
                return err
            e, d = found
            if p["key"] == "Enter" and d.get("on_enter"):
                self.tabs[tab]["url"] = d["on_enter"].replace("{value}", str(d.get("value", "")).replace(" ", "+"))
                action = "form_submit"
        return {"result": {"key": p["key"], "default_action": action}, "verification": self._verif(before, tab, text_changed=bool(action))}

    def _a_navigate(self, p):
        tab = self._resolve_tab(p)
        before = self.tabs[tab]["url"]
        url = p["url"] if "://" in p["url"] else "https://" + p["url"].rstrip("/") + "/"
        self.tabs[tab]["url"] = url
        return {"result": {"tab_id": tab, "final_url": url, "title": self.page(tab)["title"], "redirected": False}, "verification": self._verif(before, tab)}

    def _a_new_tab(self, p):
        before = self.tabs[self.active]["url"]
        nt = self.open_tab(p.get("url", "chrome://newtab/"), active=p.get("active", True))
        return {"result": {"tab": self.tab_view(nt)}, "verification": self._verif(before, self.active, new_tabs=[{"tab_id": nt}])}

    def _a_switch_tab(self, p):
        tid = p.get("tab_id")
        if tid not in self.tabs:
            return self._fail("TAB_NOT_FOUND", f"No tab with id {tid}.")
        for t in self.tabs.values():
            t["active"] = False
        self.tabs[tid]["active"] = True
        return {"result": {"tab": self.tab_view(tid), "window_focused": True}, "verification": {"observed_change": True, "url": {"changed": False}, "new_tabs": [], "closed_tabs": [], "checks": []}}

    def _a_close_tab(self, p):
        tid = p["tab_id"]
        if tid not in self.tabs:
            return self._fail("TAB_NOT_FOUND", f"No tab with id {tid}.")
        was_active = self.tabs.pop(tid)["active"]
        self.snap.pop(tid, None)
        if was_active and self.tabs:
            self.tabs[next(iter(self.tabs))]["active"] = True
        return {"result": {"closed_tab_id": tid}, "verification": {"observed_change": True, "closed_tabs": [tid], "new_tabs": [], "url": {"changed": False}, "checks": []}}

    def _a_scroll(self, p):
        return {"result": {"moved": True, "after": {"y": 300}, "max": {"y": 500}, "reached_limit": False, "mode": "by"}, "verification": self._verif(self.tabs[self.active]["url"], self.active)}

    def _a_read_page(self, p):
        tab = self._resolve_tab(p)
        return {"result": {"scope": p.get("scope", "viewport"), "text": self.page(tab).get("text", ""), "total_chars": 10, "truncated": False, "url": self.tabs[tab]["url"], "title": self.page(tab)["title"]}}

    def _a_find_text(self, p):
        """Searches the page's `text` (case-insensitive unless asked); a page may also list `hidden_text` that exists but is not visible."""
        tab = self._resolve_tab(p)
        if not self.tab_view(tab)["supported"]:
            return self._fail("UNSUPPORTED_PAGE", "This Chrome page cannot be controlled by the extension.", url=self.tabs[tab]["url"])
        pg = self.page(tab)
        text, needle = pg.get("text", ""), p["text"].strip()
        hay, n = (text, needle) if p.get("case_sensitive") else (text.lower(), needle.lower())
        starts, i = [], hay.find(n)
        while i != -1 and n:
            starts.append(i)
            i = hay.find(n, i + len(n))
        hidden = (pg.get("hidden_text", "") if p.get("case_sensitive") else pg.get("hidden_text", "").lower()).count(n) if n else 0
        ctx = p.get("context_chars", 80)
        meta = pg.get("match_meta", [])  # optional per-match semantics (match_type, is_heading, ...), applied in document order
        matches = [{"match_index": k, "text": text[s:s + len(n)], "context": text[max(0, s - ctx):s] + "[[" + text[s:s + len(n)] + "]]" + text[s + len(n):s + len(n) + ctx],
                    "element_tag": "p", "is_heading": False, "is_link": False, "match_type": "body_text", "visibility": "in_view", "in_viewport": True, "page_y": s,
                    **(meta[k] if k < len(meta) else {})} for k, s in enumerate(starts[:p.get("max_matches", 10)])]
        idx = p.get("index", 0)
        focused = None
        before = self.tabs[tab].get("scroll_y", 0)
        if p.get("scroll", True) and idx < len(starts):
            self.tabs[tab]["scroll_y"] = starts[idx]
            focused = {**(matches[idx] if idx < len(matches) else {"match_index": idx, "text": needle, "context": "", "element_tag": "p", "match_type": "body_text"}), "in_viewport": True, "visibility": "in_view", "scrolled": True}
        result = {"query": needle, "found": bool(starts), "total_matches": len(starts), "hidden_matches": hidden, "case_sensitive": bool(p.get("case_sensitive")),
                  "whole_word": False, "matches": matches, "focused": focused, "url": self.tabs[tab]["url"], "title": pg["title"], "frames_searched": 1,
                  "scroll": {"before": {"x": 0, "y": before}, "after": {"x": 0, "y": self.tabs[tab].get("scroll_y", 0)}, "max_y": 500} if focused else None}
        v = self._verif(self.tabs[tab]["url"], tab)
        if focused:
            v["observed_change"] = self.tabs[tab].get("scroll_y", 0) != before
            v["page"]["scroll_changed"] = v["observed_change"]
        return {"result": result, "verification": v}

    def _a_get_page_state(self, p):
        tab = self._resolve_tab(p)
        return {"result": {"tab": self.tab_view(tab), "page": {"url": self.tabs[tab]["url"], "scroll": {"y": 0, "max_y": 500}}}}

    def _a_wait(self, p):
        tab = self._resolve_tab(p)
        ok = True
        for c in p.get("until") or []:
            if c["type"] == "url_contains":
                ok &= c["value"] in self.tabs[tab]["url"]
            elif c["type"] == "text_present":
                ok &= c["value"].lower() in (self.page(tab).get("text", "") + self.page(tab)["title"]).lower()
        res = {"result": {"satisfied": ok, "waited_ms": 10, "checks": []}}
        if not ok:
            res.update(success=False, error={"code": "POSTCONDITION_FAILED", "message": "condition not met"})
        return res

    def _a_verify(self, p):
        return {"result": {"all_passed": True, "checks": []}}

    def _a_screenshot(self, p):
        return {"result": {"data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==", "format": "png", "width": 1, "height": 1}}


def shop_pages() -> dict[str, dict]:
    """Generic pages used by the tests (a made-up shop; nothing here is specific to any real website)."""
    return {
        "https://start.test/": {"title": "Start", "text": "Welcome", "elements": [el("link", "Go to shop", href="https://shop.test/", goto="https://shop.test/")]},
        "https://shop.test/": {
            "title": "Shop home", "text": "Welcome to the shop. Find products.",
            "elements": [
                el("link", "Home", href="https://shop.test/"),
                el("searchbox", "Search for products", placeholder="Search", on_enter="https://shop.test/search?q={value}"),
                el("button", "Search", goto="https://shop.test/search?q=headphones"),
                el("button", "Delete account", goto="https://shop.test/deleted"),
                el("link", "Help (opens in new tab)", href="https://help.test/", opens_tab="https://help.test/"),
            ]},
        "https://shop.test/search?q=headphones": {"title": "headphones - Search results", "text": "Showing results for headphones", "elements": [el("link", "Result one", href="https://shop.test/p/1")]},
        "https://shop.test/search?q=wireless+headphones": {"title": "wireless headphones - Search results", "text": "Showing results for wireless headphones", "elements": [el("link", "Result one", href="https://shop.test/p/1")]},
        "https://shop.test/deleted": {"title": "Account deleted", "text": "gone", "elements": []},
        "https://help.test/": {"title": "Help centre", "text": "How can we help?", "elements": [el("link", "Contact", href="https://help.test/contact")]},
        "https://login.test/": {"title": "Sign in", "text": "Sign in to continue", "elements": [
            el("textbox", "Email"), el("textbox", "Password", type="password", sensitive=True), el("button", "Sign in", submits_form=True)]},
        "https://captcha.test/": {"title": "Just a moment", "text": "Verify you are human", "iframes": [{"src": "https://www.google.com/recaptcha/api2/bframe?x=1", "visible": True}],
                                   "elements": [el("button", "Continue")]},
    }
