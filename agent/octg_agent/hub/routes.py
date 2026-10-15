"""The Knowledge Hub's HTTP surface: static pages under /hub/ and a JSON API under /hub/api/. Every API call is a thin wrapper over `business.admin.Admin` (and the import pipeline): the security
decisions live in that Python code, never in the page's JavaScript. See `hub/security.py` for who may call it."""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from ..appkeys import RT
from ..business import records as R
from ..business.admin import Admin, AdminError
from ..business.service import BusinessBrain, BusinessConfig
from ..business.spreadsheet import ImportRefused
from ..business.store import StoreError
from ..files.authorization import AuthorizationError
from .security import (ALLOWED_HOSTS, COOKIE, SECURITY_HEADERS, HubPin, HubSessions, LoginCodes, LoginThrottle, host_only, origin_ok)
from .staging import StagedImports

log = logging.getLogger("octg.hub")
HUB = web.AppKey("octg_hub", object)
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 12 * 1024 * 1024
MAX_XLSX = 10 * 1024 * 1024
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
PUBLIC_API = {"/hub/api/session", "/hub/api/login"}
STATUS = {"NOT_FOUND": 404, "NO_SUCH_CLIENT": 404, "NO_SUCH_FACT": 404, "NO_SUCH_IMPORT": 404, "NO_SUCH_PREVIEW": 410, "DUPLICATE": 409, "DUPLICATE_CLIENT": 409, "DUPLICATE_REF": 409,
          "STALE": 409, "BLOCKED": 409, "WRONG_FACT": 409, "CLIENT_ARCHIVED": 409, "SENSITIVE_UNCONFIRMED": 409}


class HubState:
    def __init__(self, rt):
        self.rt = rt
        self.sessions = HubSessions()
        self.throttle = LoginThrottle()
        self.codes = LoginCodes(rt.settings.data_dir)
        self.pin = HubPin(rt.settings.data_dir)
        self.staged = StagedImports()
        self._admin: Admin | None = None

    def admin(self) -> Admin:
        if self._admin is None:
            rt = self.rt
            brain = rt.business or BusinessBrain(rt.settings.data_dir, rt.folders, BusinessConfig(enabled=False))
            self._admin = Admin(brain, rt.settings)
        return self._admin


# ====================================================================== plumbing
def ok(data: dict | None = None, **extra) -> web.Response:
    return web.json_response({"ok": True, **(data or {}), **extra})


def fail(code: str, message: str, status: int | None = None, **extra) -> web.Response:
    return web.json_response({"ok": False, "code": code, "error": message, **extra}, status=status or STATUS.get(code, 400))


async def call(fn, *a, **k):
    """Run blocking work (SQLite, parsing a workbook) off the event loop so the agent's other duties are never stalled."""
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *a, **k))


async def body(request: web.Request) -> dict:
    try:
        d = await request.json()
    except ValueError:
        raise AdminError("BAD_REQUEST", "the request body must be JSON") from None
    if not isinstance(d, dict):
        raise AdminError("BAD_REQUEST", "the request body must be a JSON object")
    return d


def endpoint(fn):
    """Map the backend's errors to JSON answers. Messages never contain a value (the backend guarantees it); anything unexpected is reported by its TYPE only."""
    @functools.wraps(fn)
    async def wrapped(request: web.Request):
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except (StoreError, AdminError, ImportRefused, R.RecordError) as e:
            extra = {"details": e.details} if getattr(e, "details", None) else {}
            return fail(e.code, e.message, **extra)
        except AuthorizationError as e:
            return fail(getattr(e, "code", "FILE_NOT_AUTHORIZED"), str(e), 403)
        except Exception as e:  # noqa: BLE001
            log.error("Knowledge Hub request failed (%s)", type(e).__name__)
            return fail("HUB_ERROR", "Something went wrong. Nothing was saved unless a message above says so; the agent log has the error type.", 500)
    return wrapped


def hub(request: web.Request) -> HubState:
    return request.app[HUB]


def adm(request: web.Request) -> Admin:
    return hub(request).admin()


# ====================================================================== guard
@web.middleware
async def hub_guard(request: web.Request, handler):
    path = request.path
    try:
        if path.startswith("/hub/api/"):
            host = request.headers.get("Host", "")
            if host_only(host) not in ALLOWED_HOSTS:
                return _with_headers(web.json_response({"ok": False, "code": "BAD_HOST", "error": "bad host"}, status=403))
            sfs = request.headers.get("Sec-Fetch-Site")
            if sfs is not None and sfs != "same-origin":
                return _with_headers(web.json_response({"ok": False, "code": "CROSS_SITE", "error": "this request did not come from the Knowledge Hub page"}, status=403))
            if request.method not in ("GET", "HEAD"):
                origin = request.headers.get("Origin")
                if (origin is not None or sfs is not None) and not origin_ok(origin, host):
                    return _with_headers(web.json_response({"ok": False, "code": "BAD_ORIGIN", "error": "this request did not come from the Knowledge Hub page"}, status=403))
            if path not in PUBLIC_API:
                st = request.app[HUB]
                s = st.sessions.get(request.cookies.get(COOKIE))
                if s is None:
                    return _with_headers(web.json_response({"ok": False, "code": "LOCKED", "error": "sign in to the Knowledge Hub first"}, status=401))
                if not hmac.compare_digest(request.headers.get("X-Hub-CSRF", "").encode(), s.csrf.encode()):
                    return _with_headers(web.json_response({"ok": False, "code": "BAD_CSRF", "error": "missing or wrong session token: reload the page"}, status=403))
                request["hub_session"] = s
        elif path.startswith("/hub"):
            if host_only(request.headers.get("Host", "")) not in ALLOWED_HOSTS:
                return _with_headers(web.Response(status=403, text="bad host"))
        resp = await handler(request)
    except web.HTTPException as e:
        for k, v in SECURITY_HEADERS.items():
            e.headers[k] = v
        raise
    return _with_headers(resp)


def _with_headers(resp: web.StreamResponse) -> web.StreamResponse:
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    return resp


# ====================================================================== pages
async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Content-Type": TYPES[".html"]})


async def redirect_root(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/hub/")


async def static_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    p = STATIC / name
    if os.path.sep in name or "/" in name or name.startswith(".") or p.suffix not in TYPES or not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": TYPES[p.suffix]})


# ====================================================================== session
@endpoint
async def session_info(request: web.Request):
    st = hub(request)
    s = st.sessions.get(request.cookies.get(COOKIE))
    return ok({"authenticated": s is not None, "csrf": s.csrf if s else None, "local_only": True, "locked_for": round(st.throttle.check()), "pin_set": st.pin.is_set()})


@endpoint
async def login(request: web.Request):
    st = hub(request)
    wait = st.throttle.check()
    if wait > 0:
        return fail("TOO_MANY_ATTEMPTS", f"too many wrong attempts: wait {int(wait) + 1} seconds", 429)
    d = await body(request)
    token, code = d.get("token"), d.get("code")
    good = False
    if isinstance(code, str) and code:
        good = st.codes.redeem(code)
    elif isinstance(token, str) and token:
        secrets = st.rt.secrets
        if token.isdigit() and st.pin.is_set():   # the owner's PIN (the long access token below stays as the way back in if it is forgotten)
            good = await call(st.pin.verify, token)
        if not good:
            good = any(hmac.compare_digest(token.encode("utf-8", "replace"), t.encode()) for t in (secrets.api_token, secrets.pairing_token))
    if not good:
        st.throttle.failed()
        return fail("BAD_LOGIN", "that is not the right PIN or access code", 401)
    st.throttle.ok()
    s = st.sessions.create()
    await call(lambda: st.admin().store.audit("hub_login", actor="hub", outcome="signed_in"))
    resp = ok({"authenticated": True, "csrf": s.csrf})
    resp.set_cookie(COOKIE, s.sid, httponly=True, samesite="Strict", path="/hub")
    return resp


@endpoint
async def change_pin(request: web.Request):
    """Set, change or remove the sign-in PIN. Needs a signed-in session AND (when a PIN already exists) the current PIN, so a walk-up on an unlocked screen cannot change it."""
    st = hub(request)
    wait = st.throttle.check()
    if wait > 0:
        return fail("TOO_MANY_ATTEMPTS", f"too many wrong attempts: wait {int(wait) + 1} seconds", 429)
    d = await body(request)
    current, new, remove = d.get("current"), d.get("new"), bool(d.get("remove"))
    if st.pin.is_set():
        if not (isinstance(current, str) and await call(st.pin.verify, current)):
            st.throttle.failed()
            return fail("BAD_PIN", "the current PIN is not right", 401)
        st.throttle.ok()
    if remove:
        await call(st.pin.clear)
        await call(lambda: st.admin().store.audit("hub_pin_removed", actor="hub", outcome="removed"))
        return ok({"pin_set": False})
    if not st.pin.valid(new):
        return fail("BAD_PIN_FORMAT", "a PIN is 4 to 12 digits (numbers only)", 400)
    await call(st.pin.set, new)
    await call(lambda: st.admin().store.audit("hub_pin_changed", actor="hub", outcome="set"))
    return ok({"pin_set": True})


@endpoint
async def logout(request: web.Request):
    st = hub(request)
    st.sessions.drop(request.cookies.get(COOKIE))
    await call(lambda: st.admin().store.audit("hub_logout", actor="hub", outcome="signed_out"))
    resp = ok()
    resp.del_cookie(COOKIE, path="/hub")
    return resp


@endpoint
async def end_all_sessions(request: web.Request):
    st = hub(request)
    n = st.sessions.drop_all()
    resp = ok({"ended": n})
    resp.del_cookie(COOKIE, path="/hub")
    return resp


# ====================================================================== read views
@endpoint
async def scopes(request):
    return ok(await call(adm(request).scopes))


@endpoint
async def dashboard(request):
    return ok(await call(adm(request).dashboard), staged=len(hub(request).staged.list()))


@endpoint
async def me_profile(request):
    return ok(await call(adm(request).me_profile))


@endpoint
async def clients_list(request):
    return ok({"clients": await call(adm(request).clients_table)})


@endpoint
async def client_overview(request):
    return ok(await call(adm(request).client_overview, request.match_info["cid"]))


@endpoint
async def client_section(request):
    return ok(await call(adm(request).client_profile, request.match_info["cid"], request.match_info["section"]))


@endpoint
async def records_list(request):
    return ok({"rows": await call(adm(request).records, request.match_info["cid"], request.match_info["kind"])})


@endpoint
async def client_activity(request):
    return ok(await call(adm(request).activity, request.match_info["cid"], int(request.query.get("n", "80"))))


@endpoint
async def client_sources(request):
    a = adm(request)
    cid = request.match_info["cid"]
    return ok({"sources": await call(a.sources_of_scope, cid), "documents": await call(lambda: a.documents(cid))})


@endpoint
async def seo_view(request):
    return ok(await call(adm(request).seo, request.query.get("client") or None))


@endpoint
async def search(request):
    d = await body(request)
    return ok(await call(adm(request).search, str(d.get("q", ""))))


@endpoint
async def sources(request):
    return ok(await call(adm(request).sources))


@endpoint
async def imports_list(request):
    a = adm(request)
    st = hub(request).staged
    staged = [{"id": s.sid, "kind": s.kind, "name": s.name, "scope": s.scope, "scope_name": (a.scope_name(s.scope) if s.scope else "Several (see the preview)"), "status": s.status, "created": s.created,
               "records": s.preview.to_dict("LOW")["detected"]} for s in st.list()]
    return ok({"staged": staged, "imports": await call(a.imports_list)})


@endpoint
async def import_detail(request):
    return ok(await call(adm(request).import_detail, request.match_info["sid"]))


@endpoint
async def import_verify(request):
    return ok(await call(adm(request).verify_import, request.match_info["sid"]))


@endpoint
async def conflicts(request):
    return ok({"conflicts": await call(adm(request).conflicts), "pending": await call(adm(request).pending)})


@endpoint
async def activity(request):
    q = request.query
    return ok(await call(adm(request).activity, q.get("scope") or None, int(q.get("n", "80")), event=q.get("event") or None))


@endpoint
async def documents(request):
    return ok(await call(adm(request).documents, request.query.get("scope") or None))


@endpoint
async def security_view(request):
    d = await call(adm(request).security)
    d["hub_sessions"] = len(hub(request).sessions)
    d["pin_set"] = hub(request).pin.is_set()
    return ok(d, backups=await call(adm(request).backups))


@endpoint
async def settings_get(request):
    return ok(await call(adm(request).settings_view))


# ====================================================================== changes
@endpoint
async def scope_save(request):
    d = await body(request)
    r = await call(adm(request).save_fields, request.match_info["scope"], d.get("items") or [], confirm_replace=bool(d.get("confirm_replace")), allow_format=bool(d.get("allow_format")))
    return web.json_response(r, status=200 if r.get("ok") else (409 if r.get("needs_confirmation") else 422))


@endpoint
async def scope_remove(request):
    d = await body(request)
    return ok(await call(adm(request).remove_value, request.match_info["scope"], str(d.get("key", ""))))


@endpoint
async def scope_resolve(request):
    d = await body(request)
    return ok(await call(adm(request).resolve, request.match_info["scope"], str(d.get("key", "")), str(d.get("fact_id", ""))))


@endpoint
async def fact_action(request):
    a, fid, act = adm(request), request.match_info["fid"], request.match_info["act"]
    fn = {"confirm": a.confirm, "reject": a.reject, "reveal": a.reveal}.get(act)
    if fn is None:
        raise web.HTTPNotFound()
    return ok(await call(fn, fid))


@endpoint
async def client_create(request):
    d = await body(request)
    r = await call(adm(request).create_client, d, allow_format=bool(d.get("allow_format")))
    return web.json_response(r, status=200 if r.get("ok") else (409 if r.get("needs_confirmation") else 422))


@endpoint
async def client_update(request):
    d = await body(request)
    return ok(await call(adm(request).update_client, request.match_info["cid"], name=d.get("name"), ref=d.get("ref")))


@endpoint
async def client_archive(request):
    return ok(await call(adm(request).set_archived, request.match_info["cid"], request.match_info["act"] == "archive"))


@endpoint
async def record_save(request):
    d = await body(request)
    rec = d.get("rec")
    if not isinstance(rec, dict):
        raise AdminError("BAD_REQUEST", "send the row as 'rec'")
    return ok(await call(adm(request).save_record, request.match_info["cid"], request.match_info["kind"], rec, d.get("rec_id") if isinstance(d.get("rec_id"), str) else None))


@endpoint
async def record_retire(request):
    m = request.match_info
    return ok(await call(adm(request).retire_record, m["cid"], m["kind"], m["rec_id"]))


@endpoint
async def documents_action(request):
    a, act = adm(request), request.match_info["act"]
    if act == "scan":
        return ok(await call(a.documents_scan))
    if act == "index":
        d = await body(request)
        return ok(await call(a.documents_index, d.get("doc_id") if isinstance(d.get("doc_id"), str) else None))
    raise web.HTTPNotFound()


@endpoint
async def document_one(request):
    a, did, act = adm(request), request.match_info["did"], request.match_info["act"]
    fn = {"approve": a.document_approve, "exclude": a.document_exclude}.get(act)
    if fn is None:
        raise web.HTTPNotFound()
    return ok(await call(fn, did))


@endpoint
async def settings_save(request):
    return ok(await call(adm(request).save_settings, await body(request)))


@endpoint
async def backup(request):
    return ok(await call(adm(request).backup))


@endpoint
async def export(request):
    name, data = await call(adm(request).export, request.match_info["kind"], request.query.get("scope") or None)
    return web.Response(body=data, headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Content-Disposition": f'attachment; filename="{name}"',
                                            "X-Export-Name": name})


# ====================================================================== imports (staged, reviewed, then confirmed)
def _stage_view(st, a: Admin, reveal: str = "MEDIUM") -> dict:
    pv = st.preview
    return {"id": st.sid, "kind": st.kind, "name": st.name, "scope": st.scope, "scope_name": a.scope_name(st.scope) if st.scope else "", "status": st.status, "created": st.created,
            "preview": pv.to_dict(reveal), "items": pv.items(reveal), "info": pv.parsed.info, "blocking": pv.blocking(), "blocking_if_problems_left_out": pv.blocking(accept_errors=True)}


@endpoint
async def stage_spreadsheet(request):
    a, st = adm(request), hub(request)
    if request.content_length is None or request.content_length > MAX_XLSX:
        raise ImportRefused("TOO_LARGE", "choose a workbook of at most 10 MB")
    name = unquote(request.headers.get("X-File-Name", "workbook.xlsx"))[-80:]
    if not name.lower().endswith(".xlsx"):
        raise ImportRefused("UNSUPPORTED", "choose an .xlsx workbook (CSV files: import them with the terminal command)")
    data = await request.read()
    pv = await call(a.importer.analyze, data, name=name)
    s = st.staged.add("spreadsheet", name, "", pv)
    return ok(await call(_stage_view, s, a))


@endpoint
async def markdown_files(request):
    a = adm(request)
    found = await call(lambda: a.brain.folders.find("*.md", max_results=100, max_depth=5) + a.brain.folders.find("*.markdown", max_results=50, max_depth=5))
    return ok({"files": [{"path": f["path"], "name": Path(f["path"]).name, "size": f["size"], "modified": f["modified"]} for f in found if f["size"] <= 1024 * 1024],
               "authorized": len(a.brain.folders.roots())})


@endpoint
async def stage_markdown(request):
    a, st = adm(request), hub(request)
    d = await body(request)
    scope = str(d.get("scope", ""))
    if not scope:
        raise AdminError("SCOPE_REQUIRED", "choose whose knowledge this is: your own business or one client")
    if scope not in ("me", "new"):
        a._scope(scope, writing=True)
    client_name = str(d.get("client_name") or "")[:120]
    if isinstance(d.get("path"), str) and d["path"]:   # a file inside an AUTHORIZED folder: the same authorization the browser agent's file actions use
        p = a.brain.folders.resolve_file(d["path"])
        if p.suffix.lower() not in (".md", ".markdown"):
            raise ImportRefused("UNSUPPORTED", "choose a .md file")
        if p.stat().st_size > 1024 * 1024:
            raise ImportRefused("TOO_LARGE", "the Markdown file is larger than 1 MB")
        text, name = p.read_text(encoding="utf-8", errors="replace"), p.name
    else:
        text, name = d.get("text"), str(d.get("name") or "knowledge.md")[-80:]
        if not isinstance(text, str) or not text.strip():
            raise AdminError("EMPTY", "choose a Markdown file")
        if not name.lower().endswith((".md", ".markdown")):
            raise ImportRefused("UNSUPPORTED", "choose a .md file")
    pv = await call(a.importer.analyze_markdown, text, name=name, scope=scope, client_name=client_name)
    s = st.staged.add("markdown", name, scope, pv)
    return ok(await call(_stage_view, s, a))


@endpoint
async def markdown_template(request):
    """A blank Markdown profile ("client" or "me") to fill in and import."""
    from ..business.markdown_templates import template
    try:
        fname, text = template(request.match_info["kind"])
    except KeyError:
        raise web.HTTPNotFound() from None
    return web.Response(body=text.encode("utf-8"), headers={"Content-Type": "text/markdown; charset=utf-8", "Content-Disposition": f'attachment; filename="{fname}"', "X-Export-Name": fname})


@endpoint
async def staged_get(request):
    a = adm(request)
    return ok(await call(_stage_view, hub(request).staged.get(request.match_info["sid"]), a))


@endpoint
async def staged_act(request):
    a, h = adm(request), hub(request)
    sid, act = request.match_info["sid"], request.match_info["act"]
    s = h.staged.get(sid)
    pv = s.preview
    d = await body(request) if request.can_read_body else {}
    if act == "decide":
        await call(pv.decide, str(d.get("id", "")), str(d.get("choice", "")))
    elif act == "map":
        await call(pv.map_column, str(d.get("sheet", "")), str(d.get("header", "")), str(d.get("column", "")))
    elif act == "skip":
        await call(pv.skip_column, str(d.get("sheet", "")), str(d.get("header", "")))
    elif act == "exclude":
        await call(pv.exclude, str(d.get("uid", "")))
    elif act == "include":
        await call(pv.include, str(d.get("uid", "")))
    elif act == "keep-all":
        await call(pv.keep_all_existing)
    elif act == "reject":
        sess = await call(a.importer.record_outcome, pv, "rejected", actor="hub", note="rejected in the Knowledge Hub")
        h.staged.remove(sid)
        return ok({"rejected": True, "import_id": sess})
    elif act == "commit":
        verified = bool(d.get("assume_verified"))
        try:
            res = await call(a.importer.commit, pv, assume_verified=verified, confirm_sensitive=bool(d.get("confirm_sensitive")), accept_errors=bool(d.get("accept_errors")), actor="hub")
        except ImportRefused as e:
            if e.code in ("BLOCKED", "SENSITIVE_UNCONFIRMED"):
                return fail(e.code, e.message, 409, blocking=pv.blocking(bool(d.get("accept_errors"))), high_writes=pv.diff.high_writes())
            raise
        h.staged.remove(sid)
        return ok({"committed": True, "import_id": res["session_id"], "counts": res["counts"]})
    else:
        raise web.HTTPNotFound()
    return ok(await call(_stage_view, s, a))


# ====================================================================== collaboration: the owner's view of AI-to-agent work and the ONLY place (with the TTY CLI) where approvals are decided
def _collab(request: web.Request):
    svc = getattr(hub(request).rt, "collab", None)
    if svc is None:
        raise AdminError("COLLAB_OFF", "the collaboration service is not running")
    return svc


@endpoint
async def collab_view(request: web.Request) -> web.Response:
    svc = _collab(request)
    def build():
        names = {c["client_id"]: c["name"] for c in adm(request).store.list_clients(include_archived=True)}
        tasks = []
        for t in svc.store.list_tasks(40):
            jobs = svc.store.jobs(task_id=t["task_id"], limit=500)
            tasks.append({**t, "client_name": names.get(t["client_id"] or "", ""), "jobs": len(jobs), "jobs_failed": sum(j["status"] in ("failed", "timed_out") for j in jobs),
                          "artifacts": svc.store.artifacts(task_id=t["task_id"], limit=200)})
        aps = [{**a, "client_name": names.get(a["client_id"] or "", "")} for a in svc.store.approvals(limit=200)]
        recent = [{k: j[k] for k in ("request_id", "task_id", "capability", "client_id", "status", "created_at", "finished_at", "caller")} for j in svc.store.jobs(limit=60)]
        return {"tasks": tasks, "approvals": aps, "requests": recent, "callers": svc.callers.list()}
    return ok(await call(build))


async def pin_gate(st: HubState, d: dict, what: str) -> web.Response | None:
    """Actions that authorise something (approving a deploy, giving a client access to a data source) need the owner's PIN typed each time: a signed-in Hub session is NOT enough, so a page click, or
    anything that can operate this page, cannot do them on its own. Returns an error response, or None when the PIN was right."""
    wait = st.throttle.check()
    if wait > 0:
        return fail("TOO_MANY_ATTEMPTS", f"too many wrong attempts: wait {int(wait) + 1} seconds", 429)
    if not st.pin.is_set():
        return fail("NO_PIN_SET", f"set a sign-in PIN first (Security page): {what} needs it", 409)
    if not (isinstance(d.get("pin"), str) and await call(st.pin.verify, d["pin"])):
        st.throttle.failed()
        return fail("BAD_PIN", f"type your PIN to {what}", 401)
    st.throttle.ok()
    return None


@endpoint
async def approval_decide(request: web.Request) -> web.Response:
    svc = _collab(request)
    st = hub(request)
    d = await body(request)
    act = request.match_info["act"]
    from ..collab.store import CollabError
    if act == "approve":
        denied = await pin_gate(st, d, "approve")     # denying is always safe and needs no PIN
        if denied is not None:
            return denied
    try:
        ap = svc.decide(request.match_info["aid"], act == "approve", actor="owner:hub", note=str(d.get("note", ""))[:1000])
    except CollabError as e:
        return fail(e.code, e.message, e.status)
    return ok({"approval": ap})


# ---- improvement loops (the owner's view and controls; plans and reports come only from the collaborator, approvals still need the PIN above)
@endpoint
async def loops_view(request: web.Request) -> web.Response:
    svc = _collab(request)

    def build():
        names = {c["client_id"]: c["name"] for c in adm(request).store.list_clients(include_archived=True)}
        rows = []
        for lp in svc.loops.db.list(limit=100):
            d = svc.loops.describe(lp["loop_id"])
            rows.append({**d, "client_name": names.get(d["client_id"], "")})
        return {"loops": rows}
    return ok(await call(build))


@endpoint
async def loop_detail(request: web.Request) -> web.Response:
    from ..collab.store import CollabError
    svc = _collab(request)
    try:
        d = await call(lambda: svc.loops.describe(request.match_info["lid"], full=True))
    except CollabError as e:
        return fail(e.code, e.message, e.status)
    d["client_name"] = await call(lambda: next((c["name"] for c in adm(request).store.list_clients(include_archived=True) if c["client_id"] == d["client_id"]), ""))
    return ok({"loop": d})


@endpoint
async def loop_act(request: web.Request) -> web.Response:
    from ..collab.store import CollabError
    svc = _collab(request)
    d = await body(request)
    try:
        res = await svc.loops.control(request.match_info["lid"], request.match_info["act"].replace("-", "_"), actor="owner:hub", reason=str(d.get("reason", ""))[:500])
    except CollabError as e:
        return fail(e.code, e.message, e.status)
    return ok({"loop": res})


# ---- Ask: the owner types a plain sentence ("search for X and tell me what you find"), the agent runs it (read-only) and answers in plain language.
# The logic lives in collab/asks.py so the Chrome side panel runs exactly the same code. (Submitting must happen on the event-loop thread, so these do not use call().)
from ..collab.asks import ASK_CALLER, ASK_CAP, ASK_GRANTS, AskError, AskService  # noqa: E402,F401 - ASK_* are re-exported for the tests


@endpoint
async def ask_send(request: web.Request) -> web.Response:
    d = await body(request)
    try:
        res = AskService(_collab(request)).send(text=d.get("text"), client_id=d.get("client_id"), queries=d.get("queries") if isinstance(d.get("queries"), list) else None, location=d.get("location") or "")
    except AskError as e:
        return fail(e.code, e.message, e.status)
    return ok(res)


@endpoint
async def asks_list(request: web.Request) -> web.Response:
    return ok({"asks": await call(AskService(_collab(request)).list)})


@endpoint
async def ask_cancel(request: web.Request) -> web.Response:
    try:
        await call(AskService(_collab(request)).cancel, request.match_info["aid"])
    except AskError as e:
        return fail(e.code, e.message, e.status)
    return ok({"cancelling": True})


# ---- Inbox: what the AI asked the owner to do ("go here, do this, tell me what you see") and the owner's answers
@endpoint
async def inbox_view(request: web.Request) -> web.Response:
    svc = _collab(request)

    def build():
        names = _client_names(request)
        rows = [{**{k: v for k, v in r.items() if k != "attachments"}, "attachments": [{k: x.get(k) for k in ("kind", "url", "title", "chars")} for x in r["attachments"]],
                 "client_name": names.get(r["client_id"] or "", "")} for r in svc.inbox.list(limit=60)]
        return {"requests": rows, "open": svc.inbox.open_count()}
    return ok(await call(build))


@endpoint
async def inbox_reply(request: web.Request) -> web.Response:
    from ..collab.store import CollabError
    svc = _collab(request)
    d = await body(request)
    try:
        r = await call(lambda: svc.inbox.answer(request.match_info["oid"], text=str(d.get("text") or ""), choice=str(d.get("choice") or ""), via="hub"))
    except CollabError as e:
        return fail(e.code, e.message, e.status)
    svc.notify_inbox()
    return ok({"request": {k: v for k, v in r.items() if k != "attachments"}})


@endpoint
async def inbox_dismiss(request: web.Request) -> web.Response:
    from ..collab.store import CollabError
    svc = _collab(request)
    try:
        await call(lambda: svc.inbox.dismiss(request.match_info["oid"], via="hub"))
    except CollabError as e:
        return fail(e.code, e.message, e.status)
    svc.notify_inbox()
    return ok({"dismissed": True})


# ---- data sources (GSC, Bing Webmaster): status, live verification, and the OWNER's mapping of properties to clients
def _client_names(request) -> dict:
    return {c["client_id"]: c["name"] for c in adm(request).store.list_clients(include_archived=True)}


def _client_domains(request, cid: str) -> list[str]:
    from ..business.campaigns import Campaigns
    from ..collab.caps.core import client_context, flat
    a = adm(request)
    site = flat(client_context(a, cid)).get("website")
    return list(dict.fromkeys(([site] if site else []) + [c["website"] for c in Campaigns(a.store).list(cid) if c.get("website")]))


def _connector_error(e) -> web.Response:
    return fail(e.code, e.message, 409)


@endpoint
async def connectors_view(request: web.Request) -> web.Response:
    svc = _collab(request)

    def build():
        names = _client_names(request)
        return {"sources": svc.connectors.status(), "clients": [{"id": k, "name": v} for k, v in names.items()],
                "mappings": [{**m, "client_name": names.get(m["client_id"], "")} for m in svc.connectors.db.mappings()]}
    return ok(await call(build))


@endpoint
async def connector_verify(request: web.Request) -> web.Response:
    from ..connectors import ConnectorError
    svc = _collab(request)
    try:
        return ok({"result": await svc.connectors.verify(request.match_info["source"])})
    except ConnectorError as e:
        return _connector_error(e)


@endpoint
async def connector_properties(request: web.Request) -> web.Response:
    """The OWNER's view of what a credential can see, with a hint for which client each property looks like (by domain). Never shown to the collaborator."""
    from ..connectors import ConnectorError, domain_of
    svc = _collab(request)
    src = request.match_info["source"]
    cid = request.query.get("client") or ""
    try:
        rows = await svc.connectors.sites(src, fresh=True)
    except ConnectorError as e:
        return _connector_error(e)
    names = _client_names(request)
    doms = {domain_of(d) for d in (await call(_client_domains, request, cid) if cid and cid in names else [])}
    out = []
    for r in rows:
        owner = svc.connectors.db.owner_of(src, r["property"])
        out.append({**r, "usable": svc.connectors._usable(src, r["permission"]), "domain": domain_of(r["property"]), "matches_client": domain_of(r["property"]) in doms,
                    "mapped_to": names.get(owner, "") if owner else ""})
    return ok({"properties": out})


@endpoint
async def connector_map(request: web.Request) -> web.Response:
    from ..connectors import SOURCES, ConnectorError
    svc, st = _collab(request), hub(request)
    d = await body(request)
    denied = await pin_gate(st, d, "map a data source to a client")
    if denied is not None:
        return denied
    cid, src, prop = str(d.get("client_id", "")), str(d.get("source", "")), str(d.get("property", ""))
    if src not in SOURCES or cid not in _client_names(request) or not prop:
        return fail("BAD_REQUEST", "choose a source, a client and a property")
    try:
        m = await svc.connectors.map(cid, src, prop, client_domains=await call(_client_domains, request, cid), mapped_by="owner:hub", allow_mismatch=bool(d.get("allow_mismatch")))
    except ConnectorError as e:
        return _connector_error(e)
    await call(lambda: svc.store.audit("connector_mapped", actor="owner:hub", client_id=cid, outcome="ok", detail=f"{src} -> {prop}"))
    return ok({"mapping": m})


@endpoint
async def connector_unmap(request: web.Request) -> web.Response:
    from ..connectors import ConnectorError
    svc, st = _collab(request), hub(request)
    d = await body(request)
    denied = await pin_gate(st, d, "remove a data-source mapping")
    if denied is not None:
        return denied
    try:
        m = svc.connectors.db.get(str(d.get("map_id", "")))
    except ConnectorError as e:
        return _connector_error(e)
    svc.connectors.unmap(m["map_id"])
    await call(lambda: svc.store.audit("connector_unmapped", actor="owner:hub", client_id=m["client_id"], outcome="ok", detail=f"{m['source']} -> {m['property']}"))
    return ok({"unmapped": m["map_id"]})


# ====================================================================== wiring
def add_hub_routes(app: web.Application) -> None:
    sub = web.Application(middlewares=[hub_guard], client_max_size=MAX_UPLOAD)
    sub[RT] = app[RT]
    sub[HUB] = HubState(app[RT])
    r = sub.router
    r.add_get("", redirect_root)
    r.add_get("/", index)
    r.add_get("/static/{name}", static_file)
    api = "/api"
    r.add_get(api + "/session", session_info)
    r.add_post(api + "/login", login)
    r.add_post(api + "/pin", change_pin)
    r.add_post(api + "/logout", logout)
    r.add_post(api + "/sessions/end-all", end_all_sessions)
    r.add_get(api + "/scopes", scopes)
    r.add_get(api + "/dashboard", dashboard)
    r.add_get(api + "/me", me_profile)
    r.add_get(api + "/clients", clients_list)
    r.add_post(api + "/clients", client_create)
    r.add_get(api + "/clients/{cid}", client_overview)
    r.add_get(api + "/clients/{cid}/section/{section}", client_section)
    r.add_post(api + "/clients/{cid}/update", client_update)
    r.add_post(api + "/clients/{cid}/{act:archive|restore}", client_archive)
    r.add_get(api + "/clients/{cid}/records/{kind}", records_list)
    r.add_post(api + "/clients/{cid}/records/{kind}", record_save)
    r.add_post(api + "/clients/{cid}/records/{kind}/{rec_id}/retire", record_retire)
    r.add_get(api + "/clients/{cid}/activity", client_activity)
    r.add_get(api + "/clients/{cid}/sources", client_sources)
    r.add_post(api + "/scope/{scope}/save", scope_save)
    r.add_post(api + "/scope/{scope}/remove", scope_remove)
    r.add_post(api + "/scope/{scope}/resolve", scope_resolve)
    r.add_post(api + "/fact/{fid}/{act}", fact_action)
    r.add_get(api + "/seo", seo_view)
    r.add_get(api + "/collab", collab_view)
    r.add_post(api + "/approvals/{aid}/{act:approve|deny}", approval_decide)
    r.add_get(api + "/connectors", connectors_view)
    r.add_post(api + "/connectors/{source}/verify", connector_verify)
    r.add_get(api + "/connectors/{source}/properties", connector_properties)
    r.add_post(api + "/connectors/map", connector_map)
    r.add_post(api + "/connectors/unmap", connector_unmap)
    r.add_get(api + "/loops", loops_view)
    r.add_get(api + "/loops/{lid}", loop_detail)
    r.add_post(api + "/loops/{lid}/{act:pause|resume|stop|check-now}", loop_act)
    r.add_get(api + "/asks", asks_list)
    r.add_post(api + "/asks", ask_send)
    r.add_post(api + "/asks/{aid}/cancel", ask_cancel)
    r.add_get(api + "/inbox", inbox_view)
    r.add_post(api + "/inbox/{oid}/reply", inbox_reply)
    r.add_post(api + "/inbox/{oid}/dismiss", inbox_dismiss)
    r.add_post(api + "/search", search)
    r.add_get(api + "/sources", sources)
    r.add_get(api + "/imports", imports_list)
    r.add_get(api + "/imports/{sid}", import_detail)
    r.add_post(api + "/imports/spreadsheet", stage_spreadsheet)
    r.add_post(api + "/imports/{sid}/verify", import_verify)
    r.add_post(api + "/imports/markdown", stage_markdown)
    r.add_get(api + "/markdown-files", markdown_files)
    r.add_get(api + "/imports/markdown-template/{kind}", markdown_template)
    r.add_get(api + "/staged/{sid}", staged_get)
    r.add_post(api + "/staged/{sid}/{act}", staged_act)
    r.add_get(api + "/conflicts", conflicts)
    r.add_get(api + "/activity", activity)
    r.add_get(api + "/documents", documents)
    r.add_post(api + "/documents/{act:scan|index}", documents_action)
    r.add_post(api + "/documents/{did}/{act:approve|exclude}", document_one)
    r.add_get(api + "/export/{kind}", export)
    r.add_post(api + "/backup", backup)
    r.add_get(api + "/security", security_view)
    r.add_get(api + "/settings", settings_get)
    r.add_post(api + "/settings", settings_save)
    app.add_subapp("/hub", sub)
