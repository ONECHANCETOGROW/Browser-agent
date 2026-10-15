"""The AI -> owner direction: owner.ask / owner.replies / owner.cancel, the Hub inbox endpoints and the side-panel commands (ask_*, inbox_*) over the real WebSocket with a fake extension."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from octg_agent.collab.capabilities import REGISTRY
from octg_agent.collab.ownerio import OwnerInbox
from octg_agent.collab.store import CollabError, CollabStore

from .helpers import tab, wait_until
from .test_collab import Collab, CollabCase

STEPS = ["Open https://example.test/verify in Chrome", "Click the blue Verify button", "Read the message that appears"]


class InboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.d.cleanup)
        self.inbox = OwnerInbox(CollabStore(Path(self.d.name) / "c.db"))

    def ask(self, **kw):
        base = dict(caller="collaborator", title="Verify the account", steps=STEPS, question="What does the page say?", choices=["It worked", "I see an error"])
        return self.inbox.create(**{**base, **kw})

    def test_a_request_has_a_strict_shape(self):
        r = self.ask()
        self.assertEqual((r["status"], r["steps"], r["choices"], r["created_by"]), ("open", STEPS, ["It worked", "I see an error"], "collaborator"))
        for bad in (dict(steps=[]), dict(steps=["x"] * 13), dict(title="  "), dict(choices=["a", "A"]), dict(choices=list("abcdefg"))):
            with self.assertRaises(CollabError, msg=bad):
                self.ask(**bad)

    def test_secrets_are_scrubbed_from_what_the_owner_writes(self):
        r = self.ask()
        out = self.inbox.answer(r["or_id"], text="My card is 4111 1111 1111 1111 and the page says Error 7", choice="I see an error")
        self.assertNotIn("4111", out["reply_text"])
        self.assertIn("Error 7", out["reply_text"])

    def test_an_answer_is_final_and_must_say_something(self):
        r = self.ask()
        with self.assertRaises(CollabError) as e:
            self.inbox.answer(r["or_id"], text="")
        self.assertEqual(e.exception.code, "EMPTY_REPLY")
        with self.assertRaises(CollabError) as e:
            self.inbox.answer(r["or_id"], choice="Banana")
        self.assertEqual(e.exception.code, "BAD_CHOICE")
        done = self.inbox.answer(r["or_id"], choice="it worked")
        self.assertEqual((done["status"], done["reply_choice"], done["answered_via"]), ("answered", "it worked", "hub"))
        with self.assertRaises(CollabError) as e:
            self.inbox.answer(r["or_id"], text="again")
        self.assertEqual(e.exception.code, "NOT_OPEN")

    def test_only_the_asker_can_cancel_and_only_while_open(self):
        r = self.ask()
        with self.assertRaises(CollabError):
            self.inbox.cancel(r["or_id"], caller="someone-else")
        self.assertEqual(self.inbox.cancel(r["or_id"], caller="collaborator")["status"], "cancelled")
        with self.assertRaises(CollabError):
            self.inbox.cancel(r["or_id"], caller="collaborator")
        self.assertEqual(self.inbox.open_count(), 0)

    def test_open_requests_come_first_and_old_unanswered_ones_expire(self):
        a, b = self.ask(title="first"), self.ask(title="second")
        self.inbox.answer(a["or_id"], text="done")
        self.assertEqual([r["title"] for r in self.inbox.list()], ["second", "first"])
        self.assertEqual(self.inbox.expire_old(hours=-1), 1)
        self.assertEqual(self.inbox.get(b["or_id"])["status"], "dismissed")


class OwnerCapabilityTests(CollabCase):
    async def test_the_collaborator_asks_the_owner_answers_in_the_hub_and_the_collaborator_reads_it(self):
        r = await self.c.run("owner.ask", {"title": "Verify the account", "steps": STEPS, "question": "What does the page say?", "choices": ["It worked", "I see an error"], "want_page": True})
        self.assertEqual(r["status"], "succeeded", r)
        oid = r["result"]["or_id"]
        st, v = await self.hub.get("/inbox")
        self.assertEqual((st, v["open"]), (200, 1))
        row = v["requests"][0]
        self.assertEqual((row["or_id"], row["title"], row["steps"], row["status"], row["want_page"]), (oid, "Verify the account", STEPS, "open", True))
        pending = await self.c.run("owner.replies", {"id": oid})
        self.assertEqual((pending["result"]["requests"][0]["status"], pending["result"]["still_open"]), ("open", [oid]))
        st, d = await self.hub.post(f"/inbox/{oid}/reply", {"text": "It says: code expired", "choice": "I see an error"})
        self.assertEqual((st, d["request"]["status"]), (200, "answered"))
        got = await self.c.run("owner.replies", {})
        req = got["result"]["requests"][0]
        self.assertEqual((req["or_id"], req["reply_text"], req["reply_choice"], req["answered_via"], got["result"]["still_open"]), (oid, "It says: code expired", "I see an error", "hub", []))
        st, d = await self.hub.post(f"/inbox/{oid}/reply", {"text": "again"})
        self.assertEqual((st, d["code"]), (409, "NOT_OPEN"))
        _, v = await self.hub.get("/inbox")
        self.assertEqual(v["open"], 0)

    async def test_owner_replies_can_wait_for_the_answer(self):
        r = await self.c.run("owner.ask", {"title": "Look at the page", "steps": ["Open the page"]})
        oid = r["result"]["or_id"]
        waiter = asyncio.create_task(self.c.run("owner.replies", {"id": oid, "wait_s": 20}, wait=40))
        await asyncio.sleep(1.5)
        self.assertFalse(waiter.done())
        await self.hub.post(f"/inbox/{oid}/reply", {"text": "I see a green tick"})
        got = await asyncio.wait_for(waiter, 15)
        self.assertEqual(got["result"]["requests"][0]["reply_text"], "I see a green tick")
        self.assertLess(got["result"]["waited_s"], 10)

    async def test_waiting_that_times_out_says_so(self):
        r = await self.c.run("owner.ask", {"title": "Nobody answers", "steps": ["Do it"]})
        got = await self.c.run("owner.replies", {"id": r["result"]["or_id"], "wait_s": 1})
        self.assertEqual((got["status"], got["completeness"], got["result"]["requests"][0]["status"]), ("succeeded", "none", "open"))
        self.assertIn("not answered", " ".join(got["limitations"]))

    async def test_a_caller_only_sees_its_own_requests(self):
        r = await self.c.run("owner.ask", {"title": "Mine", "steps": ["a"]})
        oid = r["result"]["or_id"]
        self.svc.callers.ensure("other-ai")
        other = Collab(self.h, token=self.svc.callers.token_path("other-ai").read_text().strip())
        got = await other.run("owner.replies", {"id": oid})
        self.assertEqual((got["status"], got["error"]["code"]), ("failed", "NO_SUCH_REQUEST"))
        self.assertEqual((await other.run("owner.replies", {"status": "all"}))["result"]["requests"], [])
        c = await other.run("owner.cancel", {"id": oid})
        self.assertEqual(c["status"], "failed")

    async def test_a_caller_without_the_permission_is_refused(self):
        self.svc.callers.set_grants("collaborator", tuple(g for g in self.svc.callers.list()[0]["grants"] if g != "owner:message"))
        st, d = await self.c.submit("owner.ask", {"title": "x y z", "steps": ["a"]})
        self.assertEqual((st, d["error"]["code"] if "error" in d else d["response"]["error"]["code"]), (403, "PERMISSION_DENIED"))

    async def test_cancel_withdraws_it_from_the_inbox(self):
        r = await self.c.run("owner.ask", {"title": "Withdraw me", "steps": ["a"]})
        await self.c.run("owner.cancel", {"id": r["result"]["or_id"]})
        _, v = await self.hub.get("/inbox")
        self.assertEqual(v["open"], 0)
        self.assertEqual(v["requests"][0]["status"], "cancelled")

    async def test_the_shape_is_enforced_at_the_door(self):
        r = await self.c.run("owner.ask", {"title": "no steps", "steps": []})
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "BAD_INPUT"))
        for cap in ("owner.ask", "owner.replies", "owner.cancel"):
            c = REGISTRY[cap]
            self.assertEqual((c.approval, c.external_effects, c.needs_browser), ("never", False, False))

    async def test_the_standard_collaborator_has_the_new_permission(self):
        self.assertIn("owner:message", self.svc.callers.list()[0]["grants"])


class PanelCommandTests(CollabCase):
    """The same features through the Chrome side panel's channel (ui commands -> ui_result), with a fake extension standing in for Chrome."""

    async def connect(self, **handlers):
        ext = await self.h.extension(handlers={"screenshot": self._shot, **handlers})
        await ext.send({"type": "tabs_snapshot", "tabs": [tab(5, "https://example.test/verify", active=True, title="Verify")]})
        self.ext = ext
        return ext

    def _shot(self, p, cmd):
        f = Path(self.h.tmp) / "shot.jpg" if hasattr(self.h, "tmp") else Path(tempfile.gettempdir()) / "octg_test_shot.jpg"
        f.write_bytes(b"\xff\xd8\xff\xd9")
        return {"result": {"path": str(f)}}

    async def ui(self, command, **kw):
        rid = f"t{len(self.ext.others)}x{command}"
        await self.ext.send({"type": "ui", "command": command, "request_id": rid, **kw})
        assert await wait_until(lambda: any(m.get("request_id") == rid for m in self.ext.others), 10), command
        return next(m for m in self.ext.others if m.get("request_id") == rid)

    async def test_the_badge_count_is_pushed_on_connect_and_when_a_request_arrives(self):
        await self.c.run("owner.ask", {"title": "Already waiting", "steps": ["a"]})
        ext = await self.connect()
        self.assertTrue(await wait_until(lambda: any(m.get("type") == "inbox" and m.get("open") == 1 for m in ext.others), 5), "the count is pushed when Chrome connects")
        await self.c.run("owner.ask", {"title": "Second", "steps": ["b"]})
        self.assertTrue(await wait_until(lambda: any(m.get("type") == "inbox" and m.get("open") == 2 for m in ext.others), 5))

    async def test_list_and_reply_with_a_quick_answer_from_the_panel(self):
        await self.connect()
        r = await self.c.run("owner.ask", {"title": "Check the button", "steps": STEPS, "choices": ["It worked", "I see an error"]})
        oid = r["result"]["or_id"]
        lst = await self.ui("inbox_list")
        self.assertTrue(lst["ok"])
        self.assertEqual((lst["panel"]["open"], lst["panel"]["requests"][0]["or_id"], lst["panel"]["requests"][0]["steps"]), (1, oid, STEPS))
        self.assertNotIn("created_by", lst["panel"]["requests"][0])
        res = await self.ui("inbox_reply", or_id=oid, choice="It worked", text="all fine")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["panel"]["request"]["status"], res["panel"]["open"]), ("answered", 0))
        got = await self.c.run("owner.replies", {"id": oid})
        self.assertEqual((got["result"]["requests"][0]["answered_via"], got["result"]["requests"][0]["reply_choice"]), ("panel", "It worked"))
        again = await self.ui("inbox_reply", or_id=oid, text="x")
        self.assertEqual((again["ok"], again["code"]), (False, "NOT_OPEN"))

    async def test_the_owner_can_attach_what_is_on_the_tab_and_the_ai_reads_it(self):
        def state(p, c):
            return {"result": {"tab": {}, "page": {"url": "https://example.test/verify", "title": "Verify your account", "headings": []}}}

        def read(p, c):
            return {"result": {"text": "Error 17: the code expired. Card on file 4111 1111 1111 1111", "url": "https://example.test/verify", "title": "t", "total_chars": 60}}
        await self.connect(get_page_state=state, read_page=read)
        r = await self.c.run("owner.ask", {"title": "What does it say?", "steps": ["Open the page"], "want_page": True})
        oid = r["result"]["or_id"]
        a = await self.ui("inbox_attach", or_id=oid, tab_id=5)
        self.assertTrue(a["ok"], a)
        att = a["panel"]["attachment"]
        self.assertEqual((att["url"], att["title"], att["screenshot"]), ("https://example.test/verify", "Verify your account", True))
        res = await self.ui("inbox_reply", or_id=oid, text="This is what I see", attachments=[att["att_id"]])
        self.assertTrue(res["ok"], res)
        self.assertEqual([x["kind"] for x in res["panel"]["request"]["attachments"]], ["page_text", "screenshot"])
        got = (await self.c.run("owner.replies", {"id": oid}))["result"]["requests"][0]
        page = next(x for x in got["attachments"] if x["kind"] == "page_text")
        shot = next(x for x in got["attachments"] if x["kind"] == "screenshot")
        self.assertIn("Error 17: the code expired", page["text"])
        self.assertNotIn("4111", page["text"], "secret-looking numbers are removed from attached page text")
        self.assertTrue(Path(shot["path"]).is_file())
        self.assertEqual(page["url"], "https://example.test/verify")

    async def test_an_unreadable_tab_is_explained_and_an_attachment_cannot_be_reused_elsewhere(self):
        def refuse(p, c):
            return {"success": False, "error": {"code": "TAB_RESTRICTED", "message": "chrome:// pages block extension scripts"}}
        await self.connect(get_page_state=refuse)
        r1 = await self.c.run("owner.ask", {"title": "One", "steps": ["a"]})
        a = await self.ui("inbox_attach", or_id=r1["result"]["or_id"], tab_id=5)
        self.assertFalse(a["ok"])
        self.assertIn("cannot be read", a["error"])
        bad = await self.ui("inbox_attach", or_id=r1["result"]["or_id"], tab_id="five")
        self.assertEqual(bad["code"], "NO_TAB")
        r2 = await self.c.run("owner.ask", {"title": "Two", "steps": ["b"]})
        res = await self.ui("inbox_reply", or_id=r2["result"]["or_id"], text="x", attachments=["att_nope"])
        self.assertEqual((res["ok"], res["code"]), (False, "ATTACHMENT_GONE"))

    async def test_dismiss_from_the_panel(self):
        await self.connect()
        r = await self.c.run("owner.ask", {"title": "Skip me", "steps": ["a"]})
        d = await self.ui("inbox_dismiss", or_id=r["result"]["or_id"])
        self.assertEqual((d["ok"], d["panel"]["open"]), (True, 0))

    async def test_ask_the_agent_works_from_the_panel_too_and_the_panel_cannot_approve_anything(self):
        await self.new_client("Example Painting Co", website="https://example-painting.test/")
        await self.connect()
        cl = await self.ui("ask_clients")
        self.assertEqual([c["name"] for c in cl["panel"]["clients"]], ["Example Painting Co"])
        self.assertEqual(cl["panel"]["default"], cl["panel"]["clients"][0]["id"])
        none = await self.ui("ask_send", text="go to google and tell me what you find", client_id=cl["panel"]["default"])
        self.assertEqual((none["ok"], none["panel"]["understood"]), (True, False))
        miss = await self.ui("ask_send", text="search for painters in Alabama")
        self.assertEqual((miss["ok"], miss["code"]), (False, "CLIENT_REQUIRED"))
        lst = await self.ui("ask_list")
        self.assertEqual(lst["panel"]["asks"], [])
        unk = await self.ui("inbox_approve", or_id="x")
        self.assertFalse(unk["ok"])
        self.assertEqual(unk["code"], "UNKNOWN_COMMAND")


if __name__ == "__main__":
    unittest.main()
