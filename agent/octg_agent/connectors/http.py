"""The only way a connector reaches the internet: fixed API hosts, https, no redirects, bounded size and time, bounded retries.

Why so strict: a credentials file or a model-supplied URL must never be able to point an authorized request somewhere else. Hosts are a constant (not read from any file);
a redirect is refused rather than followed (it would carry the Authorization header to another host).
Retries: timeouts, connection errors, 429 and 5xx are retried a bounded number of times with back-off (Retry-After honoured, capped); the last answer is returned for the caller
to map to a named error. `sleep` is injectable so tests do not wait.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from . import PROVIDERS, SOURCES, ConnectorError

ALLOWED_HOSTS = frozenset(h for s in SOURCES for h in PROVIDERS[s]["hosts"] + ([PROVIDERS[s]["inspect_host"]] if PROVIDERS[s].get("inspect_host") else []))
RETRY_STATUS = (429, 500, 502, 503, 504)


@dataclass
class HttpResult:
    status: int
    data: Any = None
    headers: dict = field(default_factory=dict)
    text: str = ""


class Http:
    def __init__(self, *, allowed_hosts=ALLOWED_HOSTS, schemes: tuple[str, ...] = ("https",), max_retries: int = 2, base_delay: float = 0.5, max_delay: float = 10.0,
                 timeout: float = 30.0, max_bytes: int = 5_000_000, sleep=None):
        self.allowed_hosts, self.schemes = frozenset(allowed_hosts), schemes
        self.max_retries, self.base_delay, self.max_delay, self.timeout, self.max_bytes = max_retries, base_delay, max_delay, timeout, max_bytes
        self.sleep = sleep or asyncio.sleep
        self.attempts = 0   # total requests actually sent (for tests and diagnostics)

    def check(self, url: str) -> None:
        p = urlsplit(url)
        if p.scheme not in self.schemes or not p.hostname or p.hostname.lower() not in self.allowed_hosts or p.username or p.password:
            raise ConnectorError("BLOCKED_HOST", "this connector may only call its provider's fixed API hosts over https")

    async def request(self, method: str, url: str, *, headers: dict | None = None, json_body: Any = None, form: dict | None = None) -> HttpResult:
        self.check(url)
        last: HttpResult | None = None
        for attempt in range(self.max_retries + 1):
            self.attempts += 1
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
                    async with s.request(method, url, headers=headers, json=json_body, data=form, allow_redirects=False) as r:
                        raw = await r.content.read(self.max_bytes + 1)
                        if len(raw) > self.max_bytes:
                            raise ConnectorError("RESPONSE_TOO_LARGE", f"the provider's answer is larger than {self.max_bytes // 1000} KB: nothing was used")
                        text = raw.decode("utf-8", "replace")
                        try:
                            data = json.loads(text) if text.strip() else None
                        except ValueError:
                            data = None
                        res = HttpResult(r.status, data, {k.lower(): v for k, v in r.headers.items()}, text[:2000])
            except ConnectorError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt >= self.max_retries:
                    raise ConnectorError("API_UNREACHABLE", f"the provider's API could not be reached ({type(e).__name__}) after {attempt + 1} attempt(s)", retryable=True) from None
                await self.sleep(self._delay(attempt, None))
                continue
            if 300 <= res.status < 400:
                raise ConnectorError("REDIRECT_REFUSED", "the provider answered with a redirect; it was not followed (an authorized request is never sent to another address)")
            if res.status in RETRY_STATUS and attempt < self.max_retries:
                await self.sleep(self._delay(attempt, res.headers.get("retry-after")))
                last = res
                continue
            return res
        return last   # pragma: no cover - the loop always returns or raises

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        try:
            if retry_after:
                return min(self.max_delay, max(0.0, float(retry_after)))
        except ValueError:
            pass
        return min(self.max_delay, self.base_delay * (2 ** attempt))
