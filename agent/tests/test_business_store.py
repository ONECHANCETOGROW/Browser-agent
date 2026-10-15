"""Business Profile storage: structured facts, source/verification, conflicts, sensitivity, DPAPI protection, audit. Synthetic data only."""
import contextlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from octg_agent.business import validators as v
from octg_agent.business.cipher import Cipher, CipherError, DpapiCipher, FernetFileCipher, PlainCipher
from octg_agent.business.schema import BY_KEY, CATALOG, KeyError_, match_label, parse_key
from octg_agent.business.store import BusinessStore, NotAvailable, StoreError, mask_value
from octg_agent.sensitive import SECRETS

NAME, GSTIN, PAN = "Example Growth LLC", "TEST-GSTIN-123", "ABCDE1234F"
GOOD_GSTIN = v.gstin_with_checksum("27ABCDE1234F1Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.s = BusinessStore(self.dir / "business.db")

    def tearDown(self):
        SECRETS.clear()
        self.tmp.cleanup()

    def put(self, key, value, src="USER_ENTERED", **kw):
        return self.s.put_candidate(key, value, source_type=src, **kw)


class ProfileCreationTests(Base):
    def test_every_fact_carries_the_required_metadata(self):
        r = self.put("identity.legal_name", NAME)
        st = self.s.status("identity.legal_name")
        f = st.facts[0]
        self.assertEqual((st.status, f.sensitivity, f.verification, f.source_type, f.state), ("VERIFIED", "MEDIUM", "VERIFIED", "USER_ENTERED", "LIVE"))
        self.assertTrue(f.updated_at <= time.time() and f.fact_id == r.fact_id and f.mask)
        self.assertNotIn(NAME, json.dumps(st.to_dict()), "status views carry a mask, never the value")

    def test_unset_fields_are_MISSING(self):
        self.assertEqual(self.s.status("taxation.pan").status, "MISSING")
        with self.assertRaises(NotAvailable) as cm:
            self.s.get_value("taxation.pan")
        self.assertEqual(cm.exception.status, "MISSING")

    def test_list_fields_covers_the_whole_catalog(self):
        keys = {f.key for f in self.s.list_fields()}
        self.assertIn("identity.legal_name", keys)
        self.assertIn("owners[0].name", keys)
        self.assertIn("addresses.principal", keys)

    def test_catalog_has_no_banking_or_aadhaar_in_v1(self):
        keys = " ".join(f.key for f in CATALOG)
        for banned in ("bank", "aadhaar", "aadhar", "account_number", "ifsc", "password", "otp"):
            self.assertNotIn(banned, keys)

    def test_unknown_field_is_refused(self):
        with self.assertRaises(StoreError) as cm:
            self.put("banking.account_number", "123456789012")
        self.assertEqual(cm.exception.code, "UNKNOWN_FIELD")

    def test_instances_are_independent(self):
        self.put("owners[0].name", "Alice Example")
        self.put("owners[1].name", "Bob Example")
        self.assertEqual(self.s.get_value("owners[0].name").value, "Alice Example")
        self.assertEqual(self.s.get_value("owners[1].name").value, "Bob Example")
        self.assertEqual(self.s.status("owners[1].name").status, "VERIFIED")

    def test_custom_fields_default_to_HIGH(self):
        self.put("other.food_licence", "FL-123")
        self.assertEqual(self.s.status("other.food_licence").sensitivity, "HIGH")

    def test_compound_address_is_one_unit_and_parts_cannot_be_set_separately(self):
        self.put("addresses.principal", {"line1": "123 Example Street", "state": "Exampleshire", "pin": "560001"})
        self.assertEqual(self.s.get_value("addresses.principal.pin").value, "560001")
        self.assertEqual(self.s.get_value("addresses.principal.full").value, "123 Example Street, Exampleshire, 560001")
        with self.assertRaises(StoreError):
            self.put("addresses.principal.pin", "999999")
        with self.assertRaises(StoreError):
            self.put("addresses.principal", {"bogus": "x"})
        with self.assertRaises(NotAvailable):  # a part that was never given is MISSING, not made up
            self.s.get_value("addresses.principal.line2")


class VerificationAndSourceTests(Base):
    def test_user_entry_is_verified_document_and_import_are_not(self):
        self.assertEqual(self.put("identity.legal_name", NAME).status, "VERIFIED")
        self.assertEqual(self.put("taxation.pan", PAN, "DOCUMENT", doc_id="doc_1").status, "UNVERIFIED")
        self.assertEqual(self.put("contact.email", "ops@example.test", "IMPORT", source_ref="file.json").status, "UNVERIFIED")
        self.assertEqual(self.put("contact.phone", "9876543210", "IMPORT", assume_verified=True).status, "VERIFIED")

    def test_unverified_values_are_not_available_for_use(self):
        self.put("taxation.pan", PAN, "DOCUMENT", doc_id="doc_1")
        with self.assertRaises(NotAvailable) as cm:
            self.s.get_value("taxation.pan")
        self.assertEqual(cm.exception.status, "UNVERIFIED")
        self.assertIn("document doc_1", cm.exception.sources)

    def test_a_value_cannot_enter_from_a_model_or_as_pre_confirmed(self):
        for src in ("MODEL", "LLM", "USER_CONFIRMED", "WEB", ""):
            with self.assertRaises(StoreError, msg=src):
                self.put("identity.legal_name", NAME, src)

    def test_confirm_turns_a_document_value_into_a_user_confirmed_fact(self):
        r = self.put("taxation.pan", PAN, "DOCUMENT", doc_id="doc_1")
        st = self.s.confirm(r.fact_id, actor="test")
        self.assertEqual(st.status, "VERIFIED")
        self.assertEqual(st.facts[0].source_type, "USER_CONFIRMED")
        self.assertEqual(st.facts[0].doc_id, "doc_1", "the document origin is kept")
        self.assertEqual(self.s.get_value("taxation.pan").value, PAN)

    def test_the_same_value_from_two_sources_is_not_a_conflict(self):
        self.put("taxation.pan", PAN)
        r = self.put("taxation.pan", PAN.lower(), "DOCUMENT", doc_id="doc_1")
        self.assertFalse(r.created)
        self.assertEqual(self.s.status("taxation.pan").status, "VERIFIED")

    def test_format_problems_warn_but_never_alter_or_block(self):
        r = self.put("taxation.gstin", GSTIN)
        self.assertTrue(r.warnings)
        self.assertEqual(self.s.get_value("taxation.gstin").value, GSTIN, "stored exactly as entered")
        self.assertFalse(self.s.status("taxation.gstin").facts[0].format_ok)
        self.assertTrue(self.put("taxation.gstin", GOOD_GSTIN, replace=True).warnings == [])


class ConflictTests(Base):
    def test_two_different_values_are_CONFLICTING_and_never_chosen(self):
        self.put("addresses.principal", {"line1": "123 Example Street"})
        r = self.put("addresses.principal", {"line1": "9 Other Road"}, "DOCUMENT", doc_id="doc_2")
        self.assertEqual(r.status, "CONFLICTING")
        self.assertTrue(any("CONFLICTING" in w for w in r.warnings))
        with self.assertRaises(NotAvailable) as cm:
            self.s.get_value("addresses.principal.full")
        self.assertEqual(cm.exception.status, "CONFLICTING")
        self.assertEqual(len(cm.exception.sources), 2)

    def test_resolving_makes_the_chosen_one_user_confirmed_and_supersedes_the_rest(self):
        a = self.put("addresses.principal", {"line1": "123 Example Street"})
        b = self.put("addresses.principal", {"line1": "9 Other Road"}, "DOCUMENT", doc_id="doc_2")
        st = self.s.resolve("addresses.principal", b.fact_id, actor="test")
        self.assertEqual(st.status, "VERIFIED")
        chosen = next(f for f in st.facts if f.fact_id == b.fact_id)
        self.assertEqual((chosen.verification, chosen.source_type), ("VERIFIED", "USER_CONFIRMED"))
        self.assertEqual(next(f for f in st.facts if f.fact_id == a.fact_id).state, "SUPERSEDED")
        self.assertEqual(self.s.get_value("addresses.principal.line1").value, "9 Other Road")

    def test_rejecting_a_document_value_resolves_the_conflict_and_a_rescan_does_not_bring_it_back(self):
        self.put("taxation.pan", PAN)
        r = self.put("taxation.pan", "ZZZZZ9999Z", "DOCUMENT", doc_id="doc_2")
        self.assertEqual(self.s.status("taxation.pan").status, "CONFLICTING")
        self.s.reject(r.fact_id)
        self.assertEqual(self.s.status("taxation.pan").status, "VERIFIED")
        again = self.put("taxation.pan", "ZZZZZ9999Z", "DOCUMENT", doc_id="doc_2")
        self.assertFalse(again.created)
        self.assertEqual(self.s.status("taxation.pan").status, "VERIFIED")

    def test_a_verified_value_is_never_silently_overwritten(self):
        self.put("identity.legal_name", NAME)
        r = self.put("identity.legal_name", "Another Name Inc")  # the user types a different value without --replace
        self.assertEqual(r.status, "CONFLICTING")
        self.assertEqual(len([f for f in self.s.status("identity.legal_name").live]), 2)

    def test_replace_is_explicit_and_only_for_the_users_own_entry(self):
        self.put("identity.legal_name", NAME)
        with self.assertRaises(StoreError):
            self.put("identity.legal_name", "Doc Name", "DOCUMENT", replace=True)
        self.put("identity.legal_name", "Renamed LLC", replace=True)
        self.assertEqual(self.s.get_value("identity.legal_name").value, "Renamed LLC")
        self.assertEqual(self.s.status("identity.legal_name").status, "VERIFIED")

    def test_resolve_refuses_a_fact_from_another_field(self):
        a = self.put("identity.legal_name", NAME)
        self.put("identity.trade_name", "Brand")
        with self.assertRaises(StoreError):
            self.s.resolve("identity.trade_name", a.fact_id)

    def test_document_changes_retire_what_the_document_used_to_say(self):
        self.put("taxation.pan", PAN, "DOCUMENT", doc_id="doc_9")
        self.assertEqual(self.s.supersede_document_facts("doc_9"), 1)
        self.assertEqual(self.s.status("taxation.pan").status, "MISSING")


class SensitivityTests(Base):
    def test_catalog_classification(self):
        expect = {"taxation.pan": "HIGH", "taxation.gstin": "HIGH", "registration.cin": "HIGH", "owners[].pan": "HIGH", "contact.email": "MEDIUM", "contact.phone": "MEDIUM",
                  "addresses.principal": "MEDIUM", "identity.legal_name": "MEDIUM", "contact.website": "LOW", "services.description": "LOW", "identity.description": "LOW"}
        for k, lvl in expect.items():
            self.assertEqual(BY_KEY[k].sensitivity, lvl, k)

    def test_sensitivity_can_be_raised_but_lowering_needs_an_explicit_confirmation(self):
        self.put("contact.website", "https://example.test", sensitivity="HIGH")
        self.assertEqual(self.s.status("contact.website").sensitivity, "HIGH")
        with self.assertRaises(StoreError) as cm:
            self.put("taxation.pan", PAN, sensitivity="LOW")
        self.assertEqual(cm.exception.code, "LOWERING_REFUSED")
        self.put("taxation.pan", PAN, sensitivity="LOW", allow_lower=True)
        self.assertEqual(self.s.status("taxation.pan").sensitivity, "LOW")

    def test_passwords_otps_keys_and_cards_are_never_stored(self):
        cases = [("other.password", "x"), ("other.my_otp", "123456"), ("other.api_key", "x"), ("other.cvv", "123"), ("other.note", "sk-live-abcdefghijklmnopqrstuvwx"),
                 ("other.note2", "AIza" "SyA-synthetic-synthetic-synthetic1"), ("other.note3", "4111 1111 1111 1111"), ("other.note4", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig")]
        for key, value in cases:
            with self.assertRaises(StoreError, msg=key) as cm:
                self.put(key, value)
            self.assertEqual(cm.exception.code, "SECRET_NOT_STORABLE", key)
        self.assertEqual([f for f in self.s.list_fields(include_missing=False)], [])
        events = [r["event"] for r in self.s.audit_tail(50)]
        self.assertEqual(events.count("rejected_secret"), len(cases))
        self.assertNotIn("4111", json.dumps(self.s.audit_tail(50)))

    def test_a_secret_is_refused_even_inside_a_catalog_field(self):
        with self.assertRaises(StoreError):
            self.put("identity.description", "4111111111111111")


class EncryptionTests(Base):
    def test_high_values_are_not_readable_in_the_database_file(self):
        self.put("taxation.gstin", GSTIN)
        self.put("taxation.pan", PAN)
        self.put("identity.legal_name", NAME)
        raw = (self.dir / "business.db").read_bytes() + b"".join(p.read_bytes() for p in self.dir.glob("business.db-*"))
        self.assertNotIn(GSTIN.encode(), raw)
        self.assertNotIn(PAN.encode(), raw)
        self.assertNotIn(b"TEST-GSTIN", raw)
        self.assertEqual(self.s.counts()["encrypted_facts"], 2)

    def test_dpapi_is_the_default_here_and_round_trips(self):
        self.assertEqual(self.s.cipher.name, "dpapi")
        self.assertTrue(self.s.cipher.strong)
        c = DpapiCipher()
        blob = c.encrypt(b"secret value")
        self.assertNotIn(b"secret value", blob)
        self.assertEqual(c.decrypt(blob), b"secret value")

    def test_a_damaged_or_foreign_blob_cannot_be_decrypted_and_never_yields_a_wrong_value(self):
        c = DpapiCipher()
        blob = bytearray(c.encrypt(b"abc"))
        blob[-3] ^= 0xFF
        with self.assertRaises(CipherError):
            c.decrypt(bytes(blob))
        with self.assertRaises(CipherError):
            c.decrypt(b"not a blob")

    def test_undecryptable_fact_is_reported_not_guessed(self):
        self.put("taxation.pan", PAN)

        class Broken(PlainCipher):
            def decrypt(self, blob):
                raise CipherError("different windows user")

        s2 = BusinessStore.__new__(BusinessStore)
        s2.__dict__.update(self.s.__dict__)
        s2.cipher = Broken()
        s2._pepper_cache = self.s._pepper()
        with self.assertRaises(NotAvailable) as cm:
            s2.get_value("taxation.pan")
        self.assertEqual(cm.exception.status, "UNDECRYPTABLE")
        with self.assertRaises(StoreError):
            s2.reveal(self.s.status("taxation.pan").facts[0].fact_id)

    def test_fernet_fallback_round_trips_and_is_marked_weaker(self):
        c = FernetFileCipher(self.dir / "k.key")
        self.assertFalse(c.strong)
        self.assertEqual(c.decrypt(c.encrypt(b"x")), b"x")
        s = BusinessStore(self.dir / "f.db", c)
        s.put_candidate("taxation.pan", PAN, source_type="USER_ENTERED")
        self.assertNotIn(PAN.encode(), (self.dir / "f.db").read_bytes())
        self.assertEqual(s.get_value("taxation.pan").value, PAN)

    def test_equality_hashes_are_keyed_not_a_plain_hash_of_the_value(self):
        import hashlib

        self.put("taxation.pan", PAN)
        with contextlib.closing(sqlite3.connect(self.dir / "business.db")) as con:
            h = con.execute("SELECT value_hash FROM facts").fetchone()[0]
        self.assertNotEqual(h, hashlib.sha256(("pan:" + PAN).encode()).hexdigest())


class MaskTests(unittest.TestCase):
    def test_masks_reveal_little(self):
        self.assertEqual(mask_value("ABCDE1234F", "HIGH"), "AB••••••4F")
        self.assertEqual(set(mask_value("abc", "HIGH")), {"•"})
        self.assertEqual(mask_value("ops@example.test", "MEDIUM", "email"), "o•••@example.test")
        self.assertTrue(mask_value("9876543210", "MEDIUM", "phone").endswith("210"))
        self.assertNotIn("Example Street", mask_value(json.dumps({"line1": "123 Example Street", "pin": "560001"}), "MEDIUM", "address"))


class AuditTests(Base):
    def test_audit_is_append_only(self):
        self.s.audit("x", outcome="ok")
        with contextlib.closing(sqlite3.connect(self.dir / "business.db")) as con:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("UPDATE audit SET event='y'")
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM audit")

    def test_every_change_is_audited_without_values(self):
        r = self.put("taxation.pan", PAN)
        self.s.reveal(r.fact_id, actor="test")
        self.s.delete_fact(r.fact_id)
        ev = [(x["event"], x["actor"]) for x in self.s.audit_tail(20)]
        self.assertEqual([e for e, _ in ev], ["fact_added", "reveal", "fact_deleted"])
        self.assertNotIn(PAN, json.dumps(self.s.audit_tail(20)))

    def test_old_audit_rows_can_be_pruned_only_through_the_explicit_method(self):
        clock = [1000.0]
        s = BusinessStore(self.dir / "c.db", clock=lambda: clock[0])
        s.audit("old")
        clock[0] += 400 * 86400
        s.audit("new")
        self.assertEqual(s.prune_audit(365), 1)
        self.assertEqual([r["event"] for r in s.audit_tail(10)], ["new"])
        with contextlib.closing(sqlite3.connect(self.dir / "c.db")) as con:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM audit")


class HealthTests(Base):
    def test_integrity_and_backup(self):
        self.put("identity.legal_name", NAME)
        self.assertTrue(self.s.integrity_ok())
        b = self.s.backup()
        self.assertTrue(b.exists())
        self.assertEqual(BusinessStore(b).get_value("identity.legal_name").value, NAME)

    def test_a_corrupt_database_is_reported_not_trusted(self):
        (self.dir / "bad.db").write_bytes(b"this is not a database" * 100)
        with self.assertRaises(Exception):
            BusinessStore(self.dir / "bad.db")


class ValidatorTests(unittest.TestCase):
    def test_formats(self):
        self.assertTrue(v.check_pan("ABCDE1234F")[0])
        self.assertFalse(v.check_pan("ABCDE12345")[0])
        self.assertTrue(v.check_gstin(GOOD_GSTIN)[0])
        self.assertFalse(v.check_gstin(GOOD_GSTIN[:-1] + ("A" if GOOD_GSTIN[-1] != "A" else "B"))[0], "bad check character")
        self.assertFalse(v.check_gstin("TEST-GSTIN-123")[0])
        self.assertTrue(v.check_pin("560001")[0])
        self.assertFalse(v.check_pin("056001")[0])
        self.assertTrue(v.check_email("ops@example.test")[0])
        self.assertTrue(v.check_date("2024-05-01")[0])
        self.assertFalse(v.check_date("01/05/2024")[0])
        self.assertTrue(v.check_phone("+91 98765 43210")[0])

    def test_comparison_normalisation_does_not_change_stored_values(self):
        self.assertEqual(v.normalize_for_compare("pan", " abcde1234f "), "ABCDE1234F")
        self.assertEqual(v.normalize_for_compare("phone", "+91 98765-43210"), "9876543210")
        self.assertEqual(v.normalize_for_compare("text", "Example   Growth LLC"), "example growth llc")


class CatalogTests(unittest.TestCase):
    def test_key_parsing(self):
        self.assertEqual(parse_key("owners[2].name").instance, 2)
        self.assertEqual(parse_key("addresses.principal.pin").part, "pin")
        self.assertEqual(parse_key("addresses.additional[1].state").instance, 1)
        for bad in ("", "nope", "owners[x].name", "identity.legal_name[1]", "addresses.principal.nope", "other.UPPER", "x" * 200):
            with self.assertRaises(KeyError_, msg=bad):
                parse_key(bad)

    def test_label_matching_is_specific_and_deterministic(self):
        keys = lambda *t: [pk.display() for pk, _ in match_label(*t)]
        self.assertEqual(keys("Business Name"), ["identity.legal_name"])
        self.assertIn("addresses.principal.full", keys("Business Address"))
        self.assertEqual(keys("Address Line 1"), ["addresses.principal.line1"])
        self.assertEqual(keys("PIN Code"), ["addresses.principal.pin"])
        self.assertIn("taxation.gstin", keys("GSTIN"))
        self.assertIn("taxation.pan", keys("PAN Number"))
        self.assertEqual(keys("Newsletter signup"), [])
        self.assertEqual(keys("Search"), [])
        self.assertEqual(keys("", None), [])

    def test_ambiguous_labels_list_every_candidate_instead_of_guessing(self):
        got = [pk.display() for pk, _ in match_label("Name of business owner", None)]
        self.assertTrue(got)


if __name__ == "__main__":
    unittest.main()
