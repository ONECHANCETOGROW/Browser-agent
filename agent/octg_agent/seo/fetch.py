"""A guarded HTTP(S) fetcher for public web pages.

Why guarded: the collaboration API lets a remote AI name URLs. Without a guard it could make this machine fetch http://127.0.0.1:8765/api/... (the agent
itself), a router admin page or any LAN device. So every hop (including each redirect) must resolve ONLY to public addresses, the scheme must be http(s),
the port must be a web port, and the body is size-capped. Redirects are followed manually so the chain is recorded as evidence.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import aiohttp

CHALLENGE_RE = re.compile(r"checking your browser|just a moment\.\.\.|attention required|verify you are (a )?human|ddos protection by|cf-chl-|challenge-platform|"
                          r"please enable javascript and cookies|bot protection|are you a robot|captcha-delivery|px-captcha")
UA = "Mozilla/5.0 (compatible; OCTG-Agent/1.0; owner-authorized site check)"
MAX_REDIRECTS = 8
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
KEEP_HEADERS = ("content-type", "content-length", "x-robots-tag", "cache-control", "last-modified", "server", "location", "link", "content-language", "strict-transport-security",
                "x-frame-options", "content-security-policy", "vary", "etag", "age", "x-cache", "cf-cache-status", "platform", "x-powered-by")


class FetchError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class Hop:
    url: str
    status: int
    location: str = ""


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    truncated: bool
    elapsed_ms: int
    chain: list[Hop] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml") or (not self.content_type and self.body[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")))

    def text(self) -> str:
        ct = self.headers.get("content-type") or ""
        enc = "utf-8"
        if "charset=" in ct.lower():
            enc = ct.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(enc, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")

    @property
    def challenge(self) -> bool:
        """A bot-protection / "checking your browser" interstitial instead of the real page. Never treated as the site's content, never bypassed."""
        if self.status not in (403, 429, 503) and not (self.status == 200 and len(self.body) < 20000):
            return False
        head = self.body[:8000].decode("utf-8", "replace").lower()
        return bool(CHALLENGE_RE.search(head))

    def summary(self) -> dict:
        return {"challenge": self.challenge, "requested_url": self.requested_url, "final_url": self.final_url, "status": self.status, "content_type": self.content_type, "bytes": len(self.body),
                "truncated": self.truncated, "elapsed_ms": self.elapsed_ms, "redirects": [{"url": h.url, "status": h.status, "location": h.location} for h in self.chain],
                "headers": self.headers, "fetched_at": self.fetched_at}


def normalise_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise FetchError("BAD_URL", "empty URL")
    if "://" not in u:
        u = "https://" + u
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https"):
        raise FetchError("BAD_URL", "only http and https URLs can be fetched")
    if not parts.hostname:
        raise FetchError("BAD_URL", "the URL has no host")
    if parts.username or parts.password:
        raise FetchError("BAD_URL", "URLs with embedded credentials are refused")
    try:
        port = parts.port
    except ValueError:
        raise FetchError("BAD_URL", "bad port") from None
    if port not in ALLOWED_PORTS:
        raise FetchError("BLOCKED_PORT", f"port {port} is not a public web port")
    return u


def _is_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_loopback or a.is_private or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


async def assert_public_host(host: str, resolver=None) -> list[str]:
    """Resolve and refuse unless EVERY address is public. `resolver` is injectable for tests."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        ips = [host.strip("[]")]
    except ValueError:
        if host.lower() in ("localhost",) or host.lower().endswith((".localhost", ".local", ".internal", ".lan", ".home")):
            raise FetchError("BLOCKED_ADDRESS", f"{host} is a local name") from None
        if resolver is not None:
            ips = await resolver(host)
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            except socket.gaierror as e:
                raise FetchError("DNS_FAILED", f"{host} does not resolve ({e})", retryable=True) from None
            ips = sorted({i[4][0] for i in infos})
    bad = [ip for ip in ips if not _is_public(ip)]
    if not ips or bad:
        raise FetchError("BLOCKED_ADDRESS", f"{host} resolves to a non-public address; only public websites can be fetched")
    return ips


class PublicOnlyResolver(aiohttp.ThreadedResolver):
    """The resolver aiohttp actually connects with: it drops nothing silently, it REFUSES a host if any answer is non-public. Checking at connect time
    (not only before the request) closes the DNS-rebinding gap between our check and the socket."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        res = await super().resolve(host, port, family)
        if not res or any(not _is_public(r["host"]) for r in res):
            raise OSError(f"{host} resolves to a non-public address")
        return res


def new_session(timeout_s: float = 25.0, allow_private: bool = False) -> aiohttp.ClientSession:
    conn = aiohttp.TCPConnector(resolver=None if allow_private else PublicOnlyResolver(), limit=6)
    return aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=timeout_s),
                                 headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.8"})


async def fetch(url: str, *, max_bytes: int = 3_000_000, timeout_s: float = 25.0, session: aiohttp.ClientSession | None = None, resolver=None,
                method: str = "GET", allow_private: bool = False) -> FetchResult:
    """`allow_private` exists ONLY for the test-suite's local fixture server; no capability passes it. Pass a `session` from `new_session()` to reuse connections."""
    start = time.perf_counter()
    cur = normalise_url(url)
    chain: list[Hop] = []
    own = session is None
    sess = session or new_session(timeout_s, allow_private)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(cur).hostname or ""
            if not allow_private:
                await assert_public_host(host, resolver)
            try:
                async with sess.request(method, cur, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
                    headers = {k.lower(): v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        nxt = urljoin(cur, r.headers["Location"])
                        chain.append(Hop(cur, r.status, nxt))
                        cur = normalise_url(nxt)
                        continue
                    body = b""
                    truncated = False
                    async for chunk in r.content.iter_chunked(65536):
                        body += chunk
                        if len(body) > max_bytes:
                            body, truncated = body[:max_bytes], True
                            break
                    return FetchResult(url, cur, r.status, headers, body, truncated, int((time.perf_counter() - start) * 1000), chain, time.time())
            except asyncio.TimeoutError:
                raise FetchError("FETCH_TIMEOUT", f"{cur} did not answer within {timeout_s:g} s", retryable=True) from None
            except aiohttp.ClientError as e:
                raise FetchError("FETCH_FAILED", f"{cur}: {type(e).__name__}: {str(e)[:200]}", retryable=True) from None
        raise FetchError("TOO_MANY_REDIRECTS", f"more than {MAX_REDIRECTS} redirects starting at {url}")
    finally:
        if own:
            await sess.close()
