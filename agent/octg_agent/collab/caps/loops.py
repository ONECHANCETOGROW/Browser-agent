"""Loop controller capabilities: how the collaborator starts, reads, plans, reports on and controls a research-improve-verify loop for ONE client.

Every one of these works on exactly one client (explicit, server-checked) and only on loops that caller created. The loop itself uses the creator's own grants for every
measurement it requests, so a loop can never do more than its creator could do by hand, and nothing here approves anything.
"""
from __future__ import annotations

from .. import loop_kinds  # noqa: F401 - registers the loop kinds
from ..capabilities import A, B, I, O, S, capability
from ..loops import KINDS
from .core import require_client

LOOP_ID = S("Loop id (from loop.create).", minLength=1)


@capability("loop.kinds", "List the loop kinds this agent can run (what each measures, its config and goal) so a loop can be created without guessing.",
            output={"kinds": "[{name, title, description, permissions}]"}, permissions=("agent:read",), category="loops", timeout_s=10)
async def loop_kinds_cap(ctx, p: dict) -> dict:
    return {"kinds": [{"name": k.name, "title": k.title, "description": k.description, "permissions": list(k.permissions)} for k in KINDS.values()]}


@capability("loop.create", "Start a durable improvement loop for ONE client: it records a baseline now (research requested through this same layer), then waits for a plan, an implementation, "
            "a re-check date and a verification, round after round, until the goal is reliably reached or a stop condition (rounds, deadline, request budget, manual stop) ends it. "
            "Nothing runs or stays open while it waits; everything is stored and survives a restart.",
            props={"kind": S("Loop kind (loop.kinds), e.g. seo_visibility.", minLength=1), "title": S("Short name.", minLength=3, maxLength=200),
                   "config": O("Kind-specific settings (e.g. campaign_id, engine, location, recheck_after_hours, confirm_gap_hours, max_attempts)."),
                   "goal": O("Targets and stop conditions: targets[], confirmations, require, max_rounds, deadline (ISO date), max_requests. At least one of max_rounds / deadline / max_requests is required.")},
            required=("kind", "title", "goal"), output={"loop": "the loop description (status, phase, next_action, goal_progress, rounds, sources)"},
            permissions=("collab:write", "research:write", "client:read"), scope="client", read_only=False, category="loops", timeout_s=60)
async def loop_create(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    d = await ctx.svc.loops.create(ctx.caller, cid, p["kind"], p["title"], p.get("config"), p["goal"])
    ctx.source("loop_controller", "", loop=d["loop_id"])
    return {"loop": d}


@capability("loop.get", "Read one loop: status, phase, who must act next, goal progress per target (with reliability: confirmations needed, volatility), every round with its plan, changes and "
            "before/after comparison, latest observations, data-source status, blockers and the budget used. While it awaits a plan it includes everything needed to plan (for_planning).",
            props={"loop_id": LOOP_ID, "full": B("Also return the complete event history, all observations and all comparisons.")},
            required=("loop_id",), output={"loop": "description"}, permissions=("client:read",), scope="client", category="loops", timeout_s=20)
async def loop_get(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    ctx.svc.loops.own(p["loop_id"], client_id=cid, caller=ctx.caller)
    return {"loop": ctx.svc.loops.describe(p["loop_id"], full=bool(p.get("full")))}


@capability("loop.list", "List this client's loops that this caller created, newest first.",
            props={"status": S("Only loops in this status (active, paused, blocked, goal_reached, stopped, exhausted)."), "limit": I("Max rows (default 30).", minimum=1, maximum=200)},
            output={"loops": "[{loop_id, kind, title, status, phase, round, next_action, goal_progress}]"}, permissions=("client:read",), scope="client", category="loops", timeout_s=20)
async def loop_list(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    rows = [lp for lp in ctx.svc.loops.db.list(client_id=cid, status=p.get("status"), limit=p.get("limit", 30)) if lp["caller"] == ctx.caller]
    out = []
    for lp in rows:
        d = ctx.svc.loops.describe(lp["loop_id"])
        out.append({k: d[k] for k in ("loop_id", "kind", "title", "status", "phase", "round", "next_check_at", "next_action", "goal_progress", "outcome")})
    return {"loops": out}


@capability("loop.advance", "Do whatever is due for a loop right now (collect finished measurements, evaluate, start the next measurement). The scheduler does this by itself when a re-check date "
            "arrives; call it to check immediately. A blocked loop is retried only with force.",
            props={"loop_id": LOOP_ID, "force": B("Run the re-check now even though its date has not arrived (or retry a blocked loop).")},
            required=("loop_id",), output={"loop": "description"}, permissions=("research:write", "client:read"), scope="client", read_only=False, category="loops", timeout_s=60)
async def loop_advance(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    ctx.svc.loops.own(p["loop_id"], client_id=cid, caller=ctx.caller)
    return {"loop": await ctx.svc.loops.advance(p["loop_id"], force=bool(p.get("force")))}


ITEM = {"type": "object", "description": "{title, action_kind, pages?[], finding_key?, hypothesis?, expected_effect?, targets?[keys]}"}


@capability("loop.submit_plan", "Submit the plan for the next round once the evidence is in (phase awaiting_plan): the concrete changes you will make, each tied to a finding, pages and target keys. "
            "A change identical to one already implemented in an earlier round is refused unless you give an override_reason; a wait-only round (no change, re-measure later) is allowed but limited.",
            props={"loop_id": LOOP_ID, "items": {"type": "array", "items": ITEM, "maxItems": 30, "description": "Planned changes (empty only with wait_only)."},
                   "summary": S("Why this plan, based on which evidence.", minLength=10, maxLength=4000), "wait_only": B("No change this round: just wait for the search engines and re-measure."),
                   "recheck_after_hours": {"type": "number", "minimum": 0, "maximum": 2160, "description": "How long to wait after the changes are live before re-measuring."},
                   "override_reason": S("Required (20+ characters) to repeat a change already implemented before.", maxLength=1000)},
            required=("loop_id", "items", "summary"), output={"loop": "description"}, permissions=("collab:write", "client:read"), scope="client", read_only=False, category="loops", timeout_s=20)
async def loop_submit_plan(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    return {"loop": ctx.svc.loops.submit_plan(p["loop_id"], ctx.caller, items=p["items"], summary=p["summary"], wait_only=bool(p.get("wait_only")),
                                                recheck_after_hours=p.get("recheck_after_hours"), override_reason=p.get("override_reason", ""), client_id=cid)}


@capability("loop.report_implemented", "Report that the plan's changes are LIVE. Each change must be a recorded change of this client in status deployed/verified, and a production change must have an "
            "executed owner approval (the PIN-approved production_deploy): otherwise this is refused and nothing is counted. Starts the wait before the verification re-measurement.",
            props={"loop_id": LOOP_ID, "change_ids": A("Change artifact ids (change.record / change.mark_deployed).", minItems=1, maxItems=30), "note": S("Anything the next round should know.", maxLength=2000),
                   "recheck_after_hours": {"type": "number", "minimum": 0, "maximum": 2160, "description": "Override the wait before re-measuring."}},
            required=("loop_id", "change_ids"), output={"loop": "description (phase waiting, next_check_at)"}, permissions=("collab:write", "client:read"), scope="client", read_only=False,
            category="loops", timeout_s=20)
async def loop_report_implemented(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    return {"loop": ctx.svc.loops.report_implemented(p["loop_id"], ctx.caller, change_ids=p["change_ids"], note=p.get("note", ""), recheck_after_hours=p.get("recheck_after_hours"), client_id=cid)}


@capability("loop.control", "Pause, resume, stop or immediately re-check a loop you created. Stopping is final and cancels anything still being measured.",
            props={"loop_id": LOOP_ID, "action": S("pause | resume | stop | check_now", enum=["pause", "resume", "stop", "check_now"]), "reason": S("Why (recorded in the history).", maxLength=1000)},
            required=("loop_id", "action"), output={"loop": "description"}, permissions=("collab:write", "client:read"), scope="client", read_only=False, category="loops", timeout_s=60)
async def loop_control(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    return {"loop": await ctx.svc.loops.control(p["loop_id"], p["action"], actor=ctx.caller, reason=p.get("reason", ""), client_id=cid, caller=ctx.caller)}
