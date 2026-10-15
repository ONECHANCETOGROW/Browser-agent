"""GSC API client (Google's webmaster API, read-only scope).

Credentials (the owner puts ONE of these in <data dir>/connectors/):
  gsc_service_account.json   a Google Cloud service-account key; the service account's e-mail must be added as a user of each property.
  gsc_oauth_token.json       {"client_id", "client_secret", "refresh_token"} for an OAuth client (created by `python -m octg_agent connectors gsc-login`), acting as the owner's Google user.
The token endpoint is a constant (never read from the file). Access tokens are kept in memory only.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from . import PROVIDERS, ConnectorError, scrub
from .http import Http

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/webmasters/v3"
INSPECT_URL = "https://" + PROVIDERS["google_search_console"]["inspect_host"] + "/v1/urlInspection/index:inspect"
FILES = ("gsc_service_account.json", "gsc_oauth_token.json")
QUERYABLE = ("siteOwner", "siteFullUser", "siteRestrictedUser")   # siteUnverifiedUser cannot read data
DIMENSIONS = ("query", "page", "date", "country", "device", "searchAppearance")
HOW = PROVIDERS["google_search_console"]["how"]


class GscClient:
    name = "google_search_console"

    def __init__(self, folder: Path | str, http: Http, clock=time.time):
        self.folder, self.http, self.clock = Path(folder), http, clock
        self._token: tuple[str, float] | None = None

    # ------------------------------------------------------------------ credentials (never returned)
    def _load(self) -> tuple[str, dict]:
        for fname, kind in (("gsc_service_account.json", "service_account"), ("gsc_oauth_token.json", "oauth")):
            p = self.folder / fname
            if not p.is_file():
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ConnectorError("CREDENTIALS_INVALID", f"{fname} is not valid JSON") from None
            need = ("client_email", "private_key") if kind == "service_account" else ("client_id", "client_secret", "refresh_token")
            if not isinstance(d, dict) or any(not isinstance(d.get(k), str) or not d[k] for k in need):
                raise ConnectorError("CREDENTIALS_INVALID", f"{fname} is missing {', '.join(k for k in need if not (isinstance(d, dict) and d.get(k)))}")
            if kind == "service_account" and d.get("type") not in (None, "service_account"):
                raise ConnectorError("CREDENTIALS_INVALID", f"{fname} is not a service-account key")
            return kind, d
        raise ConnectorError("NOT_CONNECTED", f"no GSC credentials: put {' or '.join(FILES)} in {self.folder} ({HOW})")

    def configured(self) -> dict:
        """Offline: what is present, never its contents. `identity` is the service-account e-mail (not secret) the owner must add to each property."""
        try:
            kind, d = self._load()
        except ConnectorError as e:
            return {"configured": False, "kind": None, "identity": None, "problem": e.message, "code": e.code}
        return {"configured": True, "kind": kind, "identity": d.get("client_email") if kind == "service_account" else "the owner's Google user (OAuth)", "problem": "", "code": ""}

    def reset(self) -> None:
        """Forget the cached access token: the next call proves the stored credentials again (used by verification)."""
        self._token = None

    # ------------------------------------------------------------------ tokens
    async def access_token(self, *, force: bool = False) -> str:
        if self._token and not force and self._token[1] > self.clock() + 60:
            return self._token[0]
        kind, d = self._load()
        if kind == "service_account":
            try:
                import jwt
            except ImportError:
                raise ConnectorError("SIGNING_UNAVAILABLE", "the PyJWT/cryptography packages are needed to sign a service-account request") from None
            now = int(self.clock())
            try:
                assertion = jwt.encode({"iss": d["client_email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3000}, d["private_key"], algorithm="RS256")
            except Exception as e:  # noqa: BLE001 - a malformed key must be a named error, not a crash
                raise ConnectorError("CREDENTIALS_INVALID", f"the service-account private key could not be used ({type(e).__name__})") from None
            form = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        else:
            form = {"grant_type": "refresh_token", "refresh_token": d["refresh_token"], "client_id": d["client_id"], "client_secret": d["client_secret"]}
        res = await self.http.request("POST", TOKEN_URL, form=form)
        if res.status != 200 or not isinstance(res.data, dict) or not res.data.get("access_token"):
            err = (res.data or {}).get("error") if isinstance(res.data, dict) else ""
            if res.status in (429,) or res.status >= 500:
                raise ConnectorError("API_ERROR", f"Google's token service answered {res.status}", retryable=True)
            raise ConnectorError("AUTH_FAILED", "Google rejected these credentials" + (f" ({scrub(err)})" if err else "") + ": they may be revoked, expired or for another project")
        tok = res.data["access_token"]
        self._token = (tok, self.clock() + float(res.data.get("expires_in", 3600)))
        try:
            from ..sensitive import SECRETS
            SECRETS.register(tok, "connector:gsc")   # the log scrubber must know it
        except Exception:  # noqa: BLE001
            pass
        return tok

    # ------------------------------------------------------------------ calls
    async def _call(self, method: str, url: str, *, json_body=None, what: str):
        for attempt in (0, 1):
            tok = await self.access_token(force=attempt == 1)
            res = await self.http.request(method, url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"}, json_body=json_body)
            if res.status == 401 and attempt == 0:
                self._token = None      # an expired/revoked token: fetch a fresh one once
                continue
            break
        if res.status == 200:
            return res.data if isinstance(res.data, dict) else {}
        raise self._error(res, what)

    @staticmethod
    def _error(res, what: str) -> ConnectorError:
        e = (res.data or {}).get("error") if isinstance(res.data, dict) else None
        msg = scrub((e or {}).get("message", "") if isinstance(e, dict) else (e or ""))
        blob = json.dumps(e)[:2000] if isinstance(e, dict) else str(e)
        st = res.status
        if st == 401:
            return ConnectorError("AUTH_FAILED", f"Google rejected the credentials while {what}")
        if st == 403:
            if "SERVICE_DISABLED" in blob or "accessNotConfigured" in blob or "has not been used in project" in msg:
                return ConnectorError("API_DISABLED", f"the GSC API is not enabled in the Google Cloud project of these credentials ({what})")
            return ConnectorError("UNAUTHORIZED_PROPERTY", f"these credentials have no permission on that property ({what}): add the account as a user of the property in the webmaster tool")
        if st == 404:
            return ConnectorError("PROPERTY_NOT_FOUND", f"GSC does not know that property ({what})")
        if st == 400:
            return ConnectorError("BAD_REQUEST", f"GSC refused the request ({what}): {msg or 'invalid'}")
        if st == 429:
            return ConnectorError("RATE_LIMITED", f"GSC's quota was reached ({what}); try again later", retryable=True)
        if st >= 500:
            return ConnectorError("API_ERROR", f"GSC had a server error ({st}) while {what}", retryable=True)
        return ConnectorError("API_ERROR", f"GSC answered {st} while {what}: {msg}", retryable=st >= 500)

    async def list_sites(self) -> list[dict]:
        d = await self._call("GET", f"{API}/sites", what="listing properties")
        return [{"property": s["siteUrl"], "permission": s.get("permissionLevel", "")} for s in d.get("siteEntry", []) if s.get("siteUrl")]

    @staticmethod
    def window(days: int = 28, end_offset_days: int = 3, today: date | None = None) -> tuple[str, str]:
        """GSC data lags by about two days: the default window ends three days ago."""
        end = (today or date.today()) - timedelta(days=end_offset_days)
        return (end - timedelta(days=max(1, days) - 1)).isoformat(), end.isoformat()

    async def query(self, site: str, *, start: str, end: str, dimensions: list[str] | None = None, row_limit: int = 100, queries: list[str] | None = None,
                    pages: list[str] | None = None, search_type: str = "web", start_row: int = 0) -> dict:
        dims = [x for x in (dimensions or []) if x in DIMENSIONS]
        body: dict = {"startDate": start, "endDate": end, "rowLimit": max(1, min(row_limit, 1000)), "startRow": start_row, "type": search_type, "dataState": "final"}
        if dims:
            body["dimensions"] = dims
        filters = [{"dimension": "query", "operator": "equals", "expression": q} for q in (queries or [])]
        filters += [{"dimension": "page", "operator": "equals", "expression": u} for u in (pages or [])]
        if filters:   # all filters of one group are ANDed
            body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
        d = await self._call("POST", f"{API}/sites/{quote(site, safe='')}/searchAnalytics/query", json_body=body, what="reading search analytics")
        return {"rows": d.get("rows", []), "aggregation": d.get("responseAggregationType", "")}

    async def sitemaps(self, site: str) -> list[dict]:
        d = await self._call("GET", f"{API}/sites/{quote(site, safe='')}/sitemaps", what="listing sitemaps")
        out = []
        for s in d.get("sitemap", []):
            out.append({"path": s.get("path"), "last_submitted": s.get("lastSubmitted"), "last_downloaded": s.get("lastDownloaded"), "pending": s.get("isPending"), "is_index": s.get("isSitemapsIndex"),
                        "warnings": int(s.get("warnings", 0) or 0), "errors": int(s.get("errors", 0) or 0),
                        "contents": [{"type": c.get("type"), "submitted": int(c.get("submitted", 0) or 0), "indexed": int(c.get("indexed", 0) or 0)} for c in s.get("contents", [])]})
        return out

    async def inspect(self, site: str, url: str) -> dict:
        d = await self._call("POST", INSPECT_URL, json_body={"inspectionUrl": url, "siteUrl": site, "languageCode": "en-US"}, what="inspecting a URL")
        r = (d.get("inspectionResult") or {}).get("indexStatusResult") or {}
        return {"url": url, "verdict": r.get("verdict"), "coverage_state": r.get("coverageState"), "indexing_state": r.get("indexingState"), "robots_txt_state": r.get("robotsTxtState"),
                "page_fetch_state": r.get("pageFetchState"), "last_crawl_time": r.get("lastCrawlTime"), "crawled_as": r.get("crawledAs"), "google_canonical": r.get("googleCanonical"),
                "user_canonical": r.get("userCanonical")}
