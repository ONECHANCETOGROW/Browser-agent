"""One connected extension: request/response correlation by request_id."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from ..engine.protocol import make_command

log = logging.getLogger("octg.bridge")


class BridgeError(Exception):
    pass


class BridgeTimeout(BridgeError):
    pass


class BridgeDisconnected(BridgeError):
    pass


class ExtensionSession:
    def __init__(self, ws: web.WebSocketResponse, hello: dict):
        self.ws = ws
        self.hello = hello
        self.pending: dict[str, asyncio.Future] = {}
        self.closed = False

    async def send_json(self, obj: dict) -> bool:
        if self.closed or self.ws.closed:
            return False
        try:
            await self.ws.send_str(json.dumps(obj))
            return True
        except (ConnectionResetError, RuntimeError):
            return False

    async def request(self, request_id: str, action: str, parameters: dict, timeout: float) -> dict:
        if self.closed or self.ws.closed:
            raise BridgeDisconnected("The extension is not connected.")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = fut
        try:
            if not await self.send_json(make_command(request_id, action, parameters)):
                raise BridgeDisconnected("The extension connection dropped while sending.")
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise BridgeTimeout(f"No reply from the extension within {timeout:g}s.") from None
        finally:
            self.pending.pop(request_id, None)

    def resolve(self, msg: dict) -> bool:
        fut = self.pending.get(msg.get("request_id"))
        if fut is None or fut.done():
            log.warning("Ignoring reply for unknown/finished request %s", msg.get("request_id"))
            return False
        fut.set_result(msg)
        return True

    def fail_all(self, reason: str) -> None:
        self.closed = True
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(BridgeDisconnected(reason))

    async def close(self, reason: str = "closing", code: int = 1000) -> None:
        self.fail_all(reason)
        if not self.ws.closed:
            await self.ws.close(code=code, message=reason.encode()[:100])
