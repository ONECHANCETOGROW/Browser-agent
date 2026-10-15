"""OpenAI-compatible chat/completions adapter (FreeLLMAPI, LM Studio, llama.cpp server, any compatible endpoint)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any, Callable

import aiohttp

from ..keys import redact
from ..provider import BaseProvider, Capabilities, Message, ModelResponse, ProviderError, ToolCall, json_mode_instruction, tool_call_from_text


log = logging.getLogger("octg.provider")

# FreeLLMAPI re-routes every request across upstream providers, so a second try can land on a different route. Only these kinds
# of endpoints retry (generic OpenAI-compatible servers do not).
_TRANSIENT_RETRY_KINDS = {"freellmapi"}
_RETRY_STATUSES = {429, 500, 502, 503, 504}
# An upstream model that emitted a malformed tool call (Groq: "Parsing failed … failed_generation"). Deliberately narrow: other 400s are
# our own bad requests and are never retried.
_PARSE_FAILURE = re.compile(r"parsing failed|failed_generation|generated output that could not be parsed", re.I)
_RETRY_DELAY_S = 0.5
_RETRY_AFTER_CAP_S = 5.0


async def _sleep(seconds: float) -> None:  # indirection so tests can run without real delays
    await asyncio.sleep(seconds)


def _mime(b: bytes) -> str:
    return "image/png" if b[:4] == b"\x89PNG" else "image/jpeg"


def transient_reason(status: int, body: str) -> str | None:
    """A short, content-free label if this HTTP failure is worth one more try (the model reply was never produced), else None."""
    if status == 400 and _PARSE_FAILURE.search(body):
        return "upstream_tool_call_parse_failure"
    if status in _RETRY_STATUSES:
        return f"http_{status}"
    return None


class OpenAICompatProvider(BaseProvider):
    kind = "openai_compat"
    name = "openai_compat"

    def __init__(self, base_url: str, model: str = "auto", key_getter: Callable[[], str | None] = lambda: None, *, name: str | None = None,
                 tool_mode: str = "auto", timeout_s: float = 60.0, local: bool = False, vision: bool = False, extra_headers: dict | None = None):
        super().__init__(model, timeout_s=timeout_s)
        self.base_url = base_url.rstrip("/")
        self.key_getter = key_getter
        if name:
            self.name = name
        self.tool_mode = tool_mode  # auto | native | json
        self._json_fallback = tool_mode == "json"
        self.extra_headers = extra_headers or {}
        self.capabilities = Capabilities(native_tools=tool_mode != "json", vision=vision, local=local)
        self._session: aiohttp.ClientSession | None = None

    # ---- plumbing
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", **self.extra_headers}
        key = self.key_getter()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def available(self) -> tuple[bool, str]:
        try:
            async with self._http().get(f"{self.base_url}/models", headers=self._headers(), timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status in (401, 403):
                    return False, "endpoint rejected the API key"
                return (True, "ok") if r.status < 400 else (False, f"HTTP {r.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False, f"not reachable at {self.base_url}"

    # ---- request building / parsing
    def _convert_messages(self, messages: list[Message], tools: list[dict] | None, json_mode: bool) -> list[dict]:
        out = []
        for m in messages:
            content: Any = m.content
            if m.role == "system" and json_mode and tools:
                content = m.content + json_mode_instruction(tools)
            if m.images and self.capabilities.vision:
                content = [{"type": "text", "text": m.content}] + [
                    {"type": "image_url", "image_url": {"url": f"data:{_mime(i)};base64,{base64.b64encode(i).decode()}"}} for i in m.images]
            out.append({"role": m.role, "content": content})
        return out

    @staticmethod
    def _tools_payload(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}} for t in tools]

    def _parse(self, data: dict, tools: list[dict] | None) -> ModelResponse:
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("invalid_response", "response has no choices", provider=self.name) from None
        text = (choice.get("content") or "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        calls: list[ToolCall] = []
        for i, tc in enumerate(choice.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    args = {}
            calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name", ""), args if isinstance(args, dict) else {}))
        if not calls and tools and text:
            rescued = tool_call_from_text(text)
            if rescued:
                calls.append(rescued)
        u = data.get("usage") or {}
        return ModelResponse(text=text, tool_calls=calls, usage={"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")}, raw=None)

    async def _complete(self, messages: list[Message], tools: list[dict] | None, options: dict) -> ModelResponse:
        force = options.get("force_tool", True)
        json_mode = bool(tools) and self._json_fallback
        attempt = 0                  # tool-support negotiation steps (tool_choice -> auto -> JSON mode)
        transient_left = 1 if self.kind in _TRANSIENT_RETRY_KINDS else 0
        while attempt < 3:
            payload: dict[str, Any] = {"model": self.model, "messages": self._convert_messages(messages, tools, json_mode),
                                       "temperature": options.get("temperature", 0.2), "stream": False}
            if options.get("max_output_tokens"):
                payload["max_tokens"] = options["max_output_tokens"]
            if tools and not json_mode:
                payload["tools"] = self._tools_payload(tools)
                payload["tool_choice"] = "required" if force and attempt == 0 else "auto"
            try:
                async with self._http().post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers()) as r:
                    body = await r.text()
                    if r.status == 200:
                        return self._parse(json.loads(body), tools if tools else None)
                    msg = redact(body[:400], self.key_getter())
                    retry_after = None
                    if r.status == 429:
                        ra = r.headers.get("Retry-After")
                        retry_after = float(ra) if ra and ra.replace(".", "").isdigit() else None
                    # No reply was produced, so the loop has nothing to act on: retrying cannot repeat a browser action.
                    reason = transient_reason(r.status, body) if transient_left else None
                    if reason:
                        transient_left -= 1
                        delay = min(retry_after, _RETRY_AFTER_CAP_S) if retry_after is not None else _RETRY_DELAY_S
                        self._retry_reasons.append(reason)  # timing instrumentation only
                        log.warning("provider %s: %s (HTTP %s); retrying once in %.1fs so the router can pick another route", self.name, reason, r.status, delay)
                        await _sleep(delay)
                        continue
                    if r.status == 429:
                        raise ProviderError("rate_limit", f"rate limited: {msg}", provider=self.name, retry_after=retry_after)
                    if r.status in (401, 403):
                        raise ProviderError("auth", f"HTTP {r.status}: key rejected", provider=self.name)
                    if (r.status == 400 and tools and not json_mode and self.tool_mode == "auto" and re.search(r"tool|function", body, re.I)
                            and not _PARSE_FAILURE.search(body) and attempt < 2):
                        attempt += 1
                        self._retry_reasons.append("tool_negotiation")  # timing instrumentation only
                        if "tool_choice" in body:
                            continue  # retried with tool_choice=auto on the next loop
                        self._json_fallback = True
                        self.capabilities.native_tools = False
                        json_mode = True
                        continue
                    if r.status >= 500:
                        raise ProviderError("unavailable", f"HTTP {r.status}: {msg}", provider=self.name)
                    raise ProviderError("bad_request", f"HTTP {r.status}: {msg}", provider=self.name)
            except asyncio.TimeoutError:
                raise ProviderError("timeout", f"no answer within {self.timeout_s:g}s", provider=self.name) from None
            except (aiohttp.ClientError, OSError) as e:
                raise ProviderError("unavailable", redact(f"cannot reach {self.base_url}: {type(e).__name__}", self.key_getter()), provider=self.name) from None
            except ValueError:
                raise ProviderError("invalid_response", "response was not JSON", provider=self.name) from None
        raise ProviderError("bad_request", "tool calling was rejected by the endpoint", provider=self.name)
