"""Authorized Documents: explicit enrolment, no automatic ingestion, containment, hashing, sandboxed deterministic extraction, use-time revalidation.
Synthetic documents only."""
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from octg_agent.business import extract as extract_mod
from octg_agent.business.documents import DOC_TYPES, DocNotAvailable, DocumentRegistry, classify_name
from octg_agent.business.extract import extract_text
from octg_agent.business.extract.fields import extract_candidates
from octg_agent.business.store import BusinessStore, StoreError
from octg_agent.business.validators import gstin_with_checksum
from octg_agent.files.authorization import AuthorizedFolders

GSTIN = gstin_with_checksum("27ABCDE1234F1Z")
PAN = "ABCDE1234F"
CERT = f"Certificate of Registration\nLegal Name of Business: Example Growth LLC\nTrade Name: Example Growth\nGSTIN: {GSTIN}\nPAN: {PAN}\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.docs_dir = self.root / "bizdocs"
        self.docs_dir.mkdir()
        self.other = self.root / "elsewhere"
        self.other.mkdir()
        self.folders = AuthorizedFolders(self.data / "folders.json")
        self.store = BusinessStore(self.data / "business.db")
        self.reg = DocumentRegistry(self.store, self.folders, extract_timeout_s=20)

    def tearDown(self):
        self.tmp.cleanup()

    def authorize_and_enroll(self, d=None):
        d = d or self.docs_dir
        self.folders.allow(str(d))
        return self.reg.enroll(str(d), "test")

    def write(self, name, text=CERT, folder=None):
        p = (folder or self.docs_dir) / name
        p.write_text(text, encoding="utf-8")
        return p


class EnrolmentTests(Base):
    def test_a_folder_must_be_authorized_before_it_can_be_enrolled(self):
        with self.assertRaises(StoreError) as cm:
            self.reg.enroll(str(self.docs_dir))
        self.assertEqual(cm.exception.code, "FILE_NOT_AUTHORIZED")
        self.assertIn("fs allow", cm.exception.message)

    def test_enrolment_is_idempotent_and_reads_nothing(self):
        self.write("gst_certificate.txt")
        a = self.authorize_and_enroll()
        b = self.reg.enroll(str(self.docs_dir))
        self.assertEqual(a["folder_id"], b["folder_id"])
        self.assertEqual(self.reg.list(), [], "enrolling does not scan: nothing is registered until `scan` is run")

    def test_no_automatic_ingestion_anywhere(self):
        self.write("gst_certificate.txt", folder=self.other)  # authorized for uploads but NOT enrolled
        self.folders.allow(str(self.other))
        self.authorize_and_enroll()
        self.reg.scan()
        self.assertEqual(self.reg.list(), [])

    def test_revoking_enrolment_deactivates_its_documents(self):
        self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        did = self.reg.list()[0]["doc_id"]
        self.reg.approve(did)
        self.assertTrue(self.reg.revoke(str(self.docs_dir)))
        self.assertEqual(self.reg.list()[0]["state"], "MISSING")
        self.assertEqual(self.reg.approved_manifest(), [])
        with self.assertRaises(DocNotAvailable):
            self.reg.resolve_for_upload("gst_certificate")


class ScanTests(Base):
    def test_scan_registers_id_path_hash_type_sensitivity_and_source(self):
        p = self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        rep = self.reg.scan()
        self.assertEqual(rep.registered, 1)
        d = self.reg.list()[0]
        self.assertTrue(d["doc_id"].startswith("doc_"))
        self.assertEqual((d["doc_type"], d["sensitivity"], d["state"], d["verification"]), ("gst_certificate", "HIGH", "DISCOVERED", "UNVERIFIED"))
        self.assertIn("enrolled folder test", d["source"])
        import hashlib

        self.assertEqual(d["sha256"], hashlib.sha256(p.read_bytes()).hexdigest()[:12])

    def test_rescan_is_stable_and_detects_changes_and_removals(self):
        p = self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        did = self.reg.list()[0]["doc_id"]
        self.reg.approve(did)
        self.assertEqual(self.reg.scan().unchanged, 1)
        p.write_text(CERT + "edited\n", encoding="utf-8")
        rep = self.reg.scan()
        self.assertEqual(rep.changed, 1)
        d = self.reg.list()[0]
        self.assertEqual((d["state"], d["verification"]), ("DISCOVERED", "UNVERIFIED"), "a changed file must be indexed and approved again")
        self.assertEqual(self.store.status("taxation.gstin").status, "MISSING", "what the old version said is no longer evidence")
        p.unlink()
        self.assertEqual(self.reg.scan().missing, 1)
        self.assertEqual(self.reg.list()[0]["state"], "MISSING")

    def test_only_allowed_extensions_and_safe_names_are_registered(self):
        self.write("notes.exe", "x")
        self.write("script.ps1", "x")
        self.write(".hidden.txt", "x")
        self.write("~$lock.txt", "x")
        self.write("evil\u202etxt.exe.txt", "x")
        self.write("ok.txt", "plain")
        self.authorize_and_enroll()
        rep = self.reg.scan()
        self.assertEqual([d["name"] for d in self.reg.list()], ["ok.txt"])
        self.assertTrue(any("unsafe name" in s for s in rep.skipped))

    def test_banking_identity_and_credential_files_are_blocked(self):
        for n in ("bank_statement.txt", "aadhaar_card.txt", "passwords.txt", "api_key_backup.txt", "cancelled_cheque.txt"):
            self.write(n, "x")
        self.authorize_and_enroll()
        self.reg.scan()
        self.assertEqual({d["state"] for d in self.reg.list()}, {"BLOCKED"})
        self.reg.index()
        for d in self.reg.list():
            with self.assertRaises(StoreError):
                self.reg.approve(d["doc_id"])
        self.assertEqual(self.store.list_fields(include_missing=False), [])

    def test_content_that_does_not_match_the_extension_is_blocked(self):
        (self.docs_dir / "scan.pdf").write_bytes(b"MZ\x90\x00 this is not a pdf")
        self.authorize_and_enroll()
        self.reg.scan()
        d = self.reg.list()[0]
        self.assertEqual(d["state"], "BLOCKED")
        self.assertIn("do not match", d["note"])

    def test_oversized_files_are_skipped(self):
        small = DocumentRegistry(self.store, self.folders, max_bytes=100)
        self.write("big.txt", "x" * 1000)
        self.authorize_and_enroll()
        rep = small.scan()
        self.assertEqual(small.list(), [])
        self.assertTrue(rep.skipped)

    def test_a_junction_or_symlink_leading_outside_is_not_followed(self):
        secret = self.write("outside_gst_certificate.txt", folder=self.other)
        self.write("inside.txt", "ok")
        self.authorize_and_enroll()
        link = self.docs_dir / "linked"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.other)], capture_output=True).returncode == 0
        if not made:
            try:
                os.symlink(self.other, link, target_is_directory=True)
                made = True
            except OSError:
                pass
        if not made:
            self.skipTest("cannot create a junction or symlink here")
        try:
            os.symlink(secret, self.docs_dir / "filelink.txt")
        except OSError:
            pass
        rep = self.reg.scan()
        names = [d["name"] for d in self.reg.list()]
        self.assertEqual(names, ["inside.txt"], "nothing reachable only through a link that leaves the folder may be registered")
        self.assertTrue(rep.skipped)

    def test_the_documents_listing_never_exposes_a_full_path(self):
        self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        self.reg.scan()
        import json

        self.assertNotIn(str(self.docs_dir), json.dumps(self.reg.list()))


class IndexingTests(Base):
    def test_extraction_produces_unverified_candidates_with_the_document_as_source(self):
        self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        self.reg.scan()
        out = self.reg.index()
        self.assertEqual(out["indexed"], 1)
        self.assertGreaterEqual(out["candidates"], 4)
        for key in ("taxation.gstin", "taxation.pan", "identity.legal_name", "identity.trade_name"):
            st = self.store.status(key)
            self.assertEqual(st.status, "UNVERIFIED", key)
            self.assertEqual(st.facts[0].source_type, "DOCUMENT")
            self.assertTrue(st.facts[0].doc_id.startswith("doc_"))
        d = self.reg.list()[0]
        self.assertEqual((d["state"], d["fields_found"]), ("INDEXED", out["candidates"]))
        self.assertEqual(d["doc_type"], "gst_certificate")

    def test_only_format_valid_identifiers_become_candidates(self):
        bad = GSTIN[:-1] + ("A" if GSTIN[-1] != "A" else "B")
        self.write("gst_certificate.txt", f"GSTIN: {bad}\nPAN: NOTAPAN123\n")  # wrong check character, not a PAN
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        self.assertEqual(self.store.status("taxation.gstin").status, "MISSING")
        self.assertEqual(self.store.status("taxation.pan").status, "MISSING")

    def test_two_documents_that_disagree_create_a_conflict_not_a_choice(self):
        self.write("gst_certificate.txt", f"GSTIN: {GSTIN}\n")
        self.write("old_gst_certificate.txt", f"GSTIN: {gstin_with_checksum('29ZYXWV9876Q1Z')}\n")
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        self.assertEqual(self.store.status("taxation.gstin").status, "CONFLICTING")

    def test_indexing_never_creates_a_verified_fact(self):
        self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        self.assertTrue(all(f.status != "VERIFIED" for f in self.store.list_fields(include_missing=False)))

    def test_docx_and_xlsx_text_is_extracted_by_the_sandbox(self):
        docx = self.docs_dir / "registration.docx"
        with zipfile.ZipFile(docx, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>GSTIN: ' + GSTIN + '</w:t></w:r></w:p></w:body></w:document>')
        xlsx = self.docs_dir / "details.xlsx"
        with zipfile.ZipFile(xlsx, "w") as z:
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>PAN</t></si><si><t>' + PAN + '</t></si></sst>')
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row></sheetData></worksheet>')
        r1, r2 = extract_text(docx, ".docx"), extract_text(xlsx, ".xlsx")
        self.assertTrue(r1.ok and GSTIN in r1.text, r1.note)
        self.assertTrue(r2.ok and PAN in r2.text, r2.note)

    def test_json_and_csv_are_flattened(self):
        j = self.docs_dir / "d.json"
        j.write_text('{"a": {"b": "' + PAN + '"}}', encoding="utf-8")
        self.assertIn(PAN, extract_text(j, ".json").text)

    def test_pdf_without_the_optional_library_is_registered_but_not_read(self):
        (self.docs_dir / "gst_certificate.pdf").write_bytes(b"%PDF-1.4\n%synthetic\n")
        self.authorize_and_enroll()
        self.reg.scan()
        r = extract_text(self.docs_dir / "gst_certificate.pdf", ".pdf")
        self.assertFalse(r.ok)
        self.assertTrue("pypdf" in r.note or "could not read" in r.note)
        self.reg.index()
        self.assertEqual(self.reg.list()[0]["state"], "INDEXED")

    def test_images_are_registered_but_their_content_is_not_read(self):
        (self.docs_dir / "pan_card.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        d = self.reg.list()[0]
        self.assertEqual((d["doc_type"], d["fields_found"]), ("pan_card", 0))
        self.assertIn("OCR", d["note"])


class HostileInputTests(Base):
    def test_a_zip_bomb_is_refused(self):
        bomb = self.docs_dir / "bomb.docx"
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"0" * (60 * 1024 * 1024))
        r = extract_text(bomb, ".docx")
        self.assertFalse(r.ok)

    def test_xml_with_a_doctype_or_entities_is_refused(self):
        evil = self.docs_dir / "evil.docx"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&lol;</w:t></w:r></w:p></w:body></w:document>')
        r = extract_text(evil, ".docx")
        self.assertFalse(r.ok)

    def test_corrupt_files_cost_only_a_failed_extraction(self):
        bad = self.docs_dir / "bad.docx"
        bad.write_bytes(b"PK\x03\x04 truncated nonsense")
        self.assertFalse(extract_text(bad, ".docx").ok)

    def test_a_hung_extractor_is_killed_at_the_timeout(self):
        sleeper = self.root / "sleeper.py"
        sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        with mock.patch.object(extract_mod, "WORKER", sleeper):
            r = extract_text(self.docs_dir / "x.txt", ".txt", timeout_s=1.0)
        self.assertFalse(r.ok)
        self.assertIn("timed out", r.note)

    def test_the_extractor_gets_no_secrets_from_the_environment(self):
        probe = self.root / "probe.py"
        probe.write_text("import os, json, sys\nsys.stdout.write(json.dumps({'ok': True, 'text': ','.join(sorted(k for k in os.environ if 'KEY' in k.upper() or 'TOKEN' in k.upper())), 'extractor': 'p'}))\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "synthetic", "MY_TOKEN": "synthetic"}), mock.patch.object(extract_mod, "WORKER", probe):
            r = extract_text(self.docs_dir / "x.txt", ".txt")
        self.assertEqual(r.text, "")

    def test_output_flood_is_refused(self):
        flood = self.root / "flood.py"
        flood.write_text("import sys\nsys.stdout.write('x' * 5_000_000)\n", encoding="utf-8")
        with mock.patch.object(extract_mod, "WORKER", flood):
            r = extract_text(self.docs_dir / "x.txt", ".txt")
        self.assertFalse(r.ok)


class FieldExtractionTests(unittest.TestCase):
    def test_labelled_names_and_identifiers(self):
        got = dict((k, v) for k, v in extract_candidates(CERT))
        self.assertEqual(got["taxation.gstin"], GSTIN)
        self.assertEqual(got["taxation.pan"], PAN)
        self.assertEqual(got["identity.legal_name"], "Example Growth LLC")

    def test_a_pan_inside_a_gstin_is_not_reported_separately(self):
        got = extract_candidates(f"GSTIN {GSTIN}")
        self.assertEqual([k for k, _ in got], ["taxation.gstin"])

    def test_no_text_no_candidates(self):
        self.assertEqual(extract_candidates("nothing useful here"), [])


class UseTimeTests(Base):
    def ready(self, name="gst_certificate.txt"):
        p = self.write(name)
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        did = self.reg.list()[0]["doc_id"]
        self.reg.approve(did)
        return p, did

    def test_an_approved_document_resolves_after_every_check(self):
        p, did = self.ready()
        path, info = self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(Path(path), p.resolve())
        self.assertEqual((info["doc_id"], info["sensitivity"]), (did, "HIGH"))
        path2, _ = self.reg.resolve_for_upload(did)
        self.assertEqual(path, path2)

    def test_an_unapproved_document_cannot_be_used(self):
        self.write("gst_certificate.txt")
        self.authorize_and_enroll()
        self.reg.scan()
        self.reg.index()
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(cm.exception.status, "MISSING")
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload(self.reg.list()[0]["doc_id"])
        self.assertEqual(cm.exception.status, "UNAPPROVED")

    def test_a_changed_file_is_refused_at_use_time(self):
        p, _ = self.ready()
        p.write_text("tampered", encoding="utf-8")
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(cm.exception.status, "CHANGED")

    def test_a_deleted_file_is_refused(self):
        p, _ = self.ready()
        p.unlink()
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(cm.exception.status, "MISSING")

    def test_revoked_authorization_is_refused(self):
        p, _ = self.ready()
        self.folders.revoke(str(self.docs_dir))
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(cm.exception.status, "UNAUTHORIZED")

    def test_two_approved_documents_of_one_kind_are_ambiguous_never_guessed(self):
        self.ready()
        self.write("gst_certificate_2024.txt", CERT + "renewal\n")
        self.reg.scan()
        self.reg.index()
        new = next(d for d in self.reg.list() if d["state"] == "INDEXED")
        self.reg.approve(new["doc_id"])
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(cm.exception.status, "AMBIGUOUS")

    def test_a_tampered_registry_row_pointing_outside_the_folder_is_refused(self):
        import hashlib

        p, did = self.ready()
        outside = self.write("evil.txt", "evil", folder=self.other)
        self.folders.allow(str(self.other))  # even an authorized folder is not the enrolled one
        with self.store.conn() as c:
            c.execute("UPDATE documents SET rel_path=?, sha256=? WHERE doc_id=?", (os.path.relpath(outside, self.docs_dir), hashlib.sha256(b"evil").hexdigest(), did))
        with self.assertRaises(DocNotAvailable) as cm:
            self.reg.resolve_for_upload("gst_certificate")
        self.assertEqual(cm.exception.status, "UNAUTHORIZED")

    def test_a_file_replaced_by_a_link_leaving_the_folder_is_refused(self):
        p, did = self.ready()
        outside = self.write("evil.txt", CERT, folder=self.other)
        p.unlink()
        try:
            os.symlink(outside, p)
        except OSError:
            self.skipTest("symlinks need privileges here")
        with self.assertRaises(DocNotAvailable):
            self.reg.resolve_for_upload("gst_certificate")

    def test_the_model_facing_manifest_has_labels_and_counts_but_no_names_or_paths(self):
        self.ready()
        import json

        m = self.reg.approved_manifest()
        self.assertEqual(m, [{"token": "gst_certificate", "label": "GST registration certificate", "count": 1}])
        blob = json.dumps(m)
        self.assertNotIn("gst_certificate.txt", blob)
        self.assertNotIn(str(self.docs_dir), blob)

    def test_an_arbitrary_path_is_not_a_token(self):
        self.ready()
        for bad in (str(self.other / "evil.txt"), "../elsewhere/evil.txt", "C:\\Windows\\system.ini"):
            with self.assertRaises(DocNotAvailable):
                self.reg.resolve_for_upload(bad)


class ClassificationTests(unittest.TestCase):
    def test_name_classification(self):
        for name, t in [("GST_Certificate.pdf", "gst_certificate"), ("pan.png", "pan_card"), ("certificate_of_incorporation.pdf", "incorporation_certificate"),
                        ("electricity-bill.pdf", "address_proof"), ("invoice-001.pdf", "invoice"), ("random.pdf", "other"), ("company_profile.docx", "other")]:
            self.assertEqual(classify_name(name), t, name)
        self.assertTrue(all(v[3] in ("MEDIUM", "HIGH") for v in DOC_TYPES.values()))
        self.assertEqual(DOC_TYPES["other"][3], "HIGH", "unknown documents default to HIGH")


if __name__ == "__main__":
    unittest.main()
