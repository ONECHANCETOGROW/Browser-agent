"""Wire protocol shared by the agent API and the Chrome extension.

command  (agent -> extension): {"request_id", "action", "parameters"}
response (extension -> agent): {"request_id", "success", "action", "result", "error", "verification", ...}
final    (agent -> caller):    {"request_id", "success", "action", "result", "error", "verification", "risk", "duration_ms"}
messages with a "type" field are control traffic: hello, hello_ack, hello_error, event, tabs_snapshot, status, ping, pong.
"""
from __future__ import annotations

import uuid
from typing import Any


class ErrorCode:
    # protocol / agent
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    INVALID_PARAMS = "INVALID_PARAMS"
    EXTENSION_NOT_CONNECTED = "EXTENSION_NOT_CONNECTED"
    EXTENSION_DISCONNECTED = "EXTENSION_DISCONNECTED"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    BAD_EXTENSION_RESPONSE = "BAD_EXTENSION_RESPONSE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # browser
    UNSUPPORTED_PAGE = "UNSUPPORTED_PAGE"
    STALE_ELEMENT = "STALE_ELEMENT"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    ELEMENT_NOT_INTERACTABLE = "ELEMENT_NOT_INTERACTABLE"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    # files
    FILE_NOT_AUTHORIZED = "FILE_NOT_AUTHORIZED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"


class AgentError(Exception):
    """Raised by agent-side logic (validation, file authorization, ...) and turned into a structured error response."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


def make_command(request_id: str, action: str, parameters: dict | None = None) -> dict:
    return {"request_id": request_id, "action": action, "parameters": parameters or {}}


def make_error(code: str, message: str, details: Any = None) -> dict:
    err = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return err


def build_response(
    request_id: str,
    action: str,
    success: bool,
    *,
    result: Any = None,
    error: dict | None = None,
    verification: Any = None,
    risk: dict | None = None,
    duration_ms: int | None = None,
) -> dict:
    """The canonical final response. Every key is always present so callers never need `.get`."""
    return {
        "request_id": request_id,
        "success": bool(success),
        "action": action,
        "result": result,
        "error": error,
        "verification": verification,
        "risk": risk,
        "duration_ms": duration_ms,
    }


def validate_extension_response(msg: Any) -> list[str]:
    """Return a list of problems (empty if the extension reply is well-formed)."""
    problems: list[str] = []
    if not isinstance(msg, dict):
        return ["response is not an object"]
    if not isinstance(msg.get("request_id"), str):
        problems.append("request_id missing")
    if not isinstance(msg.get("success"), bool):
        problems.append("success must be boolean")
    if msg.get("success") is False:
        err = msg.get("error")
        if not (isinstance(err, dict) and err.get("code") and err.get("message")):
            problems.append("failed responses need error.code and error.message")
    return problems
