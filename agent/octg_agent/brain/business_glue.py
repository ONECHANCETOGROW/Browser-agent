"""Per-task Business Brain bookkeeping for the task loop (the only place `brain/` meets `business/`).

Holds the read-only reader, the fill ledger (labels and sensitivity, never values), the per-site HIGH approvals and the repeated-wait guard.
The loop calls: `manifest` (what the model may know), `prepare` (model call -> browser call), `decision` (disclosure approval), `record`
(ledger + audit) and `release`.
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..business.resolver import BrainReader, CallPlan, Fill, Manifest
from ..reasoning.router import provider_is_local
from .approval import ApprovalPolicy, Decision
from .observation import Observation, ObservationBuilder, _clip
from .task import TaskState


def origin_of(url: str) -> str:
    try:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}" if u.netloc else (url[:60] or "unknown page")
    except ValueError:
        return "unknown page"


def origin_flags(url: str, el: dict | None) -> list[str]:
    flags: list[str] = []
    try:
        u = urlparse(url)
    except ValueError:
        return ["the page address could not be read"]
    host = (u.hostname or "")
    if u.scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
        flags.append("the page is not HTTPS")
    if "xn--" in host or any(ord(ch) > 127 for ch in host):
        flags.append("the site name uses non-Latin or look-alike characters")
    if el and el.get("frame"):
        flags.append("the field is inside an embedded frame, which may belong to another site")
    return flags


class TaskBusiness:
    def __init__(self, reader: BrainReader, task: TaskState):
        self.reader = reader
        self.t = task
        self.approvals: ApprovalPolicy | None = None
        self.approved: set[tuple[str, str]] = set()   # (origin, field key) the user approved in THIS task
        self._waits: dict[str, int] = {}
        self._fact_ids: dict[str, str] = {}           # key -> fact id used earlier in this task (detects edits during the task)
        task.business = {"active": reader.active, "reason": reader.reason, "fills": [], "client": self.client_info()}

    def client_info(self) -> dict | None:
        r = self.reader
        if not (r.client_id or r.client_pending):
            return None
        return {"id": r.client_id, "name": r.client_name, "pending": r.client_pending, "reason": r.client_reason, "candidates": list(getattr(r, "client_candidates", []) or [])}

    def select_client(self, client_id: str, name: str) -> None:
        self.reader.select_client(client_id, name)
        self.t.business["client"] = self.client_info()
        self.t.business["reason"] = self.reader.reason
        self.t.business["active"] = True

    @property
    def active(self) -> bool:
        return self.reader.active

    def has_fills(self) -> bool:
        return bool(self.t.business and self.t.business["fills"])

    def release(self) -> None:
        self.reader.release()

    # ---- what the model may know
    def manifest(self, obs: Observation, *, allow_values: bool) -> Manifest:
        if not self.active or not obs.supported:
            return Manifest()
        ordered = [e for _, _, e, _ in sorted(obs.cands, key=lambda c: c[1])]
        return self.reader.plan(ordered, allow_values=allow_values)

    def sanitize_observation(self, obs: Observation) -> None:
        """Form-field values that are private never reach a prompt: hide the value of a sensitive-labelled field, a sensitive-shaped value, or one of ours."""
        rebuilt, hidden = [], []
        for score, order, e, line in obs.cands:
            if self.reader.hides_value(e):
                e2 = {**e, "value": "[hidden]"}
                rebuilt.append((score, order, e2, ObservationBuilder._line(e2, obs.url)))
                hidden.append(f'CHANGED {e["role"]} "{_clip(e["name"], 24)}"')
            else:
                rebuilt.append((score, order, e, line))
        obs.cands = rebuilt
        if obs.text:
            obs.text = self.reader.redact(obs.text)
        # "CHANGED field: 'old' -> 'new'" lines quote (clipped) field values: say THAT it changed, never what it now holds
        obs.changes = [(next((h + ": value hidden" for h in hidden if c.startswith(h)), None)) or self.reader.redact(c) for c in obs.changes]

    def redact(self, text: str) -> str:
        return self.reader.redact(text)

    # ---- the model's call -> the browser's call
    def prepare(self, tool: str, args: dict, expect: list[dict], el: dict | None) -> CallPlan:
        plan = self.reader.prepare(tool, args, expect, el)
        if plan.status == "ok":
            for f in plan.fills:
                if f.kind in ("biz", "client"):
                    prev = self._fact_ids.get(f.key)
                    if prev and prev != f.fact_id:
                        self.reader.audit_event("profile_changed_during_task", field_key=f.key, outcome="asked_user")
                        self._fact_ids[f.key] = f.fact_id
                        return CallPlan("wait", message=f"Your Business Profile changed while this task was running ({f.label}). Check the value you want used, then press Resume.", kind="conflict")
                    self._fact_ids[f.key] = f.fact_id
        return plan

    def repeated_wait(self, plan: CallPlan) -> bool:
        k = plan.kind + "|" + plan.message
        self._waits[k] = self._waits.get(k, 0) + 1
        return self._waits[k] >= 2

    def decision(self, fills: list[Fill], obs: Observation, el: dict | None, risk: dict) -> tuple[Decision, str] | None:
        """HIGH-sensitivity disclosure approval (the existing approval gate; remembered per site and field for this task only)."""
        if not fills or self.approvals is None:
            return None
        origin = origin_of(obs.url)
        mentioned = self.reader.mentioned
        return self.approvals.decide_fills(fills, self.approved, origin, origin_flags(obs.url, el), risk, mentioned)

    def grant(self, fills: list[Fill], obs: Observation) -> None:
        origin = origin_of(obs.url)
        for f in fills:
            if f.kind in ("biz", "client") and f.sensitivity == "HIGH":
                self.approved.add((origin, f.key))

    def describe(self, tool: str, fills: list[Fill], el: dict | None, obs: Observation) -> str:
        target = f' "{el.get("name")}"' if el and el.get("name") else ""
        parts = []
        for f in fills:
            if f.kind == "doc":
                parts.append(f"file: {f.label} ({(f.doc or {}).get('name', '?')})")
            else:
                parts.append(f"{f.label} ({f.sensitivity})")
        verb = {"type": "Enter", "select": "Choose", "upload_file": "Upload"}.get(tool, tool)
        who = next((f' for client "{f.who}"' for f in fills if f.kind != "biz" and f.who), "")
        return f"{verb}{target} ← " + "; ".join(parts) + who + f" on {origin_of(obs.url)}"

    def ledger_summary(self) -> str:
        fills = (self.t.business or {}).get("fills") or []
        seen = []
        for f in fills:
            if f["label"] not in seen:
                seen.append(f["label"])
        return ", ".join(seen[:8])

    # ---- after the action
    def record(self, fills: list[Fill], *, step: int, obs: Observation, el: dict | None, ok: bool) -> None:
        origin = origin_of(obs.url)
        label = (el or {}).get("name") or ""
        for f in fills:
            if ok:
                self.t.business["fills"].append({"key": f.key, "sensitivity": f.sensitivity, "origin": origin, "label": f.label, "field": str(label)[:40], "scope": f.scope})
            self.reader.audit_use(f, step=step, origin=origin, label=str(label), outcome="typed" if ok and f.kind == "biz" else "done" if ok else "failed")

    # ---- model routing boundary
    @staticmethod
    def locality(provider) -> str:
        return "local" if provider_is_local(provider) else "cloud"
