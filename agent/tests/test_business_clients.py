"""Client Brain foundation: clients are first-class, separate from the owner's own business and from each other. Synthetic clients only.

  Client A: Example Locksmith LLC / example-a.test        Client B: Example Roofing LLC / example-b.test        Owner: Example Growth LLC
"""
import contextlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from octg_agent.business import research as research_mod
from octg_agent.business.documents import DocNotAvailable, DocumentRegistry
from octg_agent.business.onboard import missing_fields, run_onboarding
from octg_agent.business.research import ResearchLog
from octg_agent.business.resolver import BrainReader, pick_client
from octg_agent.business.schema import CLIENT_CATALOG, CATALOG, KeyError_, match_label, parse_key
from octg_agent.business.store import BusinessStore, NotAvailable, StoreError
from octg_agent.files.authorization import AuthorizedFolders
from octg_agent.sensitive import SECRETS

A = {"name": "Example Locksmith LLC", "site": "https://example-a.test", "phone": "+1 555 010 1000", "email": "a@example-a.test", "gstin": "TEST-A-GSTIN",
     "addr": {"line1": "1 Alpha Street", "city": "Alphaville", "state": "FL", "country": "US", "postal_code": "34652"}}
B = {"name": "Example Roofing LLC", "site": "https://example-b.test", "phone": "+1 555 020 2000", "email": "b@example-b.test", "gstin": "TEST-B-GSTIN",
     "addr": {"line1": "2 Beta Avenue", "city": "Betatown", "state": "GA", "country": "US", "postal_code": "30301"}}
OWNER = {"name": "Example Growth LLC", "phone": "+91 98765 43210"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.store = BusinessStore(self.root / "data" / "business.db")
        self.folders = AuthorizedFolders(self.root / "data" / "folders.json")
        self.docs = DocumentRegistry(self.store, self.folders)
        self.a = self.store.create_client(A["name"])["client_id"]
        self.b = self.store.create_client(B["name"])["client_id"]
        for cid, d in ((self.a, A), (self.b, B)):
            self.put(cid, "contact.website", d["site"])
            self.put(cid, "contact.phone", d["phone"])
            self.put(cid, "contact.email", d["email"])
            self.put(cid, "location.address", d["addr"])
            self.put(cid, "taxation.gstin", d["gstin"])
        self.store.put_candidate("identity.legal_name", OWNER["name"], source_type="USER_ENTERED")
        self.store.put_candidate("contact.phone", OWNER["phone"], source_type="USER_ENTERED")

    def tearDown(self):
        SECRETS.clear()
        self.tmp.cleanup()

    def put(self, cid, key, value, src="USER_ENTERED", **kw):
        return self.store.put_candidate(key, value, source_type=src, scope=cid, **kw)

    def reader(self, instruction="Fill in the selected client's business information.", client=None, **kw):
        c = self.store.get_client(client) if client else None
        return BrainReader(self.store, self.docs, "task_t", instruction, client_id=client, client_name=c["name"] if c else "", client_reason="selected by you", **kw)


class ClientCrudTests(Base):
    def test_clients_can_be_created_listed_renamed_and_archived(self):
        names = [c["name"] for c in self.store.list_clients()]
        self.assertEqual(names, [A["name"], B["name"]])
        self.assertTrue(self.a.startswith("cli_") and self.a != self.b)
        self.store.rename_client(self.a, "Example Locksmith Services LLC")
        self.assertEqual(self.store.get_client(self.a)["name"], "Example Locksmith Services LLC")
        self.store.set_client_archived(self.b, True)
        self.assertEqual([c["name"] for c in self.store.list_clients()], ["Example Locksmith Services LLC"])
        self.assertEqual(len(self.store.list_clients(include_archived=True)), 2)

    def test_duplicate_and_secret_like_names_are_refused(self):
        with self.assertRaises(StoreError) as cm:
            self.store.create_client("example locksmith llc")
        self.assertEqual(cm.exception.code, "DUPLICATE_CLIENT")
        self.store.create_client("example locksmith llc", allow_duplicate=True)
        with self.assertRaises(StoreError):
            self.store.create_client("4111 1111 1111 1111")
        with self.assertRaises(StoreError):
            self.store.create_client("   ")

    def test_find_client_is_exact_never_a_guess(self):
        self.assertEqual(self.store.find_client(A["name"].upper())["client_id"], self.a)
        self.assertEqual(self.store.find_client(self.b)["name"], B["name"])
        with self.assertRaises(StoreError) as cm:
            self.store.find_client("Example")
        self.assertEqual(cm.exception.code, "NO_SUCH_CLIENT")
        self.store.create_client("Example Locksmith LLC", allow_duplicate=True)
        with self.assertRaises(StoreError) as cm:
            self.store.find_client(A["name"])
        self.assertEqual(cm.exception.code, "AMBIGUOUS_CLIENT")

    def test_an_archived_client_is_read_only(self):
        self.store.set_client_archived(self.a, True)
        with self.assertRaises(StoreError) as cm:
            self.put(self.a, "contact.email", "new@example-a.test")
        self.assertEqual(cm.exception.code, "CLIENT_ARCHIVED")
        self.assertEqual(self.store.get_value("contact.website", self.a).value, A["site"])
        with self.assertRaises(StoreError):
            self.docs.enroll(str(self.root), scope=self.a)

    def test_writing_to_an_unknown_client_is_refused(self):
        with self.assertRaises(StoreError) as cm:
            self.put("cli_nope", "contact.email", "x@example.test")
        self.assertEqual(cm.exception.code, "NO_SUCH_CLIENT")


class SchemaTests(unittest.TestCase):
    def test_client_catalog_covers_the_requested_areas(self):
        groups = {f.group for f in CLIENT_CATALOG}
        self.assertTrue({"identity", "contact", "location", "service_area", "services", "local_seo", "seo", "marketing", "account"} <= groups)
        keys = {f.key for f in CLIENT_CATALOG}
        for k in ("identity.business_name", "identity.legal_name", "identity.trade_name", "identity.description", "contact.website", "contact.email", "contact.phone",
                  "contact.person", "location.address", "service_area.cities[]", "service_area.towns[]", "service_area.counties[]", "service_area.regions[]", "service_area.other[]",
                  "services.primary[]", "services.secondary[]", "services.categories[]", "local_seo.gbp_reference", "local_seo.primary_category", "local_seo.secondary_categories[]",
                  "local_seo.target_locations[]", "seo.target_keywords[]", "seo.target_services[]", "seo.target_locations[]", "seo.goals[]", "seo.current_status",
                  "marketing.social_profiles[]", "marketing.directories[]", "marketing.content_notes", "account.status", "account.onboarding_date", "account.plan",
                  "account.internal_notes", "account.authorized_users[]"):
            self.assertIn(k, keys)
        addr = next(f for f in CLIENT_CATALOG if f.key == "location.address")
        self.assertEqual([p.name for p in addr.parts], ["line1", "line2", "city", "state", "country", "postal_code"])

    def test_catalogs_are_separate_namespaces(self):
        with self.assertRaises(Exception):
            parse_key("seo.target_keywords[0]", "me")
        with self.assertRaises(Exception):
            parse_key("owners[0].name", "client")
        self.assertEqual(parse_key("seo.target_keywords", "client").display(), "seo.target_keywords[0]")
        self.assertEqual({pk.display() for pk, _ in match_label("Business Name", kind="client")}, {"identity.business_name"})
        self.assertEqual({pk.display() for pk, _ in match_label("Business Name", kind="me")}, {"identity.legal_name"})

    def test_sensitivity_of_client_fields(self):
        by = {f.key: f.sensitivity for f in CLIENT_CATALOG}
        self.assertEqual((by["taxation.gstin"], by["taxation.pan"], by["registration.udyam"], by["account.internal_notes"]), ("HIGH",) * 4)
        self.assertEqual((by["contact.phone"], by["contact.email"], by["location.address"]), ("MEDIUM",) * 3)
        self.assertEqual((by["seo.target_keywords[]"], by["contact.website"]), ("LOW", "LOW"))

    def test_udyam_is_a_catalog_identifier_for_the_owner_and_clients_and_is_validated_not_guessed(self):
        self.assertIn("registration.udyam", {f.key for f in CATALOG})
        s = BusinessStore(Path(tempfile.mkdtemp()) / "b.db")
        r = s.put_candidate("registration.udyam", "UDYAM-XX-00-0000000", source_type="USER_ENTERED")
        self.assertEqual((r.status, r.warnings), ("VERIFIED", []))
        self.assertEqual(s.status("registration.udyam").sensitivity, "HIGH")
        bad = s.put_candidate("registration.udyam", "NOT-UDYAM", source_type="USER_ENTERED", replace=True)
        self.assertTrue(bad.warnings)
        self.assertEqual(s.get_value("registration.udyam").value, "NOT-UDYAM", "stored exactly as entered; the format problem is only a warning")


class IsolationTests(Base):
    def test_each_client_has_its_own_values_for_the_same_field(self):
        self.assertEqual(self.store.get_value("contact.phone", self.a).value, A["phone"])
        self.assertEqual(self.store.get_value("contact.phone", self.b).value, B["phone"])
        self.assertEqual(self.store.get_value("contact.phone").value, OWNER["phone"])
        self.assertEqual(self.store.status("contact.phone", self.a).status, "VERIFIED")

    def test_a_field_set_only_for_the_owner_is_missing_for_a_client_and_vice_versa(self):
        with self.assertRaises(NotAvailable) as cm:
            self.store.get_value("identity.legal_name", self.a)
        self.assertEqual(cm.exception.status, "MISSING")
        with self.assertRaises(KeyError_):
            self.store.get_value("location.address.line1")   # a client-only key does not even exist in the owner's catalog
        self.store.put_candidate("contact.email", "owner@example-growth.test", source_type="USER_ENTERED")
        self.assertEqual(self.store.get_value("contact.email", self.a).value, A["email"], "the owner's e-mail never answers for a client")

    def test_owner_values_never_appear_in_a_client_listing_and_the_reverse(self):
        client_view = json.dumps([f.to_dict() for f in self.store.list_fields(scope=self.a)])
        owner_view = json.dumps([f.to_dict() for f in self.store.list_fields()])
        for other in (B["site"], B["phone"], OWNER["name"], OWNER["phone"]):
            self.assertNotIn(other, client_view)
        self.assertNotIn(A["site"], owner_view)
        # masks differ per scope: nothing from client B shows in client A's masks
        self.assertNotIn("example-b", client_view)
        self.assertEqual({f.scope for f in self.store.list_fields(scope=self.a)}, {self.a})

    def test_conflicts_are_per_client(self):
        self.put(self.a, "contact.phone", "+1 555 999 9999", "DOCUMENT", doc_id="doc_x")
        self.assertEqual(self.store.status("contact.phone", self.a).status, "CONFLICTING")
        self.assertEqual(self.store.status("contact.phone", self.b).status, "VERIFIED")
        self.assertEqual(self.store.status("contact.phone").status, "VERIFIED")
        with self.assertRaises(NotAvailable) as cm:
            self.store.get_value("contact.phone", self.a)
        self.assertEqual(cm.exception.status, "CONFLICTING")

    def test_resolving_uses_the_right_scope_and_refuses_a_fact_of_another_scope(self):
        new = self.put(self.a, "contact.phone", "+1 555 999 9999", "DOCUMENT", doc_id="doc_x")
        b_fact = self.store.status("contact.phone", self.b).facts[0].fact_id
        with self.assertRaises(StoreError) as cm:
            self.store.resolve("contact.phone", b_fact, scope=self.a)
        self.assertEqual(cm.exception.code, "WRONG_FACT")
        with self.assertRaises(StoreError):
            self.store.resolve("contact.phone", new.fact_id)          # owner scope: that fact is not the owner's
        self.store.resolve("contact.phone", new.fact_id, scope=self.a)
        self.assertEqual(self.store.get_value("contact.phone", self.a).value, "+1 555 999 9999")
        self.assertEqual(self.store.get_value("contact.phone", self.b).value, B["phone"], "resolving one client's conflict never touches another")

    def test_identical_values_in_different_scopes_are_not_duplicates_of_each_other(self):
        r1 = self.put(self.a, "contact.email", "shared@example.test")
        r2 = self.put(self.b, "contact.email", "shared@example.test", replace=True)
        self.assertTrue(r2.created and r1.fact_id != r2.fact_id)

    def test_missing_client_information_is_reported_never_guessed(self):
        miss = {m["key"] for m in missing_fields(self.store, self.a)}
        self.assertIn("contact.person", miss)
        self.assertIn("seo.target_keywords[0]", miss)
        self.assertNotIn("contact.phone", miss)
        with self.assertRaises(NotAvailable) as cm:
            self.store.get_value("contact.person", self.a)
        self.assertEqual(cm.exception.status, "MISSING")

    def test_audit_rows_carry_the_client_and_views_filter_by_scope(self):
        rows_a = self.store.audit_tail(100, scope=self.a)
        self.assertTrue(rows_a and {r["scope"] for r in rows_a} == {self.a})
        self.assertFalse([r for r in self.store.audit_tail(100, scope="me") if r["scope"]], "the owner's view never lists client rows")
        self.assertTrue({r["scope"] for r in self.store.audit_tail(200)} >= {"", self.a, self.b})

    def test_client_values_are_protected_like_the_owners(self):
        raw = (self.root / "data" / "business.db").read_bytes() + b"".join(p.read_bytes() for p in (self.root / "data").glob("business.db-*"))
        self.assertNotIn(A["gstin"].encode(), raw)
        self.assertNotIn(B["gstin"].encode(), raw)
        self.assertEqual(self.store.status("taxation.gstin", self.a).sensitivity, "HIGH")
        for f in self.store.list_fields(scope=self.a, include_missing=False):
            self.assertNotIn(A["gstin"], json.dumps(f.to_dict()))
        for secret in ("other.password",):
            with self.assertRaises(StoreError):
                self.put(self.a, secret, "x")
        with self.assertRaises(StoreError):
            self.put(self.a, "account.internal_notes", "4111 1111 1111 1111")


class ResolverScopeTests(Base):
    def test_a_client_reader_resolves_only_that_clients_values(self):
        ra, rb = self.reader(client=self.a), self.reader(client=self.b)
        self.assertEqual(ra.prepare("type", {"text": "{{client:contact.phone}}"}, None).args["text"], A["phone"])
        self.assertEqual(rb.prepare("type", {"text": "{{client:contact.phone}}"}, None).args["text"], B["phone"])
        self.assertEqual(ra.prepare("type", {"text": "{{client:location.address.full}}"}, None).args["text"], "1 Alpha Street, Alphaville, FL, US, 34652")
        self.assertEqual(rb.prepare("type", {"text": "{{client:location.address.postal_code}}"}, None).args["text"], "30301")

    def test_manifests_name_only_this_clients_fields_and_never_a_value_or_another_client(self):
        els = [{"role": "textbox", "name": "Business Name"}, {"role": "textbox", "name": "Phone Number"}, {"role": "textbox", "name": "GSTIN Number"}]
        m = self.reader(client=self.a).plan(els)
        self.assertEqual(sorted(m.keys), ["contact.phone", "identity.business_name", "taxation.gstin"])
        self.assertIn("CLIENT FIELDS", m.text)
        self.assertIn("{{client:KEY}}", m.text)
        for secret in (A["phone"], B["phone"], A["gstin"], B["gstin"], OWNER["name"], OWNER["phone"], A["name"], B["name"], "Alpha", "Beta"):
            self.assertNotIn(secret, m.text, secret)
        self.assertNotIn("{{biz:", m.text)

    def test_a_client_task_cannot_reach_the_owners_values_and_an_owner_task_cannot_reach_a_clients(self):
        ra = self.reader(client=self.a)
        p = ra.prepare("type", {"text": "{{biz:identity.legal_name}}"}, None)
        self.assertEqual(p.status, "feedback")
        self.assertIn("specific client", p.message)
        self.assertIsNone(p.args)
        owner = BrainReader(self.store, self.docs, "t2", "Fill in my business name.")
        p = owner.prepare("type", {"text": "{{client:contact.phone}}"}, None)
        self.assertEqual(p.status, "feedback")
        self.assertIsNone(p.args)
        self.assertNotIn(A["phone"], repr(p) + p.message)

    def test_a_field_missing_for_the_client_is_missing_even_if_the_owner_or_another_client_has_it(self):
        self.put(self.b, "contact.person", "Bea Roofer")
        p = self.reader(client=self.a).prepare("type", {"text": "{{client:contact.person}}"}, None)
        self.assertEqual((p.status, p.kind), ("wait", "missing_info"))
        self.assertNotIn("Bea", p.message)

    def test_conflict_for_one_client_is_surfaced_without_values_or_the_other_clients(self):
        self.put(self.a, "contact.phone", "+1 555 999 9999", "DOCUMENT", doc_id="doc_x")
        p = self.reader(client=self.a).prepare("type", {"text": "{{client:contact.phone}}"}, None)
        self.assertEqual((p.status, p.kind), ("wait", "conflict"))
        for v in (A["phone"], "999 9999", B["phone"]):
            self.assertNotIn(v, p.message)
        ok = self.reader(client=self.b).prepare("type", {"text": "{{client:contact.phone}}"}, None)
        self.assertEqual(ok.status, "ok")

    def test_resolved_client_values_are_scrubbed_from_logs_until_released(self):
        r = self.reader(client=self.a)
        r.prepare("type", {"text": "{{client:taxation.gstin}}"}, None)
        self.assertTrue(SECRETS.contains(A["gstin"]))
        self.assertFalse(SECRETS.contains(B["gstin"]), "only what this task resolved is registered")
        r.release()
        self.assertFalse(SECRETS.contains(A["gstin"]))

    def test_audit_of_a_client_task_names_the_client_and_never_a_value(self):
        r = self.reader(client=self.a)
        r.prepare("type", {"text": "{{client:taxation.gstin}}"}, None)
        r.prepare("type", {"text": "{{client:contact.person}}"}, None)
        rows = self.store.audit_tail(50, task_id="task_t")
        self.assertEqual([x["scope"] for x in rows], [self.a, self.a])
        self.assertNotIn(A["gstin"], json.dumps(rows))

    def test_the_client_cannot_be_switched_after_values_were_filled(self):
        r = self.reader(client=self.a)
        r.prepare("type", {"text": "{{client:contact.phone}}"}, None)
        with self.assertRaises(ValueError):
            r.select_client(self.b, B["name"])

    def test_a_task_about_a_client_with_no_client_selected_asks_never_guesses(self):
        r = BrainReader(self.store, self.docs, "t3", "Research my client.", client_intent=True)
        self.assertTrue(r.active and r.client_pending)
        m = r.plan([{"role": "textbox", "name": "Business Name"}])
        self.assertIn("no client is selected", m.text)
        self.assertEqual(m.entries, [])
        p = r.prepare("type", {"text": "{{client:contact.phone}}"}, None)
        self.assertEqual((p.status, p.kind), ("wait", "client_needed"))
        self.assertEqual(r.prepare("type", {"text": "{{biz:identity.legal_name}}"}, None).status, "feedback")
        r.select_client(self.b, B["name"])
        self.assertEqual(r.prepare("type", {"text": "{{client:contact.phone}}"}, None).args["text"], B["phone"])

    def test_instruction_driven_keys_are_offered_for_a_client_task(self):
        self.put(self.a, "seo.target_keywords[0]", "locksmith")
        m = self.reader("Search for the client's target keyword", client=self.a).plan([{"role": "searchbox", "name": "Search"}])
        self.assertIn("seo.target_keywords[0]", m.keys)
        self.assertNotIn("locksmith", m.text)


class PickClientTests(Base):
    def test_a_unique_name_in_the_wording_selects_that_client(self):
        for text, want in (("Research my locksmith client", self.a), ("Fill in the roofing client's business information", self.b),
                           ("check example-b.test for the client", self.b), ("do the Example Locksmith LLC task", self.a)):
            p = pick_client(self.store, text)
            self.assertEqual(p.client_id, want, text)
            self.assertEqual(p.reason, "matched by name")

    def test_nothing_is_guessed_when_nothing_or_several_match(self):
        self.assertIsNone(pick_client(self.store, "Research my client").client_id)
        self.assertIsNone(pick_client(self.store, "Fill in the selected client's business information.").client_id)
        both = pick_client(self.store, "compare the locksmith and roofing clients")
        self.assertIsNone(both.client_id)
        self.assertEqual(sorted(both.candidates), sorted([A["name"], B["name"]]))
        self.assertIsNone(pick_client(self.store, "Research my plumbing client").client_id)

    def test_generic_words_and_company_suffixes_never_decide(self):
        self.assertIsNone(pick_client(self.store, "update the llc company business client services").client_id)

    def test_archived_clients_are_not_picked(self):
        self.store.set_client_archived(self.a, True)
        self.assertIsNone(pick_client(self.store, "Research my locksmith client").client_id)

    def test_a_trade_name_or_website_host_can_identify_a_client(self):
        c = self.store.create_client("Zed Holdings")["client_id"]
        self.put(c, "identity.trade_name", "Quickfix Doors")
        self.assertEqual(pick_client(self.store, "work on the Quickfix client").client_id, c)


class ClientDocumentTests(Base):
    def make(self, name, text="Certificate of Registration\n"):
        d = self.root / name
        d.mkdir()
        (d / "gst_certificate.txt").write_text(text, encoding="utf-8")
        self.folders.allow(str(d))
        return d

    def ready(self, scope, name):
        d = self.make(name)
        self.docs.enroll(str(d), name, scope=scope)
        self.docs.scan()
        self.docs.index()
        doc = next(x for x in self.docs.list(scope=scope))
        self.docs.approve(doc["doc_id"])
        return d, doc["doc_id"]

    def test_a_folder_belongs_to_exactly_one_scope(self):
        d = self.make("shared")
        self.docs.enroll(str(d), scope=self.a)
        with self.assertRaises(StoreError) as cm:
            self.docs.enroll(str(d), scope=self.b)
        self.assertEqual(cm.exception.code, "FOLDER_SCOPE")
        with self.assertRaises(StoreError):
            self.docs.enroll(str(d))   # the owner's own business either
        self.assertEqual(self.docs.enroll(str(d), scope=self.a)["client_id"], self.a)

    def test_documents_are_listed_and_offered_only_inside_their_own_scope(self):
        self.ready(self.a, "docs_a")
        self.assertEqual(len(self.docs.list(scope=self.a)), 1)
        self.assertEqual(self.docs.list(scope=self.b), [])
        self.assertEqual(self.docs.list(scope="me"), [])
        self.assertEqual(self.docs.approved_manifest(self.a), [{"token": "gst_certificate", "label": "GST registration certificate", "count": 1}])
        self.assertEqual(self.docs.approved_manifest(self.b), [])
        self.assertEqual(self.docs.approved_manifest("me"), [])

    def test_a_document_of_client_a_is_unavailable_to_client_b_and_to_the_owner_even_by_exact_id(self):
        _, did = self.ready(self.a, "docs_a")
        self.docs.resolve_for_upload("gst_certificate", self.a)
        for scope in (self.b, "me"):
            for token in ("gst_certificate", did):
                with self.assertRaises(DocNotAvailable, msg=(scope, token)) as cm:
                    self.docs.resolve_for_upload(token, scope)
                self.assertEqual(cm.exception.status, "MISSING")

    def test_each_client_can_have_its_own_certificate_without_ambiguity(self):
        self.ready(self.a, "docs_a")
        self.ready(self.b, "docs_b")
        pa, ia = self.docs.resolve_for_upload("gst_certificate", self.a)
        pb, ib = self.docs.resolve_for_upload("gst_certificate", self.b)
        self.assertNotEqual((pa, ia["doc_id"]), (pb, ib["doc_id"]))
        self.assertIn("docs_a", pa)
        self.assertIn("docs_b", pb)

    def test_what_a_document_says_becomes_a_candidate_for_that_client_only(self):
        d = self.make("docs_a2", "Legal Name of Business: Example Locksmith LLC\nTrade Name: Alpha Locks\n")
        self.docs.enroll(str(d), scope=self.a)
        self.docs.scan()
        self.docs.index()
        self.assertEqual(self.store.status("identity.legal_name", self.a).status, "UNVERIFIED")
        self.assertEqual(self.store.status("identity.legal_name", self.a).facts[0].source_type, "DOCUMENT")
        self.assertEqual(self.store.status("identity.legal_name", self.b).status, "MISSING")
        self.assertEqual(self.store.status("identity.legal_name").status, "VERIFIED", "the owner's own legal name is untouched")
        self.assertEqual(self.store.get_value("identity.legal_name").value, OWNER["name"])

    def test_two_documents_of_one_client_that_disagree_conflict_inside_that_client_only(self):
        for n, v in (("d1", "Legal Name of Business: Alpha One LLC"), ("d2", "Legal Name of Business: Alpha Two LLC")):
            self.docs.enroll(str(self.make(n, v + "\n")), scope=self.a)
        self.docs.scan()
        self.docs.index()
        self.assertEqual(self.store.status("identity.legal_name", self.a).status, "CONFLICTING")
        self.assertEqual(self.store.status("identity.legal_name", self.b).status, "MISSING")

    def test_the_document_client_association_is_recorded_and_listed(self):
        self.ready(self.a, "docs_a")
        row = self.docs.list(scope=self.a)[0]
        self.assertEqual(row["client_id"], self.a)
        self.assertEqual(self.docs.enrolled(self.a)[0]["client_id"], self.a)
        self.assertEqual(self.docs.enrolled(self.b), [])
        self.assertNotIn(str(self.root), json.dumps(self.docs.list()))

    def test_revoking_a_clients_folder_deactivates_only_its_documents(self):
        da, _ = self.ready(self.a, "docs_a")
        self.ready(self.b, "docs_b")
        self.assertTrue(self.docs.revoke(str(da)))
        self.assertEqual(self.docs.list(scope=self.a)[0]["state"], "MISSING")
        self.assertEqual(self.docs.list(scope=self.b)[0]["state"], "APPROVED")
        self.docs.resolve_for_upload("gst_certificate", self.b)

    def test_document_uses_keep_every_revalidation_for_clients(self):
        d, did = self.ready(self.a, "docs_a")
        (d / "gst_certificate.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaises(DocNotAvailable) as cm:
            self.docs.resolve_for_upload("gst_certificate", self.a)
        self.assertEqual(cm.exception.status, "CHANGED")
        self.folders.revoke(str(d))
        with self.assertRaises(DocNotAvailable):
            self.docs.resolve_for_upload("gst_certificate", self.a)


class OnboardingTests(Base):
    def drive(self, scope, answers, only=None):
        said, asked = [], []
        it = iter(answers)

        def ask(prompt, hidden):
            asked.append((prompt, hidden))
            return next(it, "")

        return run_onboarding(self.store, scope, ask, said.append, only=only), said, asked

    def test_blank_answers_store_nothing_and_nothing_is_guessed(self):
        c = self.store.create_client("Blank Client LLC")["client_id"]
        res, said, asked = self.drive(c, [])
        self.assertEqual(res["stored"], 0)
        self.assertGreater(res["skipped"], 10)
        self.assertEqual(self.store.list_fields(scope=c, include_missing=False), [])

    def test_the_wizard_stores_only_what_was_typed_as_user_entered_and_hides_high_fields(self):
        c = self.store.create_client("New Client LLC")["client_id"]
        answers = ["New Client LLC", "https://example-n.test", "TEST-N-GSTIN"]
        res, said, asked = self.drive(c, answers, only=["identity.business_name", "contact.website", "taxation.gstin"])
        self.assertEqual(res["stored"], 3)
        self.assertEqual(self.store.get_value("identity.business_name", c).value, "New Client LLC")
        hidden = {p.split("[")[0].strip(): h for p, h in asked}
        self.assertTrue(all(h for p, h in asked if "GSTIN" in p), "HIGH fields are read with a hidden prompt")
        self.assertFalse(any(h for p, h in asked if "Website" in p))
        self.assertEqual(self.store.status("taxation.gstin", c).facts[0].source_type, "USER_ENTERED")
        for say in said:
            self.assertNotIn("TEST-N-GSTIN", say)

    def test_existing_values_are_kept_unless_the_user_asks_to_replace_them(self):
        res, said, asked = self.drive(self.a, [""], only=["contact.website"])
        self.assertEqual((res["kept"], res["stored"]), (1, 0))
        self.assertEqual(self.store.get_value("contact.website", self.a).value, A["site"])
        res, _, _ = self.drive(self.a, ["https://example-a2.test"], only=["contact.website"])
        self.assertEqual(self.store.get_value("contact.website", self.a).value, "https://example-a2.test")

    def test_conflicts_are_reported_and_left_for_the_user_to_resolve(self):
        self.put(self.a, "contact.website", "https://other-a.test", "DOCUMENT", doc_id="d")
        res, said, _ = self.drive(self.a, ["x"], only=["contact.website"])
        self.assertEqual(res["conflicts"], 1)
        self.assertTrue(any("CONFLICTING" in s for s in said))
        self.assertEqual(self.store.status("contact.website", self.a).status, "CONFLICTING")

    def test_a_secret_typed_into_a_field_is_refused(self):
        c = self.store.create_client("Secret Client LLC")["client_id"]
        res, said, _ = self.drive(c, ["4111 1111 1111 1111"], only=["identity.description"])
        self.assertEqual(res["stored"], 0)
        self.assertTrue(any("payment card" in s for s in said))

    def test_repeatable_fields_take_several_entries_and_a_compound_is_entered_part_by_part(self):
        c = self.store.create_client("List Client LLC")["client_id"]
        res, _, _ = self.drive(c, ["locksmith", "emergency locksmith", ""], only=["seo.target_keywords[]"])
        self.assertEqual(self.store.get_value("seo.target_keywords[1]", c).value, "emergency locksmith")
        res, _, _ = self.drive(c, ["y", "9 Gamma Road", "", "Gammaton", "TX", "US", "75001"], only=["location.address"])
        self.assertEqual(self.store.get_value("location.address.city", c).value, "Gammaton")

    def test_the_owner_flow_uses_the_owners_catalog_and_scope(self):
        said = []
        run_onboarding(self.store, "me", lambda p, h: "Example Growth Studio" if "Trade name" in p else "", said.append, only=["identity.trade_name", "identity.legal_name"])
        self.assertEqual(self.store.get_value("identity.trade_name").value, "Example Growth Studio")
        self.assertEqual(self.store.get_value("identity.legal_name").value, OWNER["name"])
        with self.assertRaises(NotAvailable):
            self.store.get_value("identity.trade_name", self.a)


class MigrationTests(unittest.TestCase):
    V1 = """
    CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE facts (fact_id TEXT PRIMARY KEY, field_key TEXT NOT NULL, instance INTEGER, value_plain TEXT, value_enc BLOB, display_mask TEXT, value_hash TEXT,
      sensitivity TEXT NOT NULL, state TEXT NOT NULL, verification TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT, doc_id TEXT, format_ok INTEGER, format_note TEXT,
      confirmed_at REAL, created_at REAL, updated_at REAL, supersedes TEXT);
    CREATE INDEX facts_field ON facts(field_key, instance, state);
    CREATE TABLE enrolled_folders (folder_id TEXT PRIMARY KEY, path TEXT NOT NULL, label TEXT, added_at REAL);
    CREATE TABLE documents (doc_id TEXT PRIMARY KEY, folder_id TEXT NOT NULL, rel_path TEXT NOT NULL, ext TEXT, size INTEGER, sha256 TEXT, mtime REAL, doc_type TEXT, label TEXT,
      sensitivity TEXT, state TEXT, verification TEXT, source TEXT, indexed_at REAL, extractor TEXT, extract_note TEXT, fields_found INTEGER DEFAULT 0, created_at REAL, updated_at REAL);
    CREATE TABLE audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event TEXT, task_id TEXT, field_key TEXT, fact_id TEXT, doc_id TEXT, provider TEXT, locality TEXT, actor TEXT, outcome TEXT, detail TEXT);
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
    """

    def test_a_version_1_database_is_migrated_in_place_after_a_backup_and_keeps_its_meaning(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = Path(d) / "business.db"
            # a real v1 file: create it with the NEW code's cipher so the protected pepper and values are valid, then rewrite it into the v1 shape
            s = BusinessStore(path)
            s.put_candidate("identity.legal_name", "Example Growth LLC", source_type="USER_ENTERED")
            s.put_candidate("taxation.gstin", "TEST-GSTIN-123", source_type="USER_ENTERED")
            pepper = s.meta("pepper")
            with s.conn() as c:
                rows = [tuple(r) for r in c.execute("SELECT fact_id,field_key,instance,value_plain,value_enc,display_mask,value_hash,sensitivity,state,verification,source_type,source_ref,doc_id,"
                                                    "format_ok,format_note,confirmed_at,created_at,updated_at,supersedes FROM facts").fetchall()]
            del s
            path.unlink()
            for ext in ("-wal", "-shm"):
                Path(str(path) + ext).unlink(missing_ok=True)
            with contextlib.closing(sqlite3.connect(path)) as con:
                con.executescript(self.V1)
                con.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                con.executemany("INSERT INTO schema_meta VALUES (?,?)", [("schema_version", "1"), ("catalog_version", "1"), ("pepper", pepper), ("cipher", "dpapi")])
                con.execute("INSERT INTO audit(ts,event,outcome) VALUES (1,'old_event','ok')")
                con.commit()
            s2 = BusinessStore(path)
            self.assertEqual(s2.migrated_from, 1)
            self.assertTrue(list(Path(d).glob("business.db.bak-*-pre-v4")), "an automatic backup is taken before migrating")
            self.assertEqual(s2.meta("schema_version"), "4")
            self.assertEqual(s2.get_value("identity.legal_name").value, "Example Growth LLC", "v1 facts are the owner's own business")
            self.assertEqual(s2.get_value("taxation.gstin").value, "TEST-GSTIN-123")
            self.assertEqual({r["scope"] for r in s2.audit_tail(10)}, {""})
            cid = s2.create_client("Migrated Client LLC")["client_id"]
            s2.put_candidate("contact.phone", "+1 555 030 3000", source_type="USER_ENTERED", scope=cid)
            self.assertEqual(s2.get_value("contact.phone", cid).value, "+1 555 030 3000")
            self.assertTrue(s2.integrity_ok())
            self.assertIsNone(BusinessStore(path).migrated_from, "a second open does not migrate again")

    def test_a_fresh_database_is_created_at_the_current_version(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            s = BusinessStore(Path(d) / "b.db")
            self.assertEqual((s.meta("schema_version"), s.migrated_from), ("4", None))
            with s.conn() as c:
                self.assertTrue({"scope"} <= {r["name"] for r in c.execute("PRAGMA table_info(facts)")})
                self.assertTrue({"client_id"} <= {r["name"] for r in c.execute("PRAGMA table_info(documents)")})


class ResearchObservationTests(Base):
    def setUp(self):
        super().setUp()
        self.log = ResearchLog(self.store)

    def session(self, cid=None, **kw):
        return self.log.start_session(cid or self.a, "Why is the client not ranking for locksmith?", query="locksmith", location="New Port Richey, FL", search_engine="google", **kw)

    def test_a_session_belongs_to_exactly_one_client(self):
        s = self.session()
        self.assertEqual((s["client_id"], s["status"]), (self.a, "open"))
        with self.assertRaises(StoreError):
            self.log.start_session("me", "x")
        with self.assertRaises(StoreError):
            self.log.start_session("cli_nope", "x")
        self.store.set_client_archived(self.b, True)
        with self.assertRaises(StoreError):
            self.log.start_session(self.b, "x")

    def test_observations_carry_their_context_and_a_timestamp(self):
        s = self.session()
        o = self.log.add_observation(s["session_id"], "keyword_rank", query="locksmith", location="New Port Richey, FL", search_engine="google", result_type="organic",
                                     position=3, url="https://competitor.test/", domain="competitor.test", business="Competitor Locks", evidence="screenshot saved", source="serp")
        for k in ("query", "location", "search_engine", "result_type", "position", "url", "domain", "business", "observed_at", "session_id", "client_id"):
            self.assertTrue(o[k] not in (None, ""), k)
        self.assertEqual(o["client_id"], self.a)
        self.assertLessEqual(abs(o["observed_at"] - time.time()), 5)

    def test_a_ranking_without_query_location_and_engine_is_refused(self):
        s = self.session()
        for missing in ("query", "location", "search_engine"):
            kw = {"query": "locksmith", "location": "NPR, FL", "search_engine": "google", "position": 1}
            kw[missing] = ""
            with self.assertRaises(StoreError) as cm:
                self.log.add_observation(s["session_id"], "keyword_rank", **kw)
            self.assertEqual(cm.exception.code, "INCOMPLETE_OBSERVATION")
        with self.assertRaises(StoreError):
            self.log.add_observation(s["session_id"], "keyword_rank", query="q", location="l", search_engine="g", position=0)
        with self.assertRaises(StoreError):
            self.log.add_observation(s["session_id"], "made_up_kind")

    def test_observations_are_append_only(self):
        s = self.session()
        o = self.log.add_observation(s["session_id"], "competitor", business="Competitor Locks", evidence="offers 24/7 service")
        with contextlib.closing(sqlite3.connect(self.root / "data" / "business.db")) as con:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("UPDATE research_observations SET position=1")
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM research_observations")
        self.assertEqual(self.log.get_observation(o["obs_id"])["evidence"], "offers 24/7 service")

    def test_a_closed_session_takes_no_more_observations_so_history_stays_separate(self):
        s = self.session()
        self.log.finish_session(s["session_id"], findings="Competitor X observed first.", recommendations="Improve GBP categories.")
        with self.assertRaises(StoreError) as cm:
            self.log.add_observation(s["session_id"], "competitor", business="Y")
        self.assertEqual(cm.exception.code, "SESSION_CLOSED")
        self.assertEqual(self.log.get_session(s["session_id"])["findings"], "Competitor X observed first.")

    def test_observations_are_scoped_per_client(self):
        sa, sb = self.session(self.a), self.session(self.b)
        self.log.add_observation(sa["session_id"], "keyword_rank", query="locksmith", location="L", search_engine="g", position=2, business="A Locks")
        self.log.add_observation(sb["session_id"], "keyword_rank", query="roofing", location="L", search_engine="g", position=5, business="B Roofs")
        self.assertEqual([o["business"] for o in self.log.observations(self.a)], ["A Locks"])
        self.assertEqual([o["business"] for o in self.log.observations(self.b)], ["B Roofs"])
        self.assertEqual(self.log.summary(self.a), {"sessions": 1, "observations": 1})

    def test_history_supports_later_ranking_comparison_over_time(self):
        for pos, t in ((7, 1000.0), (4, 2000.0), (2, 3000.0)):
            s = self.session()
            self.log.add_observation(s["session_id"], "keyword_rank", query="locksmith", location="New Port Richey, FL", search_engine="google", position=pos,
                                     business="Example Locksmith LLC", domain="example-a.test", observed_at=t)
            self.log.finish_session(s["session_id"])
        h = self.log.history(self.a, "locksmith", "New Port Richey, FL", "example-a.test")
        self.assertEqual([o["position"] for o in h], [7, 4, 2])
        self.assertEqual([o["observed_at"] for o in h], [1000.0, 2000.0, 3000.0])

    def test_the_only_sentence_is_an_observation_with_its_context_never_a_standing_claim(self):
        s = self.session()
        o = self.log.add_observation(s["session_id"], "keyword_rank", query="locksmith", location="New Port Richey, FL", search_engine="google", result_type="organic",
                                     position=1, business="Competitor Locks", domain="competitor.test")
        text = ResearchLog.describe(o)
        for part in ("Competitor Locks", "was observed", "position #1", "locksmith", "New Port Richey, FL", "google", s["session_id"], "during research session"):
            self.assertIn(part, text)
        for banned in (" is #1", " is number one", "permanently", "always ranks"):
            self.assertNotIn(banned, text)

    def test_research_text_is_scrubbed_like_everything_else(self):
        s = self.session()
        o = self.log.add_observation(s["session_id"], "other", evidence="the owner wrote ABCDE1234F in a note")
        self.assertNotIn("ABCDE1234F", json.dumps(o))

    def test_research_adds_no_search_engine_and_no_model_calls(self):
        text = Path(research_mod.__file__).read_text(encoding="utf-8")
        for banned in ("aiohttp", "urllib", "requests", "import socket", "subprocess", "playwright", "reasoning", "provider"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
