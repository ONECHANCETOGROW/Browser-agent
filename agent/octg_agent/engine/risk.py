"""Dynamic risk assessment: start from the registry's static tags, escalate using what we know about the target element.

Phase 1 only TAGS actions. Nothing here blocks anything; a later approval gate reads `risk.approval` / `risk.level`.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .registry import ActionSpec
from .state import BrowserState

_LEVELS = ["none", "low", "medium", "high"]
_APPROVAL = ["never", "contextual", "required"]

IRREVERSIBLE = re.compile(
    r"\b(delete|remove|erase|destroy|discard|publish|post|send|submit|pay|purchase|buy|checkout|place order|confirm|subscribe|unsubscribe|"
    r"transfer|withdraw|deposit|sign ?out|log ?out|deactivate|close account|reset|approve|authori[sz]e|grant|invite|share|donate)\b",
    re.I,
)


def _escalate(risk: dict, level: str | None = None, tags: tuple[str, ...] = (), approval: str | None = None, reason: str | None = None) -> None:
    if level and _LEVELS.index(level) > _LEVELS.index(risk["level"]):
        risk["level"] = level
    if approval and _APPROVAL.index(approval) > _APPROVAL.index(risk["approval"]):
        risk["approval"] = approval
    for t in tags:
        if t not in risk["tags"]:
            risk["tags"].append(t)
    if reason:
        risk["reasons"].append(reason)


def assess(spec: ActionSpec, params: dict, state: BrowserState) -> dict:
    risk = spec.risk.as_dict()
    el = None
    if params.get("element_id"):
        tab = params.get("tab_id") if params.get("tab_id") is not None else state.active_tab_id()
        el = state.element(tab, params.get("snapshot_id"), params["element_id"])
    if el:
        label = f"{el.get('name') or ''} {el.get('href') or ''}"
        m = IRREVERSIBLE.search(label)
        if m and spec.name in ("click", "press_key", "check", "uncheck", "select"):
            _escalate(risk, "high", ("potentially_irreversible",), "required", f"element label matches '{m.group(0).lower()}'")
        if (el.get("type") == "password" or el.get("sensitive")) and spec.name in ("type", "clear"):
            _escalate(risk, "high", ("credential_field",), "required", "target is a password or other sensitive field")
        if (el.get("submits_form") or el.get("type") in ("submit", "image")) and spec.name == "click":
            _escalate(risk, "medium", ("form_submit",), "contextual", "target submits a form")
    if spec.name == "press_key" and str(params.get("key", "")).lower() == "enter":
        _escalate(risk, None, ("may_submit_form",), "contextual", "Enter can submit a form")
    if spec.name in ("navigate", "new_tab") and params.get("url"):
        scheme = urlparse(params["url"] if "://" in params["url"] or ":" in params["url"][:12] else "https://" + params["url"]).scheme.lower()
        if scheme in ("file", "chrome", "chrome-extension", "view-source", "data", "about"):
            _escalate(risk, "medium", ("non_web_scheme",), "contextual", f"{scheme}: URL")
    return risk
