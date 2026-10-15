"""Wires settings, state, file authorization, dispatcher, the extension WebSocket and the local HTTP API into one aiohttp app."""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from .appkeys import RT

from .api.http import add_api_routes, guard_middleware
from .api.task_routes import add_task_routes
from .bridge.server import add_bridge_routes
from .hub.routes import add_hub_routes
from .collab.routes import add_collab_routes
from .collab.service import CollabService
from .collab import caps as _collab_caps  # noqa: F401 - registers the capabilities
from .bridge.session import ExtensionSession
from .config import Secrets, Settings
from .engine.dispatcher import Dispatcher
from .engine.state import BrowserState
from .files.authorization import AuthorizedFolders
from .logging_setup import ActionLog
from .brain.history import TaskHistory
from .business.service import BusinessBrain, BusinessConfig
from .reasoning.router import provider_is_local
from .brain.manager import TaskManager
from .brain.settings import BrainSettings
from .reasoning.keys import KeyStore

log = logging.getLogger("octg.app")


class Runtime:
    """Everything one running agent owns."""

    def __init__(self, settings: Settings, secrets: Secrets, providers: dict | None = None, brain: BrainSettings | None = None):
        settings.ensure_dirs()
        self.settings = settings
        self.secrets = secrets
        self.state = BrowserState(settings.max_events)
        self.folders = AuthorizedFolders(settings.folders_path)
        self.actionlog = ActionLog(settings.logs_dir)
        self.session: ExtensionSession | None = None
        self.dispatcher = Dispatcher(settings, self.state, self.folders, self.actionlog, lambda: self.session)
        # AI layer: sits on top of the dispatcher; the browser engine does not know it exists
        self.keys = KeyStore(settings.data_dir)
        self.brain = brain or BrainSettings.load(settings.data_dir)
        self.history = TaskHistory(settings.data_dir / "tasks.db")
        # Business Brain: only created (and business.db only touched) when the feature flag is on
        cfg = BusinessConfig.from_dict(self.brain.business)
        self.business: BusinessBrain | None = None
        self.business_error: str | None = None
        if cfg.enabled:
            try:
                self.business = BusinessBrain(settings.data_dir, self.folders, cfg)
            except Exception as e:  # noqa: BLE001 - a damaged database must not stop the agent: tasks simply run without the Business Brain (and tokens are refused)
                self.business_error = f"{type(e).__name__}"
                log.error("Business Brain is switched on but could not start (%s); tasks will run without it", self.business_error)
        self.tasks = TaskManager(self.dispatcher, self.state, self.brain, self.keys, self.history, providers, business=self.business)
        self.tasks.listeners.append(self._push_to_extension)
        self.collab = CollabService(self)
        from .collab.panel_api import PanelApi
        self.panel = PanelApi(self)   # Ask the agent + Inbox, for the side panel
        self.ui_handler = self._ui  # generic hooks used by bridge/server.py
        self.on_connect = self._on_extension_connect

    async def _ui(self, msg: dict) -> dict:
        """Side-panel commands: Business Brain views/decisions (biz_*) are answered here; everything else is a task command."""
        cmd = str(msg.get("command", ""))
        if self.panel.handles(cmd):
            if cmd in self.panel.SLOW:       # these talk to the browser: this loop must keep reading the extension's replies, so they answer later
                asyncio.ensure_future(self._answer_later(msg))
                return None
            return await self.panel.handle(cmd, msg)
        if cmd.startswith("biz_"):
            if self.business is None:
                if cmd == "biz_status":
                    return {"ok": True, "biz": {"enabled": False, **({"error": f"Business Brain could not start ({self.business_error})"} if self.business_error else {})}}
                return {"ok": False, "error": "Business Brain is switched off.", "code": "BIZ_DISABLED"}
            provs = [{"name": n, "local": provider_is_local(p)} for n, p in self.tasks.providers.items()]
            return self.business.ui(cmd, msg, provs)
        return await self.tasks.handle_ui(msg)

    async def _answer_later(self, msg: dict) -> None:
        try:
            res = await self.panel.handle(str(msg.get("command", "")), msg)
        except Exception:  # noqa: BLE001 - the panel must always get an answer
            log.exception("panel command %s", msg.get("command"))
            res = {"ok": False, "error": "The agent could not do that.", "code": "INTERNAL_ERROR"}
        s = self.session
        if s is not None:
            await s.send_json({"type": "ui_result", "request_id": msg.get("request_id"), "command": msg.get("command"), **res})

    def _push_to_extension(self, event: dict) -> None:
        s = self.session
        if s is not None:
            asyncio.ensure_future(s.send_json(event))

    async def _on_extension_connect(self) -> None:
        s = self.session
        if s is None:
            return
        summary = self.tasks.summary()
        if summary:
            await s.send_json({"type": "task_update", "task": summary})
        try:
            await s.send_json({"type": "inbox", "open": self.collab.inbox.open_count()})   # the toolbar badge shows what the AI is waiting for
        except Exception:  # noqa: BLE001 - the badge is a convenience
            log.exception("inbox badge on connect")
        rows = (await self.tasks.providers_status())
        await s.send_json({"type": "providers", "policy": rows["policy"], "policies": rows["policies"],
                           "providers": [{"name": r["name"], "model": r["model"], "available": r["available"], "local": r["local"]} for r in rows["providers"]]})


def create_app(settings: Settings, secrets: Secrets, providers: dict | None = None, brain: BrainSettings | None = None) -> web.Application:
    rt = Runtime(settings, secrets, providers, brain)
    app = web.Application(middlewares=[guard_middleware], client_max_size=12 * 1024 * 1024)   # the Knowledge Hub accepts workbooks up to 10 MB; every other route takes small JSON
    app[RT] = rt
    add_bridge_routes(app)
    add_api_routes(app)
    add_task_routes(app)
    add_hub_routes(app)
    add_collab_routes(app, rt.collab)
    return app
