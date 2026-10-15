"""Provider factory: builds adapters from plain config dicts (data/brain.json -> "providers") and a KeyStore."""
from __future__ import annotations

from typing import Any

from ..keys import KeyStore
from ..provider import BaseProvider
from .freellmapi import FreeLLMAPIProvider
from .gemini import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from .gemini import GeminiProvider
from .mock import MockProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider

DEFAULT_PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    "local": {"enabled": True, "runtime": "auto", "model": "qwen3:8b", "url": "http://127.0.0.1:11434", "num_ctx": 8192, "think": False, "timeout_s": 240},
    "gemini": {"enabled": True, "model": GEMINI_DEFAULT_MODEL, "thinking_level": None, "timeout_s": 60},
    "freellmapi": {"enabled": True, "url": "http://localhost:3001/v1", "model": "auto", "timeout_s": 90},
    "openai_compat": {"enabled": False, "url": "", "model": "", "timeout_s": 60},
}


def merged_config(user: dict | None) -> dict[str, dict[str, Any]]:
    out = {k: dict(v) for k, v in DEFAULT_PROVIDER_CONFIG.items()}
    for name, cfg in (user or {}).items():
        out.setdefault(name, {}).update(cfg or {})
    return out


def build_providers(user_cfg: dict | None, keys: KeyStore) -> dict[str, BaseProvider]:
    cfg = merged_config(user_cfg)
    out: dict[str, BaseProvider] = {}
    c = cfg["local"]
    if c.get("enabled", True):
        if c.get("runtime") == "openai":  # LM Studio / llama.cpp / any local OpenAI-compatible server
            out["local"] = OpenAICompatProvider(c["url"], c["model"], lambda: keys.get("local"), name="local", local=True, timeout_s=c.get("timeout_s", 240))
            out["local"].kind = "local"
        else:
            out["local"] = OllamaProvider(c["url"], c["model"], num_ctx=int(c.get("num_ctx", 8192)), think=bool(c.get("think", False)), timeout_s=float(c.get("timeout_s", 240)))
    c = cfg["gemini"]
    if c.get("enabled", True):
        out["gemini"] = GeminiProvider(c["model"], lambda: keys.get("gemini"), timeout_s=float(c.get("timeout_s", 60)), thinking_level=c.get("thinking_level"))
    c = cfg["freellmapi"]
    if c.get("enabled", True):
        out["freellmapi"] = FreeLLMAPIProvider(c["url"], c["model"], lambda: keys.get("freellmapi"), timeout_s=float(c.get("timeout_s", 90)))
    c = cfg["openai_compat"]
    if c.get("enabled") and c.get("url"):
        out["openai_compat"] = OpenAICompatProvider(c["url"], c["model"], lambda: keys.get("openai_compat"), name="openai_compat", timeout_s=float(c.get("timeout_s", 60)))
    return out


__all__ = ["build_providers", "merged_config", "DEFAULT_PROVIDER_CONFIG", "MockProvider", "GeminiProvider", "OllamaProvider", "OpenAICompatProvider", "FreeLLMAPIProvider"]
