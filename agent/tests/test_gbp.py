"""Google Business Profile reviews through the eye: reading every page, finding ONE review, opening its Reply box, and posting only after the owner approves. A fake reviews page stands behind the REAL dispatcher."""
from __future__ import annotations

import asyncio
import unittest

from octg_agent.collab import gbp as G
from octg_agent.collab.capabilities import REGISTRY
from octg_agent.hub.security import HubPin

from .test_collab import CollabCase

STAR = ""
BUSINESSES = [("Example RV Service", "1 Main Street, Springfield, IL 62701, United States"), ("The Example Diner", "2 Main Street, Springfield, IL 62701, United States"),
              ("Example Cigar Co.", "3 Main Street, Springfield, IL 62701, United States")]


def make_reviews():
    out = []
    for i in range(25):
        biz, addr = BUSINESSES[i % 3]
        out.append({"biz": biz, "addr": addr, "who": f"Reviewer {i:02d}", "stars": 1 + i % 5, "when": f"{i + 1} days ago", "text": f"Review number {i:02d} says the service was fine overall.",
                    "reply": f"Thanks {i:02d}, glad you liked it." if i % 4 != 0 else None})
    out[3]["text"] = "The user didn't write a review, and has left just a rating."
    return out


class FakeReviews:
    """business.google.com/reviews: 10 reviews per page, a Next button, Reply buttons on unreplied reviews, a Reply box that Cancel closes and Post reply submits."""

    def __init__(self):
        self.reviews, self.page, self.per, self.n, self.snap = make_reviews(), 0, 10, 40, 0
        self.open_for: int | None = None
        self.box_text = ""
        self.posted: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.typed: list[str] = []
        self.closed: list[int] = []
        self.stale_next = 0
        self.misplace_box = False
        self.elements: dict[str, dict] = {}
        self.actions: dict[str, tuple] = {}

    def build(self):
        start = self.page * self.per
        chunk = list(enumerate(self.reviews))[start:start + self.per]
        lines = ["Business Profile Manager", "Businesses", "Reviews", "Linked accounts", "Settings", "Support", "Ungrouped", "one chance to grow", "sort"]
        self.snap += 1
        self.elements, self.actions = {}, {}

        def el(role, name, tag="button", action=None, **extra):
            eid = f"el_{len(self.elements) + 1:03d}"
            self.elements[eid] = {"id": eid, "tag": tag, "role": role, "name": name, "in_viewport": True, **extra}
            if action:
                self.actions[eid] = action

        el("button", "Open sort options")
        for idx, r in chunk:
            lines += [r["biz"], r["addr"], r["who"], f" {STAR * r['stars']} {r['when']}", r["text"]]
            if r["reply"]:
                lines += ["(owner)", "1 day ago", r["reply"], "Edit", "Delete"]
                el("button", "Edit")
                el("button", "Delete")
            elif self.open_for == idx and not self.misplace_box:
                lines += self.box_lines()
                self.box_elements(el, idx)
            else:
                lines += ["reply", "Reply"]
                el("button", "reply Reply", action=("reply", idx))
        if self.misplace_box and self.open_for is not None:       # a box that draws under the wrong review: the guard must notice
            lines = lines[:-2] + self.box_lines() + lines[-2:]
            self.box_elements(el, self.open_for)
        last = (start + self.per) >= len(self.reviews)
        lines += ["Rows per page:", "10", "25", "50", f"{start + 1}–{start + len(chunk)} of many", "©2026 Google-Terms-Privacy Policy"]
        el("button", "Previous", disabled=self.page == 0)
        el("button", "Next", disabled=last, action=("next", None))
        return "\n".join(lines), list(self.elements.values())

    def box_lines(self):
        return ["Your reply", "Please note that your reply will be displayed publicly on Google. Terms of service", "Post reply", "Cancel", "(owner)"]

    def box_elements(self, el, idx):
        el("textbox", "Your reply", tag="textarea", value=self.box_text)
        el("link", "Terms of service", tag="a")
        el("button", "Post reply", action=("post", idx))
        el("button", "Cancel", action=("cancel", idx))

    def handlers(self):
        def new_tab(p, c):
            self.n += 1
            self.page, self.open_for = 0, None          # a fresh tab starts on page 1 with no Reply box open
            return {"result": {"tab": {"tab_id": self.n, "url": p["url"], "status": "complete"}}}

        def read(p, c):
            text, _ = self.build()
            return {"result": {"text": text, "url": "https://business.google.com/reviews", "title": "Reviews", "total_chars": len(text)}}

        def inspect(p, c):
            _, els = self.build()
            return {"result": {"snapshot_id": f"sn{self.snap}", "tab_id": p.get("tab_id"), "url": "https://business.google.com/reviews", "elements": els, "page": {"headings": []}}}

        def click(p, c):
            action = self.actions.get(p["element_id"])
            name = self.elements[p["element_id"]]["name"]
            self.clicks.append(name)
            if action and action[0] == "next" and self.stale_next:
                self.stale_next -= 1
                return {"success": False, "result": None, "error": {"code": "STALE_ELEMENT", "message": "The element was removed from the page. Re-run inspect_page."}}
            if action:
                kind, idx = action
                if kind == "next":
                    self.page += 1
                elif kind == "reply":
                    self.open_for, self.box_text = idx, ""
                elif kind == "cancel":
                    self.open_for = None
                elif kind == "post":
                    self.reviews[idx]["reply"] = self.box_text
                    self.posted.append((self.reviews[idx]["who"], self.box_text))
                    self.open_for = None
            return {"result": {"clicked": True}}

        def typ(p, c):
            self.typed.append(p["text"])
            self.box_text = p["text"]
            return {"result": {"typed": True}}

        def close(p, c):
            self.closed.append(p["tab_id"])
            return {"result": {"closed": True}}
        return {"new_tab": new_tab, "navigate": lambda p, c: {"result": {"url": p["url"]}}, "read_page": read, "inspect_page": inspect, "click": click, "type": typ, "close_tab": close,
                "press_key": lambda p, c: {"result": {}}, "wait": lambda p, c: {"result": {}}}


class PureTests(unittest.TestCase):
    def test_reviews_are_parsed_with_rating_reply_state_and_the_owner_reply(self):
        app = FakeReviews()
        text, _ = app.build()
        rows = G.parse_reviews(text)
        self.assertEqual(len(rows), 10)
        first = rows[0]
        self.assertEqual((first["biz"], first["reviewer"], first["when"], first["replied"]), ("Example RV Service", "Reviewer 00", "1 days ago", False))
        self.assertEqual((rows[1]["replied"], rows[1]["owner_reply"]), (True, "Thanks 01, glad you liked it."))
        self.assertTrue(rows[3]["rating_only"])
        self.assertEqual(G.page_range(text), "1–10 of many")
        sm = G.summarize(rows)
        self.assertEqual((sm["reviews"], sm["unreplied"]), (10, 3))

    def test_finding_a_review_needs_business_reviewer_and_excerpt_to_agree(self):
        text, _ = FakeReviews().build()
        rows = G.parse_reviews(text)
        self.assertEqual(G.find_review(rows, business="example rv", reviewer="reviewer 00", excerpt="Review number 00 says"), [0])
        self.assertEqual(G.find_review(rows, business="example diner", reviewer="Reviewer 00", excerpt="Review number 00 says"), [], "wrong business")
        self.assertEqual(G.find_review(rows, business="example rv", reviewer="Reviewer 99", excerpt="Review number 00 says"), [], "wrong reviewer")
        self.assertEqual(G.find_review(rows, business="example rv", reviewer="Reviewer 00", excerpt="something else entirely"), [], "wrong text")


class GbpTests(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = FakeReviews()
        self.ext = await self.h.extension(handlers=self.app.handlers())

    async def go(self, cap, params=None, **kw):
        return await self.c.run(cap, params or {}, wait=60, **kw)

    async def ok(self, cap, params=None):
        r = await self.go(cap, params)
        self.assertEqual(r["status"], "succeeded", (cap, r.get("error")))
        return r["result"]

    async def approve(self, approval_id: str) -> None:
        HubPin(self.h.settings.data_dir).set("4711")
        st, x = await self.hub.post(f"/approvals/{approval_id}/approve", {"note": "ok", "pin": "4711"})
        self.assertEqual(st, 200, x)

    TARGET = {"business": "example diner", "reviewer": "Reviewer 04", "review_excerpt": "Review number 04 says the service"}      # i=4: unreplied (4 % 4 == 0), on page 1

    async def test_it_reads_every_page_and_counts_the_reviews_with_no_owner_reply(self):
        d = await self.ok("gbp.reviews", {})
        self.assertEqual((d["reviews_read"], d["pages_read"], d["complete"]), (25, 3, True))
        want = sum(1 for r in self.app.reviews if r["reply"] is None)
        self.assertEqual(d["unreplied"], want)
        self.assertEqual(sum(d["unreplied_by_business"].values()), want)
        self.assertEqual(len(d["unreplied_reviews"]), want)
        self.assertNotIn("rating", d["unreplied_reviews"][0], "the page text cannot tell filled stars from empty ones, so no rating is reported")
        self.assertEqual(set(self.app.clicks), {"Next"}, "reading only turns pages: it never opens a Reply box")
        self.assertEqual((self.app.typed, self.app.posted), ([], []))
        self.assertTrue(self.app.closed, "its tab is closed afterwards")

    async def test_a_page_that_redraws_between_look_and_click_is_retried_instead_of_failing(self):
        self.app.stale_next = 1
        d = await self.ok("gbp.reviews", {})
        self.assertEqual((d["reviews_read"], d["complete"]), (25, True))
        self.app.stale_next, self.app.page = 2, 0                  # two failures in a row is more than one retry: it stops and says so instead of pretending
        d = await self.ok("gbp.reviews", {})
        self.assertEqual((d["reviews_read"], d["complete"]), (10, False))
        self.assertIn("stopped", d["note"])

    async def test_it_can_stop_early_and_says_the_count_is_then_not_complete(self):
        d = await self.ok("gbp.reviews", {"max_pages": 2})
        self.assertEqual((d["reviews_read"], d["complete"]), (20, False))
        self.assertIn("not counted", d["note"])

    async def test_reply_check_opens_the_right_box_proves_it_and_closes_it_without_posting(self):
        d = await self.ok("gbp.reply_check", {**self.TARGET, "type_test": True})
        self.assertEqual((d["found"], d["box_is_under_this_review"], d["closed"]), (True, True, True))
        self.assertEqual((d["review"]["business"], d["review"]["reviewer"]), ("The Example Diner", "Reviewer 04"))
        self.assertEqual(self.app.posted, [], "nothing is ever posted by a check")
        self.assertIsNone(self.app.open_for, "the box was cancelled")
        self.assertIn("Cancel", self.app.clicks)
        self.assertNotIn("Post reply", self.app.clicks)

    async def test_a_review_on_a_later_page_is_found_by_turning_pages(self):
        d = await self.ok("gbp.reply_check", {"business": "example rv", "reviewer": "Reviewer 24", "review_excerpt": "Review number 24 says"})
        self.assertEqual((d["found"], d["closed"]), (True, True))
        self.assertEqual(self.app.clicks.count("Next"), 2)

    async def test_wrong_name_already_replied_and_look_alike_reviews_are_refused_before_any_box_opens(self):
        for params, code in (({**self.TARGET, "reviewer": "Nobody At All"}, "REVIEW_NOT_FOUND"),
                             ({"business": "example diner", "reviewer": "Reviewer 01", "review_excerpt": "Review number 01 says"}, "ALREADY_REPLIED")):
            r = await self.go("gbp.reply_check", params)
            self.assertEqual((r["status"], r["error"]["code"]), ("failed", code), r)
        self.app.reviews[8].update(who="Reviewer 04", biz="The Example Diner", text="Review number 04 says the service was fine overall.", reply=None)       # a look-alike on the same page
        r = await self.go("gbp.reply_check", self.TARGET)
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "AMBIGUOUS_REVIEW"), r)
        self.assertNotIn("reply Reply", self.app.clicks)

    async def test_a_box_that_opens_under_the_wrong_review_is_cancelled_and_nothing_is_typed(self):
        self.app.misplace_box = True
        r = await self.go("gbp.reply_check", {**self.TARGET, "type_test": True})
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "WRONG_REVIEW"), r)
        self.assertEqual((self.app.typed, self.app.posted), ([], []))
        self.assertIsNone(self.app.open_for)

    async def test_posting_waits_for_the_owner_then_posts_exactly_the_approved_text_and_checks_it(self):
        text = "Thank you for the feedback. We would like to make this right: please call us."
        st, d = await self.c.submit("gbp.reply_post", {**self.TARGET, "reply_text": text})
        r = d["response"]
        self.assertEqual(r["status"], "awaiting_approval")
        self.assertEqual(self.c and r["approval_id"] is not None, True)
        await asyncio.sleep(0.4)
        self.assertEqual((self.app.clicks, self.app.typed, self.app.posted), ([], [], []), "nothing touches the browser before the owner decides")
        _, v = await self.hub.get("/collab")
        ap = [a for a in v["approvals"] if a["approval_id"] == r["approval_id"]][0]
        self.assertEqual((ap["action_kind"], ap["details"]["parameters"]["reply_text"]), ("gbp_reply", text), "the owner sees the exact text and the review it is for")
        await self.approve(r["approval_id"])
        _, g = await self.c.call("GET", f"/api/collab/requests/{r['request_id']}?wait=20")
        res = g["response"]
        self.assertEqual(res["status"], "succeeded", res.get("error"))
        self.assertEqual((res["result"]["posted"], res["result"]["verified"]), (True, True))
        self.assertEqual(self.app.posted, [("Reviewer 04", text)])
        self.assertEqual(self.app.typed, [text])
        self.assertTrue(any(e["kind"] == "gbp_reply" for e in res["evidence"]))

    async def test_a_denied_reply_never_opens_a_box(self):
        st, d = await self.c.submit("gbp.reply_post", {**self.TARGET, "reply_text": "A reply the owner will refuse."})
        r = d["response"]
        await self.hub.post(f"/approvals/{r['approval_id']}/deny", {})
        _, g = await self.c.call("GET", f"/api/collab/requests/{r['request_id']}?wait=5")
        self.assertEqual((g["response"]["status"], g["response"]["error"]["code"]), ("cancelled", "APPROVAL_DENIED"))
        self.assertEqual((self.app.clicks, self.app.posted), ([], []))

    async def test_an_approved_post_that_hits_a_misplaced_box_posts_nothing(self):
        self.app.misplace_box = True
        st, d = await self.c.submit("gbp.reply_post", {**self.TARGET, "reply_text": "This must never be posted anywhere."})
        r = d["response"]
        await self.approve(r["approval_id"])
        _, g = await self.c.call("GET", f"/api/collab/requests/{r['request_id']}?wait=20")
        self.assertEqual((g["response"]["status"], g["response"]["error"]["code"]), ("failed", "WRONG_REVIEW"))
        self.assertEqual((self.app.posted, self.app.typed), ([], []))

    async def test_the_capabilities_are_declared_honestly_and_posting_needs_its_own_permission(self):
        self.assertEqual((REGISTRY["gbp.reviews"].external_effects, REGISTRY["gbp.reviews"].approval), (False, "never"))
        self.assertEqual((REGISTRY["gbp.reply_check"].external_effects, REGISTRY["gbp.reply_check"].approval), (False, "never"))
        post = REGISTRY["gbp.reply_post"]
        self.assertEqual((post.external_effects, post.approval, post.permissions), (True, "required", ("browser:act", "web:post")))
        self.assertIn("web:post", self.svc.callers.list()[0]["grants"])
        self.svc.callers.set_grants("collaborator", tuple(g for g in self.svc.callers.list()[0]["grants"] if g != "web:post"))
        st, d = await self.c.submit("gbp.reply_post", {**self.TARGET, "reply_text": "No permission, no reply."})
        self.assertEqual(st, 403, d)


if __name__ == "__main__":
    unittest.main()
