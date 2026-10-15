"""The AI -> owner direction: "go here, do this, tell me what you see".

The collaborator posts an OWNER REQUEST (a title, numbered steps, what to report back, optional quick-answer buttons). The owner sees it in the Chrome side panel and in the Hub's Inbox, answers there
(text, a quick answer, optionally the current tab's text and a screenshot, which the owner chooses to attach) and the collaborator reads the answer with `owner.replies`.

Rules: a request is only ever TEXT (never markup); a reply is scrubbed of anything that looks like a secret before it is stored; only the caller that asked can read the answer; an answer is final
(the collaborator posts a follow-up request for the next step); nothing here can approve, deploy or change anything.
"""
from __future__ import annotations

import time

from ..sensitive import scrub_text
from .store import CollabError, CollabStore, dumps, loads, new_id

DDL = """
CREATE TABLE IF NOT EXISTS owner_requests (
  or_id TEXT PRIMARY KEY, client_id TEXT, task_id TEXT, created_by TEXT NOT NULL, created_at REAL NOT NULL, title TEXT NOT NULL, steps TEXT NOT NULL, question TEXT,
  choices TEXT, want_page INTEGER DEFAULT 0, related TEXT, status TEXT NOT NULL, answered_at REAL, reply_text TEXT, reply_choice TEXT, attachments TEXT, answered_via TEXT
);
CREATE INDEX IF NOT EXISTS owner_requests_status ON owner_requests(status, created_at);
"""
STATES = ("open", "answered", "dismissed", "cancelled")
MAX_STEPS, MAX_CHOICES = 12, 6


def _clean(s, n: int) -> str:
    return scrub_text(str(s or "")).strip()[:n]


class OwnerInbox:
    def __init__(self, store: CollabStore):
        self.store = store
        with store.conn() as c:
            c.executescript(DDL)

    # ------------------------------------------------------------------ the collaborator's side
    def create(self, *, caller: str, title: str, steps: list, question: str = "", choices: list | None = None, want_page: bool = False, related: str = "", client_id: str | None = None,
               task_id: str | None = None) -> dict:
        title = _clean(title, 140)
        steps = [_clean(s, 500) for s in (steps or []) if _clean(s, 500)]
        choices = [_clean(c, 40) for c in (choices or []) if _clean(c, 40)]
        if not title:
            raise CollabError("BAD_INPUT", "a request needs a title")
        if not 1 <= len(steps) <= MAX_STEPS:
            raise CollabError("BAD_INPUT", f"give 1-{MAX_STEPS} steps")
        if len(choices) > MAX_CHOICES or len({c.lower() for c in choices}) != len(choices):
            raise CollabError("BAD_INPUT", f"at most {MAX_CHOICES} different quick answers")
        oid = new_id("or")
        with self.store.conn() as c:
            c.execute("INSERT INTO owner_requests (or_id,client_id,task_id,created_by,created_at,title,steps,question,choices,want_page,related,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (oid, client_id, task_id, caller, self.store.now(), title, dumps(steps), _clean(question, 400), dumps(choices), int(bool(want_page)), _clean(related, 120), "open"))
            self.store.audit("owner_request_created", actor=caller, task_id=task_id or "", client_id=client_id or "", outcome="open", detail=f"{oid} {title[:120]}", c=c)
        return self.get(oid)

    def cancel(self, or_id: str, *, caller: str) -> dict:
        r = self.get(or_id)
        if r["created_by"] != caller:
            raise CollabError("NO_SUCH_REQUEST", f"no owner request {or_id}", status=404)
        if r["status"] != "open":
            raise CollabError("NOT_OPEN", f"that request is already {r['status']}", status=409)
        return self._close(or_id, "cancelled", caller)

    # ------------------------------------------------------------------ the owner's side
    def answer(self, or_id: str, *, text: str = "", choice: str = "", attachments: list | None = None, via: str = "hub") -> dict:
        r = self.get(or_id)
        if r["status"] != "open":
            raise CollabError("NOT_OPEN", f"that request is already {r['status']}", status=409)
        text, choice = _clean(text, 6000), _clean(choice, 40)
        if choice and r["choices"] and choice.lower() not in {c.lower() for c in r["choices"]}:
            raise CollabError("BAD_CHOICE", "that is not one of the quick answers", status=400)
        if not text and not choice and not attachments:
            raise CollabError("EMPTY_REPLY", "write what you see, or press one of the quick answers", status=400)
        now = self.store.now()
        with self.store.conn() as c:
            c.execute("UPDATE owner_requests SET status='answered', answered_at=?, reply_text=?, reply_choice=?, attachments=?, answered_via=? WHERE or_id=? AND status='open'",
                      (now, text, choice, dumps(attachments or []), via, or_id))
            self.store.audit("owner_request_answered", actor=f"owner:{via}", task_id=r["task_id"] or "", client_id=r["client_id"] or "", outcome="answered", detail=f"{or_id} {len(text)} chars, {len(attachments or [])} attachment(s)", c=c)
        return self.get(or_id)

    def dismiss(self, or_id: str, *, via: str = "hub") -> dict:
        r = self.get(or_id)
        if r["status"] != "open":
            raise CollabError("NOT_OPEN", f"that request is already {r['status']}", status=409)
        return self._close(or_id, "dismissed", f"owner:{via}")

    def _close(self, or_id: str, status: str, actor: str) -> dict:
        with self.store.conn() as c:
            c.execute("UPDATE owner_requests SET status=?, answered_at=? WHERE or_id=? AND status='open'", (status, self.store.now(), or_id))
            r = c.execute("SELECT task_id, client_id FROM owner_requests WHERE or_id=?", (or_id,)).fetchone()
            self.store.audit("owner_request_" + status, actor=actor, task_id=(r["task_id"] if r else "") or "", client_id=(r["client_id"] if r else "") or "", outcome=status, detail=or_id, c=c)
        return self.get(or_id)

    # ------------------------------------------------------------------ reading
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["steps"], d["choices"], d["attachments"] = loads(d["steps"], []), loads(d["choices"], []), loads(d["attachments"], [])
        d["want_page"] = bool(d["want_page"])
        return d

    def get(self, or_id: str) -> dict:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM owner_requests WHERE or_id=?", (or_id,)).fetchone()
        if not r:
            raise CollabError("NO_SUCH_REQUEST", f"no owner request {or_id}", status=404)
        return self._row(r)

    def list(self, *, status: str | None = None, caller: str | None = None, client_id: str | None = None, since: float | None = None, limit: int = 50) -> list[dict]:
        q, a = "SELECT * FROM owner_requests WHERE 1=1", []
        for col, v in (("status", status), ("created_by", caller), ("client_id", client_id)):
            if v:
                q += f" AND {col}=?"
                a.append(v)
        if since:
            q += " AND (created_at>? OR answered_at>?)"
            a += [since, since]
        with self.store.conn() as c:
            return [self._row(r) for r in c.execute(q + " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, COALESCE(answered_at, created_at) DESC LIMIT ?", (*a, limit)).fetchall()]

    def open_count(self) -> int:
        with self.store.conn() as c:
            return c.execute("SELECT COUNT(*) FROM owner_requests WHERE status='open'").fetchone()[0]

    def expire_old(self, hours: float = 24 * 14) -> int:
        """Requests nobody answered for two weeks are closed as 'dismissed' so the inbox does not collect stale instructions."""
        cut = self.store.now() - hours * 3600
        with self.store.conn() as c:
            return c.execute("UPDATE owner_requests SET status='dismissed', answered_at=? WHERE status='open' AND created_at<?", (self.store.now(), cut)).rowcount
