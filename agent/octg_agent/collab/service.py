"""The collaboration service: validates requests, persists them as jobs, runs them, and answers status/result/cancel.

Request -> job lifecycle
    submit ──► rejected                        (bad shape, unknown capability, missing permission, wrong client scope)
           ──► queued ──► running ──► succeeded | failed | timed_out | cancelled
           ──► awaiting_approval ──(owner approves in the Knowledge Hub)──► queued ...      (capabilities with approval="required")
                                  ──(owner denies)──► cancelled

Guarantees
  * idempotency: the same (caller, idempotency_key) or the same request_id never runs twice; a different payload under a used key is refused;
  * persistence: every state change is written to collab.db before it is reported; a restart re-queues read-only work that was interrupted and
    fails (not retries) anything that may have had side effects;
  * bounded: every job has a timeout (capped by the capability), cancellation is honoured while queued or running, and waiting is long-poll with a cap;
  * scope: a client-scoped capability needs an explicit client; a job inside a parent task can only touch that task's client.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from ..sensitive import scrub_text
from . import PROTOCOL
from .auth import Callers
from .capabilities import REGISTRY, Capability, Outcome
from .store import JOB_TERMINAL, CollabError, CollabStore, dumps, loads, new_id, params_hash

log = logging.getLogger("octg.collab")
CREDENTIAL_RE = re.compile(r"(?i)(\b(api[_-]?key|passw(or)?d|passwd|secret|bearer|private[_-]?key)\b\s*[:=])|-----BEGIN [A-Z ]*PRIVATE KEY|\b(sk|rk)-[A-Za-z0-9_-]{20,}|\bAIza[0-9A-Za-z_-]{30,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abpr]-[A-Za-z0-9-]{10,}")
MAX_WAIT_S = 55.0
MAX_ATTEMPTS = 2
FORMATS = ("json", "markdown", "json+markdown")


def _secret_like(v: Any, path: str = "parameters") -> str | None:
    """The path of the first string that looks like a credential, card, PAN/GSTIN or 12-digit ID; None if the payload is clean."""
    if isinstance(v, str):
        return path if (scrub_text(v) != v or CREDENTIAL_RE.search(v)) else None
    if isinstance(v, dict):
        for k, x in v.items():
            hit = _secret_like(x, f"{path}.{k}")
            if hit:
                return hit
    if isinstance(v, list):
        for i, x in enumerate(v):
            hit = _secret_like(x, f"{path}[{i}]")
            if hit:
                return hit
    return None


class JobContext:
    """Handed to a capability handler: the job's identity plus the few services a handler may use. Handlers never see the caller's token or grants."""

    def __init__(self, svc: "CollabService", job: dict, cap: Capability):
        self.svc = svc
        self.rt = svc.rt
        self.job = job
        self.cap = cap
        self.request_id: str = job["request_id"]
        self.task_id: str | None = job["task_id"]
        self.client_id: str | None = job["client_id"]
        self.caller: str = job["caller"]
        self.outcome = Outcome()
        self.evidence_ids: list[str] = []
        self._tab_ids: list[int] = []

    # --- evidence / progress
    def evidence(self, kind: str, content: bytes | str | Any, *, source_url: str = "", summary: str = "", ext: str | None = None) -> str:
        if not isinstance(content, (bytes, str)):
            content, ext = dumps(content), ext or "json"
        ev = self.svc.store.add_evidence(request_id=self.request_id, task_id=self.task_id, client_id=self.client_id, kind=kind, content=content, source_url=source_url,
                                         summary=summary, ext=ext or "txt")
        self.evidence_ids.append(ev["evidence_id"])
        return ev["evidence_id"]

    def source(self, name: str, url: str = "", collected_at: float | None = None, **extra: Any) -> None:
        self.outcome.sources.append({"source": name, "url": url, "collected_at": collected_at or time.time(), **extra})

    def limit(self, text: str) -> None:
        if text not in self.outcome.limitations:
            self.outcome.limitations.append(text)

    def progress(self, note: str) -> None:
        self.svc.store.update_job(self.request_id, progress=scrub_text(note)[:300])

    def check_cancel(self) -> None:
        if self.svc.store.get_job(self.request_id)["cancel_requested"]:
            raise asyncio.CancelledError()

    # --- business data (always through the Knowledge Hub's own admin layer)
    def admin(self):
        return self.svc.admin()

    # --- browser: only via the dispatcher, only in tabs this job opened itself
    async def browser(self, action: str, params: dict | None = None, timeout_s: float | None = None) -> dict:
        allowed = {"get_agent_state", "get_tabs", "get_page_state", "inspect_page", "read_page", "find_text", "screenshot", "wait", "verify", "navigate", "new_tab", "close_tab"}
        if action not in allowed:
            raise CollabError("BROWSER_ACTION_NOT_ALLOWED", f"collaboration capabilities may not use {action}")
        p = dict(params or {})
        if action == "close_tab" and p.get("tab_id") not in self._tab_ids:
            raise CollabError("BROWSER_ACTION_NOT_ALLOWED", "a capability may only close a tab it opened")
        if action not in ("new_tab", "get_agent_state", "get_tabs", "close_tab") and p.get("tab_id") not in self._tab_ids:
            raise CollabError("BROWSER_ACTION_NOT_ALLOWED", "capabilities work only in the tab they opened (pass its tab_id)")
        resp = await self.rt.dispatcher.execute(action, p, timeout_s=timeout_s, source=f"collab:{self.caller}")
        if action == "new_tab" and resp.get("success"):
            tid = ((resp.get("result") or {}).get("tab") or {}).get("tab_id") or (resp.get("result") or {}).get("tab_id")
            if isinstance(tid, int):
                self._tab_ids.append(tid)
        return resp

    async def open_tab(self, url: str, timeout_ms: int = 30000) -> int:
        resp = await self.browser("new_tab", {"url": url, "active": False, "timeout_ms": timeout_ms})
        if not resp.get("success"):
            err = resp.get("error") or {}
            raise CollabError(err.get("code") or "BROWSER_ERROR", err.get("message") or "could not open a tab", retryable=err.get("code") in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED", "ACTION_TIMEOUT"))
        if not self._tab_ids:
            raise CollabError("BROWSER_ERROR", "the extension did not report the new tab's id")
        return self._tab_ids[-1]

    async def close_tabs(self) -> None:
        for t in list(self._tab_ids):
            try:
                await self.rt.dispatcher.execute("close_tab", {"tab_id": t}, source=f"collab:{self.caller}")
            except Exception:  # noqa: BLE001 - closing our own tab is best effort
                pass
            self._tab_ids.remove(t)


class CollabService:
    def __init__(self, rt, *, store: CollabStore | None = None, callers: Callers | None = None, max_parallel: int = 3):
        self.rt = rt
        data_dir = Path(rt.settings.data_dir)
        self.store = store or CollabStore(data_dir / "collab.db")
        self.callers = callers or Callers(data_dir)
        self.callers.ensure()
        self._sem = asyncio.Semaphore(max_parallel)
        self._browser_lock = asyncio.Lock()   # one browser capability at a time (the dispatcher serialises single actions; this keeps a job's sequence together)
        self._running: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._admin = None
        self._started = False
        from .loops import LoopController   # (imported here: loops.py needs the store, not the other way round)
        self.loops = LoopController(self)
        from ..connectors.hub import ConnectorHub, ConnectorStore
        self.connectors = ConnectorHub(data_dir / "connectors", ConnectorStore(self.store))   # credentials folder + owner-set property mappings; tests swap `.http`
        from .ownerio import OwnerInbox
        self.inbox = OwnerInbox(self.store)                                                   # AI -> owner instructions and the owner's answers
        from .eye import EyeSessions
        self.eye = EyeSessions(self)                                                          # the AI's dedicated browsing tabs (see collab/eye.py)

    def notify_inbox(self) -> None:
        """Tell the owner's Chrome extension how many AI requests are waiting (it shows a badge). Safe when no extension is connected."""
        push = getattr(self.rt, "_push_to_extension", None)
        if push is not None:
            try:
                push({"type": "inbox", "open": self.inbox.open_count()})
            except Exception:  # noqa: BLE001 - a notification must never break the request that caused it
                log.exception("inbox notification")

    # ------------------------------------------------------------------ helpers
    def admin(self):
        if self._admin is None:
            from ..business.admin import Admin
            from ..business.service import BusinessBrain, BusinessConfig
            brain = self.rt.business or BusinessBrain(self.rt.settings.data_dir, self.rt.folders, BusinessConfig(enabled=False))
            self._admin = Admin(brain, self.rt.settings)
        return self._admin

    def _event(self, rid: str) -> asyncio.Event:
        return self._events.setdefault(rid, asyncio.Event())

    def _resolve_client(self, text: str) -> dict:
        from ..business.store import StoreError
        store = self.admin().store
        try:
            c = store.find_client(text)
        except StoreError as e:
            t = (text or "").strip().lower()
            near = [{"client_id": c["client_id"], "name": c["name"]} for c in store.list_clients() if t and (t in c["name"].lower() or c["name"].lower() in t)]
            if e.code == "AMBIGUOUS_CLIENT" or len(near) > 1:
                raise CollabError("CLIENT_AMBIGUOUS", f"more than one client could be meant by {text!r}: ask the owner which one", {"candidates": near}) from None
            raise CollabError("CLIENT_NOT_FOUND", f"no client is exactly {text!r}", {"did_you_mean": near}) from None
        if c["archived"]:
            raise CollabError("CLIENT_ARCHIVED", "that client is archived")
        return c

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Recover after a restart: interrupted read-only jobs are re-queued (bounded), interrupted side-effecting jobs fail visibly."""
        if self._started:
            return
        self._started = True
        for j in self.store.jobs_in(("running",)):
            cap = REGISTRY.get(j["capability"])
            if cap and cap.read_only and (j["attempts"] or 0) < MAX_ATTEMPTS:
                self.store.update_job(j["request_id"], status="queued", progress="re-queued after the agent restarted")
                self.store.audit("job_requeued", actor="agent", task_id=j["task_id"] or "", request_id=j["request_id"], client_id=j["client_id"] or "", outcome="queued", detail="restart")
            else:
                self._finish(j["request_id"], "failed", error={"code": "INTERRUPTED", "message": "the agent stopped while this job was running; it may have partly run, so it was not retried automatically",
                                                           "retryable": False})
        for j in self.store.jobs_in(("queued",)):
            self._schedule(j["request_id"])
        self._watcher = asyncio.get_running_loop().create_task(self._watch_approvals())
        self.loops.start(float(os.environ.get("OCTG_LOOP_TICK_S", "20")))   # resumes loops whose re-check date has arrived; holds no browser or model while idle

    async def _watch_approvals(self, every_s: float = 3.0) -> None:
        """Decisions can also be written by the owner's CLI straight into collab.db; pick them up so a waiting job resumes or is cancelled."""
        n = 0
        while True:
            try:
                self.sync_approvals()
                n += 1
                if n % 200 == 1 and self.inbox.expire_old():      # about every 10 minutes: unanswered requests older than two weeks are closed
                    self.notify_inbox()
                if n % 10 == 0:
                    await self.eye.gc()                           # about every 30 s: eye tabs idle for ten minutes are closed
            except Exception:  # noqa: BLE001 - the watcher must never die
                log.exception("approval watcher")
            await asyncio.sleep(every_s)

    def sync_approvals(self) -> int:
        n = 0
        for j in self.store.jobs_in(("awaiting_approval",)):
            if not j["approval_id"]:
                continue
            st = self.store.get_approval(j["approval_id"])["status"]
            if st == "approved":
                self.store.update_job(j["request_id"], status="queued")
                self._schedule(j["request_id"])
                n += 1
            elif st in ("denied", "withdrawn"):
                self._finish(j["request_id"], "cancelled", error={"code": "APPROVAL_DENIED" if st == "denied" else "CANCELLED", "message": f"the approval was {st}", "retryable": False})
                n += 1
        return n

    async def stop(self) -> None:
        await self.loops.stop_ticker()
        w = getattr(self, "_watcher", None)
        if w:
            w.cancel()
        for t in list(self._running.values()):
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    # ------------------------------------------------------------------ submit
    def submit(self, caller: str, grants: tuple[str, ...], req: dict) -> tuple[dict, bool]:
        """Returns (response, created). Raises CollabError for requests that are refused before a job exists."""
        if not isinstance(req, dict):
            raise CollabError("BAD_REQUEST", "the request must be a JSON object")
        allowed = {"request_id", "task_id", "client", "capability", "parameters", "required_permissions", "expected_format", "timeout_s", "idempotency_key", "priority", "created_at", "protocol"}
        extra = sorted(set(req) - allowed)
        if extra:
            raise CollabError("BAD_REQUEST", f"unknown request fields: {extra}")
        if req.get("protocol") not in (None, PROTOCOL):
            raise CollabError("BAD_PROTOCOL", f"this agent speaks {PROTOCOL}")
        name = req.get("capability")
        if not isinstance(name, str) or not name:
            raise CollabError("BAD_REQUEST", "capability (a string) is required")
        rid = req.get("request_id")
        if rid is not None and (not isinstance(rid, str) or not (4 <= len(rid) <= 64) or not rid.replace("_", "").replace("-", "").isalnum()):
            raise CollabError("BAD_REQUEST", "request_id must be 4-64 letters, digits, - or _")
        idem = req.get("idempotency_key")
        if idem is not None and (not isinstance(idem, str) or not (1 <= len(idem) <= 120)):
            raise CollabError("BAD_REQUEST", "idempotency_key must be a string of 1-120 characters")
        params = req.get("parameters") or {}
        if not isinstance(params, dict):
            raise CollabError("BAD_REQUEST", "parameters must be an object")

        # retries: an existing request id / idempotency key returns the existing job, never a second execution
        if rid and self.store.job_exists(rid):
            j = self.store.get_job(rid)
            if j["caller"] != caller:
                raise CollabError("REQUEST_ID_TAKEN", "that request_id belongs to another caller", status=409)
            if j["params_hash"] != params_hash(name, j["client_id"], params) and j["status"] != "rejected":
                raise CollabError("IDEMPOTENCY_CONFLICT", "that request_id was already used with a different capability or parameters", status=409)
            return self.response(j), False
        if idem:
            j = self.store.find_idempotent(caller, idem)
            if j:
                if j["capability"] != name or loads(j["params"], {}) != params:
                    raise CollabError("IDEMPOTENCY_CONFLICT", "that idempotency_key was already used with a different capability or parameters", status=409)
                return self.response(j), False

        cap = REGISTRY.get(name)
        if cap is None:
            raise CollabError("UNKNOWN_CAPABILITY", f"no capability {name!r}", {"known": sorted(REGISTRY)}, status=404)
        missing = [p for p in cap.permissions if p not in grants]
        if missing:
            self.store.audit("request_refused", actor=caller, outcome="PERMISSION_DENIED", detail=f"{name} needs {missing}")
            raise CollabError("PERMISSION_DENIED", f"{name} needs permissions this caller does not have: {missing}", {"missing": missing}, status=403)
        declared = req.get("required_permissions")
        if declared is not None:
            if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
                raise CollabError("BAD_REQUEST", "required_permissions must be a list of strings")
            under = [p for p in cap.permissions if p not in declared]
            if under:
                raise CollabError("PERMISSION_MISMATCH", f"{name} needs {list(cap.permissions)}; the request declared {declared}. Declare every permission the operation uses.", status=403)
            beyond = [p for p in declared if p not in grants]
            if beyond:
                raise CollabError("PERMISSION_DENIED", f"the request declares permissions this caller does not have: {beyond}", status=403)
        leak = _secret_like(params)
        if leak:
            self.store.audit("request_refused", actor=caller, outcome="SECRET_IN_REQUEST", detail=f"{name} parameter {leak}")
            raise CollabError("SECRET_IN_REQUEST", f"parameter {leak} looks like a credential or a protected identifier: secrets never travel through the collaboration channel")
        problems = cap.check(params)
        if problems:
            raise CollabError("INVALID_PARAMS", "invalid parameters: " + "; ".join(problems), {"errors": problems, "input_schema": cap.input_schema()})
        fmt = req.get("expected_format") or "json"
        if fmt not in FORMATS:
            raise CollabError("BAD_REQUEST", f"expected_format must be one of {FORMATS}")
        timeout = req.get("timeout_s", cap.timeout_s)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise CollabError("BAD_REQUEST", "timeout_s must be a positive number")
        timeout = float(min(timeout, cap.max_timeout_s))
        prio = req.get("priority", 5)
        if not isinstance(prio, int) or isinstance(prio, bool) or not 1 <= prio <= 9:
            raise CollabError("BAD_REQUEST", "priority is 1 (most urgent) to 9")

        # scope: explicit client > the parent task's client; never a default client; never a different client than the task's
        task = None
        if req.get("task_id"):
            task = self.store.get_task(str(req["task_id"]))
            if task["status"] != "open":
                raise CollabError("TASK_CLOSED", f"task {task['task_id']} is {task['status']}", status=409)
        client_id = None
        if req.get("client"):
            if not isinstance(req["client"], str):
                raise CollabError("BAD_REQUEST", "client is a client id or exact client name")
            client_id = self._resolve_client(req["client"])["client_id"]
        elif task and task["client_id"]:
            client_id = task["client_id"]
        if task and task["client_id"] and client_id != task["client_id"]:
            self.store.audit("request_refused", actor=caller, task_id=task["task_id"], client_id=client_id or "", outcome="SCOPE_MISMATCH", detail=name)
            raise CollabError("SCOPE_MISMATCH", "this task belongs to a different client; start a separate task for another client", status=403)
        if cap.scope == "client" and not client_id:
            raise CollabError("CLIENT_REQUIRED", f"{name} works on exactly one client: pass client (id or exact name) or use a task that has one")
        if cap.scope == "none":
            client_id = client_id if task and task["client_id"] else None

        now = time.time()
        row = {"request_id": rid or new_id("rq"), "task_id": task["task_id"] if task else None, "caller": caller, "idem_key": idem, "capability": name, "client_id": client_id,
               "params": dumps(params), "params_hash": params_hash(name, client_id, params), "permissions": dumps(list(cap.permissions)), "expected_format": fmt, "priority": prio,
               "timeout_s": timeout, "status": "queued", "created_at": now, "attempts": 0, "retryable": 0, "cancel_requested": 0}
        if cap.approval == "required":
            ap = self.store.request_approval(task_id=row["task_id"], client_id=client_id, action_kind=cap.approval_kind or name, title=f"{name} requested by {caller}",
                                             description=cap.purpose, details={"capability": name, "parameters": params, "request_id": row["request_id"]}, requested_by=caller)
            row.update(status="awaiting_approval", approval_id=ap["approval_id"])
        j = self.store.insert_job(row)
        self.store.audit("job_submitted", actor=caller, task_id=j["task_id"] or "", request_id=j["request_id"], client_id=client_id or "", outcome=j["status"],
                         detail=f"{name} prio={prio} timeout={timeout:g}s client_created_at={req.get('created_at', '')}")
        self.store.touch_task(j["task_id"])
        if j["status"] == "queued":
            self._schedule(j["request_id"])
        return self.response(j), True

    # ------------------------------------------------------------------ approvals (decided by the OWNER only: hub / local CLI call these, never the protocol)
    def decide(self, approval_id: str, approve: bool, *, actor: str, note: str = "") -> dict:
        ap = self.store.transition_approval(approval_id, "approved" if approve else "denied", actor=actor, note=note)
        rid = (ap["details"] or {}).get("request_id")
        if rid and self.store.job_exists(rid):
            j = self.store.get_job(rid)
            if j["status"] == "awaiting_approval":
                if approve:
                    self.store.update_job(rid, status="queued")
                    self._schedule(rid)
                else:
                    self._finish(rid, "cancelled", error={"code": "APPROVAL_DENIED", "message": "the owner denied this operation", "retryable": False})
        return ap

    # ------------------------------------------------------------------ run
    def _schedule(self, rid: str) -> None:
        if rid in self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # no loop yet (CLI / tests): start() picks queued jobs up
        self._running[rid] = loop.create_task(self._run(rid))

    async def _run(self, rid: str) -> None:
        try:
            async with self._sem:
                j = self.store.get_job(rid)
                if j["status"] != "queued":
                    return
                if j["cancel_requested"]:
                    self._finish(rid, "cancelled", error={"code": "CANCELLED", "message": "cancelled before it started", "retryable": False})
                    return
                cap = REGISTRY.get(j["capability"])
                if cap is None:
                    self._finish(rid, "failed", error={"code": "UNKNOWN_CAPABILITY", "message": "the capability is no longer registered", "retryable": False})
                    return
                if cap.needs_browser:
                    async with self._browser_lock:
                        await self._execute(rid, cap)
                else:
                    await self._execute(rid, cap)
        finally:
            self._running.pop(rid, None)

    async def _execute(self, rid: str, cap: Capability) -> None:
        j = self.store.update_job(rid, status="running", started_at=time.time(), attempts=(self.store.get_job(rid)["attempts"] or 0) + 1, progress="")
        self.store.audit("job_started", actor="agent", task_id=j["task_id"] or "", request_id=rid, client_id=j["client_id"] or "", outcome="running", detail=cap.name)
        ctx = JobContext(self, j, cap)
        params = loads(j["params"], {})
        try:
            result = await asyncio.wait_for(cap.handler(ctx, params), timeout=j["timeout_s"])
            if not isinstance(result, dict):
                raise CollabError("BAD_HANDLER_RESULT", "the capability returned a non-object result")
            self._finish(rid, "succeeded", result=result, ctx=ctx)
        except asyncio.TimeoutError:
            self._finish(rid, "timed_out", error={"code": "TIMEOUT", "message": f"no result within {j['timeout_s']:g} s", "retryable": cap.read_only}, ctx=ctx)
        except asyncio.CancelledError:
            self._finish(rid, "cancelled", error={"code": "CANCELLED", "message": "cancelled while running", "retryable": False}, ctx=ctx)
        except CollabError as e:
            self._finish(rid, "failed", error=e.as_dict(), ctx=ctx)
        except Exception as e:  # noqa: BLE001 - a broken handler must still produce a structured, recorded failure
            log.exception("capability %s crashed", cap.name)
            self._finish(rid, "failed", error={"code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {scrub_text(str(e))[:300]}", "retryable": False}, ctx=ctx)
        finally:
            if ctx._tab_ids:
                await ctx.close_tabs()

    def _finish(self, rid: str, status: str, *, result: dict | None = None, error: dict | None = None, ctx: JobContext | None = None) -> None:
        o = ctx.outcome if ctx else Outcome(completeness="none", confidence="n/a")
        if status != "succeeded" and ctx is not None and o.completeness == "complete":
            o.completeness = "none"
        j = self.store.update_job(rid, status=status, finished_at=time.time(), result=dumps(result) if result is not None else None, error=dumps(error) if error else None,
                                  retryable=int(bool((error or {}).get("retryable"))), completeness=o.completeness if status == "succeeded" else ("partial" if ctx and ctx.evidence_ids else "none"),
                                  confidence=o.confidence if status == "succeeded" else "n/a", limitations=dumps(o.limitations), sources=dumps(o.sources))
        self.store.audit("job_" + status, actor="agent", task_id=j["task_id"] or "", request_id=rid, client_id=j["client_id"] or "", outcome=status,
                         detail=(error or {}).get("code", "") + (f" evidence={len(ctx.evidence_ids)}" if ctx else ""))
        self.store.touch_task(j["task_id"])
        self._event(rid).set()

    # ------------------------------------------------------------------ read / wait / cancel
    def _owned(self, caller: str, rid: str) -> dict:
        j = self.store.get_job(rid)
        if j["caller"] != caller:
            raise CollabError("NO_SUCH_REQUEST", f"no request {rid}", status=404)   # never reveal another caller's jobs
        return j

    async def wait(self, caller: str, rid: str, wait_s: float = 0) -> dict:
        j = self._owned(caller, rid)
        if wait_s > 0 and j["status"] not in JOB_TERMINAL:
            ev = self._event(rid)
            if j["status"] not in JOB_TERMINAL:
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(float(wait_s), MAX_WAIT_S))
                except asyncio.TimeoutError:
                    pass
            j = self.store.get_job(rid)
        return self.response(j)

    def cancel(self, caller: str, rid: str) -> dict:
        j = self._owned(caller, rid)
        if j["status"] in JOB_TERMINAL:
            return self.response(j)
        self.store.update_job(rid, cancel_requested=1)
        self.store.audit("job_cancel_requested", actor=caller, task_id=j["task_id"] or "", request_id=rid, client_id=j["client_id"] or "", outcome=j["status"])
        if j["status"] in ("queued", "awaiting_approval"):
            if j["approval_id"]:
                try:
                    self.store.transition_approval(j["approval_id"], "withdrawn", actor=caller, note="request cancelled")
                except CollabError:
                    pass
            t = self._running.get(rid)
            if t:
                t.cancel()
            self._finish(rid, "cancelled", error={"code": "CANCELLED", "message": "cancelled before it ran", "retryable": False})
        else:
            t = self._running.get(rid)
            if t:
                t.cancel()
        return self.response(self.store.get_job(rid))

    def response(self, j: dict) -> dict:
        ev = [{k: e[k] for k in ("evidence_id", "kind", "source_url", "collected_at", "sha256", "bytes", "summary")} for e in self.store.evidence_for(j["request_id"])]
        err = loads(j["error"], None)
        dur = int((j["finished_at"] - j["started_at"]) * 1000) if j["finished_at"] and j["started_at"] else None
        return {"protocol": PROTOCOL, "request_id": j["request_id"], "task_id": j["task_id"], "capability": j["capability"], "client_id": j["client_id"], "status": j["status"],
                "result": loads(j["result"], None), "evidence": ev, "sources": loads(j["sources"], []), "completeness": j["completeness"], "confidence": j["confidence"],
                "limitations": loads(j["limitations"], []), "error": err, "retryable": bool(j["retryable"]), "approval_id": j["approval_id"], "progress": j["progress"],
                "attempts": j["attempts"], "created_at": j["created_at"], "started_at": j["started_at"], "completed_at": j["finished_at"], "duration_ms": dur,
                "idempotency_key": j["idem_key"], "priority": j["priority"], "timeout_s": j["timeout_s"]}
