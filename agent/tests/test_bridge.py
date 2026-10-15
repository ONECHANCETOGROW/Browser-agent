import asyncio
import unittest

import aiohttp

from .helpers import Harness, ORIGIN, wait_until, tab


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_good_hello_connects_and_acks(self):
        async with Harness() as h:
            ext = await h.extension()
            self.assertEqual(ext.first["type"], "hello_ack")
            self.assertTrue(h.rt.state.connected)
            self.assertEqual(h.rt.state.extension["version"], "test")

    async def test_wrong_origin_rejected_before_websocket(self):
        async with Harness() as h:
            for origin in (None, "https://evil.example", "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "http://127.0.0.1"):
                with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                    await h.http.ws_connect(f"{h.base}/ext", headers={"Origin": origin} if origin else {})
                self.assertEqual(cm.exception.status, 403, origin)
            self.assertFalse(h.rt.state.connected)

    async def test_wrong_token_rejected_and_never_connected(self):
        async with Harness() as h:
            ext = await h.extension(token="nope")
            self.assertEqual(ext.first["type"], "hello_error")
            self.assertEqual(ext.first["code"], "BAD_TOKEN")
            self.assertFalse(h.rt.state.connected)
            self.assertIsNone(h.rt.session)

    async def test_non_ascii_or_odd_tokens_are_a_clean_rejection(self):
        async with Harness() as h:
            for tok in ("tést-☃", "", "x" * 5000):
                ext = await h.extension(token=tok)
                self.assertEqual(ext.first["code"], "BAD_TOKEN")
            self.assertFalse(h.rt.state.connected)

    async def test_protocol_mismatch_rejected(self):
        async with Harness() as h:
            ext = await h.extension(protocol=99)
            self.assertEqual(ext.first["code"], "PROTOCOL_MISMATCH")

    async def test_first_message_must_be_hello(self):
        async with Harness() as h:
            ws = await h.http.ws_connect(f"{h.base}/ext", headers={"Origin": ORIGIN})
            await ws.send_json({"request_id": "x", "action": "click"})
            msg = await ws.receive(timeout=3)
            self.assertIn("BAD_HELLO", msg.data)
            await ws.close()
            self.assertIsNone(h.rt.session)

    async def test_reconnect_replaces_session_and_counts(self):
        async with Harness() as h:
            first = await h.extension()
            self.assertTrue((await h.call("ping"))["success"])
            await first.close()
            self.assertTrue(await wait_until(lambda: not h.rt.state.connected))
            down = await h.call("ping")
            self.assertEqual(down["error"]["code"], "EXTENSION_NOT_CONNECTED")
            second = await h.extension()
            self.assertTrue(await wait_until(lambda: h.rt.state.connected))
            self.assertEqual(h.rt.state.reconnects, 1)
            self.assertTrue((await h.call("ping"))["success"])
            self.assertEqual(len(second.received), 1)

    async def test_new_connection_supersedes_old_one(self):
        async with Harness() as h:
            old = await h.extension(handlers={"ping": lambda p, c: {"result": {"who": "old"}}})
            new = await h.extension(handlers={"ping": lambda p, c: {"result": {"who": "new"}}})
            r = await h.call("ping")
            self.assertEqual(r["result"]["who"], "new")
            self.assertTrue(await wait_until(lambda: old.ws.closed))
            self.assertTrue(h.rt.state.connected)  # old session closing must not mark us disconnected
            self.assertIs(h.rt.session.ws is not None, True)

    async def test_pending_request_fails_cleanly_when_extension_drops(self):
        async with Harness() as h:
            ext = await h.extension(handlers={"ping": lambda p, c: None})  # never answers
            task = asyncio.create_task(h.call("ping"))
            self.assertTrue(await wait_until(lambda: len(ext.received) == 1))
            await ext.close()
            r = await asyncio.wait_for(task, 3)
            self.assertFalse(r["success"])
            self.assertEqual(r["error"]["code"], "EXTENSION_DISCONNECTED")

    async def test_events_update_tab_model(self):
        async with Harness() as h:
            ext = await h.extension()
            await ext.send({"type": "tabs_snapshot", "tabs": [tab(1, active=True), tab(2)]})
            self.assertTrue(await wait_until(lambda: len(h.rt.state.tabs) == 2))
            await ext.event("tab_created", {"tab": tab(3, "https://new.test/")})
            await ext.event("tab_activated", {"tab_id": 3, "window_id": 1})
            self.assertTrue(await wait_until(lambda: h.rt.state.active_tab_id() == 3))
            await ext.event("tab_updated", {"tab": tab(3, "https://new.test/page", active=True), "changed": ["url"]})
            self.assertTrue(await wait_until(lambda: h.rt.state.tabs[3]["url"].endswith("/page")))
            await ext.event("tab_removed", {"tab_id": 2, "window_id": 1})
            self.assertTrue(await wait_until(lambda: 2 not in h.rt.state.tabs))
            names = [e["event"] for e in h.rt.state.events]
            self.assertIn("tab_created", names)
            self.assertIn("agent_extension_connected", names)

    async def test_extension_ping_gets_pong(self):
        async with Harness() as h:
            ext = await h.extension()
            await ext.send({"type": "ping", "ts": 5})
            self.assertTrue(await wait_until(lambda: ext.pings == 1))

    async def test_late_or_unknown_replies_are_ignored(self):
        async with Harness() as h:
            ext = await h.extension()
            await ext.send({"request_id": "req_unknown", "success": True, "result": {}})
            self.assertTrue((await h.call("ping"))["success"])  # still healthy


if __name__ == "__main__":
    unittest.main()
