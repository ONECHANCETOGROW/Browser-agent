"""HTTP surface of the collaboration protocol: /api/collab/*.

Same loopback guard as every /api route (Host must be loopback, browser-originated requests are refused), but authenticated with a COLLABORATOR token
(Authorization: Bearer <token> or X-OCTG-Collab-Token), never the agent's own API token. A caller sees only its own tasks, jobs, evidence and artifacts.
Approval decisions are NOT here: the owner makes them in the Knowledge Hub (or the local CLI).
"""
from __future__ import annotations

import base64
import functools
import logging

from aiohttp import web

from ..appkeys import RT
from . import PROTOCOL
from .capabilities import REGISTRY, catalog
from .store import CollabError

log = logging.getLogger("octg.collab.http")
COLLAB = web.AppKey("octg_collab", object)
TEXT_KINDS = ("json", "txt", "md", "html", "xml", "csv")


def _svc(request: web.Request):
    return request.app[COLLAB]


def _fail(e: CollabError) -> web.Response:
    return web.json_response({"ok": False, "protocol": PROTOCOL, "error": e.as_dict()}, status=e.status)


def authed(fn):
    @functools.wraps(fn)
    async def wrapped(request: web.Request):
        svc = _svc(request)
        tok = request.headers.get("X-OCTG-Collab-Token", "")
        auth = request.headers.get("Authorization", "")
        if not tok and auth.startswith("Bearer "):
            tok = auth[7:].strip()
        who = svc.callers.authenticate(tok)
        if who is None:
            svc.store.audit("auth_failed", actor="?", outcome="401", detail=f"{request.method} {request.path}")
            return web.json_response({"ok": False, "protocol": PROTOCOL, "error": {"code": "UNAUTHORIZED", "message": "missing, wrong or disabled collaborator token", "retryable": False}}, status=401)
        request["caller"], request["grants"] = who
        try:
            return await fn(request)
        except CollabError as e:
            return _fail(e)
        except web.HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("collab request failed")
            return _fail(CollabError("INTERNAL_ERROR", f"{type(e).__name__}", status=500))
    return wrapped


async def _json(request: web.Request) -> dict:
    try:
        d = await request.json()
    except ValueError:
        raise CollabError("BAD_REQUEST", "the body must be JSON") from None
    if not isinstance(d, dict):
        raise CollabError("BAD_REQUEST", "the body must be a JSON object")
    return d


def _wait(request: web.Request) -> float:
    try:
        return max(0.0, float(request.query.get("wait", "0")))
    except ValueError:
        raise CollabError("BAD_REQUEST", "wait is a number of seconds") from None


def _own_task(svc, caller: str, task_id: str) -> dict:
    t = svc.store.get_task(task_id)
    if t["caller"] != caller:
        raise CollabError("NO_SUCH_TASK", f"no collaboration task {task_id}", status=404)
    return t


# ---------------------------------------------------------------------- discovery
@authed
async def whoami(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "protocol": PROTOCOL, "caller": request["caller"], "grants": list(request["grants"]),
                              "browser_connected": request.app[RT].session is not None})


@authed
async def capabilities(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **catalog(request["grants"])})


@authed
async def capability_one(request: web.Request) -> web.Response:
    c = REGISTRY.get(request.match_info["name"])
    if c is None:
        raise CollabError("UNKNOWN_CAPABILITY", "no such capability", status=404)
    return web.json_response({"ok": True, "capability": c.describe() | {"available_to_caller": all(p in request["grants"] for p in c.permissions)}})


# ---------------------------------------------------------------------- parent tasks
@authed
async def task_create(request: web.Request) -> web.Response:
    svc = _svc(request)
    b = await _json(request)
    title = b.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CollabError("BAD_REQUEST", "a task needs a title")
    cid = svc._resolve_client(b["client"])["client_id"] if b.get("client") else None
    t = svc.store.create_task(title.strip(), objective=str(b.get("objective") or ""), client_id=cid, caller=request["caller"])
    return web.json_response({"ok": True, "task": t}, status=201)


@authed
async def task_list(request: web.Request) -> web.Response:
    svc = _svc(request)
    rows = [t for t in svc.store.list_tasks(int(request.query.get("limit", "30")), request.query.get("status")) if t["caller"] == request["caller"]]
    return web.json_response({"ok": True, "tasks": rows})


@authed
async def task_get(request: web.Request) -> web.Response:
    svc = _svc(request)
    t = _own_task(svc, request["caller"], request.match_info["tid"])
    jobs = [{k: j[k] for k in ("request_id", "capability", "status", "client_id", "created_at", "finished_at", "attempts")} for j in svc.store.jobs(task_id=t["task_id"], limit=500)]
    return web.json_response({"ok": True, "task": t, "jobs": jobs[::-1], "artifacts": svc.store.artifacts(task_id=t["task_id"])[::-1],
                              "approvals": [{k: a[k] for k in ("approval_id", "action_kind", "title", "status", "requested_at", "decided_at")} for a in svc.store.approvals(task_id=t["task_id"])]})


@authed
async def task_finish(request: web.Request) -> web.Response:
    svc = _svc(request)
    t = _own_task(svc, request["caller"], request.match_info["tid"])
    b = await _json(request)
    open_jobs = [j["request_id"] for j in svc.store.jobs(task_id=t["task_id"], limit=500) if j["status"] in ("queued", "running", "awaiting_approval")]
    if b.get("status") == "completed" and open_jobs:
        raise CollabError("JOBS_STILL_OPEN", "this task still has unfinished requests; wait for them or cancel them first", {"open": open_jobs}, status=409)
    t = svc.store.finish_task(t["task_id"], str(b.get("status") or ""), outcome=str(b.get("outcome") or ""), summary=str(b.get("summary") or ""), actor=request["caller"])
    return web.json_response({"ok": True, "task": t})


# ---------------------------------------------------------------------- requests (jobs)
@authed
async def submit(request: web.Request) -> web.Response:
    svc = _svc(request)
    b = await _json(request)
    resp, created = svc.submit(request["caller"], request["grants"], b)
    w = _wait(request)
    if w and resp["status"] in ("queued", "running"):
        resp = await svc.wait(request["caller"], resp["request_id"], w)
    return web.json_response({"ok": True, "created": created, "response": resp}, status=202 if created else 200)


@authed
async def request_get(request: web.Request) -> web.Response:
    svc = _svc(request)
    resp = await svc.wait(request["caller"], request.match_info["rid"], _wait(request))
    return web.json_response({"ok": True, "response": resp})


@authed
async def request_cancel(request: web.Request) -> web.Response:
    svc = _svc(request)
    return web.json_response({"ok": True, "response": svc.cancel(request["caller"], request.match_info["rid"])})


@authed
async def request_list(request: web.Request) -> web.Response:
    svc = _svc(request)
    rows = [svc.response(j) for j in svc.store.jobs(task_id=request.query.get("task_id"), status=request.query.get("status"), limit=int(request.query.get("limit", "50")))
            if j["caller"] == request["caller"]]
    for r in rows:
        r["result"] = None if r["result"] is None else "(omitted in lists: GET the request)"
    return web.json_response({"ok": True, "requests": rows})


# ---------------------------------------------------------------------- evidence / artifacts / approvals (read)
@authed
async def evidence_get(request: web.Request) -> web.Response:
    svc = _svc(request)
    ev = svc.store.get_evidence(request.match_info["eid"])
    j = svc.store.get_job(ev["request_id"]) if ev["request_id"] and svc.store.job_exists(ev["request_id"]) else None
    if j is None or j["caller"] != request["caller"]:
        raise CollabError("NO_SUCH_EVIDENCE", "no such evidence", status=404)
    ev, raw = svc.store.read_evidence(ev["evidence_id"], int(request.query.get("max_bytes", "1000000")))
    ext = ev["path"].rsplit(".", 1)[-1].lower()
    body = {"text": raw.decode("utf-8", "replace")} if ext in TEXT_KINDS else {"base64": base64.b64encode(raw).decode("ascii")}
    meta = {k: ev[k] for k in ("evidence_id", "request_id", "task_id", "client_id", "kind", "source_url", "collected_at", "sha256", "bytes", "summary")}
    return web.json_response({"ok": True, "evidence": meta, "content": body, "format": ext})


@authed
async def artifact_get(request: web.Request) -> web.Response:
    svc = _svc(request)
    a = svc.store.get_artifact(request.match_info["aid"])
    if a["task_id"]:
        _own_task(svc, request["caller"], a["task_id"])
    elif a["created_by"] not in (request["caller"], f"agent:{request['caller']}"):   # capabilities record "agent:<caller>": a caller reads what its own requests produced, nobody else's
        raise CollabError("NO_SUCH_ARTIFACT", "no such artifact", status=404)
    return web.json_response({"ok": True, "artifact": a})


@authed
async def approvals_list(request: web.Request) -> web.Response:
    svc = _svc(request)
    rows = [a for a in svc.store.approvals(status=request.query.get("status"), task_id=request.query.get("task_id")) if a["requested_by"] == request["caller"]]
    return web.json_response({"ok": True, "approvals": rows})


@authed
async def audit(request: web.Request) -> web.Response:
    svc = _svc(request)
    tid = request.query.get("task_id")
    if tid:
        _own_task(svc, request["caller"], tid)
    rid = request.query.get("request_id")
    if rid:
        svc._owned(request["caller"], rid)
    if not tid and not rid:
        raise CollabError("BAD_REQUEST", "audit needs task_id or request_id")
    return web.json_response({"ok": True, "audit": svc.store.audit_tail(int(request.query.get("n", "200")), task_id=tid, request_id=rid)})


def add_collab_routes(app: web.Application, svc) -> None:
    app[COLLAB] = svc
    p = "/api/collab"
    r = app.router
    r.add_get(p + "/whoami", whoami)
    r.add_get(p + "/capabilities", capabilities)
    r.add_get(p + "/capabilities/{name}", capability_one)
    r.add_post(p + "/tasks", task_create)
    r.add_get(p + "/tasks", task_list)
    r.add_get(p + "/tasks/{tid}", task_get)
    r.add_post(p + "/tasks/{tid}/finish", task_finish)
    r.add_post(p + "/requests", submit)
    r.add_get(p + "/requests", request_list)
    r.add_get(p + "/requests/{rid}", request_get)
    r.add_post(p + "/requests/{rid}/cancel", request_cancel)
    r.add_get(p + "/evidence/{eid}", evidence_get)
    r.add_get(p + "/artifacts/{aid}", artifact_get)
    r.add_get(p + "/approvals", approvals_list)
    r.add_get(p + "/audit", audit)

    async def _start(app_):
        await svc.start()

    async def _stop(app_):
        await svc.stop()
    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
