"""Two Chrome copies (e.g. the normal Chrome and a ProfileTool profile with a US connection): only one is connected at a time. A session belongs to the browser that opened it."""
from __future__ import annotations

import unittest

from .helpers import FakeExtension
from .test_collab import CollabCase
from .test_eye_human import NORMAL, FakeSite


class BrowserSwitchTests(CollabCase):
    async def connect(self, instance: str, lines=NORMAL):
        site = FakeSite(lines, "A page")
        ext = FakeExtension(self.h, handlers=site.handlers())
        ext.instance = instance
        await ext.connect()
        self.h.exts.append(ext)
        return site, ext

    async def test_a_session_does_not_survive_a_switch_to_another_browser_and_never_closes_its_tabs(self):
        site_a, ext_a = await self.connect("copy-normal-chrome")
        r = await self.c.run("eye.open", {"url": "https://shop.example.test/"}, wait=60)
        self.assertEqual(r["status"], "succeeded", r.get("error"))
        st = (await self.c.run("eye.status", {}, wait=30))["result"]
        self.assertEqual((len(st["sessions"]), st["browser_copy"]), (1, "copy-nor"))
        # the ProfileTool browser connects: the agent now serves it, and the first one is dropped
        site_b, ext_b = await self.connect("copy-profiletool-us")
        look = await self.c.run("eye.look", {}, wait=30)
        self.assertEqual((look["status"], look["error"]["code"]), ("failed", "BROWSER_CHANGED"), look)
        self.assertEqual(site_b.focused, [])
        # a new page opens a fresh tab in the NEW browser; nothing was ever closed there
        r = await self.c.run("eye.open", {"url": "https://shop.example.test/"}, wait=60)
        self.assertEqual(r["status"], "succeeded", r.get("error"))
        self.assertEqual([a["action"] for a in ext_b.received if a["action"] == "close_tab"], [])
        self.assertEqual((await self.c.run("eye.status", {}, wait=30))["result"]["browser_copy"], "copy-pro")

    async def test_closing_an_old_browsers_session_after_a_switch_does_not_send_close_tab_to_the_new_browser(self):
        site_a, ext_a = await self.connect("copy-one")
        await self.c.run("eye.open", {"url": "https://shop.example.test/"}, wait=60)
        site_b, ext_b = await self.connect("copy-two")
        r = await self.c.run("eye.close", {}, wait=30)
        self.assertEqual((r["status"], r["result"]["closed"]), ("succeeded", True))
        self.assertEqual([a["action"] for a in ext_b.received if a["action"] == "close_tab"], [], "that tab id belongs to the other browser and could be one of the owner's own tabs")


if __name__ == "__main__":
    unittest.main()
