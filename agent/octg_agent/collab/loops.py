"""The loop controller: a durable, general-purpose "research -> plan -> implement -> wait -> verify -> decide" cycle for ONE client.

It is not SEO-specific. A *loop kind* (see loop_kinds.py) says what to measure, how to turn the answers into comparable observations and when the goal is met;
the controller owns everything else: baseline, rounds, scheduling, retries, budgets, stop conditions, history and the hand-offs to the collaborator.

Who does what (the collaborator is the reasoning/coding AI; the agent is this machine):
    controller (this module, no model, no browser kept open)   baseline + re-measurements through the existing collab capabilities, comparison, goal check, scheduling
    collaborator                                               analyses the evidence, submits a PLAN, implements it through the existing handoff/change/approval workflow
    owner                                                      approves production deploys (PIN), may pause / stop at any time

A loop is only ever a queue of state: while it waits (for the collaborator, an approval or a re-check date) nothing runs and no model or browser session is held.
Everything is in <data dir>/collab.db and survives a restart; the scheduler tick resumes whatever is due.

States (status):  active | paused | blocked | goal_reached | stopped | exhausted   (the last three are final)
Phases (while active):  measuring -> awaiting_plan -> awaiting_implementation -> waiting -> measuring -> ...
Approvals are never bypassed here: a round can only be reported as implemented when its changes are recorded as DEPLOYED, and a production change is only ever
marked deployed after the owner's approval (change.mark_deployed). The controller approves nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .store import CollabError, CollabStore, dumps, loads, new_id

log = logging.getLogger("octg.collab.loops")

FINAL = ("goal_reached", "stopped", "exhausted", "completed")   # completed = ended by design (a read-only baseline audit)
HIGHER_IS_BETTER = {"clicks", "impressions", "ctr"}      # every other metric (positions, counts of problems) improves when it goes DOWN
POSITION_METRICS = {"organic_position", "avg_position"}
PHASES = ("measuring", "awaiting_plan", "awaiting_implementation", "waiting", "done")
DEFAULTS = {"max_attempts": 3, "retry_backoff_s": [300, 900, 3600], "recheck_after_hours": 72, "confirm_gap_hours": 12, "max_waits": 2, "min_gap_hours": 12}
HOUR = 3600.0
SMALL_CHANGE = 2          # a move of this many positions or fewer is reported as possibly ordinary fluctuation
VOLATILE_RANGE = 5        # positions between the best and worst of the last observations before a target is called volatile

DDL = """
CREATE TABLE IF NOT EXISTS loops (
  loop_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, client_id TEXT NOT NULL, caller TEXT NOT NULL, task_id TEXT,
  config TEXT, goal TEXT, status TEXT NOT NULL, phase TEXT NOT NULL, round_no INTEGER DEFAULT 0, next_check_at REAL, pending TEXT, blockers TEXT,
  attempts INTEGER DEFAULT 0, retry_at REAL, requests_used INTEGER DEFAULT 0, outcome TEXT, summary TEXT, paused_from TEXT,
  created_at REAL, updated_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS loops_client ON loops(client_id);
CREATE TABLE IF NOT EXISTS loop_rounds (
  loop_id TEXT NOT NULL, n INTEGER NOT NULL, plan TEXT, changes TEXT, comparison TEXT, verdict TEXT, notes TEXT,
  started_at REAL, planned_at REAL, implemented_at REAL, measured_at REAL, PRIMARY KEY (loop_id, n)
);
CREATE TABLE IF NOT EXISTS loop_observations (
  obs_id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, round_no INTEGER NOT NULL, key TEXT NOT NULL, label TEXT, metric TEXT, value REAL, status TEXT NOT NULL,
  comparable INTEGER, cmp_key TEXT, observed_at REAL, source TEXT, evidence_id TEXT, request_id TEXT, limits TEXT
);
CREATE INDEX IF NOT EXISTS loop_obs_loop ON loop_observations(loop_id, key, observed_at);
CREATE TABLE IF NOT EXISTS loop_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, loop_id TEXT NOT NULL, at REAL NOT NULL, round_no INTEGER, event TEXT NOT NULL, actor TEXT, detail TEXT, data TEXT
);
CREATE TRIGGER IF NOT EXISTS loop_events_no_update BEFORE UPDATE ON loop_events BEGIN SELECT RAISE(ABORT, 'loop history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS loop_events_no_delete BEFORE DELETE ON loop_events BEGIN SELECT RAISE(ABORT, 'loop history is append-only'); END;
"""

OBS_STATUS = ("found", "not_found", "unavailable")   # found = measured a value; not_found = measured and the thing is absent (a NEGATIVE result); unavailable = could not measure (NOT a result)


def fingerprint(item: dict) -> str:
    """Identity of a planned change, so the same change is not applied twice without a reason: its action, the pages it touches and the finding it addresses."""
    pages = sorted({str(p).strip().rstrip("/").lower() for p in (item.get("pages") or [])})
    raw = "|".join([str(item.get("action_kind", "")).strip().lower(), ",".join(pages), str(item.get("finding_key", "")).strip().lower()])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _epoch(v: Any) -> float | None:
    """A deadline given as epoch seconds or an ISO date/time."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise CollabError("BAD_GOAL", "deadline must be an ISO date/time (e.g. 2026-12-31) or epoch seconds") from None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


# ============================================================================================================================ kinds
class LoopKind:
    """What one kind of loop measures. Subclasses live in loop_kinds.py; register with `register_kind`."""
    name = ""
    title = ""
    description = ""
    permissions: tuple[str, ...] = ()          # what the creating caller must hold: the loop never has more rights than its creator

    svc = None   # bound by the controller (a kind may look at connector readiness, mappings, ...)

    def check(self, svc, client_id: str, config: dict, goal: dict) -> list[str]:
        return []

    def followups(self, loop: dict, purpose: str, stage: int, responses: list[dict]) -> list[dict]:
        """Requests that can only be made once the earlier ones answered (e.g. competitor research needs the search results). Return [] when there is nothing more; `stage` is how many
        rounds of requests have completed. Same request shape as `requests`."""
        return []

    def cross_checks(self, loop: dict, latest: dict[str, dict]) -> list[str]:
        """Plain-language notes where independent sources disagree."""
        return []

    def requests(self, loop: dict, purpose: str) -> list[dict]:
        """[{capability, parameters, label}] to submit through the collaboration layer. purpose: baseline | verify."""
        raise NotImplementedError

    def observations(self, loop: dict, purpose: str, responses: list[dict]) -> list[dict]:
        """Normalised observations from the capability responses. Each: {key, label, metric, value, status (OBS_STATUS), comparable, cmp_key, observed_at, source, evidence_id, request_id, limits[]}."""
        raise NotImplementedError

    def targets(self, loop: dict) -> list[dict]:
        """[{key, label, ...}] the goal is about."""
        return []

    def evaluate_target(self, target: dict, series: list[dict], confirmations: int, min_gap_s: float) -> dict:
        """Verdict of one target from its time-ordered observations."""
        raise NotImplementedError

    def sources(self, loop: dict, responses: list[dict]) -> list[dict]:
        return []

    def brief(self, svc, loop: dict) -> dict:
        """Pointers the collaborator needs to plan (artifact ids, pages...). References only; the evidence stays in the store."""
        return {}


KINDS: dict[str, LoopKind] = {}


def register_kind(kind: LoopKind) -> LoopKind:
    if not kind.name or kind.name in KINDS:
        raise ValueError(f"loop kind {kind.name!r} is empty or registered twice")
    KINDS[kind.name] = kind
    return kind


# ============================================================================================================================ store
class LoopStore:
    def __init__(self, store: CollabStore):
        self.store = store
        with store.conn() as c:
            c.executescript(DDL)

    # --- loops
    def insert(self, row: dict) -> None:
        with self.store.conn() as c:
            c.execute(f"INSERT INTO loops ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def get(self, loop_id: str) -> dict:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM loops WHERE loop_id=?", (loop_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_LOOP", f"no loop {loop_id}", status=404)
        return self._decode(dict(r))

    @staticmethod
    def _decode(d: dict) -> dict:
        for k in ("config", "goal"):
            d[k] = loads(d.get(k), {})
        d["pending"] = loads(d.get("pending"), None)
        d["blockers"] = loads(d.get("blockers"), [])
        return d

    def update(self, loop_id: str, **f: Any) -> dict:
        f["updated_at"] = self.store.now()
        for k in ("config", "goal", "pending", "blockers"):
            if k in f:
                f[k] = dumps(f[k])
        with self.store.conn() as c:
            c.execute(f"UPDATE loops SET {','.join(f'{k}=?' for k in f)} WHERE loop_id=?", (*f.values(), loop_id))
        return self.get(loop_id)

    def list(self, *, client_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        q, a = "SELECT * FROM loops WHERE 1=1", []
        for col, v in (("client_id", client_id), ("status", status)):
            if v:
                q += f" AND {col}=?"
                a.append(v)
        with self.store.conn() as c:
            return [self._decode(dict(r)) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*a, limit)).fetchall()]

    def open_loops(self) -> list[dict]:
        with self.store.conn() as c:
            return [self._decode(dict(r)) for r in c.execute("SELECT * FROM loops WHERE status IN ('active','blocked','paused')").fetchall()]

    # --- rounds
    def round(self, loop_id: str, n: int) -> dict | None:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM loop_rounds WHERE loop_id=? AND n=?", (loop_id, n)).fetchone()
        return self._round(dict(r)) if r else None

    @staticmethod
    def _round(d: dict) -> dict:
        for k in ("plan", "changes", "comparison", "verdict"):
            d[k] = loads(d.get(k), None)
        return d

    def rounds(self, loop_id: str) -> list[dict]:
        with self.store.conn() as c:
            return [self._round(dict(r)) for r in c.execute("SELECT * FROM loop_rounds WHERE loop_id=? ORDER BY n", (loop_id,)).fetchall()]

    def put_round(self, loop_id: str, n: int, **f: Any) -> dict:
        for k in ("plan", "changes", "comparison", "verdict"):
            if k in f:
                f[k] = dumps(f[k])
        with self.store.conn() as c:
            if c.execute("SELECT 1 FROM loop_rounds WHERE loop_id=? AND n=?", (loop_id, n)).fetchone():
                if f:
                    c.execute(f"UPDATE loop_rounds SET {','.join(f'{k}=?' for k in f)} WHERE loop_id=? AND n=?", (*f.values(), loop_id, n))
            else:
                f.setdefault("started_at", self.store.now())
                c.execute(f"INSERT INTO loop_rounds (loop_id,n,{','.join(f)}) VALUES (?,?,{','.join('?' * len(f))})", (loop_id, n, *f.values()))
        return self.round(loop_id, n)

    # --- observations
    def add_observations(self, loop_id: str, round_no: int, obs: list[dict]) -> None:
        with self.store.conn() as c:
            for o in obs:
                c.execute("INSERT INTO loop_observations (obs_id,loop_id,round_no,key,label,metric,value,status,comparable,cmp_key,observed_at,source,evidence_id,request_id,limits) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (new_id("ob"), loop_id, round_no, o["key"], o.get("label", ""), o.get("metric", ""), o.get("value"), o["status"], int(bool(o.get("comparable"))), o.get("cmp_key", ""),
                           o.get("observed_at") or self.store.now(), o.get("source", ""), o.get("evidence_id", ""), o.get("request_id", ""), dumps(o.get("limits") or [])))

    def observations(self, loop_id: str, *, round_no: int | None = None) -> list[dict]:
        q, a = "SELECT * FROM loop_observations WHERE loop_id=?", [loop_id]
        if round_no is not None:
            q += " AND round_no=?"
            a.append(round_no)
        with self.store.conn() as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY observed_at, rowid", a).fetchall()]
        for r in rows:
            r["limits"] = loads(r["limits"], [])
            r["comparable"] = bool(r["comparable"])
        return rows

    # --- history (append-only)
    def event(self, loop_id: str, event: str, *, round_no: int | None = None, actor: str = "controller", detail: str = "", data: Any = None) -> None:
        with self.store.conn() as c:
            c.execute("INSERT INTO loop_events (loop_id,at,round_no,event,actor,detail,data) VALUES (?,?,?,?,?,?,?)",
                      (loop_id, self.store.now(), round_no, event, actor, detail[:1000], dumps(data) if data is not None else None))

    def events(self, loop_id: str, limit: int = 300) -> list[dict]:
        with self.store.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM loop_events WHERE loop_id=? ORDER BY seq DESC LIMIT ?", (loop_id, limit)).fetchall()]
        for r in rows:
            r["data"] = loads(r["data"], None)
        return rows[::-1]


# ============================================================================================================================ controller
class LoopController:
    def __init__(self, svc, *, clock=time.time):
        self.svc = svc
        self.store: CollabStore = svc.store
        self.db = LoopStore(self.store)
        self.clock = clock                  # injectable (tests move time forward); job timing itself is always real
        self._locks: dict[str, asyncio.Lock] = {}
        self._ticker: asyncio.Task | None = None

    # ------------------------------------------------------------------ small helpers
    def _lock(self, loop_id: str) -> asyncio.Lock:
        return self._locks.setdefault(loop_id, asyncio.Lock())

    def now(self) -> float:
        return self.clock()

    def kind(self, name: str) -> LoopKind:
        k = KINDS.get(name)
        if k is None:
            raise CollabError("UNKNOWN_LOOP_KIND", f"no loop kind {name!r}", {"known": sorted(KINDS)}, status=404)
        k.svc = self.svc
        return k

    def _grants(self, caller: str) -> tuple[str, ...]:
        for c in self.svc.callers.list():
            if c["name"] == caller and not c["disabled"]:
                return tuple(c["grants"])
        raise CollabError("CALLER_DISABLED", f"the caller {caller!r} that created this loop is disabled or no longer exists", status=403)

    def own(self, loop_id: str, *, client_id: str | None = None, caller: str | None = None) -> dict:
        lp = self.db.get(loop_id)
        if (client_id and lp["client_id"] != client_id) or (caller and lp["caller"] != caller):
            raise CollabError("NO_SUCH_LOOP", f"no loop {loop_id}", status=404)   # never reveal another client's or caller's loop
        return lp

    def _event(self, lp: dict, event: str, detail: str = "", *, actor: str = "controller", data: Any = None, round_no: int | None = None) -> None:
        self.db.event(lp["loop_id"], event, round_no=lp["round_no"] if round_no is None else round_no, actor=actor, detail=detail, data=data)
        self.store.audit("loop_" + event, actor=actor, task_id=lp.get("task_id") or "", client_id=lp["client_id"], outcome=lp["status"], detail=f"{lp['loop_id']} {detail}"[:300])

    # ------------------------------------------------------------------ creation
    async def create(self, caller: str, client_id: str, kind: str, title: str, config: dict | None, goal: dict | None) -> dict:
        k = self.kind(kind)
        config, goal = dict(config or {}), dict(goal or {})
        grants = self._grants(caller)
        missing = [p for p in k.permissions if p not in grants]
        if missing:
            raise CollabError("PERMISSION_DENIED", f"a {kind} loop needs permissions this caller does not have: {missing}", {"missing": missing}, status=403)
        problems = k.check(self.svc, client_id, config, goal)
        for key in ("max_rounds", "max_requests"):
            if goal.get(key) is not None and (not isinstance(goal[key], int) or isinstance(goal[key], bool) or goal[key] < 1):
                problems.append(f"{key} must be a whole number of at least 1")
        goal["deadline"] = _epoch(goal.get("deadline"))
        if goal["deadline"] and goal["deadline"] <= self.now():
            problems.append("the deadline is already in the past")
        if not (goal.get("max_rounds") or goal.get("deadline") or goal.get("max_requests") or goal.get("baseline_only")):
            problems.append("give the loop a way to end: at least one of max_rounds, deadline, max_requests or baseline_only (a manual stop is always possible too)")
        if problems:
            raise CollabError("BAD_LOOP", "; ".join(problems), {"errors": problems})
        cfg = {**DEFAULTS, **config}
        task = self.store.create_task(f"Loop: {title}"[:200], objective=f"{kind} loop", client_id=client_id, caller=caller)
        now = self.now()
        loop_id = new_id("lp")
        self.db.insert({"loop_id": loop_id, "kind": kind, "title": title[:200], "client_id": client_id, "caller": caller, "task_id": task["task_id"], "config": dumps(cfg), "goal": dumps(goal),
                        "status": "active", "phase": "measuring", "round_no": 0, "attempts": 0, "requests_used": 0, "blockers": "[]", "created_at": now, "updated_at": now})
        self.db.put_round(loop_id, 0, started_at=now)
        lp = self.db.get(loop_id)
        self._event(lp, "created", f"{kind} loop for client {client_id}", actor=caller, data={"goal": goal, "config": cfg})
        return await self.advance(loop_id)

    # ------------------------------------------------------------------ the state machine
    async def advance(self, loop_id: str, *, force: bool = False) -> dict:
        """Do whatever is due for this loop, once, then return its description. Safe to call at any time and any number of times."""
        async with self._lock(loop_id):
            lp = self.db.get(loop_id)
            if lp["status"] in FINAL or lp["status"] == "paused":
                return self.describe(loop_id)
            try:
                self._check_end(lp)
                lp = self.db.get(loop_id)
                if lp["status"] in FINAL:
                    return self.describe(loop_id)
                if lp["status"] == "blocked" and not force:
                    return self.describe(loop_id)
                if lp["status"] == "blocked":
                    lp = self.db.update(loop_id, status="active", blockers=[], attempts=0, retry_at=None)
                    self._event(lp, "unblocked", "resumed by request", actor="controller")
                if lp["phase"] == "measuring":
                    await self._measure_step(lp, force)
                elif lp["phase"] == "waiting" and (force or lp["next_check_at"] is None or lp["next_check_at"] <= self.now()):
                    await self._begin_measurement(self.db.get(loop_id), "verify")
            except CollabError as e:
                lp = self.db.get(loop_id)
                self._block(lp, e.code, e.message)
            return self.describe(loop_id)

    def _check_end(self, lp: dict) -> None:
        g = lp["goal"]
        if g.get("deadline") and self.now() >= g["deadline"]:
            self._finish(lp, "exhausted", "deadline", "the deadline passed before the goal was confirmed")

    def _block(self, lp: dict, code: str, message: str) -> None:
        bl = [b for b in lp["blockers"] if b.get("code") != code] + [{"code": code, "message": message, "since": self.now()}]
        lp = self.db.update(lp["loop_id"], status="blocked", blockers=bl)
        self._event(lp, "blocked", f"{code}: {message}")

    def _finish(self, lp: dict, status: str, outcome: str, summary: str) -> None:
        for rid in ((lp.get("pending") or {}).get("request_ids") or []):   # cancel what is still running: a finished loop leaves nothing behind
            try:
                self.svc.cancel(lp["caller"], rid)
            except CollabError:
                pass
        lp = self.db.update(lp["loop_id"], status=status, phase="done", outcome=outcome, summary=summary, pending=None, next_check_at=None, finished_at=self.now())
        self._event(lp, status, f"{outcome}: {summary}")
        if lp["task_id"]:
            try:
                self.store.finish_task(lp["task_id"], "cancelled" if status == "stopped" else "completed", outcome=outcome, summary=summary, actor="controller")
            except CollabError:
                pass

    # ------------------------------------------------------------------ measuring
    async def _begin_measurement(self, lp: dict, purpose: str) -> None:
        kind = self.kind(lp["kind"])
        reqs = kind.requests(lp, purpose)
        cap = lp["goal"].get("max_requests")
        if cap and lp["requests_used"] + len(reqs) > cap:
            self._finish(lp, "exhausted", "budget", f"the request budget ({cap}) would be exceeded by the next measurement ({len(reqs)} request(s); {lp['requests_used']} used)")
            return
        n = lp["round_no"] + (1 if purpose == "verify" and lp["phase"] == "waiting" and self._round_planned(lp) else 0)
        attempt = lp["attempts"]   # (the idempotency key uses requests_used: unique per measurement, identical when the same one is re-submitted after a crash)
        ids, labels = self._submit(lp, reqs, n, purpose, 1)
        lp = self.db.update(lp["loop_id"], phase="measuring", round_no=n, pending={"purpose": purpose, "round": n, "attempt": attempt, "request_ids": ids, "labels": labels, "stage": 1, "started_at": self.now()},
                            requests_used=lp["requests_used"] + len(ids), retry_at=None, next_check_at=None)
        self.db.put_round(lp["loop_id"], n)
        self._event(lp, "measurement_started", f"{purpose}: {len(ids)} request(s) {', '.join(labels)}", data={"request_ids": ids})

    def _submit(self, lp: dict, reqs: list[dict], n: int, purpose: str, stage: int) -> tuple[list[str], list[str]]:
        grants = self._grants(lp["caller"])
        ids, labels = [], []
        for i, r in enumerate(reqs):
            body = {"task_id": lp["task_id"], "client": lp["client_id"], "capability": r["capability"], "parameters": r.get("parameters") or {}, "priority": 6,
                    "idempotency_key": f"{lp['loop_id']}:r{n}:{purpose}:s{stage}:c{lp['requests_used']}:{i}", "created_at": str(self.now())}
            resp, _ = self.svc.submit(lp["caller"], grants, body)
            ids.append(resp["request_id"])
            labels.append(r.get("label") or r["capability"])
        return ids, labels

    def _round_planned(self, lp: dict) -> bool:
        r = self.db.round(lp["loop_id"], lp["round_no"] + 1)
        return bool(r and r["plan"] is not None)

    async def _measure_step(self, lp: dict, force: bool) -> None:
        pend = lp["pending"]
        if not pend:                       # retry window or first start
            if lp["retry_at"] and lp["retry_at"] > self.now() and not force:
                return
            await self._begin_measurement(lp, "baseline" if lp["round_no"] == 0 else "verify")
            return
        jobs = [self.store.get_job(rid) for rid in pend["request_ids"]]
        if any(j["status"] in ("queued", "running", "awaiting_approval") for j in jobs):
            return
        responses = [self.svc.response(j) for j in jobs]
        kind = self.kind(lp["kind"])
        n, purpose = pend["round"], pend["purpose"]
        stage = pend.get("stage", 1)
        more = kind.followups(lp, purpose, stage, responses)
        if more:                                        # a later stage that needs the earlier answers (e.g. competitors need the search results)
            cap = lp["goal"].get("max_requests")
            if cap and lp["requests_used"] + len(more) > cap:
                self._event(lp, "followup_skipped", f"stage {stage + 1} ({', '.join(r.get('label') or r['capability'] for r in more)}) would exceed the request budget ({cap}): skipped", round_no=n)
            else:
                ids, labels = self._submit(lp, more, n, purpose, stage + 1)
                lp = self.db.update(lp["loop_id"], pending={**pend, "request_ids": pend["request_ids"] + ids, "labels": pend["labels"] + labels, "stage": stage + 1}, requests_used=lp["requests_used"] + len(ids))
                self._event(lp, "followup_started", f"stage {stage + 1}: {', '.join(labels)}", round_no=n, data={"request_ids": ids})
                return
        obs = kind.observations(lp, purpose, responses)
        usable = [o for o in obs if o["status"] in ("found", "not_found")]
        tkeys = {t["key"] for t in kind.targets(lp)}
        if tkeys:   # a goal needs ITS targets measured: supporting data (site audit, first-party totals) alone does not make a measurement usable
            usable = [o for o in usable if o["key"] in tkeys]
        failures = [{"capability": r["capability"], "status": r["status"], "code": (r.get("error") or {}).get("code", ""), "retryable": r.get("retryable", False),
                     "message": (r.get("error") or {}).get("message", "")} for r in responses if r["status"] != "succeeded"]
        if not usable:
            attempts = lp["attempts"] + 1
            why = "; ".join(sorted({f"{f['capability']}: {f['code'] or f['status']}" for f in failures})) or "no measurable result in the responses"
            self.db.add_observations(lp["loop_id"], n, [o for o in obs])   # the unavailable observations are history too
            if attempts >= lp["config"].get("max_attempts", 3):
                lp = self.db.update(lp["loop_id"], attempts=attempts, pending=None)
                self._block(lp, "NO_DATA", f"{attempts} attempt(s) produced no usable measurement ({why}). Fix the cause (e.g. connect the browser) and resume the loop")
                return
            back = lp["config"].get("retry_backoff_s") or DEFAULTS["retry_backoff_s"]
            retry_at = self.now() + back[min(attempts - 1, len(back) - 1)]
            lp = self.db.update(lp["loop_id"], attempts=attempts, pending=None, retry_at=retry_at)
            self._event(lp, "measurement_failed", f"attempt {attempts}: {why}; retry at {retry_at:.0f}", data={"failures": failures})
            return
        self.db.add_observations(lp["loop_id"], n, obs)
        lp = self.db.update(lp["loop_id"], pending=None, attempts=0, retry_at=None)
        unavailable = [o for o in obs if o["status"] == "unavailable"]
        self._event(lp, "measured", f"{len(usable)} usable, {len(unavailable)} unavailable observation(s)", round_no=n,
                    data={"unavailable": [{"key": o["key"], "why": (o.get("limits") or [""])[0]} for o in unavailable], "failures": failures})
        await self._evaluate(lp, n, purpose, responses)

    # ------------------------------------------------------------------ evaluation: compare, judge the goal, decide what comes next
    async def _evaluate(self, lp: dict, n: int, purpose: str, responses: list[dict]) -> None:
        kind = self.kind(lp["kind"])
        allobs = self.db.observations(lp["loop_id"])
        comparison = self.compare(allobs, n)
        verdict = self.judge(lp, kind, allobs)
        round_prev = self.db.round(lp["loop_id"], n)
        effects = self._item_effects(lp, n, comparison)
        sources = kind.sources(lp, responses)
        self.db.put_round(lp["loop_id"], n, comparison=comparison, verdict={**verdict, "item_effects": effects, "sources": sources}, measured_at=self.now())
        lp = self.db.get(lp["loop_id"])
        self._event(lp, "evaluated", verdict["summary"], round_no=n, data={"goal": verdict["status"], "comparison": comparison["summary"], "effects": effects})
        g = lp["goal"]
        if g.get("baseline_only"):
            self._finish(lp, "completed", "baseline_only", "read-only baseline audit complete: nothing was changed or planned. " + verdict["summary"])
            return
        if verdict["status"] == "met_confirmed":
            self._finish(lp, "goal_reached", "goal", verdict["summary"])
            return
        if g.get("max_rounds") and n >= g["max_rounds"]:
            self._finish(lp, "exhausted", "max_rounds", f"{n} round(s) done without a confirmed result. {verdict['summary']}")
            return
        if verdict["status"] == "met_unconfirmed":      # looks good once: do not stop on one observation; check again after the confirmation gap
            gap = lp["config"].get("confirm_gap_hours", DEFAULTS["confirm_gap_hours"]) * HOUR
            lp = self.db.update(lp["loop_id"], phase="waiting", next_check_at=self.now() + gap)
            self.db.put_round(lp["loop_id"], n, notes=(round_prev or {}).get("notes") or "target met once: waiting to confirm with another comparable observation")
            self._event(lp, "confirmation_scheduled", "the target was met in this observation; one more comparable observation is required before calling it reached", data={"at": lp["next_check_at"]})
            return
        lp = self.db.update(lp["loop_id"], phase="awaiting_plan", next_check_at=None)
        self.db.put_round(lp["loop_id"], n + 1)
        self._event(lp, "awaiting_plan", "evidence is ready: the collaborator should analyse it and submit a plan", round_no=n + 1)

    # ------------------------------------------------------------------ comparison (before / after)
    def compare(self, allobs: list[dict], n: int) -> dict:
        """Per key: baseline vs previous vs now. Unavailable data is never a change; position moves of SMALL_CHANGE or less are labelled as possibly ordinary variation."""
        by_key: dict[str, list[dict]] = {}
        for o in allobs:
            by_key.setdefault(o["key"], []).append(o)
        rows = []
        tally = {"improved": 0, "worse": 0, "same": 0, "no_data": 0, "new": 0}
        for key, series in by_key.items():
            cur = [o for o in series if o["round_no"] == n]
            if not cur:
                continue
            cur = cur[-1]
            usable = [o for o in series if o["status"] in ("found", "not_found") and o["comparable"] and o["cmp_key"] == cur["cmp_key"]]
            base = usable[0] if usable and usable[0]["round_no"] == 0 else None
            idx = next((i for i, o in enumerate(series) if o is cur), len(series))
            prev = next((o for o in reversed(series[:idx]) if o in usable), None)
            row = {"key": key, "label": cur["label"], "metric": cur["metric"], "now": self._val(cur), "baseline": self._val(base) if base else None, "previous": self._val(prev) if prev else None,
                   "status_now": cur["status"], "evidence_id": cur["evidence_id"]}
            if cur["status"] == "unavailable":
                row.update(change="no_data", note=(cur["limits"] or ["could not be measured this round"])[0], delta_vs_baseline=None, delta_vs_previous=None)
                tally["no_data"] += 1
            else:
                sign = -1 if cur["metric"] in HIGHER_IS_BETTER else 1          # deltas are always "negative = better"
                dp = self._delta(prev, cur)
                db = self._delta(base, cur) if base is not None and base is not cur else None
                dp = None if dp is None else sign * dp
                db = None if db is None else sign * db
                raw = None if prev is None or prev["status"] != "found" or cur["status"] != "found" else cur["value"] - prev["value"]
                if prev is None:
                    change, note = "new", "first comparable observation"
                elif dp < 0:
                    change, note = "improved", ""
                elif dp > 0:
                    change, note = "worse", ""
                else:
                    change, note = "same", ""
                if change in ("improved", "worse") and cur["metric"] in POSITION_METRICS and abs(dp) <= SMALL_CHANGE and cur["status"] == "found" and prev["status"] == "found":
                    note = f"moved {abs(dp):g} position(s): within ordinary variation, not evidence of an effect"
                elif prev is not None and prev["status"] == "not_found" and cur["status"] == "found":
                    note = "now present (it was absent from the results read before)"
                elif prev is not None and prev["status"] == "found" and cur["status"] == "not_found":
                    note = "no longer present in the results read (it was present before)"
                row.update(change=change, note=note, delta_vs_baseline=db, delta_vs_previous=dp, raw_change_vs_previous=raw)
                tally[change] += 1
            rows.append(row)
        return {"round": n, "rows": rows, "summary": tally}

    @staticmethod
    def _val(o: dict | None) -> dict | None:
        return None if o is None else {"status": o["status"], "value": o["value"], "observed_at": o["observed_at"]}

    @staticmethod
    def _delta(a: dict | None, b: dict | None) -> float | None:
        """Change in rank-like value (negative = better). Absent counts as worse than any position (1000)."""
        if a is None or b is None:
            return None
        av = a["value"] if a["status"] == "found" else 1000.0
        bv = b["value"] if b["status"] == "found" else 1000.0
        return bv - av

    def _item_effects(self, lp: dict, n: int, comparison: dict) -> dict:
        r = self.db.round(lp["loop_id"], n)
        items = ((r or {}).get("plan") or {}).get("items") or []
        if not items:
            return {}
        out = {}
        rows = {x["key"]: x for x in comparison["rows"]}
        for it in items:
            keys = [k for k in (it.get("targets") or []) if k in rows] or list(rows)
            ch = [rows[k]["change"] for k in keys]
            real = [c for c in ch if c in ("improved", "worse", "same")]
            out[it["fingerprint"]] = "improved" if "improved" in ch else "worse" if "worse" in ch else "no_improvement" if real else "unknown"
        return out

    # ------------------------------------------------------------------ the goal
    def judge(self, lp: dict, kind: LoopKind, allobs: list[dict]) -> dict:
        g = lp["goal"]
        targets = kind.targets(lp)
        if not targets:
            return {"status": "no_goal", "summary": "this loop has no measurable target: it ends by rounds, deadline or budget only", "targets": []}
        conf = int(g.get("confirmations") or 2)
        gap = float(lp["config"].get("min_gap_hours", DEFAULTS["min_gap_hours"])) * HOUR
        per = []
        for t in targets:
            series = [o for o in allobs if o["key"] == t["key"]]
            v = kind.evaluate_target(t, series, conf, gap)
            per.append({"key": t["key"], "label": t["label"], **v})
        need_all = (g.get("require") or "all") == "all"
        confirmed = [p for p in per if p["verdict"] == "met_confirmed"]
        once = [p for p in per if p["verdict"] in ("met_confirmed", "met_unconfirmed")]
        if (need_all and len(confirmed) == len(per)) or (not need_all and confirmed):
            status = "met_confirmed"
        elif (need_all and len(once) == len(per)) or (not need_all and once):
            status = "met_unconfirmed"
        elif all(p["verdict"] == "insufficient" for p in per):
            status = "insufficient"
        else:
            status = "not_met"
        bits = [f"{p['label']}: {p['verdict'].replace('_', ' ')}" + (" (volatile)" if p.get("volatile") else "") for p in per]
        return {"status": status, "summary": f"goal {status.replace('_', ' ')}; " + "; ".join(bits), "targets": per, "confirmations_required": conf}

    # ------------------------------------------------------------------ collaborator hand-offs
    def submit_plan(self, loop_id: str, actor: str, *, items: list[dict], summary: str, wait_only: bool = False, recheck_after_hours: float | None = None,
                    override_reason: str = "", client_id: str | None = None) -> dict:
        lp = self.own(loop_id, client_id=client_id, caller=actor)
        if lp["status"] != "active" or lp["phase"] != "awaiting_plan":
            raise CollabError("NOT_AWAITING_PLAN", f"the loop is {lp['status']}/{lp['phase']}: a plan is only accepted while it is awaiting_plan", status=409)
        n = lp["round_no"] + 1
        if wait_only and items:
            raise CollabError("BAD_PLAN", "a wait-only plan has no items")
        if not wait_only and not items:
            raise CollabError("BAD_PLAN", "a plan needs at least one item, or wait_only=true with a reason in the summary (waiting is a legitimate round)")
        history = self.db.rounds(loop_id)
        if wait_only:
            consecutive = 0
            for r in reversed([r for r in history if r["n"] < n and r["plan"] is not None]):
                if r["plan"].get("items"):
                    break
                consecutive += 1
            if consecutive >= lp["config"].get("max_waits", DEFAULTS["max_waits"]):
                raise CollabError("TOO_MANY_WAITS", f"{consecutive} consecutive wait-only round(s) already: change something, or stop the loop", status=409)
        prior: dict[str, dict] = {}
        for r in history:
            if r["n"] >= n or not r["plan"]:
                continue
            eff = (r["verdict"] or {}).get("item_effects") or {}
            for it in r["plan"].get("items") or []:
                if it.get("fingerprint") and r["implemented_at"]:
                    prior[it["fingerprint"]] = {"round": r["n"], "title": it.get("title"), "effect": eff.get(it["fingerprint"], "not yet measured")}
        clean, repeats = [], []
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not isinstance(it.get("title"), str) or len(it["title"].strip()) < 5 or not isinstance(it.get("action_kind"), str) or not it["action_kind"].strip():
                raise CollabError("BAD_PLAN", f"item {i + 1} needs a title (5+ characters) and an action_kind")
            fp = fingerprint(it)
            item = {**{k: it[k] for k in ("title", "action_kind", "pages", "finding_key", "hypothesis", "expected_effect", "targets") if k in it}, "fingerprint": fp}
            if fp in prior:
                repeats.append({"item": it["title"], **prior[fp]})
            clean.append(item)
        if repeats and len(override_reason.strip()) < 20:
            raise CollabError("REPEAT_CHANGE", "this plan repeats a change already implemented in an earlier round (see details): choose something different, or give an override_reason of at least 20 characters explaining "
                                               "why repeating it is justified", {"repeats": repeats}, status=409)
        hrs = recheck_after_hours if recheck_after_hours is not None else lp["config"].get("recheck_after_hours", DEFAULTS["recheck_after_hours"])
        plan = {"items": clean, "summary": summary[:4000], "wait_only": wait_only, "override_reason": override_reason.strip(), "repeats": repeats, "submitted_by": actor, "recheck_after_hours": hrs}
        self.db.put_round(loop_id, n, plan=plan, planned_at=self.now())
        if wait_only:
            lp = self.db.update(loop_id, phase="waiting", next_check_at=self.now() + hrs * HOUR)
            self.db.put_round(loop_id, n, implemented_at=self.now(), changes=[])
        else:
            lp = self.db.update(loop_id, phase="awaiting_implementation")
        self._event(lp, "plan_submitted", f"{len(clean)} item(s)" + (" (wait only)" if wait_only else ""), actor=actor, round_no=n, data={"repeats": repeats} if repeats else None)
        return self.describe(loop_id)

    def report_implemented(self, loop_id: str, actor: str, *, change_ids: list[str], note: str = "", recheck_after_hours: float | None = None, client_id: str | None = None) -> dict:
        lp = self.own(loop_id, client_id=client_id, caller=actor)
        if lp["status"] != "active" or lp["phase"] != "awaiting_implementation":
            raise CollabError("NOT_AWAITING_IMPLEMENTATION", f"the loop is {lp['status']}/{lp['phase']}: changes are only reported while it is awaiting_implementation", status=409)
        n = lp["round_no"] + 1
        if not change_ids:
            raise CollabError("BAD_REQUEST", "report at least one recorded change")
        used = {c for r in self.db.rounds(loop_id) for c in (r["changes"] or [])}
        out = []
        for cid in change_ids:
            ch = self.store.get_artifact(cid)
            if ch["kind"] != "change" or ch["client_id"] != lp["client_id"]:
                raise CollabError("NO_SUCH_CHANGE", f"{cid} is not a recorded change of this loop's client", status=404)
            if cid in used:
                raise CollabError("CHANGE_ALREADY_REPORTED", f"{cid} was already reported in an earlier round", status=409)
            if ch["created_at"] < lp["created_at"]:
                raise CollabError("CHANGE_TOO_OLD", f"{cid} was recorded before this loop started", status=409)
            if ch["status"] not in ("deployed", "verified"):
                raise CollabError("CHANGE_NOT_DEPLOYED", f"{cid} is {ch['status']}: only a DEPLOYED change counts as implemented (a production change is deployed only after the owner's approval)", status=409)
            production = ((ch["body"].get("target") or {}).get("environment") or "production") == "production"
            ap_id = (ch["body"].get("deployment") or {}).get("approval_id")
            if production:
                ap = self.store.get_approval(ap_id) if ap_id else None
                if not ap or ap["status"] not in ("executed", "verified"):
                    raise CollabError("APPROVAL_REQUIRED", f"{cid} targets production and has no executed owner approval: nothing is counted as implemented without it", status=403)
            out.append(cid)
        hrs = recheck_after_hours if recheck_after_hours is not None else (self.db.round(loop_id, n)["plan"] or {}).get("recheck_after_hours") or lp["config"].get("recheck_after_hours", DEFAULTS["recheck_after_hours"])
        self.db.put_round(loop_id, n, changes=out, implemented_at=self.now(), notes=note[:2000])
        lp = self.db.update(loop_id, phase="waiting", next_check_at=self.now() + hrs * HOUR)
        self._event(lp, "implemented", f"{len(out)} change(s) deployed; re-check due in {hrs:g} h (no model or browser is kept running while waiting)", actor=actor, round_no=n,
                    data={"change_ids": out, "next_check_at": lp["next_check_at"]})
        return self.describe(loop_id)

    # ------------------------------------------------------------------ owner / collaborator controls
    async def control(self, loop_id: str, action: str, *, actor: str, reason: str = "", client_id: str | None = None, caller: str | None = None) -> dict:
        lp = self.own(loop_id, client_id=client_id, caller=caller)
        if lp["status"] in FINAL:
            raise CollabError("LOOP_FINISHED", f"the loop is already {lp['status']}", status=409)
        if action == "pause":
            if lp["status"] == "paused":
                return self.describe(loop_id)
            lp = self.db.update(loop_id, status="paused", paused_from=lp["status"])
            self._event(lp, "paused", reason, actor=actor)
        elif action == "resume":
            if lp["status"] not in ("paused", "blocked"):
                return self.describe(loop_id)
            lp = self.db.update(loop_id, status="active", blockers=[], attempts=0, retry_at=None, paused_from=None)
            self._event(lp, "resumed", reason, actor=actor)
            return await self.advance(loop_id)
        elif action == "stop":
            async with self._lock(loop_id):
                self._finish(self.db.get(loop_id), "stopped", "manual", reason or "stopped by request")
            self.db.event(loop_id, "stopped_by", actor=actor, detail=reason)
        elif action == "check_now":
            return await self.advance(loop_id, force=True)
        else:
            raise CollabError("BAD_ACTION", "action is pause, resume, stop or check_now")
        return self.describe(loop_id)

    # ------------------------------------------------------------------ scheduler
    async def tick(self) -> int:
        """Resume everything that is due. Called by the background ticker; also directly (tests, CLI). Returns how many loops were advanced."""
        n = 0
        for lp in self.db.open_loops():
            if lp["status"] != "active":
                continue
            due = (lp["phase"] == "waiting" and (lp["next_check_at"] or 0) <= self.now()) or lp["phase"] == "measuring" or (lp["goal"].get("deadline") and self.now() >= lp["goal"]["deadline"])
            if due:
                await self.advance(lp["loop_id"])
                n += 1
        return n

    async def run_forever(self, every_s: float = 20.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must never die
                log.exception("loop scheduler tick")
            await asyncio.sleep(every_s)

    def start(self, every_s: float = 20.0) -> None:
        if self._ticker is None:
            self._ticker = asyncio.get_running_loop().create_task(self.run_forever(every_s))

    async def stop_ticker(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
            self._ticker = None

    # ------------------------------------------------------------------ views
    def next_action(self, lp: dict) -> dict:
        st, ph = lp["status"], lp["phase"]
        if st in FINAL:
            return {"who": "nobody", "what": "finished", "detail": lp.get("summary") or lp.get("outcome") or ""}
        if st == "paused":
            return {"who": "owner", "what": "resume", "detail": "the loop is paused"}
        if st == "blocked":
            return {"who": "owner_or_collaborator", "what": "unblock", "detail": "; ".join(b["message"] for b in lp["blockers"]) or "blocked"}
        if ph == "measuring":
            pend = lp.get("pending")
            if pend:
                return {"who": "agent", "what": "collecting measurements", "detail": f"{len(pend['request_ids'])} request(s) running"}
            return {"who": "scheduler", "what": "retry measurement", "detail": f"at {lp['retry_at']}" if lp["retry_at"] else "starting"}
        if ph == "awaiting_plan":
            return {"who": "collaborator", "what": "analyse the evidence and call loop.submit_plan", "detail": "see for_planning in loop.get"}
        if ph == "awaiting_implementation":
            return {"who": "collaborator", "what": "implement the plan (handoff -> change -> owner approval -> deploy), then call loop.report_implemented", "detail": "production deploys need the owner's PIN approval"}
        if ph == "waiting":
            return {"who": "scheduler", "what": "re-measure", "detail": f"due at {lp['next_check_at']}"}
        return {"who": "nobody", "what": ph, "detail": ""}

    def describe(self, loop_id: str, *, full: bool = False) -> dict:
        lp = self.db.get(loop_id)
        kind = self.kind(lp["kind"])
        rounds = self.db.rounds(loop_id)
        allobs = self.db.observations(loop_id)
        verdict = self.judge(lp, kind, allobs) if allobs else None
        latest = {}
        for o in allobs:
            latest[o["key"]] = o
        ineffective = []
        for r in rounds:
            eff = (r["verdict"] or {}).get("item_effects") or {}
            for it in (r["plan"] or {}).get("items") or []:
                if r["implemented_at"] and it.get("fingerprint") in eff:
                    ineffective.append({"round": r["n"], "title": it["title"], "action_kind": it["action_kind"], "effect": eff[it["fingerprint"]]})
        out = {"loop_id": lp["loop_id"], "kind": lp["kind"], "title": lp["title"], "client_id": lp["client_id"], "caller": lp["caller"], "task_id": lp["task_id"], "status": lp["status"], "phase": lp["phase"],
               "round": lp["round_no"], "goal": lp["goal"], "config": lp["config"], "next_check_at": lp["next_check_at"], "retry_at": lp["retry_at"], "blockers": lp["blockers"], "requests_used": lp["requests_used"],
               "outcome": lp["outcome"], "summary": lp["summary"], "created_at": lp["created_at"], "updated_at": lp["updated_at"], "finished_at": lp["finished_at"], "next_action": self.next_action(lp),
               "goal_progress": verdict,
               "rounds": [{"n": r["n"], "plan": r["plan"], "changes": r["changes"], "comparison_summary": (r["comparison"] or {}).get("summary"), "goal": (r["verdict"] or {}).get("status"),
                           "item_effects": (r["verdict"] or {}).get("item_effects"), "started_at": r["started_at"], "planned_at": r["planned_at"], "implemented_at": r["implemented_at"],
                           "measured_at": r["measured_at"], "notes": r["notes"]} for r in rounds],
               "latest_observations": [{k: o[k] for k in ("key", "label", "metric", "value", "status", "comparable", "observed_at", "round_no", "evidence_id", "limits")} for o in latest.values()],
               "sources": next((((r["verdict"] or {}).get("sources")) for r in reversed(rounds) if (r["verdict"] or {}).get("sources")), [])}
        notes = kind.cross_checks(lp, latest)
        if notes:
            out["cross_checks"] = notes
        if lp.get("task_id"):
            out["artifacts"] = [{k: a[k] for k in ("artifact_id", "kind", "title", "status", "created_at")} for a in self.store.artifacts(task_id=lp["task_id"], limit=40)]
        if lp["phase"] == "awaiting_plan" or full:
            last = next((r for r in reversed(rounds) if r["comparison"]), None)
            out["for_planning"] = {"comparison": last["comparison"] if last else None, "previous_changes": ineffective, "brief": kind.brief(self.svc, lp),
                                   "rules": "do not repeat a change listed in previous_changes without an override_reason; waiting is allowed (wait_only) but limited; unavailable data is not a result"}
        if full:
            out["events"] = self.db.events(loop_id)
            out["observations"] = allobs
            out["comparisons"] = [{"round": r["n"], **(r["comparison"] or {})} for r in rounds if r["comparison"]]
        return out
