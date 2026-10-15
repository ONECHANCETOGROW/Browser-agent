"""Local LLM via Ollama's native API (exact token counts, thinking switch, model listing). Never installs or downloads anything."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from pathlib import Path

import aiohttp

from ..provider import BaseProvider, Capabilities, Message, ModelResponse, ProviderError, ToolCall, json_mode_instruction, tool_call_from_text

DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaProvider(BaseProvider):
    kind = "local"
    name = "local"

    def __init__(self, base_url: str = DEFAULT_URL, model: str = "qwen3:8b", *, num_ctx: int = 8192, think: bool = False,
                 timeout_s: float = 240.0, keep_alive: str = "10m", vision: bool = False):
        super().__init__(model, timeout_s=timeout_s)
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.num_ctx = num_ctx
        self.think = think
        self.keep_alive = keep_alive
        self.capabilities = Capabilities(native_tools=True, vision=vision, local=True, max_context_tokens=num_ctx)
        self._json_fallback = False
        self._no_think_param = False
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def installed_models(self) -> list[str]:
        async with self._http().get(f"{self.base_url}/api/tags", timeout=aiohttp.ClientTimeout(total=4)) as r:
            r.raise_for_status()
            return [m["name"] for m in (await r.json()).get("models", [])]

    async def available(self) -> tuple[bool, str]:
        try:
            models = await self.installed_models()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            exe = shutil.which("ollama") or (Path.home() / "AppData/Local/Programs/Ollama/ollama.exe")
            hint = "Ollama is installed but not running: start it with `ollama serve`" if Path(str(exe)).exists() else f"no local runtime at {self.base_url}"
            return False, hint
        want = self.model if ":" in self.model else self.model + ":latest"
        if want not in models and self.model not in models:
            return False, f"model {self.model!r} is not installed in Ollama (installed: {', '.join(models) or 'none'})"
        return True, "ok"

    def _convert(self, messages: list[Message], tools: list[dict] | None, json_mode: bool) -> list[dict]:
        out = []
        for m in messages:
            d: dict = {"role": m.role, "content": m.content}
            if m.role == "system" and json_mode and tools:
                d["content"] += json_mode_instruction(tools)
            if m.images and self.capabilities.vision:
                d["images"] = [base64.b64encode(i).decode() for i in m.images]
            out.append(d)
        return out

    async def _complete(self, messages: list[Message], tools: list[dict] | None, options: dict) -> ModelResponse:
        json_mode = bool(tools) and self._json_fallback
        for attempt in range(3):
            payload: dict = {
                "model": self.model, "messages": self._convert(messages, tools, json_mode), "stream": False, "keep_alive": self.keep_alive,
                "options": {"temperature": options.get("temperature", 0.2), "num_ctx": self.num_ctx, "num_predict": options.get("max_output_tokens") or 700},
            }
            if not self._no_think_param:
                payload["think"] = self.think
            if tools and not json_mode:
                payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}} for t in tools]
            try:
                async with self._http().post(f"{self.base_url}/api/chat", json=payload) as r:
                    body = await r.text()
                    if r.status == 200:
                        return self._parse(json.loads(body), tools)
                    if r.status == 404:
                        raise ProviderError("unavailable", f"model {self.model!r} not found in Ollama", provider=self.name)
                    if r.status == 400 and attempt < 2:
                        if "think" in body and not self._no_think_param:
                            self._no_think_param = True
                            continue
                        if re.search(r"does not support tools|tools", body) and tools and not json_mode:
                            self._json_fallback = True
                            self.capabilities.native_tools = False
                            json_mode = True
                            continue
                    if r.status >= 500:
                        raise ProviderError("unavailable", f"Ollama error {r.status}: {body[:200]}", provider=self.name)
                    raise ProviderError("bad_request", f"Ollama HTTP {r.status}: {body[:200]}", provider=self.name)
            except asyncio.TimeoutError:
                raise ProviderError("timeout", f"local model did not answer within {self.timeout_s:g}s", provider=self.name) from None
            except (aiohttp.ClientError, OSError):
                raise ProviderError("unavailable", f"cannot reach Ollama at {self.base_url} (start it with `ollama serve`)", provider=self.name) from None
            except ValueError:
                raise ProviderError("invalid_response", "Ollama reply was not JSON", provider=self.name) from None
        raise ProviderError("bad_request", "Ollama rejected the request", provider=self.name)

    def _parse(self, data: dict, tools: list[dict] | None) -> ModelResponse:
        msg = data.get("message") or {}
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(f"call_{i}", fn.get("name", ""), args if isinstance(args, dict) else {}))
        if not calls and tools and text:
            rescued = tool_call_from_text(text)
            if rescued:
                calls.append(rescued)
        return ModelResponse(text=text, tool_calls=calls, usage={"input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")})


async def detect_local_runtimes(timeout: float = 1.5) -> list[dict]:
    """Report what local LLM runtimes exist or run on this machine. Read-only: probes loopback ports and looks for installed binaries."""
    found: list[dict] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        probes = [("ollama", "http://127.0.0.1:11434/api/tags"), ("lm-studio", "http://127.0.0.1:1234/v1/models"), ("llama.cpp-server", "http://127.0.0.1:8080/v1/models")]
        for name, url in probes:
            try:
                async with s.get(url) as r:
                    if r.status == 200:
                        data = await r.json()
                        models = [m.get("name") or m.get("id") for m in (data.get("models") or data.get("data") or [])]
                        found.append({"runtime": name, "running": True, "url": url.rsplit("/", 2)[0] if name != "ollama" else "http://127.0.0.1:11434", "models": models})
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                pass
    if not any(f["runtime"] == "ollama" for f in found):
        exe = shutil.which("ollama") or str(Path.home() / "AppData/Local/Programs/Ollama/ollama.exe")
        if Path(exe).exists():
            manifests = Path.home() / ".ollama/models/manifests"
            models = sorted({"/".join(p.parts[-2:]) for p in manifests.rglob("*") if p.is_file()}) if manifests.exists() else []
            found.append({"runtime": "ollama", "running": False, "installed": True, "models_on_disk": models, "hint": "start with: ollama serve"})
    return found
