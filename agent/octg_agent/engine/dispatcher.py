"""Runs one action end to end: validate -> assess risk -> (prepare) -> execute -> post-process -> log -> structured response."""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
import time
from datetime import datetime, timezone
from typing import Callable

from ..bridge.session import BridgeDisconnected, BridgeTimeout, ExtensionSession
from ..config import Settings
from ..files.authorization import AuthorizationError, AuthorizedFolders
from ..logging_setup import ActionLog
from ..sensitive import element_category, redact_call_params
from . import risk as risk_mod
from .local_actions import LOCAL
from .protocol import AgentError, ErrorCode, build_response, make_error, new_request_id, validate_extension_response
from .registry import ACTIONS
from .state import BrowserState
from .validate import validate

log = logging.getLogger("octg.dispatcher")

# How long the extension itself may legitimately wait for these (seconds) when the caller gives no timeout_ms.
EXT_WAIT_DEFAULTS = {"navigate": 30, "new_tab": 30, "reload": 30, "back": 30, "forward": 30, "download_file": 60, "wait": 10}
TIMEOUT_MARGIN_S = 15


class Dispatcher:
    def __init__(self, settings: Settings, state: BrowserState, folders: AuthorizedFolders, actionlog: ActionLog,
                 get_session: Callable[[], ExtensionSession | None]):
        self.settings = settings
        self.state = state
        self.folders = folders
        self.actionlog = actionlog
        self.get_session = get_session
        self._lock = asyncio.Lock()  # one action at a time: browser actions must not interleave

    # ------------------------------------------------------------------ public
    async def execute(self, action: str, parameters: dict | None = None, *, request_id: str | None = None,
                      timeout_s: float | None = None, source: str = "api", sensitive_call: bool = False) -> dict:
        """`sensitive_call`: the caller declares that this call carries a private value (e.g. one resolved from a secret store); the action log then
        never records the text it entered nor the value the page reported back. It changes logging only, never behaviour."""
        rid = request_id or new_request_id()
        params = dict(parameters or {})
        t0 = time.perf_counter()
        spec = ACTIONS.get(action)
        risk = None
        if spec is None:
            resp = build_response(rid, action, False, error=make_error(ErrorCode.UNKNOWN_ACTION, f"Unknown action: {action}", {"known_actions": sorted(ACTIONS)}))
        else:
            try:
                problems = self._check_params(spec, params)
                risk = spec.risk.as_dict() if problems else risk_mod.assess(spec, params, self.state)
            except Exception as e:  # a bug in validation must still yield a structured answer
                log.exception("Parameter check crashed for %s", action)
                problems = [f"internal validation error: {type(e).__name__}: {e}"]
                risk = spec.risk.as_dict()
            if problems:
                resp = build_response(rid, action, False, error=make_error(ErrorCode.INVALID_PARAMS, "Invalid parameters: " + "; ".join(problems), {"errors": problems}))
            else:
                async with self._lock:
                    self.state.current_action = action
                    await self.push_status()
                    try:
                        resp = await self._run(spec, rid, params, timeout_s)
                    except Exception as e:  # never let one action take the agent down
                        log.exception("Unhandled error in %s", action)
                        resp = build_response(rid, action, False, error=make_error(ErrorCode.INTERNAL_ERROR, f"{type(e).__name__}: {e}"))
                    finally:
                        self.state.current_action = None
                        await self.push_status()
        resp["risk"] = risk
        resp["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        self._log(resp, params, source, sensitive_call)
        return resp

    async def push_status(self) -> None:
        s = self.get_session()
        if s:
            await s.send_json({"type": "status", "task": self.state.task, "current_action": self.state.current_action})

    async def call_extension(self, action: str, parameters: dict, timeout_s: float = 15) -> dict:
        """Internal extension call for agent-side actions (does not take the action lock; callers already hold it)."""
        s = self.get_session()
        if not s:
            raise AgentError(ErrorCode.EXTENSION_NOT_CONNECTED, "The Chrome extension is not connected.")
        try:
            return await s.request(new_request_id(), action, parameters, timeout_s)
        except BridgeTimeout as e:
            raise AgentError(ErrorCode.ACTION_TIMEOUT, str(e)) from e
        except BridgeDisconnected as e:
            raise AgentError(ErrorCode.EXTENSION_DISCONNECTED, str(e)) from e

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _check_params(spec, params: dict) -> list[str]:
        errs = validate(spec.input_schema(), params)
        if errs:
            return errs
        if params.get("element_id") and not params.get("snapshot_id"):
            errs.append("snapshot_id is required with element_id (use the snapshot_id from inspect_page)")
        if spec.name == "click" and not params.get("element_id") and not ("x" in params and "y" in params):
            errs.append("click needs element_id (+snapshot_id) or both x and y")
        if spec.name == "upload_file" and not (params.get("local_file_path") or params.get("local_file_paths")):
            errs.append("upload_file needs local_file_path or local_file_paths")
        if spec.name == "switch_tab" and params.get("tab_id") is None and not params.get("match"):
            errs.append("switch_tab needs tab_id or match")
        if spec.name == "scroll" and not (params.get("direction") or params.get("to") or params.get("element_id")):
            errs.append("scroll needs direction, to, or element_id")
        if spec.name == "find_text" and not str(params.get("text", "")).strip():
            errs.append("find_text needs text with something other than whitespace")
        if spec.name == "wait" and params.get("ms") is None and not params.get("until"):
            errs.append("wait needs ms or until")
        if spec.name == "select" and all(params.get(k) is None for k in ("option", "values")):
            errs.append("select needs option or values")
        return errs

    async def _run(self, spec, rid: str, params: dict, timeout_s: float | None) -> dict:
        name = spec.name
        if spec.target == "agent":
            try:
                out = await LOCAL[name](self, params)
            except AgentError as e:
                return build_response(rid, name, False, error=make_error(e.code, e.message, e.details))
            return build_response(rid, name, True, result=out["result"])

        session = self.get_session()
        if session is None:
            return build_response(rid, name, False, error=make_error(
                ErrorCode.EXTENSION_NOT_CONNECTED, "The Chrome extension is not connected. Open the side panel and press Connect (see README)."))

        ext_params, local_files = params, None
        if name == "upload_file":
            try:
                ext_params, local_files = self._prepare_upload(params)
            except AgentError as e:
                return build_response(rid, name, False, error=make_error(e.code, e.message, e.details))

        ext_wait = (params.get("timeout_ms") or 0) / 1000 or (params.get("ms") or 0) / 1000 or EXT_WAIT_DEFAULTS.get(name, 0)
        timeout = timeout_s or max(self.settings.action_timeout_s, ext_wait + TIMEOUT_MARGIN_S)
        try:
            msg = await session.request(rid, name, ext_params, timeout)
        except BridgeTimeout as e:
            return build_response(rid, name, False, error=make_error(ErrorCode.ACTION_TIMEOUT, str(e), {"hint": "The page may be blocked by a JavaScript dialog or still loading."}))
        except BridgeDisconnected as e:
            return build_response(rid, name, False, error=make_error(ErrorCode.EXTENSION_DISCONNECTED, str(e)))

        bad = validate_extension_response(msg)
        if bad:
            return build_response(rid, name, False, error=make_error(ErrorCode.BAD_EXTENSION_RESPONSE, "Malformed reply from the extension: " + "; ".join(bad), {"raw_keys": sorted(msg)}))

        result = msg.get("result")
        if msg["success"]:
            result = self._postprocess(name, rid, params, result)
            if local_files is not None and isinstance(result, dict):
                result["local_files"] = local_files
        return build_response(rid, name, msg["success"], result=result, error=msg.get("error"), verification=msg.get("verification"))

    def _prepare_upload(self, params: dict) -> tuple[dict, list[dict]]:
        paths = params.get("local_file_paths") or [params["local_file_path"]]
        files, sources, total = [], [], 0
        for raw in paths:
            try:
                info = self.folders.read_for_upload(raw, self.settings.max_upload_bytes)
            except AuthorizationError as e:
                raise AgentError(e.code, e.message, e.details) from e
            total += info["size"]
            if total > self.settings.max_upload_bytes:
                raise AgentError(ErrorCode.FILE_TOO_LARGE, f"Combined upload exceeds {self.settings.max_upload_bytes} bytes.")
            mime = mimetypes.guess_type(info["name"])[0] or "application/octet-stream"
            files.append({"name": info["name"], "type": mime, "size": info["size"], "data_b64": base64.b64encode(info["data"]).decode("ascii")})
            sources.append({"path": info["path"], "size": info["size"], "sha256": info["sha256"]})
        ext = {k: params[k] for k in ("element_id", "snapshot_id", "tab_id") if k in params}
        ext["files"] = files
        return ext, sources

    def _postprocess(self, name: str, rid: str, params: dict, result):
        if not isinstance(result, dict):
            return result
        if name == "screenshot" and result.get("data_url"):
            head, _, b64 = result["data_url"].partition(",")
            raw = base64.b64decode(b64)
            ext = "jpg" if result.get("format") == "jpeg" else "png"
            self.settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.settings.screenshots_dir / f"shot_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{rid[-6:]}.{ext}"
            path.write_bytes(raw)
            result = {k: v for k, v in result.items() if k != "data_url"}
            result["path"] = str(path)
            result["bytes"] = len(raw)
            if params.get("include_base64"):
                result["data_base64"] = b64
        elif name == "inspect_page":
            self.state.record_snapshot(result)
        elif name == "get_tabs":
            self.state.replace_tabs(result.get("tabs", []))
        elif name == "get_browser_state":
            self.state.replace_tabs([t for w in result.get("windows", []) for t in w.get("tabs", [])])
        return result

    @staticmethod
    def _log_result(action: str, result, redact_values: bool = False):
        """Page content (element values, page text, check actuals) never goes to disk; only metadata does."""
        if not isinstance(result, dict):
            return result
        if redact_values and action in ("type", "clear", "select"):  # what the page reports back is the value that was entered
            return {k: ("[redacted]" if k in ("value_after", "selected") else v) for k, v in result.items()}
        if action == "inspect_page":
            return {k: result.get(k) for k in ("snapshot_id", "tab_id", "url", "title", "counts", "truncated")}
        if action == "read_page":
            return {k: result.get(k) for k in ("scope", "url", "title", "total_chars", "truncated", "frames")}
        if action == "find_text":  # matches carry page text (context, headings); only counts and positions are kept
            f = result.get("focused") or {}
            return {**{k: result.get(k) for k in ("found", "total_matches", "hidden_matches", "case_sensitive", "whole_word", "url", "title", "frames_searched", "scroll")},
                    "focused": {k: f.get(k) for k in ("match_index", "in_viewport", "page_y", "frame")} if f else None}
        if action == "get_page_state":
            return {"tab": {k: (result.get("tab") or {}).get(k) for k in ("tab_id", "url", "title")}}
        if action in ("verify", "wait") and isinstance(result.get("checks"), list):
            safe = ("url_contains", "url_equals", "url_regex", "title_contains", "tab_count", "tab_active")
            checks = [{"type": c.get("type"), "passed": c.get("passed"), **({"actual": c.get("actual")} if c.get("type") in safe else {})} for c in result["checks"]]
            return {**{k: v for k, v in result.items() if k != "checks"}, "checks": checks}
        return result

    def _log(self, resp: dict, params: dict, source: str, sensitive_call: bool = False) -> None:
        v = resp.get("verification") or {}
        # generic sensitive-field protection: the FIELD (password/OTP/card/PAN/GSTIN/bank/identity by its own label) or the VALUE's shape decides
        el = self.state.element(params.get("tab_id") if params.get("tab_id") is not None else self.state.active_tab_id(), params.get("snapshot_id"), params["element_id"])             if params.get("element_id") and params.get("snapshot_id") else None
        category = element_category(el)
        params, hit = redact_call_params(params, category=category, forced=sensitive_call)
        redact_values = hit or bool(category) or sensitive_call
        if resp["action"] in ("verify", "wait"):  # a value being compared against a field is field content
            def scrub(checks):
                return [{k: ("[not logged]" if c.get("type") == "element_value" and k in ("equals", "contains") else v) for k, v in c.items()} if isinstance(c, dict) else c for c in checks]
            params = {**params, **{k: scrub(params[k]) for k in ("checks", "until") if isinstance(params.get(k), list)}}
        if resp["action"] == "find_text" and isinstance(params.get("text"), str) and len(params["text"]) > 80:
            params = {**params, "text": params["text"][:80] + "…"}  # a search phrase is short; never log a pasted blob
        if "credential_field" in ((resp.get("risk") or {}).get("tags") or []) and "text" in params:
            params = {**params, "text": "[redacted: credential field]"}  # never persist what was typed into a password field
        self.actionlog.write(
            request_id=resp["request_id"], action=resp["action"], source=source, parameters=params, success=resp["success"],
            result=self._log_result(resp["action"], resp["result"], redact_values), error=resp["error"], duration_ms=resp["duration_ms"],
            risk=(resp["risk"] or {}).get("level"), observed_change=v.get("observed_change") if isinstance(v, dict) else None,
        )
