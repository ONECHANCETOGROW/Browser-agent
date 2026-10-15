"""Authorized data sources for research: the owner's first-party webmaster tools (Google and Bing).

Rules that hold for every source here:
  * Credentials live ONLY in <data dir>/connectors/ (files the owner puts there); they are read from disk when needed, never stored in the database, never returned, never logged
    (error messages are scrubbed), never placed in URLs that are recorded.
  * A client is never inferred from a domain: the OWNER maps a property to exactly one client (Hub, PIN-protected, or the terminal). A property can belong to only one client, so one
    client's data can never be returned for another, and the collaborator can only ask for "this client's" data, never name a property.
  * Access is re-checked against what the credential can actually see before every use (a revoked or downgraded permission is reported, not ignored).
  * Missing credentials, unauthorized properties, unavailable data and API failures are distinct, named errors (never "no data"/"not ranking").
  * Only fixed API hosts can be reached (see http.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROVIDERS = json.loads((Path(__file__).with_name("providers.json")).read_text(encoding="utf-8"))   # names, API hosts and the owner's setup steps are DATA, not logic
SOURCES = ("google_search_console", "bing_webmaster_tools")
LABEL = {s: PROVIDERS[s]["name"] for s in SOURCES}
SHORT = {s: PROVIDERS[s]["short"] for s in SOURCES}


class ConnectorError(Exception):
    """A named, scrubbed failure of a data source. `retryable` tells the loop controller whether trying again later can help."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.details = code, message, retryable, details or {}


_SECRET = re.compile(r"(?i)(apikey=)[^&\s\"']+|(bearer\s+)[A-Za-z0-9._~+/=-]{8,}|(\"?(?:access_token|refresh_token|private_key|client_secret|assertion|id_token)\"?\s*[:=]\s*\"?)[^\s\",}&]+")


def scrub(text: str) -> str:
    """Remove anything that looks like a credential from text that may be shown or stored."""
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2) or m.group(3)) + "[removed]", str(text or ""))[:500]


def domain_of(prop: str) -> str:
    """The registrable-ish host of a webmaster-tool property: 'sc-domain:example.com' or 'https://www.example.com/path/' -> 'example.com'."""
    p = (prop or "").strip().lower()
    if p.startswith("sc-domain:"):
        p = p[len("sc-domain:"):]
    else:
        p = re.sub(r"^[a-z]+://", "", p)
    p = p.split("/")[0].split(":")[0]
    return p[4:] if p.startswith("www.") else p
