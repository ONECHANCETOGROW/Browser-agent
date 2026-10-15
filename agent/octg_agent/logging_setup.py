"""Structured local logs: one JSON object per line in data/logs/agent-YYYY-MM-DD.jsonl."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sensitive import scrub_text

REDACT_KEYS = {"data_b64", "data_url", "data_base64", "password", "token", "pairing_token", "api_token"}
MAX_STR = 500
MAX_LIST = 25


def shrink(obj: Any, depth: int = 0) -> Any:
    """Make a value log-safe: redact secrets and payloads, truncate long strings and lists."""
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        return {k: ("[redacted]" if k in REDACT_KEYS else shrink(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        items = [shrink(v, depth + 1) for v in list(obj)[:MAX_LIST]]
        if len(obj) > MAX_LIST:
            items.append(f"… +{len(obj) - MAX_LIST} more")
        return items
    if isinstance(obj, str):
        obj = scrub_text(obj)  # registered exact values and PAN/GSTIN/IFSC/card/ID-shaped strings never reach the log in plaintext
        if len(obj) > MAX_STR:
            return obj[:MAX_STR] + f"…[{len(obj)} chars]"
    return obj


class ActionLog:
    """Append-only JSONL log of every action: timestamp, request_id, action, parameters, result, duration, errors."""

    def __init__(self, logs_dir: Path):
        self.dir = Path(logs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(timezone.utc)
        return self.dir / f"agent-{when:%Y-%m-%d}.jsonl"

    def write(self, **entry: Any) -> dict:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **shrink(entry)}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock, self.path_for().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return entry

    def tail(self, n: int = 50) -> list[dict]:
        p = self.path_for()
        if not p.is_file():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]


def setup_console(level: str = "INFO") -> logging.Logger:
    log = logging.getLogger("octg")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        log.addHandler(h)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False
    return log
