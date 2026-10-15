"""The agent's live model of the browser, kept current by extension events and refreshed by authoritative calls."""
from __future__ import annotations

import time
from collections import deque
from typing import Any


class BrowserState:
    def __init__(self, max_events: int = 500):
        self.connected = False
        self.connected_at: float | None = None
        self.reconnects = 0
        self.extension: dict = {}
        self.tabs: dict[int, dict] = {}
        self.focused_window: int | None = None
        self.snapshots: dict[int, dict] = {}  # tab_id -> {snapshot_id, url, elements: {id: brief}}
        self.downloads: dict[int, dict] = {}
        self.events: deque[dict] = deque(maxlen=max_events)
        self._seq = 0
        self.task: str | None = None
        self.current_action: str | None = None
        self.started_at = time.time()

    # ---- connection
    def on_connect(self, hello: dict) -> None:
        if self.connected_at is not None:
            self.reconnects += 1
        self.connected = True
        self.connected_at = time.time()
        self.extension = {k: hello.get(k) for k in ("extension_id", "version", "protocol", "user_agent")}
        self.record("agent_extension_connected", {"extension": self.extension})

    def on_disconnect(self) -> None:
        if self.connected:
            self.record("agent_extension_disconnected", {})
        self.connected = False

    # ---- events
    def record(self, event: str, data: Any, ext_seq: int | None = None, ts: float | None = None) -> dict:
        self._seq += 1
        entry = {"seq": self._seq, "ts": ts or int(time.time() * 1000), "event": event, "data": data}
        if ext_seq is not None:
            entry["ext_seq"] = ext_seq
        self.events.append(entry)
        return entry

    def events_since(self, since: int = 0, limit: int = 100) -> list[dict]:
        return [e for e in self.events if e["seq"] > since][-limit:]

    def apply_event(self, msg: dict) -> None:
        """Apply one {"type":"event"} message from the extension."""
        name, data = msg.get("event", ""), msg.get("data") or {}
        if name in ("tab_created", "tab_updated") and data.get("tab"):
            self.tabs[data["tab"]["tab_id"]] = data["tab"]
        elif name == "tab_removed":
            self.tabs.pop(data.get("tab_id"), None)
            self.snapshots.pop(data.get("tab_id"), None)
        elif name == "tab_activated":
            wid, tid = data.get("window_id"), data.get("tab_id")
            for t in self.tabs.values():
                if t["window_id"] == wid:
                    t["active"] = t["tab_id"] == tid
        elif name == "tab_replaced":
            old = self.tabs.pop(data.get("removed_tab_id"), None)
            self.snapshots.pop(data.get("removed_tab_id"), None)
            if old:
                old["tab_id"] = data.get("added_tab_id")
                self.tabs[old["tab_id"]] = old
        elif name == "window_focus":
            self.focused_window = None if data.get("window_id") in (None, -1) else data["window_id"]
        elif name == "navigation_committed":
            snap = self.snapshots.get(data.get("tab_id"))
            if snap:
                snap["stale"] = True
        elif name in ("download_created", "download_changed"):
            d = self.downloads.setdefault(data["download_id"], {"download_id": data["download_id"]})
            d.update({k: v for k, v in data.items() if v is not None})
        self.record(name, data, ext_seq=msg.get("seq"), ts=msg.get("ts"))

    def replace_tabs(self, tabs: list[dict]) -> None:
        self.tabs = {t["tab_id"]: t for t in tabs}
        for gone in set(self.snapshots) - set(self.tabs):
            self.snapshots.pop(gone, None)

    # ---- snapshots
    def record_snapshot(self, result: dict) -> None:
        briefs = {
            e["id"]: {k: e.get(k) for k in ("tag", "role", "name", "type", "href", "disabled", "submits_form", "sensitive") if e.get(k) is not None}
            for e in result.get("elements", [])
        }
        self.snapshots[result["tab_id"]] = {"snapshot_id": result["snapshot_id"], "url": result.get("url"), "elements": briefs, "stale": False}

    def element(self, tab_id: int | None, snapshot_id: str | None, element_id: str) -> dict | None:
        snap = self.snapshots.get(tab_id) if tab_id is not None else None
        if snap and snap["snapshot_id"] == snapshot_id:
            return snap["elements"].get(element_id)
        if tab_id is None:  # unknown target tab: search every snapshot for the matching id
            for s in self.snapshots.values():
                if s["snapshot_id"] == snapshot_id:
                    return s["elements"].get(element_id)
        return None

    def active_tab_id(self) -> int | None:
        cands = [t for t in self.tabs.values() if t.get("active")]
        if self.focused_window is not None:
            focused = [t for t in cands if t["window_id"] == self.focused_window]
            if focused:
                return focused[0]["tab_id"]
        return cands[0]["tab_id"] if cands else None

    def summary(self) -> dict:
        return {
            "connected": self.connected,
            "connected_since": self.connected_at,
            "reconnects": self.reconnects,
            "extension": self.extension,
            "tab_count": len(self.tabs),
            "active_tab_id": self.active_tab_id(),
            "tabs": list(self.tabs.values()),
            "latest_snapshots": {str(t): {"snapshot_id": s["snapshot_id"], "url": s["url"], "stale": s["stale"]} for t, s in self.snapshots.items()},
            "task": self.task,
            "current_action": self.current_action,
            "event_seq": self._seq,
            "uptime_s": round(time.time() - self.started_at, 1),
        }
