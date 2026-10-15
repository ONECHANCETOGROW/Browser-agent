"""The agent loop for ONE task: observe → reason → validate → (approve) → act → observe → verify → update state → repeat.

The model never touches the browser. Every action goes through `Dispatcher.execute()`; the model only chooses among registry tools.
Control (pause/stop/approve/deny/resume) is honoured between steps and immediately before each browser action.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from ..engine import risk as risk_mod
from ..engine.dispatcher import Dispatcher
from ..engine.protocol import build_response, make_error, new_request_id
from ..engine.registry import ACTIONS
from ..reasoning.pricing import estimate_cost
from ..reasoning.provider import ProviderError
from ..reasoning.router import ModelRouter, NoProviderAvailable, RouteContext, provider_is_local
from ..business.resolver import TOKEN_ANY, Manifest
from .approval import ApprovalPolicy
from .business_glue import TaskBusiness, origin_of
from .context import ContextManager
from .history import TaskHistory
from .observation import Observation, ObservationBuilder, ObservationError, tokens
from .settings import BrainSettings
from .task import ModelCallRecord, StepRecord, TaskState, TaskStatus, brief_args
from .tools import NO_AUTO_TAB, ParsedCall, ToolCallError, ToolCatalog

log = logging.getLogger("octg.brain")

def _match_kind(m: dict) -> str:
    """Compact label for a find_text match, e.g. 'link', 'heading h2', 'body_text': just the page's own structure, no judgement."""
    kind = m.get("match_type") or "match"
    return f"{kind} h{m['heading_level']}" if m.get("is_heading") and m.get("heading_level") else kind


def _canonical_args(name: str, args: dict) -> dict:
    """The arguments that make two calls *different* for the repetition guard. Parameters left at their default are not a difference, so
    find_text(text="x") and find_text(text="x", index=0) are the same attempt; a different index (a different occurrence) still is not."""
    out = {k: v for k, v in args.items() if k not in ("snapshot_id",)}
    if name == "find_text":
        for k, default in (("index", 0), ("case_sensitive", False), ("whole_word", False), ("scroll", True)):
            if out.get(k) == default:
                out.pop(k)
        if isinstance(out.get("text"), str):
            out["text"] = " ".join(out["text"].split()) if out.get("case_sensitive") else " ".join(out["text"].split()).lower()  # matching is case-insensitive by default
    return out


READ_ONLY = {"get_tabs", "get_page_state", "read_page", "screenshot", "verify", "inspect_page", "fs_find", "fs_save_download", "get_agent_state"}
UNSUPPORTED_STRIKES = 2
PAGE_ACTIONS = {"inspect_page", "read_page", "find_text", "get_page_state", "click", "type", "clear", "select", "check", "uncheck", "scroll", "press_key", "upload_file", "screenshot"}


class Cancelled(Exception):
    pass


class TaskStop(Exception):
    """The loop decided the task is over (limit reached, unrecoverable failure)."""


class TaskControl:
    """Thread-of-control flags shared between the manager (user commands) and the running loop."""

    def __init__(self) -> None:
        self.stop = False
        self.paused = False
        self.decision: str | None = None  # approve | deny | resume
        self.wake = asyncio.Event()

    def signal(self) -> None:
        self.wake.set()


class TaskRunner:
    def __init__(self, task: TaskState, ctl: TaskControl, *, dispatcher: Dispatcher, router: ModelRouter, settings: BrainSettings, catalog: ToolCatalog,
                 observer: ObservationBuilder, context: ContextManager, approvals: ApprovalPolicy, history: TaskHistory | None,
                 notify: Callable[[TaskState], None] = lambda t: None, business: TaskBusiness | None = None):
        self.t, self.ctl = task, ctl
        self.d, self.router, self.s = dispatcher, router, settings
        self.catalog, self.observer, self.context, self.approvals, self.history = catalog, observer, context, approvals, history
        self.notify = notify
        self.biz = business
        if business is not None:
            business.approvals = approvals
        self.feedback = ""
        self.force_inspect = False
        self.want_text = False
        self.pending_image: bytes | None = None
        self.tool_error_streak = 0
        self.fail_streak = 0
        self.stale_streak = 0
        self.unsupported_streak = 0
        self.grant = 0  # one-shot: the user approved what the model announced via request_approval
        self.waited_s = 0.0
        self._inflight: asyncio.Future | None = None
        self.last_provider = ""
        self.finished = False
        self.pending_user: tuple[str, str] | None = None
        self.pending_reconnect = False
        self.last_sig: tuple | None = None
        self.repeat_streak = 0

    # ------------------------------------------------------------------ plumbing
    def _emit(self, persist: bool = False) -> None:
        self.t.touch()
        try:
            self.notify(self.t)
        except Exception:  # noqa: BLE001 - a broken UI listener must never break the task
            log.exception("task listener failed")
        if persist and self.history:
            try:
                self.history.save(self.t)
            except Exception:  # noqa: BLE001
                log.exception("could not save task history")

    def _status(self, status: TaskStatus, **kw: Any) -> None:
        self.t.set_status(status, **kw)
        self._emit(persist=True)

    def _active_elapsed(self) -> float:
        return time.time() - self.t.started_at - self.waited_s

    def _check_limits(self) -> None:
        t, s = self.t, self.s
        if t.step_count >= s.max_steps:
            raise TaskStop(f"Reached the step limit ({s.max_steps} browser actions) without finishing.")
        if t.model_calls >= s.max_model_calls:
            raise TaskStop(f"Reached the model-call limit ({s.max_model_calls}) without finishing.")
        if self._active_elapsed() > s.max_seconds:
            raise TaskStop(f"Reached the time limit ({s.max_seconds:g}s) without finishing.")

    async def _gate(self) -> None:
        """Pause/stop checkpoint. While paused no new model call or browser action starts."""
        while True:
            if self.ctl.stop:
                raise Cancelled()
            if self.ctl.paused:
                if self.t.status != TaskStatus.PAUSED:
                    self.t.log("control", "Paused: no new browser actions until resumed")
                    self._status(TaskStatus.PAUSED)
                t0 = time.time()
                self.ctl.wake.clear()
                await self.ctl.wake.wait()
                self.waited_s += time.time() - t0
                continue
            if self.t.status == TaskStatus.PAUSED:
                self.t.log("control", "Resumed")
                self._status(TaskStatus.RUNNING)
            return

    async def _wait_decision(self, allowed: tuple[str, ...]) -> str:
        t0 = time.time()
        try:
            while True:
                if self.ctl.stop:
                    raise Cancelled()
                if self.ctl.decision in allowed:
                    d, self.ctl.decision = self.ctl.decision, None
                    return d
                self.ctl.wake.clear()
                await self.ctl.wake.wait()
        finally:
            self.waited_s += time.time() - t0

    async def _wait_user(self, kind: str, message: str) -> None:
        self.t.waiting_kind = kind
        self.t.log("user", message)
        self.ctl.decision = None
        self._status(TaskStatus.WAITING_FOR_USER, message=message)
        await self._wait_decision(("resume",))
        self.t.waiting_kind = None
        self.t.log("user", "User signalled ready; re-observing")
        self.observer.mark_dirty(self.t)
        self._status(TaskStatus.RUNNING)
        await self._gate()

    async def _ask_approval(self, pending) -> str:
        self.t.pending_approval = pending
        self.t.log("approval", f"Approval needed: {pending.description}")
        self.ctl.decision = None
        self._status(TaskStatus.WAITING_FOR_APPROVAL, message=pending.description)
        decision = await self._wait_decision(("approve", "deny"))
        self.t.pending_approval = None
        self.t.log("approval", f"User {'approved' if decision == 'approve' else 'denied'}: {pending.description}")
        self._status(TaskStatus.RUNNING)
        return decision

    async def _cancellable(self, coro) -> Any:
        """Run a model call in a task `stop` can cancel."""
        self._inflight = asyncio.ensure_future(coro)
        try:
            return await self._inflight
        except asyncio.CancelledError:
            if self.ctl.stop:
                raise Cancelled() from None
            raise
        finally:
            self._inflight = None

    def cancel_inflight(self) -> None:
        if self._inflight and not self._inflight.done():
            self._inflight.cancel()

    # ------------------------------------------------------------------ run
    async def run(self) -> TaskState:
        t = self.t
        try:
            self._status(TaskStatus.PLANNING)
            t.log("task", f"Started: {t.user_instruction[:160]}")
            while not self.finished:
                self._check_limits()
                await self._gate()
                obs = await self._observe()
                if obs is None:
                    continue
                parsed = await self._think(obs)
                if parsed is None:
                    continue
                if parsed.note:
                    t.add_note(parsed.note, self.s.max_notes)
                if parsed.kind == "control":
                    await self._control(parsed)
                else:
                    await self._act(parsed, obs)
        except Cancelled:
            t.log("control", "Task cancelled")
            self._status(TaskStatus.CANCELLED, error=None)
        except TaskStop as e:
            t.log("task", str(e))
            self._status(TaskStatus.FAILED, error=str(e))
        except asyncio.CancelledError:
            self._status(TaskStatus.CANCELLED)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("task crashed")
            self._status(TaskStatus.FAILED, error=f"internal error: {type(e).__name__}: {e}")
        finally:
            if self.biz is not None:
                self.biz.release()
        return t

    # ------------------------------------------------------------------ observe
    async def _observe(self) -> Observation | None:
        t = self.t
        focus = " ".join([t.user_instruction, t.current_step, (t.last_action_result or {}).get("summary", "")[:120]])
        force, want_text = self.force_inspect, self.want_text
        self.force_inspect = self.want_text = False
        try:
            obs = await self.observer.observe(t, force=force, focus=focus, want_text=want_text)
        except ObservationError as e:
            if e.code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED"):
                await self._await_extension()
                return None
            raise TaskStop(f"Cannot observe the browser: {e.code}: {e}")
        rt = self.observer.rt(t)
        if "captcha" in obs.signals and rt.get("captcha_ack") != obs.url:
            await self._wait_user("captcha", "CAPTCHA detected. Please complete it manually in Chrome, then press Resume.")
            rt["captcha_ack"] = obs.url
            return None
        t.log("observe", f"{obs.url[:90]} | {obs.element_total} elements" + (f" | {','.join(obs.signals)}" if obs.signals else ""))
        self._emit()
        return obs

    async def _await_extension(self) -> None:
        deadline = time.time() + self.s.reconnect_wait_s
        self.t.log("browser", "Chrome extension not connected; waiting for it to reconnect")
        while time.time() < deadline:
            await self._gate()
            if self.d.get_session() is not None:
                return
            await asyncio.sleep(1.0)
        await self._wait_user("browser", "The Chrome extension is not connected. Open Chrome with the extension loaded, then press Resume.")

    # ------------------------------------------------------------------ reason
    async def _think(self, obs: Observation) -> ParsedCall | None:
        t, s = self.t, self.s
        biz = self.biz if (self.biz is not None and self.biz.active) else None
        image = self.pending_image
        needs_vision = image is not None
        feedback = self.feedback
        self.feedback = ""
        self.pending_image = None
        manifest = Manifest()
        if biz is not None:
            biz.sanitize_observation(obs)
            manifest = biz.manifest(obs, allow_values=any(provider_is_local(p) for p in self.router.providers.values()))
        # Data that must not leave this machine (an enum fact shown to the model, a screenshot of a filled form) restricts this call to LOCAL providers.
        local_only = bool(biz is not None and (manifest.class1 or (image is not None and biz.has_fills())))
        built = self.context.build(t, obs, feedback=feedback, image=image, business_block=manifest.text, redact=biz.redact if biz else None)
        last = t.last_action or {}
        rctx = RouteContext(
            step=t.step_count, first_step=t.model_calls == 0, consecutive_failures=self.fail_streak, tool_errors=self.tool_error_streak,
            stale_recovery=self.stale_streak > 0, element_count=obs.element_total, text_heavy=bool(obs.text) and len(obs.text) > 300 or last.get("tool") == "read_page",
            needs_vision=needs_vision, signals=[x for x in obs.signals if x != "restricted_page"], instruction_words=len(t.user_instruction.split()),
            tab_count=len(obs.tabs), local_only=local_only)
        await self._gate()
        routed = None
        had_image = image is not None
        for strict in ((True, False) if local_only else (False,)):
            if local_only and not strict:
                # no local model could answer: rather than send anything private to a cloud model, drop it (values hidden, screenshot withheld) and carry on
                manifest = biz.manifest(obs, allow_values=False)
                image, rctx.local_only, rctx.needs_vision = None, False, False
                note = " A screenshot was withheld because the page holds private details; rely on the text observation." if had_image else ""
                built = self.context.build(t, obs, feedback=feedback + note, image=None, business_block=manifest.text, redact=biz.redact)
                t.log("model", "no local model available: private context withheld from this call")
            try:
                routed = await self._cancellable(self.router.complete(
                    built.messages, self.catalog.llm_tools(), rctx, meta={"task": t.to_summary(), "observation": obs, "shown_ids": built.shown_ids},
                    max_output_tokens=500))
                break
            except NoProviderAvailable as e:
                for a in e.attempts:
                    if a.get("error_kind") not in ("skipped", None):
                        t.model_calls += 1
                self._record_attempts(e.attempts)
                if strict and local_only:
                    continue
                raise TaskStop(f"No model provider could answer: {e}")
        for a in routed.attempts:
            if not a.get("ok") and a.get("error_kind") not in (None, "skipped"):
                t.model_calls += 1  # a failed call still counts as a call
                t.log("model", f"{a['provider']} failed ({a['error_kind']}); trying next provider")
        t.model_calls += 1
        timing = self._record_attempts(routed.attempts)  # per-call timing (instrumentation only)
        resp = routed.response
        prov = self.router.providers[routed.provider]
        if biz is not None:
            biz.reader.audit_prompt(routed.provider, biz.locality(prov), manifest, image=image is not None)
        t.record_usage(routed.provider, resp.model, resp.usage.get("input_tokens"), resp.usage.get("output_tokens"),
                       estimate_cost(prov.kind, resp.model, resp.usage.get("input_tokens"), resp.usage.get("output_tokens"), s.pricing))
        t.routing = {"provider": routed.provider, "model": resp.model, "reason": routed.reason, "complexity": routed.complexity,
                     "prompt_tokens_est": built.tokens, "trimmed": built.trimmed}
        self.last_provider = routed.provider
        if t.status == TaskStatus.PLANNING:
            self._status(TaskStatus.RUNNING)
        if not resp.tool_calls:
            self._close_call(timing, "no_tool_call")
            return self._tool_error(f"Your reply contained no tool call (text: {resp.text[:120]!r}). Reply with exactly one tool call.")
        try:
            parsed = self.catalog.parse(resp.tool_calls[0])
        except ToolCallError as e:
            self._close_call(timing, "invalid_call")
            return self._tool_error(str(e))
        self.tool_error_streak = 0
        t.current_step = parsed.reason or parsed.name
        self._close_call(timing, parsed.name)
        return parsed

    # ---- model-call timing (instrumentation: provider, model, step, start, duration, outcome, retries, chosen action; never a prompt, reply or page text)
    def _record_attempts(self, attempts: list[dict]) -> ModelCallRecord | None:
        """One ModelCallRecord per real provider attempt of this routed call. Returns the successful one so the chosen action can be added to it."""
        t = self.t
        answered: ModelCallRecord | None = None
        failed_before = False
        for a in attempts:
            if a.get("error_kind") == "skipped" or "duration_ms" not in a:
                continue
            reasons = [str(x)[:40] for x in (a.get("retry_reasons") or [])]
            rec = ModelCallRecord(n=len(t.model_log) + 1, provider=a["provider"], model=str(a.get("model") or ""), step=t.step_count, started_at=float(a.get("started_at") or time.time()),
                                  duration_ms=int(a["duration_ms"]), ok=bool(a.get("ok")), error_kind=None if a.get("ok") else a.get("error_kind"),
                                  retries=len(reasons), retry_reasons=reasons, fell_back=bool(a.get("ok")) and failed_before)
            t.record_model_call(rec)
            if a.get("ok"):
                answered = rec
            else:
                failed_before = True
                t.log("model", rec.line())
        return answered

    def _close_call(self, rec: ModelCallRecord | None, action: str) -> None:
        if rec is not None:
            rec.action = action
            self.t.log("model", rec.line())

    def _tool_error(self, message: str) -> None:
        self.tool_error_streak += 1
        self.t.log("model", f"Unusable model reply: {message[:140]}")
        self.feedback = "Your last reply was not usable: " + message
        if self.tool_error_streak >= self.s.max_tool_errors:
            raise TaskStop(f"The model repeatedly produced unusable tool calls ({message[:120]}).")
        self._emit()
        return None

    # ------------------------------------------------------------------ control tools
    async def _control(self, p: ParsedCall) -> None:
        t = self.t
        n = len(t.recent_steps) + 1
        if p.name == "finish_task":
            ok = p.args["status"] == "completed"
            t.result = p.args["summary"]
            t.record_step(StepRecord(n, "finish_task", {"status": p.args["status"]}, p.reason, ok, p.args["summary"][:160], provider=self.last_provider))
            self.finished = True
            t.log("task", f"{'Completed' if ok else 'Failed'}: {p.args['summary'][:200]}")
            self._status(TaskStatus.COMPLETED if ok else TaskStatus.FAILED, error=None if ok else p.args["summary"])
            return
        if p.name == "ask_user":
            kind, q = p.args["kind"], p.args["question"]
            t.record_step(StepRecord(n, "ask_user", {"kind": kind}, p.reason, True, q[:160], provider=self.last_provider))
            await self._wait_user(kind, q)
            self.feedback = "The user says they are ready (they may have logged in, solved a CAPTCHA or changed the page). Re-read the observation."
            return
        # request_approval
        pending = self.approvals.make_pending("(announced step)", {}, {"level": "high", "tags": ["model_requested"], "approval": "required", "reasons": []}, p.reason, "the model asked for approval", "model")
        pending.description = p.args["summary"]
        d = await self._ask_approval(pending)
        t.record_step(StepRecord(n, "request_approval", {}, p.reason, d == "approve", f"user {'approved' if d == 'approve' else 'denied'}: {p.args['summary'][:100]}", provider=self.last_provider))
        if d == "approve":
            self.grant = 1
            self.feedback = "The user APPROVED that step. You may perform it now."
        else:
            self.feedback = "The user DENIED approval. Do not do that step; find another way or call finish_task."

    # ------------------------------------------------------------------ act
    def _bind(self, p: ParsedCall, obs: Observation) -> tuple[dict, dict | None]:
        spec = ACTIONS[p.name]
        args = dict(p.args)
        tab = args.get("tab_id")
        if isinstance(tab, str) and tab.isdigit():
            tab = args["tab_id"] = int(tab)
        if "tab_id" in spec.properties and p.name not in NO_AUTO_TAB and tab is None and obs.active_tab_id is not None:
            tab = args["tab_id"] = obs.active_tab_id
        if not obs.supported and p.name in PAGE_ACTIONS and (tab is None or tab == obs.active_tab_id):
            return args, {"code": "UNSUPPORTED_PAGE", "message": "This Chrome page cannot be controlled by the extension."}
        if args.get("element_id"):
            if obs.snapshot_id is None or tab != obs.active_tab_id:
                return args, {"code": "STALE_ELEMENT", "message": "No snapshot was shown for that tab. Switch to it and call inspect_page first."}
            args["snapshot_id"] = obs.snapshot_id
        for key in ("checks", "until"):
            if isinstance(args.get(key), list):
                args[key] = [{**c, "snapshot_id": obs.snapshot_id} if isinstance(c, dict) and c.get("element_id") else c for c in args[key]]
        return args, None

    async def _act(self, p: ParsedCall, obs: Observation) -> None:
        t, s = self.t, self.s
        spec = ACTIONS[p.name]
        args, local_err = self._bind(p, obs)
        el = self.approvals.element_brief(args)
        sensitive = bool(el and (el.get("sensitive") or el.get("type") == "password"))
        if local_err:
            resp = build_response(new_request_id(), p.name, False, error=make_error(local_err["code"], local_err["message"]))
            self._after(p, args, resp, obs, 0, sensitive, executed=False)
            await self._post()
            return

        # ---- Business Brain: the model named a field/document; code supplies the exact value (or stops and asks the user)
        exec_args, fills = args, []
        if self.biz is None:
            if any(TOKEN_ANY.search(str(v)) for v in args.values()):
                self._tool_error("Business tokens ({{biz:...}} / {{doc:...}}) are not available in this task. If you need the user's business detail, call ask_user.")
                return
        else:
            plan = self.biz.prepare(p.name, args, p.expect, el)
            if plan.status == "feedback":
                self._tool_error(plan.message)
                return
            if plan.status == "blocked":
                t.record_step(StepRecord(len(t.recent_steps) + 1, p.name, brief_args(args, sensitive=True), p.reason, False, "blocked: sensitive field", "SENSITIVE_FIELD", self.last_provider))
                await self._wait_user(plan.kind, f"I will not type into {el.get('name') if el else 'this field'}: {plan.message}. Please enter it yourself in Chrome, then press Resume.")
                self.feedback = "The user handled the sensitive field themselves. Continue with the next step."
                return
            if plan.status == "wait":
                again = self.biz.repeated_wait(plan)
                t.record_step(StepRecord(len(t.recent_steps) + 1, p.name, brief_args(args, sensitive=True), p.reason, False, f"needs the user: {plan.kind.replace('_', ' ')}", "NEEDS_USER", self.last_provider))
                await self._wait_user(plan.kind, plan.message)
                self.feedback = ("The user was asked and pressed Resume. If the business detail is still not available, do NOT guess or type anything else for it: skip that field or call finish_task(failed)."
                                 if again else "The user dealt with it (added, confirmed or resolved the detail, or filled the field themselves). Re-read the observation, then continue.")
                return
            if plan.status == "ok":
                exec_args, fills = plan.args, plan.fills

        risk = risk_mod.assess(spec, args, self.d.state)
        disclosure = self.biz.decision(fills, obs, el, risk) if (self.biz is not None and fills) else None
        if disclosure is not None:
            ddec, dtext = disclosure
            pending = self.approvals.make_pending(p.name, args, ddec.risk, p.reason, dtext)
            pending.description = dtext
            if await self._ask_approval(pending) == "deny":
                t.record_step(StepRecord(len(t.recent_steps) + 1, p.name, brief_args(args, sensitive=sensitive), p.reason, False, "denied by user", "DENIED", self.last_provider))
                t.last_action = {"tool": p.name, "args": brief_args(args, sensitive=sensitive), "reason": p.reason}
                t.last_action_result = {"ok": False, "error_code": "DENIED", "summary": "the user did not approve using that business detail on this site"}
                self.feedback = "The user DENIED using that business detail on this site. Do not retry it; ask_user or finish_task."
                return
            self.biz.grant(fills, obs)
        decision = self.approvals.decide(p.name, args, risk, t)
        if decision.action == "block":
            msg = f"I will not type into {el.get('name') if el else 'this field'}: {decision.why}. Please enter it yourself in Chrome, then press Resume."
            t.record_step(StepRecord(len(t.recent_steps) + 1, p.name, brief_args(args, sensitive=True), p.reason, False, "blocked: sensitive field", "SENSITIVE_FIELD", self.last_provider))
            await self._wait_user("sensitive_input", msg)
            self.feedback = "The user handled the sensitive field themselves. Continue with the next step."
            return
        if decision.action == "ask":
            if self.grant > 0:
                self.grant -= 1
                t.log("approval", f"Pre-approved by the user's earlier consent: {self.approvals.describe(p.name, args, risk)}")
            else:
                pending = self.approvals.make_pending(p.name, args, risk, p.reason, decision.why)
                if fills and self.biz is not None:
                    pending.description = self.biz.describe(p.name, fills, el, obs)
                elif self.biz is not None and self.biz.has_fills() and "business details were entered" in decision.why:
                    pending.description += f" — business details entered so far: {self.biz.ledger_summary()}"
                if await self._ask_approval(pending) == "deny":
                    t.record_step(StepRecord(len(t.recent_steps) + 1, p.name, brief_args(args, sensitive=sensitive), p.reason, False, "denied by user", "DENIED", self.last_provider))
                    t.last_action = {"tool": p.name, "args": brief_args(args, sensitive=sensitive), "reason": p.reason}
                    t.last_action_result = {"ok": False, "error_code": "DENIED", "summary": "the user denied approval for this action"}
                    self.feedback = "The user DENIED that action. Do not retry it; choose another approach or call finish_task."
                    return
        await self._gate()
        t.log("act", f"{p.name} {json.dumps(brief_args(args, sensitive=sensitive), ensure_ascii=False)[:140]} — {p.reason[:80]}")
        self._emit()
        t0 = time.time()
        resp = await asyncio.shield(self.d.execute(p.name, exec_args, timeout_s=s.action_timeout_s, source="agent", sensitive_call=bool(fills)))
        await self._after_with_verify(p, args, resp, obs, int((time.time() - t0) * 1000), sensitive, fills=fills, el=el)
        await self._post()

    async def _post(self) -> None:
        """Waits that error recovery decided on (they need `await`, so they cannot run inside the synchronous bookkeeping)."""
        if self.pending_reconnect:
            self.pending_reconnect = False
            await self._await_extension()
        if self.pending_user:
            kind, msg = self.pending_user
            self.pending_user = None
            await self._wait_user(kind, msg)
            self.feedback = "The user looked at the page and is ready; re-read the observation."

    async def _after_with_verify(self, p, args, resp, obs, ms, sensitive, fills=None, el=None) -> None:
        expect_summary = None
        if p.expect and resp["success"]:
            checks = [dict(c) for c in p.expect]
            vr = await asyncio.shield(self.d.execute("wait", {"tab_id": args.get("tab_id", obs.active_tab_id), "until": checks, "timeout_ms": 4000, "poll_ms": 300}, source="agent"))
            met = bool(vr["success"] and (vr["result"] or {}).get("satisfied"))
            expect_summary = {"ok": met, "checks": [{"type": c["type"], "value": c["value"]} for c in checks]}
        self._after(p, args, resp, obs, ms, sensitive, executed=True, expect=expect_summary, fills=fills, el=el)

    def _after(self, p: ParsedCall, args: dict, resp: dict, obs: Observation, ms: int, sensitive: bool, *, executed: bool, expect: dict | None = None,
               fills: list | None = None, el: dict | None = None) -> None:
        t = self.t
        ok = resp["success"]
        code = (resp.get("error") or {}).get("code")
        outcome = self._summarize(p.name, args, resp, private=bool(fills))
        vsum = self._verif_summary(resp, expect)
        if executed:
            t.step_count += 1
        rec = StepRecord(len(t.recent_steps) + 1, p.name, brief_args(args, sensitive=sensitive), p.reason, ok, outcome, code, self.last_provider, ms,
                         stored_outcome=("read page text (content not stored)" if p.name == "read_page" else
                                         f"searched the page: {(resp.get('result') or {}).get('total_matches', 0)} match(es) (content not stored)" if p.name == "find_text" else None) if ok else None)
        t.record_step(rec)
        t.last_action = {"tool": p.name, "args": rec.args, "reason": p.reason}
        t.last_action_result = {"ok": ok, "error_code": code, "summary": outcome}
        t.verification_result = {"ok": ok and (expect is None or expect["ok"]), "observed_change": (resp.get("verification") or {}).get("observed_change"), "summary": vsum, "expect": expect}
        t.log("result", f"{p.name}: {'ok' if ok else 'FAILED ' + str(code)} — {outcome[:140]}")
        self._emit(persist=True)
        if fills and self.biz is not None:
            self.biz.record(fills, step=t.step_count, obs=obs, el=el, ok=ok)
            if ok and (resp.get("result") or {}).get("value_matches") is False:
                # the page did not keep exactly what was entered (mask, length limit, rewrite): never let the model "fix" it with another value
                self.pending_user = ("input_mismatch", f"The field did not keep the stored {fills[0].label} exactly as it is (the page changed or limited it). I will not retry with a different value. "
                                     "Please check the field, fix it yourself or correct your profile, then press Resume.")

        # task-scoped bookkeeping from the engine's evidence
        v = resp.get("verification") or {}
        for nt in v.get("new_tabs") or []:
            t.created_tabs.add(nt["tab_id"])
        if p.name == "new_tab" and ok:
            t.created_tabs.add(resp["result"]["tab"]["tab_id"])
        if p.name == "screenshot" and ok and resp["result"].get("path"):
            try:
                self.pending_image = open(resp["result"]["path"], "rb").read()
            except OSError:
                pass
        if p.name in ("inspect_page",):
            self.force_inspect = True
        if p.name == "read_page":
            self.feedback = ""
        if p.name not in READ_ONLY:
            self.observer.mark_dirty(t)
        if expect is not None and not expect["ok"]:
            self.feedback = "The expectation you set was NOT observed: " + ", ".join(f"{c['type']}={c['value']!r}" for c in expect["checks"]) + ". Check the observation before continuing."

        self._track_repeats(p, args, resp, ok)
        self.fail_streak = 0 if ok else self.fail_streak + 1
        if ok:
            self.stale_streak = self.unsupported_streak = 0
            return
        self._recover(p, resp, code or "")

    def _track_repeats(self, p: ParsedCall, args: dict, resp: dict, ok: bool) -> None:
        """Generic loop guard: the same action, with the same arguments, that did nothing (or failed) again and again."""
        sig = (p.name, json.dumps(_canonical_args(p.name, args), sort_keys=True, default=str))
        changed = bool((resp.get("verification") or {}).get("observed_change"))
        useless = (not ok) or (not changed and p.name not in ("get_tabs", "wait"))
        if sig == self.last_sig and useless:
            self.repeat_streak += 1
        else:
            self.repeat_streak = 1 if useless else 0
        self.last_sig = sig
        n = self.repeat_streak
        if n >= self.s.max_repeat_actions:
            self.repeat_streak = 0
            self.pending_user = ("stuck", f"I keep repeating '{p.name}' without any effect and I am not making progress. Please look at the page and tell me what to do (or do the step yourself), then press Resume.")
        elif n >= 3:
            self.feedback = (f"You have now repeated {p.name} {n} times with the same arguments and nothing changed. Do something DIFFERENT: another element, scroll, read_page, "
                             "navigate somewhere else, or call ask_user / finish_task(failed).")

    # ------------------------------------------------------------------ error recovery
    def _recover(self, p: ParsedCall, resp: dict, code: str) -> None:
        err = resp["error"]
        msg = err["message"][:200]
        if code in ("STALE_ELEMENT", "ELEMENT_NOT_FOUND"):
            self.force_inspect = True
            self.stale_streak += 1
            self.feedback = f"{code}: the page changed, so I re-inspected it. Element ids have changed; re-identify the element in the NEW observation before acting."
            if self.stale_streak >= self.s.max_stale_retries:
                self.stale_streak = 0
                self.pending_user = ("element", "I cannot reliably identify the element I need after several tries. Please look at the page and tell me (or do the step yourself), then press Resume.")
        elif code == "ELEMENT_NOT_INTERACTABLE":
            self.force_inspect = True
            self.feedback = f"{msg} A dialog, overlay or cookie banner may be in the way: close it (close button / Escape) or scroll the element into view, then retry."
        elif code in ("PAGE_NAVIGATED", "PAGE_NOT_READY"):
            self.force_inspect = True
            self.feedback = "The page was changing while I acted. I re-observed it; check the new state before acting."
        elif code in ("ACTION_TIMEOUT", "PAGE_UNRESPONSIVE", "NAVIGATION_FAILED"):
            self.force_inspect = True
            self.feedback = f"{code}: {msg} Verify the actual state in the observation before deciding; do not assume the action did or did not happen."
        elif code == "UNSUPPORTED_PAGE":
            self.unsupported_streak += 1
            self.feedback = "That page cannot be controlled by the extension (restricted Chrome page). Navigate to a normal website first."
            if self.unsupported_streak >= UNSUPPORTED_STRIKES:
                raise TaskStop("This Chrome page cannot be controlled by the extension, so the task cannot continue here. Open a normal website tab and run the task again.")
        elif code in ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED"):
            self.pending_reconnect = True
            self.feedback = "The browser connection dropped; I am waiting for it to reconnect."
        elif code == "INVALID_PARAMS":
            self.tool_error_streak += 1
            self.feedback = f"Invalid arguments for {p.name}: {msg}"
            if self.tool_error_streak >= self.s.max_tool_errors:
                raise TaskStop(f"The model repeatedly produced invalid arguments ({msg}).")
        else:
            self.force_inspect = True
            self.feedback = f"{p.name} failed with {code}: {msg}"

    # ------------------------------------------------------------------ summaries
    def _summarize(self, name: str, args: dict, resp: dict, private: bool = False) -> str:
        if not resp["success"]:
            e = resp["error"]
            extra = ""
            d = e.get("details") or {}
            if d.get("obscured_by"):
                extra = f" (covered by {d['obscured_by'].get('tag')} {d['obscured_by'].get('name') or ''})"
            return (e["message"][:180] + extra).strip()
        r = resp.get("result") or {}
        c = self.s.read_result_chars
        if name == "navigate":
            return f"loaded {r.get('final_url', '')[:100]} title={r.get('title', '')[:60]!r}"
        if name == "new_tab":
            tb = r.get("tab") or {}
            return f"opened tab #{tb.get('tab_id')} {tb.get('url', '')[:80]}"
        if name == "switch_tab":
            return f"now on tab #{(r.get('tab') or {}).get('tab_id')}"
        if name in ("back", "forward", "reload"):
            return f"now at {r.get('final_url', '')[:100]}"
        if name == "close_tab":
            return f"closed tab #{r.get('closed_tab_id')}"
        if name == "click":
            return f"clicked {((r.get('element') or {}).get('name') or args.get('element_id') or 'point')!s}" + (" (opened new tab)" if r.get("opened_tab") else "")
        if name == "type":
            shown = "" if r.get("value_matches", True) else f"; BUT the field shows {str(r.get('value_after'))[:40]!r} (not what was typed)"
            if private:
                shown = "" if r.get("value_matches", True) else "; BUT the page did not keep it exactly"
            return f"typed {args.get('text', '') and len(str(args.get('text')))} chars into {(r.get('element') or {}).get('name')!r}; field value matches" + shown
        if name == "clear":
            return "cleared"
        if name == "select":
            return "selected the requested option" if private else "selected " + ", ".join(x["label"] for x in r.get("selected", []))
        if name in ("check", "uncheck"):
            return f"checked={r.get('checked')}"
        if name == "scroll":
            return f"scrolled to y={(r.get('after') or {}).get('y')} of {(r.get('max') or {}).get('y')}" + (" (limit reached)" if r.get("reached_limit") else "")
        if name == "press_key":
            return f"pressed {r.get('key')}" + (f" → {r['default_action']}" if r.get("default_action") else "")
        if name == "read_page":
            return f"text: {r.get('text', '')[:c]}"
        if name == "find_text":
            q = str(args.get("text", ""))[:40]
            if not r.get("found"):
                hid = r.get("hidden_matches") or 0
                return f"no visible match for {q!r}" + (f" ({hid} only inside hidden/collapsed content)" if hid else "")
            f = r.get("focused") or (r.get("matches") or [{}])[0]
            head = f" under {f['nearest_heading']!r}" if f.get("nearest_heading") else ""
            moved = "scrolled to" if r.get("focused") else "first match (not scrolled)"
            line = f"{r.get('total_matches')} match(es) for {q!r}; {moved} #{f.get('match_index', 0)} [{_match_kind(f)}]{head}: {str(f.get('context', ''))[:c]}"
            if r.get("total_matches", 0) > 1:  # the model chooses the occurrence it wants (index=); nothing is ranked for it
                line += " | all: " + "; ".join(f"#{m.get('match_index')} {_match_kind(m)}" + (f" under {m['nearest_heading'][:28]!r}" if m.get("nearest_heading") else "") for m in (r.get("matches") or [])[:8])
            return line
        if name == "get_page_state":
            pg = r.get("page") or {}
            return f"{pg.get('url', '')[:90]} scroll {(pg.get('scroll') or {}).get('y')}/{(pg.get('scroll') or {}).get('max_y')}"
        if name == "get_tabs":
            return f"{r.get('count')} tabs, active #{r.get('active_tab_id')}"
        if name == "inspect_page":
            return f"inspected: {r.get('counts', {}).get('returned')} elements"
        if name == "screenshot":
            return f"screenshot {r.get('width')}x{r.get('height')}"
        if name == "upload_file":
            return "attached the requested document" if private else "attached " + ", ".join(f["name"] for f in r.get("files", []))
        if name == "download_file":
            return f"downloaded to {r.get('filename')}"
        if name in ("verify", "wait"):
            return f"conditions {'met' if r.get('all_passed', r.get('satisfied')) else 'NOT met'}"
        return json.dumps(r)[:160]

    @staticmethod
    def _verif_summary(resp: dict, expect: dict | None) -> str:
        v = resp.get("verification") or {}
        parts = []
        if (v.get("url") or {}).get("changed"):
            parts.append(f"URL changed to {v['url']['after'][:90]}")
        if v.get("navigated") and not (v.get("url") or {}).get("changed"):
            parts.append("page reloaded")
        for nt in v.get("new_tabs") or []:
            parts.append(f"new tab #{nt['tab_id']} {nt.get('url', '')[:60]}")
        if v.get("closed_tabs"):
            parts.append(f"closed tabs {v['closed_tabs']}")
        pg = v.get("page") or {}
        if pg.get("text_changed"):
            parts.append("page text changed")
        if pg.get("dialogs_opened"):
            parts.append(f"dialog opened: {pg['dialogs_opened'][0][:60]!r}")
        if pg.get("scroll_changed"):
            parts.append("scroll moved")
        if pg.get("focus_changed"):
            parts.append("focus moved")
        failed = [c["name"] for c in v.get("checks", []) if not c.get("passed")]
        if failed:
            parts.append("failed checks: " + ", ".join(failed))
        if expect is not None:
            parts.append("expectation " + ("MET" if expect["ok"] else "NOT met"))
        if not parts:
            parts.append("no visible change" if v else "(read-only)")
        return "; ".join(parts)
