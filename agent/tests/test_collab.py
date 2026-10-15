"""The collaboration protocol over its real HTTP surface (a real agent on a loopback port, a fake extension, a local fixture website).

Covers the acceptance list: submit -> execute -> result; follow-up requests; long-running jobs; idempotent retries; unauthorized requests; client-scope
isolation; no secrets in logs/artifacts; browser disconnect + recovery; traceable SEO evidence; report + handoff consumable without copying; change
records with tests; approval gates; verification recorded. Synthetic data only.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from octg_agent.collab import PROTOCOL
from octg_agent.collab.capabilities import REGISTRY, Capability, register
from octg_agent.collab.service import CollabService
from octg_agent.collab.store import CollabStore
from octg_agent.hub.security import HubPin
from octg_agent.seo import fetch as F
from octg_agent.seo import serp as SERP
from octg_agent.seo.signals import extract, parse_robots, robots_allows

from .helpers import API, Harness, wait_until
from .test_hub import Hub

# ------------------------------------------------------------------ test-only capabilities (registered once, permission test:internal)
COUNTS: dict[str, int] = {}
HITS: list[str] = []   # paths of the fixture site that must never be requested


async def _echo(ctx, p):
    COUNTS["echo"] = COUNTS.get("echo", 0) + 1
    ctx.evidence("note", {"seen": p.get("value")}, summary="echo")
    return {"value": p.get("value"), "doubled": (p.get("value") or 0) * 2}


async def _slow(ctx, p):
    for i in range(int(p.get("steps", 3))):
        ctx.progress(f"step {i + 1}")
        await asyncio.sleep(float(p.get("delay", 0.3)))
    COUNTS["slow"] = COUNTS.get("slow", 0) + 1
    return {"done": True}


async def _sidefx(ctx, p):
    COUNTS["sidefx"] = COUNTS.get("sidefx", 0) + 1
    return {"changed": True}


def _reg(name, fn, **kw):
    if name not in REGISTRY:
        register(Capability(name, "test", kw.pop("props", {"value": {"type": "integer"}, "steps": {"type": "integer"}, "delay": {"type": "number"}}), (), {}, ("test:internal",), fn, **kw))


_reg("test.echo", _echo)
_reg("test.slow", _slow, max_timeout_s=30)
_reg("test.sidefx", _sidefx, read_only=False, external_effects=True, approval="required", approval_kind="other_high_impact")


class Collab:
    """Collaborator-side HTTP client used by the tests (what Cloudy does)."""

    def __init__(self, h: Harness, token: str | None = None):
        self.h = h
        self.token = token if token is not None else (h.settings.data_dir / "collab_collaborator.token").read_text().strip()

    async def call(self, method, path, body=None, token=None, headers=None):
        hdr = {"Authorization": f"Bearer {self.token if token is None else token}"} if (token is None and self.token) or token else {}
        hdr.update(headers or {})
        async with self.h.http.request(method, self.h.base + path, json=body, headers=hdr) as r:
            return r.status, await r.json(content_type=None)

    async def submit(self, capability, params=None, **kw):
        st, d = await self.call("POST", "/api/collab/requests", {"capability": capability, "parameters": params or {}, **kw})
        return st, d

    async def run(self, capability, params=None, wait=20, **kw):
        st, d = await self.submit(capability, params, **kw)
        assert st in (200, 202), (st, d)
        rid = d["response"]["request_id"]
        end = time.time() + wait
        r = d["response"]
        while r["status"] not in ("succeeded", "failed", "cancelled", "timed_out", "rejected") and time.time() < end:
            _, g = await self.call("GET", f"/api/collab/requests/{rid}?wait=5")
            r = g["response"]
        return r


class CollabCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        COUNTS.clear()
        self.h = Harness()
        await self.h.__aenter__()
        self.svc: CollabService = self.h.rt.collab
        self.svc.callers.set_grants("collaborator", tuple(list(self.svc.callers.list()[0]["grants"]) + ["test:internal"]))
        self.c = Collab(self.h)
        self.hub = Hub(self.h)
        await self.hub.login()

    async def asyncTearDown(self):
        await self.hub.close()
        await self.h.__aexit__(None, None, None)

    async def new_client(self, name, **data):
        st, d = await self.hub.post("/clients", {"business_name": name, **data})
        assert st == 200 and d["ok"], d
        return d["client_id"]


# =================================================================================================================  the loop
class LoopTests(CollabCase):
    async def test_submit_execute_return_and_follow_up_in_one_parent_task(self):
        st, d = await self.c.call("POST", "/api/collab/tasks", {"title": "demo", "objective": "prove the loop"})
        self.assertEqual(st, 201)
        tid = d["task"]["task_id"]
        r1 = await self.c.run("test.echo", {"value": 21}, task_id=tid)
        self.assertEqual(r1["status"], "succeeded")
        self.assertEqual(r1["protocol"], PROTOCOL)
        self.assertEqual(r1["result"], {"value": 21, "doubled": 42})
        self.assertEqual(len(r1["evidence"]), 1)
        self.assertTrue(r1["evidence"][0]["sha256"])
        # the collaborator uses the first result in a second request
        r2 = await self.c.run("test.echo", {"value": r1["result"]["doubled"]}, task_id=tid)
        self.assertEqual(r2["result"]["doubled"], 84)
        _, ev = await self.c.call("GET", f"/api/collab/evidence/{r2['evidence'][0]['evidence_id']}")
        self.assertEqual(json.loads(ev["content"]["text"]), {"seen": 42})
        st, fin = await self.c.call("POST", f"/api/collab/tasks/{tid}/finish", {"status": "completed", "outcome": "loop proven", "summary": "two requests"})
        self.assertEqual((st, fin["task"]["status"]), (200, "completed"))
        _, t = await self.c.call("GET", f"/api/collab/tasks/{tid}")
        self.assertEqual([j["status"] for j in t["jobs"]], ["succeeded", "succeeded"])
        _, a = await self.c.call("GET", f"/api/collab/audit?task_id={tid}")
        events = [x["event"] for x in a["audit"]]
        for e in ("task_created", "job_submitted", "job_started", "job_succeeded", "task_finished"):
            self.assertIn(e, events)
        st, d = await self.c.submit("test.echo", {"value": 1}, task_id=tid)
        self.assertEqual((st, d["error"]["code"]), (409, "TASK_CLOSED"))

    async def test_capability_discovery_describes_contracts(self):
        st, d = await self.c.call("GET", "/api/collab/capabilities")
        self.assertEqual(st, 200)
        names = {c["name"]: c for c in d["capabilities"]}
        for n in ("client.context", "web.fetch_page", "browser.inspect_page", "search.web", "seo.audit_site", "handoff.create", "verify.page", "approval.request"):
            self.assertIn(n, names)
        c = names["browser.inspect_page"]
        for k in ("purpose", "input_schema", "output", "permissions", "read_only", "external_effects", "approval", "timeout_s", "cancellable", "limitations"):
            self.assertIn(k, c)
        self.assertFalse(names["agent.browser_task"]["available_to_caller"])   # not granted by default
        # nothing raw is exposed
        self.assertFalse(any(n in names for n in ("click", "type", "upload_file", "fs_find", "shell", "python")))

    async def test_long_running_job_is_trackable_with_progress_then_completes(self):
        st, d = await self.c.submit("test.slow", {"steps": 4, "delay": 0.25})
        rid = d["response"]["request_id"]
        self.assertIn(d["response"]["status"], ("queued", "running"))
        await asyncio.sleep(0.4)
        _, g = await self.c.call("GET", f"/api/collab/requests/{rid}")
        self.assertEqual(g["response"]["status"], "running")
        self.assertTrue(g["response"]["progress"].startswith("step"))
        _, g = await self.c.call("GET", f"/api/collab/requests/{rid}?wait=10")
        self.assertEqual(g["response"]["status"], "succeeded")
        self.assertGreaterEqual(g["response"]["duration_ms"], 900)

    async def test_timeout_and_cancellation(self):
        r = await self.c.run("test.slow", {"steps": 10, "delay": 0.2}, timeout_s=0.5)
        self.assertEqual(r["status"], "timed_out")
        self.assertTrue(r["retryable"])
        st, d = await self.c.submit("test.slow", {"steps": 20, "delay": 0.2})
        rid = d["response"]["request_id"]
        await asyncio.sleep(0.3)
        _, c = await self.c.call("POST", f"/api/collab/requests/{rid}/cancel")
        _, g = await self.c.call("GET", f"/api/collab/requests/{rid}?wait=5")
        self.assertEqual(g["response"]["status"], "cancelled")
        self.assertNotIn("slow", COUNTS)

    async def test_retries_never_execute_twice(self):
        st1, d1 = await self.c.submit("test.echo", {"value": 5}, idempotency_key="k-1")
        st2, d2 = await self.c.submit("test.echo", {"value": 5}, idempotency_key="k-1")
        self.assertEqual((st1, st2), (202, 200))
        self.assertFalse(d2["created"])
        self.assertEqual(d1["response"]["request_id"], d2["response"]["request_id"])
        await self.c.run("test.echo", {"value": 6}, request_id="rq_fixed_0001")
        st3, d3 = await self.c.submit("test.echo", {"value": 6}, request_id="rq_fixed_0001")   # a network retry with the same request id
        self.assertEqual((st3, d3["response"]["status"]), (200, "succeeded"))
        await wait_until(lambda: COUNTS.get("echo", 0) >= 2)
        self.assertEqual(COUNTS["echo"], 2)
        st, d = await self.c.submit("test.echo", {"value": 7}, idempotency_key="k-1")
        self.assertEqual((st, d["error"]["code"]), (409, "IDEMPOTENCY_CONFLICT"))
        st, d = await self.c.submit("test.echo", {"value": 99}, request_id="rq_fixed_0001")
        self.assertEqual((st, d["error"]["code"]), (409, "IDEMPOTENCY_CONFLICT"))

    async def test_restart_recovers_read_only_jobs_and_fails_side_effecting_ones(self):
        st = self.svc.store
        now = time.time()
        base = {"caller": "collaborator", "params": "{}", "params_hash": "x", "permissions": "[]", "expected_format": "json", "priority": 5, "timeout_s": 10, "created_at": now,
                "started_at": now, "attempts": 1, "retryable": 0, "cancel_requested": 0}
        st.insert_job({**base, "request_id": "rq_interrupted_ro", "capability": "test.echo", "status": "running"})
        st.insert_job({**base, "request_id": "rq_interrupted_rw", "capability": "test.sidefx", "status": "running"})
        fresh = CollabService(self.h.rt, store=CollabStore(st.path), callers=self.svc.callers)
        await fresh.start()
        await wait_until(lambda: fresh.store.get_job("rq_interrupted_ro")["status"] == "succeeded", 5)
        self.assertEqual(fresh.store.get_job("rq_interrupted_ro")["status"], "succeeded")
        rw = fresh.store.get_job("rq_interrupted_rw")
        self.assertEqual(rw["status"], "failed")
        self.assertEqual(json.loads(rw["error"])["code"], "INTERRUPTED")
        self.assertNotIn("sidefx", COUNTS)
        await fresh.stop()


# =================================================================================================================  security
class SecurityTests(CollabCase):
    async def test_unauthenticated_and_wrong_token_requests_are_rejected(self):
        st, d = await self.c.call("GET", "/api/collab/capabilities", token="")
        self.assertEqual((st, d["error"]["code"]), (401, "UNAUTHORIZED"))
        st, d = await self.c.call("GET", "/api/collab/capabilities", token=API)       # the agent's own API token is NOT a collaborator token
        self.assertEqual(st, 401)
        async with self.h.http.post(self.h.base + "/api/action", json={"action": "get_tabs"}, headers={"Authorization": f"Bearer {self.c.token}"}) as r:
            self.assertEqual(r.status, 401)                                            # a collaborator token cannot drive raw browser actions
        st, d = await self.c.call("GET", "/api/collab/capabilities", headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)                                                      # web pages can never call it
        self.svc.callers.disable("collaborator")
        st, _ = await self.c.call("GET", "/api/collab/whoami")
        self.assertEqual(st, 401)

    async def test_permissions_cannot_be_escalated_by_the_request(self):
        st, d = await self.c.submit("agent.browser_task", {"instruction": "do something"})
        self.assertEqual((st, d["error"]["code"]), (403, "PERMISSION_DENIED"))
        st, d = await self.c.submit("agent.status", {}, required_permissions=["agent:task"])
        self.assertEqual((st, d["error"]["code"]), (403, "PERMISSION_MISMATCH"))
        st, d = await self.c.submit("agent.status", {}, required_permissions=["agent:read", "agent:task"])
        self.assertEqual((st, d["error"]["code"]), (403, "PERMISSION_DENIED"))
        st, d = await self.c.submit("no.such.cap", {})
        self.assertEqual((st, d["error"]["code"]), (404, "UNKNOWN_CAPABILITY"))
        st, d = await self.c.submit("web.fetch_page", {"url": "https://x.test", "extra": 1})
        self.assertEqual((st, d["error"]["code"]), (400, "INVALID_PARAMS"))

    async def test_secrets_never_travel_or_get_logged(self):
        for bad in ({"value": 1, "note": "password: hunter2"}, {"card": "4111 1111 1111 1111"}, {"k": "sk-" "abcdefghijklmnopqrstuvwxyz123456"}):
            st, d = await self.c.submit("artifact.record", {"kind": "note", "stage": "reasoning", "title": "x", "body": bad})
            self.assertEqual((st, d["error"]["code"]), (400, "SECRET_IN_REQUEST"), bad)
        await self.c.run("agent.status")
        tok = self.c.token
        cfg = (self.h.settings.data_dir / "collab.json").read_text()
        self.assertNotIn(tok, cfg)                                                    # only the hash is kept
        db = sqlite3.connect(self.h.settings.data_dir / "collab.db")
        dump = "\n".join(json.dumps(r, default=str) for t in ("jobs", "audit", "artifacts", "evidence") for r in db.execute(f"SELECT * FROM {t}").fetchall())
        db.close()
        for s in (tok, "hunter2", "4111 1111 1111 1111", API):
            self.assertNotIn(s, dump)
        logs = "".join(p.read_text(errors="replace") for p in (self.h.settings.data_dir / "logs").glob("*"))
        self.assertNotIn(tok, logs)

    async def test_client_scope_is_enforced_server_side(self):
        a = await self.new_client("Alpha Plumbing LLC", website="https://alpha.test", phone="(555) 201-3344", internal_notes="owner owes us a favour",
                                  address={"city": "Springfield", "state": "IL"}, services=["Drain cleaning"])
        b = await self.new_client("Beta Roofing LLC", website="https://beta.test", phone="(555) 909-1111")
        r = await self.c.run("client.context", client=a)
        self.assertEqual(r["status"], "succeeded")
        blob = json.dumps(r["result"])
        self.assertIn("alpha.test", blob)
        self.assertNotIn("beta.test", blob)
        self.assertNotIn("909", blob)
        self.assertNotIn("favour", blob)                                               # HIGH field withheld ...
        self.assertIn("account.internal_notes", r["result"]["status"]["withheld_protected"])   # ... and only named
        self.assertEqual(r["result"]["summary"]["phone"], "(555) 201-3344")
        r = await self.c.run("client.context", client="Alpha Plumbing LLC")           # exact name works
        self.assertEqual(r["client_id"], a)
        st, d = await self.c.submit("client.context")
        self.assertEqual(d["error"]["code"], "CLIENT_REQUIRED")
        await self.new_client("Alpha Roofing LLC")
        st, d = await self.c.submit("client.context", client="Alpha")
        self.assertEqual(d["error"]["code"], "CLIENT_AMBIGUOUS")
        self.assertEqual(len(d["error"]["details"]["candidates"]), 2)
        _, t = await self.c.call("POST", "/api/collab/tasks", {"title": "alpha only", "client": a})
        tid = t["task"]["task_id"]
        st, d = await self.c.submit("client.context", task_id=tid, client=b)
        self.assertEqual((st, d["error"]["code"]), (403, "SCOPE_MISMATCH"))
        r = await self.c.run("client.context", task_id=tid)                            # the task's client is used, never a default
        self.assertEqual(r["client_id"], a)
        # a second collaborator cannot see the first one's jobs
        self.svc.callers.rotate("other", ("agent:read",))
        other = Collab(self.h, (self.h.settings.data_dir / "collab_other.token").read_text().strip())
        st, d = await other.call("GET", f"/api/collab/requests/{r['request_id']}")
        self.assertEqual(st, 404)


# =================================================================================================================  approvals
class ApprovalTests(CollabCase):
    async def test_side_effect_capability_waits_for_the_owner(self):
        st, d = await self.c.submit("test.sidefx", {})
        r = d["response"]
        self.assertEqual(r["status"], "awaiting_approval")
        await asyncio.sleep(0.3)
        self.assertNotIn("sidefx", COUNTS)
        # the collaborator has no route to approve
        st, _ = await self.c.call("POST", f"/hub/api/approvals/{r['approval_id']}/approve", {})
        self.assertIn(st, (401, 403))
        # a signed-in Hub session alone cannot approve: it needs a PIN to exist and to be typed
        st, x = await self.hub.post(f"/approvals/{r['approval_id']}/approve", {"note": "ok"})
        self.assertEqual((st, x["code"]), (409, "NO_PIN_SET"), x)
        HubPin(self.h.settings.data_dir).set("4711")
        st, x = await self.hub.post(f"/approvals/{r['approval_id']}/approve", {"note": "ok"})
        self.assertEqual((st, x["code"]), (401, "BAD_PIN"), x)
        st, x = await self.hub.post(f"/approvals/{r['approval_id']}/approve", {"note": "ok", "pin": "0000"})
        self.assertEqual((st, x["code"]), (401, "BAD_PIN"), x)
        _, still = await self.hub.get("/collab")
        self.assertEqual([a["status"] for a in still["approvals"]], ["proposed"], "nothing was approved by the refused attempts")
        st, x = await self.hub.post(f"/approvals/{r['approval_id']}/approve", {"note": "ok", "pin": "4711"})
        self.assertEqual(st, 200, x)
        _, g = await self.c.call("GET", f"/api/collab/requests/{r['request_id']}?wait=5")
        self.assertEqual(g["response"]["status"], "succeeded")
        st, d = await self.c.submit("test.sidefx", {"value": 2})
        await self.hub.post(f"/approvals/{d['response']['approval_id']}/deny", {})
        _, g = await self.c.call("GET", f"/api/collab/requests/{d['response']['request_id']}?wait=5")
        self.assertEqual((g["response"]["status"], g["response"]["error"]["code"]), ("cancelled", "APPROVAL_DENIED"))
        self.assertEqual(COUNTS["sidefx"], 1)
        _, v = await self.hub.get("/collab")
        self.assertEqual(sorted(a["status"] for a in v["approvals"]), ["approved", "denied"])

    async def test_cli_decision_written_to_the_db_is_picked_up(self):
        st, d = await self.c.submit("test.sidefx", {})
        self.svc.store.transition_approval(d["response"]["approval_id"], "approved", actor="owner:cli")
        _, g = await self.c.call("GET", f"/api/collab/requests/{d['response']['request_id']}?wait=8")
        self.assertEqual(g["response"]["status"], "succeeded")


# =================================================================================================================  browser
def fake_pages(serp_for_client_domain="alpha.test"):
    """Handlers for a fake extension that serves one page and one search-results page."""
    state = {"next": 500, "closed": [], "opened": []}

    def new_tab(p, cmd):
        state["next"] += 1
        state["opened"].append((state["next"], p.get("url")))
        state[state["next"]] = p.get("url")
        return {"result": {"tab": {"tab_id": state["next"], "url": p.get("url"), "status": "complete"}}}

    def is_serp(tid):
        return "search?" in (state.get(tid) or "")

    def page_state(p, cmd):
        if is_serp(p["tab_id"]):
            heads = [{"level": 3, "text": "Best Plumbers in Springfield - Rival Co"}, {"level": 3, "text": "Alpha Plumbing LLC | Drain cleaning Springfield"},
                     {"level": 3, "text": "Springfield plumbers - Yelp"}]
            return {"result": {"tab": {}, "page": {"url": state[p["tab_id"]], "title": "results", "headings": heads}}}
        return {"result": {"tab": {}, "page": {"url": state[p["tab_id"]], "title": "Rendered", "headings": [{"level": 1, "text": "Hello"}]}}}

    def read(p, cmd):
        return {"result": {"text": "Sponsored ... results text" if is_serp(p["tab_id"]) else "Rendered body text with Springfield", "url": state[p["tab_id"]], "title": "t", "total_chars": 30}}

    def inspect(p, cmd):
        els = [{"tag": "a", "name": "Best Plumbers in Springfield - Rival Co https://rival.test", "href": "https://rival.test/plumbers"},
               {"tag": "a", "name": "Sitelink", "href": "https://rival.test/contact"},
               {"tag": "a", "name": "Ad - Cheap plumbing", "href": "https://www.googleadservices.com/pagead/aclk?x=1"},
               {"tag": "a", "name": f"Alpha Plumbing LLC | Drain cleaning Springfield https://{serp_for_client_domain}", "href": f"https://www.{serp_for_client_domain}/drains/"},
               {"tag": "a", "name": "Springfield plumbers - Yelp https://yelp.com", "href": "https://www.google.com/url?q=https://www.yelp.com/search%3Fx&sa=U"},
               {"tag": "a", "name": "Alpha Plumbing LLC", "href": "https://www.google.com/maps/place/alpha"}]
        return {"result": {"snapshot_id": "s1", "tab_id": p.get("tab_id"), "url": state[p["tab_id"]], "elements": [{"id": f"el_{i:03d}", **e} for i, e in enumerate(els, 1)], "page": {"headings": []}}}

    def find(p, cmd):
        return {"result": {"found": True, "total_matches": 1, "matches": [{"context": "…with [[Springfield]]"}]}}

    def close(p, cmd):
        state["closed"].append(p["tab_id"])
        return {"result": {"closed": True}}

    return state, {"new_tab": new_tab, "get_page_state": page_state, "read_page": read, "inspect_page": inspect, "find_text": find, "close_tab": close, "wait": lambda p, c: {"result": {}}}


class BrowserTests(CollabCase):
    async def test_disconnected_browser_fails_cleanly_then_recovers(self):
        r = await self.c.run("browser.inspect_page", {"url": "https://example.test/"})
        self.assertEqual((r["status"], r["error"]["code"], r["retryable"]), ("failed", "EXTENSION_NOT_CONNECTED", True))
        state, handlers = fake_pages()
        await self.h.extension(handlers=handlers)
        r = await self.c.run("browser.inspect_page", {"url": "https://example.test/", "find": ["Springfield"]})
        self.assertEqual(r["status"], "succeeded", r)
        self.assertEqual(r["result"]["headings"][0]["text"], "Hello")
        self.assertTrue(r["result"]["find"]["Springfield"]["found"])
        self.assertEqual(state["closed"], [state["opened"][-1][0]])                    # its own tab, closed afterwards

    async def test_search_parses_organic_results_without_ads_sitelinks_or_engine_links(self):
        state, handlers = fake_pages()
        await self.h.extension(handlers=handlers)
        r = await self.c.run("search.web", {"query": "plumber springfield", "location": "Springfield,Illinois,United States"})
        self.assertEqual(r["status"], "succeeded", r)
        org = r["result"]["organic"]
        self.assertEqual([o["domain"] for o in org], ["rival.test", "alpha.test", "yelp.com"])
        self.assertEqual([o["position"] for o in org], [1, 2, 3])
        self.assertTrue(r["result"]["local_pack"]["detected"])
        self.assertTrue(r["result"]["ads_detected"])
        self.assertIn("uule=", r["result"]["request_url"])
        self.assertTrue(any("personalise" in l or "location" in l for l in r["limitations"]))


# =================================================================================================================  web + SEO on a local fixture site
def fixture_site() -> web.Application:
    app = web.Application()
    page = lambda title, body, extra_head="": web.Response(text=f"<!doctype html><html lang=en><head><title>{title}</title><meta name=viewport content='width=device-width'>{extra_head}</head><body>{body}</body></html>", content_type="text/html")

    async def home(r):
        host = r.host
        return page("Alpha Plumbing LLC | Springfield", f"<h1>Alpha Plumbing</h1><p>Call (555) 201-3344.</p><a href='/drains/'>Drains</a> <a href='/old'>Old</a> <a href='/hidden/'>Hidden</a><img src='a.jpg'>"
                    "<a href='/reset_password.php?u=1'>reset</a> <a href='/admin/'>admin</a>",
                    "<link rel=canonical href='http://" + host + "/'><script type='application/ld+json'>{\"@type\":\"Plumber\",\"telephone\":\"555-201-3344\"}</script>")

    async def drains(r):
        return page("Alpha Plumbing LLC | Springfield", "<h1>Drains</h1><h1>Second</h1><p>short</p>")

    async def hidden(r):
        return page("Hidden", "<h1>Hidden</h1>", "<meta name=robots content='noindex'>")

    async def robots(r):
        return web.Response(text=f"User-agent: *\nDisallow: /private/\nSitemap: http://{r.host}/sitemap.xml\n")

    async def sitemap(r):
        h = f"http://{r.host}"
        return web.Response(text=f"<?xml version='1.0'?><urlset><url><loc>{h}/</loc></url><url><loc>{h}/drains/</loc></url><url><loc>{h}/hidden/</loc></url><url><loc>{h}/orphan/</loc></url></urlset>",
                            content_type="application/xml")

    async def orphan(r):
        return page("Orphan page about water heaters", "<h1>Water heaters</h1>" + "<p>word</p>" * 300, "<meta name=description content='Water heater installs.'>")

    async def danger(r):
        HITS.append(r.path)
        return web.Response(text="side effect!")

    app.router.add_get("/reset_password.php", danger)
    app.router.add_get("/admin/", danger)
    app.router.add_get("/", home)
    app.router.add_get("/drains/", drains)
    app.router.add_get("/hidden/", hidden)
    app.router.add_get("/orphan/", orphan)
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/sitemap.xml", sitemap)
    return app


class WebSeoTests(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.site = TestServer(fixture_site(), host="127.0.0.1")
        await self.site.start_server()
        self.url = f"http://127.0.0.1:{self.site.port}/"
        self.patches = [mock.patch.object(F, "_is_public", lambda ip: True), mock.patch.object(F, "ALLOWED_PORTS", {None, 80, 443, self.site.port})]

    async def asyncTearDown(self):
        await self.site.close()
        await super().asyncTearDown()

    async def test_fetch_refuses_private_and_local_targets(self):
        for u in (f"http://127.0.0.1:{self.h.server.port}/api/state", "http://localhost/", "http://10.0.0.1/", "file:///C:/Windows/win.ini", "http://user:pw@example.com/",
                  "http://169.254.169.254/latest/meta-data/"):
            r = await self.c.run("web.fetch_page", {"url": u})
            self.assertEqual(r["status"], "failed", u)
            self.assertIn(r["error"]["code"], ("BLOCKED_ADDRESS", "BAD_URL", "BLOCKED_PORT"), (u, r["error"]))

    async def test_audit_report_handoff_change_verify_end_to_end(self):
        with self.patches[0], self.patches[1]:
            cid = await self.new_client("Alpha Plumbing LLC", website=self.url, phone="(555) 201-3344", address={"city": "Springfield", "state": "IL"})
            _, t = await self.c.call("POST", "/api/collab/tasks", {"title": "SEO audit", "client": cid})
            tid = t["task"]["task_id"]
            r = await self.c.run("seo.campaign_upsert", {"objective": "more drain calls", "locations": ["Springfield, IL"],
                                                         "keywords": ["drain cleaning | Springfield, IL", "Drain  Cleaning | springfield, il", "water heater install | Chicago, IL"]}, task_id=tid)
            self.assertEqual(r["status"], "succeeded", r)
            self.assertEqual(len(r["result"]["added"]), 2)
            self.assertEqual(len(r["result"]["duplicates"]), 1)                        # case/space-insensitive de-duplication
            self.assertTrue(any("Chicago" in w for w in r["result"]["warnings"]))      # outside the known service area
            a = await self.c.run("seo.audit_site", {"max_pages": 10}, task_id=tid, wait=60)
            self.assertEqual(a["status"], "succeeded", a)
            rules = {f["rule"]: f for f in a["result"]["findings"]}
            self.assertEqual(HITS, [], "the crawler requested an unsafe-looking URL")
            for expected in ("broken_pages", "title_duplicate", "noindex_page", "h1_multiple", "img_alt_missing", "meta_description_missing", "orphan_pages"):
                self.assertIn(expected, rules, sorted(rules))
            self.assertNotIn("nap_phone_absent", rules)                               # the phone is on the page and in JSON-LD
            self.assertEqual(rules["noindex_page"]["severity"], "high")                # noindex AND in the sitemap
            for f in a["result"]["findings"]:
                for k in ("evidence", "severity", "confidence", "recommendation", "verification", "pages", "priority_score", "type"):
                    self.assertIn(k, f)
            self.assertTrue(a["evidence"])
            self.assertTrue(all(e["sha256"] for e in a["evidence"]))
            # observations are dated and traceable to the request
            obs = self.svc.admin().research.observations(cid, limit=500)
            self.assertTrue(obs and all(o["observed_at"] for o in obs))
            self.assertTrue(any(a["request_id"] in (o["data"] or "") for o in obs))
            conn = await self.c.run("connectors.status")
            rep = await self.c.run("seo.report", {"connectors": conn["result"]}, task_id=tid)
            self.assertEqual(rep["status"], "succeeded", rep)
            md = rep["result"]["markdown"]
            for section in ("## Executive summary", "## Data sources", "## Baseline", "## Competitor findings", "## Prioritised recommendations", "## Missing data", "## Implementation tasks", "## Success criteria"):
                self.assertIn(section, md)
            self.assertIn("Search Console is not connected", md)
            self.assertTrue(Path(rep["result"]["markdown_path"]).is_file())
            # the collaborator pulls the report artifact itself (no copying)
            _, art = await self.c.call("GET", f"/api/collab/artifacts/{rep['result']['artifact_id']}")
            self.assertEqual(art["artifact"]["body_md"], md)
            site_dir = self.h.tmp / "site"
            (site_dir / "drains").mkdir(parents=True)
            (site_dir / "drains" / "index.html").write_text("x")
            tg = await self.c.run("target.register", {"domain": f"127.0.0.1:{self.site.port}", "environment": "production", "local_path": str(site_dir), "deploy_method": "test"}, task_id=tid)
            self.assertEqual(tg["status"], "succeeded", tg)
            fid = rules["h1_multiple"]["finding_id"]
            hd = await self.c.run("handoff.create", {"finding_ids": [fid]}, task_id=tid)
            self.assertEqual(hd["status"], "succeeded", hd)
            pkg = hd["result"]["package"]
            self.assertEqual(pkg["research"]["source_artifact_id"], a["result"]["artifact_id"])
            self.assertTrue(pkg["file_references"][0]["candidates"][0].endswith("index.html"))
            self.assertEqual(pkg["approval_requirements"][0]["action_kind"], "production_deploy")
            for k in ("business_context", "findings", "pages", "constraints", "acceptance_criteria", "test_requirements", "rollback"):
                self.assertTrue(pkg[k], k)
            got = await self.c.run("handoff.get", {"handoff_id": hd["result"]["handoff_id"]}, task_id=tid)
            self.assertEqual(got["result"]["package"]["findings"][0]["finding_id"], fid)
            ch = await self.c.run("change.record", {"handoff_id": hd["result"]["handoff_id"], "summary": "demote second h1", "files": ["drains/index.html"],
                                                    "tests": [{"name": "php -l", "passed": True}, {"name": "local render", "passed": False}], "status": "ready_to_deploy"}, task_id=tid)
            self.assertEqual(ch["result"]["status"], "implemented")                   # failing test: cannot be ready_to_deploy
            ch = await self.c.run("change.record", {"handoff_id": hd["result"]["handoff_id"], "summary": "demote second h1", "files": ["drains/index.html"],
                                                    "tests": [{"name": "local render", "passed": True}], "status": "ready_to_deploy"}, task_id=tid)
            chid = ch["result"]["change_id"]
            # skipped tests: allowed only with a reason, never alone, and they travel with the approval request
            hid = hd["result"]["handoff_id"]
            for bad in ([{"name": "render", "skipped": True, "detail": "x"}], [{"name": "render", "skipped": True, "detail": "cannot render locally; checked by verify.page"}]):
                r = await self.c.run("change.record", {"handoff_id": hid, "summary": "summary one", "files": ["a"], "tests": bad, "status": "tested"}, task_id=tid)
                self.assertEqual(r["error"]["code"], "BAD_TESTS")
            sk = await self.c.run("change.record", {"handoff_id": hid, "summary": "with a skipped render", "files": ["a"], "status": "ready_to_deploy",
                                                    "tests": [{"name": "lint", "passed": True}, {"name": "render", "skipped": True, "detail": "cannot render locally; checked by verify.page after deploy"}]}, task_id=tid)
            self.assertEqual((sk["result"]["status"], sk["result"]["skipped_tests"]), ("ready_to_deploy", ["render"]))
            apx = await self.c.run("approval.request", {"action_kind": "production_deploy", "title": "Deploy skipped-test change", "artifact_id": sk["result"]["change_id"]}, task_id=tid)
            self.assertEqual(self.svc.store.get_approval(apx["result"]["approval_id"])["details"]["change"]["skipped_tests"][0]["name"], "render")
            impl = await self.c.run("change.record", {"handoff_id": hid, "summary": "untested", "files": ["a"], "tests": [{"name": "lint", "passed": False}], "status": "implemented"}, task_id=tid)
            r = await self.c.run("approval.request", {"action_kind": "production_deploy", "title": "Deploy an untested change", "artifact_id": impl["result"]["change_id"]}, task_id=tid)
            self.assertEqual(r["error"]["code"], "NOT_READY")
            self.svc.store.transition_approval(apx["result"]["approval_id"], "withdrawn", actor="test")
            # approval gate on deployment
            d = await self.c.run("change.mark_deployed", {"change_id": chid, "operation": "deploy", "succeeded": True}, task_id=tid)
            self.assertEqual(d["error"]["code"], "APPROVAL_REQUIRED")
            ap = await self.c.run("approval.request", {"action_kind": "production_deploy", "title": "Deploy h1 fix", "artifact_id": chid}, task_id=tid)
            apid = ap["result"]["approval_id"]
            d = await self.c.run("change.mark_deployed", {"change_id": chid, "approval_id": apid, "operation": "deploy", "succeeded": True}, task_id=tid)
            self.assertEqual(d["error"]["code"], "APPROVAL_REQUIRED")                 # proposed is not approved
            HubPin(self.h.settings.data_dir).set("4711")
            await self.hub.post(f"/approvals/{apid}/approve", {"pin": "4711"})
            d = await self.c.run("change.mark_deployed", {"change_id": chid, "approval_id": apid, "operation": "deploy", "succeeded": True, "detail": "ok"}, task_id=tid)
            self.assertEqual((d["status"], d["result"]["approval"]["status"]), ("succeeded", "executed"))
            v = await self.c.run("verify.page", {"url": self.url + "drains/", "checks": [{"type": "status", "value": 200}, {"type": "h1_count", "value": 2}], "change_id": chid}, task_id=tid)
            self.assertEqual(v["status"], "succeeded", v)
            self.assertTrue(v["result"]["passed"])
            self.assertEqual(v["result"]["change_status"], "verified")
            self.assertEqual(self.svc.store.get_approval(apid)["status"], "verified")
            vf = self.svc.store.get_artifact(v["result"]["verification_id"])
            self.assertEqual((vf["kind"], vf["stage"], vf["status"]), ("verification", "verification", "passed"))
            self.assertGreater(vf["body"]["follow_up_on"], time.time() + 86400 * 20)
            bad = await self.c.run("verify.page", {"url": self.url + "drains/", "checks": [{"type": "h1_count", "value": 1}]}, task_id=tid)
            self.assertFalse(bad["result"]["passed"])
            self.assertEqual(bad["result"]["checks"][0]["actual"], 2)
            st, fin = await self.c.call("POST", f"/api/collab/tasks/{tid}/finish", {"status": "completed", "outcome": "audit+fix verified"})
            self.assertEqual(fin["task"]["status"], "completed")
            _, view = await self.hub.get(f"/seo?client={cid}")
            self.assertEqual(len(view["clients"][0]["campaigns"]), 1)

    async def test_an_unreadable_results_page_never_becomes_a_ranking_observation(self):
        state, handlers = fake_pages("alpha.test")
        handlers["inspect_page"] = lambda p, c: {"result": {"snapshot_id": "s", "tab_id": p.get("tab_id"), "url": "https://www.google.com/search?q=x", "elements": [], "page": {"headings": []}}}
        await self.h.extension(handlers=handlers)
        cid = await self.new_client("Alpha Plumbing LLC", website="https://alpha.test")
        await self.c.run("seo.campaign_upsert", {"keywords": ["plumber | Springfield, IL"]}, client=cid)
        r = await self.c.run("seo.serp_observe", {}, client=cid, wait=30)
        self.assertEqual(r["result"]["keywords"][0]["error"], "NO_RESULTS_PARSED")
        self.assertEqual(r["completeness"], "none")
        self.assertEqual([o for o in self.svc.admin().research.observations(cid, limit=50) if o["kind"] == "keyword_rank"], [])
        camp = (await self.c.run("seo.campaign_get", {}, client=cid))["result"]["campaigns"][0]
        self.assertIsNone(camp["baseline_date"])                                       # no baseline from a page that could not be read
        r = await self.c.run("seo.competitors", {}, client=cid)
        self.assertEqual(r["error"]["code"], "NO_SERP_DATA")

    async def test_serp_observations_are_recorded_with_classification_and_baseline(self):
        state, handlers = fake_pages("alpha.test")
        await self.h.extension(handlers=handlers)
        cid = await self.new_client("Alpha Plumbing LLC", website="https://alpha.test", address={"city": "Springfield", "state": "IL"})
        await self.c.run("seo.campaign_upsert", {"keywords": ["plumber | Springfield, IL"]}, client=cid)
        r = await self.c.run("seo.serp_observe", {}, client=cid, wait=30)
        self.assertEqual(r["status"], "succeeded", r)
        kw = r["result"]["keywords"][0]
        self.assertEqual(kw["client_position"], 2)
        self.assertEqual([t["class"] for t in kw["top"]], ["competitor", "client", "directory"])
        self.assertTrue(kw["client_in_local_pack"])
        obs = self.svc.admin().research.observations(cid, limit=100)
        kinds = sorted({o["kind"] for o in obs})
        self.assertEqual(kinds, ["keyword_rank", "local_pack_result", "serp_result"])
        rank = next(o for o in obs if o["kind"] == "keyword_rank")
        self.assertEqual((rank["position"], rank["search_engine"], rank["location"]), (2, "google", "Springfield, IL"))
        camp = (await self.c.run("seo.campaign_get", {}, client=cid))["result"]["campaigns"][0]
        self.assertEqual(camp["status"], "baseline_set")
        self.assertTrue(camp["baseline_session_id"])


# =================================================================================================================  bot protection: detect, never bypass, read like a browser
def challenge_site() -> web.Application:
    app = web.Application()

    async def wall(r):
        return web.Response(status=403, text="<html><head><title>Checking your browser before accessing. Just a moment...</title></head><body>spinner</body></html>", content_type="text/html")
    app.router.add_get("/{tail:.*}", wall)
    return app


def browser_site(base: str):
    """Fake extension serving a 2-page site through navigate/read/inspect (what the real Chrome sees past the CDN)."""
    pages = {base + "/": {"title": "24/7 Emergency Towing & Mobile Locksmith | Alpha Towing · Springfield, IL | Alpha Towing & Lockout Service Incorporated",
                          "headings": [{"level": 1, "text": "Stranded? We roll out in minutes."}, {"level": 2, "text": "Towing in Springfield"}],
                          "text": "Call (555) 201-3344 any time. " + "towing " * 400, "hrefs": [base + "/services/towing/", "tel:+15552013344"]},
             base + "/services/towing/": {"title": "Towing", "headings": [{"level": 2, "text": "Towing"}], "text": "short", "hrefs": [base + "/"]}}
    st = {"tab": None, "url": None, "nav": []}

    def new_tab(p, c):
        st["tab"], st["url"] = 900, p.get("url")
        return {"result": {"tab": {"tab_id": 900}}}

    def navigate(p, c):
        st["url"] = p["url"].rstrip("/") + "/" if not p["url"].endswith(".txt") and not p["url"].endswith(".xml") else p["url"]
        st["nav"].append(st["url"])
        return {"result": {"tab": {"tab_id": 900, "url": st["url"]}}}

    def page():
        return pages.get(st["url"]) or {"title": "404 Not Found", "headings": [], "text": "", "hrefs": []}

    return st, {"new_tab": new_tab, "navigate": navigate, "close_tab": lambda p, c: {"result": {}}, "wait": lambda p, c: {"result": {}},
                "get_page_state": lambda p, c: {"result": {"page": {"url": st["url"], "title": page()["title"], "headings": page()["headings"]}}},
                "read_page": lambda p, c: {"result": {"text": page()["text"], "url": st["url"]}},
                "inspect_page": lambda p, c: {"result": {"snapshot_id": "s", "tab_id": 900, "url": st["url"], "elements": [{"id": f"el_{i}", "tag": "a", "name": "x", "href": h} for i, h in enumerate(page()["hrefs"])],
                                                         "page": {"headings": page()["headings"]}}}}


class ChallengeTests(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.site = TestServer(challenge_site(), host="127.0.0.1")
        await self.site.start_server()
        self.base = f"http://127.0.0.1:{self.site.port}"
        self.p1, self.p2 = mock.patch.object(F, "_is_public", lambda ip: True), mock.patch.object(F, "ALLOWED_PORTS", {None, 80, 443, self.site.port})
        self.p1.start()
        self.p2.start()

    async def asyncTearDown(self):
        self.p1.stop()
        self.p2.stop()
        await self.site.close()
        await super().asyncTearDown()

    async def test_bot_wall_is_never_audited_as_the_site_and_the_browser_reads_it_instead(self):
        cid = await self.new_client("Alpha Towing & Lockout Service", website=self.base + "/", phone="(555) 201-3344")
        r = await self.c.run("seo.audit_site", {"mode": "http"}, client=cid, wait=30)
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "SITE_CHALLENGE"))   # http only: fail loudly, no fake 403 "defects"
        self.assertFalse(self.svc.store.artifacts(client_id=cid, kind="finding_set"))
        state, handlers = browser_site(self.base)
        await self.h.extension(handlers=handlers)
        r = await self.c.run("seo.audit_site", {"max_pages": 5}, client=cid, wait=60)
        self.assertEqual(r["status"], "succeeded", r)
        self.assertEqual(r["result"]["mode"], "browser")
        self.assertEqual(r["completeness"], "partial")
        self.assertTrue(any("bot-protection" in l for l in r["limitations"]))
        self.assertTrue(any("not measured" in l and "meta description" in l for l in r["limitations"]))
        rules = {f["rule"] for f in r["result"]["findings"]}
        self.assertIn("title_long", rules)                                            # measured in the browser
        self.assertIn("h1_missing", rules)                                            # /services/towing/ has no H1
        for unmeasurable in ("meta_description_missing", "canonical_missing", "localbusiness_schema_missing", "img_alt_missing", "broken_pages", "robots_missing", "sitemap_missing", "host_variant"):
            self.assertNotIn(unmeasurable, rules)
        self.assertNotIn("nap_phone_absent", rules)                                  # the phone is in the rendered text
        self.assertTrue(all("password" not in u and "admin" not in u for u in state["nav"]))
        # verification behind the same wall: plain HTTP refuses, the browser verifies what it can see, the rest is NOT MEASURED (never guessed)
        v = await self.c.run("verify.page", {"url": self.base + "/", "mode": "http", "checks": [{"type": "status", "value": 200}]}, client=cid)
        self.assertEqual(v["error"]["code"], "SITE_CHALLENGE")
        v = await self.c.run("verify.page", {"url": self.base + "/", "cache_bust": False, "checks": [
            {"type": "title_contains", "value": "Alpha Towing"}, {"type": "link_present", "value": "/services/towing/"}, {"type": "link_absent", "value": "/towing/"},
            {"type": "tel_link_present"}, {"type": "status", "value": 200}, {"type": "meta_description_contains", "value": "x"}]}, client=cid, wait=30)
        self.assertEqual(v["status"], "succeeded", v)
        self.assertEqual(v["result"]["via"], "browser")
        self.assertEqual([c["passed"] for c in v["result"]["checks"]], [True, True, True, True, None, None])
        self.assertTrue(v["result"]["passed"])
        self.assertEqual(v["completeness"], "partial")
        # a wrong finding set can be invalidated with a reason; reports then ignore it
        inv = await self.c.run("artifact.invalidate", {"artifact_id": r["result"]["artifact_id"], "reason": "test: pretend this crawl read the wrong thing"}, client=cid)
        self.assertEqual(inv["result"]["status"], "invalidated")
        rep = await self.c.run("seo.report", {}, client=cid)
        self.assertIn("No site audit", " ".join(rep["result"]["report"]["missing_data"]))
        h = await self.c.run("handoff.create", {"source_artifact_id": r["result"]["artifact_id"]}, client=cid)
        self.assertEqual(h["error"]["code"], "INVALIDATED")


# =================================================================================================================  MCP adapter
class McpTests(CollabCase):
    async def test_mcp_stdio_adapter_round_trip(self):
        import os
        import sys
        env = {**os.environ, "OCTG_COLLAB_BASE": self.h.base, "OCTG_COLLAB_TOKEN_FILE": str(self.h.settings.data_dir / "collab_collaborator.token"), "PYTHONIOENCODING": "utf-8"}
        p = await asyncio.create_subprocess_exec(sys.executable, "-m", "octg_agent.collab.mcp_server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, env=env, cwd=str(Path(__file__).resolve().parents[1]))
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "collab_run", "arguments": {"capability": "test.echo", "parameters": {"value": 4}}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "collab_run", "arguments": {"capability": "agent.browser_task", "parameters": {"instruction": "x y z"}}}}]
        out, err = await asyncio.wait_for(p.communicate(("\n".join(json.dumps(m) for m in msgs) + "\n").encode()), timeout=60)
        replies = {r["id"]: r for r in (json.loads(l) for l in out.decode().splitlines() if l.strip())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "octg-collab")
        self.assertIn("collab_run", [t["name"] for t in replies[2]["result"]["tools"]])
        r3 = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual((r3["status"], r3["result"]["doubled"]), ("succeeded", 8))
        self.assertTrue(replies[4]["result"]["isError"])                               # same permission rules through MCP
        self.assertIn("PERMISSION_DENIED", replies[4]["result"]["content"][0]["text"])


# =================================================================================================================  pure units
class UnitTests(unittest.TestCase):
    def test_signals_extract(self):
        s = extract("<html lang=en><head><title> T </title><meta name=description content=D><meta name=robots content='noindex,follow'><link rel=canonical href='/x'>"
                    "<script type='application/ld+json'>{\"@graph\":[{\"@type\":\"LocalBusiness\"},{\"@type\":[\"Plumber\",\"Thing\"]}]}</script></head>"
                    "<body><h1>A</h1><h2>B</h2><a href='/in'>in</a><a href='https://other.test/'>out</a><a href='tel:+15551234567'>call</a><img src=i.png><img src=j.png alt=''>"
                    "<script>var hidden = 'not text';</script>Hello world</body></html>", "https://www.site.test/page")
        self.assertEqual((s["title"], s["meta_description"], s["noindex"], s["canonical"]), ("T", "D", True, "https://www.site.test/x"))
        self.assertEqual(s["jsonld_types"], ["LocalBusiness", "Plumber", "Thing"])
        self.assertEqual((s["internal_link_count"], s["external_link_count"], s["tel_links"]), (1, 1, ["+15551234567"]))
        self.assertEqual((s["images"], s["images_missing_alt"], s["images_empty_alt"]), (2, 1, 1))
        self.assertNotIn("not text", s["text"])

    def test_audit_groups_pages_finds_soft_404s_and_weakly_linked_siblings(self):
        from octg_agent.seo import audit as AUDIT

        def pg(url, title, inbound=30, linked_from=()):
            return {"url": url, "final_url": url, "status": 200, "in_sitemap": False, "inbound_internal_links": inbound, "linked_from": list(linked_from),
                    "signals": {"title": title, "h1": ["H"], "word_count": 900, "meta_description": "d" * 120, "canonical": url, "viewport": "w", "lang": "en", "tel_links": ["1"],
                                "jsonld_types": ["LocalBusiness"]}}
        long = "A very long title that keeps going well past what a results page can show in full x"
        pages = [pg("https://s.test/", "Home page title for S company here")] + [pg(f"https://s.test/areas/t{i}.php", long + str(i)) for i in range(6)]
        pages += [pg("https://s.test/areas/new.php", long + "n", inbound=3), pg("https://s.test/gone.php", "This Page Does Not Exist", inbound=1, linked_from=["https://s.test/"])]
        F = AUDIT.run({"pages": pages, "robots": {}, "sitemap": {}, "mode": "browser", "measured": ["title", "h1", "tel", "links", "words"]})
        rules = {f["rule"]: f for f in F}
        self.assertEqual(len(rules["title_long"]["pages"]), 7)                          # one grouped finding, every page listed
        self.assertEqual(sum(1 for f in F if f["rule"] == "title_long"), 1)
        self.assertEqual(rules["broken_internal_links"]["pages"], ["https://s.test/gone.php"])
        self.assertIn("linked from https://s.test/", rules["broken_internal_links"]["evidence"][0]["detail"])
        self.assertEqual(rules["broken_internal_links"]["confidence"], "likely")            # browser mode: status code unknown
        self.assertEqual(rules["weakly_linked_pages"]["pages"], ["https://s.test/areas/new.php"])
        self.assertNotIn("title_duplicate", rules)
        self.assertNotIn("meta_description_missing", rules)

    def test_robots_rules(self):
        r = parse_robots("User-agent: *\nDisallow: /private/\nAllow: /private/ok\nSitemap: https://x.test/s.xml\n\nUser-agent: Googlebot\nDisallow: /g/\n")
        self.assertEqual(r["sitemaps"], ["https://x.test/s.xml"])
        self.assertFalse(robots_allows(r["star"], "/private/a"))
        self.assertTrue(robots_allows(r["star"], "/private/ok"))
        self.assertTrue(robots_allows(r["star"], "/"))
        self.assertFalse(robots_allows(r["googlebot"], "/g/1"))

    def test_map_pack_names_come_from_directions_links_in_order(self):
        els = [{"tag": "a", "name": "Maps", "href": "https://maps.google.com/maps?q=towing&source=lnms"},
               {"tag": "a", "name": "Directions", "href": "https://www.google.com/maps/dir//24%2F7+Buffalo+Towing,+415+Rhode+Island+St,+Buffalo,+NY/data=!4m6"},
               {"tag": "a", "name": "Directions", "href": "https://www.google.com/maps/dir//Citys+Towing+%26+Lockout+Service+Inc,+Buffalo,+NY/data=!4m6"}]
        r = SERP.parse("google", els, [], "Businesses ...", "https://www.google.com/search?q=x")
        self.assertTrue(r["local_pack"]["detected"])
        self.assertEqual(r["local_pack"]["names"], ["24/7 Buffalo Towing", "Examples Towing & Lockout Service Inc"])
        self.assertTrue(SERP.in_pack("Example Towing & Lockout Service", r["local_pack"]["names"]))
        self.assertFalse(SERP.parse("google", els[:1], [], "", "https://www.google.com/search?q=x")["local_pack"]["detected"])   # the Maps tab alone is not a pack

    def test_wrapped_result_links_fall_back_to_the_domain_label_and_empty_pages_parse_to_nothing(self):
        wrapped = "https://www.bing.com/ck/a?!&&p=866142445e871cd8e1f34b173bacbb8eb64a4851f0869369a79f730ad7e9d2c4Jmltd"
        els = [{"tag": "a", "name": "IMAGES", "href": "https://www.bing.com/ck/a?!&&p=aaaa"}, {"tag": "a", "name": "rival.test", "href": wrapped},
               {"tag": "a", "name": "Rival Towing - Buffalo", "href": wrapped}]
        r = SERP.parse("bing", els, [{"level": 2, "text": "Rival Towing - Buffalo"}], "text", "https://www.bing.com/search?q=x")
        self.assertEqual([(o["domain"], o.get("url_inferred")) for o in r["organic"]], [("rival.test", True)])
        self.assertEqual(SERP.parse("bing", els[:1], [], "text", "https://www.bing.com/search?q=x")["organic"], [])

    def test_serp_block_detection_and_uule(self):
        self.assertEqual(SERP.parse("google", [], [], "Our systems have detected unusual traffic", "https://www.google.com/sorry/index")["blocked"], "CAPTCHA")
        self.assertEqual(SERP.detect_block("https://consent.google.com/x", ""), "CONSENT_REQUIRED")
        self.assertTrue(SERP.uule("Portland,Oregon,United States").startswith("w+CAIQICI"))
        self.assertEqual(SERP._real_target("https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8"), "https://example.com/")

    def test_fetch_url_rules(self):
        for u in ("ftp://x.test/", "http://u:p@x.test/", "http://x.test:22/", "javascript:alert(1)"):
            with self.assertRaises(F.FetchError):
                F.normalise_url(u)
        self.assertEqual(F.normalise_url("example.com"), "https://example.com")
        self.assertFalse(F._is_public("127.0.0.1"))
        self.assertFalse(F._is_public("192.168.1.10"))
        self.assertFalse(F._is_public("::ffff:10.0.0.1"))
        self.assertTrue(F._is_public("93.184.216.34"))


if __name__ == "__main__":
    unittest.main()


class IdTests(unittest.TestCase):
    def test_generated_ids_are_never_mistaken_for_a_secret_by_the_request_scanner(self):
        from octg_agent.collab.service import _secret_like
        from octg_agent.collab.store import new_id
        for prefix in ("ar", "rq", "ct", "lp", "ev", "ap", "cm", "ob"):
            for _ in range(20000):
                i = new_id(prefix)
                self.assertFalse(i.split("_", 1)[1].isdigit(), i)
        self.assertEqual([i for i in (new_id("ar") for _ in range(50000)) if _secret_like({"ids": [i]})], [], "no generated id may trip the credential/12-digit-identifier scan")
