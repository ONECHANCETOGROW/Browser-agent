"""Model router: decides which provider answers each step, and falls back when one fails.

Policies
  LOCAL_FIRST   cheap local model for simple steps; stronger providers (strong_order) for complex ones; local is the last resort for those.
  GEMINI_FIRST  gemini -> freellmapi -> local
  FALLBACK      fixed order: local -> gemini -> freellmapi (config `order`)
  MANUAL        exactly one named provider, no fallback

"Complex" is a score over the RouteContext (see `complexity`). Weights and the threshold are configuration, not code.
A provider that fails goes on cooldown; repeated failures escalate to a stronger provider and stay escalated for `sticky_steps` steps.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from .provider import BaseProvider, Message, ModelResponse, ProviderError


def provider_is_local(p: BaseProvider) -> bool:
    """True only for a provider that runs on this machine: it declares itself local AND, where it has an address, that address is loopback.
    (FreeLLMAPI declares itself non-local because it forwards to cloud services, so it never qualifies.)"""
    if not p.capabilities.local:
        return False
    host = urlparse(str(getattr(p, "base_url", "") or "")).hostname
    return host is None or host in ("127.0.0.1", "localhost", "::1")


class Policy(str, Enum):
    LOCAL_FIRST = "LOCAL_FIRST"
    GEMINI_FIRST = "GEMINI_FIRST"
    FALLBACK = "FALLBACK"
    MANUAL = "MANUAL"


@dataclass
class RouterConfig:
    policy: Policy = Policy.LOCAL_FIRST
    manual_provider: str | None = None
    order: list[str] = field(default_factory=lambda: ["local", "gemini", "freellmapi"])  # FALLBACK order
    strong_order: list[str] = field(default_factory=lambda: ["gemini", "freellmapi"])  # LOCAL_FIRST: who handles complex steps
    complexity_threshold: float = 2.0  # score >= threshold -> complex
    weights: dict[str, float] = field(default_factory=lambda: {
        "first_step": 1.0, "failure": 1.5, "tool_error": 1.0, "stale_recovery": 1.0, "many_elements": 1.0, "text_heavy": 1.0,
        "vision": 3.0, "signal": 1.5, "long_instruction": 1.0, "multi_tab": 0.5})
    many_elements: int = 70
    long_instruction_words: int = 60
    escalate_after_failures: int = 2  # failures on the chosen provider before we stop using it for this call
    sticky_steps: int = 3  # after an escalation, keep using the stronger provider for this many steps

    @classmethod
    def from_dict(cls, d: dict | None) -> "RouterConfig":
        c = cls()
        for k, v in (d or {}).items():
            if k == "policy":
                c.policy = Policy(str(v).upper())
            elif k == "weights":
                c.weights = {**c.weights, **v}
            elif hasattr(c, k):
                setattr(c, k, v)
        return c


@dataclass
class RouteContext:
    step: int = 0
    first_step: bool = False
    consecutive_failures: int = 0  # failed browser actions in a row
    tool_errors: int = 0  # invalid/unusable model replies for this step
    stale_recovery: bool = False
    element_count: int = 0
    text_heavy: bool = False
    needs_vision: bool = False
    signals: list[str] = field(default_factory=list)
    instruction_words: int = 0
    tab_count: int = 1
    force_strong: bool = False
    local_only: bool = False  # the prompt carries data that must not leave this machine: only local providers may answer (no cloud fallback)


@dataclass
class RoutedResponse:
    response: ModelResponse
    provider: str
    attempts: list[dict]  # [{provider, ok, error_kind?, message?}]
    reason: str
    complexity: float


class NoProviderAvailable(ProviderError):
    def __init__(self, message: str, attempts: list[dict] | None = None):
        super().__init__("unavailable", message)
        self.attempts = attempts or []


class ModelRouter:
    def __init__(self, providers: dict[str, BaseProvider], config: RouterConfig | None = None, *, clock=time.monotonic, cooldown: dict[str, float] | None = None):
        self.providers = providers
        self.config = config or RouterConfig()
        self._cooldown: dict[str, float] = cooldown if cooldown is not None else {}  # may be shared by several routers (one per task)
        self._sticky_until_step = -1
        self._clock = clock

    # ---- scoring
    def complexity(self, ctx: RouteContext) -> tuple[float, list[str]]:
        w, c = self.config.weights, self.config
        score, why = 0.0, []

        def add(key: str, cond: bool, label: str | None = None):
            nonlocal score
            if cond:
                score += w.get(key, 0)
                why.append(label or key)

        add("first_step", ctx.first_step)
        add("failure", ctx.consecutive_failures > 0, f"{ctx.consecutive_failures} failed action(s)")
        add("tool_error", ctx.tool_errors > 0, "unusable model reply")
        add("stale_recovery", ctx.stale_recovery)
        add("many_elements", ctx.element_count > c.many_elements)
        add("text_heavy", ctx.text_heavy)
        add("vision", ctx.needs_vision)
        add("signal", bool(ctx.signals), "page signals: " + ",".join(ctx.signals))
        add("long_instruction", ctx.instruction_words > c.long_instruction_words)
        add("multi_tab", ctx.tab_count > 3)
        return score, why

    # ---- candidate order
    def order_for(self, ctx: RouteContext) -> tuple[list[str], str, float]:
        c = self.config
        score, why = self.complexity(ctx)
        known = list(self.providers)
        if c.policy == Policy.MANUAL:
            name = c.manual_provider or (known[0] if known else None)
            return ([name] if name else []), f"manual: {name}", score
        if c.policy == Policy.GEMINI_FIRST:
            return [n for n in ["gemini", "freellmapi", "local"] if n in known], "gemini-first", score
        if c.policy == Policy.FALLBACK:
            return [n for n in c.order if n in known], "fallback order", score
        # LOCAL_FIRST
        strong = [n for n in c.strong_order if n in known]
        local = [n for n in ["local"] if n in known]
        rest = [n for n in known if n not in strong and n not in local]
        sticky = ctx.step <= self._sticky_until_step
        if ctx.force_strong or sticky or score >= c.complexity_threshold:
            reason = "forced escalation" if ctx.force_strong else "sticky escalation" if sticky else f"complex step ({score:g}: {', '.join(why)})"
            return strong + local + rest, reason, score
        return local + strong + rest, f"simple step ({score:g})", score

    async def _usable(self, name: str) -> tuple[bool, str]:
        p = self.providers.get(name)
        if p is None:
            return False, "not configured"
        until = self._cooldown.get(name, 0)
        if until > self._clock() and self.config.policy != Policy.MANUAL:  # an explicitly chosen provider is always tried; cooldowns only steer automatic routing
            return False, f"cooling down for {until - self._clock():.0f}s"
        ok, why = await p.available()
        return ok, why

    def mark_failed(self, name: str, err: ProviderError) -> None:
        if err.cooldown_s:
            self._cooldown[name] = self._clock() + err.cooldown_s

    def clear_cooldowns(self) -> None:
        self._cooldown.clear()

    # ---- main entry
    async def complete(self, messages: list[Message], tools: list[dict] | None, ctx: RouteContext, **options: Any) -> RoutedResponse:
        order, reason, score = self.order_for(ctx)
        attempts: list[dict] = []
        if ctx.local_only:
            order = [n for n in order if n in self.providers and provider_is_local(self.providers[n])]
            reason += "; local only"
            if not order:
                raise NoProviderAvailable("this step needs a local model and none is configured", attempts)
        if not order:
            raise NoProviderAvailable("no model provider is configured")
        first_name = order[0]
        for name in order:
            ok, why = await self._usable(name)
            if not ok:
                attempts.append({"provider": name, "ok": False, "error_kind": "skipped", "message": why})
                continue
            provider = self.providers[name]
            if ctx.needs_vision and not provider.capabilities.vision and any(self.providers[n].capabilities.vision for n in order if n in self.providers and n != name):
                attempts.append({"provider": name, "ok": False, "error_kind": "skipped", "message": "no vision support"})
                continue
            failures = 0
            while failures < max(1, self.config.escalate_after_failures):
                started, t0 = time.time(), time.perf_counter()  # timing instrumentation: when this attempt began and how long it took (no content)
                timing = lambda: {"model": provider.model, "started_at": started, "duration_ms": int((time.perf_counter() - t0) * 1000)}  # noqa: E731
                try:
                    resp = await provider.complete(messages, tools, **options)
                except ProviderError as e:
                    e.provider = e.provider or name
                    attempts.append({"provider": name, "ok": False, "error_kind": e.kind, "message": str(e)[:200], **timing(), "retry_reasons": list(getattr(e, "retry_reasons", []))})
                    failures += 1
                    self.mark_failed(name, e)
                    if e.kind in ("rate_limit", "auth", "not_configured", "unavailable", "bad_request"):
                        break  # retrying the same provider will not help
                    continue
                attempts.append({"provider": name, "ok": True, **timing(), "model": resp.model or provider.model, "retry_reasons": list(resp.retry_reasons)})
                if name != first_name and self.config.policy == Policy.LOCAL_FIRST:
                    self._sticky_until_step = ctx.step + self.config.sticky_steps
                return RoutedResponse(resp, name, attempts, reason if name == first_name else f"{reason}; fell back to {name}", score)
            if self.config.policy == Policy.MANUAL:
                break
        raise NoProviderAvailable("every model provider failed or is unavailable: " + "; ".join(f"{a['provider']}: {a.get('message', a.get('error_kind'))}" for a in attempts), attempts)

    # ---- introspection
    async def status(self) -> list[dict]:
        out = []
        for name, p in self.providers.items():
            ok, why = await p.available()
            cd = max(0.0, self._cooldown.get(name, 0) - self._clock())
            out.append({**p.describe(), "available": ok and cd == 0, "detail": why if cd == 0 else f"cooling down {cd:.0f}s ({why})"})
        return out
