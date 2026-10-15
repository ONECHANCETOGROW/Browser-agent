"""Bing Webmaster Tools API client (read-only calls).

Credential: bing_webmaster_api_key.txt in <data dir>/connectors/ (Bing Webmaster Tools -> Settings -> API access -> generate key). The key belongs to the owner's Bing account and sees every site
verified in it, so a client is only ever served the property the owner mapped to it. The key travels in the URL query as Bing requires, so URLs are never logged and error text is scrubbed.
Bing's error names/codes are mapped from its documented list; anything unrecognised becomes API_ERROR carrying Bing's own (scrubbed) message.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import PROVIDERS, ConnectorError, scrub
from .http import Http

BASE = "https://ssl.bing.com/webmaster/api.svc/json"
FILE = "bing_webmaster_api_key.txt"
HOW = PROVIDERS["bing_webmaster_tools"]["how"]
_AUTH = {"InvalidApiKey", "UserBlocked", "NotAllowed"}
_UNAUTH = {"NotAuthorized", "NotFound"}
_THROTTLE = {"ThrottleUser", "ThrottleHost", "NoQuota"}
_CODES = {3: "InvalidApiKey", 6: "UserBlocked", 13: "NotAllowed", 14: "NotAuthorized", 12: "NotFound", 4: "ThrottleUser", 5: "ThrottleHost", 7: "NoQuota", 1: "InternalError"}


def bing_date(v) -> str | None:
    """'/Date(1700000000000)/' (optionally with an offset) -> ISO date."""
    m = re.search(r"/Date\((-?\d+)", str(v or ""))
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat() if m else None


class BingWebmaster:
    name = "bing_webmaster_tools"

    def __init__(self, folder: Path | str, http: Http, clock=time.time):
        self.folder, self.http, self.clock = Path(folder), http, clock

    def _key(self) -> str:
        p = self.folder / FILE
        if not p.is_file():
            raise ConnectorError("NOT_CONNECTED", f"no Bing Webmaster credentials: put {FILE} in {self.folder} ({HOW})")
        try:
            k = p.read_text(encoding="utf-8").strip()
        except OSError:
            raise ConnectorError("CREDENTIALS_INVALID", f"{FILE} cannot be read") from None
        if not re.fullmatch(r"[A-Za-z0-9]{16,64}", k):
            raise ConnectorError("CREDENTIALS_INVALID", f"{FILE} does not look like a Bing Webmaster API key (16-64 letters/digits, nothing else)")
        try:
            from ..sensitive import SECRETS
            SECRETS.register(k, "connector:bing")
        except Exception:  # noqa: BLE001
            pass
        return k

    def configured(self) -> dict:
        try:
            self._key()
        except ConnectorError as e:
            return {"configured": False, "kind": None, "identity": None, "problem": e.message, "code": e.code}
        return {"configured": True, "kind": "api_key", "identity": "the owner's Bing Webmaster account (API key)", "problem": "", "code": ""}

    async def _get(self, method: str, params: dict | None = None, *, what: str) -> list | dict:
        q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in (params or {}).items())
        url = f"{BASE}/{method}?{q + '&' if q else ''}apikey={self._key()}"
        res = await self.http.request("GET", url, headers={"Accept": "application/json"})
        d = res.data if isinstance(res.data, (dict, list)) else None
        err = d if isinstance(d, dict) and ("ErrorCode" in d or ("Message" in d and "d" not in d)) else None
        if res.status == 200 and err is None:
            return (d or {}).get("d", []) if isinstance(d, dict) else (d or [])
        raise self._error(res.status, err, what)

    @staticmethod
    def _error(status: int, err: dict | None, what: str) -> ConnectorError:
        name = (err or {}).get("Message") if isinstance((err or {}).get("Message"), str) else ""
        name = name if name in _AUTH | _UNAUTH | _THROTTLE else _CODES.get((err or {}).get("ErrorCode"), name)
        msg = scrub(name or "")
        if name in _AUTH or status in (401,):
            return ConnectorError("AUTH_FAILED", f"Bing rejected the API key while {what}" + (f" ({msg})" if msg else ""))
        if name in _UNAUTH or status == 403:
            return ConnectorError("UNAUTHORIZED_PROPERTY", f"this Bing account has no access to that site ({what}): verify the site in Bing Webmaster Tools under the account that owns the API key")
        if name in _THROTTLE or status == 429:
            return ConnectorError("RATE_LIMITED", f"Bing's quota or throttle was reached ({what}); try again later", retryable=True)
        if status >= 500 or name == "InternalError":
            return ConnectorError("API_ERROR", f"Bing had a server error ({status}) while {what}", retryable=True)
        return ConnectorError("API_ERROR", f"Bing answered {status} while {what}" + (f": {msg}" if msg else ""))

    async def list_sites(self) -> list[dict]:
        rows = await self._get("GetUserSites", what="listing sites")
        # (the verification codes Bing returns with each site are deliberately dropped)
        return [{"property": s["Url"], "permission": "verified" if s.get("IsVerified") else "unverified"} for s in rows if isinstance(s, dict) and s.get("Url")]

    async def query_stats(self, site: str) -> list[dict]:
        rows = await self._get("GetQueryStats", {"siteUrl": site}, what="reading query statistics")
        return [{"query": r.get("Query"), "date": bing_date(r.get("Date")), "impressions": int(r.get("Impressions", 0) or 0), "clicks": int(r.get("Clicks", 0) or 0),
                 "avg_click_position": r.get("AvgClickPosition"), "avg_impression_position": r.get("AvgImpressionPosition")} for r in rows if isinstance(r, dict)]

    async def page_stats(self, site: str) -> list[dict]:
        rows = await self._get("GetPageStats", {"siteUrl": site}, what="reading page statistics")
        return [{"page": r.get("Query") or r.get("Page"), "date": bing_date(r.get("Date")), "impressions": int(r.get("Impressions", 0) or 0), "clicks": int(r.get("Clicks", 0) or 0),
                 "avg_click_position": r.get("AvgClickPosition"), "avg_impression_position": r.get("AvgImpressionPosition")} for r in rows if isinstance(r, dict)]

    async def traffic_stats(self, site: str) -> list[dict]:
        rows = await self._get("GetRankAndTrafficStats", {"siteUrl": site}, what="reading traffic statistics")
        return [{"date": bing_date(r.get("Date")), "impressions": int(r.get("Impressions", 0) or 0), "clicks": int(r.get("Clicks", 0) or 0)} for r in rows if isinstance(r, dict)]
