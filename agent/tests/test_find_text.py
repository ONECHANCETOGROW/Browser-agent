"""find_text: the generic find-in-page action. Registry/schema, dispatcher validation, log hygiene, the LLM tool view, the loop using it on a long
page instead of blind scrolling, and static guards on the extension code (the real-browser behaviour is in tests/e2e/run_e2e.py case 26)."""
import json
import re
import unittest
from pathlib import Path

from octg_agent.brain.tools import ToolCatalog
from octg_agent.engine.registry import ACTIONS, tool_schema
from octg_agent.reasoning.provider import ToolCall
from octg_agent.reasoning.providers.mock import MockProvider

from .fakebrowser import FakeBrowser, el
from .helpers import Harness, tab, wait_until
from .test_brain_loop import Env, T, finish

EXT = Path(__file__).resolve().parents[2] / "extension"
PAGE_AGENT = (EXT / "content" / "page_agent.js").read_text(encoding="utf-8")
ACTIONS_JS = (EXT / "background" / "actions.js").read_text(encoding="utf-8")

_M = {"match_index": 0, "text": "warranty period", "context": "…SECRET-CONTEXT [[warranty period]] is twelve months…", "nearest_heading": "SECRET-HEADING", "element_tag": "p", "role": None,
      "is_heading": False, "is_link": False, "match_type": "body_text", "visibility": "in_view", "in_viewport": True, "page_y": 9000}
FOUND = {
    "query": "warranty period", "found": True, "total_matches": 3, "hidden_matches": 1, "case_sensitive": False, "whole_word": False,
    "matches": [dict(_M)], "focused": {**_M, "scrolled": True},
    "scroll": {"before": {"x": 0, "y": 0}, "after": {"x": 0, "y": 8700}, "max_y": 20000}, "url": "https://x.test/", "title": "T", "frames_searched": 1,
}


class RegistryTests(unittest.TestCase):
    def test_find_text_is_a_registry_action_with_a_strict_schema(self):
        spec = ACTIONS["find_text"]
        self.assertEqual(spec.required, ("text",))
        self.assertEqual(spec.target, "extension")
        self.assertFalse(spec.uses_elements, "find_text must not take element ids; it returns them")
        self.assertEqual(spec.risk.level, "none")
        self.assertIn("read_only", spec.risk.tags)
        self.assertTrue(spec.mutating, "it moves the viewport, so the engine must capture before/after evidence")
        schema = next(t for t in tool_schema()["tools"] if t["name"] == "find_text")["input_schema"]
        self.assertIs(schema["additionalProperties"], False)
        for p in ("text", "tab_id", "case_sensitive", "whole_word", "index", "max_matches", "context_chars", "scroll"):
            self.assertIn(p, schema["properties"])
        self.assertIn("never searched", spec.description)  # form-field contents are out of scope, and the description says so

    def test_it_is_not_site_specific(self):
        text = json.dumps(next(t for t in tool_schema()["tools"] if t["name"] == "find_text"))
        self.assertNotRegex(text, r"(?i)wikipedia|google|flipkart|amazon|facebook")


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_arguments_never_reach_the_extension(self):
        async with Harness() as h:
            ext = await h.extension()
            for params in ({}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x", "index": -1}, {"text": "x", "max_matches": 0},
                           {"text": "x", "max_matches": 51}, {"text": "x", "context_chars": 301}, {"text": "x", "scroll": "yes"}, {"text": "x", "selector": "#a"}):
                r = await h.call("find_text", params)
                self.assertEqual((r["success"], r["error"]["code"]), (False, "INVALID_PARAMS"), params)
            self.assertEqual(ext.received, [])

    async def test_valid_call_is_forwarded_unchanged_and_the_result_returned(self):
        async with Harness() as h:
            ext = await h.extension(handlers={"find_text": lambda p, c: {"result": FOUND}})
            r = await h.call("find_text", {"text": "warranty period", "index": 1, "case_sensitive": True, "tab_id": 7})
            self.assertTrue(r["success"])
            self.assertEqual(ext.received[0]["parameters"], {"text": "warranty period", "index": 1, "case_sensitive": True, "tab_id": 7})
            self.assertEqual(r["result"]["total_matches"], 3)
            self.assertEqual(r["risk"]["level"], "none")

    async def test_restricted_page_failure_is_passed_through_not_masked(self):
        async with Harness() as h:
            await h.extension(handlers={"find_text": lambda p, c: {"success": False, "result": None, "error": {"code": "UNSUPPORTED_PAGE", "message": "restricted"}}})
            r = await h.call("find_text", {"text": "anything"})
            self.assertEqual((r["success"], r["error"]["code"]), (False, "UNSUPPORTED_PAGE"))

    async def test_page_text_never_reaches_the_log_only_counts_and_positions(self):
        async with Harness() as h:
            await h.extension(handlers={"find_text": lambda p, c: {"result": FOUND}})
            await h.call("find_text", {"text": "warranty period"})
            await h.call("find_text", {"text": "q" * 400})
            entries = [e for e in h.rt.actionlog.tail(10) if e["action"] == "find_text"]
            blob = json.dumps(entries)
            self.assertNotIn("SECRET-CONTEXT", blob)
            self.assertNotIn("SECRET-HEADING", blob)
            self.assertNotIn("q" * 200, blob)  # a pasted blob is truncated
            first = entries[0]["result"]
            self.assertEqual((first["found"], first["total_matches"], first["hidden_matches"]), (True, 3, 1))
            self.assertEqual(first["focused"], {"match_index": 0, "in_viewport": True, "page_y": 9000, "frame": None})
            self.assertNotIn("match_type", json.dumps(entries), "semantic metadata is for the model, not for the operational log")


class ToolViewTests(unittest.TestCase):
    cat = ToolCatalog()

    def test_the_model_sees_a_small_find_text_tool(self):
        t = next(x for x in self.cat.llm_tools() if x["name"] == "find_text")
        self.assertEqual(set(t["input_schema"]["properties"]), {"text", "index", "case_sensitive", "reason"})
        self.assertEqual(t["input_schema"]["required"], ["text"])
        self.assertIn("long page", t["description"])
        self.assertLess(self.cat.approx_tokens(), 3000)

    def test_arguments_are_coerced_and_unknown_ones_rejected(self):
        p = self.cat.parse(ToolCall("1", "find_text", {"text": "Nobel", "index": "2", "case_sensitive": "false", "reason": "jump"}))
        self.assertEqual((p.kind, p.name, p.args), ("browser", "find_text", {"text": "Nobel", "index": 2, "case_sensitive": False}))
        from octg_agent.brain.tools import ToolCallError
        with self.assertRaises(ToolCallError):
            self.cat.parse(ToolCall("1", "find_text", {"text": "x", "selector": "#a"}))


def long_page():
    filler = "Lorem ipsum filler sentence without the target. " * 400
    return {"https://long.test/": {
        "title": "Long article", "elements": [el("link", "Home", href="https://long.test/")],
        "text": filler + "SECTION: The warranty period is twelve months from purchase. " + filler,
        "hidden_text": "warranty period inside a collapsed block"}}


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_agent_jumps_to_the_text_instead_of_scrolling_blindly(self):
        script = [T("navigate", url="https://long.test/"), T("find_text", text="Warranty Period", reason="jump to the section"),
                  T("read_page", scope="viewport", reason="read around it"), finish("Located and read the section")]
        async with Env(MockProvider(script), pages=long_page(), start_url="https://long.test/") as e:
            t = await e.run("What is the warranty period?")
            self.assertEqual(t.status.value, "COMPLETED", t.error)
            self.assertEqual([a for a, _ in e.actions()], ["navigate", "find_text"], "no blind scroll steps were needed")
            self.assertEqual(e.actions("scroll"), [])
            _, params = e.actions("find_text")[0]
            self.assertEqual(params["text"], "Warranty Period")
            self.assertEqual(params["tab_id"], e.fb.active, "the loop pins the tab it showed the model")
            self.assertNotIn("snapshot_id", params)
            self.assertGreater(e.fb.tabs[e.fb.active]["scroll_y"], 1000, "the page really moved to the match")
            step = next(s for s in t.recent_steps if s.tool == "find_text")
            self.assertTrue(step.ok)
            self.assertIn("1 match", step.outcome)
            self.assertIn("[[warranty period]]", step.outcome)  # the model gets the surrounding text
            self.assertEqual(step.stored_outcome, "searched the page: 1 match(es) (content not stored)")
            row = e.h.rt.history.get(t.task_id)
            self.assertNotIn("twelve months", json.dumps(row), "page text is not persisted in history")

    async def test_not_found_is_an_answer_not_an_error_and_mentions_hidden_content(self):
        script = [T("find_text", text="warranty period", reason="look"), T("find_text", text="completely absent phrase"), finish("not on the page", "failed")]
        pages = long_page()
        pages["https://long.test/"]["text"] = "Just a short page."
        async with Env(MockProvider(script), pages=pages, start_url="https://long.test/") as e:
            t = await e.run("find it")
            steps = [s for s in t.recent_steps if s.tool == "find_text"]
            self.assertEqual([s.ok for s in steps], [True, True])
            self.assertIn("no visible match", steps[0].outcome)
            self.assertIn("hidden/collapsed", steps[0].outcome)  # 1 match exists only in hidden content
            self.assertNotIn("hidden", steps[1].outcome)
            self.assertEqual(e.actions("scroll"), [])

    async def test_the_model_sees_what_each_match_is_and_picks_the_occurrence_itself(self):
        """TOC link, section heading and a paragraph all match. Nothing is ranked: match 0 stays the link, and the model chooses with index=."""
        pages = {"https://long.test/": {
            "title": "Handbook", "elements": [el("link", "Home", href="https://long.test/")],
            "text": "Contents: Maintenance window. " + "x " * 600 + "Maintenance window (section heading). " + "y " * 600 + "The maintenance window is Thursday.",
            "match_meta": [
                {"element_tag": "a", "role": "link", "is_link": True, "match_type": "link", "nearest_heading": "Contents"},
                {"element_tag": "h2", "role": "heading", "is_heading": True, "heading_level": 2, "match_type": "heading", "nearest_heading": "Maintenance window"},
                {"element_tag": "p", "match_type": "body_text", "nearest_heading": "Maintenance window"}]}}
        script = [T("find_text", text="maintenance window", reason="look"), T("find_text", text="maintenance window", index=1, reason="the heading is what I want"),
                  finish("moved to the section heading")]
        async with Env(MockProvider(script), pages=pages, start_url="https://long.test/") as e:
            t = await e.run("Go to the maintenance window section")
            self.assertEqual(t.status.value, "COMPLETED", t.error)
            calls = [p for _, p in e.actions("find_text")]
            self.assertEqual([p.get("index") for p in calls], [None, 1], "the model's index is passed through untouched")
            first, second = [s for s in t.recent_steps if s.tool == "find_text"]
            self.assertIn("scrolled to #0 [link]", first.outcome, "the default is the first match in document order, never a 'best' one")
            self.assertIn("all: #0 link under 'Contents'; #1 heading h2 under 'Maintenance window'; #2 body_text", first.outcome)
            self.assertIn("scrolled to #1 [heading h2]", second.outcome)
            fb_scroll = e.fb.tabs[e.fb.active]["scroll_y"]
            self.assertEqual(fb_scroll, pages["https://long.test/"]["text"].lower().index("maintenance window", 40), "index=1 moved to the second occurrence")

    async def test_find_text_on_a_restricted_page_is_handled_like_any_page_action(self):
        script = [T("find_text", text="anything"), T("navigate", url="https://long.test/"), T("find_text", text="Lorem"), finish("done")]
        async with Env(MockProvider(script), pages=long_page(), start_url="chrome://newtab/") as e:
            t = await e.run("search the page")
            self.assertEqual(t.status.value, "COMPLETED", t.error)
            self.assertEqual(e.actions("find_text")[0][0], "find_text")
            # the first call was refused before reaching the browser (restricted page), the second ran on the real page
            self.assertEqual(len(e.actions("find_text")), 1)


class GuidanceTests(unittest.TestCase):
    def test_prompt_and_schema_tell_the_model_to_choose_among_matches(self):
        from octg_agent.brain.prompts import SYSTEM_PROMPT
        self.assertIn("find_text", SYSTEM_PROMPT)
        self.assertIn("pass the right one as index", SYSTEM_PROMPT)
        desc = ACTIONS["find_text"].description
        for word in ("match_type", "is_heading", "heading_level", "is_link", "element_tag", "nearest_heading", "visibility", "nothing is ranked or skipped", "match_index"):
            self.assertIn(word, desc)
        self.assertLess(len(SYSTEM_PROMPT) // 4, 700, "the system prompt stays small")


class ExtensionGuardTests(unittest.TestCase):
    """Static guards on the code that runs in the browser; the behaviour itself is exercised by the e2e suite."""

    def test_matches_are_described_not_ranked(self):
        body = PAGE_AGENT[PAGE_AGENT.index("OCTG.findText"):PAGE_AGENT.index("// ---------------------------------------------------------------- evidence")]
        for field in ("element_tag", "is_heading", "heading_level", "is_link", "nearest_heading", "visibility", "match_type"):
            self.assertIn(field, body)
        self.assertNotIn(".sort(", body, "matches stay in document order")
        self.assertNotIn("matches.reverse", body)
        self.assertNotRegex(body, r"(?i)wikipedia|mw-|toc\b|nobel", "no website-specific heuristics")
        self.assertIn("match_index", ACTIONS_JS[ACTIONS_JS.index("H.find_text"):ACTIONS_JS.index("H.navigate")])

    def test_the_extension_implements_and_registers_the_action(self):
        self.assertIsNotNone(re.search(r"^H\.find_text\s*=", ACTIONS_JS, re.M))
        self.assertRegex(ACTIONS_JS, r"find_text:\s*\{\s*tab:\s*true,\s*page:\s*true,\s*mutating:\s*true\s*\}")
        self.assertIn("OCTG.findText", PAGE_AGENT)

    def test_form_control_contents_are_never_searched(self):
        skip = re.search(r"const FIND_SKIP = new Set\(\[([^\]]*)\]\)", PAGE_AGENT)
        self.assertIsNotNone(skip)
        for tag in ("INPUT", "TEXTAREA", "SELECT", "SCRIPT", "STYLE"):
            self.assertIn(f"'{tag}'", skip.group(1))
        body = PAGE_AGENT[PAGE_AGENT.index("OCTG.findText"):PAGE_AGENT.index("// ---------------------------------------------------------------- evidence")]
        for forbidden in (".value", "innerHTML", "captureVisibleTab", "screenshot"):
            self.assertNotIn(forbidden, body.replace("nodeValue", ""), f"findText must not use {forbidden}")

    def test_element_ids_are_only_returned_for_the_current_snapshot(self):
        h = ACTIONS_JS[ACTIONS_JS.index("H.find_text"):ACTIONS_JS.index("H.navigate")]
        self.assertIn("currentSnapshot(ctx.tab.id)", h)
        self.assertIn("r.snapshot_id === snap.snapshot_id", h)
        self.assertNotIn("storeSnapshot", h, "find_text must not create or replace snapshots")
        self.assertNotIn("newSnapshotId", h)


if __name__ == "__main__":
    unittest.main()
