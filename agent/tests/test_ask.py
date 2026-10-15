"""The "Ask the agent" feature: a plain sentence becomes Google searches (read-only), and the answer comes back in plain language. Fake Google behind the real dispatcher; nothing here
proves behaviour against live Google beyond what the rank-check live run already showed."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from octg_agent.collab.caps import rank, searchreport
from octg_agent.collab.intent import parse_search_request
from octg_agent.seo import serp as SERP

from .test_collab import CollabCase

SITE = "example-painting.test"
JPG = b"\xff\xd8\xff\xd9"


def surl(q: str) -> str:
    return SERP.build_url("google", q, country="us", language="en")[0]


class FakeSerp:
    """pages: requested URL -> {"results": [(title, domain)], "businesses": [(name, rating_line)], "paa": bool}; redirects: requested URL -> loaded URL."""

    def __init__(self, pages: dict, redirects: dict | None = None):
        self.pages, self.redirects, self.opened, self.tabs, self.n = pages, redirects or {}, [], {}, 700
        self.tmp = tempfile.TemporaryDirectory()

    def final(self, tid):
        u = self.tabs[tid]
        return self.redirects.get(u, u)

    def page(self, tid):
        return self.pages.get(self.tabs[tid], {"results": [], "businesses": [], "paa": False})

    def handlers(self):
        def new_tab(p, cmd):
            self.n += 1
            self.opened.append(p["url"])
            self.tabs[self.n] = p["url"]
            return {"result": {"tab": {"tab_id": self.n, "url": p["url"], "status": "complete"}}}

        def state(p, cmd):
            pg = self.page(p["tab_id"])
            heads = [{"level": 1, "text": "Search Results"}, {"level": 3, "text": "More businesses"}, {"level": 3, "text": "Map"}] + [{"level": 3, "text": t} for t, _d in pg["results"]]
            return {"result": {"tab": {}, "page": {"url": self.final(p["tab_id"]), "title": "r", "headings": heads}}}

        def read(p, cmd):
            pg = self.page(p["tab_id"])
            lines = ["Skip to main content", "All", "Maps", "Search Results", "Montgomery, AL"]
            if pg["businesses"]:
                lines.append("Businesses")
                for name, rating in pg["businesses"]:
                    lines += [name, rating + " · Painter", "Open · Closes 7 PM · (555) 010-0100", '"They did a wonderful job."', "Website"]
                lines += ["More businesses", "Map"]
            lines.append("Web results")
            for t, d in pg["results"]:
                lines += [t, "", d, f"https://{d}", "Snippet text about painting services."]
            if pg["paa"]:
                lines += ["People also ask", "What do painters charge?", "Related searches", "x"]
            text = "\n".join(lines)
            return {"result": {"text": text, "url": self.final(p["tab_id"]), "title": "t", "total_chars": len(text)}}

        def inspect(p, cmd):
            pg = self.page(p["tab_id"])
            els = [{"tag": "a", "name": f"{t} https://{d}", "href": f"https://www.{d}/page"} for t, d in pg["results"]]
            return {"result": {"snapshot_id": "s", "tab_id": p["tab_id"], "url": self.final(p["tab_id"]), "elements": [{"id": f"el_{i:03d}", **e} for i, e in enumerate(els, 1)], "page": {"headings": []}}}

        def shot(p, cmd):
            f = Path(self.tmp.name) / f"s{len(list(Path(self.tmp.name).iterdir()))}.jpg"
            f.write_bytes(JPG)
            return {"result": {"path": str(f)}}

        return {"new_tab": new_tab, "get_page_state": state, "read_page": read, "inspect_page": inspect, "screenshot": shot, "close_tab": lambda p, c: {"result": {"closed": True}}, "wait": lambda p, c: {"result": {}}}


RIVALS = [("THE BEST 10 PAINTERS IN MONTGOMERY, AL", "yelp.com"), ("JAIRO'S PAINTING LLC | Home", "jairospaintingllc.com"), ("Chuck's Quality Painting - Montgomery, AL", "chucksqualitypainting.com")]
PACK = [("Chuck's Quality Painting L.L.C", "4.7(60)"), ("T&J's Pro Painting", "4.9(66)"), ("Platinum Paint Co.", "4.8(33)")]
SENTENCE = ("go to Google and uh, search for painters in Alabama and uh, search for best painters in Alabama. Search for affordable painters in Alabama. And uh, tell me what you find out.")


def pages(client_on: dict | None = None):
    """Three searches: the client is on page one for 'best painters in Alabama' only (at #2)."""
    out = {}
    for q in ("painters in Alabama", "best painters in Alabama", "affordable painters in Alabama"):
        res = list(RIVALS)
        if q == (client_on or {}).get("q", "best painters in Alabama"):
            res.insert(1, ("Example Painting Co | Montgomery AL", SITE))
        out[surl(q)] = {"results": res, "businesses": PACK + ([("Example Painting Co", "5.0(7)")] if q.startswith("best") else []), "paa": True}
    return out


class IntentTests(unittest.TestCase):
    def test_the_owners_own_sentence(self):
        got = parse_search_request(SENTENCE)
        self.assertEqual(got["queries"], ["painters in Alabama", "best painters in Alabama", "affordable painters in Alabama"])
        self.assertEqual(got["notes"], [])

    def test_other_phrasings(self):
        for text, want in (('search Google for "painters montgomery al", "house painters montgomery al" and tell me where my website is', ["painters montgomery al", "house painters montgomery al"]),
                           ("Please search for:\n- a b c\n- d e f\nand tell me what you see", ["a b c", "d e f"]),
                           ("google painters in Alabama then google best painters in Alabama, then report back", ["painters in Alabama", "best painters in Alabama"]),
                           ("look up emergency towing buffalo ny and tell me who is on the first page", ["emergency towing buffalo ny"]),
                           ("search for plumbers on Google and tell me the top three", ["plumbers"])):
            self.assertEqual(parse_search_request(text)["queries"], want, text)

    def test_nothing_to_search_is_said_not_guessed(self):
        for text in ("go to google and tell me what you find", "painters in Alabama", "", "hello"):
            got = parse_search_request(text)
            self.assertEqual(got["queries"], [], text)
            self.assertTrue(got["notes"], text)

    def test_duplicates_and_the_limit(self):
        self.assertEqual(parse_search_request("search for a cat. Search for A CAT. search for dogs")["queries"], ["a cat", "dogs"])
        many = " ".join(f"search for thing number {i}." for i in range(14))
        got = parse_search_request(many)
        self.assertEqual(len(got["queries"]), 10)
        self.assertIn("first 10", got["notes"][0])


class ReportTests(unittest.TestCase):
    def row(self, **kw):
        base = {"query": "painters in Alabama", "status": "read", "observed_at": 1.0, "client_position": None, "client_url": None,
                "results": [{"position": 1, "title": "Yelp list", "domain": "yelp.com", "url": "u", "class": "directory"}, {"position": 2, "title": "Jairo", "domain": "jairos.com", "url": "u", "class": "competitor"}],
                "competitors": ["jairos.com"], "directories": ["yelp.com"], "map": [{"position": 1, "name": "Chuck's", "rating": 4.7, "review_count": 60}], "client_in_map": None, "features": ["map_pack", "people_also_ask"]}
        return {**base, **kw}

    def test_plain_language_answer(self):
        text = searchreport.compose([self.row(), self.row(query="best painters", client_position=3, client_url="https://x/", client_in_map=2)], site="x.com", brand="Acme", location=None, ran_at=1.7e9, stopped=None, remaining=[])
        self.assertIn("I searched Google for 2 phrases", text)
        self.assertIn("was on the first page for 1 of 2: “best painters” (#3)", text)
        self.assertIn("not on the first page", text)
        self.assertIn("Competitors that showed up most: jairos.com (2 of 2)", text)
        self.assertIn("Chuck's (4.7, 60 reviews)", text)
        self.assertIn("Your business (Acme) is in the map listings at #2", text)
        self.assertIn("Your business (Acme) was not among the map listings", text)
        self.assertNotIn("None", text)

    def test_a_stop_says_what_to_do_and_lists_the_rest(self):
        rows = [self.row(), self.row(query="two", status="blocked", results=[], competitors=[], directories=[], map=[]), self.row(query="three", status="not_run", results=[])]
        text = searchreport.compose(rows, site="x.com", brand="Acme", location="Alabama,United States", ran_at=1.7e9, stopped="Google showed a CAPTCHA page on search 2.", remaining=["two", "three"])
        self.assertIn("I ran 1 of 3 searches on Google, then stopped", text)
        self.assertIn("did not try to get around it", text)
        self.assertIn("complete its check yourself", text)
        self.assertIn("“two”; “three”", text)
        self.assertIn("as if searching from Alabama,United States", text)

    def test_no_website_on_file_is_stated(self):
        text = searchreport.compose([self.row()], site="", brand="Acme", location=None, ran_at=1.7e9, stopped=None, remaining=[])
        self.assertIn("No website is on file", text)
        self.assertNotIn("not on the first page", text)

    def test_map_block_is_cut_above_the_web_results(self):
        t = "Businesses\nA Co\n4.5(10) · Painter\nMore businesses\nWeb results\nSomething\nYelp\n3.9(53)\nSnippet"
        self.assertNotIn("3.9", searchreport.pack_text(t))


class AskTests(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cid = await self.new_client("Example Painting Co", website="https://example-painting.test/")
        self.pauses: list[float] = []

        async def fake_pause(ctx, seconds):
            self.pauses.append(seconds)
        p = mock.patch.object(rank, "_pause", fake_pause)
        p.start()
        self.addCleanup(p.stop)

    async def ask(self, text=SENTENCE, **kw):
        return await self.hub.post("/asks", {"text": text, "client_id": self.cid, **kw})

    async def wait_ask(self, until=("succeeded", "failed", "cancelled", "timed_out"), timeout=30):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            _, d = await self.hub.get("/asks")
            if d["asks"] and d["asks"][0]["status"] in until:
                return d["asks"][0]
            await asyncio.sleep(0.2)
        self.fail(f"the ask did not finish: {d}")

    async def test_the_owners_sentence_runs_three_paced_searches_and_comes_back_in_plain_language(self):
        g = FakeSerp(pages())
        await self.h.extension(handlers=g.handlers())
        st, d = await self.ask()
        self.assertEqual(st, 200, d)
        self.assertTrue(d["understood"])
        self.assertEqual(d["queries"], ["painters in Alabama", "best painters in Alabama", "affordable painters in Alabama"])
        a = await self.wait_ask()
        self.assertEqual((a["status"], a["outcome"]), ("succeeded", "complete"))
        self.assertEqual(g.opened, [surl(q) for q in d["queries"]])            # exactly those searches, in order
        self.assertEqual(len(self.pauses), 2)
        self.assertTrue(all(rank.PACE[0] <= s <= rank.PACE[1] for s in self.pauses))
        r = a["report"]
        self.assertIn("I searched Google for 3 phrases", r)
        self.assertIn(f"Your website ({SITE}) was on the first page for 1 of 3: “best painters in Alabama” (#2)", r)
        self.assertIn("not on the first page", r)
        self.assertIn("jairospaintingllc.com", r)
        self.assertIn("Chuck's Quality Painting L.L.C (4.7, 60 reviews)", r)
        self.assertIn("yelp.com", r)
        self.assertIn("Your business (Example Painting Co) is in the map listings at #4", r)
        self.assertEqual([s["client_position"] for s in a["searches"]], [None, 2, None])
        self.assertEqual(a["remaining"], [])
        self.assertEqual(a["client_name"], "Example Painting Co")
        obs = self.svc.admin().research.observations(self.cid, kind="keyword_rank", limit=20)
        self.assertEqual(sorted((o["query"], o["position"]) for o in obs), [("affordable painters in Alabama", None), ("best painters in Alabama", 2), ("painters in Alabama", None)])
        # the task closed itself, attributed to the owner's page, and it never had more rights than a read-only search
        task = self.svc.store.list_tasks(5)[0]
        self.assertEqual((task["caller"], task["status"]), ("owner-ui", "completed"))

    async def test_a_sentence_without_a_search_is_answered_not_run(self):
        g = FakeSerp(pages())
        await self.h.extension(handlers=g.handlers())
        st, d = await self.ask("go to google and tell me what you find")
        self.assertEqual((st, d["understood"]), (200, False))
        self.assertIn("search for", d["reply"])
        self.assertEqual(g.opened, [])
        _, lst = await self.hub.get("/asks")
        self.assertEqual(lst["asks"], [])

    async def test_a_captcha_stops_the_run_and_try_again_runs_only_what_is_left(self):
        pg = pages()
        q2 = surl("best painters in Alabama")
        g = FakeSerp(pg, redirects={q2: "https://www.google.com/sorry/index?continue=" + q2})
        await self.h.extension(handlers=g.handlers())
        await self.ask()
        a = await self.wait_ask()
        self.assertEqual((a["status"], a["outcome"]), ("succeeded", "blocked"))
        self.assertEqual(a["remaining"], ["best painters in Alabama", "affordable painters in Alabama"])
        self.assertEqual(len(g.opened), 2, "nothing was opened after the wall")
        self.assertIn("complete its check yourself", a["report"])
        self.assertIn("I ran 1 of 3 searches on Google, then stopped", a["report"])
        g.redirects.clear()                                                        # the owner completed Google's check in Chrome
        st, d = await self.hub.post("/asks", {"text": a["text"], "client_id": self.cid, "queries": a["remaining"]})
        self.assertEqual((st, d["queries"]), (200, a["remaining"]))
        end = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < end:
            _, lst = await self.hub.get("/asks")
            if lst["asks"][0]["ask_id"] != a["ask_id"] and lst["asks"][0]["status"] == "succeeded":
                break
            await asyncio.sleep(0.2)
        again = lst["asks"][0]
        self.assertEqual((again["status"], again["outcome"], again["queries"]), ("succeeded", "complete", a["remaining"]))
        self.assertEqual(len(g.opened), 4)

    async def test_only_one_search_run_at_a_time(self):
        gate = asyncio.Event()

        async def slow_pause(ctx, seconds):
            await gate.wait()
        g = FakeSerp(pages())
        await self.h.extension(handlers=g.handlers())
        with mock.patch.object(rank, "_pause", slow_pause):
            await self.ask()
            await asyncio.sleep(0.5)
            st, d = await self.ask()
            self.assertEqual((st, d["code"]), (409, "BUSY"))
            gate.set()
            await self.wait_ask()

    async def test_without_chrome_connected_the_answer_says_so(self):
        await self.ask()
        a = await self.wait_ask()
        self.assertEqual((a["status"], a["error_code"]), ("failed", "EXTENSION_NOT_CONNECTED"))

    async def test_the_client_is_required_and_must_exist(self):
        st, d = await self.hub.post("/asks", {"text": SENTENCE})
        self.assertEqual((st, d["code"]), (400, "CLIENT_REQUIRED"))
        st, d = await self.hub.post("/asks", {"text": SENTENCE, "client_id": "cli_nope"})
        self.assertFalse(d["ok"])

    async def test_a_client_without_a_website_is_told_so_not_guessed_about(self):
        other = await self.new_client("No Site Co")
        g = FakeSerp(pages())
        await self.h.extension(handlers=g.handlers())
        await self.hub.post("/asks", {"text": "search for painters in Alabama", "client_id": other})
        a = await self.wait_ask()
        self.assertIn("No website is on file", a["report"])
        self.assertEqual(a["searches"][0]["client_position"], None)

    async def test_the_owner_page_can_only_run_read_only_searches(self):
        from octg_agent.collab.capabilities import REGISTRY
        from octg_agent.hub import routes
        cap = REGISTRY[routes.ASK_CAP]
        self.assertEqual((cap.approval, cap.external_effects), ("never", False))
        self.assertTrue(set(cap.permissions) <= set(routes.ASK_GRANTS))
        self.assertNotIn("agent:task", routes.ASK_GRANTS)
        self.assertNotIn("approval:request", routes.ASK_GRANTS)


if __name__ == "__main__":
    unittest.main()
