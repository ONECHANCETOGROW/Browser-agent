"""Google Gemini adapter on the current `google-genai` SDK (pip install google-genai): `client.aio.models.generate_content` with
function declarations given as JSON Schema (`parameters_json_schema`), automatic function calling disabled (the orchestrator, not the SDK,
executes tools through the dispatcher) and function-calling mode ANY so every reply is a tool call.

Docs checked 2026-10-07: ai.google.dev/gemini-api/docs/{function-calling,models,pricing} and github.com/googleapis/python-genai.
The API key comes only from the KeyStore (env GEMINI_API_KEY / GOOGLE_API_KEY, or the local key file); it is never logged or returned.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from ..keys import redact
from ..provider import BaseProvider, Capabilities, Message, ModelResponse, ProviderError, ToolCall

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def _mime(b: bytes) -> str:
    return "image/png" if b[:4] == b"\x89PNG" else "image/jpeg"


class GeminiProvider(BaseProvider):
    kind = "gemini"
    name = "gemini"

    def __init__(self, model: str = DEFAULT_MODEL, key_getter: Callable[[], str | None] = lambda: None, *, timeout_s: float = 60.0,
                 thinking_level: str | None = None, client_factory: Callable[[str], Any] | None = None):
        super().__init__(model or DEFAULT_MODEL, timeout_s=timeout_s)
        self.key_getter = key_getter
        self.thinking_level = thinking_level
        self._client_factory = client_factory  # tests inject a fake client
        self._client: Any = None
        self._client_key: str | None = None
        self.capabilities = Capabilities(native_tools=True, vision=True, local=False, max_context_tokens=1_000_000)

    async def available(self) -> tuple[bool, str]:
        if not self.key_getter():
            return False, "no Gemini API key (set GEMINI_API_KEY or run: python -m octg_agent provider set-key gemini)"
        return True, "key configured"

    def _get_client(self) -> Any:
        key = self.key_getter()
        if not key:
            raise ProviderError("not_configured", "no Gemini API key configured", provider=self.name)
        if self._client is None or key != self._client_key:
            if self._client_factory:
                self._client = self._client_factory(key)
            else:
                try:
                    from google import genai
                except ImportError as e:
                    raise ProviderError("not_configured", "the google-genai package is not installed (pip install google-genai)", provider=self.name) from e
                self._client = genai.Client(api_key=key)
            self._client_key = key
        return self._client

    # ---- schema / content conversion
    @staticmethod
    def _declaration_schema(schema: dict) -> dict:
        """Keep the JSON-Schema subset Gemini accepts reliably."""
        keep = {"type", "description", "enum", "items", "properties", "required"}
        out = {k: v for k, v in schema.items() if k in keep}
        if isinstance(out.get("type"), list):
            non_null = [t for t in out["type"] if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
        if "properties" in out:
            out["properties"] = {k: GeminiProvider._declaration_schema(v) for k, v in out["properties"].items()}
        if "items" in out and isinstance(out["items"], dict):
            out["items"] = GeminiProvider._declaration_schema(out["items"])
        return out

    def _build(self, messages: list[Message], tools: list[dict] | None, options: dict):
        from google.genai import types

        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        contents = []
        for m in messages:
            if m.role == "system":
                continue
            parts = [types.Part.from_text(text=m.content)]
            for img in m.images or []:
                parts.append(types.Part.from_bytes(data=img, mime_type=_mime(img)))
            contents.append(types.Content(role="model" if m.role == "assistant" else "user", parts=parts))
        cfg: dict[str, Any] = {"temperature": options.get("temperature", 0.2), "max_output_tokens": options.get("max_output_tokens") or 1024}
        if system:
            cfg["system_instruction"] = system
        if tools:
            decls = [types.FunctionDeclaration(name=t["name"], description=t.get("description", ""), parameters_json_schema=self._declaration_schema(t["input_schema"])) for t in tools]
            cfg["tools"] = [types.Tool(function_declarations=decls)]
            cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
            cfg["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY" if options.get("force_tool", True) else "AUTO"))
        if self.thinking_level:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)
        return contents, types.GenerateContentConfig(**cfg)

    async def _complete(self, messages: list[Message], tools: list[dict] | None, options: dict) -> ModelResponse:
        client = self._get_client()
        contents, config = self._build(messages, tools, options)
        try:
            resp = await asyncio.wait_for(client.aio.models.generate_content(model=self.model, contents=contents, config=config), self.timeout_s)
        except asyncio.TimeoutError:
            raise ProviderError("timeout", f"Gemini did not answer within {self.timeout_s:g}s", provider=self.name) from None
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - SDK errors are mapped, everything else is reported without secrets
            raise self._map_error(e) from None
        return self._parse(resp)

    def _map_error(self, e: Exception) -> ProviderError:
        key = self.key_getter()
        msg = redact(str(getattr(e, "message", None) or e), key)[:300]
        code = getattr(e, "code", None)
        low = msg.lower()
        if code == 429 or "resource_exhausted" in low or "quota" in low:
            m = re.search(r"retry(?: in|Delay\D+)\s*([\d.]+)\s*s", msg)
            return ProviderError("rate_limit", f"Gemini rate/quota limit: {msg}", provider=self.name, retry_after=float(m.group(1)) if m else None)
        if code in (401, 403) or "api key not valid" in low or "api_key_invalid" in low or "permission_denied" in low:
            return ProviderError("auth", "Gemini rejected the API key", provider=self.name)
        if code == 400:
            return ProviderError("bad_request", f"Gemini rejected the request: {msg}", provider=self.name)
        if isinstance(code, int) and code >= 500 or "unavailable" in low or "overloaded" in low:
            return ProviderError("unavailable", f"Gemini unavailable: {msg}", provider=self.name)
        if isinstance(e, (ConnectionError, OSError)):
            return ProviderError("unavailable", f"cannot reach Gemini: {type(e).__name__}", provider=self.name)
        return ProviderError("other", f"{type(e).__name__}: {msg}", provider=self.name)

    def _parse(self, resp: Any) -> ModelResponse:
        calls: list[ToolCall] = []
        text_parts: list[str] = []
        for cand in (getattr(resp, "candidates", None) or [])[:1]:
            for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    calls.append(ToolCall(getattr(fc, "id", None) or f"call_{len(calls)}", fc.name, dict(getattr(fc, "args", None) or {})))
                elif getattr(part, "text", None) and not getattr(part, "thought", False):
                    text_parts.append(part.text)
        u = getattr(resp, "usage_metadata", None)
        usage = {"input_tokens": getattr(u, "prompt_token_count", None), "output_tokens": None}
        if u is not None:
            out = (getattr(u, "candidates_token_count", None) or 0) + (getattr(u, "thoughts_token_count", None) or 0)
            usage["output_tokens"] = out if (getattr(u, "candidates_token_count", None) is not None or out) else None
        if not calls and not text_parts:
            raise ProviderError("invalid_response", "Gemini returned neither a tool call nor text (possibly blocked)", provider=self.name)
        return ModelResponse(text="".join(text_parts).strip(), tool_calls=calls, usage=usage)
