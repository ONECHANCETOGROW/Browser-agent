"""HTTP routes for the AI layer (same loopback + token guard as the rest of /api). There is deliberately no route that reads or writes API keys."""
from __future__ import annotations

from aiohttp import web

from ..appkeys import RT
from ..brain.manager import TaskError


def _err(e: TaskError) -> web.Response:
    return web.json_response({"ok": False, "error": {"code": e.code, "message": e.message}})


async def start_task(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("instruction"), str):
        return web.json_response({"error": "body must be an object with a string 'instruction'"}, status=400)
    try:
        t = await request.app[RT].tasks.start(body["instruction"], provider=body.get("provider"), policy=body.get("policy"),
                                              max_steps=body.get("max_steps"), approve_medium=body.get("approve_medium"),
                                              business=body.get("business") if isinstance(body.get("business"), bool) else None,
                                              client=body.get("client") if isinstance(body.get("client"), str) and body.get("client") else None)
    except TaskError as e:
        return _err(e)
    return web.json_response({"ok": True, "task": t.to_summary()})


async def list_tasks(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "tasks": request.app[RT].history.list(int(request.query.get("limit", "20")))})


async def get_task(request: web.Request) -> web.Response:
    d = request.app[RT].tasks.get(request.match_info["task_id"])
    return web.json_response({"ok": True, "task": d}) if d else web.json_response({"ok": False, "error": {"code": "NO_TASK", "message": "no such task"}})


async def task_command(request: web.Request) -> web.Response:
    cmd = request.match_info["cmd"]
    if cmd not in ("approve", "deny", "pause", "resume", "stop", "cancel"):
        return web.json_response({"error": "unknown command"}, status=404)
    try:
        t = getattr(request.app[RT].tasks, cmd)(request.match_info["task_id"])
    except TaskError as e:
        return _err(e)
    return web.json_response({"ok": True, "task": t.to_summary()})


async def select_client(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("client"), str):
        return web.json_response({"error": "body must be an object with a string 'client' (an id or the exact name)"}, status=400)
    try:
        t = request.app[RT].tasks.select_client(request.match_info["task_id"], body["client"])
    except TaskError as e:
        return _err(e)
    return web.json_response({"ok": True, "task": t.to_summary()})


async def providers(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **await request.app[RT].tasks.providers_status()})


async def test_provider(request: web.Request) -> web.Response:
    return web.json_response(await request.app[RT].tasks.test_provider(request.match_info["name"]))


async def detect_local(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "runtimes": await request.app[RT].tasks.detect_local()})


def add_task_routes(app: web.Application) -> None:
    app.router.add_post("/api/tasks", start_task)
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_get("/api/tasks/{task_id}", get_task)
    app.router.add_post("/api/tasks/{task_id}/client", select_client)
    app.router.add_post("/api/tasks/{task_id}/{cmd}", task_command)
    app.router.add_get("/api/providers", providers)
    app.router.add_get("/api/providers/detect-local", detect_local)
    app.router.add_post("/api/providers/{name}/test", test_provider)
