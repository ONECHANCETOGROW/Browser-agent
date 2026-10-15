"""Small, human-set preferences of the Knowledge Hub that tasks may also read: the DEFAULT SCOPE for client tasks, and the two Business Brain switches that already live in brain.json.

`active_scope` is only ever a default for a task that is ABOUT a client ("fill in the selected client's ...") and whose wording did not name exactly one: instead of pausing to ask, it uses the client
you chose as the default in the Hub. It never changes which scope a task about "my business" uses, and a client named in the wording always wins. It is stored as a plain id (no values).
"""
from __future__ import annotations

import json
from pathlib import Path

ME = "me"


class HubConfig:
    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "hub.json"

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, patch: dict) -> dict:
        d = {**self.load(), **patch}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return d

    def active_scope(self) -> str | None:
        v = self.load().get("active_scope")
        return v if isinstance(v, str) and v else None

    def set_active_scope(self, scope: str | None) -> None:
        self.save({"active_scope": scope or None})


def read_flags(data_dir: Path | str) -> dict:
    """The Business Brain switches in brain.json (the same file `business enable` writes)."""
    p = Path(data_dir) / "brain.json"
    try:
        b = (json.loads(p.read_text(encoding="utf-8")) or {}).get("business") or {}
    except (OSError, ValueError):
        b = {}
    return {"enabled": bool(b.get("enabled", False)), "intent_detection": bool(b.get("intent_detection", True))}


def write_flags(data_dir: Path | str, *, enabled: bool | None = None, intent_detection: bool | None = None) -> dict:
    p = Path(data_dir) / "brain.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        d = {}
    biz = d.setdefault("business", {})
    if enabled is not None:
        biz["enabled"] = bool(enabled)
    if intent_detection is not None:
        biz["intent_detection"] = bool(intent_detection)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return read_flags(data_dir)
