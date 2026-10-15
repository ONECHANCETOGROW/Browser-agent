"""Observation builder: turns engine output into a compact, ranked, LLM-friendly view of the current environment.

Priority of content: browser state → URL/title → page structure → interactive elements → relevant text → (screenshot only on request).
Token savings come from: ranking by relevance to the task, de-duplicating repeated controls, capping the list and reporting what was
omitted, sending visible text only when the page changed, and describing changes as a diff. Nothing here knows any website.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..engine.dispatcher import Dispatcher
from .settings import BrainSettings
from .task import TaskState

STOP = set("the and for with that this from into your you are can will then them they what when where which about open find click press type "
           "search page website site please just also have has had been not but all any one use using make get go to on in of at by as is it its be "
           "a an or if so do does did".split())
ROLE_WEIGHT = {"searchbox": 3.5, "textbox": 3.0, "combobox": 3.0, "listbox": 2.5, "checkbox": 2.5, "radio": 2.5, "switch": 2.5, "spinbutton": 2.5, "slider": 2.0,
               "button": 2.5, "file": 2.0, "tab": 1.8, "menuitem": 1.5, "link": 1.4, "clickable": 1.2}
CAPTCHA_FRAME = re.compile(r"bframe|hcaptcha|turnstile|arkoselabs|funcaptcha|geetest|captcha/challenge|recaptcha/api2/anchor", re.I)
CAPTCHA_TEXT = re.compile(r"verify (that )?you(?:'re| are) (a )?human|i'?m not a robot|complete the captcha|solve the captcha|are you a robot|unusual traffic|security check", re.I)
ERROR_PAGE = re.compile(r"\b(404|403|500|502|503|not found|error response|page not found|access denied|forbidden|bad gateway|service unavailable|this site can.t be reached)\b", re.I)
LOGIN_TEXT = re.compile(r"\b(sign|log)[ -]?in\b|\blogin\b", re.I)


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if t not in STOP}


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url[:40]
    except ValueError:
        return url[:40]


@dataclass
class Observation:
    tabs: list[dict] = field(default_factory=list)
    active_tab_id: int | None = None
    url: str = ""
    title: str = ""
    supported: bool = True
    restricted_reason: str | None = None
    snapshot_id: str | None = None
    page: dict = field(default_factory=dict)
    cands: list[tuple[float, int, dict, str]] = field(default_factory=list)  # (score, order, element, line) best first
    element_total: int = 0
    changes: list[str] = field(default_factory=list)
    text: str | None = None
    signals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    shown_ids: list[str] = field(default_factory=list)
    omitted_by_role: dict[str, int] = field(default_factory=dict)
    fresh_inspect: bool = False

    # ------------------------------------------------------------ rendering
    def render(self, max_chars: int = 5000, max_elements: int = 45) -> str:
        head = self._head()
        picked = self.cands[:max_elements]
        while True:
            body = self._elements_block(picked)
            out = head + body
            if len(out) <= max_chars or len(picked) <= 5:
                break
            picked = picked[: max(5, int(len(picked) * 0.8))]
        self.shown_ids = [e["id"] for _, _, e, _ in picked]
        return out

    def _head(self) -> str:
        lines = []
        shown_tabs = self.tabs[:8]
        tabs = " ".join(f"[{'*' if t['active'] else ' '}#{t['tab_id']} {host_of(t['url'])[:28]} \"{_clip(t['title'], 28)}\"{' RESTRICTED' if not t.get('supported', True) else ''}]" for t in shown_tabs)
        if len(self.tabs) > 8:
            tabs += f" (+{len(self.tabs) - 8} more)"
        lines.append(f"TABS ({len(self.tabs)}): {tabs}" if self.tabs else "TABS: none open")
        if self.active_tab_id is None:
            lines.append("No controllable tab is active. Use new_tab or switch_tab.")
            return "\n".join(lines) + "\n"
        lines.append(f"ACTIVE TAB #{self.active_tab_id}: {self.url}  title=\"{_clip(self.title, 80)}\"")
        if not self.supported:
            lines.append(f"This page CANNOT be controlled ({self.restricted_reason}). Only navigate, new_tab, switch_tab and get_tabs work here.")
            return "\n".join(lines) + "\n"
        p = self.page
        if p:
            heads = " | ".join(f"h{h['level']} {_clip(h['text'], 50)}" for h in (p.get("headings") or [])[:4])
            if heads:
                lines.append("HEADINGS: " + heads)
            for d in (p.get("dialogs") or [])[:2]:
                lines.append(f"DIALOG OPEN ({d.get('role')}): \"{_clip(d.get('name') or d.get('text'), 100)}\"")
            for a in (p.get("alerts") or [])[:2]:
                lines.append(f"ALERT: {_clip(a, 100)}")
            sc = p.get("scroll") or {}
            if sc.get("max_y"):
                lines.append(f"SCROLL: {sc.get('y', 0)}/{sc['max_y']}px")
        if self.signals:
            lines.append("SIGNALS: " + ", ".join(self.signals))
        for n in self.notes:
            lines.append("NOTE: " + n)
        if self.changes:
            lines.append("CHANGES: " + " ; ".join(self.changes[:6]))
        if self.text:
            lines.append(f"VISIBLE TEXT: \"{self.text}\"")
        return "\n".join(lines) + "\n"

    def _elements_block(self, picked: list) -> str:
        if not self.snapshot_id:
            return ""
        shown = {e["id"] for _, _, e, _ in picked}
        omitted: dict[str, int] = {}
        for _, _, e, _ in self.cands:
            if e["id"] not in shown:
                omitted[e["role"] or e["tag"]] = omitted.get(e["role"] or e["tag"], 0) + 1
        extra = self.element_total - len(self.cands)
        om = ", ".join(f"{n} {r}" for r, n in sorted(omitted.items(), key=lambda x: -x[1])[:4])
        head = f"ELEMENTS (snapshot {self.snapshot_id}; ids are valid only for this snapshot; {len(picked)} of {self.element_total} shown"
        head += f"; omitted: {om}" if om else ""
        head += "):\n"
        lines = [line for _, _, _, line in sorted(picked, key=lambda c: c[1])]
        return head + "\n".join(lines) + "\n"

    def to_summary(self) -> dict:
        return {"active_tab": self.active_tab_id, "url": self.url, "title": self.title, "supported": self.supported, "snapshot_id": self.snapshot_id,
                "elements": self.element_total, "signals": self.signals, "tabs": len(self.tabs)}


class ObservationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ObservationBuilder:
    def __init__(self, dispatcher: Dispatcher, settings: BrainSettings):
        self.d = dispatcher
        self.s = settings

    # ------------------------------------------------------------ runtime cache (per task, not persisted)
    @staticmethod
    def rt(task: TaskState) -> dict:
        return task.__dict__.setdefault("runtime", {"by_tab": {}, "prev": {}, "dirty": True})

    def mark_dirty(self, task: TaskState) -> None:
        self.rt(task)["dirty"] = True

    # ------------------------------------------------------------ main entry
    async def observe(self, task: TaskState, *, force: bool = False, focus: str = "", want_text: bool = False) -> Observation:
        rt = self.rt(task)
        r = await self.d.execute("get_tabs", {}, source="agent")
        if not r["success"]:
            raise ObservationError(r["error"]["code"], r["error"]["message"])
        res = r["result"]
        tabs = res["tabs"]
        obs = Observation(tabs=tabs, active_tab_id=res.get("active_tab_id"))
        task.known_tabs = {t["tab_id"]: {"url": t["url"], "title": t["title"], "active": t["active"], "supported": t["supported"]} for t in tabs}
        active = next((t for t in tabs if t["tab_id"] == obs.active_tab_id), None)
        if active is None:
            task.current_tab = None
            return obs
        obs.url, obs.title = active["url"], active["title"]
        task.current_tab, task.current_url, task.current_page_title = active["tab_id"], active["url"], active["title"]
        if not active.get("supported", True):
            obs.supported, obs.restricted_reason = False, active.get("restricted_reason")
            obs.signals.append("restricted_page")
            task.available_actions = ["navigate", "new_tab", "switch_tab", "get_tabs", "close_tab"]
            task.latest_observation = obs.to_summary()
            return obs

        tab_id = active["tab_id"]
        cache = rt["by_tab"].get(tab_id)
        url_changed = cache is None or cache["url"] != active["url"]
        if force or cache is None or rt.get("dirty") or url_changed:
            ir = await self.d.execute("inspect_page", {"tab_id": tab_id, "max_elements": self.s.inspect_max_elements,
                                                       "include_text": bool(want_text or url_changed), "text_chars": self.s.obs_text_chars}, source="agent")
            if not ir["success"]:
                if ir["error"]["code"] == "UNSUPPORTED_PAGE":
                    obs.supported, obs.restricted_reason = False, (ir["error"].get("details") or {}).get("reason")
                    obs.signals.append("restricted_page")
                    task.latest_observation = obs.to_summary()
                    return obs
                obs.notes.append(f"page inspection failed: {ir['error']['code']} {_clip(ir['error']['message'], 100)}")
                if cache is None:
                    task.latest_observation = obs.to_summary()
                    return obs
            else:
                prev = rt["by_tab"].get(tab_id)
                new = ir["result"]
                cache = {"snapshot_id": new["snapshot_id"], "url": new["url"], "title": new["title"], "elements": new["elements"], "page": new["page"],
                         "text": new.get("visible_text"), "total": new["counts"]["candidates"], "fresh": True}
                if prev and prev["url"] == cache["url"]:
                    obs.changes = self._diff(prev["elements"], cache["elements"])
                elif prev:
                    obs.changes = [f"navigated from {host_of(prev['url'])}{urlparse(prev['url']).path[:40]}"]
                rt["by_tab"][tab_id] = cache
                rt["dirty"] = False
                task.observations += 1
                obs.fresh_inspect = True
        if cache is None:
            return obs
        obs.snapshot_id, obs.page = cache["snapshot_id"], cache["page"]
        obs.element_total = len(cache["elements"])
        obs.text = _clip(cache["text"], self.s.obs_text_chars) if (obs.fresh_inspect and cache.get("text")) else None
        obs.title = cache["title"] or obs.title
        self._rank(obs, cache["elements"], tokens(focus or task.user_instruction), self._prev_sigs(task, tab_id, cache))
        obs.signals += self._signals(cache)
        task.latest_observation = obs.to_summary()
        task.available_actions = self._available(obs)
        return obs

    # ------------------------------------------------------------ pieces
    @staticmethod
    def _sig(e: dict) -> tuple:
        return (e["role"], _clip(e["name"], 60).lower(), (e.get("href") or "")[-40:])

    def _prev_sigs(self, task, tab_id, cache) -> set:
        return set()  # new-element boosting uses obs.changes; kept for future per-element novelty

    def _diff(self, old: list[dict], new: list[dict]) -> list[str]:
        osig = {self._sig(e): e for e in old}
        nsig = {self._sig(e): e for e in new}
        out = []
        added = [e for k, e in nsig.items() if k not in osig and e["role"] in ROLE_WEIGHT]
        if added:
            out.append("NEW: " + ", ".join(f"{e['role']} \"{_clip(e['name'], 30)}\"" for e in added[:4]) + (f" (+{len(added) - 4} more)" if len(added) > 4 else ""))
        gone = [k for k in osig if k not in nsig]
        if gone:
            out.append(f"GONE: {len(gone)} control(s)")
        for k, e in nsig.items():
            o = osig.get(k)
            if o and (o.get("value") != e.get("value") or o.get("checked") != e.get("checked")):
                out.append(f"CHANGED {e['role']} \"{_clip(e['name'], 24)}\": {_clip(o.get('value', o.get('checked')), 20)!r} -> {_clip(e.get('value', e.get('checked')), 20)!r}")
                if len(out) >= 6:
                    break
        return out or ["no control changes"]

    def _rank(self, obs: Observation, elements: list[dict], focus: set[str], prev: set) -> None:
        seen: dict[tuple, int] = {}
        scored = []
        for i, e in enumerate(elements):
            role = e["role"] or e["tag"]
            key = (role, _clip(e["name"], 50).lower(), (e.get("href") or "").split("?")[0][-50:])
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1 and not e.get("value") and role not in ("textbox", "searchbox"):
                continue  # identical repeated control (e.g. the same nav link twice)
            name_key = (role, _clip(e["name"], 50).lower())
            seen[name_key] = seen.get(name_key, 0) + 1
            if seen[name_key] > 4 and role in ("link", "button", "clickable"):
                continue  # a list of identical-looking items: keep a few
            score = ROLE_WEIGHT.get(role, 1.0)
            score += 3.0 if e.get("in_viewport") else -0.5
            if e.get("hidden"):
                score -= 0.5 if role in ("file", "checkbox", "radio") else 2.0
            if e.get("focused"):
                score += 2.0
            if e.get("disabled"):
                score -= 1.0
            overlap = len(tokens(f"{e['name']} {e.get('placeholder', '')}") & focus)
            score += min(overlap, 3) * 1.5
            score -= i * 0.001
            scored.append((score, i, e, self._line(e, obs.url)))
        scored.sort(key=lambda c: -c[0])
        obs.cands = scored

    @staticmethod
    def _line(e: dict, page_url: str) -> str:
        role = e["role"] or e["tag"]
        parts = [f"{e['id']} {role}"]
        if e.get("name"):
            parts.append(f"\"{_clip(e['name'], 60)}\"")
        if e.get("type") and e["type"] not in ("text", "button", "submit", "checkbox", "radio", "file", "password"):
            parts.append(f"type={e['type']}")
        if e.get("placeholder") and e["placeholder"] != e.get("name"):
            parts.append(f"ph=\"{_clip(e['placeholder'], 30)}\"")
        if e.get("value") not in (None, ""):
            parts.append(f"val=\"{_clip(e['value'], 30)}\"")
        if e.get("checked") is not None:
            parts.append("checked" if e["checked"] else "unchecked")
        for flag in ("disabled", "readonly", "required", "sensitive", "submits_form"):
            if e.get(flag):
                parts.append(flag.replace("_", "-"))
        if e.get("expanded") is not None:
            parts.append("expanded" if e["expanded"] else "collapsed")
        if e.get("options"):
            labels = [("*" if o.get("selected") else "") + _clip(o["label"], 18) for o in e["options"][:6]]
            more = len(e["options"]) - 6
            parts.append(f"options=[{'|'.join(labels)}{f' +{more}' if more > 0 else ''}]")
        if e.get("href"):
            u = urlparse(e["href"])
            same = u.netloc == urlparse(page_url).netloc
            parts.append("→ " + _clip((u.path or "/") + (("?" + u.query) if u.query else "") if same else u.netloc + u.path, 48))
        if e.get("frame"):
            parts.append(f"frame={e['frame']}")
        if e.get("hidden"):
            parts.append("(hidden)")
        elif not e.get("in_viewport"):
            parts.append("(off-screen)")
        return " " + " ".join(parts)

    @staticmethod
    def _signals(cache: dict) -> list[str]:
        sig: list[str] = []
        page = cache.get("page") or {}
        els = cache["elements"]
        blob = " ".join([cache.get("title") or "", *(h["text"] for h in page.get("headings") or []), *(page.get("alerts") or []),
                         *((d.get("text") or "") for d in page.get("dialogs") or [])])
        frames = [f for f in page.get("iframes") or [] if f.get("visible") and CAPTCHA_FRAME.search(f.get("src") or "") and "size=invisible" not in (f.get("src") or "")]
        if frames or CAPTCHA_TEXT.search(blob) or any(CAPTCHA_TEXT.search(e["name"] or "") for e in els):
            sig.append("captcha")
        has_pw = any(e.get("type") == "password" and not e.get("hidden") for e in els)
        if has_pw and (LOGIN_TEXT.search(blob) or any(LOGIN_TEXT.search(e["name"] or "") for e in els if e["role"] == "button")):
            sig.append("login_form")
        if page.get("dialogs"):
            sig.append("dialog_open")
        if ERROR_PAGE.search(" ".join([cache.get("title") or "", *(h["text"] for h in (page.get("headings") or [])[:2])])):
            sig.append("error_page")
        return sig

    @staticmethod
    def _available(obs: Observation) -> list[str]:
        base = ["get_tabs", "navigate", "new_tab", "switch_tab", "back", "forward", "reload", "read_page", "find_text", "scroll", "screenshot", "press_key", "inspect_page"]
        roles = {e["role"] for _, _, e, _ in obs.cands}
        if roles & {"textbox", "searchbox"}:
            base += ["type", "clear"]
        if roles & {"button", "link", "clickable", "tab", "menuitem"}:
            base += ["click"]
        if "combobox" in roles or "listbox" in roles:
            base += ["select"]
        if roles & {"checkbox", "radio", "switch"}:
            base += ["check", "uncheck"]
        if "file" in roles:
            base += ["upload_file"]
        return base
