"""Deterministic provider for tests: replays a script of tool calls / errors, or computes a reply from a handler."""
from __future__ import annotations

import inspect
from typing import Any, Callable

from ..provider import BaseProvider, Capabilities, Message, ModelResponse, ProviderError, ToolCall


class MockProvider(BaseProvider):
    kind = "mock"

    def __init__(self, script: list | None = None, *, name: str = "mock", model: str = "mock-1", handler: Callable | None = None,
                 usage: tuple[int | None, int | None] = (120, 25), up: bool = True, local: bool = False, vision: bool = False, native_tools: bool = True):
        super().__init__(model)
        self.name = name
        self.script = list(script or [])
        self.handler = handler
        self.usage = usage
        self.up = up
        self.calls: list[dict] = []
        self.capabilities = Capabilities(native_tools=native_tools, vision=vision, local=local)
        if local:
            self.kind = "local"

    async def available(self) -> tuple[bool, str]:
        return (True, "ok") if self.up else (False, "mock marked unavailable")

    @staticmethod
    def call(tool: str, **arguments: Any) -> ToolCall:
        return ToolCall("call_mock", tool, arguments)

    async def _complete(self, messages: list[Message], tools: list[dict] | None, options: dict) -> ModelResponse:
        self.calls.append({"messages": messages, "tools": [t["name"] for t in tools or []], "options": options})
        if self.handler is not None:
            item = self.handler(messages, tools, options)
            if inspect.isawaitable(item):
                item = await item
        elif self.script:
            item = self.script.pop(0)
            if callable(item):
                item = item(messages, tools, options)
                if inspect.isawaitable(item):
                    item = await item
        else:
            raise ProviderError("other", "mock script exhausted", provider=self.name)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            item = ToolCall("call_mock", item["tool"], item.get("arguments", {}))
        if isinstance(item, ToolCall):
            return ModelResponse(tool_calls=[item], usage={"input_tokens": self.usage[0], "output_tokens": self.usage[1]})
        if isinstance(item, ModelResponse):
            return item
        return ModelResponse(text=str(item), usage={"input_tokens": self.usage[0], "output_tokens": self.usage[1]})
