"""Print the timing report for a finished task from the local history DB (latest task by default).

  python -m tests.e2e.task_timing_report [task_id] [--db path/to/tasks.db]

Reads only what the history stores (timing, provider/model, action names, step outcomes already stripped of page text); prints no prompts or page content.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from octg_agent.brain.loop import _canonical_args  # noqa: E402

DB = Path(__file__).resolve().parents[2] / "data" / "tasks.db"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = Path(sys.argv[sys.argv.index("--db") + 1]) if "--db" in sys.argv else DB
    if "--db" in sys.argv:
        args = [a for a in args if a != str(db)]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM tasks WHERE task_id=?", (args[0],)).fetchone() if args else con.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        print("no task found")
        return 1
    tid = row["task_id"]
    calls = [dict(r) for r in con.execute("SELECT * FROM model_calls WHERE task_id=? ORDER BY n", (tid,))]
    steps = [dict(r) for r in con.execute("SELECT * FROM steps WHERE task_id=? ORDER BY n", (tid,))]
    print(f"task {tid}: {row['status']} | {row['duration_s']} s | {row['model_calls']} model calls | {row['browser_actions']} browser actions")
    print("instruction:", row["instruction"])
    print("result:", (row["result"] or row["error"] or "")[:300])
    print("providers:", row["providers"])
    if not calls:
        print("(no per-call timing stored: this task ran before the instrumentation)")
    else:
        print("\nmodel calls (n, provider/model, step, seconds, ok, retries, action):")
        for c in calls:
            print(f"  {c['n']:>2}  {c['provider']}/{c['model']:<24} step {c['step']:<2} {c['duration_ms'] / 1000:6.1f}s  {'ok  ' if c['ok'] else 'FAIL'} {('(' + str(c['error_kind']) + ')') if c['error_kind'] else '':14} retries={c['retries']} {json.loads(c['retry_reasons'] or '[]') or ''}  → {c['action']}")
        by: dict[str, list[int]] = {}
        for c in calls:
            by.setdefault(c["provider"], []).append(c["duration_ms"])
        print("\nper provider (calls, avg s, max s, calls > 5 s):")
        for p, ms in by.items():
            print(f"  {p:10} {len(ms):>2} calls  avg {sum(ms) / len(ms) / 1000:5.1f}s  max {max(ms) / 1000:5.1f}s  over 5s: {sum(1 for x in ms if x > 5000)}")
        slow = [c for c in calls if c["provider"] == "gemini" and c["duration_ms"] > 5000]
        print("unusually slow Gemini calls (>5 s):", [f"call {c['n']} {c['duration_ms'] / 1000:.1f}s" for c in slow] or "none")
        print("FreeLLMAPI calls:", sum(1 for c in calls if c["provider"] == "freellmapi"))
    ft = [s for s in steps if s["tool"] == "find_text"]
    print("\nbrowser steps:")
    prev = None
    repeats = 0
    for s in steps:
        a = json.loads(s["args"])
        sig = (s["tool"], json.dumps(_canonical_args(s["tool"], a), sort_keys=True))
        rep = sig == prev
        repeats += rep
        prev = sig
        print(f"  {s['n']:>2} {s['tool']:12} {s['provider']:8} {'ok  ' if s['ok'] else 'FAIL'} {s['duration_ms']:>5}ms  {json.dumps(a)[:70]}{'   <-- same as previous step' if rep else ''}")
    types = [i for i, s in enumerate(steps) if s["tool"] == "type"]
    submitted = []
    for i in types:
        nxt = next((s for s in steps[i + 1:] if s["tool"] in ("press_key", "click", "navigate", "find_text")), None)
        submitted.append(f"after type (step {steps[i]['n']}): next action = {nxt['tool'] + ' ' + json.dumps(json.loads(nxt['args']))[:60] if nxt else 'none'}")
    print("\nfind_text calls:", len(ft))
    print("what followed typing:", submitted or "no typing step")
    print("consecutive repeated (canonical) steps:", repeats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
