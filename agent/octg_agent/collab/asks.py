"""The owner's "Ask the agent" logic, shared by the Hub page and the Chrome side panel so both behave identically: a plain sentence becomes Google searches (search.report, read-only), and every
question is a collab task whose job result is the plain-language answer."""
from __future__ import annotations

import json

from .intent import MAX_QUERIES, parse_search_request
from .store import CollabError

ASK_CALLER = "owner-ui"
ASK_GRANTS = ("search:read", "research:write", "client:read", "collab:write")      # exactly what search.report needs: nothing that could change a site
ASK_CAP = "search.report"
DONE = ("succeeded", "failed", "cancelled", "timed_out")


class AskError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class AskService:
    def __init__(self, svc):
        self.svc = svc

    def clients(self) -> dict:
        """Active clients for the "which client" choice, plus the owner's default client."""
        admin = self.svc.admin()
        rows = [{"id": c["client_id"], "name": c["name"]} for c in admin.store.list_clients() if not c["archived"]]
        try:
            default = admin.hubcfg.active_scope()
        except Exception:  # noqa: BLE001 - the default is a convenience only
            default = None
        return {"clients": rows, "default": default if any(r["id"] == default for r in rows) else (rows[0]["id"] if rows else None)}

    def send(self, *, text: str, client_id: str, queries: list | None = None, location: str = "") -> dict:
        """Returns {"understood": False, "reply": ...} when no search could be found, else {"understood": True, "ask_id", "queries", "notes"}. Raises AskError."""
        svc = self.svc
        text = str(text or "").strip()[:4000]
        if not isinstance(client_id, str) or not client_id:
            raise AskError("CLIENT_REQUIRED", "choose which client this is for")
        client = svc.admin().store.get_client(client_id)
        if isinstance(queries, list):      # "Try again": exactly the searches that were not run
            qs = [str(q).strip()[:160] for q in queries if str(q).strip()][:MAX_QUERIES]
            notes: list[str] = []
        else:
            parsed = parse_search_request(text)
            qs, notes = parsed["queries"], parsed["notes"]
        if not qs:
            return {"understood": False, "reply": " ".join(notes) or "I could not find what to search for."}
        if [j for s in ("queued", "running") for j in svc.store.jobs(status=s, limit=50) if j["caller"] == ASK_CALLER]:
            raise AskError("BUSY", "A search is still running. Wait for its answer first: searches are spaced out on purpose so Google does not block this browser.", 409)
        location = str(location or "").strip()[:120]
        params = {"queries": qs, **({"location": location} if location else {})}
        try:
            task = svc.store.create_task(f"Ask: {text[:80] or ', '.join(qs)[:80]}", objective=text, client_id=client["client_id"], caller=ASK_CALLER)
            svc.submit(ASK_CALLER, ASK_GRANTS, {"capability": ASK_CAP, "parameters": params, "client": client["client_id"], "task_id": task["task_id"]})
        except CollabError as e:
            raise AskError(e.code, e.message, e.status) from None
        return {"understood": True, "ask_id": task["task_id"], "queries": qs, "notes": notes}

    def list(self, limit: int = 30) -> list[dict]:
        svc = self.svc
        names = {c["client_id"]: c["name"] for c in svc.admin().store.list_clients(include_archived=True)}
        rows = []
        for t in svc.store.list_tasks(100):
            if t["caller"] != ASK_CALLER:
                continue
            jobs = [j for j in svc.store.jobs(task_id=t["task_id"], limit=5) if j["capability"] == ASK_CAP]
            j = jobs[0] if jobs else None
            res = json.loads(j["result"]) if j and j["result"] else None
            err = json.loads(j["error"]) if j and j["error"] else None
            if j and j["status"] in DONE and t["status"] == "open":      # a finished ask closes its task, so the task list stays tidy
                svc.store.finish_task(t["task_id"], "completed" if j["status"] == "succeeded" else "failed", outcome=j["status"], summary=((res or {}).get("report") or (err or {}).get("message") or "")[:4000], actor=ASK_CALLER)
            params = json.loads(j["params"]) if j else {}
            rows.append({"ask_id": t["task_id"], "text": t["objective"], "client_id": t["client_id"], "client_name": names.get(t["client_id"] or "", ""), "asked_at": t["created_at"],
                         "status": j["status"] if j else "unknown", "progress": (j or {}).get("progress") or "", "finished_at": (j or {}).get("finished_at"),
                         "queries": params.get("queries") or [], "location": params.get("location"),
                         "report": (res or {}).get("report"), "outcome": (res or {}).get("status"), "remaining": (res or {}).get("remaining") or [],
                         "searches": [{k: q[k] for k in ("query", "status", "client_position")} for q in (res or {}).get("queries", [])], "error": (err or {}).get("message") if err else None,
                         "error_code": (err or {}).get("code") if err else None, "request_id": j["request_id"] if j else None})
            if len(rows) >= limit:
                break
        return rows

    def cancel(self, ask_id: str) -> None:
        svc = self.svc
        t = svc.store.get_task(ask_id)
        if t["caller"] != ASK_CALLER:
            raise AskError("NO_SUCH_TASK", "no such question", 404)
        jobs = [j for j in svc.store.jobs(task_id=t["task_id"], limit=5) if j["capability"] == ASK_CAP]
        if not jobs:
            raise AskError("NOTHING_TO_CANCEL", "nothing is running for that question", 404)
        try:
            svc.cancel(ASK_CALLER, jobs[0]["request_id"])
        except CollabError as e:
            raise AskError(e.code, e.message, e.status) from None
