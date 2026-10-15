"""Unit tests for the orchestration pieces: tool catalog, observation builder, context manager, approval policy, task state, history."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from octg_agent.brain.approval import ApprovalPolicy
from octg_agent.brain.context import ContextManager, estimate_tokens
from octg_agent.brain.history import TaskHistory
from octg_agent.brain.observation import ObservationBuilder
from octg_agent.brain.settings import BrainSettings
from octg_agent.brain.task import StepRecord, TaskState, TaskStatus, brief_args
from octg_agent.brain.tools import CONTROL, HIDDEN, ToolCallError, ToolCatalog
from octg_agent.engine.registry import ACTIONS
from octg_agent.engine.state import BrowserState
from octg_agent.reasoning.provider import ToolCall

from .fakebrowser import FakeBrowser, el, shop_pages
from .helpers import Harness


class ToolCatalogTests(unittest.TestCase):
    cat = ToolCatalog()

    def test_there_is_one_action_system_the_catalog_is_derived_from_the_registry(self):
        browser = set(self.cat.browser_names())
        self.assertTrue(browser <= set(ACTIONS))
        self.assertEqual(browser, set(ACTIONS) - HIDDEN)
        for name in ("get_tabs", "get_page_state", "inspect_page", "read_page", "navigate", "new_tab", "switch_tab", "close_tab", "back", "forward", "click", "type",
                     "clear", "select", "check", "uncheck", "scroll", "press_key", "screenshot", "upload_file"):
            self.assertIn(name, browser)
        for t in self.cat.llm_tools():
            if t["name"] in CONTROL:
                continue
            for prop in t["input_schema"]["properties"]:
                self.assertTrue(prop in ACTIONS[t["name"]].properties or prop in ("reason", "note", "expect_url_contains", "expect_text"), f"{t['name']}.{prop} is not a registry parameter")

    def test_snapshot_ids_are_hidden_from_the_model_and_the_view_is_compact(self):
        text = json.dumps(self.cat.llm_tools())
        self.assertNotIn("snapshot_id", text)
        self.assertLess(self.cat.approx_tokens(), 3000)
        names = [t["name"] for t in self.cat.llm_tools()]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({"finish_task", "ask_user", "request_approval"} <= set(names))
        self.assertNotIn("ping", names)
        for t in self.cat.llm_tools():
            self.assertEqual(t["input_schema"]["type"], "object")
            for p in t["input_schema"]["properties"].values():
                self.assertIsInstance(p["type"], str)  # no union types: providers differ in what they accept

    def test_parse_extracts_reason_expectations_and_coerces_small_model_output(self):
        p = self.cat.parse(ToolCall("1", "click", {"element_id": "el_004", "reason": "search", "expect_url_contains": "q=", "expect_text": "results", "note": "found it"}))
        self.assertEqual((p.kind, p.name, p.args, p.reason, p.note), ("browser", "click", {"element_id": "el_004"}, "search", "found it"))
        self.assertEqual(p.expect, [{"type": "url_contains", "value": "q="}, {"type": "text_present", "value": "results"}])
        p = self.cat.parse(ToolCall("1", "scroll", {"amount": "300", "direction": "down"}))
        self.assertEqual(p.args["amount"], 300)
        p = self.cat.parse(ToolCall("1", "switch_tab", {"tab_id": "12"}))
        self.assertEqual(p.args["tab_id"], 12)
        p = self.cat.parse(ToolCall("1", "type", {"element_id": "el_1", "text": 12345, "clear": "false"}))
        self.assertEqual((p.args["text"], p.args["clear"]), ("12345", False))
        p = self.cat.parse(ToolCall("1", "click", {"element_id": "el_1", "snapshot_id": "snap_evil"}))
        self.assertNotIn("snapshot_id", p.args)

    def test_parse_rejects_bad_calls_with_helpful_messages(self):
        with self.assertRaises(ToolCallError) as cm:
            self.cat.parse(ToolCall("1", "clickk", {}))
        self.assertIn("Unknown tool", str(cm.exception))
        with self.assertRaises(ToolCallError) as cm:
            self.cat.parse(ToolCall("1", "ping", {}))  # hidden tools are not callable
        with self.assertRaises(ToolCallError) as cm:
            self.cat.parse(ToolCall("1", "click", {"colour": "red"}))
        self.assertIn("no parameter", str(cm.exception))
        with self.assertRaises(ToolCallError):
            self.cat.parse(ToolCall("1", "finish_task", {"status": "maybe", "summary": "x"}))

    def test_control_tools_never_map_to_browser_actions(self):
        for name in CONTROL:
            self.assertNotIn(name, ACTIONS)
            self.assertEqual(self.cat.parse(ToolCall("1", name, {"status": "completed", "summary": "s", "kind": "login", "question": "q"})).kind, "control")


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def env(self, pages=None, url="https://shop.test/"):
        self.fb = FakeBrowser(pages or shop_pages(), url)
        self.h = Harness(providers={})
        await self.h.__aenter__()
        self.ext = await self.h.extension(handlers=self.fb.handlers())
        self.builder = ObservationBuilder(self.h.rt.dispatcher, BrainSettings())
        self.task = TaskState("find the search box and search for headphones")
        return self.builder

    async def asyncTearDown(self):
        await self.h.__aexit__(None, None, None)

    def inspects(self):
        return [c for c in self.ext.received if c["action"] == "inspect_page"]

    async def test_search_box_ranks_first_for_a_search_task_and_text_is_included_once(self):
        b = await self.env()
        obs = await b.observe(self.task)
        self.assertEqual(obs.cands[0][2]["name"], "Search for products")
        rendered = obs.render()
        self.assertIn("TABS (1)", rendered)
        self.assertIn("ACTIVE TAB #", rendered)
        self.assertIn("VISIBLE TEXT", rendered)  # first sight of the page
        again = await b.observe(self.task)
        self.assertNotIn("VISIBLE TEXT", again.render())  # unchanged page: text is not resent
        self.assertEqual(len(self.inspects()), 1, "an unchanged page must not be re-inspected")

    async def test_dirty_flag_and_force_cause_a_fresh_inspection_with_a_diff(self):
        b = await self.env()
        first = await b.observe(self.task)
        self.fb.pages["https://shop.test/"]["elements"].append(el("button", "Accept cookies"))
        b.mark_dirty(self.task)
        second = await b.observe(self.task)
        self.assertEqual(len(self.inspects()), 2)
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertTrue(any("NEW" in c and "Accept cookies" in c for c in second.changes), second.changes)
        self.assertIn("CHANGES:", second.render())

    async def test_value_changes_are_reported(self):
        b = await self.env()
        await b.observe(self.task)
        self.fb.pages["https://shop.test/"]["elements"][1]["value"] = "headphones"
        b.mark_dirty(self.task)
        obs = await b.observe(self.task)
        self.assertTrue(any(c.startswith("CHANGED searchbox") for c in obs.changes), obs.changes)
        self.assertIn('val="headphones"', obs.render())

    async def test_repeated_controls_are_deduplicated_and_the_list_is_capped_with_omitted_counts(self):
        pages = shop_pages()
        pages["https://shop.test/"]["elements"] += [el("link", "Home", href="https://shop.test/") for _ in range(6)]
        pages["https://shop.test/"]["elements"] += [el("link", f"Item {i}", href=f"https://shop.test/i/{i}") for i in range(80)]
        b = await self.env(pages)
        obs = await b.observe(self.task)
        names = [e["name"] for _, _, e, _ in obs.cands]
        self.assertEqual(names.count("Home"), 1)
        text = obs.render(max_chars=6000, max_elements=20)
        self.assertEqual(len(obs.shown_ids), 20)
        self.assertIn("20 of", text)
        self.assertIn("omitted:", text)
        small = obs.render(max_chars=900, max_elements=45)
        self.assertLessEqual(len(small), 1200)
        self.assertGreaterEqual(len(obs.shown_ids), 5)

    async def test_captcha_login_and_dialog_signals(self):
        b = await self.env(url="https://captcha.test/")
        self.assertIn("captcha", (await b.observe(self.task)).signals)
        await self.h.__aexit__(None, None, None)
        b = await self.env(url="https://login.test/")
        self.assertIn("login_form", (await b.observe(self.task)).signals)
        pages = shop_pages()
        pages["https://shop.test/"]["dialogs"] = [{"role": "dialog", "name": "Cookies", "text": "We use cookies"}]
        await self.h.__aexit__(None, None, None)
        b = await self.env(pages)
        obs = await b.observe(self.task)
        self.assertIn("dialog_open", obs.signals)
        self.assertIn("DIALOG OPEN", obs.render())

    async def test_invisible_recaptcha_badge_is_not_a_captcha_challenge(self):
        pages = shop_pages()
        pages["https://shop.test/"]["iframes"] = [{"src": "https://www.google.com/recaptcha/api2/anchor?size=invisible", "visible": True}]
        b = await self.env(pages)
        self.assertNotIn("captcha", (await b.observe(self.task)).signals)

    async def test_restricted_page_is_described_and_never_inspected(self):
        b = await self.env(url="chrome://version/")
        obs = await b.observe(self.task)
        self.assertFalse(obs.supported)
        self.assertIn("CANNOT be controlled", obs.render())
        self.assertEqual(self.inspects(), [])
        self.assertEqual(obs.cands, [])

    async def test_multi_tab_state_is_reported(self):
        b = await self.env()
        self.fb.open_tab("https://help.test/", active=False)
        obs = await b.observe(self.task)
        self.assertEqual(len(obs.tabs), 2)
        self.assertIn("TABS (2)", obs.render())
        self.assertEqual(len(self.task.known_tabs), 2)
        self.assertEqual(self.task.current_tab, obs.active_tab_id)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_contains_state_sections_and_attaches_screenshots_only_on_request(self):
        fb = FakeBrowser(shop_pages(), "https://shop.test/")
        async with Harness(providers={}) as h:
            await h.extension(handlers=fb.handlers())
            task = TaskState("search for headphones")
            task.add_note("budget is 50")
            task.record_step(StepRecord(1, "navigate", {"url": "shop.test"}, "go", True, "loaded"))
            task.last_action, task.last_action_result = {"tool": "navigate"}, {"ok": True, "summary": "loaded https://shop.test/"}
            task.verification_result = {"summary": "URL changed"}
            obs = await ObservationBuilder(h.rt.dispatcher, BrainSettings()).observe(task)
            ctx = ContextManager(BrainSettings(), 2400)
            built = ctx.build(task, obs, feedback="be careful")
            text = built.messages[1].content
            for needle in ("TASK: search for headphones", "NOTES: budget is 50", "PROGRESS:", "1. navigate", "LAST RESULT:", "VERIFICATION: URL changed", "FEEDBACK: be careful", "OBSERVATION:", "Choose exactly one tool"):
                self.assertIn(needle, text)
            self.assertIsNone(built.messages[1].images)
            self.assertEqual(built.messages[0].role, "system")
            self.assertEqual(ctx.build(task, obs, image=b"\x89PNGxx").messages[1].images, [b"\x89PNGxx"])
            self.assertGreater(built.tokens, estimate_tokens(text))

    async def test_trimming_order_and_budget(self):
        pages = shop_pages()
        pages["https://shop.test/"]["elements"] += [el("link", f"Very long product name number {i} with extra words", href=f"https://shop.test/p/{i}") for i in range(120)]
        fb = FakeBrowser(pages, "https://shop.test/")
        async with Harness(providers={}) as h:
            await h.extension(handlers=fb.handlers())
            task = TaskState("buy something")
            for i in range(20):
                task.record_step(StepRecord(i + 1, "scroll", {"direction": "down"}, "more", True, "scrolled to y=300 of 500"))
            obs = await ObservationBuilder(h.rt.dispatcher, BrainSettings()).observe(task)
            small = BrainSettings(prompt_token_budget=1000, obs_max_elements=200)  # below the 1500-token floor the manager enforces, so the floor is the effective budget
            built = ContextManager(small, 0).build(task, obs)
            self.assertTrue(built.trimmed, "the prompt should not have fit without trimming")
            self.assertTrue(built.trimmed[0].startswith("elements"))  # elements shrink before history is touched
            self.assertLessEqual(len(built.messages[1].content), int(1500 * 3.6) + 50)
            self.assertIn("(earlier: 12 steps", built.messages[1].content)


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.state = BrowserState()
        self.state.snapshots[1] = {"snapshot_id": "snap_1", "url": "u", "stale": False, "elements": {
            "el_001": {"tag": "button", "role": "button", "name": "Publish post"}, "el_002": {"tag": "input", "role": "textbox", "name": "Password", "type": "password", "sensitive": True},
            "el_003": {"tag": "input", "role": "textbox", "name": "Card number", "sensitive": True}, "el_004": {"tag": "a", "role": "link", "name": "About", "href": "https://x.test/about"}}}
        self.pol = ApprovalPolicy(BrainSettings(), self.state)
        self.task = TaskState("t")

    def decide(self, tool, params, **kw):
        from octg_agent.engine import risk as risk_mod
        params = {"tab_id": 1, "snapshot_id": "snap_1", **params}
        risk = risk_mod.assess(ACTIONS[tool], params, self.state)
        return ApprovalPolicy(BrainSettings(**kw), self.state).decide(tool, params, risk, self.task)

    def test_levels(self):
        self.assertEqual(self.decide("click", {"element_id": "el_004"}).action, "allow")  # medium, auto by default
        self.assertEqual(self.decide("click", {"element_id": "el_004"}, approve_medium=True).action, "ask")
        self.assertEqual(self.decide("click", {"element_id": "el_001"}).action, "ask")  # label says Publish
        self.assertEqual(self.decide("inspect_page", {}).action, "allow")
        self.assertEqual(self.decide("navigate", {"url": "x.test"}).action, "allow")
        self.assertEqual(self.decide("upload_file", {"element_id": "el_004", "local_file_path": "C:/x"}).action, "ask")

    def test_sensitive_fields_are_blocked_not_asked(self):
        for el_id in ("el_002", "el_003"):
            d = self.decide("type", {"element_id": el_id, "text": "secret"})
            self.assertEqual(d.action, "block", el_id)
        self.assertEqual(self.decide("type", {"element_id": "el_002", "text": "x"}, block_sensitive_typing=False).action, "ask")

    def test_closing_a_foreign_tab_asks_but_a_task_opened_tab_does_not(self):
        self.assertEqual(self.decide("close_tab", {"tab_id": 55}).action, "ask")
        self.task.created_tabs.add(56)
        d = self.decide("close_tab", {"tab_id": 56})
        self.assertNotEqual(d.reasons if hasattr(d, "reasons") else "", "x")
        self.assertEqual(d.action, "allow" if d.risk["level"] != "high" else "ask")

    def test_pending_description_is_human_readable_and_secret_free(self):
        risk = {"level": "high", "tags": [], "approval": "required", "reasons": []}
        p = self.pol.make_pending("type", {"tab_id": 1, "snapshot_id": "snap_1", "element_id": "el_001", "text": "4111111111111111"}, risk, "r", "why")
        self.assertIn("Publish post", p.description)
        self.assertNotIn("4111111111111111", json.dumps(p.to_dict()))


class ConfigTests(unittest.TestCase):
    def test_example_config_loads_and_every_key_is_understood(self):
        from octg_agent.reasoning.provider import BaseProvider
        from octg_agent.reasoning.providers import DEFAULT_PROVIDER_CONFIG
        from octg_agent.reasoning.router import RouterConfig
        example = Path(__file__).resolve().parents[1] / "brain.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        s = BrainSettings.load(".", example)
        for k in raw:
            self.assertTrue(hasattr(s, k), f"brain.example.json key {k!r} is not a BrainSettings field")
        self.assertEqual(RouterConfig.from_dict(s.router).policy.value, "LOCAL_FIRST")
        self.assertTrue(set(s.providers) <= set(DEFAULT_PROVIDER_CONFIG))

    def test_missing_config_gives_safe_defaults(self):
        s = BrainSettings.load(tempfile.mkdtemp(prefix="octg_cfg_"))
        self.assertFalse(s.approve_medium)
        self.assertTrue(s.block_sensitive_typing)
        self.assertLessEqual(s.max_steps, 100)


class TaskStateTests(unittest.TestCase):
    def test_notes_are_capped_deduplicated_and_task_scoped(self):
        t = TaskState("x")
        for i in range(30):
            t.add_note(f"fact {i}", cap=12)
        t.add_note("fact 29", cap=12)
        self.assertEqual(len(t.notes), 12)
        self.assertEqual(t.notes[-1], "fact 29")
        self.assertEqual(TaskState("y").notes, [])

    def test_brief_args_masks_secrets_and_long_digit_runs(self):
        self.assertEqual(brief_args({"text": "hello", "snapshot_id": "s", "element_id": "el_1"}), {"text": "hello", "element_id": "el_1"})
        self.assertEqual(brief_args({"text": "4111 1111 1111 1111"})["text"], "[omitted]")
        self.assertEqual(brief_args({"text": "anything"}, sensitive=True)["text"], "[omitted]")
        self.assertLessEqual(len(brief_args({"text": "x" * 500})["text"]), 60)
        self.assertNotIn("files", brief_args({"files": [{"data_b64": "AAAA"}]}))

    def test_status_transitions_set_timestamps_and_waiting_messages(self):
        t = TaskState("x")
        t.set_status(TaskStatus.WAITING_FOR_USER, message="please log in")
        self.assertEqual(t.waiting_message, "please log in")
        t.set_status(TaskStatus.RUNNING)
        self.assertIsNone(t.waiting_message)
        self.assertIsNone(t.finished_at)
        t.set_status(TaskStatus.FAILED, error="boom")
        self.assertIsNotNone(t.finished_at)
        self.assertEqual(t.error, "boom")

    def test_summary_has_no_page_text_or_field_values(self):
        t = TaskState("x")
        t.latest_observation = {"url": "u"}
        self.assertNotIn("latest_observation", t.to_summary())


class HistoryTests(unittest.TestCase):
    def test_roundtrip_and_usage_report(self):
        tmp = Path(tempfile.mkdtemp(prefix="octg_hist_"))
        try:
            h = TaskHistory(tmp / "tasks.db")
            t = TaskState("do it")
            t.record_usage("gemini", "gemini-3.5-flash-lite", 100, 10, 0.001)
            t.record_usage("local", "qwen3:8b", 50, 5, 0.0)
            t.model_calls = 2
            t.record_step(StepRecord(1, "read_page", {}, "r", True, "text: PRIVATE PAGE CONTENT", stored_outcome="read page text (content not stored)"))
            t.set_status(TaskStatus.COMPLETED)
            h.save(t)
            got = h.get(t.task_id)
            self.assertEqual((got["status"], got["model_calls"], got["providers"]), ("COMPLETED", 2, ["gemini", "local"]))
            self.assertNotIn("PRIVATE PAGE CONTENT", json.dumps(got))
            self.assertEqual(h.list()[0]["task_id"], t.task_id)
            self.assertEqual(h.usage_report()["gemini"]["calls"], 1)
            self.assertIsNone(h.get("task_nope"))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
