"""collab.db: parent tasks, delegated jobs, evidence, artifacts, approvals, deployment targets and an append-only audit trail.

Separate from business.db on purpose: business.db is the owner's verified record of clients; collab.db is the working memory of the collaboration
(what was asked, what was found, what was proposed, approved, executed and verified). Rows that point at a client carry its client_id, and every read that
returns client material filters by it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from ..sensitive import scrub_text

SCHEMA_VERSION = 1
JOB_STATES = ("queued", "awaiting_approval", "running", "succeeded", "failed", "cancelled", "timed_out", "rejected")
JOB_TERMINAL = ("succeeded", "failed", "cancelled", "timed_out", "rejected")
TASK_STATES = ("open", "completed", "failed", "cancelled")
APPROVAL_STATES = ("proposed", "approved", "denied", "executed", "verified", "failed", "withdrawn")
ARTIFACT_KINDS = ("analysis", "report", "handoff", "change", "verification", "finding_set", "note", "plan")
STAGES = ("research", "reasoning", "implementation", "verification", "coordination")

DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT, client_id TEXT, status TEXT NOT NULL, caller TEXT,
  created_at REAL, updated_at REAL, finished_at REAL, outcome TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  request_id TEXT PRIMARY KEY, task_id TEXT, caller TEXT NOT NULL, idem_key TEXT, capability TEXT NOT NULL, client_id TEXT,
  params TEXT, params_hash TEXT, permissions TEXT, expected_format TEXT, priority INTEGER DEFAULT 5, timeout_s REAL,
  status TEXT NOT NULL, created_at REAL, started_at REAL, finished_at REAL, attempts INTEGER DEFAULT 0,
  result TEXT, error TEXT, retryable INTEGER DEFAULT 0, completeness TEXT, confidence TEXT, limitations TEXT, sources TEXT,
  cancel_requested INTEGER DEFAULT 0, approval_id TEXT, progress TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idem ON jobs(caller, idem_key) WHERE idem_key IS NOT NULL AND idem_key <> '';
CREATE INDEX IF NOT EXISTS jobs_task ON jobs(task_id);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY, request_id TEXT, task_id TEXT, client_id TEXT, kind TEXT, source_url TEXT, collected_at REAL,
  sha256 TEXT, path TEXT, bytes INTEGER, summary TEXT
);
CREATE INDEX IF NOT EXISTS evidence_req ON evidence(request_id);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY, task_id TEXT, client_id TEXT, kind TEXT NOT NULL, stage TEXT NOT NULL, title TEXT, body TEXT, body_md TEXT,
  links TEXT, status TEXT, created_at REAL, updated_at REAL, created_by TEXT
);
CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, task_id TEXT, client_id TEXT, action_kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT, details TEXT,
  status TEXT NOT NULL, requested_by TEXT, requested_at REAL, decided_by TEXT, decided_at REAL, decision_note TEXT,
  executed_at REAL, execution TEXT, verified_at REAL, verification TEXT, artifact_id TEXT
);
CREATE TABLE IF NOT EXISTS targets (
  target_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, domain TEXT NOT NULL, environment TEXT NOT NULL, local_path TEXT, hosting TEXT,
  deploy_method TEXT, rollback_method TEXT, notes TEXT, verified_evidence TEXT, created_at REAL, updated_at REAL, state TEXT DEFAULT 'LIVE'
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT, event TEXT NOT NULL, task_id TEXT, request_id TEXT, client_id TEXT, outcome TEXT, detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""


class CollabError(Exception):
    def __init__(self, code: str, message: str, details: Any = None, *, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.details, self.status, self.retryable = code, message, details, status, retryable

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details is not None:
            d["details"] = self.details
        return d


def new_id(prefix: str) -> str:
    """prefix_<12 hex>. Never all digits: a purely numeric id (about 1 in 280 by chance) is exactly what the secret scanner calls a 12-digit identifier, and the request carrying it would be refused."""
    while True:
        h = uuid.uuid4().hex[:12]
        if not h.isdigit():
            return f"{prefix}_{h}"


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def params_hash(capability: str, client_id: str | None, params: dict) -> str:
    return hashlib.sha256(dumps([capability, client_id or "", params]).encode()).hexdigest()


class CollabStore:
    def __init__(self, path: Path | str, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = self.path.parent / "collab" / "evidence"
        self.reports_dir = self.path.parent / "collab" / "reports"
        self._clock = clock
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(DDL)
            c.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    @contextlib.contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            try:
                yield c
            finally:
                c.close()

    def now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------ audit (append-only)
    def audit(self, event: str, *, actor: str = "", task_id: str = "", request_id: str = "", client_id: str = "", outcome: str = "", detail: str = "",
              c: sqlite3.Connection | None = None) -> None:
        row = (self.now(), actor[:60], event[:60], task_id or "", request_id or "", client_id or "", outcome[:60], scrub_text(str(detail or ""))[:500])
        if c is not None:
            c.execute("INSERT INTO audit (at,actor,event,task_id,request_id,client_id,outcome,detail) VALUES (?,?,?,?,?,?,?,?)", row)
            return
        with self.conn() as cc:
            cc.execute("INSERT INTO audit (at,actor,event,task_id,request_id,client_id,outcome,detail) VALUES (?,?,?,?,?,?,?,?)", row)

    def audit_tail(self, n: int = 100, *, task_id: str | None = None, request_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM audit WHERE 1=1", []
        if task_id:
            q += " AND task_id=?"
            a.append(task_id)
        if request_id:
            q += " AND request_id=?"
            a.append(request_id)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY seq DESC LIMIT ?", (*a, n)).fetchall()][::-1]

    # ------------------------------------------------------------------ parent tasks
    def create_task(self, title: str, *, objective: str = "", client_id: str | None = None, caller: str = "") -> dict:
        tid = new_id("ct")
        now = self.now()
        with self.conn() as c:
            c.execute("INSERT INTO tasks (task_id,title,objective,client_id,status,caller,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (tid, scrub_text(title)[:200], scrub_text(objective)[:4000], client_id, "open", caller, now, now))
            self.audit("task_created", actor=caller, task_id=tid, client_id=client_id or "", outcome="open", detail=title[:200], c=c)
        return self.get_task(tid)

    def get_task(self, task_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_TASK", f"no collaboration task {task_id}", status=404)
        return dict(r)

    def list_tasks(self, limit: int = 30, status: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM tasks", []
        if status:
            q += " WHERE status=?"
            a.append(status)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*a, limit)).fetchall()]

    def finish_task(self, task_id: str, status: str, *, outcome: str = "", summary: str = "", actor: str = "") -> dict:
        if status not in TASK_STATES or status == "open":
            raise CollabError("BAD_STATUS", f"a finished task is one of {TASK_STATES[1:]}")
        t = self.get_task(task_id)
        if t["status"] != "open":
            raise CollabError("TASK_CLOSED", f"task {task_id} is already {t['status']}", status=409)
        now = self.now()
        with self.conn() as c:
            c.execute("UPDATE tasks SET status=?, outcome=?, summary=?, finished_at=?, updated_at=? WHERE task_id=?",
                      (status, scrub_text(outcome)[:200], scrub_text(summary)[:8000], now, now, task_id))
            self.audit("task_finished", actor=actor, task_id=task_id, client_id=t["client_id"] or "", outcome=status, detail=outcome[:200], c=c)
        return self.get_task(task_id)

    def touch_task(self, task_id: str | None) -> None:
        if task_id:
            with self.conn() as c:
                c.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (self.now(), task_id))

    # ------------------------------------------------------------------ jobs
    def find_idempotent(self, caller: str, idem_key: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE caller=? AND idem_key=?", (caller, idem_key)).fetchone()
        return dict(r) if r else None

    def insert_job(self, row: dict) -> dict:
        cols = ",".join(row)
        with self.conn() as c:
            c.execute(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        return self.get_job(row["request_id"])

    def get_job(self, request_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_REQUEST", f"no request {request_id}", status=404)
        return dict(r)

    def job_exists(self, request_id: str) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM jobs WHERE request_id=?", (request_id,)).fetchone() is not None

    def update_job(self, request_id: str, **fields: Any) -> dict:
        if fields:
            sets = ",".join(f"{k}=?" for k in fields)
            with self.conn() as c:
                c.execute(f"UPDATE jobs SET {sets} WHERE request_id=?", (*fields.values(), request_id))
        return self.get_job(request_id)

    def jobs(self, *, task_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        q, a = "SELECT * FROM jobs WHERE 1=1", []
        if task_id:
            q += " AND task_id=?"
            a.append(task_id)
        if status:
            q += " AND status=?"
            a.append(status)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*a, limit)).fetchall()]

    def jobs_in(self, states: tuple[str, ...]) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(states))}) ORDER BY priority, created_at", states).fetchall()]

    # ------------------------------------------------------------------ evidence (content on disk, metadata here)
    def add_evidence(self, *, request_id: str, task_id: str | None, client_id: str | None, kind: str, content: bytes | str, source_url: str = "", summary: str = "",
                     ext: str = "json", collected_at: float | None = None) -> dict:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        eid = new_id("ev")
        day = time.strftime("%Y%m%d", time.localtime(self.now()))
        d = self.evidence_dir / day
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{eid}.{ext}"
        p.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        row = (eid, request_id, task_id, client_id, kind, source_url[:1000], float(collected_at or self.now()), sha, str(p), len(raw), scrub_text(summary)[:500])
        with self.conn() as c:
            c.execute("INSERT INTO evidence (evidence_id,request_id,task_id,client_id,kind,source_url,collected_at,sha256,path,bytes,summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        return self.get_evidence(eid)

    def get_evidence(self, evidence_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_EVIDENCE", f"no evidence {evidence_id}", status=404)
        return dict(r)

    def evidence_for(self, request_id: str) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM evidence WHERE request_id=? ORDER BY collected_at", (request_id,)).fetchall()]

    def read_evidence(self, evidence_id: str, max_bytes: int = 2_000_000) -> tuple[dict, bytes]:
        ev = self.get_evidence(evidence_id)
        p = Path(ev["path"])
        if not p.is_file() or self.evidence_dir.resolve() not in p.resolve().parents:
            raise CollabError("EVIDENCE_MISSING", "the evidence file is gone", status=410)
        raw = p.read_bytes()[:max_bytes]
        if hashlib.sha256(p.read_bytes()).hexdigest() != ev["sha256"]:
            raise CollabError("EVIDENCE_TAMPERED", "the evidence file no longer matches its recorded hash", status=409)
        return ev, raw

    # ------------------------------------------------------------------ artifacts
    def add_artifact(self, *, task_id: str | None, client_id: str | None, kind: str, stage: str, title: str, body: Any = None, body_md: str = "", links: dict | None = None,
                     status: str = "recorded", created_by: str = "") -> dict:
        if kind not in ARTIFACT_KINDS:
            raise CollabError("BAD_KIND", f"artifact kind must be one of {ARTIFACT_KINDS}")
        if stage not in STAGES:
            raise CollabError("BAD_STAGE", f"stage must be one of {STAGES}")
        aid = new_id("ar")
        now = self.now()
        with self.conn() as c:
            c.execute("INSERT INTO artifacts (artifact_id,task_id,client_id,kind,stage,title,body,body_md,links,status,created_at,updated_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, task_id, client_id, kind, stage, scrub_text(title)[:300], dumps(body if body is not None else {}), body_md, dumps(links or {}), status, now, now, created_by))
            self.audit("artifact_" + kind, actor=created_by, task_id=task_id or "", client_id=client_id or "", outcome=status, detail=f"{aid} {title[:120]}", c=c)
        return self.get_artifact(aid)

    def get_artifact(self, artifact_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_ARTIFACT", f"no artifact {artifact_id}", status=404)
        d = dict(r)
        d["body"] = loads(d["body"], {})
        d["links"] = loads(d["links"], {})
        return d

    def update_artifact(self, artifact_id: str, *, status: str | None = None, body: Any = None, body_md: str | None = None, links: dict | None = None, actor: str = "") -> dict:
        a = self.get_artifact(artifact_id)
        fields: dict[str, Any] = {"updated_at": self.now()}
        if status is not None:
            fields["status"] = status
        if body is not None:
            fields["body"] = dumps(body)
        if body_md is not None:
            fields["body_md"] = body_md
        if links is not None:
            fields["links"] = dumps({**a["links"], **links})
        with self.conn() as c:
            c.execute(f"UPDATE artifacts SET {','.join(f'{k}=?' for k in fields)} WHERE artifact_id=?", (*fields.values(), artifact_id))
            self.audit("artifact_updated", actor=actor, task_id=a["task_id"] or "", client_id=a["client_id"] or "", outcome=status or a["status"], detail=artifact_id, c=c)
        return self.get_artifact(artifact_id)

    def artifacts(self, *, task_id: str | None = None, client_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[dict]:
        q, a = "SELECT artifact_id,task_id,client_id,kind,stage,title,status,created_at,updated_at,created_by FROM artifacts WHERE 1=1", []
        for col, val in (("task_id", task_id), ("client_id", client_id), ("kind", kind)):
            if val:
                q += f" AND {col}=?"
                a.append(val)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*a, limit)).fetchall()]

    # ------------------------------------------------------------------ approvals
    def request_approval(self, *, task_id: str | None, client_id: str | None, action_kind: str, title: str, description: str = "", details: dict | None = None,
                         requested_by: str = "", artifact_id: str | None = None) -> dict:
        aid = new_id("ap")
        with self.conn() as c:
            c.execute("INSERT INTO approvals (approval_id,task_id,client_id,action_kind,title,description,details,status,requested_by,requested_at,artifact_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, task_id, client_id, action_kind, scrub_text(title)[:300], scrub_text(description)[:4000], dumps(details or {}), "proposed", requested_by, self.now(), artifact_id))
            self.audit("approval_requested", actor=requested_by, task_id=task_id or "", client_id=client_id or "", outcome="proposed", detail=f"{aid} {action_kind}: {title[:150]}", c=c)
        return self.get_approval(aid)

    def get_approval(self, approval_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_APPROVAL", f"no approval {approval_id}", status=404)
        d = dict(r)
        d["details"] = loads(d["details"], {})
        d["execution"] = loads(d["execution"], None)
        d["verification"] = loads(d["verification"], None)
        return d

    def approvals(self, *, status: str | None = None, task_id: str | None = None, limit: int = 100) -> list[dict]:
        q, a = "SELECT approval_id FROM approvals WHERE 1=1", []
        if status:
            q += " AND status=?"
            a.append(status)
        if task_id:
            q += " AND task_id=?"
            a.append(task_id)
        with self.conn() as c:
            ids = [r[0] for r in c.execute(q + " ORDER BY requested_at DESC LIMIT ?", (*a, limit)).fetchall()]
        return [self.get_approval(i) for i in ids]

    _TRANSITIONS = {"approved": ("proposed",), "denied": ("proposed",), "withdrawn": ("proposed",), "executed": ("approved",), "failed": ("approved", "executed"),
                    "verified": ("executed",)}

    def transition_approval(self, approval_id: str, to: str, *, actor: str, note: str = "", data: dict | None = None) -> dict:
        """The ONLY way an approval changes state. Who may call which transition is decided by the caller (hub/CLI for decisions, the protocol for execution/verification)."""
        ap = self.get_approval(approval_id)
        allowed = self._TRANSITIONS.get(to)
        if allowed is None:
            raise CollabError("BAD_STATUS", f"cannot move an approval to {to!r}")
        if ap["status"] not in allowed:
            raise CollabError("BAD_TRANSITION", f"approval {approval_id} is {ap['status']}; {to} needs it to be {' or '.join(allowed)}", status=409)
        now = self.now()
        fields: dict[str, Any] = {"status": to}
        if to in ("approved", "denied", "withdrawn"):
            fields.update(decided_by=actor, decided_at=now, decision_note=scrub_text(note)[:1000])
        elif to in ("executed", "failed"):
            fields.update(executed_at=now, execution=dumps(data or {"note": note}))
        elif to == "verified":
            fields.update(verified_at=now, verification=dumps(data or {"note": note}))
        with self.conn() as c:
            c.execute(f"UPDATE approvals SET {','.join(f'{k}=?' for k in fields)} WHERE approval_id=? AND status=?", (*fields.values(), approval_id, ap["status"]))
            self.audit("approval_" + to, actor=actor, task_id=ap["task_id"] or "", client_id=ap["client_id"] or "", outcome=to, detail=f"{approval_id} {note[:200]}", c=c)
        return self.get_approval(approval_id)

    # ------------------------------------------------------------------ deployment targets
    def put_target(self, *, client_id: str, domain: str, environment: str, local_path: str = "", hosting: str = "", deploy_method: str = "", rollback_method: str = "",
                   notes: str = "", verified_evidence: list | None = None, actor: str = "") -> dict:
        domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").strip("/")
        now = self.now()
        with self.conn() as c:
            old = c.execute("SELECT target_id FROM targets WHERE client_id=? AND domain=? AND environment=? AND state='LIVE'", (client_id, domain, environment)).fetchone()
            if old:
                c.execute("UPDATE targets SET state='SUPERSEDED', updated_at=? WHERE target_id=?", (now, old[0]))
            tid = new_id("tg")
            c.execute("INSERT INTO targets (target_id,client_id,domain,environment,local_path,hosting,deploy_method,rollback_method,notes,verified_evidence,created_at,updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (tid, client_id, domain, environment, local_path, hosting, deploy_method, rollback_method, scrub_text(notes)[:2000],
                                                           dumps(verified_evidence or []), now, now))
            self.audit("target_registered", actor=actor, client_id=client_id, outcome=environment, detail=f"{tid} {domain}", c=c)
        return self.get_target(tid)

    def get_target(self, target_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM targets WHERE target_id=?", (target_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_TARGET", f"no deployment target {target_id}", status=404)
        d = dict(r)
        d["verified_evidence"] = loads(d["verified_evidence"], [])
        return d

    def targets(self, client_id: str) -> list[dict]:
        with self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT target_id FROM targets WHERE client_id=? AND state='LIVE' ORDER BY created_at", (client_id,)).fetchall()]
        return [self.get_target(i) for i in ids]
