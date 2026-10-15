"""The capability registry: the ONLY operations a remote caller can ask this agent to perform.

A capability is a named, documented, schema-checked operation with declared permissions, client scoping, side-effect class, approval rule, timeout and
limitations. Adding a new tool later = writing one handler and calling `register(...)`; the protocol, the HTTP surface, the worker and the client never change.

Deliberately impossible through this registry: arbitrary Python, shell commands, arbitrary filesystem paths, and raw browser actions (click/type/upload...).
Browser work is exposed only as read-only, purpose-built capabilities that open and close their own tab.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..engine.validate import validate

# Permission vocabulary. A caller's token is granted a subset; a capability lists what it needs.
PERMISSIONS = {
    "agent:read": "Read the agent's own status and capability list.",
    "clients:list": "List client ids and names (no facts).",
    "client:read": "Read ONE client's verified business context (protected fields are never returned).",
    "net:fetch": "Fetch public web pages over HTTP(S) from this machine (private and loopback addresses are refused).",
    "browser:read": "Open the agent's own browser tab, read pages, close the tab. No clicking, typing or uploads.",
    "search:read": "Run a web search in the agent's browser and read the results page.",
    "research:write": "Record dated research observations and campaigns for one client.",
    "connector:read": "Read ONE client's data from the owner-authorized data sources mapped to that client (GSC, Bing Webmaster). Never other clients', never credentials.",
    "owner:message": "Send the owner a step-by-step instruction or question (shown in the Chrome side panel and the Hub inbox) and read the owner's answers to the requests this caller made.",
    "browser:act": "Drive ONE dedicated tab in the owner's real Chrome (open pages, scroll, click, sort, page through tables) with refusals for anything that looks like it changes something and for the sites the owner blocked.",
    "web:post": "Post a public reply on a website in the owner's real Chrome (e.g. answer a Google review). Only ever runs after the owner approves the exact text; the approval is what makes it safe.",
    "collab:write": "Record artifacts (analyses, reports, handoffs, change and verification records) and deployment targets.",
    "approval:request": "Ask the owner for approval of a high-impact action (cannot approve anything).",
    "agent:task": "Start an instruction on the agent's own AI task loop (its own approval gate still applies).",
    "test:internal": "Test-only capabilities (never granted by default).",
}
DEFAULT_GRANTS = tuple(p for p in PERMISSIONS if p not in ("agent:task", "test:internal"))

Handler = Callable[["Any", dict], Awaitable[dict]]


@dataclass(frozen=True)
class Capability:
    name: str
    purpose: str
    properties: dict
    required: tuple[str, ...]
    output: dict                      # human/machine description of the result object
    permissions: tuple[str, ...]
    handler: Handler
    category: str = "general"
    scope: str = "none"               # none | client (a client id/name is REQUIRED) | client_optional
    read_only: bool = True            # False: may change state somewhere (local records or the outside world)
    external_effects: bool = False    # True: may change something OUTSIDE this machine (a website, an account, a message)
    approval: str = "never"           # never | required (the job waits in awaiting_approval until the owner decides)
    approval_kind: str = ""
    timeout_s: float = 60.0
    max_timeout_s: float = 600.0
    cancellable: bool = True
    needs_browser: bool = False
    limitations: tuple[str, ...] = ()
    version: int = 1

    def input_schema(self) -> dict:
        return {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False}

    def check(self, params: dict) -> list[str]:
        return validate(self.input_schema(), params)

    def describe(self) -> dict:
        return {"name": self.name, "version": self.version, "category": self.category, "purpose": self.purpose, "input_schema": self.input_schema(), "output": self.output,
                "permissions": list(self.permissions), "client_scope": self.scope, "read_only": self.read_only, "external_effects": self.external_effects,
                "approval": self.approval, "approval_kind": self.approval_kind or None, "timeout_s": {"default": self.timeout_s, "max": self.max_timeout_s},
                "cancellable": self.cancellable, "needs_browser": self.needs_browser, "limitations": list(self.limitations)}


REGISTRY: dict[str, Capability] = {}


def register(cap: Capability) -> Capability:
    if cap.name in REGISTRY:
        raise ValueError(f"capability {cap.name} registered twice")
    unknown = [p for p in cap.permissions if p not in PERMISSIONS]
    if unknown:
        raise ValueError(f"{cap.name}: unknown permissions {unknown}")
    if cap.external_effects and cap.read_only:
        raise ValueError(f"{cap.name}: a capability with external effects cannot be read-only")
    if cap.external_effects and cap.approval != "required":
        raise ValueError(f"{cap.name}: external effects always need owner approval")
    REGISTRY[cap.name] = cap
    return cap


def capability(name: str, purpose: str, *, props: dict | None = None, required: tuple[str, ...] = (), output: dict | None = None, permissions: tuple[str, ...] = (), **kw: Any):
    """Decorator form of `register`."""
    def deco(fn: Handler) -> Handler:
        register(Capability(name, purpose, props or {}, required, output or {}, permissions, fn, **kw))
        return fn
    return deco


def catalog(granted: tuple[str, ...] | None = None) -> dict:
    from . import PROTOCOL
    caps = [c.describe() | ({"available_to_caller": all(p in granted for p in c.permissions)} if granted is not None else {}) for c in REGISTRY.values()]
    return {"protocol": PROTOCOL, "capabilities": sorted(caps, key=lambda c: (c["category"], c["name"])), "permissions": PERMISSIONS}


# JSON-Schema helpers shared by the capability modules
def S(desc: str, **kw: Any) -> dict:
    return {"type": "string", "description": desc, **kw}


def I(desc: str, **kw: Any) -> dict:
    return {"type": "integer", "description": desc, **kw}


def B(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


def A(desc: str, items: dict | None = None, **kw: Any) -> dict:
    return {"type": "array", "description": desc, "items": items or {"type": "string"}, **kw}


def O(desc: str, **kw: Any) -> dict:
    return {"type": "object", "description": desc, **kw}


@dataclass
class Outcome:
    """What a handler may set besides its result (the worker copies these into the response)."""
    completeness: str = "complete"          # complete | partial | none
    confidence: str = "high"                # high | medium | low | n/a
    limitations: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
