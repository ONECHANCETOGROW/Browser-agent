"""Provider-independent reasoning interface.

Nothing under `engine/`, `bridge/` or `files/` imports this package (tests/test_isolation.py enforces it). The orchestration layer
(`brain/`) talks to models only through `BaseProvider.complete()` and the router built on top of it, so a local LLM, Gemini, FreeLLMAPI or
any OpenAI-compatible endpoint can be swapped without touching the browser engine.

`tools` are dicts shaped like the registry's tool schema: {"name", "description", "input_schema"}; adapters translate them to the
provider's own function-calling format.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str
    images: list[bytes] | None = None  # PNG/JPEG bytes, only sent to providers that report vision support
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ToolCall:
    id: str
    name: str  # a tool name from the catalog
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int | None] = field(default_factory=dict)  # input_tokens / output_tokens when the provider reports them
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    raw: Any = None
    retry_reasons: list[str] = field(default_factory=list)  # timing instrumentation: why the adapter re-sent the request (e.g. "http_502"), never content


class ProviderError(Exception):
    """A model call failed. `kind` drives routing: rate_limit/unavailable/timeout/auth are retried on another provider."""

    KINDS = ("unavailable", "rate_limit", "auth", "bad_request", "timeout", "invalid_response", "not_configured", "other")

    def __init__(self, kind: str, message: str, *, provider: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind if kind in self.KINDS else "other"
        self.provider = provider
        self.retry_after = retry_after

    @property
    def cooldown_s(self) -> float:
        """How long the router should avoid this provider after this error."""
        if self.kind == "rate_limit":
            return self.retry_after or 60.0
        if self.kind in ("unavailable", "timeout"):
            return 20.0
        if self.kind in ("auth", "not_configured"):
            return 300.0
        return 0.0


@dataclass
class Capabilities:
    native_tools: bool = True
    vision: bool = False
    local: bool = False
    max_context_tokens: int | None = None


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    model: str
    capabilities: Capabilities

    async def available(self) -> tuple[bool, str]: ...

    async def complete(self, messages: list[Message], tools: list[dict] | None = None, **options: Any) -> ModelResponse: ...


class BaseProvider(ABC):
    """Common behaviour. Subclasses implement `_complete`; `complete` adds timing and stamps provider/model on the response."""

    name = "base"
    kind = "base"

    def __init__(self, model: str = "", *, timeout_s: float = 60.0):
        self.model = model
        self.timeout_s = timeout_s
        self.capabilities = Capabilities()
        self._retry_reasons: list[str] = []  # an adapter that re-sends a request appends a short reason; reported by `complete`, never used for decisions

    async def available(self) -> tuple[bool, str]:
        return True, "ok"

    @abstractmethod
    async def _complete(self, messages: list[Message], tools: list[dict] | None, options: dict) -> ModelResponse: ...

    async def complete(self, messages: list[Message], tools: list[dict] | None = None, **options: Any) -> ModelResponse:
        import time

        t0 = time.perf_counter()
        self._retry_reasons = []
        try:
            resp = await self._complete(messages, tools, options)
        except ProviderError as e:
            e.retry_reasons = list(self._retry_reasons)
            raise
        resp.latency_ms = int((time.perf_counter() - t0) * 1000)
        resp.provider = resp.provider or self.name
        resp.model = resp.model or self.model
        resp.retry_reasons = list(self._retry_reasons)
        return resp

    async def close(self) -> None:
        return None

    def describe(self) -> dict:
        """Safe to show anywhere: never contains keys."""
        return {"name": self.name, "kind": self.kind, "model": self.model, "native_tools": self.capabilities.native_tools,
                "vision": self.capabilities.vision, "local": self.capabilities.local}


# ----------------------------------------------------------------------------- tool-call text rescue (JSON mode / sloppy models)
def extract_json_object(text: str) -> dict | None:
    """Pull the first balanced JSON object out of free text (handles ```json fences and surrounding prose)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def tool_call_from_text(text: str, call_id: str = "call_0") -> ToolCall | None:
    """Accept {"tool": ..., "arguments": {...}} / {"name": ..., "arguments"|"args"|"parameters": {...}} written as plain text."""
    obj = extract_json_object(text)
    if not obj:
        return None
    name = obj.get("tool") or obj.get("name") or obj.get("function")
    if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
        obj, name = name, name.get("name")
    if not isinstance(name, str):
        return None
    args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    if "reason" in obj and "reason" not in args:
        args["reason"] = obj["reason"]
    return ToolCall(call_id, name, args)


def json_mode_instruction(tools: list[dict]) -> str:
    """System-prompt suffix for models without native function calling."""
    names = ", ".join(t["name"] for t in tools)
    return ("\n\nYou have no native tool calling. Reply with ONE JSON object and nothing else, shaped exactly as "
            '{"tool": "<name>", "arguments": {...}, "reason": "<short>"}. '
            f"Allowed tool names: {names}. Argument names and types are given in the tool list.")
