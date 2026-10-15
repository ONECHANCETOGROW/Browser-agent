"""WebSocket endpoint the Chrome extension dials into: /ext.

Security: loopback only (enforced at bind time), Origin must be our extension, first message must be a hello carrying the pairing token.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
import logging

from aiohttp import WSMsgType, web
from ..appkeys import RT

from .. import PROTOCOL_VERSION, __version__
from ..engine.protocol import validate_extension_response
from .session import ExtensionSession

log = logging.getLogger("octg.bridge")
MAX_WS_MESSAGE = 64 * 1024 * 1024
HELLO_TIMEOUT_S = 10


def origin_allowed(origin: str | None, settings) -> bool:
    if not origin:
        return False
    if origin in settings.extra_allowed_origins:
        return True
    if not origin.startswith("chrome-extension://"):
        return False
    return not settings.extension_id or origin == f"chrome-extension://{settings.extension_id}"


def _note_takeover(rt, previous: ExtensionSession, new: ExtensionSession) -> None:
    """One extension is served at a time. When a DIFFERENT copy (another Chrome window/profile) takes over repeatedly, say so plainly in the log instead of leaving a silent connect/disconnect loop."""
    a, b = str(previous.hello.get("instance") or "?")[:16], str(new.hello.get("instance") or "?")[:16]
    if a == b:
        return
    now = time.monotonic()
    hist = [t for t in getattr(rt, "_takeovers", []) if now - t < 20] + [now]
    rt._takeovers = hist
    if len(hist) >= 3 and now - getattr(rt, "_takeover_warned", 0) > 60:
        rt._takeover_warned = now
        log.warning("Two Chrome windows/profiles with the OCTG extension (copies %s and %s) are taking the connection from each other. Close one, or disable the extension in one profile.", a, b)


async def ext_ws(request: web.Request) -> web.StreamResponse:
    rt = request.app[RT]
    if not origin_allowed(request.headers.get("Origin"), rt.settings):
        log.warning("Rejected WebSocket from origin %r", request.headers.get("Origin"))
        return web.Response(status=403, text="origin not allowed")

    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=MAX_WS_MESSAGE)
    await ws.prepare(request)

    # ---- handshake
    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
    except (asyncio.TimeoutError, TypeError, ValueError):
        await ws.close(code=4400, message=b"hello expected")
        return ws
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await ws.send_json({"type": "hello_error", "code": "BAD_HELLO", "message": "First message must be a hello."})
        await ws.close(code=4400)
        return ws
    if not hmac.compare_digest(str(hello.get("token", "")).encode("utf-8", "replace"), rt.secrets.pairing_token.encode()):
        log.warning("Extension presented a wrong pairing token")
        await ws.send_json({"type": "hello_error", "code": "BAD_TOKEN", "message": "Pairing token is wrong. Run `python -m octg_agent token` and paste it in the side panel."})
        await ws.close(code=4401, message=b"bad token")
        return ws
    if hello.get("protocol") != PROTOCOL_VERSION:
        await ws.send_json({"type": "hello_error", "code": "PROTOCOL_MISMATCH", "message": f"Agent speaks protocol {PROTOCOL_VERSION}."})
        await ws.close(code=4402)
        return ws

    # ---- one extension at a time; a reconnect replaces the stale session
    session = ExtensionSession(ws, hello)
    previous = rt.session
    rt.session = session
    if previous is not None:
        await previous.close("replaced by a newer connection", code=4000)
        _note_takeover(rt, previous, session)
    rt.state.on_connect(hello)
    await ws.send_json({"type": "hello_ack", "agent_version": __version__, "protocol": PROTOCOL_VERSION})
    log.info("Extension connected (version %s, copy %s)", hello.get("version"), str(hello.get("instance") or "?")[:16])
    await rt.dispatcher.push_status()
    if getattr(rt, "on_connect", None):
        await rt.on_connect()

    # ---- message loop
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(m.data)
            except ValueError:
                continue
            await _route(rt, session, msg)
    finally:
        session.fail_all("The extension disconnected.")
        if rt.session is session:
            rt.session = None
            rt.state.on_disconnect()
            log.info("Extension disconnected")
    return ws


async def _route(rt, session: ExtensionSession, msg: dict) -> None:
    kind = msg.get("type") if isinstance(msg, dict) else None
    if kind == "event":
        rt.state.apply_event(msg)
    elif kind == "tabs_snapshot":
        rt.state.replace_tabs(msg.get("tabs", []))
        rt.state.record("tabs_snapshot", {"count": len(msg.get("tabs", []))})
    elif kind == "ping":
        await session.send_json({"type": "pong", "ts": msg.get("ts")})
    elif kind == "pong":
        pass
    elif kind == "ui":  # side-panel commands (start/approve/deny/pause/resume/stop); handled by whatever the app registered
        handler = getattr(rt, "ui_handler", None)
        res = await handler(msg) if handler else {"ok": False, "error": "no task handler is registered"}
        if res is not None:      # None: a slow command that needs the extension's replies (read on THIS loop) answers later, from the background
            await session.send_json({"type": "ui_result", "request_id": msg.get("request_id"), "command": msg.get("command"), **res})
    elif kind is None and isinstance(msg, dict) and "request_id" in msg:
        problems = validate_extension_response(msg)
        if problems:
            log.warning("Malformed reply %s: %s", msg.get("request_id"), problems)
        session.resolve(msg)


def add_bridge_routes(app: web.Application) -> None:
    app.router.add_get("/ext", ext_ws)
