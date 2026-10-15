"""Search Console + Bing Webmaster connectors, competitor research, and their use inside the loop controller and the read-only audit.

EVERYTHING AGAINST GOOGLE AND BING HERE IS MOCKED (tests/connector_fakes.py): these tests prove our client code, access control, error mapping, retries and secret hygiene, not live connectivity.
The Http layer itself is exercised for real against a local server. Competitor sites are local fixture servers; the owner's Chrome is a scripted fake extension.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCTG_LOOP_TICK_S", "3600")

from aiohttp import web
from aiohttp.test_utils import TestServer

from octg_agent.collab.auth import Callers
from octg_agent.collab.store import CollabStore
from octg_agent.connectors import ConnectorError, domain_of, scrub
from octg_agent.connectors.bing import BingWebmaster, bing_date
from octg_agent.connectors.gsc import GscClient
from octg_agent.connectors.hub import ConnectorHub, ConnectorStore
from octg_agent.connectors.http import Http
from octg_agent.hub.security import HubPin
from octg_agent.seo import fetch as F

from .connector_fakes import BING_KEY, SA_EMAIL, FakeProviders, write_bing_key, write_oauth, write_service_account
from .test_collab import Collab, CollabCase, HITS, fixture_site
from .test_collab_loops import Clock, serp_world

GSC, BING = "google_search_console", "bing_webmaster_tools"
A_SITE, B_SITE = "sc-domain:alpha-plumbing.test", "https://www.beta-roofing.test/"
SECRETS_TO_KEEP_OUT = ["GOCSPX-test-secret", "1//refresh-test", BING_KEY, "SECRET-VERIFY-CODE", "SECRET-DNS-CODE", "BEGIN PRIVATE KEY", "ya29.fake-token"]


# =================================================================================================================  the HTTP layer (REAL requests to a local server)
class HttpLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hits = {"flaky": 0, "always": 0, "rate": 0, "capped": 0, "redirected": 0}
        app = web.Application()

        async def ok(r):
            return web.json_response({"ok": True})

        async def flaky(r):
            self.hits["flaky"] += 1
            return web.json_response({"n": self.hits["flaky"]}, status=503 if self.hits["flaky"] < 3 else 200)

        async def always(r):
            self.hits["always"] += 1
            return web.Response(status=503, text="down")

        async def rate(r):
            self.hits["rate"] += 1
            return web.Response(status=429, headers={"Retry-After": "2"}) if self.hits["rate"] == 1 else web.json_response({"ok": True})

        async def capped(r):
            self.hits["capped"] += 1
            return web.Response(status=429, headers={"Retry-After": "999"}) if self.hits["capped"] == 1 else web.json_response({"ok": True})

        async def redirect(r):
            self.hits["redirected"] += 1
            raise web.HTTPFound("http://127.0.0.1:1/elsewhere")

        async def big(r):
            return web.Response(body=b"x" * 5000)

        for path, fn in (("/ok", ok), ("/flaky", flaky), ("/always", always), ("/rate", rate), ("/capped", capped), ("/redirect", redirect), ("/big", big)):
            app.router.add_get(path, fn)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.sleeps: list[float] = []

        async def fake_sleep(s):
            self.sleeps.append(s)
        self.http = Http(allowed_hosts={"127.0.0.1"}, schemes=("http",), sleep=fake_sleep, max_retries=2, base_delay=0.5, max_delay=10, max_bytes=2000, timeout=3)

    async def asyncTearDown(self):
        await self.server.close()

    def test_only_the_providers_fixed_https_hosts_can_be_reached_by_default(self):
        h = Http()
        for bad in ("http://www.googleapis.com/x", "https://evil.example/token", "https://user:pw@www.googleapis.com/x", "https://www.googleapis.com.evil.example/x", "ftp://www.googleapis.com/x", "https://127.0.0.1/x"):
            with self.assertRaises(ConnectorError, msg=bad) as cm:
                h.check(bad)
            self.assertEqual(cm.exception.code, "BLOCKED_HOST")
        for good in ("https://www.googleapis.com/webmasters/v3/sites", "https://oauth2.googleapis.com/token", "https://searchconsole.googleapis.com/v1/x", "https://ssl.bing.com/webmaster/api.svc/json/GetUserSites?apikey=k"):
            h.check(good)

    async def test_a_good_answer_is_returned_without_any_retry(self):
        r = await self.http.request("GET", self.base + "/ok")
        self.assertEqual((r.status, r.data, self.http.attempts, self.sleeps), (200, {"ok": True}, 1, []))

    async def test_server_errors_are_retried_a_bounded_number_of_times_with_backoff(self):
        r = await self.http.request("GET", self.base + "/flaky")
        self.assertEqual((r.status, self.hits["flaky"], self.sleeps), (200, 3, [0.5, 1.0]))
        r = await self.http.request("GET", self.base + "/always")
        self.assertEqual((r.status, self.hits["always"]), (503, 3), "after the bounded retries the last answer is returned for the caller to name")

    async def test_retry_after_is_honoured_and_capped(self):
        await self.http.request("GET", self.base + "/rate")
        self.assertEqual(self.sleeps, [2.0])
        self.sleeps.clear()
        await self.http.request("GET", self.base + "/capped")
        self.assertEqual(self.sleeps, [10.0], "a provider cannot make the agent wait for 999 seconds")

    async def test_a_redirect_is_never_followed_because_it_would_carry_the_authorization_header(self):
        with self.assertRaises(ConnectorError) as cm:
            await self.http.request("GET", self.base + "/redirect", headers={"Authorization": "Bearer x"})
        self.assertEqual((cm.exception.code, self.hits["redirected"]), ("REDIRECT_REFUSED", 1))

    async def test_an_oversized_answer_is_refused(self):
        with self.assertRaises(ConnectorError) as cm:
            await self.http.request("GET", self.base + "/big")
        self.assertEqual(cm.exception.code, "RESPONSE_TOO_LARGE")

    async def test_an_unreachable_api_is_a_retryable_named_error_after_the_bounded_attempts(self):
        http = Http(allowed_hosts={"127.0.0.1"}, schemes=("http",), sleep=self.http.sleep, max_retries=2, timeout=2)
        with self.assertRaises(ConnectorError) as cm:
            await http.request("GET", "http://127.0.0.1:1/x")
        self.assertEqual((cm.exception.code, cm.exception.retryable, http.attempts), ("API_UNREACHABLE", True, 3))
        self.assertNotIn("127.0.0.1:1", cm.exception.message, "only the exception type is reported")


# =================================================================================================================  credentials
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name) / "connectors"
        self.fake = FakeProviders()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_credentials_say_exactly_what_to_put_where(self):
        for cli, names in ((GscClient(self.folder, self.fake), ("gsc_service_account.json", "gsc_oauth_token.json")), (BingWebmaster(self.folder, self.fake), ("bing_webmaster_api_key.txt",))):
            c = cli.configured()
            self.assertEqual((c["configured"], c["code"]), (False, "NOT_CONNECTED"))
            self.assertIn(str(self.folder), c["problem"])
            for n in names:
                self.assertIn(n, c["problem"])

    def test_malformed_credentials_are_named_not_crashed_on(self):
        self.folder.mkdir(parents=True)
        (self.folder / "gsc_service_account.json").write_text("{not json")
        self.assertEqual(GscClient(self.folder, self.fake).configured()["code"], "CREDENTIALS_INVALID")
        (self.folder / "gsc_service_account.json").write_text(json.dumps({"type": "service_account", "client_email": "a@b.iam.gserviceaccount.com"}))
        c = GscClient(self.folder, self.fake).configured()
        self.assertIn("private_key", c["problem"])
        (self.folder / "gsc_service_account.json").write_text(json.dumps({"type": "authorized_user", "client_email": "a", "private_key": "b"}))
        self.assertIn("not a service-account key", GscClient(self.folder, self.fake).configured()["problem"])
        (self.folder / "bing_webmaster_api_key.txt").write_text("not a key !!")
        self.assertEqual(BingWebmaster(self.folder, self.fake).configured()["code"], "CREDENTIALS_INVALID")
        write_bing_key(self.folder)
        self.assertTrue(BingWebmaster(self.folder, self.fake).configured()["configured"])

    def test_scrub_removes_anything_that_looks_like_a_credential(self):
        raw = ('GET https://ssl.bing.com/x?siteUrl=a&apikey=0123456789abcdef0123456789abcdef failed; Authorization: Bearer ya29.a0AfH6SMBx1234567890; '
               '{"refresh_token": "1//0gabc", "client_secret": "GOCSPX-zzz", "private_key": "-----BEGIN"}')
        out = scrub(raw)
        for s in ("0123456789abcdef0123456789abcdef", "ya29.a0AfH6SMBx1234567890", "1//0gabc", "GOCSPX-zzz"):
            self.assertNotIn(s, out)
        self.assertIn("[removed]", out)

    def test_status_never_contains_a_credential(self):
        write_service_account(self.folder)
        write_bing_key(self.folder)
        write_oauth(self.folder)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            hub = ConnectorHub(self.folder, ConnectorStore(CollabStore(Path(d) / "c.db")), http=self.fake)
            text = json.dumps(hub.status())
        for s in SECRETS_TO_KEEP_OUT:
            self.assertNotIn(s, text)
        self.assertIn(SA_EMAIL, text, "the service account's e-mail is not secret and is what the owner must add to each property")

    def test_domain_of_understands_both_property_forms(self):
        self.assertEqual([domain_of(x) for x in ("sc-domain:Example.com", "https://www.example.com/path/", "http://example.com:8080/", "example.com", "https://sub.example.com/")],
                         ["example.com", "example.com", "example.com", "example.com", "sub.example.com"])


# =================================================================================================================  Google Search Console client
class GscClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name)
        self.fake = FakeProviders()
        self.fake.google_sites = {A_SITE: "siteOwner"}
        self.fake.google = {A_SITE: {"totals": {"clicks": 120, "impressions": 4000, "ctr": 0.03, "position": 18.4},
                                     "queries": [{"keys": ["drain cleaning"], "clicks": 40, "impressions": 900, "ctr": 0.044, "position": 9.6}, {"keys": ["plumber"], "clicks": 30, "impressions": 1500, "ctr": 0.02, "position": 14.1}],
                                     "pages": [{"keys": ["https://alpha-plumbing.test/"], "clicks": 70, "impressions": 2000, "ctr": 0.035, "position": 12.0}],
                                     "sitemaps": [{"path": "https://alpha-plumbing.test/sitemap.xml", "lastDownloaded": "2026-10-01T00:00:00Z", "isSitemapsIndex": False, "warnings": "0", "errors": "1",
                                                   "contents": [{"type": "web", "submitted": "20", "indexed": "17"}]}],
                                     "inspect": {"https://alpha-plumbing.test/": {"verdict": "PASS", "coverageState": "Submitted and indexed", "lastCrawlTime": "2026-10-01T00:00:00Z",
                                                                                   "googleCanonical": "https://alpha-plumbing.test/", "userCanonical": "https://alpha-plumbing.test/"}}}}
        self.clock = Clock()
        write_service_account(self.folder)
        self.cli = GscClient(self.folder, self.fake, clock=self.clock)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_a_service_account_signs_a_correct_request_for_the_fixed_token_endpoint_only(self):
        sites = await self.cli.list_sites()
        self.assertEqual(sites, [{"property": A_SITE, "permission": "siteOwner"}])
        claims = self.fake.assertions[0]
        self.assertEqual((claims["iss"], claims["scope"], claims["aud"]), (SA_EMAIL, "https://www.googleapis.com/auth/webmasters.readonly", "https://oauth2.googleapis.com/token"))
        self.assertEqual({e["host"] for e in self.fake.log}, {"oauth2.googleapis.com", "www.googleapis.com"}, "the token_uri in the key file (evil.example) was ignored")
        self.assertTrue(all(e["authed"] for e in self.fake.log if e["host"] == "www.googleapis.com"))

    async def test_the_access_token_is_cached_until_it_nearly_expires(self):
        for _ in range(3):
            await self.cli.list_sites()
        self.assertEqual(len(self.fake.issued), 1)
        self.clock.t += 3600
        await self.cli.list_sites()
        self.assertEqual(len(self.fake.issued), 2)

    async def test_a_token_that_expired_early_is_replaced_once_and_the_call_succeeds(self):
        await self.cli.list_sites()
        self.fake.expire_tokens()
        self.assertEqual(len(await self.cli.list_sites()), 1)
        self.assertEqual(len(self.fake.issued), 2)

    async def test_the_oauth_refresh_token_flow_works_and_a_wrong_token_is_an_auth_failure(self):
        for f in ("gsc_service_account.json",):
            (self.folder / f).unlink()
        write_oauth(self.folder)
        self.assertEqual((await self.cli.list_sites())[0]["property"], A_SITE)
        (self.folder / "gsc_oauth_token.json").write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "wrong"}))
        cli = GscClient(self.folder, self.fake, clock=self.clock)
        with self.assertRaises(ConnectorError) as cm:
            await cli.list_sites()
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("AUTH_FAILED", False))

    async def test_revoked_credentials_are_an_auth_failure_that_does_not_leak_anything(self):
        self.fake.token_error = "invalid_grant"
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.list_sites()
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("AUTH_FAILED", False))
        self.assertIn("revoked", cm.exception.message)
        self.assertFalse(any(t in cm.exception.message for t in self.fake.issued))

    async def test_each_failure_has_its_own_name_and_retryability(self):
        cases = [(lambda: setattr(self.fake, "api_disabled", True), "API_DISABLED", False), (None, "UNAUTHORIZED_PROPERTY", False)]
        self.fake.api_disabled = True
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.list_sites()
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("API_DISABLED", False))
        self.assertIn("not enabled", cm.exception.message)
        self.fake.api_disabled = False
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query("sc-domain:other.test", start="2026-09-01", end="2026-09-28")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("UNAUTHORIZED_PROPERTY", False))
        self.fake.fail_next("searchAnalytics", 429, {"error": {"code": 429, "message": "Quota exceeded"}})
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query(A_SITE, start="2026-09-01", end="2026-09-28")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("RATE_LIMITED", True))
        self.fake.fail_next("searchAnalytics", 503, {})
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query(A_SITE, start="2026-09-01", end="2026-09-28")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("API_ERROR", True))
        self.fake.fail_next("searchAnalytics", 400, {"error": {"code": 400, "message": "Invalid value for startDate"}})
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query(A_SITE, start="x", end="y")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("BAD_REQUEST", False))
        self.fake.fail_next("sitemaps", 404, {"error": {"code": 404, "message": "Not found"}})
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.sitemaps(A_SITE)
        self.assertEqual(cm.exception.code, "PROPERTY_NOT_FOUND")

    async def test_search_analytics_returns_totals_rows_and_filters(self):
        start, end = self.cli.window(28, 3, today=date(2026, 10, 9))
        self.assertEqual((start, end), ("2026-09-09", "2026-10-06"), "28 days (inclusive) ending three days ago: recent days are incomplete")
        tot = await self.cli.query(A_SITE, start=start, end=end)
        self.assertEqual(tot["rows"][0]["clicks"], 120)
        rows = await self.cli.query(A_SITE, start=start, end=end, dimensions=["query"], row_limit=50)
        self.assertEqual([r["keys"] for r in rows["rows"]], [["drain cleaning"], ["plumber"]])
        one = await self.cli.query(A_SITE, start=start, end=end, dimensions=["query"], queries=["plumber"])
        self.assertEqual([r["keys"] for r in one["rows"]], [["plumber"]])
        pages = await self.cli.query(A_SITE, start=start, end=end, dimensions=["page"])
        self.assertEqual(pages["rows"][0]["clicks"], 70)

    async def test_sitemaps_and_url_inspection_are_normalised(self):
        sm = await self.cli.sitemaps(A_SITE)
        self.assertEqual((sm[0]["errors"], sm[0]["contents"][0]["indexed"], sm[0]["contents"][0]["submitted"]), (1, 17, 20))
        ins = await self.cli.inspect(A_SITE, "https://alpha-plumbing.test/")
        self.assertEqual((ins["verdict"], ins["coverage_state"], ins["google_canonical"]), ("PASS", "Submitted and indexed", "https://alpha-plumbing.test/"))
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.inspect("sc-domain:other.test", "https://other.test/")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED_PROPERTY")


# =================================================================================================================  Bing client
class BingClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name)
        write_bing_key(self.folder)
        self.fake = FakeProviders()
        d = date.today() - timedelta(days=5)
        self.fake.bing_sites = {"https://www.alpha-plumbing.test/": True, "https://new-unverified.test/": False}
        self.fake.bing = {"https://www.alpha-plumbing.test/": {"queries": [{"query": "drain cleaning", "date": d.isoformat(), "impressions": 50, "clicks": 4, "pos": 7.0},
                                                                          {"query": "drain cleaning", "date": (d - timedelta(days=1)).isoformat(), "impressions": 30, "clicks": 1, "pos": 9.0}],
                                                              "traffic": [{"date": d.isoformat(), "impressions": 80, "clicks": 5}]}}
        self.cli = BingWebmaster(self.folder, self.fake)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_sites_come_back_without_the_verification_codes_bing_includes(self):
        sites = await self.cli.list_sites()
        self.assertEqual({s["property"]: s["permission"] for s in sites}, {"https://www.alpha-plumbing.test/": "verified", "https://new-unverified.test/": "unverified"})
        self.assertNotIn("SECRET", json.dumps(sites))

    async def test_statistics_are_parsed_including_dotnet_dates(self):
        q = await self.cli.query_stats("https://www.alpha-plumbing.test/")
        self.assertEqual((q[0]["query"], q[0]["impressions"], q[0]["avg_impression_position"]), ("drain cleaning", 50, 7.0))
        self.assertRegex(q[0]["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual((await self.cli.traffic_stats("https://www.alpha-plumbing.test/"))[0]["clicks"], 5)
        self.assertEqual(bing_date("/Date(1700000000000-0800)/"), "2023-11-14")
        self.assertIsNone(bing_date("garbage"))

    async def test_each_failure_has_its_own_name_and_the_key_never_appears_in_an_error(self):
        self.fake.bing_key = "ffffffffffffffffffffffffffffffff"     # the account's key changed: ours is now wrong
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.list_sites()
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("AUTH_FAILED", False))
        self.assertNotIn(BING_KEY, cm.exception.message)
        self.fake.bing_key = BING_KEY
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query_stats("https://new-unverified.test/")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED_PROPERTY")
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query_stats("https://never-heard-of-it.test/")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED_PROPERTY")
        self.fake.fail_next("GetQueryStats", 400, {"Message": "ThrottleUser", "ErrorCode": 4})
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query_stats("https://www.alpha-plumbing.test/")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("RATE_LIMITED", True))
        self.fake.fail_next("GetQueryStats", 500, {})
        with self.assertRaises(ConnectorError) as cm:
            await self.cli.query_stats("https://www.alpha-plumbing.test/")
        self.assertEqual((cm.exception.code, cm.exception.retryable), ("API_ERROR", True))
        self.assertNotIn("apikey", json.dumps(self.fake.log), "the recorded requests never include the query string that carries the key")


# =================================================================================================================  mapping + isolation rules of the hub
class HubRulesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.tmp.name) / "connectors"
        self.fake = FakeProviders()
        self.clock = Clock()
        self.hub = ConnectorHub(self.folder, ConnectorStore(CollabStore(Path(self.tmp.name) / "c.db")), http=self.fake, clock=self.clock)
        self.fake.google_sites = {A_SITE: "siteOwner", B_SITE: "siteFullUser", "sc-domain:unrelated-client.test": "siteOwner", "sc-domain:lurker.test": "siteUnverifiedUser"}
        self.alpha = ["https://www.alpha-plumbing.test/"]
        self.beta = ["https://www.beta-roofing.test/"]

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_status_walks_from_no_credentials_to_verified(self):
        row = lambda: next(r for r in self.hub.status() if r["source"] == GSC)
        self.assertEqual((row()["connected"], row()["credentials_present"]), (False, False))
        write_service_account(self.folder)
        self.assertEqual((row()["connected"], row()["credentials_present"]), (False, True))
        self.assertIn("not been verified", row()["missing"])
        res = await self.hub.verify(GSC)
        self.assertEqual((res["ok"], res["properties_visible"]), (True, 4))
        self.assertEqual((row()["connected"], row()["last_verification"]["properties_visible"]), (True, 4))
        self.fake.token_error = "invalid_grant"
        with self.assertRaises(ConnectorError):
            await self.hub.verify(GSC)
        self.assertEqual((row()["connected"], row()["last_verification"]["code"]), (False, "AUTH_FAILED"))
        self.assertIn("AUTH_FAILED", row()["missing"])

    async def test_verify_without_credentials_is_a_named_error_and_is_recorded(self):
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.verify(BING)
        self.assertEqual(cm.exception.code, "NOT_CONNECTED")
        self.assertEqual(self.hub.db.state(BING)["ok"], 0)

    async def test_suggestions_show_only_properties_that_match_the_clients_own_domain(self):
        write_service_account(self.folder)
        s = await self.hub.suggest("cli_a", GSC, self.alpha)
        self.assertEqual([c["property"] for c in s["candidates"]], [A_SITE])
        text = json.dumps(s)
        for other in ("beta-roofing", "unrelated-client", "lurker"):
            self.assertNotIn(other, text, "the credential's other properties (other clients') are never revealed")
        s = await self.hub.suggest("cli_b", GSC, self.beta)
        self.assertEqual([c["property"] for c in s["candidates"]], [B_SITE])

    async def test_mapping_rules(self):
        write_service_account(self.folder)
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.map("cli_a", GSC, "sc-domain:not-visible.test", client_domains=self.alpha, mapped_by="t")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED_PROPERTY")
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.map("cli_a", GSC, "sc-domain:lurker.test", client_domains=["https://lurker.test/"], mapped_by="t")
        self.assertIn("cannot read data", cm.exception.message)
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.map("cli_a", GSC, "sc-domain:unrelated-client.test", client_domains=self.alpha, mapped_by="t")
        self.assertEqual(cm.exception.code, "DOMAIN_MISMATCH")
        self.assertEqual(self.hub.db.mappings(), [], "a refused mapping leaves nothing behind")
        m = await self.hub.map("cli_a", GSC, A_SITE, client_domains=self.alpha, mapped_by="owner:test")
        again = await self.hub.map("cli_a", GSC, A_SITE, client_domains=self.alpha, mapped_by="owner:test")
        self.assertEqual(m["map_id"], again["map_id"], "mapping the same pair twice is harmless")
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.map("cli_b", GSC, A_SITE, client_domains=self.alpha, mapped_by="t")
        self.assertEqual(cm.exception.code, "ALREADY_MAPPED")
        forced = await self.hub.map("cli_a", GSC, "sc-domain:unrelated-client.test", client_domains=self.alpha, mapped_by="owner:test", allow_mismatch=True)
        self.assertEqual(forced["domain"], "unrelated-client.test", "the owner can explicitly confirm a property whose domain differs")
        self.hub.unmap(m["map_id"])
        self.assertEqual([x["property"] for x in self.hub.db.mappings(client_id="cli_a")], ["sc-domain:unrelated-client.test"])

    async def test_resolve_names_every_reason_it_cannot_answer(self):
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.resolve("cli_a", GSC)
        self.assertEqual(cm.exception.code, "NOT_CONNECTED")
        write_service_account(self.folder)
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.resolve("cli_a", GSC)
        self.assertEqual(cm.exception.code, "NOT_MAPPED")
        self.assertEqual(self.fake.queried, [])
        await self.hub.map("cli_a", GSC, A_SITE, client_domains=self.alpha, mapped_by="t")
        res = await self.hub.resolve("cli_a", GSC)
        self.assertEqual((res.property, res.permission), (A_SITE, "siteOwner"))
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.resolve("cli_b", GSC)
        self.assertEqual(cm.exception.code, "NOT_MAPPED", "client B never gets client A's property")

    async def test_revoked_or_downgraded_access_is_detected_and_recorded(self):
        write_service_account(self.folder)
        await self.hub.map("cli_a", GSC, A_SITE, client_domains=self.alpha, mapped_by="t")
        await self.hub.resolve("cli_a", GSC)
        self.fake.google_sites[A_SITE] = "siteUnverifiedUser"
        self.hub._sites.clear()
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.resolve("cli_a", GSC)
        self.assertEqual(cm.exception.code, "UNAUTHORIZED_PROPERTY")
        self.assertIn("siteUnverifiedUser", self.hub.db.mappings(client_id="cli_a")[0]["last_error"])
        del self.fake.google_sites[A_SITE]
        self.hub._sites.clear()
        with self.assertRaises(ConnectorError) as cm:
            await self.hub.resolve("cli_a", GSC)
        self.assertEqual(cm.exception.code, "UNAUTHORIZED_PROPERTY")
        self.assertEqual(self.hub.db.mappings(client_id="cli_a")[0]["last_error"], "no longer accessible")

    async def test_the_visible_property_list_is_cached_briefly_then_refreshed(self):
        write_service_account(self.folder)
        await self.hub.map("cli_a", GSC, A_SITE, client_domains=self.alpha, mapped_by="t")
        count = lambda: sum(1 for e in self.fake.log if e["path"] == "/webmasters/v3/sites")
        before = count()
        await self.hub.resolve("cli_a", GSC)
        await self.hub.resolve("cli_a", GSC)
        self.assertEqual(count(), before, "the list fetched while mapping is reused for a few minutes")
        self.clock.t += 301
        await self.hub.resolve("cli_a", GSC)
        self.assertEqual(count(), before + 1)


# =================================================================================================================  the collaboration capabilities
class SourcesCase(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.folder = self.h.settings.data_dir / "connectors"
        self.fake = FakeProviders()
        self.hubc = self.svc.connectors
        self.hubc.http = self.fake
        write_service_account(self.folder)
        write_bing_key(self.folder)
        today = date.today()
        self.a = await self.new_client("Alpha Plumbing LLC", website="https://www.alpha-plumbing.test/")
        self.b = await self.new_client("Beta Roofing LLC", website="https://www.beta-roofing.test/")
        self.fake.google_sites = {A_SITE: "siteOwner", B_SITE: "siteFullUser", "sc-domain:unrelated-client.test": "siteOwner"}
        self.fake.google = {
            A_SITE: {"totals": {"clicks": 120, "impressions": 4000, "ctr": 0.03, "position": 18.4},
                     "queries": [{"keys": ["drain cleaning springfield il"], "clicks": 40, "impressions": 900, "ctr": 0.044, "position": 9.6}, {"keys": ["plumber springfield"], "clicks": 30, "impressions": 1500, "ctr": 0.02, "position": 14.1}],
                     "pages": [], "sitemaps": [{"path": "https://alpha-plumbing.test/sitemap.xml", "lastDownloaded": "2026-10-01T00:00:00Z", "warnings": "0", "errors": "0", "contents": [{"type": "web", "submitted": "20", "indexed": "17"}]}],
                     "inspect": {"https://www.alpha-plumbing.test/": {"verdict": "PASS", "coverageState": "Submitted and indexed", "googleCanonical": "https://www.alpha-plumbing.test/"}}},
            B_SITE: {"totals": {"clicks": 777, "impressions": 9999, "ctr": 0.07, "position": 5.5}, "queries": [{"keys": ["roof repair springfield"], "clicks": 300, "impressions": 3000, "ctr": 0.1, "position": 3.2}], "pages": [], "sitemaps": [], "inspect": {}}}
        d = (today - timedelta(days=5)).isoformat()
        self.fake.bing_sites = {"https://www.alpha-plumbing.test/": True, "https://www.beta-roofing.test/": True}
        self.fake.bing = {"https://www.alpha-plumbing.test/": {"queries": [{"query": "drain cleaning springfield il", "date": d, "impressions": 50, "clicks": 4, "pos": 7.0},
                                                                          {"query": "drain cleaning springfield il", "date": (today - timedelta(days=6)).isoformat(), "impressions": 30, "clicks": 1, "pos": 9.0},
                                                                          {"query": "drain cleaning springfield il", "date": (today - timedelta(days=90)).isoformat(), "impressions": 999, "clicks": 99, "pos": 1.0}],
                                                              "traffic": [{"date": d, "impressions": 80, "clicks": 5}, {"date": (today - timedelta(days=90)).isoformat(), "impressions": 999, "clicks": 99}]},
                          "https://www.beta-roofing.test/": {"queries": [{"query": "roof repair", "date": d, "impressions": 500, "clicks": 50, "pos": 2.0}], "traffic": [{"date": d, "impressions": 500, "clicks": 50}]}}
        self.alpha = ["https://www.alpha-plumbing.test/"]
        self.beta = ["https://www.beta-roofing.test/"]

    async def map_alpha(self, gsc=True, bing=True):
        if gsc:
            await self.hubc.map(self.a, GSC, A_SITE, client_domains=self.alpha, mapped_by="owner:test")
        if bing:
            await self.hubc.map(self.a, BING, "https://www.alpha-plumbing.test/", client_domains=self.alpha, mapped_by="owner:test")

    async def ok(self, cap, params=None, **kw):
        r = await self.c.run(cap, params or {}, wait=60, **kw)
        self.assertEqual(r["status"], "succeeded", (cap, r.get("error")))
        return r

    async def err(self, cap, params=None, **kw):
        """The error of a request that must fail: refused at submission (validation, permission, scope: an HTTP error) or failed while running."""
        st, d = await self.c.submit(cap, params or {}, **kw)
        if st not in (200, 202):
            return d["error"]
        r = d["response"]
        end = time.time() + 60
        while r["status"] not in ("succeeded", "failed", "cancelled", "timed_out", "rejected") and time.time() < end:
            _, g = await self.c.call("GET", f"/api/collab/requests/{r['request_id']}?wait=5")
            r = g["response"]
        self.assertEqual(r["status"], "failed", (cap, r.get("result")))
        return r["error"]


class StatusAndVerifyTests(SourcesCase):
    async def test_status_is_offline_honest_and_per_client(self):
        r = await self.ok("connectors.status")
        rows = {s["source"]: s for s in r["result"]["sources"]}
        self.assertEqual((rows[GSC]["connected"], rows[GSC]["credentials_present"]), (False, True))
        self.assertIn("not been verified", rows[GSC]["missing"])
        self.assertFalse(rows["google_analytics_4"]["connected"])
        self.assertEqual(self.fake.log, [], "status never contacts a provider")
        for s in SECRETS_TO_KEEP_OUT:
            self.assertNotIn(s, json.dumps(r))
        await self.ok("connectors.verify", {"source": GSC})
        await self.map_alpha(bing=False)
        mine = {s["source"]: s for s in (await self.ok("connectors.status", client=self.a))["result"]["sources"]}
        self.assertEqual((mine[GSC]["connected"], mine[GSC]["client_mapping"]["ready"], mine[GSC]["client_mapping"]["properties"]), (True, True, [A_SITE]))
        self.assertEqual(mine[BING]["client_mapping"]["ready"], False)
        theirs = {s["source"]: s for s in (await self.ok("connectors.status", client=self.b))["result"]["sources"]}
        self.assertEqual((theirs[GSC]["client_mapping"]["mapped"], theirs[GSC]["client_mapping"]["ready"]), (False, False))
        self.assertIn("no property is mapped", theirs[GSC]["client_mapping"]["why_not"])
        self.assertNotIn(A_SITE, json.dumps(theirs), "another client's property is not shown")

    async def test_verify_succeeds_and_each_failure_is_named(self):
        r = await self.ok("connectors.verify", {"source": GSC})
        self.assertEqual((r["result"]["ok"], r["result"]["properties_visible"]), (True, 3))
        self.assertNotIn("sc-domain", json.dumps(r["result"]), "a count, never the property names")
        self.fake.token_error = "invalid_grant"
        e = await self.err("connectors.verify", {"source": GSC})
        self.assertEqual((e["code"], e["retryable"]), ("AUTH_FAILED", False))
        self.fake.token_error = None
        self.fake.fail_next("/webmasters/v3/sites", 503, {})
        e = await self.err("connectors.verify", {"source": GSC})
        self.assertEqual((e["code"], e["retryable"]), ("API_ERROR", True))
        (self.folder / "bing_webmaster_api_key.txt").unlink()
        e = await self.err("connectors.verify", {"source": BING})
        self.assertEqual(e["code"], "NOT_CONNECTED")
        e = await self.err("connectors.verify", {"source": "nonsense"})
        self.assertEqual(e["code"], "INVALID_PARAMS")


class GscCapabilityTests(SourcesCase):
    async def test_search_analytics_returns_the_mapped_clients_data_with_standard_observations_and_evidence(self):
        await self.map_alpha(bing=False)
        r = await self.ok("gsc.search_analytics", {"keywords": ["Drain Cleaning Springfield IL", "emergency plumber"], "days": 28}, client=self.a)
        res = r["result"]
        self.assertEqual((res["property"], res["totals"]["clicks"], res["totals"]["impressions"]), (A_SITE, 120, 4000))
        self.assertEqual(res["window"]["days"], 28)
        kws = {k["keyword"]: k for k in res["keywords"]}
        self.assertEqual((kws["Drain Cleaning Springfield IL"]["found"], kws["Drain Cleaning Springfield IL"]["position"]), (True, 9.6))
        self.assertEqual(kws["emergency plumber"]["found"], False)
        obs = {o["key"]: o for o in res["observations"]}
        self.assertEqual((obs["gsc|site|clicks"]["status"], obs["gsc|site|clicks"]["value"]), ("found", 120.0))
        self.assertEqual(obs["gsc|query|drain cleaning springfield il|position"]["value"], 9.6)
        missing = obs["gsc|query|emergency plumber|position"]
        self.assertEqual((missing["status"], missing["comparable"]), ("unavailable", False))
        self.assertIn("not proof of absence", missing["limits"][0], "a query with no row is NOT reported as 'no impressions'")
        self.assertTrue(r["evidence"] and r["evidence"][0]["sha256"])
        self.assertTrue(any("Search Console's average position" in l for l in r["limitations"]))
        perf = [o for o in self.svc.admin().research.observations(self.a, kind="search_performance", limit=50)]
        self.assertTrue(perf and all(json.loads(o["data"])["request_id"] == r["request_id"] for o in perf))

    async def test_an_unmapped_client_is_refused_before_anything_is_queried(self):
        await self.map_alpha(bing=False)
        e = await self.err("gsc.search_analytics", {}, client=self.b)
        self.assertEqual(e["code"], "NOT_MAPPED")
        self.assertEqual(self.fake.queried, [], "no data call was made for client B")

    async def test_each_client_only_ever_sees_its_own_property_and_a_property_has_one_owner(self):
        await self.map_alpha(bing=False)
        await self.hubc.map(self.b, GSC, B_SITE, client_domains=self.beta, mapped_by="owner:test")
        ra = await self.ok("gsc.search_analytics", {}, client=self.a)
        rb = await self.ok("gsc.search_analytics", {}, client=self.b)
        self.assertEqual((ra["result"]["totals"]["clicks"], rb["result"]["totals"]["clicks"]), (120, 777))
        self.assertEqual(self.fake.queried, [A_SITE, A_SITE, B_SITE, B_SITE])
        self.assertNotIn("777", json.dumps(ra["result"]))
        self.assertNotIn("120", json.dumps(rb["result"]["totals"]))
        with self.assertRaises(ConnectorError) as cm:
            await self.hubc.map(self.b, GSC, A_SITE, client_domains=self.alpha, mapped_by="owner:test")
        self.assertEqual(cm.exception.code, "ALREADY_MAPPED")

    async def test_the_caller_cannot_name_a_property_or_widen_the_request(self):
        await self.map_alpha(bing=False)
        for bad in ({"property": B_SITE}, {"site": B_SITE}, {"client": self.b}):
            e = await self.err("gsc.search_analytics", bad, client=self.a)
            self.assertEqual(e["code"], "INVALID_PARAMS", bad)
        self.assertEqual(self.fake.queried, [])
        tid = (await self.c.call("POST", "/api/collab/tasks", {"title": "x", "client": self.b}))[1]["task"]["task_id"]
        e = await self.err("gsc.search_analytics", {}, client=self.a, task_id=tid)
        self.assertEqual(e["code"], "SCOPE_MISMATCH", "a task of client B cannot read client A's data")

    async def test_revoked_access_is_reported_as_unauthorized_property(self):
        await self.map_alpha(bing=False)
        await self.ok("gsc.search_analytics", {}, client=self.a)
        del self.fake.google_sites[A_SITE]
        self.hubc._sites.clear()
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual((e["code"], e["retryable"]), ("UNAUTHORIZED_PROPERTY", False))

    async def test_api_failures_are_named_and_retryable_only_when_retrying_can_help(self):
        await self.map_alpha(bing=False)
        self.fake.fail_next("searchAnalytics", 429, {"error": {"code": 429, "message": "Quota exceeded"}})
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual((e["code"], e["retryable"]), ("RATE_LIMITED", True))
        self.fake.fail_next("searchAnalytics", 503, {})
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual((e["code"], e["retryable"]), ("API_ERROR", True))
        self.fake.api_disabled = True
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual((e["code"], e["retryable"]), ("API_DISABLED", False))
        self.fake.api_disabled = False
        self.fake.token_error = "invalid_grant"
        self.hubc._sites.clear()
        self.hubc.client(GSC)._token = None
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual((e["code"], e["retryable"]), ("AUTH_FAILED", False))

    async def test_no_credentials_means_not_connected(self):
        await self.map_alpha(bing=False)
        (self.folder / "gsc_service_account.json").unlink()
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual(e["code"], "NOT_CONNECTED")
        self.assertIn("gsc_service_account.json", e["message"])

    async def test_an_empty_window_is_reported_as_no_data_not_as_a_ranking(self):
        await self.map_alpha(bing=False)
        self.fake.google[A_SITE]["totals"] = None
        r = await self.ok("gsc.search_analytics", {"keywords": ["plumber springfield"]}, client=self.a)
        obs = {o["key"]: o for o in r["result"]["observations"]}
        self.assertEqual((obs["gsc|site|impressions"]["value"], obs["gsc|site|position"]["status"]), (0.0, "unavailable"))
        self.assertTrue(any("no impressions" in l for l in r["limitations"]))

    async def test_sitemaps_and_url_inspection_stay_inside_the_mapped_property(self):
        await self.map_alpha(bing=False)
        r = await self.ok("gsc.sitemaps", {}, client=self.a)
        self.assertEqual(r["result"]["sitemaps"][0]["contents"][0]["indexed"], 17)
        r = await self.ok("gsc.inspect_url", {"urls": ["https://www.alpha-plumbing.test/", "https://www.beta-roofing.test/", "https://evil.example/alpha-plumbing.test"]}, client=self.a)
        self.assertEqual([x["url"] for x in r["result"]["results"]], ["https://www.alpha-plumbing.test/"])
        self.assertEqual({e["error"] for e in r["result"]["errors"]}, {"URL_NOT_IN_PROPERTY"})
        self.assertEqual(r["completeness"], "partial")
        self.assertEqual(self.fake.queried.count(B_SITE), 0)
        e = await self.err("gsc.inspect_url", {"urls": ["https://www.beta-roofing.test/"]}, client=self.a)
        self.assertEqual(e["code"], "URL_NOT_IN_PROPERTY")


class BingCapabilityTests(SourcesCase):
    async def test_webmaster_stats_filters_to_the_window_and_weights_positions_by_impressions(self):
        await self.map_alpha(gsc=False)
        r = await self.ok("bing.webmaster_stats", {"days": 28, "keywords": ["drain cleaning springfield il", "nothing here"]}, client=self.a)
        res = r["result"]
        self.assertEqual((res["totals"]["clicks"], res["totals"]["impressions"]), (5, 80), "the 90-day-old rows are outside the window")
        k = {x["keyword"]: x for x in res["keywords"]}
        self.assertEqual((k["drain cleaning springfield il"]["impressions"], k["drain cleaning springfield il"]["avg_impression_position"]), (80, 7.75))
        self.assertFalse(k["nothing here"]["found"])
        obs = {o["key"]: o for o in res["observations"]}
        self.assertEqual(obs["bing|query|drain cleaning springfield il|position"]["value"], 7.75)
        self.assertEqual(obs["bing|query|nothing here|position"]["status"], "unavailable")
        self.assertTrue(r["evidence"])

    async def test_unmapped_unauthorized_and_failing_bing_are_named(self):
        e = await self.err("bing.webmaster_stats", {}, client=self.a)
        self.assertEqual(e["code"], "NOT_MAPPED")
        await self.map_alpha(gsc=False)
        self.fake.bing_sites["https://www.alpha-plumbing.test/"] = False
        self.hubc._sites.clear()
        e = await self.err("bing.webmaster_stats", {}, client=self.a)
        self.assertEqual(e["code"], "UNAUTHORIZED_PROPERTY")
        self.fake.bing_sites["https://www.alpha-plumbing.test/"] = True
        self.hubc._sites.clear()
        self.fake.fail_next("GetQueryStats", 400, {"Message": "ThrottleUser", "ErrorCode": 4})
        e = await self.err("bing.webmaster_stats", {}, client=self.a)
        self.assertEqual((e["code"], e["retryable"]), ("RATE_LIMITED", True))
        self.assertEqual(self.fake.queried.count("https://www.beta-roofing.test/"), 0)

    async def test_bing_isolation_between_clients(self):
        await self.map_alpha(gsc=False)
        await self.hubc.map(self.b, BING, "https://www.beta-roofing.test/", client_domains=self.beta, mapped_by="owner:test")
        ra = await self.ok("bing.webmaster_stats", {}, client=self.a)
        rb = await self.ok("bing.webmaster_stats", {}, client=self.b)
        self.assertEqual((ra["result"]["totals"]["clicks"], rb["result"]["totals"]["clicks"]), (5, 50))


class SuggestAndPermissionTests(SourcesCase):
    async def test_the_collaborator_is_only_ever_shown_the_clients_own_candidates(self):
        r = await self.ok("connectors.suggest_mapping", {"source": GSC}, client=self.a)
        self.assertEqual([c["property"] for c in r["result"]["candidates"]], [A_SITE])
        text = json.dumps(r["result"])
        for other in ("beta-roofing", "unrelated-client"):
            self.assertNotIn(other, text)
        self.assertEqual(r["result"]["mapped"], [])
        await self.map_alpha(bing=False)
        r = await self.ok("connectors.suggest_mapping", {"source": GSC}, client=self.a)
        self.assertEqual((r["result"]["mapped"], r["result"]["candidates"][0]["mapped_to_this_client"]), ([A_SITE], True))
        r = await self.ok("connectors.suggest_mapping", {"source": GSC}, client=self.b)
        self.assertEqual([c["property"] for c in r["result"]["candidates"]], [B_SITE])
        self.assertFalse(r["result"]["candidates"][0]["mapped_to_another_client"])
        self.assertTrue([c for c in self.svc.connectors.db.mappings()], "mapping is owner-made: there is no capability that creates one")
        from octg_agent.collab.capabilities import REGISTRY
        self.assertEqual([n for n in REGISTRY if n.split(".")[-1] in ("map", "unmap", "set_mapping", "map_property", "create_mapping", "remove_mapping")], [])

    async def test_a_caller_without_the_connector_permission_is_refused_and_standard_callers_are_upgraded_once(self):
        grants = tuple(g for g in self.svc.callers.list()[0]["grants"] if g != "connector:read")
        self.svc.callers.set_grants("collaborator", grants)
        e = await self.err("gsc.search_analytics", {}, client=self.a)
        self.assertEqual(e["code"], "PERMISSION_DENIED")
        r = await self.c.run("connectors.status", {})          # agent:read only: still fine
        self.assertEqual(r["status"], "succeeded")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            import hashlib
            from octg_agent.collab.capabilities import PERMISSIONS
            std = [p for p in PERMISSIONS if p not in ("agent:task", "test:internal", "connector:read")]
            restricted = ["agent:read", "clients:list"]
            (Path(d) / "collab.json").write_text(json.dumps({"callers": {"collaborator": {"sha256": hashlib.sha256(b"a").hexdigest(), "grants": std, "created_at": 1, "disabled": False},
                                                                          "narrow": {"sha256": hashlib.sha256(b"b").hexdigest(), "grants": restricted, "created_at": 1, "disabled": False}}}))
            (Path(d) / "collab_collaborator.token").write_text("a")
            cs = Callers(Path(d))
            cs.ensure()
            got = {c["name"]: c["grants"] for c in cs.list()}
            self.assertIn("connector:read", got["collaborator"])
            self.assertEqual(got["narrow"], restricted, "a deliberately restricted caller is not widened")
            cs.set_grants("collaborator", tuple(g for g in got["collaborator"] if g != "connector:read"))
            Callers(Path(d)).ensure()
            self.assertNotIn("connector:read", {c["name"]: c["grants"] for c in Callers(Path(d)).list()}["collaborator"], "the upgrade happens once, never undoing an owner's later choice")


class SecretHygieneTests(SourcesCase):
    async def test_no_credential_or_token_reaches_the_database_evidence_audit_or_any_response(self):
        await self.map_alpha()
        await self.ok("connectors.verify", {"source": GSC})
        await self.ok("connectors.verify", {"source": BING})
        results = [await self.ok("gsc.search_analytics", {"keywords": ["plumber springfield"]}, client=self.a), await self.ok("gsc.sitemaps", {}, client=self.a),
                   await self.ok("bing.webmaster_stats", {}, client=self.a), await self.ok("connectors.suggest_mapping", {"source": BING}, client=self.a)]
        self.fake.fail_next("searchAnalytics", 503, {})
        await self.err("gsc.search_analytics", {}, client=self.a)
        blob = json.dumps(results) + json.dumps(await self.ok("connectors.status", client=self.a))
        import sqlite3
        con = sqlite3.connect(self.svc.store.path)
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            blob += json.dumps(con.execute(f"SELECT * FROM {t}").fetchall(), default=str)
        con.close()
        biz = sqlite3.connect(self.h.settings.data_dir / "business.db")
        for (t,) in biz.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            blob += json.dumps(biz.execute(f"SELECT * FROM {t}").fetchall(), default=str)
        biz.close()
        for f in self.svc.store.evidence_dir.rglob("*"):
            if f.is_file():
                blob += f.read_text(encoding="utf-8", errors="replace")
        for s in SECRETS_TO_KEEP_OUT:
            self.assertNotIn(s, blob, s)
        self.assertNotIn("apikey", json.dumps(self.fake.log))


class HubDataSourceTests(SourcesCase):
    async def test_the_owner_sees_status_properties_and_maps_only_with_the_pin(self):
        st, v = await self.hub.get("/connectors")
        self.assertEqual(st, 200)
        gsc = next(s for s in v["sources"] if s["source"] == GSC)
        self.assertEqual((gsc["credentials_present"], gsc["connected"], gsc["identity"]), (True, False, SA_EMAIL))
        for s in SECRETS_TO_KEEP_OUT:
            self.assertNotIn(s, json.dumps(v))
        st, d = await self.hub.post(f"/connectors/{GSC}/verify")
        self.assertEqual((st, d["result"]["properties_visible"]), (200, 3))
        st, p = await self.hub.get(f"/connectors/{GSC}/properties?client={self.a}")
        props = {x["property"]: x for x in p["properties"]}
        self.assertEqual(len(props), 3, "the OWNER sees everything the credential can see")
        self.assertEqual((props[A_SITE]["matches_client"], props[B_SITE]["matches_client"]), (True, False))
        body = {"client_id": self.a, "source": GSC, "property": A_SITE}
        st, d = await self.hub.post("/connectors/map", body)
        self.assertEqual((st, d["code"]), (409, "NO_PIN_SET"))
        HubPin(self.h.settings.data_dir).set("4711")
        st, d = await self.hub.post("/connectors/map", body)
        self.assertEqual((st, d["code"]), (401, "BAD_PIN"))
        st, d = await self.hub.post("/connectors/map", {**body, "pin": "0000"})
        self.assertEqual(st, 401)
        self.assertEqual(self.hubc.db.mappings(), [], "nothing was mapped by the refused attempts")
        st, d = await self.hub.post("/connectors/map", {**body, "pin": "4711"})
        self.assertEqual((st, d["mapping"]["property"]), (200, A_SITE))
        st, d = await self.hub.post("/connectors/map", {"client_id": self.b, "source": GSC, "property": A_SITE, "pin": "4711"})
        self.assertEqual((st, d["code"]), (409, "DOMAIN_MISMATCH"))
        st, d = await self.hub.post("/connectors/map", {"client_id": self.b, "source": GSC, "property": A_SITE, "pin": "4711", "allow_mismatch": True})
        self.assertEqual((st, d["code"]), (409, "ALREADY_MAPPED"), "even the owner's override cannot give one property to two clients")
        st, d = await self.hub.post("/connectors/map", {"client_id": self.b, "source": GSC, "property": "sc-domain:unrelated-client.test", "pin": "4711"})
        self.assertEqual((st, d["code"]), (409, "DOMAIN_MISMATCH"))
        st, d = await self.hub.post("/connectors/map", {"client_id": self.b, "source": GSC, "property": "sc-domain:unrelated-client.test", "pin": "4711", "allow_mismatch": True})
        self.assertEqual(st, 200, "the owner can confirm a property whose domain differs")
        mid = self.hubc.db.mappings(client_id=self.a)[0]["map_id"]
        st, d = await self.hub.post("/connectors/unmap", {"map_id": mid})
        self.assertEqual((st, d["code"]), (401, "BAD_PIN"))
        st, d = await self.hub.post("/connectors/unmap", {"map_id": mid, "pin": "4711"})
        self.assertEqual(st, 200)
        self.assertEqual([m["client_id"] for m in self.hubc.db.mappings()], [self.b])
        events = [r["event"] for r in self.svc.store.audit_tail(100)]
        self.assertTrue({"connector_mapped", "connector_unmapped"} <= set(events))
        anon = type(self.hub)(self.h)
        st, d = await anon.get("/connectors")
        await anon.close()
        self.assertEqual(st, 401)

    async def test_a_failing_verification_is_shown_to_the_owner_with_its_name(self):
        self.fake.token_error = "invalid_grant"
        st, d = await self.hub.post(f"/connectors/{GSC}/verify")
        self.assertEqual((st, d["code"]), (409, "AUTH_FAILED"))
        st, v = await self.hub.get("/connectors")
        gsc = next(s for s in v["sources"] if s["source"] == GSC)
        self.assertEqual((gsc["connected"], gsc["last_verification"]["code"]), (False, "AUTH_FAILED"))


# =================================================================================================================  competitor research (local fixture sites + a scripted Chrome)
def competitor_site(kind: str) -> web.Application:
    app = web.Application()
    page = lambda title, body, head="": web.Response(text=f"<!doctype html><html lang=en><head><title>{title}</title>{head}</head><body>{body}</body></html>", content_type="text/html")
    words = " ".join(["plumbing"] * 400)

    async def home(r):
        if kind == "rich":
            return page("Springfield Plumbing Pros | Drain Cleaning in Springfield", f"<h1>Springfield plumbers</h1><h2>Drain cleaning</h2><h2>Water heaters</h2><a href='tel:5550001111'>Call</a>"
                        f"<a href='/services/drain-cleaning'>Drains</a><a href='/areas/springfield'>Springfield</a><a href='/blog/tips'>Blog</a><a href='/contact'>Contact</a>"
                        f"<p>UNIQUE-COMPETITOR-SENTENCE-XYZ {words}</p>", "<script type='application/ld+json'>{\"@type\":\"Plumber\",\"name\":\"SPP\"}</script>")
        if kind == "walled":
            return web.Response(status=403, text="<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing</body></html>", content_type="text/html")
        return page("Pipes", "<h1>Pipes</h1><p>thin page</p>")

    async def sub(r):
        return page(f"{r.path.strip('/').replace('/', ' ').title()} | Springfield", f"<h1>{r.path}</h1><p>content {words[:200]}</p><a href='/'>Home</a>")

    async def sitemap(r):
        h = f"http://{r.host}"
        return web.Response(text=f"<?xml version='1.0'?><urlset><url><loc>{h}/</loc></url><url><loc>{h}/services/drain-cleaning</loc></url><url><loc>{h}/areas/springfield</loc></url>"
                                 f"<url><loc>{h}/blog/tips</loc></url><url><loc>{h}/contact</loc></url></urlset>", content_type="application/xml")

    async def robots(r):
        return web.Response(text=f"User-agent: *\nSitemap: http://{r.host}/sitemap.xml\n")
    app.router.add_get("/", home)
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/sitemap.xml", sitemap)
    for p in ("/services/drain-cleaning", "/areas/springfield", "/blog/tips", "/contact"):
        app.router.add_get(p, sub)
    return app


class CompetitorCase(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        HITS.clear()
        self.sites = {}
        for name, host, kind in (("client", "127.0.0.1", "client"), ("rich", "127.0.0.2", "rich"), ("thin", "127.0.0.3", "thin"), ("walled", "127.0.0.4", "walled")):
            srv = TestServer(fixture_site() if kind == "client" else competitor_site(kind), host=host)
            await srv.start_server()
            self.sites[name] = srv
        self.ports = {k: v.port for k, v in self.sites.items()}
        real = F.assert_public_host

        async def quick(host, resolver=None):   # unknown ".test" names fail at once (no real DNS in tests); loopback fixtures pass
            if host.endswith(".test"):
                raise F.FetchError("DNS_FAILED", f"{host} does not resolve", retryable=True)
            return [host]
        self.patches = [mock.patch.object(F, "_is_public", lambda ip: True), mock.patch.object(F, "ALLOWED_PORTS", {None, 80, 443, *self.ports.values()}), mock.patch.object(F, "assert_public_host", quick)]
        for p in self.patches:
            p.start()
        self.world, self.handlers = serp_world(client_href=f"http://127.0.0.1:{self.ports['client']}/drains/")

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        for s in self.sites.values():
            await s.close()
        await super().asyncTearDown()

    def url(self, name: str, path: str = "/") -> str:
        return f"http://{dict(client='127.0.0.1', rich='127.0.0.2', thin='127.0.0.3', walled='127.0.0.4')[name]}:{self.ports[name]}{path}"

    async def ok(self, cap, params=None, **kw):
        r = await self.c.run(cap, params or {}, wait=120, **kw)
        self.assertEqual(r["status"], "succeeded", (cap, r.get("error")))
        return r


class CompetitorTests(CompetitorCase):
    async def test_competitors_are_profiled_compared_with_the_client_and_never_copied(self):
        cid = await self.new_client("Alpha Plumbing LLC", website=self.url("client"), phone="(555) 201-3344", address={"city": "Springfield", "state": "IL"}, services=["Drain Cleaning"])
        r = await self.ok("research.competitors", {"urls": [self.url("rich"), self.url("thin")], "depth": "page"}, client=cid)
        res = r["result"]
        by = {p["domain"]: p for p in res["profiles"]}
        rich, thin = by["127.0.0.2"], by["127.0.0.3"]
        self.assertEqual((rich["via"], rich["status"]), ("http", 200))
        self.assertIn("Springfield Plumbing Pros", rich["title"])
        self.assertEqual((rich["tel_links"], rich["schema"], rich["h2_sample"]), (1, ["Plumber"], ["Drain cleaning", "Water heaters"]))
        self.assertGreater(rich["word_count"], 300)
        self.assertEqual(thin["word_count"] < 50, True)
        self.assertEqual(res["client_page"]["domain"], "127.0.0.1")
        texts = [c["observation"] for c in res["comparison"]]
        # this client has an address but no target locations on file: the location count must say "not counted", never a misleading "0 of 2"
        self.assertTrue(any("not counted: the client has no target locations on file" in t for t in texts))
        self.assertFalse(any("name a target location in their title" in t for t in texts))
        self.assertTrue(any("click-to-call" in t for t in texts))
        self.assertIn("client page vs competitors", texts[-1])
        self.assertNotIn("UNIQUE-COMPETITOR-SENTENCE-XYZ", json.dumps(r["result"]), "competitor wording is never returned")
        self.assertTrue(res["artifact_id"] and r["evidence"])
        self.assertEqual(r["confidence"], "low", "two competitors are anecdotal and say so")
        obs = self.svc.admin().research.observations(cid, kind="competitor", limit=20)
        self.assertEqual({o["domain"] for o in obs}, {"127.0.0.2", "127.0.0.3"})

    async def test_the_clients_own_site_is_not_a_competitor_and_nothing_to_read_is_a_named_error(self):
        cid = await self.new_client("Alpha Plumbing LLC", website=self.url("client"))
        r = await self.c.run("research.competitors", {"urls": [self.url("client")]}, client=cid)
        self.assertEqual(r["error"]["code"], "NOT_A_COMPETITOR")
        r = await self.c.run("research.competitors", {})
        self.assertEqual(r["error"]["code"], "NO_TARGETS")
        r = await self.c.run("research.competitors", {}, client=cid)
        self.assertEqual(r["error"]["code"], "NO_CAMPAIGN")

    async def test_a_site_that_refuses_scripts_is_read_in_the_real_browser_and_says_what_that_cannot_see(self):
        await self.h.extension(handlers=self.handlers)
        r = await self.ok("research.competitors", {"urls": [self.url("walled"), self.url("rich")]})
        by = {p["domain"]: p for p in r["result"]["profiles"]}
        self.assertEqual(by["127.0.0.2"]["via"], "http")
        walled = by["127.0.0.4"]
        self.assertEqual(walled["via"], "browser")
        self.assertIsNone(walled["schema"])
        self.assertIn("JSON-LD / schema", walled["not_measured"])
        self.assertTrue(any("real browser" in l and "not measured" in l for l in r["limitations"]))

    async def test_without_the_browser_a_walled_or_unreachable_site_is_listed_as_unreadable_not_guessed(self):
        r = await self.ok("research.competitors", {"urls": [self.url("walled"), self.url("rich"), "https://nowhere-at-all.test/"]})
        un = {u["domain"]: u["error"] for u in r["result"]["unreadable"]}
        self.assertEqual(un, {"127.0.0.4": "EXTENSION_NOT_CONNECTED", "nowhere-at-all.test": "EXTENSION_NOT_CONNECTED"})
        self.assertEqual([p["domain"] for p in r["result"]["profiles"]], ["127.0.0.2"])
        self.assertEqual(r["completeness"], "partial")
        self.assertTrue(any("could not be read" in l for l in r["limitations"]))

    async def test_a_whole_site_read_describes_what_kind_of_business_it_looks_like(self):
        cid = await self.new_client("Alpha Plumbing LLC", website=self.url("client"), services=["Drain Cleaning"], address={"city": "Springfield", "state": "IL"})
        await self.c.run("seo.campaign_upsert", {"locations": ["Springfield, IL"], "keywords": ["drain cleaning | Springfield, IL"], "services": ["drain cleaning"]}, client=cid)
        r = await self.ok("research.competitors", {"urls": [self.url("rich")], "depth": "site", "max_pages_per_site": 6}, client=cid)
        site = r["result"]["profiles"][0]["site"]
        self.assertTrue(site["ok"])
        self.assertGreaterEqual(site["pages_read"], 4)
        self.assertEqual({"home", "contact", "blog"} <= set(site["page_types"]), True)
        self.assertIn("bounded sample", site["sampled"])
        self.assertIn("Plumber", site["schema_types"] or [])

    async def test_the_serp_based_review_also_falls_back_to_the_browser(self):
        cid = await self.new_client("Alpha Plumbing LLC", website=self.url("client"), address={"city": "Springfield", "state": "IL"})
        await self.ok("seo.campaign_upsert", {"locations": ["Springfield, IL"], "keywords": ["drain cleaning | Springfield, IL"]}, client=cid)
        await self.h.extension(handlers=self.handlers)
        self.world["rank"] = {"drain cleaning springfield, il": 4}
        await self.ok("seo.serp_observe", {}, client=cid)
        r = await self.ok("seo.competitors", {"max_competitors": 3}, client=cid)
        comps = r["result"]["competitors"]
        self.assertTrue(comps and all(not c.get("error") for c in comps), comps)
        self.assertTrue(all(c["via"] == "browser" for c in comps), "the .test competitor hosts cannot be reached by HTTP here, so the real browser read them")


# =================================================================================================================  the loop and the read-only audit
class AuditCase(CompetitorCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = Clock()
        self.svc.loops.clock = self.clock
        self.folder = self.h.settings.data_dir / "connectors"
        self.fake = FakeProviders()
        self.hubc = self.svc.connectors
        self.hubc.http = self.fake
        self.cid = await self.new_client("Alpha Plumbing LLC", website=self.url("client"), phone="(555) 201-3344", address={"city": "Springfield", "state": "IL"}, services=["Drain Cleaning"], service_areas=["Springfield, IL"])
        self.other = await self.new_client("Beta Roofing LLC", website="https://www.beta-roofing.test/")
        await self.ok("seo.campaign_upsert", {"locations": ["Springfield, IL"], "keywords": ["drain cleaning | Springfield, IL", "water heater install | Springfield, IL"]}, client=self.cid)
        self.world["rank"] = {"drain cleaning springfield, il": 7, "water heater install springfield, il": None}
        self.prop = "https://127.0.0.1/"
        self.fake.google_sites = {self.prop: "siteOwner", B_SITE: "siteOwner"}
        self.fake.google = {self.prop: {"totals": {"clicks": 120, "impressions": 4000, "ctr": 0.03, "position": 18.4},
                                        "queries": [{"keys": ["drain cleaning"], "clicks": 40, "impressions": 900, "ctr": 0.044, "position": 22.3}], "pages": [], "sitemaps": [], "inspect": {}},
                            B_SITE: {"totals": {"clicks": 777, "impressions": 9999, "ctr": 0.07, "position": 5.5}, "queries": [], "pages": [], "sitemaps": [], "inspect": {}}}

    async def settle(self, lid, timeout=180.0):
        end = time.time() + timeout
        while True:
            d = await self.svc.loops.advance(lid)
            if d["phase"] != "measuring" or d["status"] != "active" or d["retry_at"] or time.time() > end:
                return d
            await asyncio.sleep(0.1)

    async def connect_gsc(self):
        write_service_account(self.folder)
        await self.hubc.map(self.cid, GSC, self.prop, client_domains=[self.url("client")], mapped_by="owner:test")


class PreflightTests(AuditCase):
    async def test_preflight_lists_exactly_what_the_owner_still_has_to_do_and_starts_nothing(self):
        before = len(self.svc.loops.db.list())
        r = await self.ok("seo.audit_preflight", {}, client=self.cid)
        pf = r["result"]
        self.assertEqual((pf["ready_to_start"], pf["complete_data"], pf["blocking"]), (True, False, []))
        names = {c["name"]: c for c in pf["checks"]}
        self.assertEqual(names["Client website"]["level"], "ok")
        self.assertEqual(names["Google Search Console"]["level"], "limits")
        steps = " ".join(names["Google Search Console"]["owner_actions"])
        for needle in ("Google Search Console API", "gsc_service_account.json", "Add user", "Map a property", str(self.folder)):
            self.assertIn(needle, steps)
        self.assertIn("bing_webmaster_api_key.txt", " ".join(names["Bing Webmaster Tools"]["owner_actions"]))
        self.assertEqual(names["Chrome extension (search results, bot-protected sites)"]["level"], "limits")
        self.assertEqual(pf["will_not_run"][:2], ["any change to the client's website", "any deployment"])
        self.assertEqual(len(self.svc.loops.db.list()), before, "a preflight starts nothing")
        self.assertEqual(self.svc.store.approvals(), [])

    async def test_preflight_moves_to_complete_as_the_owner_connects_things(self):
        await self.h.extension(handlers=self.handlers)
        write_service_account(self.folder)
        names = lambda pf: {c["name"]: c for c in pf["checks"]}
        pf = (await self.ok("seo.audit_preflight", {}, client=self.cid))["result"]
        gsc = names(pf)["Google Search Console"]
        self.assertEqual(gsc["level"], "limits")
        self.assertIn("no property is mapped", gsc["detail"])
        self.assertTrue(any(SA_EMAIL in a for a in gsc["owner_actions"]), "it tells the owner which e-mail to add to the property")
        await self.hubc.map(self.cid, GSC, self.prop, client_domains=[self.url("client")], mapped_by="owner:test")
        write_bing_key(self.folder)
        self.fake.bing_sites = {"https://127.0.0.1/": True}
        await self.hubc.map(self.cid, BING, "https://127.0.0.1/", client_domains=[self.url("client")], mapped_by="owner:test")
        pf = (await self.ok("seo.audit_preflight", {}, client=self.cid))["result"]
        self.assertEqual((pf["ready_to_start"], pf["complete_data"], pf["owner_actions"]), (True, True, []))

    async def test_preflight_blocks_when_there_is_nothing_to_audit(self):
        r = await self.ok("seo.audit_preflight", {}, client=self.other)
        self.assertFalse(r["result"]["ready_to_start"])
        self.assertIn("SEO campaign", r["result"]["blocking"])
        e = await self.c.run("seo.start_baseline_audit", {}, client=self.other)
        self.assertEqual(e["error"]["code"], "PREFLIGHT_BLOCKED")
        self.assertTrue(e["error"]["details"]["owner_actions"])
        self.assertEqual(self.svc.loops.db.list(client_id=self.other), [])


class AuditLoopTests(AuditCase):
    async def test_the_read_only_baseline_audit_collects_everything_available_reports_and_ends_by_itself(self):
        await self.h.extension(handlers=self.handlers)
        await self.connect_gsc()
        r = await self.ok("seo.start_baseline_audit", {"max_pages": 10}, client=self.cid)
        self.assertEqual(r["result"]["loop"]["goal"], {"baseline_only": True, "max_requests": 40, "deadline": None})
        self.assertEqual(r["result"]["preflight"]["will_be_missing"], ["Bing Webmaster Tools"])
        lid = r["result"]["loop"]["loop_id"]
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["outcome"]), ("completed", "baseline_only"), d.get("summary"))
        # what was collected
        jobs = {j["capability"]: j for j in self.svc.store.jobs(task_id=d["task_id"], limit=50)}
        self.assertTrue({"connectors.status", "seo.serp_observe", "seo.audit_site", "gsc.search_analytics", "research.competitors", "seo.report"} <= set(jobs), sorted(jobs))
        self.assertNotIn("bing.webmaster_stats", jobs, "Bing is not ready for this client, so it was not requested (and the report says so)")
        self.assertTrue(all(j["status"] == "succeeded" for j in jobs.values()), {k: v["status"] for k, v in jobs.items()})
        self.assertGreaterEqual(jobs["research.competitors"]["created_at"], jobs["seo.serp_observe"]["finished_at"], "competitors are researched from the search results that were just observed")
        self.assertGreaterEqual(jobs["seo.report"]["created_at"], jobs["research.competitors"]["finished_at"])
        events = [e["event"] for e in self.svc.loops.db.events(lid)]
        self.assertEqual(events.count("followup_started"), 2)
        # honest data sources
        src = {s["source"]: s for s in d["sources"]}
        self.assertEqual((src[GSC]["connected"], src[GSC].get("used_in_this_loop")), (True, True))
        self.assertFalse(src[BING]["connected"])
        self.assertIn("no Bing Webmaster credentials", src[BING]["detail"])
        obs = {o["key"]: o for o in d["latest_observations"]}
        self.assertEqual(obs["gsc|site|clicks"]["value"], 120.0)
        self.assertEqual(obs["google|organic|drain cleaning|springfield, il"]["value"], 7.0)
        # the report
        art = [a for a in d["artifacts"] if a["kind"] == "report"]
        self.assertEqual(len(art), 1)
        md = self.svc.store.get_artifact(art[0]["artifact_id"])["body_md"]
        for section in ("## Executive summary", "## Search performance (first-party data)", "Google Search Console", "## Competitor findings", "## Prioritised recommendations", "## Missing data"):
            self.assertIn(section, md)
        self.assertIn("| drain cleaning |", md)
        self.assertIn("Bing Webmaster Tools is not connected for this client", md)
        self.assertNotIn("Search Console is not connected", md)
        # read-only
        self.assertEqual(self.svc.store.approvals(), [])
        self.assertEqual([a for a in self.svc.store.artifacts(client_id=self.cid, limit=100) if a["kind"] == "change"], [])
        self.assertEqual(HITS, [], "the crawler never requested a risky path")
        self.assertEqual(self.svc.store.get_task(d["task_id"])["status"], "completed")

    async def test_the_audit_without_any_credentials_still_runs_and_says_what_is_missing(self):
        await self.h.extension(handlers=self.handlers)
        r = await self.ok("seo.start_baseline_audit", {"max_pages": 10}, client=self.cid)
        d = await self.settle(r["result"]["loop"]["loop_id"])
        self.assertEqual(d["status"], "completed")
        alljobs = {j["capability"]: j for j in self.svc.store.jobs(task_id=d["task_id"], limit=50)}
        jobs = set(alljobs)
        self.assertFalse({"gsc.search_analytics", "bing.webmaster_stats"} & jobs)
        self.assertEqual({k: (v["status"], json.loads(v["error"])["message"]) for k, v in alljobs.items() if v["status"] != "succeeded"}, {}, "every request of the audit succeeded")
        self.assertIn("seo.report", jobs)
        md = self.svc.store.get_artifact(next(a for a in d["artifacts"] if a["kind"] == "report")["artifact_id"])["body_md"]
        self.assertNotIn("## Search performance (first-party data)", md)
        self.assertIn("Google Search Console is not connected for this client", md)
        self.assertIn("Bing Webmaster Tools is not connected for this client", md)

    async def test_a_second_audit_alongside_an_open_loop_needs_an_explicit_decision(self):
        await self.h.extension(handlers=self.handlers)
        r = await self.ok("seo.start_baseline_audit", {"max_pages": 5}, client=self.cid)
        lid = r["result"]["loop"]["loop_id"]
        await self.svc.loops.advance(lid)
        if self.svc.loops.describe(lid)["status"] == "active":
            e = await self.c.run("seo.start_baseline_audit", {}, client=self.cid)
            self.assertEqual(e["error"]["code"], "LOOP_ALREADY_OPEN")
        await self.svc.loops.control(lid, "stop", actor="test")

    async def test_a_failing_source_is_recorded_as_unavailable_and_does_not_stop_the_round_or_claim_a_result(self):
        await self.h.extension(handlers=self.handlers)
        await self.connect_gsc()
        self.fake.fail_next("searchAnalytics", 429, {"error": {"code": 429, "message": "Quota exceeded"}})
        r = await self.ok("seo.start_baseline_audit", {"max_pages": 5}, client=self.cid)
        d = await self.settle(r["result"]["loop"]["loop_id"])
        self.assertEqual(d["status"], "completed")
        gsc_job = next(j for j in self.svc.store.jobs(task_id=d["task_id"], limit=50) if j["capability"] == "gsc.search_analytics")
        self.assertEqual((gsc_job["status"], json.loads(gsc_job["error"])["code"], gsc_job["retryable"]), ("failed", "RATE_LIMITED", 1))
        obs = self.svc.loops.db.observations(d["loop_id"])
        bad = [o for o in obs if o["key"] == "gsc|site|clicks"]
        self.assertEqual([(o["status"], o["value"]) for o in bad], [("unavailable", None)], "unavailable, never zero and never 'not ranking'")
        self.assertIn("RATE_LIMITED", bad[0]["limits"][0])
        src = {s["source"]: s for s in d["sources"]}
        self.assertFalse(src[GSC]["connected"])
        self.assertIn("requested but failed: RATE_LIMITED", src[GSC]["detail"])

    async def test_a_loop_for_one_client_never_requests_or_receives_another_clients_data(self):
        await self.h.extension(handlers=self.handlers)
        await self.connect_gsc()
        await self.ok("seo.campaign_upsert", {"locations": ["Springfield, IL"], "keywords": ["roof repair | Springfield, IL"]}, client=self.other)
        r = await self.ok("seo.start_baseline_audit", {"max_pages": 5}, client=self.other)
        d = await self.settle(r["result"]["loop"]["loop_id"])
        jobs = {j["capability"] for j in self.svc.store.jobs(task_id=d["task_id"], limit=50)}
        self.assertNotIn("gsc.search_analytics", jobs, "client B has no mapped property, so nothing was requested for it")
        self.assertEqual(self.fake.queried, [], "no property of anyone was read")
        self.assertFalse(next(s for s in d["sources"] if s["source"] == GSC)["connected"])
        self.assertNotIn("120", json.dumps(d["latest_observations"]))

    async def test_a_loop_creator_without_the_connector_permission_does_not_get_first_party_requests(self):
        await self.h.extension(handlers=self.handlers)
        await self.connect_gsc()
        self.svc.callers.set_grants("collaborator", tuple(g for g in self.svc.callers.list()[0]["grants"] if g != "connector:read"))
        r = await self.c.run("loop.create", {"kind": "seo_visibility", "title": "visibility loop", "config": {"audit": False}, "goal": {"max_rounds": 2, "targets": [{"keyword": "drain cleaning", "location": "Springfield, IL"}]}}, client=self.cid)
        self.assertEqual(r["status"], "succeeded", r.get("error"))
        d = await self.settle(r["result"]["loop"]["loop_id"])
        jobs = {j["capability"] for j in self.svc.store.jobs(task_id=d["task_id"], limit=50)}
        self.assertNotIn("gsc.search_analytics", jobs)

    async def test_the_first_party_figures_improve_in_the_right_direction_and_disagreements_are_called_out(self):
        await self.h.extension(handlers=self.handlers)
        await self.connect_gsc()
        goal = {"max_rounds": 3, "targets": [{"keyword": "drain cleaning", "location": "Springfield, IL", "max_position": 3}]}
        r = await self.ok("loop.create", {"kind": "seo_visibility", "title": "visibility", "config": {"audit": False, "keywords": ["water heater install"]}, "goal": goal}, client=self.cid)
        d = await self.settle(r["result"]["loop"]["loop_id"])
        self.assertEqual(d["phase"], "awaiting_plan", d.get("summary"))
        notes = d.get("cross_checks") or []
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("position 7", notes[0])
        self.assertIn("22.3", notes[0])
        self.assertIn("different things", notes[0])
        # comparison direction: more clicks / impressions are an improvement, a higher (worse) average position is not
        mk = lambda rnd, key, metric, v, t: {"key": key, "label": key, "metric": metric, "value": v, "status": "found", "comparable": True, "cmp_key": "k", "observed_at": t, "round_no": rnd, "evidence_id": "", "limits": []}
        obs = [mk(0, "gsc|site|clicks", "clicks", 100.0, 1), mk(1, "gsc|site|clicks", "clicks", 130.0, 2), mk(0, "gsc|site|position", "avg_position", 12.0, 1), mk(1, "gsc|site|position", "avg_position", 18.0, 2),
               mk(0, "gsc|site|impressions", "impressions", 1000.0, 1), mk(1, "gsc|site|impressions", "impressions", 900.0, 2)]
        rows = {x["key"]: x for x in self.svc.loops.compare(obs, 1)["rows"]}
        self.assertEqual((rows["gsc|site|clicks"]["change"], rows["gsc|site|clicks"]["delta_vs_previous"], rows["gsc|site|clicks"]["raw_change_vs_previous"]), ("improved", -30.0, 30.0))
        self.assertEqual(rows["gsc|site|position"]["change"], "worse")
        self.assertEqual(rows["gsc|site|impressions"]["change"], "worse")
        self.assertNotIn("ordinary variation", rows["gsc|site|clicks"]["note"], "the +/-2 positions tolerance only applies to positions")

    async def test_supporting_data_alone_never_makes_a_goal_measurement_usable(self):
        """Search Console totals are 'found' even when the search itself failed: a goal loop must not advance on that."""
        await self.connect_gsc()          # (no extension connected: the search cannot run)
        goal = {"max_rounds": 3, "targets": [{"keyword": "drain cleaning", "location": "Springfield, IL", "max_position": 10}]}
        r = await self.ok("loop.create", {"kind": "seo_visibility", "title": "visibility", "config": {"audit": False, "retry_backoff_s": [5]}, "goal": goal}, client=self.cid)
        d = await self.settle(r["result"]["loop"]["loop_id"])
        self.assertEqual((d["phase"], d["retry_at"] is not None), ("measuring", True), "retry later: the targeted search was unavailable")
        self.assertEqual(d["goal_progress"]["status"], "insufficient")
        self.assertTrue(any(o["status"] == "unavailable" for o in self.svc.loops.db.observations(d["loop_id"])))


# =================================================================================================================  the Google sign-in helper
class OAuthLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_login_flow_checks_state_exchanges_the_code_and_stores_only_the_refresh_token(self):
        from octg_agent.connectors import oauth
        import threading
        import urllib.request
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            secret = Path(d) / "client_secret.json"
            secret.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-test-secret"}}))
            urls: list[str] = []
            seen: dict = {}

            class Fake:
                async def request(self, method, url, *, headers=None, json_body=None, form=None):
                    from octg_agent.connectors.http import HttpResult
                    seen.update(form or {})
                    return HttpResult(200, {"access_token": "ya29.x", "refresh_token": "1//refresh-new", "expires_in": 3600}, {})

            def browser():   # plays the user's browser: waits for the authorization URL, then calls the loopback redirect with the right state
                import time
                for _ in range(100):
                    if urls:
                        break
                    time.sleep(0.05)
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(urls[0]).query)
                urllib.request.urlopen(q["redirect_uri"][0] + f"/?code=AUTHCODE&state={q['state'][0]}", timeout=5).read()
            th = threading.Thread(target=browser)
            th.start()
            path = await oauth.login(secret, Path(d) / "connectors", open_browser=False, timeout_s=20, http=Fake(), on_url=urls.append)
            th.join()
            stored = json.loads(Path(path).read_text())
            self.assertEqual(stored["refresh_token"], "1//refresh-new")
            self.assertEqual((seen["grant_type"], seen["code"]), ("authorization_code", "AUTHCODE"))
            self.assertIn("code_verifier", seen)
            self.assertIn("code_challenge_method=S256", urls[0])
            self.assertIn("webmasters.readonly", urls[0])
            self.assertNotIn("1//refresh-new", " ".join(urls))

    async def test_a_wrong_state_or_a_refusal_stores_nothing(self):
        from octg_agent.connectors import oauth
        import threading
        import urllib.request
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            secret = Path(d) / "s.json"
            secret.write_text(json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}))
            urls: list[str] = []

            def browser(query):
                import time
                for _ in range(100):
                    if urls:
                        break
                    time.sleep(0.05)
                from urllib.parse import parse_qs, urlsplit
                redirect = parse_qs(urlsplit(urls[0]).query)["redirect_uri"][0]
                urllib.request.urlopen(redirect + "/" + query, timeout=5).read()
            for query, code in (("?code=X&state=WRONG", "LOGIN_REFUSED"), ("?error=access_denied&state=WRONG", "LOGIN_REFUSED")):
                urls.clear()
                th = threading.Thread(target=browser, args=(query,))
                th.start()
                with self.assertRaises(ConnectorError) as cm:
                    await oauth.login(secret, Path(d) / "connectors", open_browser=False, timeout_s=20, on_url=urls.append)
                th.join()
                self.assertEqual(cm.exception.code, code)
            self.assertFalse((Path(d) / "connectors" / "gsc_oauth_token.json").exists())
            with self.assertRaises(ConnectorError) as cm:
                oauth.read_client_secret(Path(d) / "missing.json")
            self.assertEqual(cm.exception.code, "CREDENTIALS_INVALID")


if __name__ == "__main__":
    unittest.main()
