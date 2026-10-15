"""Local HTTP API. Loopback only; every /api request needs the API token and must NOT come from a web page.

Why: any web page can make a browser send requests to http://127.0.0.1. We reject requests that carry an Origin header (browsers always
send one on cross-site fetches), reject unexpected Host headers (DNS rebinding) and require a secret header a page cannot know.
"""
from __future__ import annotations

import hmac

from aiohttp import web
from ..appkeys import RT

from ..engine.registry import tool_schema

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_only(h: str) -> str:
    return h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"


@web.middleware
async def guard_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        if _host_only(request.headers.get("Host", "")) not in ALLOWED_HOSTS:
            return web.json_response({"error": "bad host"}, status=403)
        if "Origin" in request.headers:
            return web.json_response({"error": "browser-originated requests are not allowed"}, status=403)
        if request.path.startswith("/api/collab/"):   # collaborator routes authenticate their own (restricted) tokens; the agent API token is not accepted there
            return await handler(request)
        token = request.headers.get("X-OCTG-Token", "")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = token or auth[7:]
        if not hmac.compare_digest(token.encode("utf-8", "replace"), request.app[RT].secrets.api_token.encode()):
            return web.json_response({"error": "missing or wrong API token"}, status=401)
    return await handler(request)


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})  # intentionally reveals nothing else


async def state(request: web.Request) -> web.Response:
    return web.json_response(request.app[RT].state.summary())


async def tools(request: web.Request) -> web.Response:
    return web.json_response(tool_schema())


async def events(request: web.Request) -> web.Response:
    rt = request.app[RT]
    since = int(request.query.get("since", "0"))
    limit = int(request.query.get("limit", "100"))
    return web.json_response({"events": rt.state.events_since(since, limit)})


async def logs(request: web.Request) -> web.Response:
    return web.json_response({"entries": request.app[RT].actionlog.tail(int(request.query.get("tail", "50")))})


async def action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("action"), str):
        return web.json_response({"error": "body must be an object with a string 'action'"}, status=400)
    params = body.get("parameters") or {}
    if not isinstance(params, dict):
        return web.json_response({"error": "'parameters' must be an object"}, status=400)
    resp = await request.app[RT].dispatcher.execute(
        body["action"], params, request_id=body.get("request_id"), timeout_s=body.get("timeout_s"), source="api")
    return web.json_response(resp)


def add_api_routes(app: web.Application) -> None:
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/tools", tools)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/logs", logs)
    app.router.add_post("/api/action", action)
