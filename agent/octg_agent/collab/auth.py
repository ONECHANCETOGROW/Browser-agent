"""Caller identities for the collaboration API.

Each caller (e.g. "collaborator") has its own random token and its own set of granted permissions. Only the token's SHA-256 is kept in collab.json; the
plaintext lives in collab_<caller>.token in the data directory so the local client can present it. A collab token is useless on every other /api route
(those still require the agent's own API token), so a collaborator can only ever use registered capabilities, never raw browser actions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from .capabilities import DEFAULT_GRANTS, PERMISSIONS

GRANTS_VERSION = 5
# Permissions that became part of the DEFAULT grant set after the first callers were created: callers that hold the whole earlier default set (the standard collaborator) get them once.
UPGRADES = {2: ("connector:read",), 3: ("owner:message",), 4: ("browser:act",), 5: ("web:post",)}
DEFAULT_CALLER = "collaborator"   # the reasoning/coding AI; more callers can be added with their own grants
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


class Callers:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir)
        self.path = self.dir / "collab.json"

    def _load(self) -> dict:
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("callers"), dict):
                    return d
            except ValueError:
                pass
        return {"callers": {}}

    def _save(self, d: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def token_path(self, name: str) -> Path:
        return self.dir / f"collab_{name}.token"

    def ensure(self, name: str = DEFAULT_CALLER, grants: tuple[str, ...] = DEFAULT_GRANTS) -> None:
        """Create the caller (and its token file) on first run; never changes an existing caller."""
        d = self._load()
        if name in d["callers"] and self.token_path(name).is_file():
            self._upgrade(d)
            return
        self.rotate(name, grants if name not in d["callers"] else tuple(d["callers"][name].get("grants", grants)))

    def _upgrade(self, d: dict) -> None:
        old_default = {p for p in PERMISSIONS if p not in ("agent:task", "test:internal") and not any(p in u for u in UPGRADES.values())}
        changed = False
        for c in d["callers"].values():
            if int(c.get("grants_ver", 1)) >= GRANTS_VERSION:
                continue
            if old_default <= set(c.get("grants", [])):
                c["grants"] = list(dict.fromkeys(list(c.get("grants", [])) + [p for u in UPGRADES.values() for p in u]))
            c["grants_ver"] = GRANTS_VERSION
            changed = True
        if changed:
            self._save(d)

    def rotate(self, name: str, grants: tuple[str, ...] | None = None) -> str:
        if not NAME_RE.match(name):
            raise ValueError("caller names are lower-case letters, digits, - and _")
        d = self._load()
        old = d["callers"].get(name, {})
        g = tuple(grants if grants is not None else old.get("grants", DEFAULT_GRANTS))
        bad = [p for p in g if p not in PERMISSIONS]
        if bad:
            raise ValueError(f"unknown permissions: {bad}")
        tok = secrets.token_urlsafe(32)
        d["callers"][name] = {"sha256": _sha(tok), "grants": list(g), "created_at": time.time(), "disabled": False, "grants_ver": GRANTS_VERSION}
        self._save(d)
        self.token_path(name).write_text(tok, encoding="utf-8")
        return tok

    def set_grants(self, name: str, grants: tuple[str, ...]) -> None:
        d = self._load()
        if name not in d["callers"]:
            raise KeyError(name)
        bad = [p for p in grants if p not in PERMISSIONS]
        if bad:
            raise ValueError(f"unknown permissions: {bad}")
        d["callers"][name]["grants"] = list(grants)
        self._save(d)

    def disable(self, name: str, disabled: bool = True) -> None:
        d = self._load()
        if name in d["callers"]:
            d["callers"][name]["disabled"] = disabled
            self._save(d)

    def list(self) -> list[dict]:
        return [{"name": n, "grants": c.get("grants", []), "disabled": c.get("disabled", False), "created_at": c.get("created_at")} for n, c in self._load()["callers"].items()]

    def authenticate(self, token: str) -> tuple[str, tuple[str, ...]] | None:
        """(caller name, grants) for a valid, enabled token; None otherwise. Constant-time compare against every caller."""
        if not token:
            return None
        h = _sha(token)
        found = None
        for name, c in self._load()["callers"].items():
            if hmac.compare_digest(h, str(c.get("sha256", ""))) and not c.get("disabled"):
                found = (name, tuple(c.get("grants", [])))
        return found
