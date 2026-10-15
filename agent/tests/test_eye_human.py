"""The eye and checks that only a person can pass: it recognises them, never tries to get past one, asks the owner, and carries on when the page is clear."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from octg_agent.collab import eye as E
from octg_agent.collab.caps import eye as eye_caps

from .test_collab import CollabCase

CAPTCHA = ["google.com/sorry", "About this page", "Our systems have detected unusual traffic from your computer network.", "I'm not a robot"]
CLOUDFLARE = ["referralautogroup.com", "Verify you are human by completing the action below.", "Performing security verification"]
BLOCKED = ["Sorry, you have been blocked", "You are unable to access this site", "Cloudflare Ray ID: 8a1b"]
NORMAL = ["Winter RV roof inspection", "Book your inspection today", "Call 209 555 0100"]


class PureTests(unittest.TestCase):
    def test_check_pages_are_recognised_and_a_normal_page_is_not(self):
        self.assertEqual(E.human_check("Before you continue", CAPTCHA), "captcha")
        self.assertEqual(E.human_check("Just a moment...", CLOUDFLARE), "bot_check")
        self.assertEqual(E.human_check("Attention Required! | Cloudflare", BLOCKED), "blocked")
        self.assertIsNone(E.human_check("Winter inspection", NORMAL))

    def test_a_long_article_that_only_mentions_a_captcha_is_not_a_check(self):
        article = ["How to build a contact form"] + [f"paragraph {i} about forms" for i in range(150)] + ["add a captcha to stop spam"]
        self.assertIsNone(E.human_check("Contact forms", article))


class FakeSite:
    def __init__(self, lines, title="A page"):
        self.lines, self.title, self.n, self.focused, self.reads = list(lines), title, 70, [], 0
        self.clear_after: int | None = None

    def handlers(self):
        def read(p, c):
            self.reads += 1
            if self.clear_after is not None and self.reads > self.clear_after:
                self.lines, self.title = list(NORMAL), "Winter RV roof inspection"
            return {"result": {"text": "\n".join(self.lines), "url": "https://shop.example.test/", "title": self.title, "total_chars": 100}}

        def new_tab(p, c):
            self.n += 1
            return {"result": {"tab": {"tab_id": self.n, "url": p["url"], "status": "complete"}}}

        return {"new_tab": new_tab, "read_page": read, "wait": lambda p, c: {"result": {}}, "close_tab": lambda p, c: {"result": {"closed": True}},
                "inspect_page": lambda p, c: {"result": {"snapshot_id": "s1", "tab_id": p.get("tab_id"), "elements": [], "url": "https://shop.example.test/", "page": {"headings": []}}},
                "switch_tab": lambda p, c: (self.focused.append(p["tab_id"]) or {"result": {}})}


class HumanCheckTests(CollabCase):
    async def start(self, lines, title="A page", clear_after=None):
        self.site = FakeSite(lines, title)
        self.site.clear_after = clear_after
        self.ext = await self.h.extension(handlers=self.site.handlers())
        r = await self.c.run("eye.open", {"url": "https://shop.example.test/"}, wait=60)
        self.assertEqual(r["status"], "succeeded", r.get("error"))
        return r["result"]

    async def test_the_digest_says_it_is_a_check_and_tells_the_ai_not_to_try(self):
        d = await self.start(CAPTCHA, "Before you continue")
        self.assertEqual(d["human_check"], "captcha")
        self.assertIn("eye.wait_human", d["hint"])
        self.assertIn("Never try", d["hint"])

    async def test_a_normal_page_has_no_flag_and_wait_human_returns_at_once(self):
        d = await self.start(NORMAL, "Winter inspection")
        self.assertNotIn("human_check", d)
        r = await self.c.run("eye.wait_human", {}, wait=30)
        self.assertEqual((r["status"], r["result"]["cleared"]), ("succeeded", True))
        self.assertEqual(self.site.focused, [], "no reason to disturb the owner")

    async def test_a_block_page_cannot_be_cleared_by_the_owner_so_nobody_is_asked(self):
        d = await self.start(BLOCKED, "Attention Required! | Cloudflare")
        self.assertEqual(d["human_check"], "blocked")
        r = await self.c.run("eye.wait_human", {}, wait=30)
        self.assertEqual((r["result"]["cleared"], r["result"]["reason"]), (False, "blocked"))
        self.assertEqual((self.site.focused, self.svc.inbox.open_count()), ([], 0))

    async def test_it_brings_the_tab_forward_asks_the_owner_and_carries_on_when_the_page_clears(self):
        await self.start(CLOUDFLARE, "Just a moment...", clear_after=4)
        with mock.patch.object(eye_caps, "HUMAN_POLL_S", 0.05):
            r = await self.c.run("eye.wait_human", {"max_wait_s": 30}, wait=60)
        self.assertEqual(r["status"], "succeeded", r.get("error"))
        self.assertTrue(r["result"]["cleared"])
        self.assertIn("Winter RV roof inspection", r["result"]["text"])
        self.assertEqual(len(self.site.focused), 1, "the tab was brought to the front once")
        self.assertEqual(self.svc.inbox.open_count(), 0, "the request is withdrawn once the page is clear")
        asked = self.svc.inbox.list(caller="collaborator")
        self.assertEqual(len(asked), 1)
        self.assertIn("pass the check", asked[0]["title"].lower())

    async def test_if_the_owner_says_they_cannot_it_stops_and_does_not_loop(self):
        await self.start(CAPTCHA, "Before you continue")
        with mock.patch.object(eye_caps, "HUMAN_POLL_S", 0.05):
            task = asyncio.create_task(self.c.run("eye.wait_human", {"max_wait_s": 30}, wait=60))
            for _ in range(100):
                await asyncio.sleep(0.05)
                open_ = self.svc.inbox.list(status="open", caller="collaborator")
                if open_:
                    self.svc.inbox.answer(open_[0]["or_id"], choice="I can't pass it", via="panel")
                    break
            r = await task
        self.assertEqual((r["result"]["cleared"], r["result"]["reason"]), (False, "the owner could not pass the check"))

    async def test_if_nobody_comes_it_gives_up_after_the_time_limit(self):
        await self.start(CAPTCHA, "Before you continue")
        with mock.patch.object(eye_caps, "HUMAN_POLL_S", 0.05):
            r = await self.c.run("eye.wait_human", {"max_wait_s": 10}, wait=60)
        self.assertEqual(r["result"]["cleared"], False)


if __name__ == "__main__":
    unittest.main()
