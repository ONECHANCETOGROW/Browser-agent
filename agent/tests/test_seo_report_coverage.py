"""The SEO report must never present missing data as a number: stale / unavailable searches are labelled, and competitor positions that the search data does not show are 'unavailable'."""
import json
import unittest

from octg_agent.seo.report import build_report

NOW = 1_800_000_000.0
H = 3600.0


def kw(query, at, pos=None):
    return {"kind": "keyword_rank", "query": query, "location": "Buffalo, NY", "search_engine": "google", "observed_at": at, "position": pos, "url": "https://client.example/" if pos else "",
            "evidence": "ev", "data": json.dumps({"checked_results": 9}), "domain": "client.example"}


def res(query, pos, domain, at=NOW - H, cls="competitor"):
    return {"kind": "serp_result", "query": query, "location": "Buffalo, NY", "search_engine": "google", "observed_at": at, "position": pos, "url": f"https://{domain}/", "evidence": "ev",
            "data": json.dumps({"class": cls}), "domain": domain}


def client():
    return {"client": {"name": "Acme Towing", "client_id": "cli_x"}, "facts": [], "services": [], "locations": [], "status": {"unverified": []}}


def camp(*keywords):
    return {"campaign_id": "cmp_1", "name": "c", "status": "baseline_set", "keywords": [{"keyword": k, "location": "Buffalo, NY"} for k in keywords], "services": [], "locations": []}


def comp(domain, **kw_):
    return {"domain": domain, "url": f"https://{domain}/", "via": "http", "title": f"{domain} title", "word_count": 400, "schema": ["LocalBusiness"], **kw_}


def build(serp, keywords, competitors=None, attempts=None, **kw_):
    return build_report(client=client(), summary={"website": "https://client.example/"}, campaign=camp(*keywords), audit=None, competitors=competitors, serp=serp, connectors=None,
                        generated_at=NOW, request_id="rq_1", attempts=attempts, **kw_)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.serp = [kw("alpha Buffalo, NY", NOW - H, 3), kw("bravo Buffalo, NY", NOW - 20 * H), res("alpha Buffalo, NY", 1, "rival.example")]
        self.attempts = [{"query": "charlie Buffalo, NY", "at": NOW - 2 * H, "outcome": "hit a CAPTCHA/consent wall and was not bypassed"}]
        self.rep = build(self.serp, ["alpha", "bravo", "charlie", "delta"], attempts=self.attempts)
        self.md = self.rep["markdown"]
        self.rows = {r["query"]: r for r in self.rep["data"]["baseline"]["keywords"]}

    def test_each_keyword_is_fresh_older_or_unavailable(self):
        self.assertEqual({q: r["status"] for q, r in self.rows.items()},
                         {"alpha Buffalo, NY": "fresh", "bravo Buffalo, NY": "older", "charlie Buffalo, NY": "unavailable", "delta Buffalo, NY": "unavailable"})
        self.assertIn("FRESH", self.md)
        self.assertIn("OLDER (20 h old)", self.md)
        self.assertIn("UNAVAILABLE", self.md)

    def test_coverage_is_not_claimed_complete(self):
        cov = self.rep["data"]["baseline"]["coverage"]
        self.assertEqual((cov["keywords"], cov["fresh"], cov["older"], cov["unavailable"], cov["complete"]), (4, 1, 1, 2, False))
        self.assertIn("Coverage is NOT complete", self.md)
        self.assertIn("1 of 4 campaign keywords have a fresh observation", self.md)
        self.assertTrue(any("Search coverage is incomplete" in m for m in self.rep["data"]["missing_data"]))

    def test_unavailable_rows_say_why_and_carry_no_position(self):
        self.assertIn("hit a CAPTCHA/consent wall and was not bypassed", self.rows["charlie Buffalo, NY"]["note"])
        self.assertIn("has not been searched", self.rows["delta Buffalo, NY"]["note"])
        for q in ("charlie Buffalo, NY", "delta Buffalo, NY"):
            self.assertIsNone(self.rows[q]["client_position"])
            self.assertIsNone(self.rows[q]["observed_at"])
        line = next(l for l in self.md.splitlines() if l.startswith("| delta"))
        self.assertIn("unavailable", line)
        self.assertNotIn("not in results shown", line)

    def test_older_observation_keeps_its_own_date(self):
        self.assertEqual(self.rows["bravo Buffalo, NY"]["observed_at"], NOW - 20 * H)
        self.assertEqual(self.rows["bravo Buffalo, NY"]["age_hours"], 20.0)

    def test_later_failed_attempt_is_noted_on_the_older_row(self):
        rep = build(self.serp, ["alpha", "bravo"], attempts=[{"query": "bravo Buffalo, NY", "at": NOW - 5 * H, "outcome": "hit a CAPTCHA/consent wall and was not bypassed"}])
        row = next(r for r in rep["data"]["baseline"]["keywords"] if r["query"].startswith("bravo"))
        self.assertEqual(row["status"], "older")
        self.assertIn("later attempt", row["note"])

    def test_a_failed_attempt_before_a_valid_observation_is_ignored(self):
        rep = build(self.serp, ["alpha"], attempts=[{"query": "alpha Buffalo, NY", "at": NOW - 9 * H, "outcome": "hit a CAPTCHA/consent wall and was not bypassed"}])
        self.assertEqual(rep["data"]["baseline"]["keywords"][0]["note"], "")

    def test_complete_when_every_keyword_is_fresh(self):
        rep = build([kw("alpha Buffalo, NY", NOW - H, 3), kw("bravo Buffalo, NY", NOW - 2 * H)], ["alpha", "bravo"])
        self.assertTrue(rep["data"]["baseline"]["coverage"]["complete"])
        self.assertIn("(complete)", rep["markdown"])
        self.assertNotIn("NOT complete", rep["markdown"])
        self.assertFalse(any("coverage is incomplete" in m for m in rep["data"]["missing_data"]))

    def test_freshness_window_is_a_parameter(self):
        rep = build(self.serp, ["alpha", "bravo"], fresh_hours=24)
        self.assertEqual({r["status"] for r in rep["data"]["baseline"]["keywords"]}, {"fresh"})


class CompetitorTableTests(unittest.TestCase):
    def build(self, competitors, serp=None):
        serp = serp if serp is not None else [kw("alpha Buffalo, NY", NOW - H), res("alpha Buffalo, NY", 4, "www.rival.example"), res("bravo Buffalo, NY", 2, "rival.example")]
        return build(serp, ["alpha", "bravo"], competitors={"artifact_id": "ar_1", "body": competitors})

    def test_positions_come_from_the_search_data_and_missing_ones_are_unavailable(self):
        rep = self.build({"competitors": [comp("rival.example"), comp("other.example")], "comparison": [{"observation": "2 of 2 name a location", "evidence": []}]})
        self.assertNotIn("#None", rep["markdown"])
        rows = {c["domain"]: c for c in rep["data"]["competitors"]}
        self.assertEqual((rows["rival.example"]["best_position_observed"], rows["rival.example"]["searches_seen_in"]), (2, 2))
        self.assertIsNone(rows["other.example"]["best_position_observed"])
        self.assertIsNone(rows["other.example"]["searches_seen_in"])
        line = next(l for l in rep["markdown"].splitlines() if l.startswith("| other.example"))
        self.assertEqual(line.split("|")[2].strip(), "unavailable")
        self.assertEqual(line.split("|")[3].strip(), "unavailable")        # not "0"
        line = next(l for l in rep["markdown"].splitlines() if l.startswith("| rival.example"))
        self.assertEqual((line.split("|")[2].strip(), line.split("|")[3].strip()), ("#2", "2"))

    def test_comparison_notes_are_shown(self):
        rep = self.build({"competitors": [comp("rival.example")], "comparison": [{"observation": "competitor pages carry a median of 400 words", "evidence": []}]})
        self.assertIn("- competitor pages carry a median of 400 words", rep["markdown"])

    def test_unreadable_competitor_is_listed_as_unavailable(self):
        rep = self.build({"competitors": [comp("rival.example")], "unreadable": [{"domain": "walled.example", "url": "https://walled.example/", "error": "BOT_CHALLENGE_IN_BROWSER", "tried": "browser"}]})
        line = next(l for l in rep["markdown"].splitlines() if l.startswith("| walled.example"))
        self.assertIn("could not be read (BOT_CHALLENGE_IN_BROWSER)", line)
        self.assertEqual(rep["data"]["competitors_unreadable"][0]["domain"], "walled.example")

    def test_source_line_reports_how_pages_were_really_read(self):
        rep = self.build({"competitors": [comp("a.example"), comp("b.example", via="browser", schema=None)]})
        src = next(s for s in rep["data"]["data_sources"] if s["source"] == "Competitor pages")
        self.assertIn("1 via browser", src["detail"])
        self.assertIn("1 via http", src["detail"])
        self.assertNotIn("(HTTP)", src["source"])
        self.assertIn("not measured (browser read)", rep["markdown"])

    def test_review_made_by_seo_competitors_keeps_its_own_positions(self):
        rep = self.build({"competitors": [comp("legacy.example", keywords=["alpha Buffalo, NY"], best_position=5)]}, serp=[kw("alpha Buffalo, NY", NOW - H)])
        self.assertEqual(rep["data"]["competitors"][0]["best_position_observed"], 5)


class CompareWithoutLocationsTests(unittest.TestCase):
    def profile(self, domain, title):
        return {"domain": domain, "evidence_id": "ev_" + domain, "title": title, "word_count": 500, "schema": [], "tel_links": 1}

    def test_no_target_locations_means_not_counted_never_zero(self):
        from octg_agent.collab.caps.competitors import _compare
        out = _compare([self.profile("a.example", "Towing in Buffalo"), self.profile("b.example", "Jump starts")], {"title": "Home", "word_count": 500, "tel_links": 1}, [])
        text = " | ".join(o["observation"] for o in out)
        self.assertNotIn("0 of 2 competitor pages name a target location", text)
        self.assertIn("not counted", text)
        self.assertIn("not compared", text)

    def test_with_locations_it_still_counts(self):
        from octg_agent.collab.caps.competitors import _compare
        out = _compare([self.profile("a.example", "Towing in Buffalo"), self.profile("b.example", "Jump starts")], None, ["buffalo"])
        self.assertIn("1 of 2 competitor pages name a target location in their title", out[0]["observation"])


class DefaultCompetitorSelectionTests(unittest.TestCase):
    def test_most_frequent_competitors_come_first_whatever_the_storage_order(self):
        from octg_agent.collab.caps.competitors import default_targets
        rows = [res("q1", 1, "rare.example"), res("q2", 2, "rare.example"),                       # 2 searches
                res("q1", 5, "common.example"), res("q2", 6, "common.example"), res("q3", 7, "common.example"),   # 3 searches, worse positions
                res("q3", 1, "once.example"),                                                      # 1 search, best position
                res("q3", 2, "mine.example", cls="client"), res("q3", 3, "yelp.com", cls="directory")]
        self.assertEqual(default_targets(rows), ["https://common.example/", "https://rare.example/", "https://once.example/"])
        self.assertEqual(default_targets(list(reversed(rows))), default_targets(rows))
        self.assertEqual(default_targets(rows, limit=1), ["https://common.example/"])

    def test_ties_break_on_best_position(self):
        from octg_agent.collab.caps.competitors import default_targets
        rows = [res("q1", 4, "b.example"), res("q1", 2, "a.example")]
        self.assertEqual(default_targets(rows), ["https://a.example/", "https://b.example/"])


class BasisTests(unittest.TestCase):
    def test_hypotheses_are_labelled_as_inferred(self):
        finding = lambda i, t, c: {"finding_id": f"F-{i}", "title": f"t{i}", "type": t, "confidence": c, "severity": "medium", "effort": "S", "pages": [], "evidence": [], "category": "content",
                                   "recommendation": "r", "verification": "v", "priority_score": 10 - i, "rule": "x"}
        audit = {"artifact_id": "ar_a", "body": {"site": "https://client.example/", "findings": [finding(1, "opportunity", "likely"), finding(2, "opportunity", "hypothesis")], "pages": [], "summary": {"by_severity": {}}}}
        rep = build_report(client=client(), summary={}, campaign=None, audit=audit, competitors=None, serp=[], connectors=None, generated_at=NOW)
        rows = [l for l in rep["markdown"].splitlines() if l.startswith("| 1 |") or l.startswith("| 2 |")]
        self.assertIn("measured on site", rows[0])
        self.assertIn("inferred hypothesis", rows[1])


if __name__ == "__main__":
    unittest.main()
