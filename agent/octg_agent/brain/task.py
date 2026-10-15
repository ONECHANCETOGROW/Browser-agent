"""Task state: the single source of truth for a running task. It lives here (and in the history DB), never only in a chat transcript."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..sensitive import looks_sensitive_value, scrub_text


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"  # added to the requested set: pause must be visible in the UI
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
ACTIVE = {TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.WAITING_FOR_USER}


@dataclass
class StepRecord:
    n: int
    tool: str
    args: dict  # compact, secret-free
    reason: str
    ok: bool
    outcome: str  # one short line: what happened
    error_code: str | None = None
    provider: str = ""
    duration_ms: int = 0
    ts: float = field(default_factory=time.time)
    stored_outcome: str | None = None  # what history keeps when `outcome` could contain page content

    def line(self) -> str:
        a = " ".join(f"{k}={v}" for k, v in self.args.items() if k not in ("tab_id",))
        return f"{self.n}. {self.tool}{(' ' + a) if a else ''} -> {'ok' if self.ok else 'FAILED ' + (self.error_code or '')}: {self.outcome}"

    def to_dict(self) -> dict:
        return {"n": self.n, "tool": self.tool, "args": self.args, "reason": self.reason, "ok": self.ok, "outcome": self.outcome,
                "error_code": self.error_code, "provider": self.provider, "duration_ms": self.duration_ms}


@dataclass
class ModelCallRecord:
    """Timing of ONE model call (one provider attempt). Instrumentation only: it never holds a prompt, page text, a reply or any secret."""
    n: int                      # 1-based call number within the task, in the order the attempts were made
    provider: str
    model: str
    step: int                   # browser actions already executed when the call was made (the step this call decided)
    started_at: float           # epoch seconds
    duration_ms: int
    ok: bool
    error_kind: str | None = None          # when it failed: rate_limit / timeout / unavailable / ...
    retries: int = 0                       # extra requests the adapter sent inside this call (e.g. one http_502 retry)
    retry_reasons: list[str] = field(default_factory=list)
    action: str | None = None              # the tool the model chose (browser tool, control tool, "no_tool_call" or "invalid_call")
    fell_back: bool = False                # a different provider answered after an earlier attempt failed

    def to_dict(self) -> dict:
        return {"n": self.n, "provider": self.provider, "model": self.model, "step": self.step, "started_at": round(self.started_at, 3), "duration_ms": self.duration_ms,
                "ok": self.ok, "error_kind": self.error_kind, "retries": self.retries, "retry_reasons": list(self.retry_reasons), "action": self.action, "fell_back": self.fell_back}

    def line(self) -> str:
        """The activity-feed text: provider/model, seconds, outcome, retries, and the action chosen."""
        out = f"call {self.n}: {self.provider}/{self.model} {self.duration_ms / 1000:.1f}s " + ("ok" if self.ok else f"FAILED ({self.error_kind})")
        if self.retries:
            out += f" +{self.retries} retr{'y' if self.retries == 1 else 'ies'} ({','.join(self.retry_reasons)})"
        if self.action:
            out += f" → {self.action}"
        return out


@dataclass
class PendingApproval:
    id: str
    tool: str
    arguments: dict  # secret-free summary
    risk: dict
    description: str
    reason: str
    source: str  # policy | model
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "arguments": self.arguments, "risk": {k: self.risk.get(k) for k in ("level", "tags", "approval", "reasons")},
                "description": self.description, "reason": self.reason, "source": self.source}


@dataclass
class UsageTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_token_calls: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    model: str = ""

    def to_dict(self) -> dict:
        return {"calls": self.calls, "model": self.model, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "calls_without_usage": self.unknown_token_calls, "estimated_cost_usd": round(self.cost_usd, 6) if self.cost_known else None}


class TaskState:
    def __init__(self, instruction: str, *, task_id: str | None = None, goal: str | None = None):
        now = time.time()
        self.task_id = task_id or "task_" + uuid.uuid4().hex[:10]
        self.user_instruction = instruction.strip()
        self.status = TaskStatus.PENDING
        self.goal = goal or self.user_instruction[:240]
        self.current_step = ""  # what the agent is doing now (short operational rationale)
        self.completed_steps: list[StepRecord] = []
        self.failed_steps: list[StepRecord] = []
        self.current_tab: int | None = None
        self.known_tabs: dict[int, dict] = {}
        self.current_url = ""
        self.current_page_title = ""
        self.latest_observation: dict = {}
        self.available_actions: list[str] = []
        self.last_action: dict | None = None
        self.last_action_result: dict | None = None
        self.verification_result: dict | None = None
        self.pending_approval: PendingApproval | None = None
        self.error: str | None = None
        self.result: str | None = None
        self.started_at = now
        self.updated_at = now
        self.finished_at: float | None = None
        # task-scoped working memory (never shared with another task)
        self.notes: list[str] = []
        self.created_tabs: set[int] = set()
        self.activity: list[dict] = []
        self.step_count = 0  # executed browser actions
        self.model_calls = 0
        self.observations = 0
        self.usage: dict[str, UsageTotals] = {}
        self.model_log: list[ModelCallRecord] = []  # per-call timing (instrumentation; no content)
        self.routing: dict = {}
        self.waiting_message: str | None = None
        self.waiting_kind: str | None = None
        self.business: dict | None = None  # Business Brain bookkeeping: {active, reason, fills: [{key, sensitivity, origin, label}]}; metadata only, never a value

    # ---- mutation helpers (all bump updated_at)
    def set_status(self, status: TaskStatus, *, error: str | None = None, message: str | None = None) -> None:
        self.status = status
        if error is not None:
            self.error = scrub_text(error)  # an error message can quote what a page or provider echoed: never an identifier or a private value
        self.waiting_message = message if status in (TaskStatus.WAITING_FOR_USER, TaskStatus.WAITING_FOR_APPROVAL) else None
        if status in TERMINAL and self.finished_at is None:
            self.finished_at = time.time()
        self.touch()

    def touch(self) -> None:
        self.updated_at = time.time()

    def log(self, kind: str, text: str) -> None:
        self.activity.append({"ts": time.time(), "kind": kind, "text": scrub_text(text)[:300]})  # activity is shown in the panel and kept in memory: never a private value
        del self.activity[:-120]
        self.touch()

    def add_note(self, note: str, cap: int = 12) -> None:
        note = " ".join(note.split())[:200]
        if note and note not in self.notes:
            self.notes.append(note)
            del self.notes[:-cap]

    def record_step(self, rec: StepRecord) -> None:
        (self.completed_steps if rec.ok else self.failed_steps).append(rec)
        self.touch()

    def record_model_call(self, rec: ModelCallRecord, cap: int = 300) -> None:
        self.model_log.append(rec)
        del self.model_log[:-cap]
        self.touch()

    def record_usage(self, provider: str, model: str, input_tokens: int | None, output_tokens: int | None, cost: float | None) -> None:
        u = self.usage.setdefault(provider, UsageTotals())
        u.calls += 1
        u.model = model
        if input_tokens is None or output_tokens is None:
            u.unknown_token_calls += 1
        u.input_tokens += input_tokens or 0
        u.output_tokens += output_tokens or 0
        if cost is None:
            u.cost_known = False  # unknown price or unknown token counts: report "unknown", never an invented number
        else:
            u.cost_usd += cost

    # ---- views
    @property
    def recent_steps(self) -> list[StepRecord]:
        return sorted(self.completed_steps + self.failed_steps, key=lambda s: s.n)

    def duration_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 1)

    def usage_dict(self) -> dict:
        by = {p: u.to_dict() for p, u in self.usage.items()}
        known = [u for u in self.usage.values() if u.cost_known]
        return {"model_calls": self.model_calls, "by_provider": by,
                "input_tokens": sum(u.input_tokens for u in self.usage.values()), "output_tokens": sum(u.output_tokens for u in self.usage.values()),
                "estimated_cost_usd": round(sum(u.cost_usd for u in known), 6) if len(known) == len(self.usage) else None}

    def to_summary(self, activity: int = 14) -> dict:
        """Compact view for the side panel / CLI / API. No page text, no field values."""
        return {
            "task_id": self.task_id, "status": self.status.value, "user_instruction": self.user_instruction, "instruction": self.user_instruction, "goal": self.goal,
            "current_step": scrub_text(self.current_step), "step_count": self.step_count, "model_calls": self.model_calls,
            "current_tab": self.current_tab, "current_url": self.current_url, "current_page_title": self.current_page_title,
            "last_action": self.last_action, "last_action_result": self.last_action_result, "verification_result": self.verification_result,
            "pending_approval": self.pending_approval.to_dict() if self.pending_approval else None,
            "waiting_message": scrub_text(self.waiting_message or "") or None, "waiting_kind": self.waiting_kind, "error": self.error, "result": scrub_text(self.result) if self.result else self.result,
            "started_at": self.started_at, "updated_at": self.updated_at, "duration_s": self.duration_s(),
            "usage": self.usage_dict(), "routing": self.routing, "notes": list(self.notes), "activity": self.activity[-activity:],
            "business": ({"active": self.business["active"], "reason": self.business["reason"], "fills": len(self.business["fills"]), "client": self.business.get("client")} if self.business else None),
        }

    def to_dict(self) -> dict:
        d = self.to_summary(activity=120)
        d.update({"known_tabs": {str(k): v for k, v in self.known_tabs.items()}, "available_actions": self.available_actions,
                  "latest_observation": self.latest_observation,
                  "completed_steps": [s.to_dict() for s in self.completed_steps], "failed_steps": [s.to_dict() for s in self.failed_steps],
                  "model_call_timing": [m.to_dict() for m in self.model_log]})
        return d


def brief_args(args: dict[str, Any], *, sensitive: bool = False) -> dict:
    """Secret-free, short rendering of tool arguments for logs, history and prompts."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in ("snapshot_id", "files", "data_b64"):
            continue
        if k == "text":
            s = str(v)
            if sensitive or sum(c.isdigit() for c in s) >= 9 or looks_sensitive_value(s) or scrub_text(s) != s:
                out[k] = "[omitted]"
            else:
                out[k] = s if len(s) <= 60 else s[:57] + "..."
        elif isinstance(v, str):
            v = scrub_text(v)
            out[k] = v if len(v) <= 80 else v[:77] + "..."
        else:
            out[k] = v
    return out
