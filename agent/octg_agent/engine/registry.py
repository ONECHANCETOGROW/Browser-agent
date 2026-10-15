"""The single source of truth for what the engine can do.

Each entry carries a JSON-Schema for its parameters and a structured risk tag. `tool_schema()` exports the whole registry in a
provider-neutral form so any future LLM can call the same capabilities without changes to the browser engine.

Approval is NOT enforced in Phase 1 (`approval_enforced` is always False in responses); the tags exist so a later phase can gate
actions without touching the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import PROTOCOL_VERSION


@dataclass(frozen=True)
class Risk:
    level: str  # none | low | medium | high
    tags: tuple[str, ...] = ()
    approval: str = "never"  # never | contextual | required   (what a future approval gate should do)

    def as_dict(self) -> dict:
        return {"level": self.level, "tags": list(self.tags), "approval": self.approval, "approval_enforced": False, "reasons": []}


READ = Risk("none", ("read_only",))
FIND = Risk("none", ("read_only", "moves_viewport"))
NAV = Risk("low", ("navigation",))
INPUT = Risk("low", ("input",))
CLICK = Risk("medium", ("interaction", "may_trigger_irreversible_action"), "contextual")
KEY = Risk("medium", ("input", "may_submit_form"), "contextual")
TABCLOSE = Risk("medium", ("tab_close", "may_lose_unsaved_state"), "contextual")
SHOT = Risk("low", ("read_only", "captures_screen_content"))
UPLOAD = Risk("high", ("file_upload", "filesystem", "sends_local_data_to_website"), "required")
DOWNLOAD = Risk("medium", ("file_download", "filesystem"), "contextual")
FSREAD = Risk("low", ("filesystem", "read_only"))
FSWRITE = Risk("medium", ("filesystem", "file_write"), "contextual")


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    risk: Risk
    category: str
    target: str = "extension"  # extension | agent
    timeout_s: float | None = None
    mutating: bool = False

    def input_schema(self) -> dict:
        return {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False}

    @property
    def uses_elements(self) -> bool:
        return "element_id" in self.properties


def _s(desc: str, **kw: Any) -> dict:
    return {"type": "string", "description": desc, **kw}


def _i(desc: str, **kw: Any) -> dict:
    return {"type": "integer", "description": desc, **kw}


def _b(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


TAB = _i("Target tab id. Defaults to the active tab of the focused window.")
EL = _s("Element id from inspect_page (e.g. el_004). Only valid together with the snapshot_id it came from.", minLength=1)
SNAP = _s("snapshot_id returned by the inspect_page call that produced element_id. Stale snapshots fail with STALE_ELEMENT.", minLength=1)
TIMEOUT = _i("Maximum wait in milliseconds.", minimum=100, maximum=120000)
CHECK = {"type": "object", "description": "One condition. type is one of: url_contains, url_equals, url_regex, title_contains, text_present, "
         "text_absent, element_exists, element_absent, element_value, element_checked, tab_count, tab_active. Other keys depend on type "
         "(value, role, name_contains, tag, element_id+snapshot_id with equals/contains, equals/at_least).",
         "properties": {"type": {"type": "string"}}, "required": ["type"]}

_ACTIONS: list[ActionSpec] = []


def _add(name: str, description: str, props: dict | None = None, required: tuple[str, ...] = (), risk: Risk = READ, category: str = "observe",
         target: str = "extension", timeout_s: float | None = None, mutating: bool = False) -> None:
    _ACTIONS.append(ActionSpec(name, description, props or {}, required, risk, category, target, timeout_s, mutating))


# ---- observation
_add("ping", "Round-trip check with the Chrome extension; returns extension version and protocol.")
_add("get_browser_state", "Overview of Chrome: all windows and their tabs, the active tab, the current website, and the latest page snapshot.")
_add("get_tabs", "List every open tab (id, window, title, url, active, status, whether the extension can control it).")
_add("get_active_tab", "The active tab of the focused window.")
_add("get_page_state", "Lightweight state of a page: url, title, scroll position, headings, open dialogs/alerts, focused element. No element list.",
     {"tab_id": TAB})
_add("inspect_page", "Inspect the page DOM/ARIA structure and list the interactive elements a human could use, with ids bound to a new snapshot_id. "
     "Prefer this over screenshots.",
     {"tab_id": TAB, "viewport_only": _b("Only elements currently on screen."), "max_elements": _i("Cap on returned elements (default 300).", minimum=1, maximum=1000),
      "include_selectors": _b("Include a CSS selector fallback per element."), "include_text": _b("Also include the visible text."),
      "text_chars": _i("Max characters of visible text.", minimum=100, maximum=50000)})
_add("read_page", "Read the visible text of a page (viewport) or all of its text (page).",
     {"tab_id": TAB, "scope": _s("viewport (default) or page.", enum=["viewport", "page"]), "max_chars": _i("Max characters (default 6000).", minimum=100, maximum=100000)})
_add("find_text", "Find a word or phrase in the page text (DOM text, never screenshots; form-field contents are never searched) and scroll the match into view. "
     "Returns whether it was found, the number of matches, and for each match its surrounding text and what it is (match_type heading/link/body_text/other, "
     "is_heading, heading_level, is_link, element_tag, role, nearest_heading, visibility), plus element_id when the current inspect_page snapshot covers it. "
     "Matches are in document order and nothing is ranked or skipped: when there are several, choose the occurrence you want and pass its match_index as index. "
     "Case-insensitive by default.",
     {"text": _s("Text to look for (whitespace is normalised).", minLength=1), "tab_id": TAB, "case_sensitive": _b("Match case exactly (default false)."),
      "whole_word": _b("Only match whole words (default false)."), "index": _i("Which match to scroll to: its match_index, 0-based (default 0).", minimum=0),
      "max_matches": _i("How many matches to describe (default 10).", minimum=1, maximum=50),
      "context_chars": _i("Characters of surrounding text on each side of a match (default 80).", minimum=0, maximum=300),
      "scroll": _b("Bring the chosen match into view (default true). false only counts and describes.")},
     ("text",), FIND, "observe", mutating=True)
_add("screenshot", "Capture the visible viewport of a tab (PNG/JPEG). The image is saved locally and its path returned. Activates the tab if needed.",
     {"tab_id": TAB, "format": _s("png (default) or jpeg.", enum=["png", "jpeg"]), "quality": _i("JPEG quality.", minimum=1, maximum=100),
      "include_base64": _b("Also return the image as base64 (large).")}, risk=SHOT)
_add("list_downloads", "Recent Chrome downloads with state and local file path.", {"limit": _i("Max items (default 20).", minimum=1, maximum=100)})
_add("verify", "Check conditions about the current state (url/title/text/elements/tabs). Succeeds only if every check passes.",
     {"tab_id": TAB, "checks": {"type": "array", "items": CHECK, "description": "Conditions that must all hold."}}, ("checks",))
_add("wait", "Pause for a fixed time (ms) or poll until conditions hold (until) up to timeout_ms.",
     {"tab_id": TAB, "ms": _i("Fixed delay in ms.", minimum=0, maximum=60000), "until": {"type": "array", "items": CHECK, "description": "Conditions to wait for."},
      "timeout_ms": TIMEOUT, "poll_ms": _i("Polling interval.", minimum=50, maximum=5000)})

# ---- navigation and tabs
_add("navigate", "Load a URL in a tab and wait for the navigation to finish. Scheme-less input gets https://.",
     {"url": _s("Destination URL.", minLength=1), "tab_id": TAB, "timeout_ms": TIMEOUT}, ("url",), NAV, "navigation", mutating=True)
_add("new_tab", "Open a new tab, optionally at a URL.", {"url": _s("Optional URL."), "active": _b("Focus the new tab (default true)."), "window_id": _i("Window to open in."), "timeout_ms": TIMEOUT},
     risk=NAV, category="tabs", mutating=True)
_add("switch_tab", "Focus a tab by tab_id, or by match on url/title.",
     {"tab_id": TAB, "match": {"type": "object", "description": "{url_contains?, title_contains?}", "properties": {"url_contains": {"type": "string"}, "title_contains": {"type": "string"}}}},
     risk=NAV, category="tabs", mutating=True)
_add("close_tab", "Close a tab (tab_id required). Refuses to close the last tab of the last window.", {"tab_id": _i("Tab to close.")}, ("tab_id",), TABCLOSE, "tabs", mutating=True)
_add("reload", "Reload a tab and wait for it to finish.", {"tab_id": TAB, "bypass_cache": _b("Skip the cache."), "timeout_ms": TIMEOUT}, risk=NAV, category="navigation", mutating=True)
_add("back", "Go back in the tab's history.", {"tab_id": TAB, "timeout_ms": TIMEOUT}, risk=NAV, category="navigation", mutating=True)
_add("forward", "Go forward in the tab's history.", {"tab_id": TAB, "timeout_ms": TIMEOUT}, risk=NAV, category="navigation", mutating=True)

# ---- interaction (all return before/after verification evidence)
_add("click", "Click an element (element_id + snapshot_id) or a viewport coordinate (x, y). Reports what changed.",
     {"element_id": EL, "snapshot_id": SNAP, "x": {"type": "number", "description": "Viewport x (with y, instead of element_id)."}, "y": {"type": "number", "description": "Viewport y."},
      "force": _b("Click even if another element covers it."), "tab_id": TAB}, risk=CLICK, category="interact", mutating=True)
_add("type", "Type text into an input, textarea or contenteditable. Replaces existing content unless clear=false. Reads the value back to confirm.",
     {"element_id": EL, "snapshot_id": SNAP, "text": {"type": "string", "description": "Text to enter."}, "clear": _b("Replace existing content (default true)."),
      "mode": _s("set (default, fast) or keys (per-character key events).", enum=["set", "keys"]), "tab_id": TAB},
     ("element_id", "snapshot_id", "text"), INPUT, "interact", mutating=True)
_add("clear", "Empty an input/textarea/contenteditable.", {"element_id": EL, "snapshot_id": SNAP, "tab_id": TAB}, ("element_id", "snapshot_id"), INPUT, "interact", mutating=True)
_add("select", "Choose an option in a native <select> by value, label or index.",
     {"element_id": EL, "snapshot_id": SNAP, "option": {"type": ["string", "integer", "number"], "description": "Option value, visible label, or zero-based index."},
      "values": {"type": "array", "description": "Several options (multi-select)."}, "tab_id": TAB}, ("element_id", "snapshot_id"), INPUT, "interact", mutating=True)
_add("check", "Tick a checkbox/radio/switch and confirm its state.", {"element_id": EL, "snapshot_id": SNAP, "force": _b("Ignore overlays."), "tab_id": TAB}, ("element_id", "snapshot_id"), INPUT, "interact", mutating=True)
_add("uncheck", "Untick a checkbox/switch and confirm its state.", {"element_id": EL, "snapshot_id": SNAP, "force": _b("Ignore overlays."), "tab_id": TAB}, ("element_id", "snapshot_id"), INPUT, "interact", mutating=True)
_add("scroll", "Scroll the page (or the element's scroll container) by direction/amount or to top/bottom, or bring an element into view.",
     {"direction": _s("up | down | left | right", enum=["up", "down", "left", "right"]), "amount": {"type": ["number", "string"], "description": "Pixels, or 'page'. Default ~85% of the viewport."},
      "to": _s("top | bottom | left | right", enum=["top", "bottom", "left", "right"]), "element_id": EL, "snapshot_id": SNAP, "tab_id": TAB},
     risk=INPUT, category="interact", mutating=True)
_add("press_key", "Send a key (Enter, Tab, Escape, ArrowDown, a, …) to the focused or given element. Page-synthesised events are untrusted; common defaults are emulated.",
     {"key": _s("Key name as in KeyboardEvent.key.", minLength=1), "modifiers": {"type": "array", "items": {"type": "string"}, "description": "ctrl, shift, alt, meta"},
      "element_id": EL, "snapshot_id": SNAP, "tab_id": TAB}, ("key",), KEY, "interact", mutating=True)

# ---- files
_add("upload_file", "Attach a local file from an AUTHORIZED folder to an <input type=file>. The agent reads the file; the extension never sees a path.",
     {"element_id": EL, "snapshot_id": SNAP, "local_file_path": _s("Absolute path inside an authorized folder."),
      "local_file_paths": {"type": "array", "items": {"type": "string"}, "description": "Several files (multi-file inputs)."}, "tab_id": TAB},
     ("element_id", "snapshot_id"), UPLOAD, "files", mutating=True)
_add("download_file", "Download an http(s) URL through Chrome (uses the browser's logged-in session) into Chrome's download folder.",
     {"url": _s("File URL.", minLength=1), "filename": _s("Optional name/subpath relative to the download folder."), "timeout_ms": TIMEOUT}, ("url",), DOWNLOAD, "files", mutating=True)

# ---- agent-side (never reach the browser)
_add("get_agent_state", "Local agent status: extension connection, tab model, latest snapshots, authorized-folder count.", category="agent", target="agent")
_add("get_events", "Browser events seen by the agent (tab opened/closed/activated, navigations, downloads).",
     {"since": _i("Return events with seq greater than this."), "limit": _i("Max events.", minimum=1, maximum=500)}, category="agent", target="agent")
_add("set_task", "Set the human-readable current task label shown in the side panel.", {"task": {"type": ["string", "null"], "description": "Label, or null to clear."}},
     ("task",), category="agent", target="agent")
_add("fs_list_roots", "List the authorized local folders (managed only via the local CLI).", risk=FSREAD, category="files", target="agent")
_add("fs_find", "Find files by name pattern (e.g. *.png) inside authorized folders.",
     {"pattern": _s("Filename glob.", minLength=1), "root": _s("Limit to this authorized folder (or a sub-folder of one)."), "max_results": _i("Default 50.", minimum=1, maximum=500)},
     ("pattern",), FSREAD, "files", "agent")
_add("fs_save_download", "Copy a completed Chrome download into an authorized folder.",
     {"download_id": _i("Id from download_file / list_downloads."), "dest_dir": _s("Authorized destination folder."), "filename": _s("Optional new file name."), "overwrite": _b("Replace an existing file.")},
     ("download_id", "dest_dir"), FSWRITE, "files", "agent")

ACTIONS: dict[str, ActionSpec] = {a.name: a for a in _ACTIONS}


def tool_schema() -> dict:
    """Provider-neutral, machine-readable description of every action (JSON-Schema inputs + risk tags)."""
    return {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "tools": [
            {
                "name": a.name,
                "description": a.description,
                "category": a.category,
                "input_schema": a.input_schema(),
                "risk": {"level": a.risk.level, "tags": list(a.risk.tags), "approval": a.risk.approval},
                "target": a.target,
                "mutating": a.mutating,
            }
            for a in _ACTIONS
        ],
    }
