"""Side-panel Business Brain commands over the authenticated extension socket, the feature flag, and the architecture guards."""
import ast
import json
import tempfile
import unittest
from pathlib import Path

from octg_agent.brain.settings import BrainSettings
from octg_agent.reasoning.providers.mock import MockProvider

from .helpers import Harness, wait_until

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "agent" / "octg_agent"
NAME, PAN = "Example Growth LLC", "ABCDE1234F"


class Panel:
    """A side panel talking to the agent over the real WebSocket."""

    def __init__(self, enabled=True, hang=False):
        self.hang = hang
        self.brain = BrainSettings(business={"enabled": True} if enabled else {}, router={"policy": "FALLBACK", "order": ["local", "cloud"]})
        self.n = 0

    async def __aenter__(self):
        import asyncio

        slow = (lambda m, t, o: asyncio.sleep(30)) if self.hang else None  # a model that never answers: the task stays running
        self.h = Harness(providers={"local": MockProvider(name="local", local=True, handler=slow), "cloud": MockProvider(name="cloud", handler=slow)}, brain=self.brain)
        await self.h.__aenter__()
        from .fakebrowser import FakeBrowser, shop_pages

        self.fb = FakeBrowser(shop_pages(), "https://start.test/")  # a page for the agent to observe, so a running task keeps running instead of failing on "no browser"
        self.ext = await self.h.extension(handlers=self.fb.handlers())
        return self

    async def __aexit__(self, *a):
        await self.h.__aexit__(*a)

    async def ask(self, command, **kw):
        self.n += 1
        rid = f"b{self.n}"
        await self.ext.send({"type": "ui", "command": command, "request_id": rid, **kw})
        assert await wait_until(lambda: any(m.get("type") == "ui_result" and m.get("request_id") == rid for m in self.ext.others)), command
        return next(m for m in self.ext.others if m.get("type") == "ui_result" and m.get("request_id") == rid)

    @property
    def biz(self):
        return self.h.rt.business


class FeatureFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_by_default_and_the_panel_is_told_so(self):
        async with Panel(enabled=False) as p:
            self.assertIsNone(p.biz)
            r = await p.ask("biz_status")
            self.assertEqual((r["ok"], r["biz"]), (True, {"enabled": False}))
            for cmd in ("biz_profile", "biz_confirm", "biz_reveal", "biz_docs", "biz_audit"):
                r = await p.ask(cmd, fact_id="x")
                self.assertEqual((r["ok"], r["code"]), (False, "BIZ_DISABLED"), cmd)
            self.assertFalse((p.h.settings.data_dir / "business.db").exists())

    async def test_default_settings_have_the_flag_off(self):
        self.assertEqual(BrainSettings().business, {})
        from octg_agent.business.service import BusinessConfig

        self.assertFalse(BusinessConfig().enabled)
        self.assertFalse(BusinessConfig.from_dict({}).enabled)

    async def test_environment_switch(self):
        import os

        os.environ["OCTG_BUSINESS"] = "1"
        try:
            self.assertTrue(BrainSettings.load(tempfile.gettempdir()).business["enabled"])
        finally:
            os.environ.pop("OCTG_BUSINESS", None)


class PanelCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_protection_counts_folders_and_provider_locality(self):
        async with Panel() as p:
            p.biz.store.put_candidate("taxation.pan", PAN, source_type="USER_ENTERED")
            b = (await p.ask("biz_status"))["biz"]
            self.assertTrue(b["enabled"] and b["healthy"])
            self.assertEqual((b["cipher"], b["encrypted_facts"], b["live_facts"]), ("dpapi", 1, 1))
            self.assertEqual({x["name"]: x["local"] for x in b["providers"]}, {"local": True, "cloud": False})
            self.assertEqual(b["reveal_seconds"], 10)

    async def test_the_profile_view_is_masked_and_never_carries_a_value(self):
        async with Panel() as p:
            p.biz.store.put_candidate("taxation.pan", PAN, source_type="USER_ENTERED")
            p.biz.store.put_candidate("identity.legal_name", NAME, source_type="USER_ENTERED")
            r = await p.ask("biz_profile")
            blob = json.dumps(r)
            self.assertNotIn(PAN, blob)
            self.assertNotIn(NAME, blob)
            pan = next(f for f in r["biz"]["fields"] if f["key"] == "taxation.pan")
            self.assertEqual((pan["status"], pan["sensitivity"], pan["facts"][0]["source_type"]), ("VERIFIED", "HIGH", "USER_ENTERED"))
            self.assertIn("•", pan["facts"][0]["mask"])
            missing = next(f for f in r["biz"]["fields"] if f["key"] == "registration.cin")
            self.assertEqual(missing["status"], "MISSING")

    async def test_confirm_reject_and_resolve_from_the_panel(self):
        async with Panel() as p:
            s = p.biz.store
            a = s.put_candidate("taxation.pan", PAN, source_type="DOCUMENT", doc_id="d1")
            r = await p.ask("biz_confirm", fact_id=a.fact_id)
            self.assertEqual(r["biz"]["field"]["status"], "VERIFIED")
            b = s.put_candidate("taxation.pan", "ZZZZZ9999Z", source_type="DOCUMENT", doc_id="d2")
            self.assertEqual(s.status("taxation.pan").status, "CONFLICTING")
            r = await p.ask("biz_resolve", key="taxation.pan", fact_id=a.fact_id)
            self.assertEqual(r["biz"]["field"]["status"], "VERIFIED")
            c = s.put_candidate("taxation.pan", "QQQQQ1111Q", source_type="DOCUMENT", doc_id="d3")
            r = await p.ask("biz_reject", fact_id=c.fact_id)
            self.assertEqual(r["biz"]["field"]["status"], "VERIFIED")
            actors = {x["actor"] for x in s.audit_tail(50) if x["event"] in ("fact_confirmed", "conflict_resolved", "fact_rejected")}
            self.assertEqual(actors, {"panel"})

    async def test_reveal_returns_the_value_only_on_request_and_is_audited_without_it(self):
        async with Panel() as p:
            f = p.biz.store.put_candidate("taxation.pan", PAN, source_type="USER_ENTERED")
            r = await p.ask("biz_reveal", fact_id=f.fact_id)
            self.assertEqual((r["biz"]["value"], r["biz"]["hide_after_s"]), (PAN, 10))
            ev = [x for x in p.biz.store.audit_tail(10) if x["event"] == "reveal"]
            self.assertEqual(len(ev), 1)
            self.assertNotIn(PAN, json.dumps(ev))
            self.assertEqual(ev[0]["actor"], "panel")

    async def test_errors_are_reported_without_values_or_crashes(self):
        async with Panel() as p:
            r = await p.ask("biz_confirm", fact_id="fact_nope")
            self.assertFalse(r["ok"])
            self.assertEqual(r["code"], "NO_SUCH_FACT")
            r = await p.ask("biz_nonsense")
            self.assertEqual(r["code"], "UNKNOWN_COMMAND")

    async def test_documents_sources_and_audit_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with Panel() as p:
                d = Path(tmp) / "docs"
                d.mkdir()
                (d / "gst_certificate.txt").write_text("Certificate of Registration\n", encoding="utf-8")
                p.h.rt.folders.allow(str(d))
                p.biz.docs.enroll(str(d), "t")
                p.biz.docs.scan()
                p.biz.docs.index()
                docs = (await p.ask("biz_docs"))["biz"]["documents"]
                self.assertEqual(docs[0]["doc_type"], "gst_certificate")
                self.assertNotIn(str(d), json.dumps(docs))
                r = await p.ask("biz_doc_approve", doc_id=docs[0]["doc_id"])
                self.assertEqual(r["biz"]["document"]["state"], "APPROVED")
                r = await p.ask("biz_doc_exclude", doc_id=docs[0]["doc_id"])
                self.assertEqual(r["biz"]["document"]["state"], "EXCLUDED")
                src = (await p.ask("biz_sources"))["biz"]
                self.assertEqual(len(src["enrolled"]), 1)
                self.assertIn("business docs enroll", src["cli"])
                ev = (await p.ask("biz_audit", n=100))["biz"]["events"]
                self.assertIn("doc_approved", [e["event"] for e in ev])

    async def test_the_panel_cannot_widen_access_there_is_no_enroll_import_or_lower_command(self):
        async with Panel() as p:
            for cmd in ("biz_enroll", "biz_import", "biz_set", "biz_lower_sensitivity", "biz_disable", "biz_export", "biz_add_fact"):
                r = await p.ask(cmd)
                self.assertEqual(r["code"], "UNKNOWN_COMMAND", cmd)

    async def test_task_summaries_expose_business_metadata_only(self):
        async with Panel() as p:
            p.biz.store.put_candidate("identity.legal_name", NAME, source_type="USER_ENTERED")
            await p.ext.send({"type": "ui", "command": "start_task", "instruction": "Fill in my business name.", "request_id": "t1", "business": True})
            await wait_until(lambda: any(m.get("type") == "ui_result" and m.get("request_id") == "t1" for m in p.ext.others))
            res = next(m for m in p.ext.others if m.get("request_id") == "t1")
            self.assertTrue(res["task"]["business"]["active"])
            self.assertEqual(res["task"]["business"]["fills"], 0)
            self.assertNotIn(NAME, json.dumps(p.ext.others))
            p.h.rt.tasks.stop()


class ClientPanelCommandTests(unittest.IsolatedAsyncioTestCase):
    async def seed(self, p):
        s = p.biz.store
        a = s.create_client("Example Locksmith LLC")["client_id"]
        b = s.create_client("Example Roofing LLC")["client_id"]
        for cid, phone, gst in ((a, "+1 555 010 1000", "TEST-A-GSTIN"), (b, "+1 555 020 2000", "TEST-B-GSTIN")):
            s.put_candidate("contact.phone", phone, source_type="USER_ENTERED", scope=cid)
            s.put_candidate("taxation.gstin", gst, source_type="USER_ENTERED", scope=cid)
        s.put_candidate("identity.legal_name", NAME, source_type="USER_ENTERED")
        s.put_candidate("contact.phone", "+91 98765 43210", source_type="USER_ENTERED")
        return a, b

    async def test_the_client_list_has_counts_and_no_values(self):
        async with Panel() as p:
            a, b = await self.seed(p)
            r = await p.ask("biz_clients")
            names = [c["name"] for c in r["biz"]["clients"]]
            self.assertEqual(names, ["Example Locksmith LLC", "Example Roofing LLC"])
            self.assertEqual({c["facts"] for c in r["biz"]["clients"]}, {2})
            blob = json.dumps(r)
            for v in ("TEST-A-GSTIN", "TEST-B-GSTIN", "555 010", "555 020", NAME):
                self.assertNotIn(v, blob)

    async def test_a_client_view_contains_only_that_client_and_masks_everything(self):
        async with Panel() as p:
            a, b = await self.seed(p)
            ra = await p.ask("biz_client", client_id=a)
            blob = json.dumps(ra)
            self.assertEqual({f["scope"] for f in ra["biz"]["fields"]}, {a})
            self.assertEqual(ra["biz"]["client"]["name"], "Example Locksmith LLC")
            for v in ("TEST-A-GSTIN", "TEST-B-GSTIN", "Roofing", "98765", NAME, "555 020"):
                self.assertNotIn(v, blob)
            phone = next(f for f in ra["biz"]["fields"] if f["key"] == "contact.phone")
            self.assertEqual(phone["status"], "VERIFIED")
            self.assertIn("•", phone["facts"][0]["mask"])

    async def test_the_owners_views_never_include_client_data_and_the_reverse(self):
        async with Panel() as p:
            a, b = await self.seed(p)
            owner = json.dumps([await p.ask("biz_profile"), await p.ask("biz_audit", n=200), await p.ask("biz_docs"), await p.ask("biz_sources")])
            for v in ("TEST-A-GSTIN", "TEST-B-GSTIN", "Locksmith", "Roofing", a, b):
                self.assertNotIn(v, owner)
            ca = json.dumps(await p.ask("biz_client_audit", client_id=a))
            self.assertIn(a, ca)
            self.assertNotIn(b, ca)
            self.assertNotIn("legal_name", ca)

    async def test_client_documents_sources_seo_and_audit_views_are_scoped(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            async with Panel() as p:
                a, b = await self.seed(p)
                d = Path(tmp) / "a_docs"
                d.mkdir()
                (d / "gst_certificate.txt").write_text("Certificate of Registration\n", encoding="utf-8")
                p.h.rt.folders.allow(str(d))
                p.biz.docs.enroll(str(d), "a", scope=a)
                p.biz.docs.scan()
                p.biz.docs.index()
                da = (await p.ask("biz_client_docs", client_id=a))["biz"]
                db = (await p.ask("biz_client_docs", client_id=b))["biz"]
                self.assertEqual(len(da["documents"]), 1)
                self.assertEqual(db["documents"], [])
                self.assertEqual((await p.ask("biz_docs"))["biz"]["documents"], [], "the owner's own document list does not include a client's")
                src = (await p.ask("biz_client_sources", client_id=a))["biz"]
                self.assertEqual(len(src["enrolled"]), 1)
                self.assertIn(f"--client {a}", src["cli"])
                self.assertEqual((await p.ask("biz_client_sources", client_id=b))["biz"]["enrolled"], [])
                seo = (await p.ask("biz_client_seo", client_id=a))["biz"]
                self.assertEqual({f["group"] for f in seo["fields"]} - {"seo", "local_seo", "service_area", "services", "marketing"}, set())
                self.assertEqual((seo["sessions"], seo["observations"]), (0, 0))
                self.assertEqual((await p.ask("biz_client", client_id="cli_nope"))["code"], "NO_SUCH_CLIENT")

    async def test_resolving_a_client_conflict_from_the_panel_uses_that_clients_scope(self):
        async with Panel() as p:
            a, b = await self.seed(p)
            s = p.biz.store
            new = s.put_candidate("contact.phone", "+1 555 999 9999", source_type="DOCUMENT", doc_id="d", scope=a)
            r = await p.ask("biz_resolve", key="contact.phone", fact_id=new.fact_id, client_id=a)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["biz"]["field"]["scope"], a)
            wrong = s.status("contact.phone", b).facts[0].fact_id
            bad = await p.ask("biz_resolve", key="contact.phone", fact_id=wrong, client_id=a)
            self.assertEqual((bad["ok"], bad["code"]), (False, "WRONG_FACT"))
            owner_try = await p.ask("biz_resolve", key="contact.phone", fact_id=new.fact_id)
            self.assertFalse(owner_try["ok"])

    async def test_the_panel_cannot_create_edit_import_or_enrol_clients(self):
        async with Panel() as p:
            await self.seed(p)
            for cmd in ("biz_client_create", "biz_client_set", "biz_client_rename", "biz_client_archive", "biz_client_import", "biz_client_enroll", "biz_client_delete",
                        "biz_research_start", "biz_research_add"):
                self.assertEqual((await p.ask(cmd, client_id="x"))["code"], "UNKNOWN_COMMAND", cmd)
            self.assertEqual(len(p.biz.store.list_clients()), 2)

    async def test_feature_off_exposes_no_client_command(self):
        async with Panel(enabled=False) as p:
            for cmd in ("biz_clients", "biz_client", "biz_client_docs", "biz_client_seo"):
                r = await p.ask(cmd, client_id="x")
                self.assertEqual((r["ok"], r["code"]), (False, "BIZ_DISABLED"), cmd)

    async def test_selecting_a_client_for_a_running_task_goes_through_the_task_channel(self):
        async with Panel(hang=True) as p:
            a, b = await self.seed(p)
            await p.ext.send({"type": "ui", "command": "start_task", "instruction": "Fill in the client's business information.", "request_id": "t1"})
            await wait_until(lambda: any(m.get("request_id") == "t1" for m in p.ext.others))
            r = await p.ask("select_client", client_id=b)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["task"]["business"]["client"]["id"], b)
            p.h.rt.tasks.stop()
            await wait_until(lambda: p.h.rt.tasks.active() is None)


class ArchitectureGuardTests(unittest.TestCase):
    @staticmethod
    def imports(py: Path):
        out = []
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                out += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                out += [("." * node.level) + (node.module or "")] + [("." * node.level) + (node.module or "") + "." + a.name for a in node.names]
        return out

    def test_the_browser_engine_bridge_files_and_models_never_import_the_business_brain(self):
        for sub in ("engine", "bridge", "files", "reasoning"):
            for py in (PKG / sub).rglob("*.py"):
                for m in self.imports(py):
                    self.assertNotIn("business", m.split("."), f"{py.relative_to(PKG)} imports the Business Brain")

    def test_the_business_brain_never_imports_the_engine_bridge_models_or_the_loop(self):
        allowed_roots = {"files", "sensitive", "business"}
        for py in (PKG / "business").rglob("*.py"):
            depth = len(py.relative_to(PKG / "business").parts)  # 1 = business/x.py, 2 = business/extract/x.py
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level:
                    # the package root reached by this relative import: level == depth means octg_agent.<module>
                    if node.level > depth:
                        top = (node.module or "").split(".")[0]
                        self.assertIn(top, allowed_roots, f"{py.relative_to(PKG)} imports from {node.module}")
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for n in names:
                        for banned in ("octg_agent.engine", "octg_agent.bridge", "octg_agent.reasoning", "octg_agent.brain", "aiohttp"):
                            self.assertFalse(n.startswith(banned), f"{py.name} imports {n}")

    def test_the_task_loop_reaches_the_business_brain_only_through_the_reader(self):
        for py in (PKG / "brain").glob("*.py"):
            for m in self.imports(py):
                parts = m.split(".")
                if "business" in parts:
                    self.assertIn("resolver", parts, f"{py.name} imports {m}: the loop may use only the read-only resolver module")
                    for forbidden in ("store", "service", "documents", "cipher"):
                        self.assertNotIn(forbidden, parts, f"{py.name} imports {m}")

    def test_the_action_registry_has_no_business_actions_and_is_unchanged_from_phase_2(self):
        import hashlib

        from octg_agent.engine.registry import ACTIONS, tool_schema

        self.assertEqual(len(ACTIONS), 35)
        self.assertFalse([n for n in ACTIONS if "biz" in n or "business" in n or "brain" in n])
        h = hashlib.sha256(json.dumps(tool_schema(), sort_keys=True).encode()).hexdigest()
        self.assertEqual(h, "6a6c5db5fc40ba09f1c3378c3a31d2e639670af22d5f12b27762c2835f030721", "the browser action registry must stay exactly as at the end of Phase 2")

    def test_no_http_route_reads_or_writes_business_data(self):
        from octg_agent.config import Secrets, Settings
        from octg_agent.app import create_app

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            app = create_app(Settings(data_dir=Path(d)), Secrets("p", "a"), {}, BrainSettings(business={"enabled": True}))
            paths = [r.canonical for r in app.router.resources()]
        self.assertFalse([x for x in paths if "business" in x.lower() or "biz" in x.lower() or "profile" in x.lower()], paths)

    def test_the_extension_manifest_is_unchanged(self):
        m = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(set(m["permissions"]), {"tabs", "scripting", "webNavigation", "sidePanel", "storage", "alarms", "downloads"})

    def test_no_provider_adapter_or_routing_policy_was_modified_for_phase_3(self):
        # the only router change is the generic local_only boundary; adapters mention nothing of the Business Brain
        for py in (PKG / "reasoning" / "providers").glob("*.py"):
            self.assertNotIn("business", py.read_text(encoding="utf-8").lower(), py.name)


if __name__ == "__main__":
    unittest.main()
