"""The Secure Context Resolver in isolation: scope, minimum-necessary manifest, token parsing/resolution, literal guard, outbound filter."""
import tempfile
import unittest
from pathlib import Path

from octg_agent.business.documents import DocumentRegistry
from octg_agent.business.resolver import BrainReader, TOKEN_ANY, TOKEN_FULL
from octg_agent.business.store import BusinessStore
from octg_agent.files.authorization import AuthorizedFolders
from octg_agent.sensitive import SECRETS

NAME, GSTIN, PAN = "Example Growth LLC", "TEST-GSTIN-123", "ABCDE1234F"
ADDR = {"line1": "123 Example Street", "locality": "Exampleville", "pin": "560001"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = BusinessStore(d / "b.db")
        self.docs = DocumentRegistry(self.store, AuthorizedFolders(d / "f.json"))
        self.store.put_candidate("identity.legal_name", NAME, source_type="USER_ENTERED")
        self.store.put_candidate("addresses.principal", ADDR, source_type="USER_ENTERED")
        self.store.put_candidate("taxation.gstin", GSTIN, source_type="USER_ENTERED")
        self.store.put_candidate("taxation.pan", PAN, source_type="USER_ENTERED")

    def tearDown(self):
        SECRETS.clear()
        self.tmp.cleanup()

    def reader(self, instruction="Fill in my business name and business address.", **kw):
        return BrainReader(self.store, self.docs, "task_t", instruction, **kw)


def els(*names, role="textbox"):
    return [{"role": role, "name": n} for n in names]


class ScopeTests(Base):
    def test_intent_detection(self):
        for text in ("Fill in my business name and business address.", "Go to the GST portal and start my business registration", "Use my company details",
                     "Upload my GST certificate", "fill the form with my details", "Use my Business Brain"):
            self.assertTrue(self.reader(text).active, text)
        for text in ("Open Google and search for One Chance to Grow.", "Find Marie Curie's Nobel Prizes", "What is the business model of this site?", "Buy headphones"):
            self.assertFalse(self.reader(text).active, text)

    def test_explicit_switches_win(self):
        self.assertTrue(self.reader("do something", override=True).active)
        self.assertFalse(self.reader("Fill in my business name", override=False).active)
        self.assertFalse(self.reader("Fill in my business name", intent=False).active)

    def test_an_inactive_reader_plans_nothing(self):
        r = self.reader("Search for shoes")
        self.assertFalse(r.plan(els("Business Name")))


class MinimumNecessaryTests(Base):
    def test_the_manifest_lists_only_what_the_page_asks_for(self):
        m = self.reader().plan(els("Business Name", "Business Address"))
        self.assertEqual(sorted(m.keys), ["addresses.principal.full", "identity.legal_name"])
        for hidden in ("taxation.gstin", "taxation.pan", "owners", "contact", "registration"):
            self.assertNotIn(hidden, m.text)

    def test_high_fields_appear_only_when_the_page_has_such_a_field(self):
        m = self.reader().plan(els("Business Name", "GSTIN"))
        self.assertIn("taxation.gstin", m.keys)
        self.assertIn("HIGH", m.text)
        self.assertNotIn("taxation.pan", m.keys)

    def test_the_manifest_never_contains_a_value(self):
        m = self.reader().plan(els("Business Name", "Business Address", "GSTIN", "PAN Number"))
        for secret in (NAME, "123 Example", GSTIN, PAN, "560001"):
            self.assertNotIn(secret, m.text)

    def test_statuses_are_reported_honestly(self):
        self.store.put_candidate("contact.email", "ops@example.test", source_type="DOCUMENT", doc_id="d")
        self.store.put_candidate("addresses.principal", {"line1": "9 Other Road"}, source_type="DOCUMENT", doc_id="d2")
        m = self.reader().plan(els("Business Name", "Email", "Business Address", "Date of incorporation"))
        by = {e.key: e.status for e in m.entries}
        self.assertEqual(by["identity.legal_name"], "VERIFIED")
        self.assertEqual(by["contact.email"], "UNVERIFIED")
        self.assertEqual(by["addresses.principal.full"], "CONFLICTING")
        self.assertEqual(by["registration.incorporation_date"], "MISSING")
        for word in ("UNVERIFIED", "CONFLICT", "MISSING"):
            self.assertIn(word, m.text)

    def test_ambiguous_labels_offer_every_candidate_and_leave_the_choice_to_the_model(self):
        m = self.reader().plan(els("Name of the business owner and company name"))
        self.assertGreaterEqual(len(m.entries), 2)

    def test_the_manifest_is_capped(self):
        many = els("Business Name", "Email", "Phone", "Website", "Business Address", "GSTIN", "PAN", "TAN", "CIN", "Date of incorporation", "Constitution of business",
                   "Services offered", "HSN code", "Owner name", "Owner PAN", "Date of birth")
        self.assertLessEqual(len(self.reader().plan(many).entries), 12)

    def test_unrelated_pages_get_no_manifest_at_all(self):
        self.assertFalse(self.reader().plan(els("Search", "Newsletter signup", "Your message")))

    def test_enum_values_are_shown_only_when_the_caller_allows_it_and_never_for_high_fields(self):
        self.store.put_candidate("identity.constitution", "Partnership", source_type="USER_ENTERED")
        page = els("Partnership", "Proprietorship", role="radio") + els("GSTIN")
        no = self.reader().plan(page, allow_values=False)
        self.assertEqual(no.class1, {})
        self.assertNotIn("Partnership\"", no.text)
        yes = self.reader().plan(page, allow_values=True)
        self.assertEqual(yes.class1, {"identity.constitution": "Partnership"})
        self.assertNotIn(GSTIN, yes.text)
        self.assertNotIn("taxation.gstin\": ", str(yes.class1))
        self.assertEqual(list(yes.class1), ["identity.constitution"])

    def test_documents_are_listed_by_type_label_and_count_only_when_relevant(self):
        d = Path(self.tmp.name) / "docs"
        d.mkdir()
        (d / "gst_certificate.txt").write_text("Certificate of Registration\n", encoding="utf-8")
        self.docs.folders.allow(str(d))
        self.docs.enroll(str(d))
        self.docs.scan()
        self.docs.index()
        self.docs.approve(self.docs.list()[0]["doc_id"])
        self.assertEqual(self.reader("Fill in my business name").plan(els("Business Name")).docs, [])
        m = self.reader("Upload my GST certificate").plan(els("Business Name") + [{"role": "file", "name": "Upload"}])
        self.assertIn("gst_certificate", m.text)
        self.assertNotIn("gst_certificate.txt", m.text)


class TokenParsingTests(unittest.TestCase):
    def test_token_shapes(self):
        for ok in ("{{biz:identity.legal_name}}", " {{ biz : taxation.gstin }} ", "{{doc:gst_certificate}}", "{{biz:owners[0].name}}"):
            self.assertIsNotNone(TOKEN_FULL.match(ok), ok)
        for bad in ("x {{biz:a.b}}", "{{biz:a.b}} x", "{{biz:}}", "{{foo:bar}}", "{biz:a.b}", "{{biz:a b}}"):
            self.assertIsNone(TOKEN_FULL.match(bad), bad)
        self.assertTrue(TOKEN_ANY.search("prefix {{ biz:abc"))
        self.assertTrue(TOKEN_ANY.search("{{doc:x}}"))
        self.assertFalse(TOKEN_ANY.search("{{unrelated}}"))


class PrepareTests(Base):
    def prep(self, tool, args, expect=None, el=None, **kw):
        return self.reader(**kw).prepare(tool, args, expect, el)

    def test_a_token_is_replaced_by_the_exact_stored_value_and_only_in_the_execution_args(self):
        args = {"element_id": "el_1", "text": "{{biz:identity.legal_name}}"}
        plan = self.prep("type", args)
        self.assertEqual(plan.status, "ok")
        self.assertEqual(plan.args["text"], NAME)
        self.assertEqual(args["text"], "{{biz:identity.legal_name}}", "the model's call is left untouched")
        self.assertEqual((plan.fills[0].key, plan.fills[0].sensitivity, plan.fills[0].label), ("identity.legal_name", "MEDIUM", "Legal name of business"))
        self.assertNotIn(NAME, repr(plan.fills[0]), "a Fill never prints its value")

    def test_parts_of_a_compound_and_the_rendered_full_form(self):
        self.assertEqual(self.prep("type", {"text": "{{biz:addresses.principal.pin}}"}).args["text"], "560001")
        self.assertEqual(self.prep("type", {"text": "{{biz:addresses.principal.full}}"}).args["text"], "123 Example Street, Exampleville, 560001")
        p = self.prep("type", {"text": "{{biz:addresses.principal.state}}"})
        self.assertEqual(p.status, "wait")
        self.assertEqual(p.kind, "missing_info")

    def test_select_options_and_upload_paths_take_tokens_too(self):
        self.assertEqual(self.prep("select", {"element_id": "e", "option": "{{biz:addresses.principal.line1}}"}).args["option"], "123 Example Street")

    def test_whole_value_only(self):
        for tool, args in [("type", {"text": "Name: {{biz:identity.legal_name}}"}), ("type", {"text": "{{biz:identity.legal_name}}{{biz:identity.legal_name}}"}),
                           ("navigate", {"url": "https://x.test/{{biz:identity.legal_name}}"}), ("click", {"element_id": "{{biz:identity.legal_name}}"}),
                           ("type", {"text": "{{doc:gst_certificate}}"}), ("upload_file", {"local_file_path": "{{biz:identity.legal_name}}"}),
                           ("find_text", {"text": "{{biz:identity.legal_name}}"})]:
            p = self.prep(tool, args)
            self.assertEqual(p.status, "feedback", (tool, args))
            self.assertIsNone(p.args)

    def test_tokens_in_expectations_are_refused(self):
        p = self.prep("type", {"text": "hi"}, expect=[{"type": "text_present", "value": "{{biz:identity.legal_name}}"}])
        self.assertEqual(p.status, "feedback")

    def test_unknown_missing_unverified_and_conflicting_stop_with_a_reason_and_no_args(self):
        self.store.put_candidate("contact.email", "a@example.test", source_type="DOCUMENT", doc_id="d")
        self.store.put_candidate("contact.phone", "9876543210", source_type="USER_ENTERED")
        self.store.put_candidate("contact.phone", "9123456789", source_type="DOCUMENT", doc_id="d2")
        cases = {"{{biz:banking.account}}": ("feedback", ""), "{{biz:registration.cin}}": ("wait", "missing_info"), "{{biz:contact.email}}": ("wait", "unverified"),
                 "{{biz:contact.phone}}": ("wait", "conflict")}
        for token, (status, kind) in cases.items():
            p = self.prep("type", {"text": token})
            self.assertEqual((p.status, p.kind), (status, kind), token)
            self.assertIsNone(p.args)
        msg = self.prep("type", {"text": "{{biz:contact.phone}}"}).message
        self.assertNotIn("9876543210", msg)
        self.assertNotIn("9123456789", msg)

    def test_nothing_resolves_when_the_brain_is_not_active_for_the_task(self):
        p = self.prep("type", {"text": "{{biz:identity.legal_name}}"}, override=False)
        self.assertEqual(p.status, "feedback")
        self.assertIsNone(p.args)

    def test_sensitive_targets_are_blocked(self):
        p = self.prep("type", {"text": "{{biz:identity.legal_name}}"}, el={"name": "Password", "type": "password", "sensitive": True})
        self.assertEqual((p.status, p.kind), ("blocked", "sensitive_input"))

    def test_resolved_values_are_registered_for_log_scrubbing_until_released(self):
        r = self.reader()
        r.prepare("type", {"text": "{{biz:taxation.gstin}}"}, None)
        self.assertTrue(SECRETS.contains(f"x {GSTIN} y"))
        r.release()
        self.assertFalse(SECRETS.contains(f"x {GSTIN} y"))

    def test_a_store_failure_means_ask_the_user_never_a_guess(self):
        r = self.reader()
        r._store = None  # simulate a broken store
        p = r.prepare("type", {"text": "{{biz:identity.legal_name}}"}, None)
        self.assertEqual((p.status, p.kind), ("wait", "missing_info"))


class LiteralGuardTests(Base):
    def test_an_identifier_the_model_was_never_given_is_blocked(self):
        for text in ("27ABCDE1234F1Z5", "ABCDE1234F", "4111 1111 1111 1111"):
            p = self.reader().prepare("type", {"text": text}, None)
            self.assertEqual(p.status, "feedback", text)

    def test_an_identifier_the_user_dictated_in_the_task_is_allowed(self):
        p = self.reader("Fill in my business details, my PAN is ABCDE1234F").prepare("type", {"text": "ABCDE1234F"}, None)
        self.assertEqual(p.status, "none")

    def test_ordinary_text_passes_and_the_guard_is_only_for_tasks_using_the_brain(self):
        self.assertEqual(self.reader().prepare("type", {"text": "hello world"}, None).status, "none")
        self.assertEqual(self.reader("search for shoes").prepare("type", {"text": "ABCDE1234F"}, None).status, "none")


class OutboundFilterTests(Base):
    def test_known_values_become_tokens_and_identifier_shapes_are_masked(self):
        r = self.reader()
        r.prepare("type", {"text": "{{biz:identity.legal_name}}"}, None)
        out = r.redact(f"field holds {NAME}; other {PAN} and 27ABCDE1234F1Z5")
        self.assertNotIn(NAME, out)
        self.assertIn("{{biz:identity.legal_name}}", out)
        self.assertNotIn(PAN, out)

    def test_hides_value_decisions(self):
        r = self.reader()
        r.prepare("type", {"text": "{{biz:identity.legal_name}}"}, None)
        self.assertTrue(r.hides_value({"name": "Anything", "value": NAME}))
        self.assertTrue(r.hides_value({"name": "Anything", "value": NAME[:8]}), "a clipped start of a known value")
        self.assertTrue(r.hides_value({"name": "PAN Number", "value": "whatever"}))
        self.assertTrue(r.hides_value({"name": "Notes", "value": PAN}))
        self.assertFalse(r.hides_value({"name": "Search", "value": "wireless headphones"}))
        self.assertFalse(r.hides_value({"name": "Search"}))


class AuditHelperTests(Base):
    def test_resolver_audit_rows_never_contain_values(self):
        import json

        r = self.reader()
        r.prepare("type", {"text": "{{biz:taxation.gstin}}"}, None)
        r.prepare("type", {"text": "{{biz:registration.cin}}"}, None)
        r.prepare("type", {"text": "27ABCDE1234F1Z5"}, None)
        rows = self.store.audit_tail(50, task_id="task_t")
        self.assertEqual([x["event"] for x in rows], ["retrieve", "missing", "blocked"])
        blob = json.dumps(rows)
        for secret in (GSTIN, "27ABCDE1234F1Z5"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
