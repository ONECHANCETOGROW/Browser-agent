"""AI orchestration tests. A scripted MockProvider stands in for the LLM; a FakeBrowser stands in for Chrome behind the REAL dispatcher."""
import asyncio
import json
import unittest

from octg_agent.brain.settings import BrainSettings
from octg_agent.brain.task import TaskStatus
from octg_agent.reasoning.provider import ProviderError
from octg_agent.reasoning.providers.mock import MockProvider

from .fakebrowser import FakeBrowser, el, shop_pages
from .helpers import Harness, wait_until

T = MockProvider.call
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def obs_of(options):
    return options["meta"]["observation"]


def eid(options, name):
    return next(e["id"] for _, _, e, _ in obs_of(options).cands if e["name"] == name)


def tab_ids(options):
    return [t["tab_id"] for t in obs_of(options).tabs]


class Env:
    """Harness + fake browser + task manager with a mock provider, configured for fast tests."""

    def __init__(self, provider=None, providers=None, pages=None, start_url="https://start.test/", **brain):
        self.fb = FakeBrowser(pages or shop_pages(), start_url)
        self.providers = providers or {"mock": provider or MockProvider()}
        self.brain = BrainSettings(**{"max_steps": 15, "max_seconds": 30, "router": {"policy": "FALLBACK", "order": list(self.providers)}, **brain})

    async def __aenter__(self):
        self.h = Harness(providers=self.providers, brain=self.brain)
        await self.h.__aenter__()
        self.ext = await self.h.extension(handlers=self.fb.handlers())
        self.mgr = self.h.rt.tasks
        self.events: list[dict] = []
        self.mgr.listeners.append(self.events.append)
        return self

    async def __aexit__(self, *a):
        await self.h.__aexit__(*a)

    async def run(self, instruction="do the thing", until=TERMINAL | {"WAITING_FOR_APPROVAL", "WAITING_FOR_USER"}, **kw):
        t = await self.mgr.start(instruction, **kw)
        await self.until(until)
        return t

    async def until(self, statuses, timeout=10):
        t = self.mgr.tasks[self.mgr.current_id]
        ok = await wait_until(lambda: t.status.value in statuses, timeout)
        assert ok, f"task stuck in {t.status.value}; activity: {[a['text'] for a in t.activity][-6:]}"
        return t

    @property
    def task(self):
        return self.mgr.tasks[self.mgr.current_id]

    def actions(self, name=None):
        return [(a, p) for a, p in self.fb.log if name is None or a == name]


def finish(summary="done", status="completed"):
    return T("finish_task", status=status, summary=summary)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_task_state_has_every_required_field(self):
        async with Env(MockProvider([finish()])) as e:
            t = await e.mgr.start("Open the shop")
            d = t.to_dict()
            for k in ("task_id", "user_instruction", "status", "goal", "current_step", "completed_steps", "failed_steps", "current_tab", "known_tabs", "current_url",
                      "current_page_title", "latest_observation", "available_actions", "last_action", "last_action_result", "verification_result",
                      "pending_approval", "error", "started_at", "updated_at"):
                self.assertIn(k, d, k)
            self.assertEqual(d["user_instruction"], "Open the shop")
            self.assertTrue(d["task_id"].startswith("task_"))
            await e.until({"COMPLETED"})

    async def test_status_lifecycle_and_completion(self):
        async with Env(MockProvider([T("navigate", url="shop.test", reason="go to the shop"), finish("On the shop page")])) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
            seen = []
            for ev in e.events:
                s = ev["task"]["status"]
                if not seen or seen[-1] != s:
                    seen.append(s)
            self.assertEqual(seen[:3], ["PENDING", "PLANNING", "RUNNING"])
            self.assertEqual(seen[-1], "COMPLETED")
            self.assertEqual(t.result, "On the shop page")
            self.assertEqual(t.step_count, 1)
            self.assertEqual(t.current_url, "https://shop.test/")
            self.assertIsNotNone(t.finished_at)
            row = e.h.rt.history.get(t.task_id)
            self.assertEqual(row["status"], "COMPLETED")
            self.assertEqual(row["browser_actions"], 1)

    async def test_empty_task_and_second_task_are_refused(self):
        async with Env(MockProvider(handler=lambda m, t, o: asyncio.sleep(5))) as e:
            from octg_agent.brain.manager import TaskError
            with self.assertRaises(TaskError) as cm:
                await e.mgr.start("   ")
            self.assertEqual(cm.exception.code, "EMPTY_TASK")
            await e.mgr.start("first")
            with self.assertRaises(TaskError) as cm:
                await e.mgr.start("second")
            self.assertEqual(cm.exception.code, "TASK_ALREADY_RUNNING")
            e.mgr.stop()


class ObserveActObserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_step_search_task_uses_tools_through_the_dispatcher(self):
        script = [
            T("navigate", url="shop.test", reason="open the shop", expect_url_contains="shop.test"),
            lambda m, t, o: T("type", element_id=eid(o, "Search for products"), text="wireless headphones", reason="enter the query"),
            lambda m, t, o: T("press_key", element_id=eid(o, "Search for products"), key="Enter", reason="submit the search", expect_text="results"),
            lambda m, t, o: finish("Results page for wireless headphones is showing"),
        ]
        prov = MockProvider(script)
        async with Env(prov) as e:
            t = await e.run("Search the shop for wireless headphones and stop on the results page")
            self.assertEqual(t.status, TaskStatus.COMPLETED, t.error)
            self.assertEqual([a for a, _ in e.actions()], ["navigate", "type", "press_key"])
            self.assertTrue(e.fb.tabs[e.fb.active]["url"].endswith("q=wireless+headphones"))
            # every browser call went through the real dispatcher: it is in the structured action log with source=agent
            logged = [x for x in e.h.rt.actionlog.tail(200) if x.get("source") == "agent" and x["action"] in ("navigate", "type", "press_key")]
            self.assertEqual(len(logged), 3)
            # observe -> act -> observe: the 2nd prompt carries the result of step 1 and a NEW observation of the loaded page
            second = prov.calls[1]["messages"][1].content
            self.assertIn("LAST RESULT: navigate ok", second)
            self.assertIn("Search for products", second)
            self.assertIn("snapshot snap_", second)
            # expectation was verified through the engine (wait/until) and recorded
            self.assertTrue(t.completed_steps[0].ok)
            self.assertIn("MET", prov.calls[1]["messages"][1].content)

    async def test_element_ids_are_bound_to_the_snapshot_that_was_shown(self):
        script = [T("navigate", url="shop.test"), lambda m, t, o: T("click", element_id=eid(o, "Search"), snapshot_id="snap_FORGED", reason="search")]
        async with Env(MockProvider(script + [lambda m, t, o: finish()])) as e:
            await e.run()
            ((_, params),) = e.actions("click")
            self.assertNotEqual(params["snapshot_id"], "snap_FORGED")  # a model-supplied snapshot id is never trusted
            self.assertTrue(params["snapshot_id"].startswith("snap_"))
            self.assertEqual(params["tab_id"], e.fb.active)  # and the action is pinned to the observed tab

    async def test_tool_validation_feeds_errors_back_without_touching_the_browser(self):
        prov = MockProvider([T("teleport"), T("click", bogus=1), T("navigate"), finish("ok")])
        async with Env(prov) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
            self.assertEqual(e.fb.log, [])  # nothing invalid reached Chrome
            self.assertIn("Unknown tool 'teleport'", prov.calls[1]["messages"][1].content)
            self.assertIn("no parameter", prov.calls[2]["messages"][1].content)
            self.assertIn("Invalid arguments for navigate", prov.calls[3]["messages"][1].content)
            self.assertEqual(t.model_calls, 4)

    async def test_repeated_unusable_replies_fail_the_task_instead_of_looping(self):
        async with Env(MockProvider(handler=lambda m, t, o: "I think I should click something"), max_tool_errors=3) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.FAILED)
            self.assertIn("unusable", t.error)
            self.assertEqual(t.model_calls, 3)

    async def test_step_limit_stops_a_task_that_never_finishes(self):
        n = {"i": 0}
        def varied(m, t, o):
            n["i"] += 1
            return T("scroll", direction="down", amount=100 + n["i"])

        async with Env(MockProvider(handler=varied), max_steps=4) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.FAILED)
            self.assertIn("step limit", t.error)
            self.assertEqual(t.step_count, 4)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_element_triggers_a_fresh_snapshot_and_re_identification(self):
        state = {"n": 0}

        def stale_once(p):
            state["n"] += 1
            if state["n"] == 1:  # the page re-rendered between observation and click
                e.fb.snap[e.fb.active]["id"] = "snap_RERENDERED"
            return None

        script = [T("navigate", url="shop.test"), lambda m, t, o: T("click", element_id=eid(o, "Search"), reason="first try"),
                  lambda m, t, o: T("click", element_id=eid(o, "Search"), reason="retry on the new snapshot"), lambda m, t, o: finish("searched")]
        prov = MockProvider(script)
        async with Env(prov) as e:
            e.fb.faults["click"] = stale_once
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED, t.error)
            clicks = e.actions("click")
            self.assertEqual(len(clicks), 2)
            self.assertNotEqual(clicks[0][1]["snapshot_id"], clicks[1][1]["snapshot_id"])  # the stale id was never reused
            self.assertEqual(t.failed_steps[0].error_code, "STALE_ELEMENT")
            third = prov.calls[2]["messages"][1].content
            self.assertIn("STALE_ELEMENT", third)
            self.assertIn("re-identify", third)
            self.assertIn(f"snapshot {clicks[1][1]['snapshot_id']}", third)
            inspects = [x for x in e.ext.received if x["action"] == "inspect_page"]
            self.assertGreaterEqual(len(inspects), 3)

    async def test_repeated_stale_results_hand_over_to_the_user(self):
        async with Env(MockProvider(handler=lambda m, t, o: T("navigate", url="shop.test") if not e_state(o) else T("click", element_id="el_999")), max_stale_retries=2) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_USER)
            self.assertIn("cannot reliably identify", t.waiting_message)
            e.mgr.stop()

    async def test_navigation_timeout_re_verifies_the_real_state(self):
        def hung_but_loaded(p):
            e.fb.tabs[e.fb.active]["url"] = "https://shop.test/"
            return {"success": False, "result": None, "error": {"code": "ACTION_TIMEOUT", "message": "no reply"}}

        prov = MockProvider([T("navigate", url="shop.test"), lambda m, t, o: finish("shop is open")])
        async with Env(prov) as e:
            e.fb.faults["navigate"] = hung_but_loaded
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
            second = prov.calls[1]["messages"][1].content
            self.assertIn("ACTION_TIMEOUT", second)
            self.assertIn("do not assume", second)
            self.assertIn("ACTIVE TAB", second)
            self.assertIn("https://shop.test/", second)  # the observation shows what actually happened

    async def test_unsupported_page_navigate_away_works_but_insisting_stops_the_task(self):
        script = [T("scroll", direction="down"), T("scroll", direction="down")]
        async with Env(MockProvider(script), start_url="chrome://newtab/") as e:
            prov = e.providers["mock"]
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.FAILED)
            self.assertIn("cannot be controlled", t.error)
            self.assertIn("CANNOT be controlled", prov.calls[0]["messages"][1].content)
            self.assertEqual(e.fb.log, [])
        async with Env(MockProvider([T("navigate", url="shop.test"), lambda m, t, o: finish("ok")]), start_url="chrome://newtab/") as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)

    async def test_extension_disconnect_waits_then_continues(self):
        prov = MockProvider([T("navigate", url="shop.test"), lambda m, t, o: finish("ok")])
        async with Env(prov, reconnect_wait_s=5) as e:
            await e.h.rt.session.close("test drop")
            t = await e.mgr.start("anything")
            await asyncio.sleep(0.5)
            self.assertNotIn(t.status.value, TERMINAL)
            await e.h.extension(handlers=e.fb.handlers())
            await e.until({"COMPLETED"})


def e_state(options):
    return obs_of(options).url.startswith("https://shop.test")


class LoopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeating_a_useless_action_triggers_a_hint_then_a_handover_instead_of_burning_steps(self):
        prov = MockProvider(handler=lambda m, t, o: T("navigate", url="https://missing.test/"))
        async with Env(prov, max_steps=30, max_repeat_actions=4) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_USER)
            self.assertEqual(t.waiting_kind, "stuck")
            self.assertEqual(t.step_count, 5, "the guard must stop the loop long before the step limit")  # 1 real navigation + 4 useless repeats
            self.assertIn("repeated navigate", prov.calls[4]["messages"][1].content)
            e.mgr.stop()

    async def test_error_pages_are_flagged_to_the_model(self):
        pages = shop_pages()
        pages["https://shop.test/gone"] = {"title": "404 Not Found", "text": "nothing here", "elements": []}
        prov = MockProvider([T("navigate", url="https://shop.test/gone"), lambda m, t, o: finish("it is a 404", "failed")])
        async with Env(prov, pages=pages) as e:
            await e.run()
            self.assertIn("error_page", prov.calls[1]["messages"][1].content)

    async def test_progress_resets_the_guard(self):
        script = [T("scroll", direction="down")] * 3 + [T("navigate", url="shop.test")] + [T("scroll", direction="down")] * 2 + [finish("ok")]
        async with Env(MockProvider(script), max_repeat_actions=4) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)


class TabTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_follows_a_new_tab_and_switches_back(self):
        origin = {}

        def click_help(m, t, o):
            origin["tab"] = obs_of(o).active_tab_id
            return T("click", element_id=eid(o, "Help (opens in new tab)"), reason="open help")

        def switch_back(m, t, o):
            return T("switch_tab", tab_id=origin["tab"], reason="back to the shop")

        prov = MockProvider([T("navigate", url="shop.test"), click_help, lambda m, t, o: T("read_page", reason="read the help tab"), switch_back, lambda m, t, o: finish("done")])
        async with Env(prov) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED, t.error)
            after_click = prov.calls[2]["messages"][1].content
            self.assertIn("help.test", after_click)
            self.assertIn("new tab #", after_click)
            self.assertEqual(len(prov.calls[2]["messages"][1].content.split("TABS (")[1].split(")")[0]), 1)
            self.assertIn("https://shop.test/", prov.calls[4]["messages"][1].content.split("ACTIVE TAB")[1])
            self.assertEqual(e.fb.active, origin["tab"])
            self.assertEqual(len(t.created_tabs), 1)  # the task remembers which tab it opened
            self.assertEqual(len(t.known_tabs), 2)

    async def test_closing_a_tab_the_task_did_not_open_needs_approval(self):
        async with Env(MockProvider(handler=lambda m, t, o: T("close_tab", tab_id=obs_of(o).active_tab_id))) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_APPROVAL)
            self.assertIn("did not open", t.pending_approval.risk["reasons"][0])
            e.mgr.deny()
            await e.until({"WAITING_FOR_APPROVAL", "FAILED", "COMPLETED"} - {"WAITING_FOR_APPROVAL"} | {"RUNNING"})
            e.mgr.stop()


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, after):
        return MockProvider([T("navigate", url="shop.test"), lambda m, t, o: T("click", element_id=eid(o, "Delete account"), reason="user asked to delete"), *after])

    async def test_high_risk_click_waits_for_approval_and_resumes_from_task_state(self):
        async with Env(self.provider([lambda m, t, o: finish("deleted")])) as e:
            t = await e.run("Delete my account on the shop")
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_APPROVAL)
            self.assertEqual(e.actions("click"), [], "a high-risk action must not run before approval")
            pa = t.pending_approval
            self.assertIn("Delete account", pa.description)
            self.assertEqual(pa.risk["level"], "high")
            self.assertEqual(pa.tool, "click")
            e.mgr.approve()
            await e.until({"COMPLETED"})
            self.assertEqual(len(e.actions("click")), 1)
            self.assertIsNone(t.pending_approval)
            self.assertEqual(e.fb.tabs[e.fb.active]["url"], "https://shop.test/deleted")

    async def test_denied_action_is_never_executed_and_the_model_is_told(self):
        prov = self.provider([lambda m, t, o: finish("left the account alone", "failed")])
        async with Env(prov) as e:
            await e.run()
            e.mgr.deny()
            await e.until({"FAILED", "COMPLETED"})
            self.assertEqual(e.actions("click"), [])
            self.assertIn("DENIED", prov.calls[-1]["messages"][1].content)

    async def test_cancel_while_waiting_for_approval(self):
        async with Env(self.provider([])) as e:
            await e.run()
            e.mgr.stop()
            await e.until({"CANCELLED"})
            self.assertEqual(e.actions("click"), [])

    async def test_model_requested_approval_is_asked_once_not_twice(self):
        script = [T("navigate", url="shop.test"), T("request_approval", summary="Delete the account via the Delete account button"),
                  lambda m, t, o: T("click", element_id=eid(o, "Delete account")), lambda m, t, o: finish("done")]
        prov = MockProvider(script)
        async with Env(prov) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_APPROVAL)
            self.assertEqual(t.pending_approval.source, "model")
            e.mgr.approve()
            await e.until({"COMPLETED"})  # the gated click consumed the grant: no second prompt
            self.assertEqual(len(e.actions("click")), 1)
            self.assertIn("APPROVED", prov.calls[2]["messages"][1].content)

    async def test_medium_risk_is_automatic_unless_configured(self):
        script = lambda: MockProvider([T("navigate", url="shop.test"), lambda m, t, o: T("click", element_id=eid(o, "Search")), lambda m, t, o: finish()])
        async with Env(script()) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
        async with Env(script(), approve_medium=True) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_APPROVAL)
            e.mgr.stop()

    async def test_approve_when_nothing_is_pending_is_an_error(self):
        from octg_agent.brain.manager import TaskError
        async with Env(MockProvider(handler=lambda m, t, o: asyncio.sleep(5))) as e:
            await e.mgr.start("x")
            with self.assertRaises(TaskError):
                e.mgr.approve()
            e.mgr.stop()


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_blocks_new_actions_and_resume_continues(self):
        prov = MockProvider([T("navigate", url="shop.test"), T("scroll", direction="down"), T("scroll", direction="down"), finish("ok")])
        async with Env(prov) as e:
            await e.mgr.start("scroll a bit")
            await wait_until(lambda: len(e.actions()) >= 1)
            e.mgr.pause()
            await e.until({"PAUSED"})
            frozen = len(e.actions())
            calls = len(prov.calls)
            await asyncio.sleep(0.5)
            self.assertEqual(len(e.actions()), frozen, "no browser action may start while paused")
            self.assertLessEqual(len(prov.calls), calls + 1)
            e.mgr.resume()
            await e.until({"COMPLETED"})
            self.assertEqual(len(e.actions()), 3)

    async def test_stop_cancels_a_slow_model_call_quickly(self):
        async def slow(m, t, o):
            await asyncio.sleep(30)

        async with Env(MockProvider(handler=slow)) as e:
            await e.mgr.start("anything")
            await asyncio.sleep(0.3)
            e.mgr.stop()
            t = await e.until({"CANCELLED"}, timeout=3)
            self.assertEqual(t.status, TaskStatus.CANCELLED)
            self.assertEqual(e.fb.log, [])

    async def test_stop_after_finish_is_rejected(self):
        from octg_agent.brain.manager import TaskError
        async with Env(MockProvider([finish()])) as e:
            await e.run()
            with self.assertRaises(TaskError):
                e.mgr.stop()


class UserInterventionTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_into_a_password_field_is_refused_and_the_user_is_asked(self):
        script = [T("navigate", url="login.test"), lambda m, t, o: T("type", element_id=eid(o, "Password"), text="hunter2-secret"), lambda m, t, o: finish("user logged in")]
        async with Env(MockProvider(script)) as e:
            t = await e.run("Log in")
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_USER)
            self.assertEqual(t.waiting_kind, "sensitive_input")
            self.assertEqual(e.actions("type"), [], "a credential must never be typed by the agent")
            e.mgr.resume()
            await e.until({"COMPLETED"})
            blob = json.dumps(t.to_dict()) + json.dumps(e.h.rt.history.get(t.task_id))
            self.assertNotIn("hunter2", blob)

    async def test_login_required_handover_via_ask_user_and_resume(self):
        script = [T("navigate", url="login.test"), lambda m, t, o: T("ask_user", kind="login", question="Login required. Please log in manually."),
                  lambda m, t, o: finish("continued after login")]
        prov = MockProvider(script)
        async with Env(prov) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_USER)
            self.assertEqual(t.waiting_kind, "login")
            self.assertIn("Login required", t.waiting_message)
            self.assertIn("login_form", prov.calls[1]["messages"][1].content)  # the page signal was shown to the model
            e.mgr.resume()
            await e.until({"COMPLETED"})
            self.assertIn("user says they are ready", prov.calls[2]["messages"][1].content)

    async def test_captcha_pauses_automatically_and_is_not_re_triggered_after_resume(self):
        prov = MockProvider([T("navigate", url="captcha.test"), lambda m, t, o: finish("past the check")])
        async with Env(prov) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.WAITING_FOR_USER)
            self.assertEqual(t.waiting_kind, "captcha")
            self.assertIn("CAPTCHA", t.waiting_message)
            calls_before = len(prov.calls)
            self.assertEqual(calls_before, 1, "no model call is made while a CAPTCHA blocks the page")
            e.mgr.resume()
            await e.until({"COMPLETED"})
            self.assertIn("captcha", prov.calls[1]["messages"][1].content)
            self.assertEqual(sum(1 for a in t.activity if "CAPTCHA detected" in a["text"]), 1)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_provider_falls_back_to_the_next_one(self):
        local = MockProvider(handler=lambda m, t, o: ProviderError("unavailable", "no runtime"), name="local", local=True)
        strong = MockProvider([T("navigate", url="shop.test"), lambda m, t, o: finish("ok")], name="gemini", model="gemini-3.5-flash-lite")
        async with Env(providers={"local": local, "gemini": strong}) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
            self.assertEqual(t.usage["gemini"].calls, 2)
            self.assertNotIn("local", t.usage)  # a failed call produces no usage
            self.assertIn("fell back to gemini", t.routing["reason"])
            self.assertGreaterEqual(t.model_calls, 3)  # failed attempts still count as calls

    async def test_all_providers_failing_fails_the_task_with_a_clear_error(self):
        bad = lambda n: MockProvider(handler=lambda m, t, o: ProviderError("rate_limit", "429", provider=n), name=n)
        async with Env(providers={"local": bad("local"), "gemini": bad("gemini")}) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.FAILED)
            self.assertIn("No model provider could answer", t.error)

    async def test_rate_limited_provider_is_skipped_on_later_steps(self):
        flaky = MockProvider([ProviderError("rate_limit", "429", retry_after=60)], name="local", local=True)
        strong = MockProvider([T("navigate", url="shop.test"), T("scroll", direction="down"), finish("ok")], name="gemini")
        async with Env(providers={"local": flaky, "gemini": strong}) as e:
            await e.run()
            self.assertEqual(len(flaky.calls), 1)  # cooldown: never asked again during the task


class AccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_and_cost_are_tracked_per_provider(self):
        gem = MockProvider([T("navigate", url="shop.test"), T("scroll", direction="down")], name="gemini", model="gemini-3.5-flash-lite", usage=(1000, 200))
        loc = MockProvider([finish("ok")], name="local", model="qwen3:8b", usage=(800, 50), local=True)
        loc.script.insert(0, ProviderError("unavailable", "x"))  # force the order gemini, gemini, local below
        async with Env(providers={"gemini": gem, "local": loc}, router={"policy": "FALLBACK", "order": ["gemini", "local"]}) as e:
            gem.script.append(ProviderError("unavailable", "gemini down"))
            gem.script.append(ProviderError("unavailable", "gemini down"))
            loc.script.pop(0)
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
            u = t.usage_dict()
            self.assertEqual(u["by_provider"]["gemini"]["calls"], 2)
            self.assertEqual(u["by_provider"]["gemini"]["input_tokens"], 2000)
            self.assertAlmostEqual(u["by_provider"]["gemini"]["estimated_cost_usd"], (2000 * 0.30 + 400 * 2.50) / 1e6, places=6)
            self.assertEqual(u["by_provider"]["local"]["estimated_cost_usd"], 0.0)
            self.assertEqual(u["by_provider"]["local"]["input_tokens"], 800)
            row = e.h.rt.history.get(t.task_id)
            self.assertEqual(row["usage"]["by_provider"]["gemini"]["calls"], 2)
            report = e.h.rt.history.usage_report()
            self.assertEqual(report["gemini"]["calls"], 2)

    async def test_missing_usage_is_counted_not_invented(self):
        async with Env(providers={"gemini": MockProvider([finish()], usage=(None, None), model="gemini-3.5-flash-lite", name="gemini")}) as e:
            t = await e.run()
            u = t.usage_dict()
            self.assertEqual(u["by_provider"]["gemini"]["calls_without_usage"], 1)
            self.assertIsNone(u["estimated_cost_usd"])


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_stays_bounded_over_many_steps_and_large_pages(self):
        big = shop_pages()
        big["https://shop.test/"]["elements"] += [el("link", f"Product number {i} in the catalogue", href=f"https://shop.test/p/{i}") for i in range(250)]
        sizes = []

        def step(m, t, o):
            sizes.append(len(m[1].content))
            return T("scroll", direction="down", amount=100 + len(sizes)) if len(sizes) < 18 else finish("enough")

        prov = MockProvider(handler=step)
        async with Env(prov, pages=big, start_url="https://shop.test/", prompt_token_budget=2500, max_steps=30) as e:
            t = await e.run()
            self.assertEqual(t.status, TaskStatus.COMPLETED)
            self.assertLessEqual(max(sizes), int(2500 * 3.6), "prompt exceeded the token budget")
            self.assertLess(sizes[-1], sizes[3] * 1.4 + 400, "the prompt grows with the number of steps: it is acting like a transcript")
            last = prov.calls[-1]["messages"][1].content
            self.assertIn("(earlier:", last)  # old steps are summarised
            self.assertLessEqual(last.count("\n scroll"), 0)
            self.assertIn("omitted", last)

    async def test_task_state_does_not_leak_between_tasks(self):
        prov = MockProvider([T("navigate", url="shop.test", note="price is 5"), finish("one")])
        async with Env(prov) as e:
            t1 = await e.run("first")
            prov.script += [lambda m, t, o: finish("two")]
            t2 = await e.run("second")
            second = prov.calls[-1]["messages"][1].content
            self.assertNotIn("price is 5", second)
            self.assertNotIn("first", second.split("TASK:")[1].split("\n")[0])
            self.assertIn("(no steps yet)", second)
            self.assertNotEqual(t1.task_id, t2.task_id)


class HistoryPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_keeps_actions_not_secrets_or_page_text(self):
        script = [T("navigate", url="login.test"), lambda m, t, o: T("type", element_id=eid(o, "Email"), text="4111111111111111", reason="email"),
                  lambda m, t, o: T("read_page", reason="read"), lambda m, t, o: finish("ok")]
        async with Env(MockProvider(script)) as e:
            t = await e.run()
            row = json.dumps(e.h.rt.history.get(t.task_id))
            self.assertNotIn("4111111111111111", row)  # long digit runs are masked even in ordinary fields
            self.assertNotIn("Sign in to continue", row)  # page text is not stored
            self.assertIn("navigate", row)


if __name__ == "__main__":
    unittest.main()
