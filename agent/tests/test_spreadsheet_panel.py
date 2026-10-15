"""The side panel and spreadsheet import: it can PREVIEW a workbook (masked, nothing written) and list past imports; committing stays in the terminal."""
import base64
import json
import unittest

from octg_agent.business import spreadsheet as S
from octg_agent.business.spreadsheet.xlsx import SheetSpec, write_xlsx
from octg_agent.sensitive import SECRETS

from .test_business_ui import Panel
from .test_spreadsheet_import import CLIENT_A, CLIENT_B, CLIENT_HEAD, GSTIN, OWNER_ROWS, PAN, UDYAM, SERVICES


def workbook_bytes() -> bytes:
    return write_xlsx(None, [SheetSpec("My Business", OWNER_ROWS), SheetSpec("Clients", [CLIENT_HEAD, CLIENT_A, CLIENT_B]), SheetSpec("Client Services", SERVICES)])


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class PanelImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_panel_previews_a_workbook_masked_and_writes_nothing(self):
        async with Panel() as p:
            before = p.biz.store.counts()
            r = await p.ask("biz_import_preview", name="OCTG_Business_Data.xlsx", data_b64=b64(workbook_bytes()))
            self.assertTrue(r["ok"], r)
            pv = r["biz"]["preview"]
            self.assertEqual(pv["workbook"], "OCTG_Business_Data.xlsx")
            self.assertEqual(pv["detected"], {"business_profile": 1, "clients": 2, "services": 4, "locations": 0, "seo_targets": 0})
            self.assertEqual(pv["records"]["clients"]["new"], 2)
            self.assertEqual(pv["sensitive"]["count"], 5)
            blob = json.dumps(r)
            for v in (PAN, GSTIN, UDYAM, "Prefers morning calls", "+1 555 010 1000", "+91 98765 43210", "1 Alpha Street"):
                self.assertNotIn(v, blob, f"{v!r} reached the panel")
            self.assertIn("business import", r["biz"]["cli"])
            self.assertEqual(p.biz.store.counts(), before)
            self.assertEqual(p.biz.store.list_clients(), [])
            self.assertEqual(S.SpreadsheetImporter(p.biz.store).sessions(), [])
            self.assertEqual(len(SECRETS), 0, "the plan's protected values were released at once")
            events = [e["event"] for e in p.biz.store.audit_tail(20)]
            self.assertIn("import_previewed", events)
            self.assertNotIn("import_committed", events)

    async def test_the_panel_has_no_way_to_import_commit_decide_or_create(self):
        async with Panel() as p:
            for cmd in ("biz_import", "biz_import_commit", "biz_import_confirm", "biz_import_decide", "biz_import_verify", "biz_template", "biz_client_import"):
                self.assertEqual((await p.ask(cmd, data_b64=b64(workbook_bytes())))["code"], "UNKNOWN_COMMAND", cmd)
            self.assertEqual(p.biz.store.counts()["live_facts"], 0)

    async def test_bad_uploads_are_refused_cleanly(self):
        async with Panel() as p:
            cases = [dict(name="x.xlsx"), dict(name="x.xlsx", data_b64=""), dict(name="x.xlsx", data_b64="!!!not base64!!!"), dict(name="x.csv", data_b64=b64(b"a,b")),
                     dict(name="x.xlsx", data_b64="A" * (4 * 1024 * 1024 + 100))]
            for kw in cases:
                r = await p.ask("biz_import_preview", **kw)
                self.assertEqual((r["ok"], r["code"]), (False, "BAD_UPLOAD"), kw.get("name"))
            for data, code in ((b"not a workbook at all", "NOT_XLSX"), (b"PK\x03\x04broken", "MALFORMED"), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50, "ENCRYPTED")):
                r = await p.ask("biz_import_preview", name="x.xlsx", data_b64=b64(data))
                self.assertEqual((r["ok"], r["code"]), (False, code))
            self.assertEqual(p.biz.store.counts()["live_facts"], 0)

    async def test_the_panel_preview_shows_conflicts_duplicates_and_problems_without_values(self):
        async with Panel() as p:
            s = p.biz.store
            cid = s.create_client("Example Locksmith LLC", ref="CLIENT-001")["client_id"]
            s.put_candidate("contact.phone", "+1 555 777 7777", source_type="USER_ENTERED", scope=cid)
            r = await p.ask("biz_import_preview", name="w.xlsx", data_b64=b64(workbook_bytes()))
            pv = r["biz"]["preview"]
            self.assertEqual(len(pv["conflicts"]), 1)
            self.assertEqual(pv["conflicts"][0]["field"], "Business phone")
            self.assertNotIn("+1 555 777 7777", json.dumps(r), "medium values are masked in the panel")
            self.assertNotIn("+1 555 010 1000", json.dumps(r))
            self.assertTrue(pv["blocking"])

    async def test_past_imports_are_listed(self):
        async with Panel() as p:
            imp = S.SpreadsheetImporter(p.biz.store)
            pv = imp.analyze(workbook_bytes(), name="Book.xlsx")
            imp.commit(pv, assume_verified=False, confirm_sensitive=True)
            pv.close()
            r = await p.ask("biz_imports")
            self.assertTrue(r["ok"])
            row = r["biz"]["imports"][0]
            self.assertEqual((row["workbook"], row["status"], row["assume_verified"]), ("Book.xlsx", "committed", False))
            self.assertIn("template", r["biz"]["template_cli"])
            self.assertNotIn(PAN, json.dumps(r))

    async def test_feature_off_exposes_no_import_command(self):
        async with Panel(enabled=False) as p:
            for cmd in ("biz_imports", "biz_import_preview"):
                r = await p.ask(cmd, name="x.xlsx", data_b64=b64(workbook_bytes()))
                self.assertEqual((r["ok"], r["code"]), (False, "BIZ_DISABLED"), cmd)
            self.assertFalse((p.h.settings.data_dir / "business.db").exists())


if __name__ == "__main__":
    unittest.main()
