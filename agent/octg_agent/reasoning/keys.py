"""Provider credentials. Environment variables win; otherwise a local file written only by `python -m octg_agent provider set-key`.

Rules: keys are never printed, logged, returned by any API, sent to the Chrome extension or shown in the side panel.
`KeyStore.describe()` reports only whether a key exists and where it came from.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENV_NAMES = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "freellmapi": ("FREELLMAPI_API_KEY",),
    "local": ("OCTG_LOCAL_LLM_API_KEY",),
    "openai_compat": ("OCTG_OPENAI_COMPAT_API_KEY",),
}


class KeyStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "provider_keys.json"

    def _file(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except (OSError, ValueError):
            return {}

    def get(self, provider: str) -> str | None:
        for env in ENV_NAMES.get(provider, ()):
            v = os.environ.get(env)
            if v:
                return v
        return self._file().get(provider) or None

    def source(self, provider: str) -> str | None:
        for env in ENV_NAMES.get(provider, ()):
            if os.environ.get(env):
                return f"env:{env}"
        return "file" if self._file().get(provider) else None

    def set(self, provider: str, key: str) -> None:
        data = self._file()
        data[provider] = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def remove(self, provider: str) -> bool:
        data = self._file()
        if provider not in data:
            return False
        del data[provider]
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True

    def describe(self, provider: str) -> dict:
        return {"key_configured": self.source(provider) is not None, "key_source": self.source(provider)}


def redact(text: str, *keys: str | None) -> str:
    """Scrub any known key (and common key shapes) out of an error message before it is stored or shown."""
    import re

    for k in keys:
        if k and len(k) >= 6:
            text = text.replace(k, "[redacted-key]")
    return re.sub(r"(AIza[0-9A-Za-z_\-]{20,}|freellmapi-[0-9A-Za-z_\-]{8,}|sk-[0-9A-Za-z_\-]{16,}|Bearer\s+[0-9A-Za-z._\-]{16,})", "[redacted-key]", text)
