"""seo.rank_check: URLs opened verbatim, location verified, pacing, abort on a wall, null for not-found, local-finder reviews, history with deltas. Everything runs against a fake Google behind the
real dispatcher; nothing here proves the parser against live Google (the spec's control keywords do that on the first real run)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from octg_agent.collab.capabilities import REGISTRY
from octg_agent.collab.caps import rank

from .test_collab import CollabCase

UULE = "w+CAIQICIgTW9udGdvbWVyeSxBbGFiYW1hLFVuaXRlZCBTdGF0ZXM"
SITE = "example-painting.test"
JPG = b"\xff\xd8\xff\xd9"


def gurl(q: str, tbm: bool = False, uule: str | None = UULE) -> str:
    return f"https://www.google.com/search?q={q.replace(' ', '+')}&gl=us&hl=en&pws=0" + ("&tbm=lcl" if tbm else "") + (f"&uule={uule}" if uule else "")


def doc(keywords=("painters montgomery al", "pressure washing montgomery al")) -> dict:
    return {"schema": "octg.rankcheck.input/1", "client": "example-painting", "site": SITE, "brand_name": "Example Painting Co",
            "locale": {"city": "Montgomery", "region": "Alabama", "country": "US", "uule": UULE},
            "keywords": [{"keyword": k, "target_page": "montgomery-al.php" if i == 0 else "pressure-washing.php", "rationale": "x", "web_url": gurl(k), "map_url": gurl(k, tbm=True)} for i, k in enumerate(keywords)]}


def web(results, paa=True):
    return {"kind": "web", "results": results, "paa": paa}


def mapp(businesses):
    return {"kind": "map", "businesses": businesses}


class FakeGoogle:
    """pages: requested URL -> page spec; redirects: requested URL -> the URL that 'loaded'."""

    def __init__(self, pages: dict, redirects: dict | None = None):
        self.pages, self.redirects, self.opened, self.tabs, self.n = pages, redirects or {}, [], {}, 900
        self.tmp = tempfile.TemporaryDirectory()

    def final(self, tid):
        u = self.tabs[tid]
        return self.redirects.get(u, u)

    def page(self, tid):
        return self.pages.get(self.tabs[tid], {"kind": "web", "results": [], "paa": False})

    def handlers(self):
        def new_tab(p, cmd):
            self.n += 1
            self.opened.append(p["url"])
            self.tabs[self.n] = p["url"]
            return {"result": {"tab": {"tab_id": self.n, "url": p["url"], "status": "complete"}}}

        def state(p, cmd):
            pg, heads = self.page(p["tab_id"]), []
            if pg["kind"] == "web":
                heads = [{"level": 3, "text": t} for t, _d in pg["results"]]
            else:
                heads = [{"level": 3, "text": b[0]} for b in pg["businesses"]]
            return {"result": {"tab": {}, "page": {"url": self.final(p["tab_id"]), "title": "r", "headings": heads}}}

        def read(p, cmd):
            pg = self.page(p["tab_id"])
            if pg["kind"] == "web":
                text = "Results\n" + ("People also ask\nWhat does it cost?\nRelated searches\nfoo\n" if pg["paa"] else "")
            else:
                lines = ["Local results", "Rating", "Hours"]
                for i, (name, fmt, cat) in enumerate(pg["businesses"]):
                    if name.startswith("Sponsored:"):
                        lines += ["Sponsored", name.split(":", 1)[1].strip()] + fmt
                    else:
                        lines += [name] + fmt
                    lines += [cat, "Open ⋅ Closes 5 PM", "Website", "Directions"]
                text = "\n".join(lines)
            return {"result": {"text": text, "url": self.final(p["tab_id"]), "title": "t", "total_chars": len(text)}}

        def inspect(p, cmd):
            pg, els = self.page(p["tab_id"]), []
            if pg["kind"] == "web":
                for t, d in pg["results"]:
                    els.append({"tag": "a", "name": f"{t} https://{d}", "href": f"https://www.{d}/page"})
                els.append({"tag": "a", "name": "Some business", "href": "https://www.google.com/maps/place/x"})
            return {"result": {"snapshot_id": "s", "tab_id": p["tab_id"], "url": self.final(p["tab_id"]), "elements": [{"id": f"el_{i:03d}", **e} for i, e in enumerate(els, 1)], "page": {"headings": []}}}

        def shot(p, cmd):
            f = Path(self.tmp.name) / f"s{len(list(Path(self.tmp.name).iterdir()))}.jpg"
            f.write_bytes(JPG)
            return {"result": {"path": str(f)}}

        return {"new_tab": new_tab, "get_page_state": state, "read_page": read, "inspect_page": inspect, "screenshot": shot, "close_tab": lambda p, c: {"result": {"closed": True}},
                "wait": lambda p, c: {"result": {}}}


GOOD_MAP = [("Sponsored: Paid Painters Inc", ["5.0", "(10)"], "Painter · Montgomery, AL"),
            ("T&J's Pro Painting", ["4.9(66)"], "Painter · Montgomery, AL"),
            ("Capital City Painters", ["4.8", "(120)"], "Painter · Montgomery, AL"),
            ("Dixie Coatings", ["4.7 (1.2K)"], "Painter · Montgomery, AL"),
            ("Prime Time Painting", ["4.6(45)"], "Painter · Montgomery, AL"),
            ("Magnolia Painting LLC", ["4.5(30)"], "Painter · Montgomery, AL"),
            ("Example Painting Co", ["4.8(12)"], "Painter · Montgomery, AL")]


def standard_pages(client_at=1, map_pos_list=GOOD_MAP):
    others = [("Rival One | Painters", "rival1.com"), ("Rival Two Painting", "rival2.com"), ("Rival Three Co", "rival3.com")]
    r1 = others[:client_at - 1] + [("Example Painting Co | Montgomery AL", SITE)] + others[client_at - 1:] if client_at else others
    return {gurl("painters montgomery al"): web(r1), gurl("painters montgomery al", tbm=True): mapp(map_pos_list),
            gurl("pressure washing montgomery al"): web(others), gurl("pressure washing montgomery al", tbm=True): mapp([b for b in map_pos_list if not b[0].startswith("All Pro")])}


class PureTests(unittest.TestCase):
    def test_registrable_domain(self):
        for u, want in (("https://www.example-painting.test/x", "example-painting.test"), ("blog.example.com", "example.com"), ("https://shop.example.co.uk/", "example.co.uk"), ("example.com", "example.com")):
            self.assertEqual(rank.registrable(u), want)

    def test_local_finder_parsing_handles_inline_split_and_k_reviews_and_skips_sponsored(self):
        text = "\n".join(["Results", "Sponsored", "Paid Painters Inc", "5.0", "(10)", "Painter · X", "T&J's Pro Painting", "4.9(66)", "Painter · Montgomery", "Capital City Painters", "4.8", "(120)", "Painter",
                          "Dixie Coatings 4.7 (1.2K)", "Prime Time Painting", "4.6 (45)", "No review shop"])
        got = rank.parse_local_finder([{"level": 3, "text": "T&J's Pro Painting"}], text)
        by = {b["name"]: b for b in got}
        self.assertEqual([b["name"] for b in got], ["Paid Painters Inc", "T&J's Pro Painting", "Capital City Painters", "Dixie Coatings", "Prime Time Painting"])
        self.assertTrue(by["Paid Painters Inc"]["sponsored"] and by["Paid Painters Inc"]["position"] is None)
        self.assertEqual([(b["position"], b["rating"], b["review_count"]) for b in got[1:]], [(1, 4.9, 66), (2, 4.8, 120), (3, 4.7, 1200), (4, 4.6, 45)])

    def test_an_unreadable_review_count_stays_none(self):
        got = rank.parse_local_finder([], "Lone Painter\n4.2\nPainter · Town")
        self.assertEqual((got[0]["name"], got[0]["rating"], got[0]["review_count"]), ("Lone Painter", 4.2, None))

    def test_spearman(self):
        self.assertAlmostEqual(rank._spearman([(1, 100), (2, 80), (3, 60), (4, 40)]), -1.0)
        self.assertAlmostEqual(rank._spearman([(1, 10), (2, 20), (3, 30)]), 1.0)
        self.assertIsNone(rank._spearman([(1, 1), (2, 2)]))

    def test_markdown_fallback_parser(self):
        md = "# T\n\n## 1. painters montgomery al\n- **Web results:** " + gurl("painters montgomery al") + "\n- **Map results:** <" + gurl("painters montgomery al", tbm=True) + ">\n\n## 2. x y\n- **Web results:** " + gurl("x y") + "\n"
        got = rank.parse_markdown(md)
        self.assertEqual([k["keyword"] for k in got], ["painters montgomery al", "x y"])
        self.assertEqual(got[0]["web_url"], gurl("painters montgomery al"))
        self.assertEqual(got[0]["map_url"], gurl("painters montgomery al", tbm=True))
        self.assertEqual(got[1]["map_url"], "")

    def test_input_validation_names_every_problem_and_never_rewrites_urls(self):
        d = doc()
        d["keywords"][0]["web_url"] = gurl("painters montgomery al", uule=None)                     # lost the location
        d["keywords"][1]["map_url"] = gurl("pressure washing montgomery al")                          # not a map URL
        d["keywords"].append({"keyword": "other", "web_url": "https://evil.example/search?q=other&uule=" + UULE, "map_url": gurl("zzz", tbm=True)})
        _, problems = rank.prepare(d)
        text = " | ".join(problems)
        self.assertIn("does not carry the location (uule)", text)
        self.assertIn("must have tbm=lcl", text)
        self.assertIn("must be an https://www.google.com/search URL", text)
        self.assertIn("is not the keyword", text)
        ok, none = rank.prepare(doc())
        self.assertEqual(none, [])
        self.assertEqual(ok["keywords"][0]["web_url"], doc()["keywords"][0]["web_url"])              # byte for byte
        self.assertTrue(rank.prepare({**doc(), "locale": {"city": "x"}})[1])                           # no uule: refused


class RankCheckTests(CollabCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cid = await self.new_client("Example Painting Co", website="https://example-painting.test/")
        self.pause = mock.patch.object(rank, "_pause", self._record_pause)
        self.pauses: list[float] = []
        self.pause.start()
        self.addCleanup(self.pause.stop)

    async def _record_pause(self, ctx, seconds):
        self.pauses.append(seconds)

    async def run_check(self, google: FakeGoogle, params: dict | None = None, **kw):
        ext = await self.h.extension(handlers=google.handlers())
        self.ext = ext
        return await self.c.run("seo.rank_check", params or doc(), client=self.cid, wait=60, **kw)

    async def test_urls_are_opened_exactly_pacing_is_applied_and_the_run_is_stored(self):
        g = FakeGoogle(standard_pages())
        r = await self.run_check(g)
        self.assertEqual(r["status"], "succeeded", r)
        d = doc()
        self.assertEqual(g.opened, [d["keywords"][0]["web_url"], d["keywords"][0]["map_url"], d["keywords"][1]["web_url"], d["keywords"][1]["map_url"]])   # verbatim, in order
        self.assertTrue(all(f"uule={UULE}" in u for u in g.opened))
        self.assertEqual(len(self.pauses), 3, "a pause between every two pages, none after the last")
        self.assertTrue(all(rank.PACE[0] <= s <= rank.PACE[1] for s in self.pauses), self.pauses)
        self.assertEqual(rank.PACE, (20.0, 45.0))
        run = r["result"]["run"]
        self.assertEqual((run["schema"], run["status"], run["total"], run["completed"]), ("octg.rankcheck.result/1", "complete", 2, 2))
        self.assertTrue(run["locale_used"]["verified"])
        self.assertEqual(run["locale_used"]["uule"], UULE)
        a, b = run["results"]
        self.assertEqual((a["position"], a["found_url"], a["web_status"]), (1, f"https://www.{SITE}/page", "read"))
        self.assertEqual(a["top3"], [SITE, "rival1.com", "rival2.com"])
        self.assertIn("map_pack", a["serp_features"])
        self.assertIn("people_also_ask", a["serp_features"])
        self.assertIn("related_searches", a["serp_features"])
        # local finder: sponsored left out, client is 6th of the organic listings, every business above it has its reviews
        self.assertEqual(a["map_position"], 6)
        self.assertEqual([c["name"] for c in a["competitors_above"]], ["T&J's Pro Painting", "Capital City Painters", "Dixie Coatings", "Prime Time Painting", "Magnolia Painting LLC"])
        self.assertEqual([c["review_count"] for c in a["competitors_above"]], [66, 120, 1200, 45, 30])
        self.assertEqual(a["competitors_above"][0]["rating"], 4.9)
        self.assertEqual(a["client_map"]["review_count"], 12)
        self.assertTrue(Path(a["evidence"]["web_screenshot"]).is_file() and Path(a["evidence"]["map_screenshot"]).is_file())
        # not found is a valid result and is null, never 0 or 11
        self.assertEqual((b["web_status"], b["position"], b["found_url"], b["map_status"], b["map_position"]), ("read", None, None, "read", None))
        self.assertEqual(len(b["competitors_above"]), 5)
        self.assertNotIn(b["position"], (0, 11))
        # stored: file with the spec's schema, an artifact in the Hub's collab store, research observations
        self.assertEqual(json.loads(Path(r["result"]["json_path"]).read_text(encoding="utf-8"))["run_id"], run["run_id"])
        _, art = await self.c.call("GET", f"/api/collab/artifacts/{r['result']['artifact_id']}")
        self.assertEqual(art["artifact"]["body"]["run_id"], run["run_id"])
        self.assertIn("| Keyword | Web pos |", r["result"]["markdown"])
        obs = self.svc.admin().research.observations(self.cid, kind="keyword_rank", limit=20)
        self.assertEqual(sorted((o["query"], o["position"]) for o in obs), [("painters montgomery al", 1), ("pressure washing montgomery al", None)])

    async def test_the_run_reads_nothing_but_never_clicks_types_or_solves(self):
        g = FakeGoogle(standard_pages())
        r = await self.run_check(g)
        self.assertEqual(r["status"], "succeeded")
        actions = {m["action"] for m in self.ext.received}
        self.assertLessEqual(actions, {"new_tab", "wait", "get_page_state", "read_page", "inspect_page", "screenshot", "close_tab"})

    async def test_a_page_that_lost_the_location_is_not_used_and_the_run_says_so(self):
        d = doc(("painters montgomery al",))
        g = FakeGoogle(standard_pages(), redirects={d["keywords"][0]["web_url"]: gurl("painters montgomery al", uule=None)})
        r = await self.run_check(g, d)
        run = r["result"]["run"]
        a = run["results"][0]
        self.assertEqual((a["web_status"], a["position"], a["found_url"]), ("location_lost", None, None))
        self.assertEqual(a["map_status"], "read")
        self.assertEqual(run["status"], "partial")
        self.assertFalse(run["locale_used"]["verified"])
        self.assertIn("location", a["notes"])
        self.assertEqual(r["completeness"], "partial")

    async def test_an_input_that_lost_the_location_is_refused_before_anything_is_opened(self):
        d = doc()
        d["keywords"][0]["web_url"] = gurl("painters montgomery al", uule=None)
        g = FakeGoogle(standard_pages())
        r = await self.run_check(g, d)
        self.assertEqual((r["status"], r["error"]["code"]), ("failed", "BAD_INPUT"))
        self.assertEqual(g.opened, [])

    async def test_a_captcha_stops_the_whole_run_keeps_the_partial_results_and_is_never_retried(self):
        d = doc(("painters montgomery al", "pressure washing montgomery al", "interior painters montgomery al"))
        pages = standard_pages()
        pages[gurl("interior painters montgomery al")] = web([("x", "x.com")])
        sorry = "https://www.google.com/sorry/index?continue=" + d["keywords"][1]["web_url"]
        g = FakeGoogle(pages, redirects={d["keywords"][1]["web_url"]: sorry})
        r = await self.run_check(g, d)
        self.assertEqual(r["status"], "succeeded")
        run = r["result"]["run"]
        self.assertEqual(run["status"], "blocked")
        self.assertIn("CAPTCHA", run["stopped_reason"])
        res = run["results"]
        self.assertEqual((res[0]["web_status"], res[0]["map_status"], res[0]["position"]), ("read", "read", 1))   # partial results kept
        self.assertEqual(res[1]["web_status"], "blocked")
        self.assertEqual((res[1]["map_status"], res[2]["web_status"], res[2]["map_status"]), ("not_run", "not_run", "not_run"))
        self.assertEqual(len(g.opened), 3, "nothing was opened after the wall: no retry, no other search")
        self.assertEqual(run["completed"], 1)
        self.assertEqual(r["completeness"], "partial")
        self.assertIn("stopped", r["result"]["markdown"].lower())

    async def test_a_consent_wall_also_stops_the_run(self):
        d = doc()
        g = FakeGoogle(standard_pages(), redirects={d["keywords"][0]["web_url"]: "https://consent.google.com/m?continue=x"})
        run = (await self.run_check(g, d))["result"]["run"]
        self.assertEqual((run["status"], run["results"][0]["web_status"]), ("blocked", "blocked"))
        self.assertEqual(len(g.opened), 1)

    async def test_results_that_cannot_be_read_reliably_record_no_position(self):
        d = doc(("painters montgomery al",))
        pages = standard_pages()
        # no result headings at all: the parser would fall back to loose link guessing, which is not good enough to publish a rank
        pages[gurl("painters montgomery al")] = {"kind": "web", "results": [], "paa": False}
        g = FakeGoogle(pages)
        ext = await self.h.extension(handlers={**g.handlers(), "inspect_page": lambda p, c: {"result": {"snapshot_id": "s", "tab_id": p["tab_id"], "url": "u", "page": {"headings": []},
                                                                                                   "elements": [{"id": "el_001", "tag": "a", "name": "Some long link name that is not a result heading", "href": "https://rival9.com/x"}]}}})
        r = await self.c.run("seo.rank_check", d, client=self.cid, wait=60)
        a = r["result"]["run"]["results"][0]
        self.assertEqual((a["web_status"], a["position"]), ("unreadable", None))
        self.assertIn("no position is recorded", a["notes"])
        self.assertEqual(r["result"]["run"]["status"], "partial")

    async def test_later_runs_carry_previous_values_and_deltas(self):
        d = doc()
        g1 = FakeGoogle(standard_pages(client_at=3))
        r1 = await self.run_check(g1, d)
        run1 = r1["result"]["run"]
        self.assertTrue(run1["first_run"])
        self.assertNotIn("previous_position", run1["results"][0])
        self.assertEqual(run1["results"][0]["position"], 3)
        better_map = [GOOD_MAP[0], ("Example Painting Co", ["4.8(12)"], "Painter")] + GOOD_MAP[1:6]
        g2 = FakeGoogle(standard_pages(client_at=1, map_pos_list=better_map))
        await self.ext.close()
        r2 = await self.run_check(g2, d)
        run2 = r2["result"]["run"]
        self.assertFalse(run2["first_run"])
        self.assertNotEqual(run2["run_id"], run1["run_id"])
        a = run2["results"][0]
        self.assertEqual((a["position"], a["previous_position"], a["position_delta"], a["previous_run_id"], a["movement"]), (1, 3, 2, run1["run_id"], "up"))
        self.assertEqual((a["map_position"], a["previous_map_position"], a["map_position_delta"]), (1, 6, 5))
        b = run2["results"][1]
        self.assertEqual((b["position"], b["previous_position"], b["position_delta"], b["movement"]), (None, None, None, "absent then and now"))
        self.assertIn("| +2 |", r2["result"]["markdown"])

    async def test_a_subset_run_compares_with_the_newest_earlier_reading_of_each_keyword(self):
        d = doc()
        await self.run_check(FakeGoogle(standard_pages(client_at=2)), d)
        await self.ext.close()
        sub = {**d, "only": ["painters montgomery al"]}
        r = await self.run_check(FakeGoogle(standard_pages(client_at=1)), sub)
        run = r["result"]["run"]
        self.assertEqual((run["total"], run["subset"], run["results"][0]["position_delta"]), (1, True, 1))

    async def test_a_different_location_is_never_compared_with_an_earlier_one(self):
        d = doc(("painters montgomery al",))
        await self.run_check(FakeGoogle(standard_pages(client_at=2)), d)
        await self.ext.close()
        other = "w+CAIQICIHQnVmZmFsbw"
        d2 = {**d, "locale": {"city": "Buffalo", "uule": other}, "keywords": [{"keyword": "painters montgomery al", "web_url": gurl("painters montgomery al", uule=other), "map_url": gurl("painters montgomery al", tbm=True, uule=other)}]}
        pages = {gurl("painters montgomery al", uule=other): web([("Example Painting Co", SITE)]), gurl("painters montgomery al", tbm=True, uule=other): mapp(GOOD_MAP)}
        r = await self.run_check(FakeGoogle(pages), d2)
        run = r["result"]["run"]
        self.assertTrue(run["first_run"])
        self.assertNotIn("previous_position", run["results"][0])

    async def test_markdown_is_only_a_fallback_and_the_json_wins(self):
        d = doc(("painters montgomery al",))
        md = "## 1. painters montgomery al\n- **Web results:** " + gurl("painters montgomery al") + "\n- **Map results:** " + gurl("painters montgomery al", tbm=True) + "\n"
        g = FakeGoogle(standard_pages())
        r = await self.run_check(g, {"site": SITE, "brand_name": "Example Painting Co", "markdown": md})
        self.assertEqual(r["status"], "succeeded", r)
        self.assertEqual(r["result"]["run"]["locale_used"]["uule"], UULE)                              # derived from the URLs
        self.assertEqual(r["result"]["run"]["results"][0]["position"], 1)
        both = {**d, "markdown": "## 1. something else\n- **Web results:** https://example.com/\n"}
        self.assertEqual(rank.prepare(both)[0]["keywords"][0]["keyword"], "painters montgomery al")

    async def test_it_needs_no_approval_and_is_not_an_external_effect(self):
        cap = REGISTRY["seo.rank_check"]
        self.assertEqual((cap.approval, cap.external_effects, cap.needs_browser, cap.scope), ("never", False, True, "client"))
        r = await self.c.run("seo.rank_check", doc(), client=self.cid, wait=20)
        self.assertEqual(r["error"]["code"], "EXTENSION_NOT_CONNECTED")                                 # a clean, retryable failure, not a hang


if __name__ == "__main__":
    unittest.main()
