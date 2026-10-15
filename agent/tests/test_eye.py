"""The EYE: compact digests, a dedicated persistent tab, gated hands, and an owner switch. A fake Search-Console-like page (real tab-separated table, toggles, sorting, paging) stands behind the REAL dispatcher."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from octg_agent.collab import eye as E
from octg_agent.collab.capabilities import REGISTRY

from .test_collab import Collab, CollabCase

ROWS = [("example painting co", 8, 118, "6.8%", "1.2"), ("ex pro painting", 3, 126, "2.4%", "1.9"), ("painters montgomery al", 2, 39, "5.1%", "8.4"), ("allpro painting", 1, 7, "14.3%", "1.0"),
        ("drywall near me", 1, 2, "50%", "11"), ("exterior painting", 0, 130, "0%", "31"), ("interior painting", 0, 116, "0%", "28"), ("stucco painters", 0, 108, "0%", "44"),
        ("painter", 0, 103, "0%", "52"), ("stucco painter", 0, 71, "0%", "39"), ("house painting", 0, 66, "0%", "35"), ("painting contractors", 0, 64, "0%", "41"), ("cabinet painting", 0, 51, "0%", "27"),
        ("commercial painters", 0, 44, "0%", "33"), ("pressure washing", 0, 40, "0%", "29"), ("painters near me", 0, 38, "0%", "24"), ("paint colors", 0, 30, "0%", "60"), ("wall painting", 0, 22, "0%", "48"),
        ("deck staining", 0, 19, "0%", "36"), ("fence painting", 0, 15, "0%", "38"), ("popcorn ceiling", 0, 12, "0%", "55"), ("epoxy floors", 0, 9, "0%", "45"), ("wallpaper removal", 0, 6, "0%", "50"),
        ("drywall repair", 0, 5, "0%", "42"), ("texture walls", 0, 3, "0%", "58")]
JPG = b"\xff\xd8\xff\xd9"


class FakeApp:
    """A page that behaves like an analytics report: metric toggles, a sortable table with paging, a filter box, and some controls the eye must never press."""

    def __init__(self):
        self.tabs, self.n, self.snap = {}, 40, 0
        self.sort, self.ctr, self.pos, self.page, self.per = "clicks", False, False, 0, 10
        self.url = "https://reports.example.test/performance"
        self.received: list[tuple[str, dict]] = []
        self.closed: list[int] = []
        self.elements: dict[str, dict] = {}
        self.filter_text = ""
        self.goto_paypal = False
        self.hits = []

    def rows(self):
        key = {"clicks": 1, "impressions": 2}[self.sort]
        data = sorted(ROWS, key=lambda r: (-r[key], r[0]))
        if self.filter_text:
            data = [r for r in data if self.filter_text.lower() in r[0]]
        return data

    def build(self):
        data = self.rows()
        start = self.page * self.per
        chunk = data[start:start + self.per]
        header = ["Top queries", "Clicks", "Impressions"] + (["CTR"] if self.ctr else []) + (["Position"] if self.pos else [])
        lines = ["help_outline", "Help", "manage_accounts", "User settings", "Performance", "keyboard_arrow_down", "EXPORT", "24 hours", "7 days", "3 months", "Search type: Web", "Search type: Web",
                 "Total clicks", "63", "Total impressions", "2.65K", "Average CTR", "2.4%", "Average position", "20", "QUERIES", "PAGES", "COUNTRIES", ""] + header + [""]
        for r in chunk:
            cells = [r[0], str(r[1]), str(r[2])] + ([r[3]] if self.ctr else []) + ([r[4]] if self.pos else [])
            lines.append("\t".join(cells))
        lines += ["Rows per page:", "5", "10", "25", f"{start + 1}-{start + len(chunk)} of {len(data)}", "Previous page", "Next page"]
        els = [("button", "Average CTR", {"checked": self.ctr}), ("button", "Average position", {"checked": self.pos}), ("columnheader", "Clicks", {}), ("columnheader", "Impressions", {}),
               ("button", "Next page", {"disabled": (start + self.per) >= len(data)}), ("button", "Previous page", {"disabled": self.page == 0}), ("textbox", "Filter table", {"type": "text"}),
               ("textbox", "Password", {"type": "password", "sensitive": True}), ("button", "Delete property", {}), ("button", "Save", {}), ("button", "Export", {}), ("link", "Go to PayPal", {"href": "https://www.paypal.com/pay"}), ("link", "Partner site", {"href": "https://mail.google.com/inbox"}),
               ("button", "Rows per page: 10", {}), ("button", "10", {}), ("button", "10", {})]
        self.snap += 1
        self.elements = {}
        out = []
        for i, (role, name, extra) in enumerate(els, 1):
            eid = f"el_{i:03d}"
            self.elements[eid] = {"id": eid, "tag": "div", "role": role, "name": name, "in_viewport": True, **extra}
            out.append(self.elements[eid])
        return "\n".join(lines), out

    def handlers(self):
        def new_tab(p, c):
            self.n += 1
            self.tabs[self.n] = p["url"]
            self.url = p["url"]
            return {"result": {"tab": {"tab_id": self.n, "url": p["url"], "status": "complete"}}}

        def navigate(p, c):
            self.url = p["url"]
            return {"result": {"url": p["url"]}}

        def read(p, c):
            text, _ = self.build()
            return {"result": {"text": text, "url": "https://mail.google.com/inbox" if self.goto_paypal else self.url, "title": "Performance", "total_chars": len(text)}}

        def inspect(p, c):
            _, els = self.build()
            return {"result": {"snapshot_id": f"sn{self.snap}", "tab_id": p.get("tab_id"), "url": self.url, "elements": els, "page": {"headings": []}}}

        def click(p, c):
            self.received.append(("click", dict(p)))
            name = self.elements[p["element_id"]]["name"]
            if name == "Average CTR":
                self.ctr = not self.ctr
            elif name == "Average position":
                self.pos = not self.pos
            elif name == "Impressions":
                self.sort = "impressions"
            elif name == "Clicks":
                self.sort = "clicks"
            elif name == "Next page":
                self.page += 1
            elif name == "Previous page":
                self.page -= 1
            elif name in ("Go to PayPal", "Partner site"):
                self.goto_paypal = True
            elif name in ("Delete property", "Save"):
                self.hits.append(name)
            return {"result": {"clicked": True}}

        def typ(p, c):
            self.received.append(("type", dict(p)))
            if self.elements[p["element_id"]]["name"] == "Filter table":
                self.filter_text = p["text"]
            return {"result": {"typed": True}}

        def press(p, c):
            self.received.append(("press_key", dict(p)))
            return {"result": {}}

        def back(p, c):
            self.received.append(("back", dict(p)))
            self.goto_paypal = False
            return {"result": {}}

        def close(p, c):
            self.closed.append(p["tab_id"])
            return {"result": {"closed": True}}
        return {"new_tab": new_tab, "navigate": navigate, "read_page": read, "inspect_page": inspect, "click": click, "type": typ, "press_key": press, "back": back, "close_tab": close,
                "wait": lambda p, c: {"result": {}}}


class PureTests(unittest.TestCase):
    def test_icon_words_and_blank_lines_are_dropped_but_table_rows_keep_their_tabs(self):
        lines = E.clean_lines("help_outline\nHelp\n\nkeyboard_arrow_down\nSearch type: Web\nSearch type: Web\nhome\nexample painting co\t8\t118\n")
        self.assertEqual(lines, ["Help", "Search type: Web", "example painting co\t8\t118"])

    def test_tables_are_found_with_their_header_and_title(self):
        text = "Intro\nTop queries\nClicks\nImpressions\n\na\t1\t10\nb\t2\t20\nRows per page:\n5"
        t = E.parse_tables(E.clean_lines(text))
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["header"], t[0]["rows"]), (["Top queries", "Clicks", "Impressions"], [["a", "1", "10"], ["b", "2", "20"]]))

    def test_a_control_listed_twice_with_its_value_in_the_label_is_one_target_and_the_real_button_wins(self):
        els = {"el_1": {"id": "el_1", "name": "Average CTR 2.4% ", "role": "clickable", "tag": "div"}, "el_2": {"id": "el_2", "name": "Average CTR 2.4%", "role": "button", "tag": "div"},
               "el_3": {"id": "el_3", "name": "Average position 20", "role": "button"}}
        self.assertEqual(E.resolve_target(els, "Average CTR")[0]["id"], "el_2")
        self.assertEqual(E.resolve_target(els, "average position")[0]["id"], "el_3")
        self.assertIsNone(E.resolve_target(els, "Average")[0])

    def test_controls_leave_out_menu_links_and_icon_words(self):
        els = [{"id": "el_1", "name": "home Overview", "role": "link", "href": "https://x/"}, {"id": "el_2", "name": "file_copy Pages", "role": "link"}, {"id": "el_3", "name": "keyboard_arrow_down Rows per page", "role": "button"},
               {"id": "el_4", "name": "EXPORT", "role": "button"}, {"id": "el_5", "name": "EXPORT", "role": "button"}]
        self.assertEqual([c["name"] for c in E.compact_controls(els)], ["Rows per page", "EXPORT"])
        self.assertEqual([c["name"] for c in E.compact_controls(els, links=True)][:2], ["Overview", "Pages"])

    def test_a_diff_reports_only_what_is_new(self):
        added, gone = E.diff_lines(["a", "b", "c"], ["a", "c", "d", "e"])
        self.assertEqual((added, gone), (["d", "e"], 1))

    def test_targets_resolve_by_ref_exact_label_or_contains_and_ambiguity_is_reported(self):
        els = {"el_1": {"id": "el_1", "name": "Average CTR", "role": "button"}, "el_2": {"id": "el_2", "name": "Average position", "role": "button"}, "el_3": {"id": "el_3", "name": "Next page", "role": "button", "disabled": True}}
        self.assertEqual(E.resolve_target(els, "el_2")[0]["name"], "Average position")
        self.assertEqual(E.resolve_target(els, "average ctr")[0]["id"], "el_1")
        self.assertIsNone(E.resolve_target(els, "ctr")[0], "a partial label is offered, never pressed")
        self.assertEqual([c["ref"] for c in E.resolve_target(els, "ctr")[1]], ["el_1"])
        self.assertEqual(E.resolve_target(els, "ctr", loose=True)[0]["id"], "el_1")
        el, cands = E.resolve_target(els, "average")
        self.assertIsNone(el)
        self.assertEqual({c["ref"] for c in cands}, {"el_1", "el_2"})
        self.assertEqual(E.resolve_target(els, "Next page"), (None, []), "a disabled control is not a target")

    def test_the_policy_blocks_sites_follows_the_owner_switch_and_persists(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            pol = E.EyePolicy(d)
            pol.check("https://search.google.com/search-console")
            for bad in ("https://www.paypal.com/x", "https://mail.google.com/mail", "https://login.chase.com", "file:///C:/x", "chrome://extensions"):
                with self.assertRaises(E.EyeError, msg=bad):
                    pol.check(bad)
            pol.deny("https://secret.example.org/page")
            with self.assertRaises(E.EyeError):
                E.EyePolicy(d).check("https://app.secret.example.org/")
            pol.allow("wise.com")
            pol.check("https://wise.com/home")
            pol.set_enabled(False)
            with self.assertRaises(E.EyeError) as e:
                pol.check("https://example.com")
            self.assertEqual(e.exception.code, "EYE_DISABLED")

    def test_the_gate_refuses_changes_personal_fields_and_odd_typing_but_allows_looking(self):
        calm = {"approval": "never", "reasons": [], "tags": []}
        E.gate_action("click", {"name": "Impressions"}, calm, params={}, last_was_type=False)
        E.gate_action("type", {"name": "Filter table", "type": "text"}, calm, params={}, last_was_type=False)
        E.gate_action("key", None, calm, params={"key": "Enter"}, last_was_type=True)
        for action, el, risk, params, last in (("click", {"name": "Delete"}, {"approval": "required", "reasons": ["x"], "tags": []}, {}, False),
                                                ("type", {"name": "Password", "type": "password"}, calm, {}, False), ("type", {"name": "Email address", "type": "email"}, calm, {}, False),
                                                ("type", {"name": "Message body", "type": "text"}, calm, {}, False), ("key", None, calm, {"key": "Enter"}, False), ("key", None, calm, {"key": "Delete"}, True),
                                                ("click", {"name": "Place order"}, {"approval": "contextual", "tags": ["form_submit"], "reasons": []}, {}, False)):
            with self.assertRaises(E.EyeError, msg=(action, el)) as e:
                E.gate_action(action, el, risk, params=params, last_was_type=last)
            self.assertEqual(e.exception.code, "NEEDS_OWNER")
        E.gate_action("click", {"name": "Search"}, {"approval": "contextual", "tags": ["form_submit"], "reasons": []}, params={}, last_was_type=False)


class EyeTests(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = FakeApp()
        self.ext = await self.h.extension(handlers=self.app.handlers())

    async def go(self, cap, params=None, **kw):
        return await self.c.run(cap, params or {}, wait=60, **kw)

    async def ok(self, cap, params=None, **kw):
        r = await self.go(cap, params, **kw)
        self.assertEqual(r["status"], "succeeded", (cap, r.get("error")))
        return r["result"]

    async def refused(self, cap, params, code):
        r = await self.go(cap, params)
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", code), r)
        return r

    async def test_opening_a_page_returns_a_small_digest_with_the_table_as_rows_and_the_useful_controls(self):
        d = await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        t = d["tables"][0]
        self.assertEqual(t["header"], ["Top queries", "Clicks", "Impressions"])
        self.assertEqual((t["row_count"], t["rows"][0]), (10, ["example painting co", "8", "118"]))
        self.assertEqual(d["page_info"], "1-10 of 25")
        names = [c["name"] for c in d["controls"]]
        for needed in ("Average CTR", "Average position", "Impressions", "Next page"):
            self.assertIn(needed, names)
        self.assertNotIn("Password", names, "sensitive fields are never listed")
        self.assertNotIn("help_outline", d["text"])
        self.assertLess(len(json.dumps(d)), 4200, "a whole report page stays a few hundred tokens")
        self.assertEqual(d["steps_used"], 1)

    async def test_the_owner_flow_toggle_columns_sort_and_read_the_top_rows_in_the_same_tab(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        a = await self.ok("eye.act", {"do": "click", "target": "Average CTR"})
        self.assertTrue(a["acted"] and a["changed"])
        self.assertEqual(a["table"]["header"], ["Top queries", "Clicks", "Impressions", "CTR"])
        await self.ok("eye.act", {"do": "click", "target": "Average position"})
        s = await self.ok("eye.act", {"do": "click", "target": "Impressions"})
        self.assertEqual(s["table"]["first_rows"][0][:3], ["exterior painting", "0", "130"], "now sorted by impressions")
        r = await self.ok("eye.table", {"all_pages": True, "max_rows": 25})
        self.assertEqual((r["header"], r["row_count"], r["pages_read"], r["page_info"], r["complete"]), (["Top queries", "Clicks", "Impressions", "CTR", "Position"], 25, 3, "21-25 of 25", True))
        self.assertEqual(r["rows"][0], ["exterior painting", "0", "130", "0%", "31"])
        imps = [int(x[2]) for x in r["rows"]]
        self.assertEqual(imps, sorted(imps, reverse=True))
        self.assertEqual(len({tuple(x) for x in r["rows"]}), 25, "no row twice")
        tabs = {n for n in self.app.tabs}
        self.assertEqual(len(tabs), 1, "everything happened in ONE dedicated tab")
        self.assertTrue(all(p["tab_id"] in tabs for _, p in self.app.received))

    async def test_the_table_stops_at_the_row_limit_and_says_it_is_not_complete(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        r = await self.ok("eye.table", {"all_pages": True, "max_rows": 12})
        self.assertEqual((r["row_count"], r["complete"]), (12, False))
        r = await self.ok("eye.table", {})
        self.assertEqual(r["complete"], False, "only the rows on screen: there is a next page")

    async def test_typing_is_allowed_only_in_a_filter_box_and_enter_only_right_after(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        await self.refused("eye.act", {"do": "key", "key": "Enter"}, "NEEDS_OWNER")
        t = await self.ok("eye.act", {"do": "type", "target": "Filter table", "text": "stucco"})
        self.assertTrue(t["acted"])
        self.assertEqual(self.app.filter_text, "stucco")
        self.assertEqual(t["table"]["row_count"], 2)
        await self.ok("eye.act", {"do": "key", "key": "Enter"})
        await self.refused("eye.act", {"do": "key", "key": "Enter"}, "NEEDS_OWNER")              # Enter is not allowed twice without typing again
        await self.refused("eye.act", {"do": "type", "target": "Password", "text": "hunter2"}, "NEEDS_OWNER")
        self.assertEqual([k for k, _ in self.app.received if k == "type"], ["type"], "the password field never received anything")

    async def test_anything_that_looks_like_a_change_is_refused_and_never_reaches_the_browser(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        for label in ("Delete property", "Save"):
            r = await self.refused("eye.act", {"do": "click", "target": label}, "NEEDS_OWNER")
            self.assertIn("owner.ask", r["error"]["message"])
        self.assertEqual(self.app.hits, [])
        self.assertEqual([p for k, p in self.app.received if k == "click"], [])
        await self.refused("eye.act", {"do": "key", "key": "Delete"}, "NEEDS_OWNER")

    async def test_a_target_that_is_missing_or_ambiguous_acts_on_nothing_and_offers_candidates(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        a = await self.ok("eye.act", {"do": "click", "target": "Average"})
        self.assertEqual((a["acted"], a["problem"], sorted(c["name"] for c in a["candidates"])), (False, "ambiguous", ["Average CTR", "Average position"]))
        b = await self.ok("eye.act", {"do": "click", "target": "Nonexistent control"})
        self.assertEqual((b["acted"], b["problem"]), (False, "not_found"))
        self.assertEqual(self.app.received, [])

    async def test_blocked_sites_are_refused_to_open_and_a_link_that_leads_to_one_is_undone(self):
        await self.refused("eye.open", {"url": "https://www.paypal.com/myaccount"}, "SITE_BLOCKED")
        self.assertEqual(self.app.tabs, {}, "no tab was opened")
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        await self.refused("eye.act", {"do": "click", "target": "Go to PayPal"}, "NEEDS_OWNER")      # a payment-looking link is refused before it is pressed
        self.assertFalse(self.app.goto_paypal)
        r = await self.refused("eye.act", {"do": "click", "target": "Partner site"}, "SITE_BLOCKED")  # a harmless-looking link that LANDS on a blocked site is undone
        self.assertIn("blocked", r["error"]["message"])
        self.assertIn("back", [k for k, _ in self.app.received], "it went back off the blocked page")
        self.assertFalse(self.app.goto_paypal)

    async def test_the_owner_switch_turns_the_eye_off_at_once(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        self.svc.eye.policy.set_enabled(False)
        await self.refused("eye.open", {"url": "https://reports.example.test/other"}, "EYE_DISABLED")
        st = await self.ok("eye.status")
        self.assertFalse(st["enabled"])
        self.svc.eye.policy.set_enabled(True)
        self.assertTrue((await self.ok("eye.status"))["enabled"])

    async def test_sessions_are_limited_private_and_cleaned_up(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/a"})
        r = await self.refused("eye.look", {"session": "other"}, "NO_SESSION")
        for i in range(2):
            await self.ok("eye.open", {"url": f"https://reports.example.test/s{i}", "session": f"s{i}"})
        await self.refused("eye.open", {"url": "https://reports.example.test/x", "session": "s9"}, "TOO_MANY_SESSIONS")
        self.svc.callers.ensure("other-ai")
        other = Collab(self.h, token=self.svc.callers.token_path("other-ai").read_text().strip())
        got = await other.run("eye.look", {}, wait=30)
        self.assertEqual((got["status"], got["error"]["code"]), ("failed", "NO_SESSION"), "another caller cannot use my tab")
        self.svc.eye.items[self.svc.eye.key("collaborator", "main")]["last_used"] = time.time() - 7200
        self.assertEqual(await self.svc.eye.gc(), 1)
        self.assertEqual(len(self.app.closed), 1)
        await self.refused("eye.look", {}, "NO_SESSION")
        self.assertTrue((await self.ok("eye.close", {"session": "s0"}))["closed"])
        self.assertFalse((await self.ok("eye.close", {"session": "s0"}))["closed"])

    async def test_a_session_has_a_step_limit(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        self.svc.eye.items[self.svc.eye.key("collaborator", "main")]["steps"] = E.MAX_STEPS
        await self.refused("eye.act", {"do": "click", "target": "Impressions"}, "STEP_LIMIT")

    async def test_look_can_find_words_and_every_step_is_recorded_as_evidence(self):
        await self.ok("eye.open", {"url": "https://reports.example.test/performance"})
        r = await self.ok("eye.look", {"controls": False, "find": ["painters montgomery", "nothing here"]})
        self.assertEqual(r["found"]["painters montgomery"], ["painters montgomery al\t2\t39"])
        self.assertEqual(r["found"]["nothing here"], [])
        self.assertNotIn("controls", r)
        act = await self.go("eye.act", {"do": "click", "target": "Impressions"})
        self.assertTrue(act["evidence"] and act["evidence"][0]["kind"] == "eye_step")

    async def test_the_permission_is_part_of_the_standard_collaborator_and_a_caller_without_it_is_refused(self):
        self.assertIn("browser:act", self.svc.callers.list()[0]["grants"])
        self.svc.callers.set_grants("collaborator", tuple(g for g in self.svc.callers.list()[0]["grants"] if g != "browser:act"))
        st, d = await self.c.submit("eye.open", {"url": "https://reports.example.test/a"})
        self.assertEqual(st, 403)
        for cap in ("eye.open", "eye.look", "eye.act", "eye.table", "eye.close", "eye.status"):
            c = REGISTRY[cap]
            self.assertEqual((c.approval, c.external_effects, c.permissions), ("never", False, ("browser:act",)))


class NotConnectedTests(CollabCase):
    async def test_without_chrome_the_eye_says_so(self):
        r = await self.c.run("eye.open", {"url": "https://reports.example.test/a"}, wait=30)
        self.assertEqual((r["status"], r["error"]["code"], r["retryable"]), ("failed", "EXTENSION_NOT_CONNECTED", True))


if __name__ == "__main__":
    unittest.main()
