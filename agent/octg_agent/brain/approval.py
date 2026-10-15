"""Approval gate. Uses the registry's risk tags (plus the dynamic escalation in engine.risk) to decide, BEFORE an action runs, whether it
runs automatically, needs the user's approval, or is refused outright.

  low / none          → automatic
  medium              → automatic unless settings.approve_medium
  high or approval=required → always asks
  typing into a sensitive field → never automatic and never through a model (the user types credentials themselves)
  closing a tab this task did not open → asks
  Business Brain: a HIGH-sensitivity business value is entered only after the user approved it for THIS site in THIS task; a form-submitting action
  after business details were entered always asks. Same Decision / PendingApproval / approval card as everything else.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..engine.registry import ACTIONS
from ..engine.state import BrowserState
from .settings import BrainSettings
from .task import PendingApproval, TaskState, brief_args


SUBMIT_TAGS = {"form_submit", "may_submit_form", "potentially_irreversible"}


@dataclass
class Decision:
    action: str  # allow | ask | block
    why: str = ""
    risk: dict | None = None


class ApprovalPolicy:
    def __init__(self, settings: BrainSettings, state: BrowserState):
        self.s = settings
        self.state = state

    def element_brief(self, params: dict) -> dict | None:
        if not params.get("element_id"):
            return None
        return self.state.element(params.get("tab_id"), params.get("snapshot_id"), params["element_id"])

    def decide(self, tool: str, params: dict, risk: dict, task: TaskState) -> Decision:
        el = self.element_brief(params)
        if self.s.block_sensitive_typing and tool in ("type", "clear") and ((el or {}).get("sensitive") or (el or {}).get("type") == "password"):
            return Decision("block", "the target is a password or other sensitive field; the user must enter it themselves", risk)
        if tool == "close_tab" and params.get("tab_id") not in task.created_tabs:
            return Decision("ask", "closing a tab this task did not open", risk)
        if risk["approval"] == "required" or risk["level"] == "high":
            return Decision("ask", "; ".join(risk.get("reasons") or []) or f"{tool} is a high-risk action", risk)
        if (task.business or {}).get("fills") and SUBMIT_TAGS & set(risk.get("tags") or []):
            return Decision("ask", "business details were entered on this page and this action may submit them", risk)
        if risk["level"] == "medium" and self.s.approve_medium:
            return Decision("ask", "medium-risk action and approve_medium is on", risk)
        return Decision("allow", "", risk)

    def describe(self, tool: str, params: dict, risk: dict) -> str:
        el = self.element_brief(params)
        target = f" \"{el.get('name')}\" ({el.get('role') or el.get('tag')})" if el and el.get("name") else (f" {params['element_id']}" if params.get("element_id") else "")
        extra = ""
        if tool == "navigate":
            extra = f" to {params.get('url')}"
        elif tool == "type":
            extra = f" the text {brief_args({'text': params.get('text', '')}).get('text')!r}"
        elif tool == "upload_file":
            extra = f" file {params.get('local_file_path') or params.get('local_file_paths')}"
        elif tool == "close_tab":
            extra = f" #{params.get('tab_id')}"
        return f"{tool}{target}{extra}".strip()

    def make_pending(self, tool: str, params: dict, risk: dict, reason: str, why: str, source: str = "policy") -> PendingApproval:
        return PendingApproval(id="appr_" + uuid.uuid4().hex[:8], tool=tool, arguments=brief_args(params),
                               risk={**risk, "reasons": [why] if why else risk.get("reasons", [])},
                               description=self.describe(tool, params, risk), reason=reason, source=source)

    # ---- Business Brain disclosure (the fills carry labels and sensitivity, never values)
    def decide_fills(self, fills: list, approved: set, origin: str, flags: list[str], risk_base: dict, mentioned: set[str]) -> tuple[Decision, str] | None:
        """HIGH-sensitivity business values need the user's approval once per (task, site, field). Returns None when nothing needs asking."""
        need = [f for f in fills if f.kind in ("biz", "client") and f.sensitivity == "HIGH" and (origin, f.key) not in approved]
        if not need:
            return None
        names = ", ".join(f"{f.label}" for f in need)
        extra = [f"{f.label} was not mentioned in your task" for f in need if f.key.split("[")[0] not in mentioned and f.key not in mentioned]
        warn = flags + extra
        who = f' of client "{need[0].who}"' if need[0].kind == "client" and need[0].who else ""
        text = f"Enter HIGH-sensitivity business data ({names}){who} on {origin}" + (" — WARNING: " + "; ".join(warn) if warn else "")
        risk = {**risk_base, "level": "high", "tags": list({*risk_base.get("tags", []), "business_data_disclosure"}), "approval": "required", "reasons": [text]}
        return Decision("ask", text, risk), text
