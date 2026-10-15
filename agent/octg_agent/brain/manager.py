"""TaskManager: starts tasks, owns their lifecycle and routes user commands (approve / deny / pause / resume / stop) to the running loop.

One task runs at a time (there is one shared browser). Everything is task-scoped: a new task gets a fresh state, context and observation cache.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Callable

from ..engine.dispatcher import Dispatcher
from ..engine.state import BrowserState
from ..reasoning.keys import KeyStore
from ..reasoning.provider import BaseProvider, Message, ProviderError
from ..reasoning.providers import build_providers, merged_config
from ..reasoning.providers.ollama import detect_local_runtimes
from ..reasoning.router import ModelRouter, Policy, RouterConfig
from .approval import ApprovalPolicy
from ..business.resolver import StoreError
from .business_glue import TaskBusiness
from .context import ContextManager
from .history import TaskHistory
from .loop import TaskControl, TaskRunner
from .observation import ObservationBuilder
from .settings import BrainSettings
from .task import ACTIVE, TERMINAL, TaskState, TaskStatus
from .tools import ToolCatalog

log = logging.getLogger("octg.manager")


class TaskError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class TaskManager:
    def __init__(self, dispatcher: Dispatcher, state: BrowserState, settings: BrainSettings, keys: KeyStore, history: TaskHistory,
                 providers: dict[str, BaseProvider] | None = None, business=None):
        self.d, self.state, self.s, self.keys, self.history = dispatcher, state, settings, keys, history
        self.providers = providers if providers is not None else build_providers(settings.providers, keys)
        self.business = business  # a BusinessBrain when the feature flag is on, else None
        self.catalog = ToolCatalog(token_hints=business is not None)
        self._cooldown: dict[str, float] = {}
        self.listeners: list[Callable[[dict], None]] = []
        self.tasks: dict[str, TaskState] = {}
        self.runners: dict[str, TaskRunner] = {}
        self.controls: dict[str, TaskControl] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.current_id: str | None = None
        self.last_id: str | None = None

    # ------------------------------------------------------------------ events
    def _notify(self, task: TaskState) -> None:
        ev = {"type": "task_update", "task": task.to_summary()}
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    def active(self) -> TaskState | None:
        t = self.tasks.get(self.current_id) if self.current_id else None
        return t if t and t.status in ACTIVE else None

    # ------------------------------------------------------------------ start
    def _router_for(self, provider: str | None, policy: str | None) -> ModelRouter:
        cfg = RouterConfig.from_dict(self.s.router)
        if provider and provider.lower() not in ("auto", ""):
            if provider not in self.providers:
                raise TaskError("UNKNOWN_PROVIDER", f"Unknown provider {provider!r}. Available: {', '.join(self.providers)}")
            cfg.policy, cfg.manual_provider = Policy.MANUAL, provider
        elif policy:
            try:
                cfg.policy = Policy(policy.upper())
            except ValueError:
                raise TaskError("BAD_POLICY", f"Unknown routing policy {policy!r}. Use LOCAL_FIRST, GEMINI_FIRST, FALLBACK or MANUAL.") from None
        return ModelRouter(self.providers, cfg, cooldown=self._cooldown)

    async def start(self, instruction: str, *, provider: str | None = None, policy: str | None = None, max_steps: int | None = None,
                    approve_medium: bool | None = None, business: bool | None = None, client: str | None = None) -> TaskState:
        if not instruction or not instruction.strip():
            raise TaskError("EMPTY_TASK", "Write what you want the agent to do.")
        if self.active():
            raise TaskError("TASK_ALREADY_RUNNING", f"Task {self.current_id} is still {self.active().status.value}. Stop it first.")
        settings = dataclasses.replace(self.s)
        if max_steps:
            settings.max_steps = int(max_steps)
        if approve_medium is not None:
            settings.approve_medium = bool(approve_medium)
        router = self._router_for(provider, policy)
        task = TaskState(instruction)
        biz = None
        if self.business is not None:
            try:
                biz = TaskBusiness(self.business.reader(task.task_id, task.user_instruction, business, client), task)
            except StoreError as e:  # an unknown, ambiguous or archived client named by the user: say so instead of starting a task for the wrong one
                raise TaskError("CLIENT_ERROR", e.message) from None
        ctl = TaskControl()
        runner = TaskRunner(
            task, ctl, dispatcher=self.d, router=router, settings=settings, catalog=self.catalog, observer=ObservationBuilder(self.d, settings),
            context=ContextManager(settings, self.catalog.approx_tokens()), approvals=ApprovalPolicy(settings, self.state), history=self.history, notify=self._notify,
            business=biz)
        self.tasks[task.task_id], self.runners[task.task_id], self.controls[task.task_id] = task, runner, ctl
        self.current_id = self.last_id = task.task_id
        self.state.task = task.user_instruction[:90]
        await self.d.push_status()
        self.history.save(task)
        self.jobs[task.task_id] = asyncio.create_task(self._run(task.task_id), name=task.task_id)
        self._notify(task)
        return task

    async def _run(self, task_id: str) -> None:
        runner = self.runners[task_id]
        try:
            await runner.run()
        finally:
            task = self.tasks[task_id]
            self.history.save(task)
            if self.current_id == task_id:
                self.state.task = None
                try:
                    await self.d.push_status()
                except Exception:  # noqa: BLE001
                    pass
            self._notify(task)

    async def wait(self, task_id: str | None = None, timeout: float | None = None) -> TaskState:
        tid = task_id or self.last_id
        job = self.jobs.get(tid)
        if job:
            await asyncio.wait_for(asyncio.shield(job), timeout)
        return self.tasks[tid]

    # ------------------------------------------------------------------ user commands
    def _resolve(self, task_id: str | None) -> tuple[TaskState, TaskControl]:
        tid = task_id if task_id and task_id != "current" else (self.current_id or self.last_id)
        if not tid or tid not in self.tasks:
            raise TaskError("NO_TASK", "There is no such task.")
        return self.tasks[tid], self.controls[tid]

    def approve(self, task_id: str | None = None) -> TaskState:
        t, ctl = self._resolve(task_id)
        if t.status != TaskStatus.WAITING_FOR_APPROVAL:
            raise TaskError("NOT_WAITING", f"Task is {t.status.value}, not waiting for approval.")
        ctl.decision = "approve"
        ctl.signal()
        return t

    def deny(self, task_id: str | None = None) -> TaskState:
        t, ctl = self._resolve(task_id)
        if t.status != TaskStatus.WAITING_FOR_APPROVAL:
            raise TaskError("NOT_WAITING", f"Task is {t.status.value}, not waiting for approval.")
        ctl.decision = "deny"
        ctl.signal()
        return t

    def pause(self, task_id: str | None = None) -> TaskState:
        t, ctl = self._resolve(task_id)
        if t.status in TERMINAL:
            raise TaskError("TASK_FINISHED", f"Task already {t.status.value}.")
        ctl.paused = True
        ctl.signal()
        return t

    def resume(self, task_id: str | None = None) -> TaskState:
        t, ctl = self._resolve(task_id)
        if t.status in TERMINAL:
            raise TaskError("TASK_FINISHED", f"Task already {t.status.value}.")
        if t.status == TaskStatus.WAITING_FOR_APPROVAL:
            raise TaskError("NEEDS_DECISION", "The task is waiting for approve or deny.")
        ctl.paused = False
        if t.status == TaskStatus.WAITING_FOR_USER:
            ctl.decision = "resume"
        ctl.signal()
        return t

    def select_client(self, task_id: str | None, client: str) -> TaskState:
        """The user picks (or changes, before anything was filled) the client of a running task. The model cannot call this."""
        t, _ = self._resolve(task_id)
        runner = self.runners[t.task_id]
        if self.business is None or runner.biz is None:
            raise TaskError("BUSINESS_OFF", "Business Brain is not available for this task.")
        if t.status in TERMINAL:
            raise TaskError("TASK_FINISHED", f"Task already {t.status.value}.")
        try:
            c = self.business.store.find_client(client)
            if c["archived"]:
                raise StoreError("CLIENT_ARCHIVED", "that client is archived")
            runner.biz.select_client(c["client_id"], c["name"])
        except StoreError as e:
            raise TaskError("CLIENT_ERROR", e.message) from None
        except ValueError as e:
            raise TaskError("CLIENT_LOCKED", f"{e}. Start a new task for that client.") from None
        self.business.store.audit("client_selected", task_id=t.task_id, scope=c["client_id"], actor="user", outcome="selected")
        t.log("task", f"Client selected for this task: {c['name']}")
        runner._emit()
        return t

    def stop(self, task_id: str | None = None) -> TaskState:
        t, ctl = self._resolve(task_id)
        if t.status in TERMINAL:
            raise TaskError("TASK_FINISHED", f"Task already {t.status.value}.")
        ctl.stop = True
        self.runners[t.task_id].cancel_inflight()
        ctl.signal()
        return t

    cancel = stop

    # ------------------------------------------------------------------ views
    def get(self, task_id: str | None = None) -> dict | None:
        try:
            t, _ = self._resolve(task_id)
        except TaskError:
            return self.history.get(task_id) if task_id else None
        return t.to_dict()

    def summary(self, task_id: str | None = None) -> dict | None:
        try:
            return self._resolve(task_id)[0].to_summary()
        except TaskError:
            return None

    async def providers_status(self) -> dict:
        router = ModelRouter(self.providers, RouterConfig.from_dict(self.s.router), cooldown=self._cooldown)
        rows = await router.status()
        cfg = merged_config(self.s.providers)
        for r in rows:
            r.update(self.keys.describe(r["name"]) if r["name"] != "local" else {})
            r["config"] = {k: v for k, v in cfg.get(r["name"], {}).items() if k not in ("api_key", "key")}
        return {"providers": rows, "policy": RouterConfig.from_dict(self.s.router).policy.value, "policies": [p.value for p in Policy],
                "usage_report": self.history.usage_report()}

    async def test_provider(self, name: str) -> dict:
        p = self.providers.get(name)
        if p is None:
            return {"ok": False, "provider": name, "error": f"unknown provider; available: {', '.join(self.providers)}"}
        ok, why = await p.available()
        if not ok:
            return {"ok": False, "provider": name, "model": p.model, "stage": "availability", "error": why}
        tool = {"name": "say", "description": "Say one word.", "input_schema": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}
        try:
            r = await p.complete([Message("system", "You are a test. Call the say tool."), Message("user", "Call say with word 'ok'.")], [tool], max_output_tokens=60)
        except ProviderError as e:
            return {"ok": False, "provider": name, "model": p.model, "stage": "call", "error_kind": e.kind, "error": str(e)[:300]}
        called = bool(r.tool_calls) and r.tool_calls[0].name == "say"
        return {"ok": called, "provider": name, "model": r.model or p.model, "latency_ms": r.latency_ms, "usage": r.usage, "tool_call": r.tool_calls[0].arguments if called else None,
                **({} if called else {"error": f"model replied without the tool call (text: {r.text[:100]!r})"})}

    async def detect_local(self) -> list[dict]:
        return await detect_local_runtimes()

    # ------------------------------------------------------------------ side-panel commands
    async def handle_ui(self, msg: dict) -> dict:
        """Commands sent by the side panel over the authenticated extension WebSocket."""
        cmd = msg.get("command")
        try:
            if cmd == "start_task":
                t = await self.start(str(msg.get("instruction", "")), provider=msg.get("provider"), policy=msg.get("policy"),
                                     business=msg.get("business") if isinstance(msg.get("business"), bool) else None,
                                     client=msg.get("client") if isinstance(msg.get("client"), str) and msg.get("client") else None)
                return {"ok": True, "task": t.to_summary()}
            if cmd == "select_client":
                return {"ok": True, "task": self.select_client(msg.get("task_id"), str(msg.get("client_id", ""))).to_summary()}
            if cmd in ("approve", "deny", "pause", "resume", "stop", "cancel"):
                t = getattr(self, cmd)(msg.get("task_id"))
                return {"ok": True, "task": t.to_summary()}
            if cmd == "get_status":
                return {"ok": True, "task": self.summary(), "providers": await self.providers_status()}
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        except TaskError as e:
            return {"ok": False, "error": e.message, "code": e.code}
