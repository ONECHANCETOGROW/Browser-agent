"""The EYE: how the AI collaborator looks at, and carefully uses, the owner's Chrome without burning tokens.

Three ideas:
  * ONE dedicated tab per session (never the owner's own tabs), kept between calls so state survives (a sort, a filter, a page of results).
  * COMPACT answers: the page is reduced to a short digest (cleaned text, tables as rows, a short list of controls with refs) and every action returns only WHAT CHANGED. A whole page is a few hundred tokens, not thousands.
  * A GATE that lives here, not in the AI: pages on blocked sites are refused, anything that looks like it changes something (delete, send, pay, publish, save, approve, log out ...) or touches a password/sensitive
    field is refused with a reason and a pointer to `owner.ask`, typing is allowed only into search/filter-like boxes, and a session has a step limit and an idle timeout.

The owner controls it with `python -m octg_agent eye status|on|off|deny <site>|allow <site>` (a plain file, data/eye.json).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

POLICY_FILE = "eye.json"
MAX_STEPS = 80                 # actions per session before a fresh session is needed
IDLE_S = 600.0                 # a session's tab is closed after ten idle minutes
MAX_SESSIONS_PER_CALLER = 3

# Sites the eye will not open or follow a link to by default. The owner edits this list with the CLI. Banks, payments, password managers, e-mail/chat and account-security pages.
DEFAULT_DENY = (
    "paypal.com", "wise.com", "stripe.com", "venmo.com", "cash.app", "coinbase.com", "binance.com", "razorpay.com", "paytm.com", "plaid.com",
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "capitalone.com", "americanexpress.com", "hdfcbank.com", "icicibank.com", "sbi.co.in", "axisbank.com",
    "1password.com", "lastpass.com", "bitwarden.com", "dashlane.com",
    "mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com", "mail.yahoo.com", "proton.me", "web.whatsapp.com", "messenger.com", "web.telegram.org",
    "accounts.google.com", "myaccount.google.com", "login.microsoftonline.com", "appleid.apple.com",
)
ALLOWED_SCHEMES = ("http", "https")

# Material-icon words that Google apps print as text next to their labels ("help_outline", "keyboard_arrow_down", "info", "home"): noise for a reader.
ICON_WORDS = {"home", "info", "search", "settings", "feedback", "layers", "lock", "trophy", "speed", "visibility_off", "file_copy", "account_tree", "link_2", "trending_up", "lightbulb_2", "add", "close", "menu",
              "check", "edit", "delete", "more_vert", "more_horiz", "chevron_forward", "chevron_backward", "chevron_left", "chevron_right", "expand_more", "expand_less", "arrow_drop_down", "arrow_drop_up",
              "arrow_back", "arrow_forward", "filter_list", "download", "upload", "refresh", "help", "launch", "open_in_new", "content_copy", "star", "star_border", "notifications", "person", "email", "call", "map",
              "place", "schedule", "check_circle", "cancel", "warning", "error", "visibility", "apps", "dashboard", "keyboard_arrow_down", "keyboard_arrow_up"}
_UNDERSCORE_WORD = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_ROLE_OK = {"button", "link", "tab", "checkbox", "radio", "switch", "combobox", "textbox", "searchbox", "menuitem", "option", "columnheader", "listbox", "treeitem", "menuitemradio", "menuitemcheckbox", "gridcell"}
_SEARCHY = re.compile(r"search|filter|find|query|keyword|url|site|domain|page|date|from|to\b|name|city|zip|location", re.I)
_SEARCH_BUTTON = re.compile(r"^(search|go|find|filter|apply|next|next page|previous|previous page|show|view|load more|more|done|ok)\b", re.I)
_NEXT = re.compile(r"^(next|next page|older|›|>|»)$", re.I)
_SAFE_KEYS = {"enter", "tab", "escape", "arrowdown", "arrowup", "arrowleft", "arrowright", "pagedown", "pageup", "home", "end"}
# Pages that only a person can pass. Matched only on SHORT pages (a long article that merely mentions a captcha is not a check). The eye never tries to get past one.
HUMAN_CHECKS = (("captcha", re.compile(r"unusual traffic from your computer|not a robot|recaptcha|select all (?:images|squares)|captcha", re.I)),
                ("bot_check", re.compile(r"verify (?:that )?you(?: are|'re) (?:a )?human|checking (?:if the site connection is secure|your browser)|just a moment|press (?:and|&) hold|are you a robot|confirm you are human", re.I)),
                ("blocked", re.compile(r"you have been blocked|sorry, you have been blocked|access denied|attention required", re.I)))


def human_check(title: str, lines: list[str]) -> str | None:
    """'captcha' | 'bot_check' | 'blocked' when the page is a check/block page, else None. 'blocked' means the site refused this connection: passing a check will not help."""
    if len(lines) > 120:
        return None
    head = (title or "") + "\n" + "\n".join(lines)
    for label, rx in HUMAN_CHECKS:
        if rx.search(head):
            return label
    return None


PAGE_INFO = re.compile(r"\b\d[\d,]*\s*[–\-]\s*\d[\d,]*\s+of\s+\d[\d,]*\b")


class EyeError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


# ---------------------------------------------------------------------------------------------- the owner's switch and the site list
class EyePolicy:
    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / POLICY_FILE

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            d = {}
        extra = sorted(map(str, d.get("deny_extra", [])))
        return {"enabled": bool(d.get("enabled", True)), "deny": sorted(set(DEFAULT_DENY) | set(extra)), "allow_override": sorted(map(str, d.get("allow_override", []))), "deny_extra": extra}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": d["enabled"], "deny_extra": d["deny_extra"], "allow_override": d["allow_override"]}, indent=2), encoding="utf-8")

    def set_enabled(self, on: bool) -> dict:
        d = self.load()
        d["enabled"] = bool(on)
        self._save(d)
        return self.load()

    def deny(self, site: str) -> dict:
        h = host_of(site) or site.strip().lower()
        d = self.load()
        d["allow_override"] = [x for x in d["allow_override"] if x != h]
        d["deny_extra"] = sorted(set(d["deny_extra"]) | {h})
        self._save(d)
        return self.load()

    def allow(self, site: str) -> dict:
        h = host_of(site) or site.strip().lower()
        d = self.load()
        d["deny_extra"] = [x for x in d["deny_extra"] if x != h]
        d["allow_override"] = sorted(set(d["allow_override"]) | {h})
        self._save(d)
        return self.load()

    def check(self, url: str) -> None:
        """Raises EyeError if the eye may not be used at all, or not on this URL."""
        p = self.load()
        if not p["enabled"]:
            raise EyeError("EYE_DISABLED", "The owner has switched the AI's browsing off (python -m octg_agent eye on turns it back on).")
        sp = urlsplit(url if "://" in url else "https://" + url)
        if sp.scheme not in ALLOWED_SCHEMES or not sp.hostname:
            raise EyeError("URL_NOT_ALLOWED", f"only http(s) pages can be opened (not {sp.scheme or 'this'}: pages)")
        h = sp.hostname.lower().rstrip(".")
        for allowed in p["allow_override"]:
            if h == allowed or h.endswith("." + allowed):
                return
        for d in p["deny"]:
            if h == d or h.endswith("." + d):
                raise EyeError("SITE_BLOCKED", f"{h} is on the owner's blocked list (banks, payments, password managers, e-mail, account security). Ask the owner with owner.ask to do it, or to allow the site.", {"site": h})


def host_of(url: str) -> str:
    try:
        return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------------------------- the digest
def clean_lines(text: str) -> list[str]:
    """Visible text as short clean lines: whitespace collapsed (tabs inside table rows kept), empty lines and icon-font words dropped, immediate repeats merged."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = re.sub(r"[  ​]+", " ", raw.replace("\t", " \t ")).strip()
        line = re.sub(r" ?\t ?", "\t", line)
        if not line:
            continue
        if "\t" not in line and (_UNDERSCORE_WORD.match(line) or line in ICON_WORDS):
            continue
        if out and out[-1] == line and "\t" not in line:
            continue
        out.append(line)
    return out


def parse_tables(lines: list[str]) -> list[dict]:
    """Tables as they appear in page text: consecutive lines whose cells are separated by tabs. The header is the short lines just before the first row (one per column)."""
    tables, i = [], 0
    while i < len(lines):
        if "\t" in lines[i] and lines[i].count("\t") >= 1:
            j, n = i, lines[i].count("\t")
            while j < len(lines) and "\t" in lines[j] and abs(lines[j].count("\t") - n) <= 0:
                j += 1
            ncols = n + 1
            head = [h for h in lines[max(i - ncols, 0):i] if "\t" not in h and len(h) <= 48]
            header = head[-ncols:] if len(head) >= ncols else head
            title = lines[i - ncols - 1] if i - ncols - 1 >= 0 and len(lines[i - ncols - 1]) <= 60 and "\t" not in lines[i - ncols - 1] and len(head) >= ncols else ""
            tables.append({"title": title, "header": header if len(header) == ncols else [], "rows": [r.split("\t") for r in lines[i:j]], "start": i, "end": j})
            i = j
        else:
            i += 1
    return tables


def diff_lines(before: list[str], after: list[str]) -> tuple[list[str], int]:
    """(lines that are new, how many lines disappeared). Order-preserving multiset difference, so a re-sorted table reads as changed rows rather than as nothing."""
    pool: dict[str, int] = {}
    for l in before:
        pool[l] = pool.get(l, 0) + 1
    added = []
    for l in after:
        if pool.get(l, 0) > 0:
            pool[l] -= 1
        else:
            added.append(l)
    return added, sum(pool.values())


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    parts = name.split(" ", 1)
    if len(parts) == 2 and (_UNDERSCORE_WORD.match(parts[0]) or parts[0] in ICON_WORDS):
        return parts[1]
    return name


def compact_controls(elements: list[dict], limit: int = 40, *, links: bool = False) -> list[dict]:
    """The few controls worth naming: things with a label that a person could use. On-screen ones first; links only when asked (menus are noise). Each is {ref, role, name, flags}."""
    seen, vis, rest = set(), [], []
    for e in elements:
        name = _clean_name(e.get("name"))
        role = e.get("role") or e.get("tag") or ""
        if not name or len(name) > 80 or e.get("sensitive"):
            continue
        if role == "link" and not links:
            continue
        if role not in _ROLE_OK and e.get("tag") not in ("button", "a", "input", "select", "textarea"):
            continue
        key = (role if role in _ROLE_OK else "x", _strip_value(name.lower()))
        if key in seen:
            continue
        seen.add(key)
        flags = [f for f, on in (("disabled", e.get("disabled")), ("checked", e.get("checked")), ("selected", e.get("selected")), ("expanded", e.get("expanded"))) if on]
        item = {"ref": e["id"], "role": role, "name": name, **({"flags": flags} if flags else {}), **({"to": e["href"][:90]} if role == "link" and e.get("href") else {})}
        (vis if e.get("in_viewport", True) else rest).append(item)
    return (vis + rest)[:limit]


def _norm_name(name) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def _strip_value(name: str) -> str:
    """'average ctr 2.4%' -> 'average ctr': report cards print their current value inside the label."""
    return re.sub(r"(?:\s+[-+]?[\d.,]+[kmb%]?)+$", "", name).strip()


def resolve_target(elements: dict[str, dict], target: str, *, loose: bool = False) -> tuple[dict | None, list[dict]]:
    """Find the element a ref or a label means. Returns (element, []) or (None, candidates) when nothing or several match.
    The same control is often listed twice (a real button and a click-wrapper around it): a real control role wins, and trailing values in a label ('Average CTR 2.4%') are ignored when matching."""
    t = (target or "").strip()
    if t in elements:
        return elements[t], []
    low = _norm_name(t)
    live = [e for e in elements.values() if not e.get("disabled") and _norm_name(e.get("name"))]
    pool = [e for e in live if _norm_name(e.get("name")) == low] or [e for e in live if _strip_value(_norm_name(e.get("name"))) == low]
    partial = [e for e in live if low and low in _norm_name(e.get("name"))]
    if not pool:
        if not loose:      # a PARTIAL match is only a suggestion ("Impressions" also matches the "Total impressions" card): it is shown, never pressed, unless loose=true
            partial.sort(key=lambda e: (not e.get("in_viewport", True), len(str(e.get("name") or ""))))
            return None, [{"ref": e["id"], "role": e.get("role") or e.get("tag"), "name": str(e.get("name") or "")[:60]} for e in partial[:8]]
        pool = partial
    if len(pool) > 1:
        real = [e for e in pool if (e.get("role") or e.get("tag")) in _ROLE_OK or e.get("tag") in ("button", "a", "input", "select", "textarea")]
        pool = real or pool
        if len({_strip_value(_norm_name(e.get("name"))) for e in pool}) == 1:      # the same control seen twice
            pool.sort(key=lambda e: (not e.get("in_viewport", True), (e.get("role") or "") not in _ROLE_OK))
            return pool[0], []
    if len(pool) == 1:
        return pool[0], []
    pool.sort(key=lambda e: (not e.get("in_viewport", True), len(str(e.get("name") or ""))))
    return None, [{"ref": e["id"], "role": e.get("role") or e.get("tag"), "name": str(e.get("name") or "")[:60]} for e in pool[:8]]


def next_button(elements: dict[str, dict]) -> dict | None:
    for e in elements.values():
        n = str(e.get("name") or "").strip()
        if _NEXT.match(n) and not e.get("disabled"):
            return e
    return None


# ---------------------------------------------------------------------------------------------- the gate
def gate_action(action: str, el: dict | None, risk: dict, *, params: dict, last_was_type: bool) -> None:
    """Raises EyeError(NEEDS_OWNER ...) for anything that is not plain looking/navigating. `risk` is the engine's own assessment of the action."""
    name = str((el or {}).get("name") or "")
    if risk.get("approval") == "required":
        why = "; ".join(risk.get("reasons") or []) or "it looks like it changes something"
        raise EyeError("NEEDS_OWNER", f"refused: {why}. The eye only looks and navigates. If this really is needed, ask the owner with owner.ask (they press it themselves).", {"element": name[:60], "tags": risk.get("tags")})
    if action == "type":
        t = str((el or {}).get("type") or "").lower()
        role = str((el or {}).get("role") or "").lower()
        if t in ("password", "email", "tel", "number", "file") or (el or {}).get("sensitive"):
            raise EyeError("NEEDS_OWNER", "refused: typing into this kind of field is the owner's job (it could be a login or personal data).")
        if not (t == "search" or role in ("searchbox", "combobox") or _SEARCHY.search(name)):
            raise EyeError("NEEDS_OWNER", f"refused: the eye only types into search or filter boxes, and {name[:50] or 'this field'!r} does not look like one.")
    elif action == "key":
        key = str(params.get("key", "")).lower()
        if key not in _SAFE_KEYS:
            raise EyeError("NEEDS_OWNER", f"refused: the key {key!r} is not one the eye uses (Enter after typing a search, Tab, Escape, arrows, Page keys).")
        if key == "enter" and not last_was_type:
            raise EyeError("NEEDS_OWNER", "refused: Enter is only used right after typing into a search or filter box (it could submit a form).")
    elif action == "click" and "form_submit" in (risk.get("tags") or []) and not _SEARCH_BUTTON.match(name.strip()):
        raise EyeError("NEEDS_OWNER", f"refused: {name[:50] or 'this button'!r} submits a form. The eye only presses search/filter/next style buttons.", {"element": name[:60]})


# ---------------------------------------------------------------------------------------------- sessions
class EyeSessions:
    """The dedicated tabs. Held in memory only: after an agent restart a session is simply gone and the AI opens a new one."""

    def __init__(self, svc):
        self.svc = svc
        self.items: dict[str, dict] = {}
        self.policy = EyePolicy(svc.rt.settings.data_dir)

    def key(self, caller: str, name: str) -> str:
        return f"{caller}:{name}"

    def get(self, caller: str, name: str) -> dict | None:
        return self.items.get(self.key(caller, name))

    def count(self, caller: str) -> int:
        return sum(1 for k in self.items if k.startswith(caller + ":"))

    def add(self, caller: str, name: str, tab_id: int, client_id: str | None) -> dict:
        s = {"caller": caller, "name": name, "tab_id": tab_id, "client_id": client_id, "opened_at": time.time(), "last_used": time.time(), "steps": 0, "elements": {}, "snapshot_id": None, "lines": [],
             "url": "", "title": "", "last_was_type": False}
        self.items[self.key(caller, name)] = s
        return s

    async def close(self, caller: str, name: str) -> bool:
        s = self.items.pop(self.key(caller, name), None)
        if not s:
            return False
        sess = getattr(self.svc.rt, "session", None)
        if s.get("instance") != str((getattr(sess, "hello", None) or {}).get("instance") or ""):
            return True        # another browser is connected now: this tab id belongs to the other one and could be one of the owner's own tabs here. Never close it.
        try:
            await self.svc.rt.dispatcher.execute("close_tab", {"tab_id": s["tab_id"]}, source=f"eye:{caller}")
        except Exception:  # noqa: BLE001 - the tab may already be gone
            pass
        return True

    async def gc(self) -> int:
        n = 0
        for k, s in list(self.items.items()):
            if time.time() - s["last_used"] > IDLE_S:
                caller, name = k.split(":", 1)
                await self.close(caller, name)
                n += 1
        return n

    async def close_all(self) -> None:
        for k in list(self.items):
            caller, name = k.split(":", 1)
            await self.close(caller, name)
