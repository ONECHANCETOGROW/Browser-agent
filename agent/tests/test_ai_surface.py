"""The surfaces that expose the AI layer: side-panel commands over the extension WebSocket, the HTTP task API and the CLI parsers."""
import asyncio
import json
import os
import unittest

from octg_agent.api import cli
from octg_agent.brain.settings import BrainSettings
from octg_agent.reasoning.providers.mock import MockProvider

from .fakebrowser import FakeBrowser, shop_pages
from .helpers import Harness, wait_until
from .test_brain_loop import T, eid, finish


class PanelCommandTests(unittest.IsolatedAsyncioTestCase):
    async def setup(self, script):
        self.fb = FakeBrowser(shop_pages(), "https://shop.test/")
        self.prov = MockProvider(script, name="local", local=True)
        self.h = Harness(providers={"local": self.prov}, brain=BrainSettings(router={"policy": "FALLBACK", "order": ["local"]}))
        await self.h.__aenter__()
        self.ext = await self.h.extension(handlers=self.fb.handlers())

    async def asyncTearDown(self):
        await self.h.__aexit__(None, None, None)

    def find(self, kind, pred=lambda m: True):
        return [m for m in self.ext.others if m.get("type") == kind and pred(m)]

    async def test_panel_receives_providers_on_connect_and_never_a_key(self):
        os.environ["GEMINI_API_KEY"] = "SECRET-PANEL-KEY-123456"
        try:
            await self.setup([])
            await wait_until(lambda: self.find("providers"))
            msg = self.find("providers")[0]
            self.assertEqual([p["name"] for p in msg["providers"]], ["local"])
            self.assertEqual(set(msg["providers"][0]), {"name", "model", "available", "local"})
            self.assertIn("LOCAL_FIRST", msg["policies"])
            self.assertNotIn("SECRET-PANEL-KEY", json.dumps(self.ext.others))
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    async def test_start_task_from_the_panel_streams_updates_until_completion(self):
        await self.setup([T("navigate", url="shop.test"), lambda m, t, o: finish("done")])
        await self.ext.send({"type": "ui", "command": "start_task", "instruction": "open the shop", "request_id": "r1"})
        await wait_until(lambda: self.find("task_update", lambda m: m["task"]["status"] == "COMPLETED"))
        res = self.find("ui_result", lambda m: m["request_id"] == "r1")[0]
        self.assertTrue(res["ok"])
        self.assertEqual(res["task"]["user_instruction"], "open the shop")
        statuses = [m["task"]["status"] for m in self.find("task_update")]
        self.assertIn("RUNNING", statuses)
        self.assertEqual(statuses[-1], "COMPLETED")
        status_msgs = self.find("status")
        self.assertTrue(any(m.get("task") for m in status_msgs), "the panel must be told the current task label")
        update = self.find("task_update")[-1]["task"]
        self.assertNotIn("latest_observation", update)

    async def test_approval_buttons_work_over_the_socket(self):
        script = [T("navigate", url="shop.test"), lambda m, t, o: T("click", element_id=eid(o, "Delete account")), lambda m, t, o: finish("ok")]
        await self.setup(script)
        await self.ext.send({"type": "ui", "command": "start_task", "instruction": "delete it", "request_id": "r1"})
        await wait_until(lambda: self.find("task_update", lambda m: m["task"]["status"] == "WAITING_FOR_APPROVAL"))
        pend = self.find("task_update", lambda m: m["task"]["status"] == "WAITING_FOR_APPROVAL")[-1]["task"]["pending_approval"]
        self.assertIn("Delete account", pend["description"])
        await self.ext.send({"type": "ui", "command": "approve", "request_id": "r2"})
        await wait_until(lambda: self.find("task_update", lambda m: m["task"]["status"] == "COMPLETED"))
        self.assertTrue(self.find("ui_result", lambda m: m["request_id"] == "r2")[0]["ok"])
        self.assertEqual(len([a for a, _ in self.fb.log if a == "click"]), 1)

    async def test_errors_are_reported_to_the_panel_not_swallowed(self):
        await self.setup([])
        for payload in ({"command": "start_task", "instruction": "   "}, {"command": "approve"}, {"command": "bogus"}):
            await self.ext.send({"type": "ui", "request_id": payload["command"], **payload})
        await wait_until(lambda: len(self.find("ui_result")) >= 3)
        results = {m["request_id"]: m for m in self.find("ui_result")}
        self.assertFalse(results["start_task"]["ok"])
        self.assertEqual(results["start_task"]["code"], "EMPTY_TASK")
        self.assertFalse(results["approve"]["ok"])
        self.assertFalse(results["bogus"]["ok"])

    async def test_get_status_returns_task_and_providers(self):
        await self.setup([finish("x")])
        await self.ext.send({"type": "ui", "command": "get_status", "request_id": "s"})
        await wait_until(lambda: self.find("ui_result", lambda m: m["request_id"] == "s"))
        res = self.find("ui_result")[-1]
        self.assertTrue(res["ok"])
        self.assertEqual(res["providers"]["providers"][0]["name"], "local")


class HttpTaskApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_lifecycle_over_http_and_key_secrecy(self):
        os.environ["GEMINI_API_KEY"] = "SECRET-HTTP-KEY-987654"
        try:
            fb = FakeBrowser(shop_pages(), "https://shop.test/")
            prov = MockProvider([T("navigate", url="shop.test"), lambda m, t, o: finish("ok")], name="local", local=True)
            gem = MockProvider([], name="gemini")
            async with Harness(providers={"local": prov, "gemini": gem}, brain=BrainSettings(router={"policy": "FALLBACK", "order": ["local", "gemini"]})) as h:
                await h.extension(handlers=fb.handlers())
                s, d = await h.api("POST", "/api/tasks", json_body={"instruction": "open the shop", "provider": "local"})
                self.assertEqual((s, d["ok"]), (200, True))
                tid = d["task"]["task_id"]
                await wait_until(lambda: h.rt.tasks.tasks[tid].status.value == "COMPLETED")
                s, d = await h.api("GET", f"/api/tasks/{tid}")
                self.assertEqual(d["task"]["status"], "COMPLETED")
                self.assertEqual(d["task"]["usage"]["by_provider"]["local"]["calls"], 2)
                s, d = await h.api("GET", "/api/tasks")
                self.assertEqual(d["tasks"][0]["task_id"], tid)
                s, d = await h.api("POST", "/api/tasks/current/stop")
                self.assertFalse(d["ok"])  # already finished
                s, d = await h.api("POST", "/api/tasks", json_body={"instruction": "x", "provider": "nope"})
                self.assertEqual(d["error"]["code"], "UNKNOWN_PROVIDER")
                s, d = await h.api("POST", "/api/tasks", json_body={"instruction": "x", "policy": "WRONG"})
                self.assertEqual(d["error"]["code"], "BAD_POLICY")
                self.assertEqual((await h.api("POST", "/api/tasks", json_body={"nope": 1}))[0], 400)
                s, d = await h.api("GET", "/api/providers")
                self.assertTrue(d["ok"])
                gem_row = next(p for p in d["providers"] if p["name"] == "gemini")
                self.assertEqual(gem_row["key_configured"], True)
                self.assertEqual(gem_row["key_source"], "env:GEMINI_API_KEY")
                s, d = await h.api("POST", "/api/providers/local/test")
                self.assertTrue(d["ok"] is False or d["ok"] is True)
                # the key must not appear in ANY response or in logs/history
                blobs = []
                for path in ("/api/providers", "/api/tasks", f"/api/tasks/{tid}", "/api/state", "/api/logs?tail=100", "/api/providers/detect-local"):
                    blobs.append(json.dumps((await h.api("GET", path))[1]))
                blobs.append(json.dumps(h.rt.history.list()))
                self.assertNotIn("SECRET-HTTP-KEY", "".join(blobs))
                for route in ("/api/providers/gemini/key", "/api/keys", "/api/providers/gemini/set-key"):
                    self.assertIn((await h.api("POST", route, json_body={"key": "x"}))[0], (404, 405))
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    async def test_task_routes_require_the_api_token_and_reject_web_pages(self):
        async with Harness(providers={}) as h:
            self.assertEqual((await h.api("POST", "/api/tasks", json_body={"instruction": "x"}, token=None))[0], 401)
            self.assertEqual((await h.api("GET", "/api/providers", token="wrong"))[0], 401)
            self.assertEqual((await h.api("POST", "/api/tasks", json_body={"instruction": "x"}, headers={"Origin": "https://evil.example"}))[0], 403)

    async def test_provider_test_reports_success_and_failure_shapes(self):
        good = MockProvider([T("say", word="ok")], name="local", local=True)
        bad = MockProvider(handler=lambda *a: __import__("octg_agent.reasoning.provider", fromlist=["x"]).ProviderError("auth", "bad key"), name="gemini")
        down = MockProvider([], name="freellmapi", up=False)
        async with Harness(providers={"local": good, "gemini": bad, "freellmapi": down}) as h:
            r = await h.rt.tasks.test_provider("local")
            self.assertTrue(r["ok"])
            self.assertEqual(r["tool_call"], {"word": "ok"})
            self.assertEqual(r["usage"], {"input_tokens": 120, "output_tokens": 25})
            r = await h.rt.tasks.test_provider("gemini")
            self.assertEqual((r["ok"], r["stage"], r["error_kind"]), (False, "call", "auth"))
            r = await h.rt.tasks.test_provider("freellmapi")
            self.assertEqual((r["ok"], r["stage"]), (False, "availability"))
            self.assertFalse((await h.rt.tasks.test_provider("nope"))["ok"])


class CliParserTests(unittest.TestCase):
    def parse(self, *argv):
        captured = {}
        orig = {k: getattr(cli, k) for k in ("cmd_task", "cmd_provider")}
        try:
            for k in orig:
                setattr(cli, k, lambda args, k=k: captured.setdefault("args", args) and 0)
            with self.assertRaises(SystemExit):
                cli.main(list(argv))
        finally:
            for k, v in orig.items():
                setattr(cli, k, v)
        return captured["args"]

    def test_task_commands_follow_the_established_cli_conventions(self):
        a = self.parse("task", "start", "open a site", "--provider", "local", "--wait", "--max-steps", "12")
        self.assertEqual((a.task_cmd, a.instruction, a.provider, a.wait, a.max_steps), ("start", "open a site", "local", True, 12))
        for c in ("status", "approve", "deny", "pause", "resume", "stop", "cancel", "show"):
            self.assertEqual(self.parse("task", c).task_cmd, c)
        self.assertEqual(self.parse("task", "approve", "task_abc").task_id, "task_abc")
        self.assertEqual(self.parse("provider", "test", "gemini").name, "gemini")
        for c in ("list", "detect", "usage"):
            self.assertEqual(self.parse("provider", c).prov_cmd, c)
        self.assertEqual(self.parse("provider", "set-key", "gemini").prov_cmd, "set-key")


if __name__ == "__main__":
    unittest.main()
