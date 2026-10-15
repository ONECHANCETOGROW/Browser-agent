"""Research OBSERVATIONS: the storage foundation for the future SEO Intelligence phase. NO research engine exists yet.

The one rule this module enforces: research output is an OBSERVATION, never a permanent fact. Every observation records what was searched
(query, location, search engine), what was seen (result type, position, URL/business) and WHEN, inside a research session for exactly one client. Observations are
append-only (SQLite triggers refuse UPDATE and DELETE), so a later session can be compared with an earlier one instead of overwriting it.

`describe()` is the only sanctioned way to turn an observation into a sentence, and it always says "observed ... during this research session":
it can never produce "X is #1".
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from ..sensitive import scrub_text
from .store import BusinessStore, StoreError

OBS_KINDS = ("keyword_rank", "serp_result", "local_pack_result", "competitor", "citation", "review", "technical", "content", "search_performance", "other")
RANKED_KINDS = ("keyword_rank", "serp_result", "local_pack_result")
SESSION_STATUS = ("open", "finished", "abandoned")


def _clip(v, n) -> str:
    return scrub_text(str(v or ""))[:n]


class ResearchLog:
    def __init__(self, store: BusinessStore):
        self.store = store

    # ---------------------------------------------------------------- sessions
    def start_session(self, client_id: str, objective: str, *, query: str = "", location: str = "", search_engine: str = "", actor: str = "cli") -> dict:
        if not client_id or client_id == "me":
            raise StoreError("NO_CLIENT", "a research session belongs to exactly one client")
        with self.store.conn() as c:
            self.store._check_scope(c, client_id, writing=True)
            sid = "rs_" + uuid.uuid4().hex[:10]
            c.execute("INSERT INTO research_sessions (session_id,client_id,objective,query,location,search_engine,started_at,status) VALUES (?,?,?,?,?,?,?,?)",
                      (sid, client_id, _clip(objective, 300), _clip(query, 200), _clip(location, 120), _clip(search_engine, 40), time.time(), "open"))
        self.store.audit("research_session_started", scope=client_id, actor=actor, outcome="open", detail=sid)
        return self.get_session(sid)

    def finish_session(self, session_id: str, *, findings: str = "", recommendations: str = "", status: str = "finished", actor: str = "cli") -> dict:
        if status not in SESSION_STATUS:
            raise StoreError("BAD_STATUS", f"status must be one of {SESSION_STATUS}")
        s = self.get_session(session_id)
        with self.store.conn() as c:
            c.execute("UPDATE research_sessions SET status=?, finished_at=?, findings=?, recommendations=? WHERE session_id=?",
                      (status, time.time(), _clip(findings, 4000), _clip(recommendations, 4000), session_id))
        self.store.audit("research_session_finished", scope=s["client_id"], actor=actor, outcome=status, detail=session_id)
        return self.get_session(session_id)

    def invalidate_session(self, session_id: str, reason: str, *, actor: str = "cli") -> dict:
        """Observations stay (append-only history); the SESSION is marked abandoned with the reason, so nothing reads its observations as valid."""
        s = self.get_session(session_id)
        with self.store.conn() as c:
            c.execute("UPDATE research_sessions SET status='abandoned', findings=? WHERE session_id=?", (_clip("INVALID: " + reason + " | " + (s.get("findings") or ""), 4000), session_id))
        self.store.audit("research_session_invalidated", scope=s["client_id"], actor=actor, outcome="abandoned", detail=session_id)
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM research_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not r:
            raise StoreError("NO_SUCH_SESSION", f"no research session {session_id}")
        return dict(r)

    def sessions(self, client_id: str, limit: int = 50) -> list[dict]:
        with self.store.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM research_sessions WHERE client_id=? ORDER BY started_at DESC LIMIT ?", (client_id, limit)).fetchall()]

    # ---------------------------------------------------------------- observations (append-only)
    def add_observation(self, session_id: str, kind: str, *, query: str = "", location: str = "", search_engine: str = "", result_type: str = "", position: int | None = None,
                        url: str = "", domain: str = "", business: str = "", evidence: str = "", source: str = "", data: dict | None = None, observed_at: float | None = None,
                        actor: str = "cli") -> dict:
        if kind not in OBS_KINDS:
            raise StoreError("BAD_KIND", f"observation kind must be one of {OBS_KINDS}")
        sess = self.get_session(session_id)
        if sess["status"] != "open":
            raise StoreError("SESSION_CLOSED", "that research session is closed: start a new one so history stays separate")
        if kind in RANKED_KINDS and not (query and location and search_engine):
            raise StoreError("INCOMPLETE_OBSERVATION", "a ranking observation needs the query, the location and the search engine it was observed with")
        if position is not None and (not isinstance(position, int) or position < 1):
            raise StoreError("BAD_POSITION", "position is a whole number from 1")
        with self.store.conn() as c:
            self.store._check_scope(c, sess["client_id"], writing=True)
            oid = "ob_" + uuid.uuid4().hex[:10]
            c.execute("INSERT INTO research_observations (obs_id,session_id,client_id,kind,query,location,search_engine,result_type,position,url,domain,business,observed_at,evidence,source,data)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (oid, session_id, sess["client_id"], kind, _clip(query, 200), _clip(location, 120), _clip(search_engine, 40), _clip(result_type, 40), position, _clip(url, 500),
                       _clip(domain, 200), _clip(business, 200), float(observed_at if observed_at is not None else time.time()), _clip(evidence, 1000), _clip(source, 200),
                       scrub_text(json.dumps(data or {}, ensure_ascii=False))[:4000]))
        self.store.audit("research_observation", scope=sess["client_id"], actor=actor, outcome=kind, detail=f"{session_id} {oid}")
        return self.get_observation(oid)

    def get_observation(self, obs_id: str) -> dict:
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM research_observations WHERE obs_id=?", (obs_id,)).fetchone()
        if not r:
            raise StoreError("NO_SUCH_OBSERVATION", f"no observation {obs_id}")
        return dict(r)

    def observations(self, client_id: str, *, kind: str | None = None, query: str | None = None, location: str | None = None, session_id: str | None = None,
                     since: float | None = None, limit: int = 200) -> list[dict]:
        q, args = "SELECT * FROM research_observations WHERE client_id=?", [client_id]
        for col, val in (("kind", kind), ("query", query), ("location", location), ("session_id", session_id)):
            if val:
                q += f" AND lower({col})=lower(?)"
                args.append(val)
        if since is not None:
            q += " AND observed_at>=?"
            args.append(since)
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY observed_at DESC LIMIT ?", (*args, limit)).fetchall()]

    def history(self, client_id: str, query: str, location: str, who: str) -> list[dict]:
        """Every observation of one business/domain for one query+location, oldest first: the basis for later ranking comparisons."""
        who_l = who.lower()
        rows = [o for o in self.observations(client_id, query=query, location=location, limit=1000) if who_l in (o["domain"] or "").lower() or who_l in (o["business"] or "").lower()]
        return sorted(rows, key=lambda o: o["observed_at"])

    def summary(self, client_id: str) -> dict:
        with self.store.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM research_observations WHERE client_id=?", (client_id,)).fetchone()[0]
            s = c.execute("SELECT COUNT(*) FROM research_sessions WHERE client_id=?", (client_id,)).fetchone()[0]
        return {"sessions": s, "observations": n}

    # ---------------------------------------------------------------- wording
    @staticmethod
    def describe(o: dict) -> str:
        """The only sanctioned sentence: an observation, with its context, never a standing claim."""
        when = datetime.fromtimestamp(o["observed_at"]).strftime("%Y-%m-%d %H:%M")
        subject = o.get("business") or o.get("domain") or o.get("url") or "a result"
        ctx = " ".join(x for x in (
            f"for the query '{o['query']}'" if o.get("query") else "",
            f"in location '{o['location']}'" if o.get("location") else "",
            f"on {o['search_engine']}" if o.get("search_engine") else "") if x)
        pos = f" at position #{o['position']}" if o.get("position") else ""
        kind = f" as a {o['result_type']} result" if o.get("result_type") else ""
        return f"{subject} was observed{pos}{kind} {ctx} during research session {o['session_id']} at {when}.".replace("  ", " ")
