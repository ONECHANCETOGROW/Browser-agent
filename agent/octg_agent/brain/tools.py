"""LLM-facing tools, derived from the existing action registry. There is no second action system.

* Every browser tool the model can call IS a registry action; the catalog only compacts its schema (short descriptions, simple types,
  `snapshot_id` hidden because the orchestrator binds element ids to the snapshot the model was shown).
* Every tool gets `reason` and `note`; mutating tools also get `expect_url_contains` / `expect_text`, which the loop turns into an engine
  `verify` call after the action.
* Three CONTROL tools exist, and they never touch the browser: finish_task, ask_user, request_approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..engine.registry import ACTIONS, ActionSpec
from ..reasoning.provider import ToolCall

HIDDEN = {"ping", "get_browser_state", "get_active_tab", "get_agent_state", "get_events", "set_task", "fs_list_roots", "list_downloads"}
CONTROL = {"finish_task", "ask_user", "request_approval"}
# Token budget: the tool list is sent on every call, so the LLM view is deliberately small. The registry keeps every parameter; these only
# decide what the MODEL is told about. The orchestrator fills in or ignores the rest.
HIDE_PARAMS = {
    "inspect_page": {"max_elements", "include_selectors", "include_text", "text_chars"}, "click": {"force"}, "type": {"mode"}, "select": {"values"},
    "new_tab": {"window_id", "timeout_ms", "active"}, "navigate": {"timeout_ms"}, "reload": {"bypass_cache", "timeout_ms"}, "back": {"timeout_ms"},
    "forward": {"timeout_ms"}, "check": {"force"}, "uncheck": {"force"}, "wait": {"until", "poll_ms", "timeout_ms"}, "screenshot": {"format", "quality", "include_base64"},
    "download_file": {"timeout_ms"}, "press_key": {"modifiers"}, "read_page": set(), "get_page_state": set(), "scroll": set(),
    "find_text": {"whole_word", "max_matches", "context_chars", "scroll"},
}
KEEP_DESC = {"element_id": "id from the current observation", "url": "address or domain", "text": "text to enter", "key": "e.g. Enter, Escape, Tab", "option": "value, label or index",
             "direction": "", "amount": "pixels", "scope": "viewport or page", "checks": "list of {type,value}, e.g. {type:text_present,value:...}", "ms": "milliseconds",
             "index": "match_index to scroll to (0 = first)"}
# Element actions and navigation always act on the tab the model was shown; only tab-switching and read-only tools take a tab_id.
for _n in ("click", "type", "clear", "select", "check", "uncheck", "scroll", "press_key", "navigate", "reload", "back", "forward", "upload_file", "wait", "find_text"):
    HIDE_PARAMS.setdefault(_n, set()).add("tab_id")
DESC_OVERRIDE = {"click": "Click an element by id, or a viewport point (x, y).", "scroll": "Scroll the page (direction or to top/bottom) or bring an element into view.",
                 "upload_file": "Attach a local file from an authorized folder to a file input.", "verify": "Check conditions about the page/tabs; succeeds only if all hold.",
                 "find_text": "Locate a word/phrase within the loaded page (best for long pages): scrolls a match into view, returns the text around it and each match's type (heading/link/body_text). index picks which match. Not for submitting a search."}
TOKEN_HINTS = {"type": ("text", "text to enter, or {{biz:KEY}} for a listed business field"), "select": ("option", "value, label or index, or {{biz:KEY}}"),
               "upload_file": ("local_file_path", "{{doc:ID}} for a listed document")}
EXPECT_TOOLS = {"click", "type", "press_key", "navigate", "select", "check"}
NOTE_TOOLS = {"read_page", "click", "scroll", "get_page_state"}
NO_AUTO_TAB = {"switch_tab", "close_tab", "new_tab", "get_tabs", "download_file", "fs_find", "fs_save_download"}


class ToolCallError(Exception):
    """The model's reply cannot be turned into a valid call; the message is fed back to it."""


@dataclass
class ParsedCall:
    kind: str  # browser | control
    name: str
    args: dict[str, Any]
    reason: str = ""
    note: str = ""
    expect: list[dict] = field(default_factory=list)


def _first_sentence(text: str, limit: int = 130) -> str:
    s = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _simple_type(t: Any) -> str:
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        return non_null[0] if non_null else "string"
    return t or "string"


def _compact_prop(name: str, p: dict) -> dict:
    out: dict[str, Any] = {"type": _simple_type(p.get("type"))}
    if "enum" in p:
        out["enum"] = p["enum"]
    if KEEP_DESC.get(name):
        out["description"] = KEEP_DESC[name]
    if out["type"] == "array":
        out["items"] = {"type": "string"} if p.get("items", {}).get("type") == "string" else {"type": "object"}
    return out


CONTROL_TOOLS: list[dict] = [
    {"name": "finish_task", "description": "End the task: completed only if the page shows the goal is achieved, else failed.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["completed", "failed"]},
         "summary": {"type": "string"}}, "required": ["status", "summary"]}},
    {"name": "ask_user", "description": "Ask the human for help: login, CAPTCHA, 2FA, missing detail.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["login", "captcha", "info", "other"]},
         "question": {"type": "string"}}, "required": ["kind", "question"]}},
    {"name": "request_approval", "description": "Ask the human to approve an irreversible step BEFORE doing it (submit, send, publish, buy, delete).",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string"}}, "required": ["summary"]}},
]


class ToolCatalog:
    def __init__(self, registry: dict[str, ActionSpec] | None = None, hidden: set[str] | None = None, token_hints: bool = False):
        self.registry = registry or ACTIONS
        self.hidden = HIDDEN if hidden is None else hidden
        self.token_hints = token_hints  # Business Brain enabled: tell the model that business details are written as tokens
        self._tools: list[dict] | None = None

    # ---- what the model sees
    def browser_names(self) -> list[str]:
        return [n for n in self.registry if n not in self.hidden]

    def all_names(self) -> list[str]:
        return self.browser_names() + [t["name"] for t in CONTROL_TOOLS]

    def llm_tools(self) -> list[dict]:
        if self._tools is None:
            tools = []
            for name in self.browser_names():
                spec = self.registry[name]
                hide = HIDE_PARAMS.get(name, set()) | {"snapshot_id"}
                props = {k: _compact_prop(k, v) for k, v in spec.properties.items() if k not in hide}
                if name == "find_text":
                    props["text"]["description"] = "word or phrase to find on the page"
                if self.token_hints and name in TOKEN_HINTS:
                    param, hint = TOKEN_HINTS[name]
                    if param in props:
                        props[param]["description"] = hint
                props["reason"] = {"type": "string"}
                if name in NOTE_TOOLS:
                    props["note"] = {"type": "string", "description": "optional fact to remember"}
                if name in EXPECT_TOOLS:
                    props["expect_url_contains"] = {"type": "string"}
                    props["expect_text"] = {"type": "string", "description": "optional: text expected on the page afterwards"}
                required = [r for r in spec.required if r not in hide]
                tools.append({"name": name, "description": DESC_OVERRIDE.get(name) or _first_sentence(spec.description, 100), "input_schema": {"type": "object", "properties": props, "required": required}})
            tools.extend(CONTROL_TOOLS)
            self._tools = tools
        return self._tools

    def approx_tokens(self) -> int:
        import json

        return len(json.dumps(self.llm_tools())) // 4

    # ---- what the model sent back
    def parse(self, call: ToolCall) -> ParsedCall:
        name = (call.name or "").strip()
        args = dict(call.arguments or {})
        if name not in self.all_names():
            hint = ""
            close = [n for n in self.all_names() if name and (name in n or n in name)]
            if close:
                hint = f" Did you mean {close[0]}?"
            raise ToolCallError(f"Unknown tool {name!r}.{hint} Choose one of: {', '.join(self.all_names())}.")
        reason = str(args.pop("reason", "") or "")[:160]
        note = str(args.pop("note", "") or "")[:200]
        expect: list[dict] = []
        if (v := args.pop("expect_url_contains", None)):
            expect.append({"type": "url_contains", "value": str(v)})
        if (v := args.pop("expect_text", None)):
            expect.append({"type": "text_present", "value": str(v)})
        if name in CONTROL:
            return ParsedCall("control", name, self._control_args(name, args), reason, note)
        spec = self.registry[name]
        args.pop("snapshot_id", None)  # never trust a model-supplied snapshot id; the orchestrator binds the one it showed
        args = self._coerce(spec, args)
        unknown = [k for k in args if k not in spec.properties]
        if unknown:
            raise ToolCallError(f"{name} has no parameter(s) {unknown}. Parameters: {[k for k in spec.properties if k != 'snapshot_id']}.")
        return ParsedCall("browser", name, args, reason, note, expect)

    @staticmethod
    def _control_args(name: str, args: dict) -> dict:
        if name == "finish_task":
            st = str(args.get("status", "")).lower()
            if st not in ("completed", "failed"):
                raise ToolCallError("finish_task.status must be 'completed' or 'failed'.")
            return {"status": st, "summary": str(args.get("summary", ""))[:600]}
        if name == "ask_user":
            return {"kind": str(args.get("kind", "other")).lower(), "question": str(args.get("question", ""))[:400] or "The agent needs your help to continue."}
        return {"summary": str(args.get("summary", ""))[:300] or "an irreversible step"}

    @staticmethod
    def _coerce(spec: ActionSpec, args: dict) -> dict:
        """Small models send numbers as strings and booleans as words; fix what is unambiguous, leave the rest for validation."""
        out = {}
        for k, v in args.items():
            t = _simple_type(spec.properties.get(k, {}).get("type"))
            if isinstance(v, str):
                s = v.strip()
                if t == "integer" and re.fullmatch(r"-?\d+", s):
                    v = int(s)
                elif t == "number" and re.fullmatch(r"-?\d+(\.\d+)?", s):
                    v = float(s) if "." in s else int(s)
                elif t == "boolean" and s.lower() in ("true", "false"):
                    v = s.lower() == "true"
            elif t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool) and k in ("option", "text"):
                v = str(v)
            out[k] = v
        return out
