"""Collaborator-side client (stdlib only) and the `python -m octg_agent collab ...` command line.

This is what the collaborator AI (or any other automation) uses: it reads its token from <data dir>/collab_<caller>.token, talks to the loopback API, retries
safely (same request_id => never a second execution) and waits with bounded long-polls. Model-provider neutral: it moves JSON, nothing else.

Owner-only commands (approve / deny / token) act on the local files directly and require a person at an interactive terminal typing YES.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..config import load_settings
from . import PROTOCOL
from .auth import DEFAULT_CALLER

TERMINAL = ("succeeded", "failed", "cancelled", "timed_out", "rejected")


class CollabClientError(Exception):
    def __init__(self, status: int, body: dict):
        err = (body or {}).get("error") or {}
        super().__init__(f"{status} {err.get('code')}: {err.get('message')}")
        self.status, self.body, self.code = status, body, err.get("code")


class CollabClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, caller: str = DEFAULT_CALLER, data_dir: Path | None = None, timeout: float = 90.0):
        s = load_settings()
        self.base = (base_url or s.base_url).rstrip("/")
        d = Path(data_dir or s.data_dir)
        self.token = token or (d / f"collab_{caller}.token").read_text(encoding="utf-8").strip()
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, *, retries: int = 3) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        last: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(self.base + path, method=method, data=data, headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    b = json.loads(e.read())
                except ValueError:
                    b = {"error": {"code": f"HTTP_{e.code}", "message": "non-JSON error"}}
                if e.code >= 500 and attempt < retries:
                    last = CollabClientError(e.code, b)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CollabClientError(e.code, b) from None
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:   # agent restarting / not reachable: retry, then give up clearly
                last = e
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
        raise CollabClientError(0, {"error": {"code": "AGENT_UNREACHABLE", "message": f"cannot reach {self.base}: {last}"}})

    # ---- discovery / tasks
    def whoami(self) -> dict:
        return self._call("GET", "/api/collab/whoami")

    def capabilities(self) -> dict:
        return self._call("GET", "/api/collab/capabilities")

    def new_task(self, title: str, *, objective: str = "", client: str | None = None) -> dict:
        return self._call("POST", "/api/collab/tasks", {"title": title, "objective": objective, **({"client": client} if client else {})})["task"]

    def task(self, task_id: str) -> dict:
        return self._call("GET", f"/api/collab/tasks/{task_id}")

    def finish_task(self, task_id: str, status: str, *, outcome: str = "", summary: str = "") -> dict:
        return self._call("POST", f"/api/collab/tasks/{task_id}/finish", {"status": status, "outcome": outcome, "summary": summary})["task"]

    # ---- requests
    def submit(self, capability: str, parameters: dict | None = None, *, client: str | None = None, task_id: str | None = None, timeout_s: float | None = None,
               idempotency_key: str | None = None, priority: int | None = None, request_id: str | None = None, required_permissions: list[str] | None = None,
               expected_format: str | None = None) -> dict:
        req = {"protocol": PROTOCOL, "request_id": request_id or "rq_" + uuid.uuid4().hex[:12], "capability": capability, "parameters": parameters or {}, "created_at": time.time()}
        for k, v in (("client", client), ("task_id", task_id), ("timeout_s", timeout_s), ("idempotency_key", idempotency_key), ("priority", priority),
                     ("required_permissions", required_permissions), ("expected_format", expected_format)):
            if v is not None:
                req[k] = v
        return self._call("POST", "/api/collab/requests", req)["response"]   # retried with the SAME request_id: the agent returns the existing job

    def get(self, request_id: str, wait_s: float = 0) -> dict:
        return self._call("GET", f"/api/collab/requests/{request_id}?wait={wait_s:g}")["response"]

    def wait(self, request_id: str, max_wait_s: float = 600) -> dict:
        end = time.time() + max_wait_s
        r = self.get(request_id)
        while r["status"] not in TERMINAL and time.time() < end:
            r = self.get(request_id, wait_s=min(50.0, max(1.0, end - time.time())))
        return r

    def run(self, capability: str, parameters: dict | None = None, *, max_wait_s: float = 600, **kw) -> dict:
        r = self.submit(capability, parameters, **kw)
        return r if r["status"] in TERMINAL else self.wait(r["request_id"], max_wait_s)

    def cancel(self, request_id: str) -> dict:
        return self._call("POST", f"/api/collab/requests/{request_id}/cancel", {})["response"]

    def evidence(self, evidence_id: str) -> dict:
        return self._call("GET", f"/api/collab/evidence/{evidence_id}")

    def artifact(self, artifact_id: str) -> dict:
        return self._call("GET", f"/api/collab/artifacts/{artifact_id}")["artifact"]

    def audit(self, *, task_id: str | None = None, request_id: str | None = None) -> list[dict]:
        q = f"task_id={task_id}" if task_id else f"request_id={request_id}"
        return self._call("GET", f"/api/collab/audit?{q}")["audit"]


# ====================================================================== CLI
def _kv(tokens: list[str]) -> dict:
    out: dict = {}
    for t in tokens:
        if "=" not in t:
            raise SystemExit(f"expected key=value, got {t!r}")
        k, v = t.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _print(obj, out: str | None = None) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(json.dumps({"written": out, "bytes": len(text)}))
    else:
        sys.stdout.buffer.write(text.encode("utf-8") + b"\n")


def _owner_confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print("Refused: approvals and token changes need a person at an interactive terminal (stdin is not a TTY).", file=sys.stderr)
        return False
    return input(f"{prompt}\nType YES to confirm: ").strip() == "YES"


def cmd(args) -> int:
    if args.cc in ("approvals", "approve", "deny", "token"):
        return _owner(args)
    c = CollabClient(caller=args.caller)
    try:
        if args.cc == "whoami":
            _print(c.whoami())
        elif args.cc == "caps":
            cat = c.capabilities()
            if args.name:
                _print(next((x for x in cat["capabilities"] if x["name"] == args.name), {"error": "no such capability"}))
            else:
                for x in cat["capabilities"]:
                    print(f"{x['name']:24} {'RO' if x['read_only'] else 'RW'} {'client' if x['client_scope'] == 'client' else '      '} {x['purpose'][:110]}")
        elif args.cc == "task":
            if args.action == "new":
                _print(c.new_task(args.title, objective=args.objective or "", client=args.client))
            elif args.action == "show":
                _print(c.task(args.title))
            elif args.action == "finish":
                _print(c.finish_task(args.title, args.status, outcome=args.outcome or "", summary=args.summary or ""))
        elif args.cc == "run":
            params = json.loads(Path(args.json).read_text(encoding="utf-8")) if args.json else {}
            params.update(_kv(args.params))
            r = c.submit(args.capability, params, client=args.client, task_id=args.task, timeout_s=args.timeout, idempotency_key=args.idem, priority=args.priority)
            if args.wait and r["status"] not in TERMINAL:
                r = c.wait(r["request_id"], args.wait)
            _print(r, args.out)
            return 0 if r["status"] in ("succeeded", "queued", "running", "awaiting_approval") else 2
        elif args.cc == "get":
            _print(c.get(args.request_id, args.wait or 0) if not args.wait or args.wait <= 50 else c.wait(args.request_id, args.wait), args.out)
        elif args.cc == "cancel":
            _print(c.cancel(args.request_id))
        elif args.cc == "evidence":
            _print(c.evidence(args.evidence_id), args.out)
        elif args.cc == "artifact":
            a = c.artifact(args.artifact_id)
            if args.markdown and a.get("body_md"):
                (Path(args.out).write_text(a["body_md"], encoding="utf-8") if args.out else sys.stdout.buffer.write(a["body_md"].encode("utf-8") + b"\n"))
            else:
                _print(a, args.out)
        elif args.cc == "audit":
            _print(c.audit(task_id=args.task, request_id=args.request))
    except CollabClientError as e:
        _print({"ok": False, "status": e.status, **(e.body or {})})
        return 1
    return 0


def _owner(args) -> int:
    from ..config import load_settings as _ls
    from .auth import Callers
    from .store import CollabError, CollabStore
    s = _ls()
    st = CollabStore(Path(s.data_dir) / "collab.db")
    if args.cc == "approvals":
        rows = st.approvals(status=args.status)
        for a in rows:
            print(f"{a['approval_id']}  {a['status']:9} {a['action_kind']:18} {a['client_id'] or '-':14} {a['title'][:90]}")
        if not rows:
            print("(none)")
        return 0
    if args.cc in ("approve", "deny"):
        a = st.get_approval(args.approval_id)
        print(json.dumps({k: a[k] for k in ("approval_id", "action_kind", "title", "description", "details", "client_id", "task_id", "status", "requested_by")}, indent=2, default=str))
        if not _owner_confirm(f"{args.cc.upper()} this request?"):
            return 3
        # written straight to collab.db; the running agent notices the decision within a few seconds (approval watcher) and resumes or cancels a waiting job
        try:
            print(json.dumps(st.transition_approval(args.approval_id, "approved" if args.cc == "approve" else "denied", actor="owner:cli", note=args.note or ""), default=str, indent=2))
        except CollabError as e:
            print(e.message, file=sys.stderr)
            return 1
        return 0
    if args.cc == "token":
        cal = Callers(s.data_dir)
        if args.action == "list":
            _print(cal.list())
            return 0
        if args.action == "rotate":
            if not _owner_confirm(f"Rotate the token of '{args.name}'? Its current token stops working."):
                return 3
            cal.rotate(args.name)
            print(f"rotated; new token written to {cal.token_path(args.name)}")
            return 0
        if args.action in ("disable", "enable"):
            if not _owner_confirm(f"{args.action} caller '{args.name}'?"):
                return 3
            cal.disable(args.name, args.action == "disable")
            print("done")
            return 0
    return 1


def add_parser(sub) -> None:
    p = sub.add_parser("collab", help="collaboration protocol: delegate work to this agent as the AI collaborator / approve as the owner")
    p.add_argument("--caller", default=DEFAULT_CALLER)
    cs = p.add_subparsers(dest="cc", required=True)
    cs.add_parser("whoami")
    x = cs.add_parser("caps")
    x.add_argument("name", nargs="?")
    x = cs.add_parser("task")
    x.add_argument("action", choices=["new", "show", "finish"])
    x.add_argument("title", help="title (new) or task id (show/finish)")
    x.add_argument("--client")
    x.add_argument("--objective")
    x.add_argument("--status", default="completed", choices=["completed", "failed", "cancelled"])
    x.add_argument("--outcome")
    x.add_argument("--summary")
    x = cs.add_parser("run", help="submit a request: collab run web.fetch_page url=https://example.com --wait 120")
    x.add_argument("capability")
    x.add_argument("params", nargs="*")
    x.add_argument("--json", help="parameters from a JSON file")
    x.add_argument("--client")
    x.add_argument("--task")
    x.add_argument("--timeout", type=float)
    x.add_argument("--idem")
    x.add_argument("--priority", type=int)
    x.add_argument("--wait", type=float, default=120)
    x.add_argument("--out")
    x = cs.add_parser("get")
    x.add_argument("request_id")
    x.add_argument("--wait", type=float)
    x.add_argument("--out")
    x = cs.add_parser("cancel")
    x.add_argument("request_id")
    x = cs.add_parser("evidence")
    x.add_argument("evidence_id")
    x.add_argument("--out")
    x = cs.add_parser("artifact")
    x.add_argument("artifact_id")
    x.add_argument("--markdown", action="store_true")
    x.add_argument("--out")
    x = cs.add_parser("audit")
    x.add_argument("--task")
    x.add_argument("--request")
    x = cs.add_parser("approvals", help="(owner) list approval requests")
    x.add_argument("--status")
    for n in ("approve", "deny"):
        x = cs.add_parser(n, help=f"(owner) {n} an approval request; needs YES at an interactive terminal")
        x.add_argument("approval_id")
        x.add_argument("--note")
    x = cs.add_parser("token", help="(owner) list / rotate / disable collaborator tokens")
    x.add_argument("action", choices=["list", "rotate", "disable", "enable"])
    x.add_argument("name", nargs="?", default=DEFAULT_CALLER)
    p.set_defaults(fn=cmd)
