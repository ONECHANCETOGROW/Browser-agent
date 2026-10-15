"""Test harness: a real agent on a random loopback port plus a scriptable fake extension that speaks the real protocol."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import aiohttp
from aiohttp.test_utils import TestServer

from octg_agent.app import create_app
from octg_agent.appkeys import RT
from octg_agent.config import DEFAULT_EXTENSION_ID, Secrets, Settings

ORIGIN = f"chrome-extension://{DEFAULT_EXTENSION_ID}"
PAIRING = "pair-token-test"
API = "api-token-test"
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="


class Harness:
    def __init__(self, providers: dict | None = None, brain: Any = None, **settings_kw: Any):
        self.settings_kw = settings_kw
        self.providers, self.brain = providers, brain

    async def __aenter__(self) -> "Harness":
        self.tmp = Path(tempfile.mkdtemp(prefix="octg_test_"))
        self.settings = Settings(data_dir=self.tmp / "data", **self.settings_kw)
        self.secrets = Secrets(PAIRING, API)
        self.app = create_app(self.settings, self.secrets, self.providers if self.providers is not None else {}, self.brain)
        self.rt = self.app[RT]
        self.server = TestServer(self.app, host="127.0.0.1")
        await self.server.start_server()
        self.http = aiohttp.ClientSession()
        self.exts: list[FakeExtension] = []
        return self

    async def __aexit__(self, *exc) -> None:
        for e in self.exts:
            await e.close()
        await self.http.close()
        await self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    async def extension(self, **kw: Any) -> "FakeExtension":
        e = FakeExtension(self, **kw)
        await e.connect()
        self.exts.append(e)
        return e

    async def call(self, action: str, params: dict | None = None, **kw: Any) -> dict:
        return await self.rt.dispatcher.execute(action, params or {}, **kw)

    async def api(self, method: str, path: str, *, json_body: Any = None, headers: dict | None = None, token: str | None = API):
        h = dict(headers or {})
        if token is not None:
            h["X-OCTG-Token"] = token
        async with self.http.request(method, self.base + path, json=json_body, headers=h) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = await r.text()
            return r.status, data


class FakeExtension:
    """handlers: action -> callable(params, command) -> dict of response fields (result/success/error/verification) or an awaitable."""

    def __init__(self, h: Harness, token: str = PAIRING, origin: str | None = ORIGIN, handlers: dict | None = None, protocol: int = 1):
        self.h, self.token, self.origin, self.protocol = h, token, origin, protocol
        self.handlers: dict[str, Callable] = handlers or {}
        self.received: list[dict] = []
        self.first: dict | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self.pings = 0
        self.others: list[dict] = []  # control messages the agent pushed (status, task_update, providers, ui_result ...)

    async def connect(self) -> None:
        headers = {"Origin": self.origin} if self.origin else {}
        self.ws = await self.h.http.ws_connect(f"{self.h.base}/ext", headers=headers)
        await self.ws.send_json({"type": "hello", "token": self.token, "extension_id": DEFAULT_EXTENSION_ID, "version": "test", "protocol": self.protocol,
                                 **({"instance": self.instance} if getattr(self, "instance", None) else {})})
        msg = await self.ws.receive(timeout=5)
        self.first = json.loads(msg.data) if msg.type == aiohttp.WSMsgType.TEXT else {"closed": msg.type}
        if self.first.get("type") == "hello_ack":
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self.ws
        async for m in self.ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if d.get("type") == "pong":
                self.pings += 1
            elif d.get("type") not in (None, "hello_ack"):
                self.others.append(d)
            if d.get("type") is None and "action" in d:
                self.received.append(d)
                handler = self.handlers.get(d["action"])
                out = handler(d.get("parameters", {}), d) if handler else {"result": {}}
                if asyncio.iscoroutine(out):
                    out = await out
                if out is None:  # handler chose not to answer (simulates a hung page)
                    continue
                resp = {"request_id": d["request_id"], "action": d["action"], "success": True, "result": {}, "error": None, "verification": None, **out}
                await self.ws.send_str(json.dumps(resp))

    async def send(self, obj: dict) -> None:
        assert self.ws
        await self.ws.send_str(json.dumps(obj))

    async def event(self, name: str, data: dict) -> None:
        await self.send({"type": "event", "event": name, "seq": len(self.received) + 1, "ts": 1, "data": data})

    async def close(self) -> None:
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


async def wait_until(cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def tab(tab_id: int, url: str = "https://example.test/", active: bool = False, window_id: int = 1, title: str = "T") -> dict:
    return {"tab_id": tab_id, "window_id": window_id, "index": 0, "active": active, "title": title, "url": url, "status": "complete", "supported": True}
