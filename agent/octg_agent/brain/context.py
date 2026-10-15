"""Context manager: builds the model input for ONE step from the task state, not from an ever-growing transcript.

Each call gets: system prompt + one compact state document (task, progress, notes, last result, verification, observation).
History is trimmed to the last N steps (older ones collapse into a count), and if the prompt is still over budget the observation and
history shrink in that order. Everything is task-scoped: a new task starts with an empty context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..reasoning.provider import Message
from .observation import Observation
from .prompts import SYSTEM_PROMPT
from .settings import BrainSettings
from .task import TaskState


def estimate_tokens(text: str) -> int:
    return int(len(text) / 3.6) + 1


@dataclass
class BuiltContext:
    messages: list[Message]
    tokens: int
    shown_ids: list[str]
    trimmed: list[str]  # what had to be cut to fit the budget


class ContextManager:
    def __init__(self, settings: BrainSettings, tools_token_estimate: int = 0):
        self.s = settings
        self.tools_tokens = tools_token_estimate

    def progress_block(self, task: TaskState, keep: int) -> str:
        steps = task.recent_steps
        older = steps[:-keep] if len(steps) > keep else []
        recent = steps[-keep:]
        lines = []
        if older:
            ok = sum(1 for s in older if s.ok)
            lines.append(f"(earlier: {len(older)} steps, {ok} ok, {len(older) - ok} failed; last of them: {older[-1].line()[:120]})")
        lines += [s.line()[:220] for s in recent]
        return "\n".join(lines) if lines else "(no steps yet)"

    def build(self, task: TaskState, obs: Observation, *, feedback: str = "", image: bytes | None = None, business_block: str = "",
              redact: Callable[[str], str] | None = None) -> BuiltContext:
        trimmed: list[str] = []
        budget_chars = int(max(1500, self.s.prompt_token_budget - self.tools_tokens - estimate_tokens(SYSTEM_PROMPT)) * 3.6)
        keep = self.s.history_steps_in_prompt
        max_el = self.s.obs_max_elements
        obs_chars = 5200

        def compose() -> tuple[str, list[str]]:
            ob = obs.render(obs_chars, max_el)
            parts = [f"TASK: {task.user_instruction}"]
            parts.append(f"STEP: {task.step_count + 1} of at most {self.s.max_steps}")
            if task.notes:
                parts.append("NOTES: " + " | ".join(task.notes[-self.s.max_notes:]))
            parts.append("PROGRESS:\n" + self.progress_block(task, keep))
            if task.last_action_result:
                parts.append("LAST RESULT: " + self._result_line(task))
            if task.verification_result:
                parts.append("VERIFICATION: " + self._verif_line(task))
            if feedback:
                parts.append("FEEDBACK: " + feedback)
            if business_block:
                parts.append(business_block)  # business FIELD KEYS and statuses; never a value
            parts.append("OBSERVATION:\n" + ob)
            parts.append("Choose exactly one tool.")
            return "\n".join(parts), list(obs.shown_ids)

        text, shown = compose()
        # shrink in a fixed order until it fits: elements, history, visible text
        while len(text) > budget_chars:
            if max_el > 12:
                max_el = max(12, int(max_el * 0.75))
                trimmed.append(f"elements→{max_el}")
            elif keep > 3:
                keep -= 2
                trimmed.append(f"history→{keep}")
            elif obs.text:
                obs.text = None
                trimmed.append("text")
            elif obs_chars > 1500:
                obs_chars = int(obs_chars * 0.7)
                trimmed.append("obs-chars")
            else:
                break
            text, shown = compose()
        if redact:
            text = redact(text)  # outbound filter for a task that uses the Business Brain (defence in depth; values are not put in prompts in the first place)
        msgs = [Message("system", SYSTEM_PROMPT), Message("user", text, images=[image] if image else None)]
        return BuiltContext(msgs, estimate_tokens(SYSTEM_PROMPT + text) + self.tools_tokens, shown, trimmed)

    @staticmethod
    def _result_line(task: TaskState) -> str:
        r = task.last_action_result or {}
        la = task.last_action or {}
        head = f"{la.get('tool', '?')}"
        if r.get("ok"):
            return f"{head} ok. {r.get('summary', '')}"[:1700]
        return f"{head} FAILED {r.get('error_code', '')}: {r.get('summary', '')}"[:700]

    @staticmethod
    def _verif_line(task: TaskState) -> str:
        v = task.verification_result or {}
        return v.get("summary", "")[:500]
