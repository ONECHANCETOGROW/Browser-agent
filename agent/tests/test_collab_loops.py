"""The loop controller: durable research -> plan -> implement -> wait -> verify -> decide cycles for one client.

Two kinds of tests:
  * controller tests with a deterministic scripted measurement (a test-only capability + loop kind): scheduling, restart recovery, cancellation, retries, isolation, approvals, stop conditions,
    comparison, unavailable-vs-negative data. Time is a controllable clock.
  * one complete SEO loop (class SeoLoopDemo) with the REAL controller, REAL collaboration capabilities (campaign, search observation, site audit, handoff, change, approval with PIN, deploy record,
    verification), REAL Hub endpoints. Mocked and labelled as such: the search-results pages (a scripted fake browser extension), the client website (a local fixture server), and the passing of time.
"""
from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("OCTG_LOOP_TICK_S", "3600")   # the background scheduler stays out of the way; tests call tick() themselves (one test starts the real ticker)

from aiohttp.test_utils import TestServer

from octg_agent.collab.loop_kinds import SeoVisibility, _standard, streak_verdict
from octg_agent.collab.loops import FINAL, LoopController, LoopKind, fingerprint, register_kind
from octg_agent.collab.store import CollabError
from octg_agent.hub.security import HubPin
from octg_agent.seo import fetch as F

from .test_collab import COUNTS, Collab, CollabCase, HITS, _reg, fixture_site

SCRIPT: dict = {"values": []}
CLOCK_REF: dict = {}


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


# ------------------------------------------------------------------ a scripted, deterministic measurement
async def _metric(ctx, p):
    COUNTS["metric"] = COUNTS.get("metric", 0) + 1
    if p.get("sleep"):
        await asyncio.sleep(p["sleep"])
    v = SCRIPT["values"].pop(0) if SCRIPT["values"] else None
    if v == "fail":
        raise CollabError("EXTENSION_NOT_CONNECTED", "the browser is not connected", retryable=True)
    if v == "unavailable":
        return {"unavailable": True}
    return {"position": v}


_reg("test.metric", _metric, props={"sleep": {"type": "number"}})


class ScriptKind(LoopKind):
    name = "test_script"
    title = "scripted"
    description = "test only"
    permissions = ("test:internal",)

    def check(self, svc, client_id, config, goal):
        goal.setdefault("targets", [{"keyword": "a"}])
        return []

    def requests(self, loop, purpose):
        return [{"capability": "test.metric", "parameters": ({"sleep": loop["config"]["sleep"]} if "sleep" in loop["config"] else {}), "label": "metric"}]

    def targets(self, loop):
        return [{"key": "metric|a", "label": "metric a", "max_position": (loop["goal"].get("targets") or [{}])[0].get("max_position", 10)}]

    def observations(self, loop, purpose, responses):
        r, now = responses[0], CLOCK_REF["c"]()
        if r["status"] != "succeeded" or r["result"].get("unavailable"):
            return [_standard({"key": "metric|a", "label": "metric a", "metric": "position", "status": "unavailable", "observed_at": now, "limits": ["scripted: unavailable"]})]
        pos = r["result"]["position"]
        return [_standard({"key": "metric|a", "label": "metric a", "metric": "position", "value": pos, "status": "found" if pos else "not_found", "comparable": True, "cmp_key": "t",
                           "observed_at": now, "request_id": r["request_id"]})]

    def evaluate_target(self, target, series, confirmations, min_gap_s):
        usable = [o for o in series if o["status"] in ("found", "not_found")]
        if not usable:
            return {"verdict": "insufficient", "observations": 0, "volatile": False}
        mp = target.get("max_position", 10)
        verdict, streak = streak_verdict(usable, lambda o: o["status"] == "found" and o["value"] <= mp, confirmations, min_gap_s)
        return {"verdict": verdict, "streak": streak, "needed": confirmations, "observations": len(usable), "volatile": False}


register_kind(ScriptKind())

ITEM = {"title": "Fix the duplicate H1 on the drains page", "action_kind": "heading_structure", "pages": ["https://x.test/drains/"], "finding_key": "h1_multiple", "targets": ["metric|a"]}
ITEM2 = {"title": "Add a drain-cleaning section with local details", "action_kind": "content_addition", "pages": ["https://x.test/drains/"], "finding_key": "thin_content", "targets": ["metric|a"]}
ITEM3 = {"title": "Link the area pages from the footer", "action_kind": "internal_links", "pages": ["https://x.test/"], "finding_key": "weak_links", "targets": ["metric|a"]}


class LoopCase(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        SCRIPT["values"] = []
        self.clock = Clock()
        self.svc.loops.clock = self.clock
        CLOCK_REF["c"] = self.clock
        self.cid = await self.new_client("Alpha Plumbing LLC")

    @property
    def ctrl(self) -> LoopController:
        return self.svc.loops

    async def restart_controller(self) -> LoopController:
        """A restart as far as the loop controller is concerned: a new controller that knows only what the database holds."""
        await self.svc.loops.stop_ticker()
        self.svc.loops = LoopController(self.svc, clock=self.clock)
        return self.svc.loops

    async def create(self, goal=None, config=None, client=None, kind="test_script"):
        g = {"targets": [{"keyword": "a", "max_position": 10}], "confirmations": 2, "max_rounds": 5, **(goal or {})}
        c = {"min_gap_hours": 0, "recheck_after_hours": 1, "confirm_gap_hours": 1, "retry_backoff_s": [5, 10], **(config or {})}
        return await self.c.run("loop.create", {"kind": kind, "title": "demo loop", "config": c, "goal": g}, client=client or self.cid)

    async def start(self, **kw) -> str:
        r = await self.create(**kw)
        self.assertEqual(r["status"], "succeeded", r)
        return r["result"]["loop"]["loop_id"]

    async def settle(self, lid, timeout=10.0):
        end = time.time() + timeout
        while True:
            d = await self.ctrl.advance(lid)
            if d["phase"] != "measuring" or d["status"] != "active" or d["retry_at"] or time.time() > end:
                return d
            await asyncio.sleep(0.05)

    def make_change(self, cid=None, *, status="deployed", production=True, approval="executed"):
        st, cid = self.svc.store, cid or self.cid
        ap_id = None
        if production and approval:
            ap_id = st.request_approval(task_id=None, client_id=cid, action_kind="production_deploy", title="deploy", requested_by="collaborator")["approval_id"]
            if approval in ("approved", "executed"):
                st.transition_approval(ap_id, "approved", actor="owner:test")
            if approval == "executed":
                st.transition_approval(ap_id, "executed", actor="collaborator")
        body = {"target": {"environment": "production" if production else "staging"}, "deployment": {"approval_id": ap_id} if status in ("deployed", "verified") else None}
        return st.add_artifact(task_id=None, client_id=cid, kind="change", stage="implementation", title="Change: x", body=body, links={}, status=status, created_by="collaborator")["artifact_id"]

    async def plan(self, lid, items=None, **kw):
        return await self.c.run("loop.submit_plan", {"loop_id": lid, "items": [ITEM] if items is None else items, "summary": "because the evidence shows it", **kw}, client=kw.pop("client", self.cid))

    async def report(self, lid, change_ids, client=None):
        return await self.c.run("loop.report_implemented", {"loop_id": lid, "change_ids": change_ids}, client=client or self.cid)

    async def round_trip(self, lid, item, hours=3601):
        """plan -> deployed change -> report -> time passes -> the re-measurement runs; returns the loop after it settled."""
        p = await self.plan(lid, [item])
        self.assertEqual(p["status"], "succeeded", p)
        r = await self.report(lid, [self.make_change()])
        self.assertEqual(r["status"], "succeeded", r)
        self.clock.t += hours
        self.assertEqual(await self.ctrl.tick(), 1)
        return await self.settle(lid)


# =====================================================================================================================  the cycle, comparison, reliable goal
class CycleTests(LoopCase):
    async def test_full_cycle_baseline_plan_implement_wait_verify_and_a_confirmed_goal(self):
        SCRIPT["values"] = [25, 12, 8, 7]
        lid = await self.start()
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["phase"], d["round"]), ("active", "awaiting_plan", 0))
        self.assertEqual(d["goal_progress"]["status"], "not_met")
        self.assertEqual(d["next_action"]["who"], "collaborator")
        self.assertIn("for_planning", d)
        self.assertEqual([o["value"] for o in d["latest_observations"]], [25.0])
        # round 1: plan, change, report, wait (nothing runs while waiting), verify
        p = await self.plan(lid)
        self.assertEqual(p["result"]["loop"]["phase"], "awaiting_implementation")
        r = await self.report(lid, [self.make_change()])
        due = r["result"]["loop"]["next_check_at"]
        self.assertEqual((r["result"]["loop"]["phase"], round(due - self.clock.t)), ("waiting", 3600))
        self.assertEqual(await self.ctrl.tick(), 0, "not due yet")
        self.assertEqual(COUNTS["metric"], 1, "nothing was measured while waiting")
        self.clock.t += 3601
        self.assertEqual(await self.ctrl.tick(), 1)
        d = await self.settle(lid)
        self.assertEqual((d["phase"], d["round"], COUNTS["metric"]), ("awaiting_plan", 1, 2))
        row = d["for_planning"]["comparison"]["rows"][0]
        self.assertEqual((row["baseline"]["value"], row["previous"]["value"], row["now"]["value"], row["change"], row["delta_vs_baseline"]), (25.0, 25.0, 12.0, "improved", -13.0))
        self.assertEqual(d["goal_progress"]["status"], "not_met")
        # round 2: the target (position 10) is reached once -> NOT declared reached on one observation
        d = await self.round_trip(lid, ITEM2)
        self.assertEqual((d["phase"], d["status"], d["goal_progress"]["status"]), ("waiting", "active", "met_unconfirmed"))
        self.assertEqual(d["goal_progress"]["targets"][0]["streak"], 1)
        events = [e["event"] for e in self.ctrl.db.events(lid)]
        self.assertIn("confirmation_scheduled", events)
        self.assertNotIn("goal_reached", events)
        # a second comparable observation after the confirmation gap -> reached
        self.clock.t += 3601
        self.assertEqual(await self.ctrl.tick(), 1)
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["phase"], d["outcome"]), ("goal_reached", "done", "goal"))
        self.assertEqual(COUNTS["metric"], 4)
        full = self.ctrl.describe(lid, full=True)
        last = full["comparisons"][-1]["rows"][0]
        self.assertEqual((last["baseline"]["value"], last["now"]["value"], last["delta_vs_baseline"]), (25.0, 7.0, -18.0))
        self.assertEqual([r["n"] for r in d["rounds"]], [0, 1, 2])
        self.assertEqual(d["rounds"][1]["item_effects"][fingerprint(ITEM)], "improved")
        order = [e["event"] for e in full["events"]]
        for needed in ("created", "measurement_started", "measured", "evaluated", "awaiting_plan", "plan_submitted", "implemented", "confirmation_scheduled", "goal_reached"):
            self.assertIn(needed, order)
        self.assertEqual(self.svc.store.get_task(d["task_id"])["status"], "completed")
        r = await self.c.run("loop.advance", {"loop_id": lid}, client=self.cid)
        self.assertEqual(r["result"]["loop"]["status"], "goal_reached", "a finished loop stays finished")

    async def test_a_target_met_once_is_not_reached_if_the_next_comparable_observation_misses(self):
        SCRIPT["values"] = [25, 8, 30]
        lid = await self.start()
        await self.settle(lid)
        d = await self.round_trip(lid, ITEM)
        self.assertEqual(d["goal_progress"]["status"], "met_unconfirmed")
        self.clock.t += 3601
        await self.ctrl.tick()
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["phase"], d["goal_progress"]["status"]), ("active", "awaiting_plan", "not_met"), "the streak was reset by a comparable miss")

    async def test_two_checks_closer_together_than_the_minimum_gap_are_one_observation_not_a_confirmation(self):
        SCRIPT["values"] = [25, 8, 7, 6]
        lid = await self.start(config={"min_gap_hours": 24, "confirm_gap_hours": 1})
        await self.settle(lid)
        d = await self.round_trip(lid, ITEM)
        self.assertEqual(d["goal_progress"]["status"], "met_unconfirmed")
        self.clock.t += 3601
        await self.ctrl.tick()
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["goal_progress"]["targets"][0]["streak"]), ("active", 1), "1 hour later is not an independent confirmation when 24 h are required")
        self.clock.t += 25 * 3600
        await self.ctrl.tick()
        d = await self.settle(lid)
        self.assertEqual(d["status"], "goal_reached")

    def test_streak_and_volatility_rules_are_deterministic(self):
        mk = lambda vals, step=100.0: [{"status": "found" if v else "not_found", "value": v, "observed_at": 1000 + i * step, "comparable": True} for i, v in enumerate(vals)]
        meets = lambda o: o["status"] == "found" and o["value"] <= 10
        self.assertEqual(streak_verdict(mk([8, 7]), meets, 2, 0), ("met_confirmed", 2))
        self.assertEqual(streak_verdict(mk([8, 7]), meets, 2, 500), ("met_unconfirmed", 1))
        self.assertEqual(streak_verdict(mk([8, 30, 7]), meets, 2, 0), ("met_unconfirmed", 1))
        self.assertEqual(streak_verdict(mk([30, 40]), meets, 2, 0), ("not_met", 0))
        v = SeoVisibility().evaluate_target({"key": "k", "max_position": 3}, mk([3, 30, 4, 28]), 2, 0)
        self.assertTrue(v["volatile"], "positions jumping between 3 and 30 are reported as volatile, not as progress")
        self.assertFalse(SeoVisibility().evaluate_target({"key": "k"}, mk([5, 6, 5, 7]), 2, 0)["volatile"])
        self.assertTrue(SeoVisibility().evaluate_target({"key": "k"}, mk([3, None, 4, None]), 2, 0)["volatile"], "flipping between present and absent is volatility")
        self.assertFalse(SeoVisibility().evaluate_target({"key": "k"}, mk([None, 7, 6]), 2, 0)["volatile"], "one move from absent to present is an improvement, not volatility")


# =====================================================================================================================  unavailable is not negative
class DataHonestyTests(LoopCase):
    async def test_unavailable_data_is_history_but_never_a_result_and_a_negative_result_is_kept_as_one(self):
        SCRIPT["values"] = [None, "unavailable", 11]
        lid = await self.start()
        d = await self.settle(lid)
        base = d["latest_observations"][0]
        self.assertEqual((base["status"], base["value"], base["comparable"]), ("not_found", None, True), "measured absence is a (negative) result")
        await self.plan(lid, [])        # a wait-only round needs wait_only
        p = await self.plan(lid, [], wait_only=True)
        self.assertEqual(p["status"], "succeeded", p)
        self.clock.t += 3601
        await self.ctrl.tick()
        d = await self.settle(lid)
        self.assertEqual((d["phase"], d["retry_at"] is not None), ("measuring", True), "an unavailable measurement is retried later, not evaluated")
        self.assertEqual(d["blockers"], [])
        statuses = [o["status"] for o in self.ctrl.db.observations(lid)]
        self.assertEqual(statuses, ["not_found", "unavailable"])
        self.assertEqual(d["goal_progress"]["targets"][0]["observations"], 1, "only the measured observation counts")
        self.clock.t += 6
        d = await self.settle(lid)
        self.assertEqual(d["phase"], "awaiting_plan")
        row = d["for_planning"]["comparison"]["rows"][0]
        self.assertEqual((row["change"], row["now"]["value"]), ("improved", 11.0))
        self.assertIn("now present", row["note"])
        self.assertEqual(d["goal_progress"]["status"], "not_met")

    async def test_no_usable_data_after_the_retries_blocks_the_loop_visibly_and_it_resumes(self):
        SCRIPT["values"] = ["unavailable", "fail", "unavailable", 30]
        lid = await self.start()
        d = await self.settle(lid)
        self.assertEqual((d["phase"], d["status"]), ("measuring", "active"))
        for _ in range(2):
            self.clock.t += 20
            d = await self.settle(lid)
        self.assertEqual(d["status"], "blocked")
        self.assertEqual(d["blockers"][0]["code"], "NO_DATA")
        self.assertIn("browser", d["blockers"][0]["message"] + " resume")
        self.assertEqual(COUNTS["metric"], 3)
        self.assertEqual(await self.ctrl.tick(), 0, "a blocked loop is not retried by the scheduler behind the owner's back")
        r = await self.c.run("loop.control", {"loop_id": lid, "action": "resume", "reason": "browser reconnected"}, client=self.cid)
        self.assertEqual(r["status"], "succeeded", r)
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["phase"], d["blockers"]), ("active", "awaiting_plan", []))


# =====================================================================================================================  scheduling, restarts, cancellation, retries
class ReliabilityTests(LoopCase):
    async def test_a_waiting_loop_survives_a_restart_and_resumes_when_due(self):
        SCRIPT["values"] = [25, 8]
        lid = await self.start()
        await self.settle(lid)
        await self.plan(lid)
        r = await self.report(lid, [self.make_change()])
        self.assertEqual(r["result"]["loop"]["phase"], "waiting")
        await self.restart_controller()
        self.assertEqual(await self.svc.loops.tick(), 0)
        self.clock.t += 3601
        self.assertEqual(await self.svc.loops.tick(), 1)
        d = await self.settle(lid)
        self.assertEqual((d["phase"], d["goal_progress"]["status"]), ("waiting", "met_unconfirmed"))

    async def test_the_real_background_ticker_resumes_a_due_loop_without_any_call(self):
        SCRIPT["values"] = [25, 12]
        lid = await self.start()
        await self.settle(lid)
        await self.plan(lid)
        await self.report(lid, [self.make_change()])
        self.clock.t += 3601
        await self.ctrl.stop_ticker()          # the service's own (idle, 1 h) ticker out of the way; then the real scheduler loop at a fast pace
        self.ctrl.start(0.05)
        try:
            end = time.time() + 10
            while time.time() < end and self.ctrl.describe(lid)["phase"] != "awaiting_plan":
                await asyncio.sleep(0.1)
        finally:
            await self.ctrl.stop_ticker()
        d = self.ctrl.describe(lid)
        self.assertEqual((d["phase"], d["round"], COUNTS["metric"]), ("awaiting_plan", 1, 2))

    async def test_a_measurement_in_flight_when_the_controller_restarts_completes_once(self):
        SCRIPT["values"] = [25]
        lid = await self.start(config={"sleep": 0.6})
        self.assertEqual(self.ctrl.describe(lid)["phase"], "measuring")
        await self.restart_controller()
        d = await self.settle(lid, timeout=15)
        self.assertEqual((d["phase"], COUNTS["metric"]), ("awaiting_plan", 1), "the pending job was picked up again, never re-submitted")

    async def test_retries_are_bounded_backed_off_and_never_duplicate_a_request(self):
        SCRIPT["values"] = ["fail", 20]
        lid = await self.start()
        d = await self.settle(lid)
        self.assertEqual((d["phase"], d["retry_at"] is not None), ("measuring", True))
        jobs = lambda: [j for j in self.svc.store.jobs(task_id=d["task_id"], limit=50) if j["capability"] == "test.metric"]
        self.assertEqual(len(jobs()), 1)
        for _ in range(5):
            await self.ctrl.advance(lid)                 # repeated calls before the retry time submit nothing
        self.assertEqual(len(jobs()), 1)
        self.clock.t += 6
        d = await self.settle(lid)
        self.assertEqual((len(jobs()), d["phase"]), (2, "awaiting_plan"))
        for _ in range(3):
            await self.ctrl.advance(lid)
        self.assertEqual(len(jobs()), 2, "an evaluated measurement is not repeated")
        first = await self.plan(lid)
        again = await self.plan(lid)
        self.assertEqual((first["status"], again["status"], again["error"]["code"]), ("succeeded", "failed", "NOT_AWAITING_PLAN"))

    async def test_stop_cancels_a_running_measurement_and_ends_the_loop_for_good(self):
        SCRIPT["values"] = [25]
        lid = await self.start(config={"sleep": 3})
        await asyncio.sleep(0.2)
        r = await self.c.run("loop.control", {"loop_id": lid, "action": "stop", "reason": "owner changed their mind"}, client=self.cid)
        self.assertEqual(r["result"]["loop"]["status"], "stopped")
        d = self.ctrl.describe(lid)
        jobs = [j for j in self.svc.store.jobs(task_id=d["task_id"], limit=20) if j["capability"] == "test.metric"]
        end = time.time() + 5
        while time.time() < end and self.svc.store.get_job(jobs[0]["request_id"])["status"] in ("queued", "running"):
            await asyncio.sleep(0.1)
        self.assertEqual(self.svc.store.get_job(jobs[0]["request_id"])["status"], "cancelled")
        self.assertEqual((d["status"], d["outcome"], d["next_action"]["what"]), ("stopped", "manual", "finished"))
        for cap, params in (("loop.submit_plan", {"items": [ITEM], "summary": "some summary text"}), ("loop.control", {"action": "stop"}), ("loop.report_implemented", {"change_ids": ["x"]})):
            r = await self.c.run(cap, {"loop_id": lid, **params}, client=self.cid)
            self.assertEqual(r["status"], "failed", cap)
        self.assertEqual(await self.ctrl.tick(), 0)

    async def test_pause_holds_everything_until_resumed(self):
        SCRIPT["values"] = [25, 12]
        lid = await self.start()
        await self.settle(lid)
        await self.plan(lid)
        await self.report(lid, [self.make_change()])
        await self.c.run("loop.control", {"loop_id": lid, "action": "pause", "reason": "holiday"}, client=self.cid)
        self.clock.t += 7200
        self.assertEqual(await self.ctrl.tick(), 0)
        self.assertEqual((await self.ctrl.advance(lid))["status"], "paused")
        self.assertEqual(COUNTS["metric"], 1)
        await self.c.run("loop.control", {"loop_id": lid, "action": "resume"}, client=self.cid)
        self.assertEqual(await self.ctrl.tick(), 1)
        d = await self.settle(lid)
        self.assertEqual(d["phase"], "awaiting_plan")

    async def test_a_disabled_creator_blocks_the_loop_instead_of_the_loop_using_rights_it_no_longer_has(self):
        SCRIPT["values"] = [25, 12]
        lid = await self.start()
        await self.settle(lid)
        await self.plan(lid)
        await self.report(lid, [self.make_change()])
        self.svc.callers.disable("collaborator")
        self.clock.t += 3601
        await self.ctrl.tick()
        d = self.ctrl.describe(lid)
        self.assertEqual((d["status"], d["blockers"][0]["code"]), ("blocked", "CALLER_DISABLED"))
        self.svc.callers.disable("collaborator", False)
        d = await self.ctrl.control(lid, "resume", actor="owner:test")
        d = await self.settle(lid)
        self.assertEqual(d["phase"], "awaiting_plan")


# =====================================================================================================================  stop conditions
class StopConditionTests(LoopCase):
    async def test_max_rounds_ends_the_loop_honestly_without_claiming_success(self):
        SCRIPT["values"] = [30, 30, 30]
        lid = await self.start(goal={"max_rounds": 2})
        await self.settle(lid)
        d = await self.round_trip(lid, ITEM)
        self.assertEqual(d["status"], "active")
        d = await self.round_trip(lid, ITEM2)
        self.assertEqual((d["status"], d["outcome"]), ("exhausted", "max_rounds"))
        self.assertIn("without a confirmed result", d["summary"])

    async def test_a_deadline_ends_a_waiting_loop_by_itself(self):
        SCRIPT["values"] = [30]
        lid = await self.start(goal={"deadline": self.clock.t + 5000, "max_rounds": None})
        await self.settle(lid)
        await self.plan(lid)
        await self.report(lid, [self.make_change()], )
        self.clock.t += 6000
        self.assertEqual(await self.ctrl.tick(), 1)
        d = self.ctrl.describe(lid)
        self.assertEqual((d["status"], d["outcome"]), ("exhausted", "deadline"))
        self.assertEqual(COUNTS["metric"], 1, "no measurement was started after the deadline")

    async def test_the_request_budget_is_enforced_before_a_measurement_is_started(self):
        SCRIPT["values"] = [30, 30]
        lid = await self.start(goal={"max_requests": 2, "max_rounds": None})
        await self.settle(lid)
        d = await self.round_trip(lid, ITEM)
        self.assertEqual((d["status"], d["requests_used"]), ("active", 2))
        await self.plan(lid, [ITEM2])
        await self.report(lid, [self.make_change()])
        self.clock.t += 3601
        await self.ctrl.tick()
        d = self.ctrl.describe(lid)
        self.assertEqual((d["status"], d["outcome"], COUNTS["metric"]), ("exhausted", "budget", 2))

    async def test_a_loop_must_have_a_way_to_end_and_a_future_deadline(self):
        for goal, code in (({"targets": [{"keyword": "a"}]}, "BAD_LOOP"), ({"max_rounds": 0}, "BAD_LOOP"), ({"max_rounds": 2, "deadline": "2001-01-01"}, "BAD_LOOP"), ({"max_rounds": 2, "deadline": "not a date"}, "BAD_GOAL")):
            r = await self.c.run("loop.create", {"kind": "test_script", "title": "bad loop", "goal": goal}, client=self.cid)
            self.assertEqual((r["status"], r["error"]["code"]), ("failed", code), goal)
        self.assertEqual(self.ctrl.db.list(), [])


# =====================================================================================================================  no repeated or unnecessary changes
class PlanningRulesTests(LoopCase):
    async def test_a_change_that_was_already_made_is_not_repeated_without_a_reason(self):
        SCRIPT["values"] = [25, 25, 25]
        lid = await self.start()
        await self.settle(lid)
        d = await self.round_trip(lid, ITEM)
        self.assertEqual(d["rounds"][1]["item_effects"][fingerprint(ITEM)], "no_improvement")
        self.assertEqual(d["for_planning"]["previous_changes"][0]["effect"], "no_improvement")
        r = await self.plan(lid, [ITEM])
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "REPEAT_CHANGE"))
        self.assertEqual(r["error"]["details"]["repeats"][0]["effect"], "no_improvement")
        r = await self.plan(lid, [ITEM], override_reason="the first attempt was deployed to the wrong template")
        self.assertEqual(r["status"], "succeeded", r)
        self.assertTrue(self.ctrl.describe(lid)["rounds"][-1]["plan"]["repeats"])
        same_pages_other_order = dict(ITEM, pages=["HTTPS://x.test/drains"])
        self.assertEqual(fingerprint(same_pages_other_order), fingerprint(ITEM), "the same change is recognised through trivial differences")

    async def test_empty_plans_and_endless_waiting_are_refused(self):
        SCRIPT["values"] = [25, 25, 25, 25]
        lid = await self.start(config={"max_waits": 1})
        await self.settle(lid)
        r = await self.plan(lid, [])
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "BAD_PLAN"))
        r = await self.plan(lid, [ITEM], wait_only=True)
        self.assertEqual(r["error"]["code"], "BAD_PLAN")
        self.assertEqual((await self.plan(lid, [], wait_only=True))["status"], "succeeded")
        self.clock.t += 3601
        await self.ctrl.tick()
        d = await self.settle(lid)
        self.assertEqual(d["phase"], "awaiting_plan")
        r = await self.plan(lid, [], wait_only=True)
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "TOO_MANY_WAITS"))
        r = await self.plan(lid, [{"title": "no", "action_kind": ""}])
        self.assertEqual(r["error"]["code"], "BAD_PLAN")


# =====================================================================================================================  approvals are never bypassed
class ApprovalTests(LoopCase):
    async def test_only_deployed_changes_with_an_executed_owner_approval_count_as_implemented(self):
        SCRIPT["values"] = [25, 12]
        lid = await self.start()
        await self.settle(lid)
        await self.plan(lid)
        for kw, code in (({"status": "tested"}, "CHANGE_NOT_DEPLOYED"), ({"status": "ready_to_deploy"}, "CHANGE_NOT_DEPLOYED"), ({"approval": "proposed"}, "APPROVAL_REQUIRED"),
                         ({"approval": "approved"}, "APPROVAL_REQUIRED"), ({"approval": None}, "APPROVAL_REQUIRED")):
            r = await self.report(lid, [self.make_change(**kw)])
            self.assertEqual((r["status"], r["error"]["code"]), ("failed", code), kw)
        self.assertEqual(self.ctrl.describe(lid)["phase"], "awaiting_implementation", "nothing was counted")
        other = await self.new_client("Beta Roofing LLC")
        r = await self.report(lid, [self.make_change(cid=other)])
        self.assertEqual(r["error"]["code"], "NO_SUCH_CHANGE", "a change of another client cannot be used")
        good = self.make_change()
        r = await self.report(lid, [good])
        self.assertEqual(r["status"], "succeeded", r)
        self.assertEqual(self.ctrl.describe(lid)["phase"], "waiting")

    async def test_a_change_cannot_be_counted_twice_and_staging_changes_need_no_production_approval(self):
        SCRIPT["values"] = [25, 25, 25]
        lid = await self.start()
        await self.settle(lid)
        ch = self.make_change()
        await self.plan(lid)
        await self.report(lid, [ch])
        self.clock.t += 3601
        await self.ctrl.tick()
        await self.settle(lid)
        await self.plan(lid, [ITEM2])
        r = await self.report(lid, [ch])
        self.assertEqual(r["error"]["code"], "CHANGE_ALREADY_REPORTED")
        r = await self.report(lid, [self.make_change(production=False, approval=None)])
        self.assertEqual(r["status"], "succeeded", r)

    async def test_the_collaborator_has_no_way_to_approve_through_loops(self):
        from octg_agent.collab.capabilities import REGISTRY
        self.assertEqual([n for n in REGISTRY if n.startswith("loop.") and ("approve" in n or "deploy" in n)], [])
        for n, c in REGISTRY.items():
            if n.startswith("loop."):
                self.assertFalse(c.external_effects, n)


# =====================================================================================================================  isolation
class IsolationTests(LoopCase):
    async def test_loops_are_scoped_to_one_client_and_to_the_caller_that_created_them(self):
        SCRIPT["values"] = [25, 25]
        lid = await self.start()
        other = await self.new_client("Beta Roofing LLC")
        r = await self.c.run("loop.get", {"loop_id": lid}, client=other)
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "NO_SUCH_LOOP"))
        for cap, params in (("loop.advance", {}), ("loop.control", {"action": "stop"}), ("loop.submit_plan", {"items": [ITEM], "summary": "some summary text"}), ("loop.report_implemented", {"change_ids": ["x"]})):
            r = await self.c.run(cap, {"loop_id": lid, **params}, client=other)
            self.assertEqual(r["error"]["code"], "NO_SUCH_LOOP", cap)
        self.assertEqual((await self.c.run("loop.list", {}, client=other))["result"]["loops"], [])
        mine = (await self.c.run("loop.list", {}, client=self.cid))["result"]["loops"]
        self.assertEqual([x["loop_id"] for x in mine], [lid])
        SCRIPT["values"] = [30]
        b = await self.create(client=other)
        self.assertEqual(b["status"], "succeeded", b)
        self.assertNotEqual(b["result"]["loop"]["loop_id"], lid)
        self.assertNotEqual(b["result"]["loop"]["task_id"], self.ctrl.describe(lid)["task_id"])
        self.assertEqual(b["result"]["loop"]["client_id"], other)
        # a second collaborator identity cannot see or touch the first one's loop either
        tok = self.svc.callers.rotate("other-ai", tuple(self.svc.callers.list()[0]["grants"]))
        rival = Collab(self.h, token=tok)
        r = await rival.run("loop.get", {"loop_id": lid}, client=self.cid)
        self.assertEqual(r["error"]["code"], "NO_SUCH_LOOP")
        r = await rival.run("loop.control", {"loop_id": lid, "action": "stop"}, client=self.cid)
        self.assertEqual(r["error"]["code"], "NO_SUCH_LOOP")
        self.assertEqual(self.ctrl.describe(lid)["status"], "active")
        # the loop's own measurement requests carry its client
        jobs = self.svc.store.jobs(task_id=self.ctrl.describe(lid)["task_id"], limit=20)
        self.assertTrue(jobs and all(j["client_id"] == self.cid for j in jobs))

    async def test_a_caller_cannot_start_a_loop_whose_requests_it_is_not_allowed_to_make(self):
        grants = tuple(g for g in self.svc.callers.list()[0]["grants"] if g != "test:internal")
        self.svc.callers.set_grants("collaborator", grants)
        r = await self.create()
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "PERMISSION_DENIED"))
        r = await self.c.run("loop.kinds", {})
        self.assertEqual(r["status"], "succeeded")
        self.assertIn("seo_visibility", [k["name"] for k in r["result"]["kinds"]])
        self.assertEqual(self.ctrl.db.list(), [])


# =====================================================================================================================  the SEO kind reads search data honestly
class SeoObservationTests(unittest.TestCase):
    def loop(self):
        return {"loop_id": "lp_x", "config": {"engine": "google", "country": "us", "language": "en", "location": "", "campaign_id": "c1", "_kw_locations": {"drain cleaning": "Springfield, IL", "water heater": "Springfield, IL"}},
                "goal": {"targets": [{"keyword": "drain cleaning", "location": "Springfield, IL", "max_position": 10}, {"keyword": "water heater", "location": "Springfield, IL", "max_position": 10}]}}

    def resp(self, status, rows=None, code=""):
        return {"capability": "seo.serp_observe", "status": status, "request_id": "rq_1", "completed_at": 1000.0, "result": {"keywords": rows or []}, "error": {"code": code} if code else None, "evidence": []}

    def test_found_absent_blocked_and_unreadable_are_four_different_things(self):
        rows = [{"keyword": "drain cleaning", "location": "Springfield, IL", "observed_at": 1.0, "client_position": 4, "top": [1] * 10, "blocked": None},
                {"keyword": "water heater", "location": "Springfield, IL", "observed_at": 2.0, "client_position": None, "top": [1] * 10, "blocked": None},
                {"keyword": "boiler", "location": "Springfield, IL", "observed_at": 3.0, "blocked": "CAPTCHA", "top": []},
                {"keyword": "furnace", "location": "Springfield, IL", "observed_at": 4.0, "error": "NO_RESULTS_PARSED", "top": []}]
        obs = {o["key"].split("|")[2]: o for o in SeoVisibility().observations(self.loop(), "verify", [self.resp("succeeded", rows)])}
        self.assertEqual((obs["drain cleaning"]["status"], obs["drain cleaning"]["value"]), ("found", 4))
        self.assertEqual((obs["water heater"]["status"], obs["water heater"]["comparable"]), ("not_found", True))
        self.assertIn("measured absence", obs["water heater"]["limits"][0])
        self.assertEqual((obs["boiler"]["status"], obs["boiler"]["comparable"]), ("unavailable", False))
        self.assertIn("CAPTCHA", obs["boiler"]["limits"][0])
        self.assertEqual(obs["furnace"]["status"], "unavailable")

    def test_a_failed_search_makes_every_target_unavailable_never_not_ranking(self):
        obs = SeoVisibility().observations(self.loop(), "verify", [self.resp("failed", code="EXTENSION_NOT_CONNECTED")])
        self.assertEqual({o["status"] for o in obs}, {"unavailable"})
        self.assertEqual(len(obs), 2)
        self.assertIn("EXTENSION_NOT_CONNECTED", obs[0]["limits"][0])

    def test_data_sources_report_what_is_not_connected(self):
        cs = {"capability": "connectors.status", "status": "succeeded", "result": {"sources": [
            {"source": "google_search_console", "connected": True, "missing": "", "client_mapping": {"mapped": False, "properties": [], "ready": False, "why_not": "no property is mapped to this client (owner: Hub)"}},
            {"source": "bing_webmaster_tools", "connected": False, "missing": "no credentials", "client_mapping": {"mapped": False, "properties": [], "ready": False, "why_not": "no credentials"}},
            {"source": "browser_search_results", "connected": True, "how": "browser"}]}}
        src = {s["source"]: s for s in SeoVisibility().sources(self.loop(), [cs])}
        self.assertFalse(src["google_search_console"]["connected"], "credentials alone are not enough: the property must be mapped to THIS client")
        self.assertIn("no property is mapped", src["google_search_console"]["detail"])
        self.assertFalse(src["bing_webmaster_tools"]["connected"])
        self.assertEqual(src["bing_webmaster_tools"]["detail"], "no credentials")
        self.assertTrue(src["browser_search_results"]["connected"])
        failed = {"capability": "gsc.search_analytics", "status": "failed", "error": {"code": "RATE_LIMITED"}}
        cs["result"]["sources"][0]["client_mapping"] = {"mapped": True, "properties": ["sc-domain:x.test"], "ready": True, "why_not": ""}
        src = {s["source"]: s for s in SeoVisibility().sources(self.loop(), [cs, failed])}
        self.assertFalse(src["google_search_console"]["connected"])
        self.assertIn("RATE_LIMITED", src["google_search_console"]["detail"])


# =====================================================================================================================  the Hub's view and controls
class HubLoopTests(LoopCase):
    async def test_the_owner_sees_every_loop_and_can_pause_resume_and_stop_but_not_plan_or_approve(self):
        SCRIPT["values"] = [25, 25]
        lid = await self.start()
        await self.settle(lid)
        st, v = await self.hub.get("/loops")
        self.assertEqual(st, 200)
        self.assertEqual([(x["loop_id"], x["client_name"], x["status"]) for x in v["loops"]], [(lid, "Alpha Plumbing LLC", "active")])
        st, d = await self.hub.get(f"/loops/{lid}")
        self.assertEqual((st, d["loop"]["client_name"], bool(d["loop"]["events"]), bool(d["loop"]["observations"])), (200, "Alpha Plumbing LLC", True, True))
        st, d = await self.hub.get("/loops/lp_nope")
        self.assertEqual((st, d["code"]), (404, "NO_SUCH_LOOP"))
        st, d = await self.hub.post(f"/loops/{lid}/pause", {"reason": "weekend"})
        self.assertEqual((st, d["loop"]["status"]), (200, "paused"))
        st, d = await self.hub.post(f"/loops/{lid}/resume")
        self.assertEqual((st, d["loop"]["status"]), (200, "active"))
        st, d = await self.hub.post(f"/loops/{lid}/stop", {"reason": "not needed"})
        self.assertEqual((st, d["loop"]["status"], d["loop"]["outcome"]), (200, "stopped", "manual"))
        st, d = await self.hub.post(f"/loops/{lid}/approve")
        self.assertEqual(st, 404, "there is no approve route for loops")
        anon = type(self.hub)(self.h)
        st, d = await anon.get("/loops")
        await anon.close()
        self.assertEqual(st, 401)
        types = [e["event"] for e in self.ctrl.db.events(lid)]
        self.assertIn("stopped", types)


# =====================================================================================================================  the complete SEO loop: real controller + real capabilities, mocked search pages / website / time
def serp_world(client_href: str = "https://www.alpha.test/drains/"):
    """A scripted fake of the owner's Chrome extension showing Google results pages. `rank[query]` is where the client appears (None/absent -> not among the 10 results read)."""
    world = {"rank": {}, "blocked": set(), "next": 900, "tabs": {}}

    def q_of(url):
        return parse_qs(urlsplit(url or "").query).get("q", [""])[0].lower()

    def rows(tid):
        rank = world["rank"].get(q_of(world["tabs"][tid]))
        out = []
        for pos in range(1, 11):
            if rank == pos:
                out.append(("Alpha Plumbing LLC | Drain cleaning Springfield", "alpha.test", client_href))
            else:
                out.append((f"Competitor {pos} plumbing and drains in Springfield", f"rival{pos}.test", f"https://www.rival{pos}.test/services"))
        return out

    def new_tab(p, cmd):
        world["next"] += 1
        world["tabs"][world["next"]] = p.get("url")
        return {"result": {"tab": {"tab_id": world["next"], "url": p.get("url"), "status": "complete"}}}

    def is_serp(tid):
        return "/search?" in (world["tabs"].get(tid) or "")

    def page_state(p, cmd):
        tid = p["tab_id"]
        if not is_serp(tid):   # an ordinary page (a competitor's site read in the browser)
            host = urlsplit(world["tabs"][tid]).hostname or "site"
            return {"result": {"tab": {}, "page": {"url": world["tabs"][tid], "title": f"{host} | Springfield plumbing and drains", "headings": [{"level": 1, "text": "Springfield plumbers"}, {"level": 2, "text": "Drain cleaning"}]}}}
        heads = [] if q_of(world["tabs"][tid]) in world["blocked"] else [{"level": 3, "text": t} for t, _, _ in rows(tid)]
        return {"result": {"tab": {}, "page": {"url": world["tabs"][tid], "title": "results", "headings": heads}}}

    def navigate(p, cmd):
        world["tabs"][p["tab_id"]] = p.get("url")
        return {"result": {"tab": {"tab_id": p["tab_id"], "url": p.get("url"), "status": "complete"}}}

    def read(p, cmd):
        tid = p["tab_id"]
        if not is_serp(tid):
            return {"result": {"text": "Springfield plumbers. Drain cleaning and repair. Call now.", "url": world["tabs"][tid], "title": "t", "total_chars": 55}}
        if q_of(world["tabs"][tid]) in world["blocked"]:
            return {"result": {"text": "Our systems have detected unusual traffic from your computer network", "url": "https://www.google.com/sorry/index?continue=x", "title": "sorry", "total_chars": 60}}
        return {"result": {"text": "results text", "url": world["tabs"][tid], "title": "t", "total_chars": 12}}

    def inspect(p, cmd):
        tid = p["tab_id"]
        if not is_serp(tid):
            u = world["tabs"][tid]
            return {"result": {"snapshot_id": "s1", "tab_id": tid, "url": u, "elements": [{"id": "el_001", "tag": "a", "name": "Contact", "href": u.rstrip("/") + "/contact"}], "page": {"headings": []}}}
        els = [] if q_of(world["tabs"][tid]) in world["blocked"] else [{"tag": "a", "name": f"{t} https://{d}", "href": h} for t, d, h in rows(tid)]
        return {"result": {"snapshot_id": "s1", "tab_id": tid, "url": world["tabs"][tid], "elements": [{"id": f"el_{i:03d}", **e} for i, e in enumerate(els, 1)], "page": {"headings": []}}}

    return world, {"new_tab": new_tab, "navigate": navigate, "get_page_state": page_state, "read_page": read, "inspect_page": inspect, "close_tab": lambda p, c: {"result": {"closed": True}}, "wait": lambda p, c: {"result": {}}}


class SeoLoopDemo(CollabCase):
    DRAIN = "drain cleaning springfield, il"
    HEATER = "water heater install springfield, il"
    KEY = "google|organic|drain cleaning|springfield, il"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        HITS.clear()
        self.clock = Clock()
        self.svc.loops.clock = self.clock
        self.site = TestServer(fixture_site(), host="127.0.0.1")
        await self.site.start_server()
        self.url = f"http://127.0.0.1:{self.site.port}/"
        self.patches = [mock.patch.object(F, "_is_public", lambda ip: True), mock.patch.object(F, "ALLOWED_PORTS", {None, 80, 443, self.site.port})]
        for p in self.patches:
            p.start()
        self.world, handlers = serp_world(client_href=self.url + "drains/")     # the client shows up under the fixture site's own address
        await self.h.extension(handlers=handlers)

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.site.close()
        await super().asyncTearDown()

    async def run_cap(self, cap, params=None, **kw):
        r = await self.c.run(cap, params or {}, wait=90, **kw)
        self.assertEqual(r["status"], "succeeded", (cap, r.get("error")))
        return r["result"]

    async def settle(self, lid, timeout=60.0):
        end = time.time() + timeout
        while True:
            d = await self.svc.loops.advance(lid)
            if d["phase"] != "measuring" or d["status"] != "active" or d["retry_at"] or time.time() > end:
                return d
            await asyncio.sleep(0.1)

    async def test_one_complete_seo_loop_from_baseline_to_a_confirmed_first_page(self):
        say = lambda msg: print(f"  [loop demo] {msg}")
        cid = await self.new_client("Alpha Plumbing LLC", website=self.url, phone="(555) 201-3344", address={"city": "Springfield", "state": "IL"})
        # ---- setup through the collaboration layer (real capabilities)
        await self.run_cap("seo.campaign_upsert", {"objective": "more drain-cleaning calls", "locations": ["Springfield, IL"],
                                                   "keywords": ["drain cleaning | Springfield, IL", "water heater install | Springfield, IL"]}, client=cid)
        site_dir = self.h.tmp / "site"
        (site_dir / "drains").mkdir(parents=True)
        (site_dir / "drains" / "index.html").write_text("<h1>x</h1>")
        await self.run_cap("target.register", {"domain": f"127.0.0.1:{self.site.port}", "environment": "production", "local_path": str(site_dir), "deploy_method": "test fixture"}, client=cid)
        self.world["rank"] = {self.DRAIN: 14, self.HEATER: None}      # MOCKED: the client is not on page one for either keyword
        # ---- 1. baseline
        goal = {"targets": [{"keyword": "drain cleaning", "location": "Springfield, IL", "max_position": 10}], "confirmations": 2, "max_rounds": 4}
        cfg = {"keywords": ["water heater install"], "min_gap_hours": 0, "recheck_after_hours": 24, "confirm_gap_hours": 12, "max_pages": 10}
        created = await self.run_cap("loop.create", {"kind": "seo_visibility", "title": "Drain cleaning to page one", "config": cfg, "goal": goal}, client=cid)
        lid = created["loop"]["loop_id"]
        d = await self.settle(lid)
        say(f"baseline recorded: phase={d['phase']} goal={d['goal_progress']['status']}; observations={[(o['label'], o['status']) for o in d['latest_observations'] if not o['key'].startswith('audit')]}")
        self.assertEqual((d["phase"], d["goal_progress"]["status"]), ("awaiting_plan", "not_met"))
        obs = {o["key"]: o for o in d["latest_observations"]}
        self.assertEqual(obs[self.KEY]["status"], "not_found")
        self.assertEqual(obs[self.KEY]["comparable"], True)
        self.assertGreater(obs["audit|findings_total"]["value"], 0)
        src = {s["source"]: s["connected"] for s in d["sources"]}
        self.assertEqual((src["google_search_console"], src["bing_webmaster_tools"], src["browser_search_results"]), (False, False, True), "unconnected sources are reported, not faked")
        brief = d["for_planning"]["brief"]
        self.assertTrue(brief["audit_artifact_ids"])
        # ---- 2. analyse (the collaborator reads the audit it was pointed to) and PLAN
        findings = (await self.run_cap("artifact.get", {"artifact_id": brief["audit_artifact_ids"][0]}, client=cid))["artifact"]["body"]["findings"]
        h1 = next(f for f in findings if f["rule"] == "h1_multiple")
        item = {"title": "Reduce the drains page to a single H1", "action_kind": "heading_structure", "pages": [self.url + "drains/"], "finding_key": h1["rule"], "targets": [self.KEY],
                "hypothesis": "the page has two H1s, which dilutes the topic signal", "expected_effect": "clearer topical relevance for drain cleaning"}
        plan = await self.run_cap("loop.submit_plan", {"loop_id": lid, "items": [item], "summary": "Baseline: not on page one; the audit confirms two H1s on the drains page."}, client=cid)
        self.assertEqual(plan["loop"]["phase"], "awaiting_implementation")
        # ---- 3. implement through the existing workflow, with the owner's PIN approval
        _, t = await self.c.call("POST", "/api/collab/tasks", {"title": "implement round 1", "client": cid})
        tid = t["task"]["task_id"]
        hd = await self.run_cap("handoff.create", {"finding_ids": [h1["finding_id"]]}, client=cid, task_id=tid)
        ch = await self.run_cap("change.record", {"handoff_id": hd["handoff_id"], "summary": "demote the second H1", "files": ["drains/index.html"], "tests": [{"name": "local render", "passed": True}],
                                                  "status": "ready_to_deploy"}, client=cid, task_id=tid)
        early = await self.c.run("loop.report_implemented", {"loop_id": lid, "change_ids": [ch["change_id"]]}, client=cid)
        self.assertEqual(early["error"]["code"], "CHANGE_NOT_DEPLOYED", "reporting before deploying is refused")
        ap = await self.run_cap("approval.request", {"action_kind": "production_deploy", "title": "Deploy the H1 fix", "artifact_id": ch["change_id"]}, client=cid, task_id=tid)
        denied = await self.c.run("change.mark_deployed", {"change_id": ch["change_id"], "approval_id": ap["approval_id"], "operation": "deploy", "succeeded": True}, client=cid, task_id=tid)
        self.assertEqual(denied["error"]["code"], "APPROVAL_REQUIRED")
        HubPin(self.h.settings.data_dir).set("4711")
        st, x = await self.hub.post(f"/approvals/{ap['approval_id']}/approve", {})
        self.assertEqual((st, x["code"]), (401, "BAD_PIN"), "the owner's PIN is required to approve")
        st, x = await self.hub.post(f"/approvals/{ap['approval_id']}/approve", {"pin": "4711", "note": "ok"})
        self.assertEqual(st, 200, x)
        dep = await self.run_cap("change.mark_deployed", {"change_id": ch["change_id"], "approval_id": ap["approval_id"], "operation": "deploy to the fixture site", "succeeded": True}, client=cid, task_id=tid)
        self.assertEqual(dep["approval"]["status"], "executed")
        ver = await self.run_cap("verify.page", {"url": self.url + "drains/", "checks": [{"type": "status", "value": 200}], "change_id": ch["change_id"]}, client=cid, task_id=tid)
        self.assertTrue(ver["passed"])
        rep = await self.run_cap("loop.report_implemented", {"loop_id": lid, "change_ids": [ch["change_id"]], "note": "H1 demoted"}, client=cid)
        self.assertEqual(rep["loop"]["phase"], "waiting")
        say("round 1 implemented (owner PIN approval, deploy recorded, page verified); the loop now waits 24 h with nothing running")
        jobs_before = len(self.svc.store.jobs(task_id=rep["loop"]["task_id"], limit=100))
        self.assertEqual(await self.svc.loops.tick(), 0)
        self.assertEqual(len(self.svc.store.jobs(task_id=rep["loop"]["task_id"], limit=100)), jobs_before, "no research while waiting")
        # ---- 4. time passes (MOCKED), the effect appears (MOCKED), the loop re-measures by itself
        self.clock.t += 25 * 3600
        self.world["rank"][self.DRAIN] = 7
        self.assertEqual(await self.svc.loops.tick(), 1)
        d = await self.settle(lid)
        row = next(r for r in self.svc.loops.describe(lid)["rounds"] if r["n"] == 1)
        say(f"re-measured after the wait: goal={d['goal_progress']['status']}; round-1 comparison={row['comparison_summary']}")
        self.assertEqual((d["status"], d["phase"], d["goal_progress"]["status"]), ("active", "waiting", "met_unconfirmed"), "page one once is promising, not proof")
        comp = [c for c in self.svc.loops.describe(lid, full=True)["comparisons"] if c["round"] == 1][0]
        drain = next(r for r in comp["rows"] if r["key"] == self.KEY)
        self.assertEqual((drain["baseline"]["status"], drain["now"]["status"], drain["now"]["value"], drain["change"]), ("not_found", "found", 7.0, "improved"))
        heater = next(r for r in comp["rows"] if r["key"].endswith("water heater install|springfield, il"))
        self.assertEqual(heater["change"], "same", "a keyword that did not move is reported as unchanged")
        self.assertEqual(d["rounds"][1]["item_effects"][fingerprint(item)], "improved")
        # ---- 5. confirmation after the gap (MOCKED time), then the loop ends by itself
        self.clock.t += 13 * 3600
        self.world["rank"][self.DRAIN] = 6
        self.assertEqual(await self.svc.loops.tick(), 1)
        d = await self.settle(lid)
        self.assertEqual((d["status"], d["outcome"]), ("goal_reached", "goal"), d["summary"])
        say(f"loop finished: {d['status']} - {d['summary']}")
        # ---- 6. the record
        full = self.svc.loops.describe(lid, full=True)
        events = [e["event"] for e in full["events"]]
        for needed in ("created", "measured", "evaluated", "awaiting_plan", "plan_submitted", "implemented", "confirmation_scheduled", "goal_reached"):
            self.assertIn(needed, events)
        self.assertEqual(HITS, [], "the crawler never requested a risky path")
        self.assertEqual(self.svc.store.get_approval(ap["approval_id"])["status"], "verified")
        st, v = await self.hub.get("/loops")
        self.assertEqual([(x["loop_id"], x["status"], x["client_name"]) for x in v["loops"]], [(lid, "goal_reached", "Alpha Plumbing LLC")])
        st, one = await self.hub.get(f"/loops/{lid}")
        self.assertEqual(st, 200)
        self.assertTrue(one["loop"]["events"] and one["loop"]["comparisons"])
        self.assertNotIn("(555) 201-3344", str(one), "client contact data is not copied into loop records")
        for key in ("google_search_console", "bing_webmaster_tools"):
            self.assertFalse(next(s for s in one["loop"]["sources"] if s["source"] == key)["connected"])
        say("the whole loop is in the history: baseline, research evidence, plan, approval, deploy, verification, scheduled waits, comparisons and the outcome")


class PageChecksLoopTests(CollabCase):
    """The same controller with a completely different kind: keep a live page honest until its checks pass twice."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = Clock()
        self.svc.loops.clock = self.clock
        self.site = TestServer(fixture_site(), host="127.0.0.1")
        await self.site.start_server()
        self.url = f"http://127.0.0.1:{self.site.port}/"
        self.patches = [mock.patch.object(F, "_is_public", lambda ip: True), mock.patch.object(F, "ALLOWED_PORTS", {None, 80, 443, self.site.port})]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.site.close()
        await super().asyncTearDown()

    async def test_the_controller_is_not_seo_specific(self):
        cid = await self.new_client("Alpha Plumbing LLC")
        cfg = {"url": self.url + "drains/", "checks": [{"type": "h1_count", "value": 1}], "min_gap_hours": 0, "confirm_gap_hours": 1, "recheck_after_hours": 1}
        r = await self.c.run("loop.create", {"kind": "page_checks", "title": "single H1 on drains", "config": cfg, "goal": {"confirmations": 2, "max_rounds": 3}}, client=cid, wait=60)
        self.assertEqual(r["status"], "succeeded", r)
        lid = r["result"]["loop"]["loop_id"]
        end = time.time() + 30
        while time.time() < end:
            d = await self.svc.loops.advance(lid)
            if d["phase"] != "measuring":
                break
            await asyncio.sleep(0.1)
        self.assertEqual((d["phase"], d["latest_observations"][0]["value"], d["goal_progress"]["status"]), ("awaiting_plan", 1.0, "not_met"), "the page has two H1s: one failing check")
        self.assertEqual(d["for_planning"]["brief"], {})


if __name__ == "__main__":
    unittest.main()
