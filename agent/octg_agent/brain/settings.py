"""Orchestration settings: limits, approval policy, observation budgets, router and provider configuration.

Loaded from <data dir>/brain.json (optional; see brain.example.json) with environment overrides for the few things you tweak often.
Secrets (API keys) are NOT here; they live in the KeyStore.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class BrainSettings:
    # limits (the loop can never run forever)
    max_steps: int = 40  # executed browser actions per task
    max_model_calls: int = 100
    max_seconds: float = 900.0
    action_timeout_s: float = 90.0
    max_tool_errors: int = 4  # consecutive unusable model replies before the task fails
    max_repeat_actions: int = 4  # the same action with no effect this many times in a row -> ask the user instead of burning steps
    max_stale_retries: int = 3  # consecutive stale/not-found results before asking the user
    reconnect_wait_s: float = 60.0
    # approval policy: risk.level high or approval=required always asks; medium asks only when approve_medium
    approve_medium: bool = False
    block_sensitive_typing: bool = True  # credentials never pass through a model
    # observation / context budgets
    obs_max_elements: int = 45
    inspect_max_elements: int = 250
    obs_text_chars: int = 500
    read_result_chars: int = 1500
    prompt_token_budget: int = 6000
    history_steps_in_prompt: int = 8
    max_notes: int = 12
    # model layer
    router: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)
    # Business Brain (Phase 3): OFF by default. See business/service.py BusinessConfig for the keys (enabled, intent_detection, ...).
    business: dict = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path | str, path: Path | str | None = None) -> "BrainSettings":
        s = cls()
        p = Path(path) if path else Path(data_dir) / "brain.json"
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in raw.items():
                if k in known:
                    setattr(s, k, v)
        env = os.environ
        if env.get("OCTG_MAX_STEPS"):
            s.max_steps = int(env["OCTG_MAX_STEPS"])
        if env.get("OCTG_LOCAL_MODEL"):
            s.providers.setdefault("local", {})["model"] = env["OCTG_LOCAL_MODEL"]
        if env.get("OCTG_GEMINI_MODEL"):
            s.providers.setdefault("gemini", {})["model"] = env["OCTG_GEMINI_MODEL"]
        if env.get("OCTG_ROUTER_POLICY"):
            s.router["policy"] = env["OCTG_ROUTER_POLICY"]
        if env.get("OCTG_BUSINESS"):
            s.business = {**s.business, "enabled": env["OCTG_BUSINESS"].lower() in ("1", "true", "on", "yes")}
        if env.get("FREELLMAPI_BASE_URL"):
            s.providers.setdefault("freellmapi", {})["url"] = env["FREELLMAPI_BASE_URL"]
        return s
