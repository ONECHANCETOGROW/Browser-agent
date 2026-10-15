"""Two Chrome windows/profiles with the extension share one pairing token. The agent serves one at a time (the newer replaces the older); it must say so clearly when they keep taking the connection from
each other, and a copy that was replaced must be told with close code 4000 (the extension stops fighting on that code)."""
from __future__ import annotations

import asyncio
import unittest

from .helpers import FakeExtension, Harness, wait_until


class TakeoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.__aenter__()

    async def asyncTearDown(self):
        await self.h.__aexit__(None, None, None)

    async def copy(self, name: str) -> FakeExtension:
        e = FakeExtension(self.h)
        e.instance = name
        await e.connect()
        self.h.exts.append(e)
        return e

    async def test_the_older_copy_is_closed_with_code_4000_and_the_newer_one_is_served(self):
        a = await self.copy("copyA")
        self.assertIs(self.h.rt.session.hello["instance"] and self.h.rt.session.ws is not None, True)
        b = await self.copy("copyB")
        self.assertTrue(await wait_until(lambda: a.ws.closed, 5))
        self.assertEqual(a.ws.close_code, 4000)
        self.assertEqual(self.h.rt.session.hello["instance"], "copyB")
        self.assertFalse(b.ws.closed)

    async def test_repeated_takeovers_between_two_copies_are_named_in_the_log(self):
        a = await self.copy("copyA")
        with self.assertLogs("octg.bridge", "WARNING") as cm:
            for name in ("copyB", "copyA", "copyB", "copyA"):
                await self.copy(name)
                await asyncio.sleep(0.05)
        text = " ".join(cm.output)
        self.assertIn("Two Chrome windows/profiles", text)
        self.assertIn("copyA", text)
        self.assertIn("copyB", text)
        self.assertEqual(len([o for o in cm.output if "Two Chrome windows" in o]), 1, "one warning, not one per takeover")

    async def test_the_same_copy_reconnecting_is_not_a_takeover(self):
        await self.copy("copyA")
        import logging
        with self.assertNoLogs("octg.bridge", logging.WARNING):
            for _ in range(5):
                await self.copy("copyA")
                await asyncio.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
