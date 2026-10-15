"""MOCKED providers: an in-process stand-in for the Google (OAuth, Search Console, URL Inspection) and Bing Webmaster HTTP APIs.

Nothing here talks to the internet and nothing proves live connectivity: it proves that OUR client code speaks the documented request/response shapes correctly, enforces access, maps errors
and keeps credentials out of everything it records. It records only what a provider would see: method, host, path (never the query string, which carries Bing's key) and whether auth was sent.
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, unquote, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from octg_agent.connectors.http import HttpResult

SA_EMAIL = "octg-reader@octg-test-1.iam.gserviceaccount.com"
BING_KEY = "0123456789abcdef0123456789abcdef"


def write_service_account(folder, email: str = SA_EMAIL) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    folder.mkdir(parents=True, exist_ok=True)
    # token_uri deliberately points somewhere else: the client must ignore it (the token endpoint is a constant)
    (folder / "gsc_service_account.json").write_text(json.dumps({"type": "service_account", "project_id": "octg-test-1", "client_email": email, "private_key": pem, "token_uri": "https://evil.example/token"}))
    return pem


def write_oauth(folder) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gsc_oauth_token.json").write_text(json.dumps({"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-test-secret", "refresh_token": "1//refresh-test"}))


def write_bing_key(folder, key: str = BING_KEY) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bing_webmaster_api_key.txt").write_text(key)


def net_date(day: str) -> str:
    y, m, d = (int(x) for x in day.split("-"))
    return f"/Date({int(time.mktime((y, m, d, 12, 0, 0, 0, 0, 0)) * 1000)})/"


class FakeProviders:
    """Drop-in for connectors.http.Http (same `request` signature)."""

    def __init__(self):
        self.log: list[dict] = []
        self.queried: list[str] = []        # properties a data call was made for (what a provider would see being read)
        self.assertions: list[dict] = []    # claims of the service-account JWTs we sent
        self.issued: list[str] = []
        self.valid: set[str] = set()
        self.token_error: str | None = None
        self.api_disabled = False
        self.failures: list[dict] = []      # {match, status, body}: consumed once each, first match wins
        self.google_sites: dict[str, str] = {}
        self.google: dict[str, dict] = {}   # property -> {totals, queries, pages, sitemaps, inspect}
        self.bing_key: str | None = BING_KEY
        self.bing_sites: dict[str, bool] = {}
        self.bing: dict[str, dict] = {}     # url -> {queries: [...], traffic: [...]}

    # ---------------------------------------------------------------- helpers for tests
    def expire_tokens(self) -> None:
        self.valid.clear()

    def fail_next(self, match: str, status: int, body: dict | None = None) -> None:
        self.failures.append({"match": match, "status": status, "body": body or {}})

    # ---------------------------------------------------------------- the "network"
    async def request(self, method, url, *, headers=None, json_body=None, form=None) -> HttpResult:
        sp = urlsplit(url)
        host, path, q = sp.hostname, sp.path, parse_qs(sp.query)
        authed = bool((headers or {}).get("Authorization", "").startswith("Bearer ")) or "apikey" in q
        self.log.append({"method": method, "host": host, "path": unquote(path), "authed": authed})
        for i, f in enumerate(self.failures):
            if f["match"] in path:
                self.failures.pop(i)
                return HttpResult(f["status"], f["body"], {})
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._token(form or {})
        if host in ("www.googleapis.com", "searchconsole.googleapis.com"):
            return self._google(method, host, path, headers or {}, json_body or {})
        if host == "ssl.bing.com":
            return self._bing(path, q)
        return HttpResult(404, {"error": "unknown host"}, {})

    def _token(self, form: dict) -> HttpResult:
        if self.token_error:
            return HttpResult(400, {"error": self.token_error, "error_description": "Token has been expired or revoked."}, {})
        gt = form.get("grant_type")
        if gt == "urn:ietf:params:oauth:grant-type:jwt-bearer":
            self.assertions.append(jwt.decode(form["assertion"], options={"verify_signature": False}))
        elif gt == "refresh_token":
            if form.get("refresh_token") != "1//refresh-test":
                return HttpResult(400, {"error": "invalid_grant"}, {})
        else:
            return HttpResult(400, {"error": "unsupported_grant_type"}, {})
        tok = f"ya29.fake-token-{len(self.issued) + 1}"
        self.issued.append(tok)
        self.valid.add(tok)
        return HttpResult(200, {"access_token": tok, "expires_in": 3600, "token_type": "Bearer"}, {})

    def _google(self, method, host, path, headers, body) -> HttpResult:
        tok = headers.get("Authorization", "")[7:]
        if tok not in self.valid:
            return HttpResult(401, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}}, {})
        if self.api_disabled:
            return HttpResult(403, {"error": {"code": 403, "message": "Google Search Console API has not been used in project 123 before or it is disabled.", "status": "PERMISSION_DENIED",
                                              "details": [{"reason": "SERVICE_DISABLED"}]}}, {})
        if host == "www.googleapis.com" and path == "/webmasters/v3/sites":
            return HttpResult(200, {"siteEntry": [{"siteUrl": s, "permissionLevel": p} for s, p in self.google_sites.items()]}, {})
        if host == "searchconsole.googleapis.com":
            site = body.get("siteUrl", "")
            if site not in self.google_sites:
                return self._forbidden(site)
            self.queried.append(site)
            info = self.google.get(site, {}).get("inspect", {}).get(body.get("inspectionUrl"), {})
            return HttpResult(200, {"inspectionResult": {"indexStatusResult": info}} if info else {"inspectionResult": {"indexStatusResult": {"verdict": "NEUTRAL", "coverageState": "URL is unknown to Google"}}}, {})
        marker = "/webmasters/v3/sites/"
        site_enc, _, rest = path[len(marker):].partition("/")     # the property is percent-encoded in the path (URL-prefix properties contain slashes)
        site = unquote(site_enc)
        if site not in self.google_sites:
            return self._forbidden(site)
        self.queried.append(site)
        data = self.google.get(site, {})
        if rest == "sitemaps":
            return HttpResult(200, {"sitemap": data.get("sitemaps", [])}, {})
        if rest == "searchAnalytics/query":
            dims = body.get("dimensions") or []
            filt = [f for g in body.get("dimensionFilterGroups", []) for f in g.get("filters", [])]
            if not dims:
                t = data.get("totals")
                return HttpResult(200, {"rows": [t] if t else [], "responseAggregationType": "byProperty"}, {})
            src = data.get("queries", []) if dims == ["query"] else data.get("pages", []) if dims == ["page"] else data.get("dates", [])
            rows = [r for r in src if all(r["keys"][dims.index(f["dimension"])] == f["expression"] for f in filt if f["dimension"] in dims)]
            return HttpResult(200, {"rows": rows[: body.get("rowLimit", 1000)], "responseAggregationType": "auto"}, {})
        return HttpResult(404, {"error": {"code": 404, "message": "not found"}}, {})

    @staticmethod
    def _forbidden(site: str) -> HttpResult:
        return HttpResult(403, {"error": {"code": 403, "message": f"User does not have sufficient permission for site '{site}'. See also: https://support.google.com/webmasters/answer/2451999.",
                                          "status": "PERMISSION_DENIED", "errors": [{"reason": "forbidden"}]}}, {})

    def _bing(self, path: str, q: dict) -> HttpResult:
        method = path.rsplit("/", 1)[-1]
        if q.get("apikey", [None])[0] != self.bing_key:
            return HttpResult(400, {"Message": "InvalidApiKey", "ErrorCode": 3}, {})
        if method == "GetUserSites":
            return HttpResult(200, {"d": [{"Url": u, "IsVerified": v, "AuthenticationCode": "SECRET-VERIFY-CODE", "DnsVerificationCode": "SECRET-DNS-CODE"} for u, v in self.bing_sites.items()]}, {})
        site = (q.get("siteUrl") or [""])[0]
        if not self.bing_sites.get(site):
            return HttpResult(400, {"Message": "NotAuthorized", "ErrorCode": 14}, {})
        self.queried.append(site)
        d = self.bing.get(site, {})
        if method == "GetQueryStats":
            return HttpResult(200, {"d": [{"Query": r["query"], "Date": net_date(r["date"]), "Impressions": r["impressions"], "Clicks": r["clicks"], "AvgClickPosition": r.get("click_pos", -1),
                                           "AvgImpressionPosition": r["pos"]} for r in d.get("queries", [])]}, {})
        if method == "GetRankAndTrafficStats":
            return HttpResult(200, {"d": [{"Date": net_date(r["date"]), "Impressions": r["impressions"], "Clicks": r["clicks"]} for r in d.get("traffic", [])]}, {})
        if method == "GetPageStats":
            return HttpResult(200, {"d": []}, {})
        return HttpResult(404, {"Message": "unknown method"}, {})
