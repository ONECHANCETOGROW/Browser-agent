"""Model-call timing (instrumentation only) and the find_text efficiency fixes:
per-call provider/model/step/start/duration/outcome/retries/action in the task state, the activity feed and the history DB, with no prompts, replies or page text;
find_text(text) and find_text(text, index=0) count as the same repeated attempt; the guidance scopes find_text to content within a loaded page."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

from octg_agent.brain.history import TaskHistory
from octg_agent.brain.loop import _canonical_args
from octg_agent.brain.prompts import SYSTEM_PROMPT
from octg_agent.brain.tools import ToolCatalog
from octg_agent.reasoning.provider import ProviderError
from octg_agent.reasoning.providers import openai_compat as oc
from octg_agent.reasoning.providers.freellmapi import FreeLLMAPIProvider
from octg_agent.reasoning.providers.mock import MockProvider
from octg_agent.reasoning.router import ModelRouter, RouteContext, RouterConfig

from .fakebrowser import el
from .test_brain_loop import Env, T, finish
from .test_providers import FakeServer, MSGS, TOOLS, openai_reply

SECRET_PAGE_TEXT = "CONFIDENTIAL-PAGE-TEXT-7731"


class RouterTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_attempt_carries_provider_model_start_duration_and_outcome(self):
        async def slow_fail(m, t, o):
            await asyncio.sleep(0.06)
            raise ProviderError("unavailable", "down", provider="local")

        async def slow_ok(m, t, o):
            await asyncio.sleep(0.04)
            return MockProvider.call("finish_task", status="completed", summary="x")

        providers = {"local": MockProvider(name="local", model="qwen-test", local=True, handler=slow_fail), "gemini": MockProvider(name="gemini", model="gem-test", handler=slow_ok)}
        router = ModelRouter(providers, RouterConfig.from_dict({"policy": "FALLBACK", "order": ["local", "gemini"]}))
        before = time.time()
        routed = await router.complete([], [], RouteContext())
        a0, a1 = routed.attempts
        self.assertEqual((a0["provider"], a0["model"], a0["ok"], a0["error_kind"]), ("local", "qwen-test", False, "unavailable"))
        self.assertEqual((a1["provider"], a1["model"], a1["ok"]), ("gemini", "gem-test", True))
        self.assertGreaterEqual(a0["duration_ms"], 50)
        self.assertGreaterEqual(a1["duration_ms"], 30)
        self.assertLess(a1["duration_ms"], 2000)
        self.assertTrue(before - 1 <= a0["started_at"] <= a1["started_at"] <= time.time() + 1, "start times are real and ordered")
        self.assertEqual((a0["retry_reasons"], a1["retry_reasons"]), ([], []))

    async def test_skipped_providers_are_not_timed(self):
        providers = {"local": MockProvider(name="local", local=True, up=False), "gemini": MockProvider([MockProvider.call("finish_task", status="completed", summary="x")], name="gemini")}
        routed = await ModelRouter(providers, RouterConfig.from_dict({"policy": "FALLBACK", "order": ["local", "gemini"]})).complete([], [], RouteContext())
        skipped = next(a for a in routed.attempts if a["provider"] == "local")
        self.assertEqual(skipped["error_kind"], "skipped")
        self.assertNotIn("duration_ms", skipped)


class RetryReasonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def fake_sleep(_):
            return None
        p = patch.object(oc, "_sleep", fake_sleep)
        p.start()
        self.addCleanup(p.stop)

    async def test_an_adapter_retry_is_reported_by_reason_without_changing_the_result(self):
        replies = iter([(502, {"error": "bad gateway"}, None), openai_reply()])

        async def models(request):
            from aiohttp import web
            return web.json_response({"data": [{"id": "auto"}]})
        async with FakeServer(lambda b, h: next(replies), extra_routes=[("GET", "/v1/models", models)]) as s:
            p = FreeLLMAPIProvider(s.url + "/v1", "auto", lambda: "freellmapi-abc12345")
            routed = await ModelRouter({"freellmapi": p}, RouterConfig.from_dict({"policy": "MANUAL", "manual_provider": "freellmapi"})).complete(MSGS, TOOLS, RouteContext())
            await p.close()
        self.assertEqual(routed.response.tool_calls[0].name, "click")
        self.assertEqual(routed.attempts[0]["retry_reasons"], ["http_502"])
        self.assertTrue(routed.attempts[0]["ok"])
        self.assertEqual(len(s.requests), 2, "behaviour is unchanged: exactly one retry")

    async def test_a_failed_call_still_reports_its_retry(self):
        async with FakeServer(lambda b, h: (502, {"error": "bad gateway"}, None)) as s:
            p = FreeLLMAPIProvider(s.url + "/v1", "auto", lambda: "freellmapi-abc12345")
            with self.assertRaises(ProviderError) as cm:
                await p.complete(MSGS, TOOLS)
            await p.close()
        self.assertEqual(cm.exception.retry_reasons, ["http_502"])

    async def test_tool_negotiation_and_clean_calls(self):
        def responder(b, h):
            return (400, {"error": "tool_choice 'required' is not supported"}, None) if b.get("tool_choice") == "required" else openai_reply()
        async with FakeServer(responder) as s:
            from octg_agent.reasoning.providers.openai_compat import OpenAICompatProvider
            p = OpenAICompatProvider(s.url + "/v1", "m")
            r = await p.complete(MSGS, TOOLS)
            self.assertEqual(r.retry_reasons, ["tool_negotiation"])
            r2 = await p.complete(MSGS, TOOLS)  # now remembered as auto: nothing to negotiate... or at most the same single reason, never stale
            self.assertLessEqual(len(r2.retry_reasons), 1)
            await p.close()
        async with FakeServer(lambda b, h: openai_reply()) as s:
            p = OpenAICompatProvider(s.url + "/v1", "m")
            self.assertEqual((await p.complete(MSGS, TOOLS)).retry_reasons, [])
            await p.close()


def timing_pages():
    return {"https://t.test/": {"title": "Timing page", "elements": [el("link", "Home", href="https://t.test/")],
                                "text": f"Plain page. {SECRET_PAGE_TEXT} The warranty period is twelve months. Another warranty period mention. And a third warranty period."}}


class LoopTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_call_is_recorded_with_its_action_and_no_content(self):
        async def local_down(m, t, o):
            await asyncio.sleep(0.03)
            raise ProviderError("unavailable", f"down {SECRET_PAGE_TEXT}?", provider="local")  # an error message must not leak either (only its kind is kept)

        gem = MockProvider([MockProvider.call("read_page", scope="viewport", reason="look"), MockProvider.call("find_text", text="warranty period"), finish("done")], name="gemini", model="gem-test")
        providers = {"local": MockProvider(name="local", model="qwen-test", local=True, handler=local_down), "gemini": gem}
        async with Env(providers=providers, pages=timing_pages(), start_url="https://t.test/") as e:
            t = await e.run("What is the warranty period?")
            self.assertEqual(t.status.value, "COMPLETED", t.error)
            log = t.model_log
            self.assertEqual(len(log), t.model_calls, "one timing record per model call, failed ones included")
            self.assertEqual([(r.provider, r.ok) for r in log], [("local", False), ("gemini", True), ("gemini", True), ("gemini", True)])
            self.assertEqual(log[0].error_kind, "unavailable")
            self.assertEqual([r.action for r in log], [None, "read_page", "find_text", "finish_task"])
            self.assertEqual([r.n for r in log], [1, 2, 3, 4])
            self.assertEqual([r.fell_back for r in log], [False, True, False, False], "only the first answer after a failed attempt is marked as a fallback")
            self.assertEqual([r.model for r in log], ["qwen-test", "gem-test", "gem-test", "gem-test"])
            self.assertTrue(all(isinstance(r.step, int) and r.step >= 0 for r in log))
            self.assertEqual([r.step for r in log][1:], sorted(r.step for r in log)[1:], "steps never go backwards")
            self.assertGreaterEqual(log[0].duration_ms, 10)   # a 30 ms sleep; Windows timer granularity makes a tight bound flaky
            starts = [r.started_at for r in log]
            self.assertEqual(starts, sorted(starts))
            self.assertTrue(abs(starts[0] - time.time()) < 60)
            # activity feed: a `model` line per call (provider/model, seconds, outcome, action)
            lines = [a["text"] for a in t.activity if a["kind"] == "model" and a["text"].startswith("call ")]
            self.assertEqual(len(lines), 4)
            self.assertRegex(lines[0], r"^call 1: local/qwen-test \d+\.\ds FAILED \(unavailable\)$")
            self.assertRegex(lines[2], r"^call 3: gemini/gem-test \d+\.\ds ok → find_text$")
            self.assertEqual(lines[3].split("→")[-1].strip(), "finish_task")
            # no prompt, reply, page text or error text anywhere in what was recorded
            blob = json.dumps([r.to_dict() for r in log]) + "\n".join(lines) + json.dumps(t.to_dict()["model_call_timing"])
            self.assertNotIn(SECRET_PAGE_TEXT, blob)
            self.assertNotIn("warranty", blob.replace("find_text", ""))
            row = e.h.rt.history.get(t.task_id)
            self.assertEqual(len(row["model_call_timing"]), 4)
            self.assertNotIn(SECRET_PAGE_TEXT, json.dumps(row))
            first = row["model_call_timing"][0]
            self.assertEqual((first["provider"], first["ok"], first["error_kind"]), ("local", False, "unavailable"))
            self.assertEqual(row["model_call_timing"][2]["action"], "find_text")
            self.assertEqual(set(first), {"task_id", "n", "provider", "model", "step", "action", "started_at", "duration_ms", "ok", "error_kind", "retries", "retry_reasons", "fell_back"})

    async def test_unusable_replies_are_timed_too(self):
        script = [MockProvider.call("no_such_tool"), "I will just talk instead of calling a tool", finish("ok")]
        async with Env(MockProvider(script, model="m"), pages=timing_pages(), start_url="https://t.test/") as e:
            t = await e.run("x")
            self.assertEqual([r.action for r in t.model_log], ["invalid_call", "no_tool_call", "finish_task"])

    async def test_history_reports_latency_per_provider(self):
        import tempfile, shutil
        from pathlib import Path
        from octg_agent.brain.task import ModelCallRecord, TaskState
        tmp = Path(tempfile.mkdtemp(prefix="octg_timing_"))
        try:
            h = TaskHistory(tmp / "tasks.db")
            t = TaskState("timing report")
            for i, ms in enumerate([900, 1500, 12000]):
                t.record_model_call(ModelCallRecord(i + 1, "gemini", "g", 0, time.time(), ms, True, action="click"))
            t.record_model_call(ModelCallRecord(4, "gemini", "g", 1, time.time(), 20000, False, error_kind="timeout", retries=1, retry_reasons=["http_502"]))
            t.record_model_call(ModelCallRecord(5, "local", "q", 1, time.time(), 2000, True, action="type"))
            t.record_usage("gemini", "g", 10, 1, 0.0)
            h.save(t)
            rep = h.usage_report()["gemini"]["latency"]
            self.assertEqual((rep["timed_calls"], rep["max_ms"], rep["calls_over_5s"], rep["failed"], rep["retried"]), (4, 20000, 2, 1, 1))
            self.assertEqual(rep["median_ms"], 12000)
            self.assertEqual(h.usage_report()["local"]["latency"]["avg_ms"], 2000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RepetitionGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_are_not_a_difference(self):
        a = _canonical_args("find_text", {"text": "Nobel Prizes", "tab_id": 5})
        self.assertEqual(a, _canonical_args("find_text", {"text": "nobel   prizes", "index": 0, "tab_id": 5}))
        self.assertEqual(a, _canonical_args("find_text", {"index": 0, "case_sensitive": False, "whole_word": False, "scroll": True, "text": " NOBEL PRIZES ", "tab_id": 5}))
        self.assertNotEqual(a, _canonical_args("find_text", {"text": "Nobel Prizes", "index": 1, "tab_id": 5}), "a different occurrence is a different attempt")
        self.assertNotEqual(a, _canonical_args("find_text", {"text": "Nobel Prizes", "tab_id": 5, "case_sensitive": True}))
        self.assertNotEqual(a, _canonical_args("find_text", {"text": "Other", "tab_id": 5}))
        self.assertEqual(_canonical_args("click", {"element_id": "el_1", "snapshot_id": "s", "tab_id": 1}), {"element_id": "el_1", "tab_id": 1}, "other tools keep their exact arguments")

    async def _run(self, calls):
        seen: list[str] = []

        def handler_factory():
            it = iter(calls + [finish("stop")])

            def h(messages, tools, options):
                seen.append(messages[-1].content)
                return next(it)
            return h
        async with Env(MockProvider(handler=handler_factory()), pages=timing_pages(), start_url="https://t.test/", max_steps=15) as e:
            t = await e.run("find something")
            return seen, t

    async def test_the_same_ineffective_search_with_or_without_index_zero_triggers_the_hint_on_the_third_try(self):
        calls = [T("find_text", text="nothing like this"), T("find_text", text="nothing like this", index=0), T("find_text", text="Nothing  Like This", index=0)]
        seen, t = await self._run(calls)
        self.assertEqual(len(seen), 4)
        self.assertNotIn("repeated find_text", seen[2], "two tries are not yet a loop")
        self.assertIn("You have now repeated find_text 3 times", seen[3])

    async def test_a_changing_index_or_text_does_not_trigger_the_guard(self):
        calls = [T("find_text", text="warranty period"), T("find_text", text="warranty period", index=1), T("find_text", text="warranty period", index=2)]
        seen, _ = await self._run(calls)
        self.assertFalse(any("repeated find_text" in s for s in seen))
        calls = [T("find_text", text="nothing one"), T("find_text", text="nothing two"), T("find_text", text="nothing three")]
        seen, _ = await self._run(calls)
        self.assertFalse(any("repeated find_text" in s for s in seen), "different searches are different attempts even when they find nothing")


class GuidanceTests(unittest.TestCase):
    def test_prompt_scopes_find_text_to_content_within_a_page_and_says_how_to_submit_a_search(self):
        self.assertIn("locate specific content within a page", SYSTEM_PROMPT)
        self.assertIn("Do not use find_text to run a search", SYSTEM_PROMPT)
        self.assertIn("press_key Enter", SYSTEM_PROMPT)
        self.assertIn("submit", SYSTEM_PROMPT)
        self.assertIn("pass the right one as index", SYSTEM_PROMPT, "the earlier multi-match guidance is kept")
        self.assertLess(len(SYSTEM_PROMPT) // 4, 750, "the system prompt stays small")

    def test_the_tool_description_the_model_sees_says_the_same(self):
        d = next(t for t in ToolCatalog().llm_tools() if t["name"] == "find_text")["description"]
        self.assertIn("within the loaded page", d)
        self.assertIn("Not for submitting a search", d)


if __name__ == "__main__":
    unittest.main()
