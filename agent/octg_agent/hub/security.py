"""Who may use the Knowledge Hub, and how it is protected from web pages.

The Hub is a page in your own browser that talks to this agent on 127.0.0.1. A web page you happen to visit can also make your browser call 127.0.0.1, so the Hub does NOT trust "it came from this computer".
Every request must prove all of the following:
  * the Host is the loopback name (blocks DNS rebinding);
  * a state-changing request comes from the Hub's OWN origin (Origin header) and the browser says it is same-origin (Sec-Fetch-Site);
  * a session cookie that only a login creates (HttpOnly, SameSite=Strict, scoped to /hub) AND a per-session CSRF header that only the Hub's own JavaScript can read (a page cannot send custom headers
    cross-origin without a CORS grant, and the Hub never grants one).
Logging in needs the owner's PIN (set with `python -m octg_agent hub pin set` or on the Security page; only a salted hash is kept), the agent's access token (what
`python -m octg_agent token --api` prints; the way back in if the PIN is forgotten) or a one-time code made by `python -m octg_agent hub open` (valid five minutes, used once).
A short PIN is easy to guess, so every run of wrong guesses locks signing in for twice as long as the one before (1, 2, 4 ... up to 60 minutes) until a correct sign-in.
The browser agent (the model's hands) cannot operate the Hub at all: the extension treats these pages as ones it may not script (see extension/background/common.js).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
COOKIE = "octg_hub"
IDLE_S = 8 * 3600
ABSOLUTE_S = 24 * 3600
MAX_SESSIONS = 8
LOGIN_CODE_TTL_S = 300


def host_only(h: str) -> str:
    h = (h or "").strip()
    return h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"


@dataclass
class Session:
    sid: str
    csrf: str
    created: float
    last: float


class HubSessions:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._s: dict[str, Session] = {}

    def create(self) -> Session:
        now = self._clock()
        self._prune(now)
        while len(self._s) >= MAX_SESSIONS:   # the oldest session makes room
            self._s.pop(min(self._s.values(), key=lambda x: x.last).sid)
        s = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(24), now, now)
        self._s[s.sid] = s
        return s

    def _prune(self, now: float) -> None:
        for sid, s in list(self._s.items()):
            if now - s.last > IDLE_S or now - s.created > ABSOLUTE_S:
                del self._s[sid]

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self._s.get(sid)
        if s is None:
            return None
        now = self._clock()
        if now - s.last > IDLE_S or now - s.created > ABSOLUTE_S:
            self._s.pop(sid, None)
            return None
        s.last = now
        return s

    def drop(self, sid: str | None) -> None:
        if sid:
            self._s.pop(sid, None)

    def drop_all(self) -> int:
        n = len(self._s)
        self._s.clear()
        return n

    def __len__(self) -> int:
        return len(self._s)


class LoginThrottle:
    """Five wrong attempts in a minute lock logging in for a minute; each further lock doubles (1, 2, 4 ... 60 minutes) until a correct sign-in. A 4-digit PIN has only 10,000
    possibilities, so this is what makes guessing it impractical: after the first few rounds a full search would take months."""
    MAX_LOCK_S = 3600

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._rounds = 0

    def check(self) -> float:
        """Seconds still locked (0 = allowed)."""
        return max(0.0, self._locked_until - self._clock())

    def failed(self) -> None:
        now = self._clock()
        self._fails = [t for t in self._fails if now - t < 60] + [now]
        if len(self._fails) >= 5:
            self._locked_until = now + min(self.MAX_LOCK_S, 60 * 2 ** self._rounds)
            self._rounds += 1
            self._fails.clear()

    def ok(self) -> None:
        self._fails.clear()
        self._rounds = 0


PIN_MIN, PIN_MAX = 4, 12


class HubPin:
    """The owner's sign-in PIN, kept as a salted scrypt hash in data/hub_pin.json (never the PIN itself). Read from disk on every use, so changing it in the terminal takes effect at once."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "hub_pin.json"

    @staticmethod
    def _hash(pin: str, salt: bytes) -> str:
        return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()

    def _load(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("salt") and d.get("hash") else None

    def is_set(self) -> bool:
        return self._load() is not None

    @staticmethod
    def valid(pin) -> bool:
        return isinstance(pin, str) and pin.isascii() and pin.isdigit() and PIN_MIN <= len(pin) <= PIN_MAX

    def set(self, pin: str) -> None:
        if not self.valid(pin):
            raise ValueError(f"a PIN is {PIN_MIN} to {PIN_MAX} digits")
        salt = secrets.token_bytes(16)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"salt": salt.hex(), "hash": self._hash(pin, salt), "set_at": time.time()}), encoding="utf-8")
        tmp.replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def verify(self, pin) -> bool:
        d = self._load()
        if d is None or not self.valid(pin):
            return False
        try:
            return hmac.compare_digest(self._hash(pin, bytes.fromhex(d["salt"])), str(d["hash"]))
        except ValueError:
            return False


class LoginCodes:
    """A one-time login code written to disk by the local CLI (so it can open the browser already signed in) and spent on first use."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "hub_login.json"

    @staticmethod
    def _h(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    def issue(self) -> str:
        code = secrets.token_urlsafe(24)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"hash": self._h(code), "expires": time.time() + LOGIN_CODE_TTL_S}), encoding="utf-8")
        return code

    def redeem(self, code: str) -> bool:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        ok = isinstance(d, dict) and time.time() < float(d.get("expires", 0)) and hmac.compare_digest(str(d.get("hash", "")), self._h(str(code or "")))
        if ok:
            try:
                self.path.unlink()
            except OSError:
                return False   # could not be spent: do not accept it
        return ok


CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; form-action 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; object-src 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                    "Cross-Origin-Resource-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin", "Cache-Control": "no-store", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}


def origin_ok(origin: str | None, host: str) -> bool:
    """The Origin of a state-changing request must be exactly this Hub (http, the same host and port)."""
    return bool(origin) and origin == f"http://{host}"
