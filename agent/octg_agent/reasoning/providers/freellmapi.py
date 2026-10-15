"""FreeLLMAPI adapter: a self-hosted OpenAI-compatible router (default http://localhost:3001/v1, bearer token `freellmapi-...`,
`model: "auto"` lets its router pick a free-tier model). Optional: if it is down or unconfigured the router simply skips it."""
from __future__ import annotations

from typing import Callable

from .openai_compat import OpenAICompatProvider

DEFAULT_URL = "http://localhost:3001/v1"


class FreeLLMAPIProvider(OpenAICompatProvider):
    kind = "freellmapi"
    name = "freellmapi"

    def __init__(self, base_url: str = DEFAULT_URL, model: str = "auto", key_getter: Callable[[], str | None] = lambda: None, **kw):
        kw.setdefault("name", "freellmapi")
        super().__init__(base_url or DEFAULT_URL, model or "auto", key_getter, **kw)

    async def available(self) -> tuple[bool, str]:
        if not self.key_getter():
            return False, "no FreeLLMAPI key configured (python -m octg_agent provider set-key freellmapi)"
        return await super().available()
