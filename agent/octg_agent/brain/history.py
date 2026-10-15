"""Local task history (SQLite). Stores what was asked, how it went, what it cost: never passwords, cookies, tokens, page text or form values."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..sensitive import scrub_text
from .task import TaskState

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, created_at REAL, finished_at REAL, instruction TEXT, status TEXT, providers TEXT,
  model_calls INTEGER, browser_actions INTEGER, duration_s REAL, result TEXT, error TEXT, usage TEXT, routing TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  task_id TEXT, n INTEGER, tool TEXT, args TEXT, reason TEXT, ok INTEGER, outcome TEXT, error_code TEXT, provider TEXT, duration_ms INTEGER, ts REAL,
  PRIMARY KEY (task_id, n)
);
CREATE TABLE IF NOT EXISTS model_calls (
  task_id TEXT, n INTEGER, provider TEXT, model TEXT, step INTEGER, action TEXT, started_at REAL, duration_ms INTEGER, ok INTEGER, error_kind TEXT,
  retries INTEGER, retry_reasons TEXT, fell_back INTEGER,
  PRIMARY KEY (task_id, n)
);
"""


class TaskHistory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, t: TaskState) -> None:
        providers = sorted(t.usage)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.task_id, t.started_at, t.finished_at, scrub_text(t.user_instruction), t.status.value, json.dumps(providers), t.model_calls, t.step_count,
                 t.duration_s(), scrub_text(t.result or "")[:1000], scrub_text(t.error or "")[:500], json.dumps(t.usage_dict()), json.dumps(t.routing)))
            for s in t.recent_steps:
                c.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (t.task_id, s.n, s.tool, scrub_text(json.dumps(s.args)), scrub_text(s.reason), int(s.ok), scrub_text(s.stored_outcome or s.outcome), s.error_code, s.provider, s.duration_ms, s.ts))
            for m in t.model_log:  # timing only: provider, model, step, action name, seconds, outcome, retries; no prompts, replies or page text
                c.execute("INSERT OR REPLACE INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (t.task_id, m.n, m.provider, m.model, m.step, m.action, m.started_at, m.duration_ms, int(m.ok), m.error_kind, m.retries, json.dumps(m.retry_reasons), int(m.fell_back)))

    def list(self, limit: int = 20) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, task_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not r:
                return None
            steps = [dict(s) for s in c.execute("SELECT * FROM steps WHERE task_id=? ORDER BY n", (task_id,)).fetchall()]
            calls = [dict(m) for m in c.execute("SELECT * FROM model_calls WHERE task_id=? ORDER BY n", (task_id,)).fetchall()]
        d = self._row(r)
        for s in steps:
            s["args"] = json.loads(s["args"])
            s["ok"] = bool(s["ok"])
        for m in calls:
            m["ok"], m["fell_back"] = bool(m["ok"]), bool(m["fell_back"])
            m["retry_reasons"] = json.loads(m["retry_reasons"] or "[]")
        d["steps"] = steps
        d["model_call_timing"] = calls
        return d

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("providers", "usage", "routing"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def usage_report(self) -> dict:
        """Where did model calls go? Lets you check whether LOCAL_FIRST actually reduces strong-provider usage."""
        totals: dict[str, dict] = {}
        for t in self.list(1000):
            for prov, u in ((t.get("usage") or {}).get("by_provider") or {}).items():
                a = totals.setdefault(prov, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0})
                a["calls"] += u["calls"]
                a["input_tokens"] += u["input_tokens"]
                a["output_tokens"] += u["output_tokens"]
                a["estimated_cost_usd"] += u.get("estimated_cost_usd") or 0.0
        # how long each provider's calls take (from the per-call timing): lets slow calls be seen in real data before anything is tuned
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT provider, duration_ms, ok, retries FROM model_calls").fetchall()
        by: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by.setdefault(r["provider"], []).append(r)
        for prov, rs in by.items():
            ms = sorted(r["duration_ms"] for r in rs)
            a = totals.setdefault(prov, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0})
            a["latency"] = {"timed_calls": len(ms), "avg_ms": int(sum(ms) / len(ms)), "median_ms": ms[len(ms) // 2], "max_ms": ms[-1],
                            "calls_over_5s": sum(1 for x in ms if x > 5000), "failed": sum(1 for r in rs if not r["ok"]), "retried": sum(1 for r in rs if r["retries"])}
        return totals
