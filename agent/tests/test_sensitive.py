"""General sensitive-value protection of operational logs, task activity, history and error text (not specific to the Business Brain).

Synthetic values only. PAN/GSTIN-shaped strings here are made up.
"""
import json
import sqlite3
import unittest
from pathlib import Path

from octg_agent.brain.task import TaskState, brief_args
from octg_agent.business.validators import gstin_with_checksum
from octg_agent.reasoning.provider import ProviderError
from octg_agent.reasoning.providers.mock import MockProvider
from octg_agent.sensitive import SECRETS, element_category, field_category, redact_call_params, scrub_text, value_category

from .fakebrowser import FakeBrowser, el
from .helpers import Harness
from .test_brain_loop import Env, finish

T = MockProvider.call
PAN = "ABCDE1234F"
GSTIN = gstin_with_checksum("27ABCDE1234F1Z")
CARD = "4111 1111 1111 1111"
ACCOUNT = "123456789012"
ODD = "TEST-GSTIN-123"  # not shaped like anything: only the registry or a sensitive-labelled field protects it


def log_text(h) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (h.settings.logs_dir).glob("*.jsonl"))


class ClassificationTests(unittest.TestCase):
    def test_sensitive_fields_are_recognised_from_their_own_labels(self):
        for label, cat in [("Password", "password"), ("Enter OTP", "otp"), ("One-time code", "otp"), ("Card number", "card"), ("CVV", "card"), ("PAN Number", "pan"),
                           ("GSTIN", "gstin"), ("GST registration number", "gstin"), ("Udyam registration number", "gstin"), ("Bank account number", "bank"), ("IFSC code", "bank"), ("Aadhaar number", "identity"),
                           ("API key", "secret")]:
            self.assertEqual(field_category(label), cat, label)

    def test_ordinary_fields_are_not_sensitive(self):
        for label in ("Business Name", "Search", "Email", "Message", "Pincode", "PIN code", "Panel colour", "Company span"):
            self.assertIsNone(field_category(label), label)

    def test_element_flags_and_types_count(self):
        self.assertEqual(element_category({"name": "x", "type": "password"}), "password")
        self.assertEqual(element_category({"name": "x", "sensitive": True}), "secret")
        self.assertEqual(element_category({"name": "Username", "autocomplete": "one-time-code"}), "otp")
        self.assertIsNone(element_category({"name": "Business Name"}))
        self.assertIsNone(element_category(None))

    def test_value_shapes(self):
        self.assertEqual(value_category(PAN), "pan")
        self.assertEqual(value_category(GSTIN), "gstin")
        self.assertEqual(value_category("HDFC0001234"), "bank")
        self.assertEqual(value_category("UDYAM-XX-00-0000000"), "gstin")
        self.assertEqual(value_category(CARD), "card")
        self.assertEqual(value_category("123456789012"), "identity")
        for ok in ("Example Growth LLC", "hello", "123 Example Street", "2024-05-01", "+91 98765 43210"):
            self.assertIsNone(value_category(ok), ok)


class ScrubTests(unittest.TestCase):
    def tearDown(self):
        SECRETS.clear()

    def test_identifier_shapes_are_masked_inside_longer_text(self):
        t = scrub_text(f"typed {PAN} and {GSTIN} and card {CARD} into the form")
        for v in (PAN, GSTIN, CARD):
            self.assertNotIn(v, t)
        self.assertIn("[redacted:pan]", t)
        self.assertIn("[redacted:gstin]", t)
        self.assertIn("[redacted:card]", t)

    def test_ordinary_numbers_and_words_are_untouched(self):
        s = "Opened https://shop.test/ in 1234 ms, 12 elements, order 4111 1111 1111 1112 failed luhn"
        self.assertEqual(scrub_text(s), s)

    def test_registered_exact_values_are_redacted_whatever_their_shape(self):
        SECRETS.register(ODD, "task_1")
        self.assertEqual(scrub_text(f"value was {ODD}!"), "value was [redacted]!")
        SECRETS.clear("task_1")
        self.assertIn(ODD, scrub_text(f"value was {ODD}!"))

    def test_whitespace_variants_of_a_registered_value_are_covered(self):
        SECRETS.register("Example  Growth LLC", "t")
        self.assertNotIn("Growth", scrub_text("Name: Example Growth LLC"))

    def test_redact_call_params(self):
        out, hit = redact_call_params({"text": "hello", "element_id": "el_1"}, category=None, forced=False)
        self.assertEqual((out["text"], hit), ("hello", False))
        out, hit = redact_call_params({"text": "hello"}, category="gstin", forced=False)
        self.assertTrue(hit)
        self.assertNotIn("hello", json.dumps(out))
        out, hit = redact_call_params({"text": PAN}, category=None, forced=False)
        self.assertTrue(hit)
        out, hit = redact_call_params({"text": "plain", "values": ["a", "b"]}, category=None, forced=True)
        self.assertNotIn("plain", json.dumps(out))
        self.assertNotIn('"a"', json.dumps(out))


class ActionLogTests(unittest.IsolatedAsyncioTestCase):
    async def setUp_env(self, pages):
        self.fb = FakeBrowser(pages, "https://form.test/")
        self.h = Harness()
        await self.h.__aenter__()
        await self.h.extension(handlers=self.fb.handlers())
        r = await self.h.call("inspect_page")
        self.snap = r["result"]["snapshot_id"]
        self.ids = {e["name"]: e["id"] for e in r["result"]["elements"]}

    async def asyncTearDown(self):
        await self.h.__aexit__(None, None, None)
        SECRETS.clear()

    def pages(self):
        return {"https://form.test/": {"title": "Form", "text": "", "elements": [
            el("textbox", "Password", type="password", sensitive=True), el("textbox", "Enter OTP"), el("textbox", "Card number"), el("textbox", "PAN Number"),
            el("textbox", "GSTIN"), el("textbox", "Bank account number"), el("textbox", "Business Name"), el("textbox", "Notes")]}}

    async def type_into(self, name, text, **kw):
        return await self.h.call("type", {"element_id": self.ids[name], "snapshot_id": self.snap, "text": text}, **kw)

    async def test_text_typed_into_sensitive_fields_is_never_logged(self):
        await self.setUp_env(self.pages())
        for name, value in [("Password", "hunter2-synthetic"), ("Enter OTP", "482913"), ("Card number", "5555-synthetic-card"), ("PAN Number", "label-only-pan-1"),
                            ("GSTIN", "label-only-gstin-1"), ("Bank account number", "label-only-acct-1")]:
            r = await self.type_into(name, value)
            self.assertTrue(r["success"], r["error"])
        logs = log_text(self.h)
        for value in ("hunter2-synthetic", "482913", "5555-synthetic-card", "label-only-pan-1", "label-only-gstin-1", "label-only-acct-1"):
            self.assertNotIn(value, logs, f"{value} reached the action log")
        self.assertIn("[redacted", logs)

    async def test_value_the_page_reports_back_is_not_logged_either(self):
        await self.setUp_env(self.pages())
        await self.type_into("GSTIN", "echoed-back-value-9")
        self.assertNotIn("echoed-back-value-9", log_text(self.h), "result.value_after must be redacted for a sensitive field")

    async def test_identifier_shaped_text_is_redacted_even_in_an_unlabelled_field(self):
        await self.setUp_env(self.pages())
        await self.type_into("Notes", f"{PAN}")
        await self.type_into("Business Name", GSTIN)
        logs = log_text(self.h)
        self.assertNotIn(PAN, logs)
        self.assertNotIn(GSTIN, logs)

    async def test_ordinary_text_is_still_logged_as_before(self):
        await self.setUp_env(self.pages())
        r = await self.type_into("Business Name", "Example Growth LLC")
        self.assertTrue(r["success"])
        self.assertEqual(r["result"]["value_after"], "Example Growth LLC", "the caller still gets the real result")
        self.assertIn("Example Growth LLC", log_text(self.h), "non-sensitive operational logging is preserved")

    async def test_a_caller_can_declare_a_call_sensitive(self):
        await self.setUp_env(self.pages())
        r = await self.type_into("Notes", "declared secret value", sensitive_call=True)
        self.assertEqual(r["result"]["value_after"], "declared secret value")
        self.assertNotIn("declared secret value", log_text(self.h))

    async def test_error_messages_cannot_carry_an_identifier_into_the_log(self):
        await self.setUp_env(self.pages())
        self.fb.faults["type"] = {"success": False, "result": None, "error": {"code": "NOT_EDITABLE", "message": f"cannot type {PAN} here"}}
        await self.type_into("Notes", "x")
        self.assertNotIn(PAN, log_text(self.h))

    async def test_registered_values_are_scrubbed_from_every_log_field(self):
        await self.setUp_env(self.pages())
        SECRETS.register(ODD, "t")
        self.fb.faults["type"] = {"success": False, "result": None, "error": {"code": "NOT_EDITABLE", "message": f"page said no to {ODD}"}}
        await self.type_into("Notes", "x")
        self.assertNotIn(ODD, log_text(self.h))

    async def test_verify_checks_comparing_a_field_value_are_not_logged(self):
        await self.setUp_env(self.pages())
        await self.h.call("verify", {"checks": [{"type": "element_value", "element_id": self.ids["Notes"], "snapshot_id": self.snap, "equals": "compared-secret-1"}]})
        self.assertNotIn("compared-secret-1", log_text(self.h))


class ActivityAndHistoryTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        SECRETS.clear()

    def test_task_activity_never_holds_identifiers(self):
        t = TaskState("x")
        t.log("act", f"type text={PAN} then {GSTIN}")
        SECRETS.register(ODD, t.task_id)
        t.log("result", f"field shows {ODD}")
        blob = json.dumps(t.activity)
        for v in (PAN, GSTIN, ODD):
            self.assertNotIn(v, blob)

    def test_brief_args_omit_identifier_text(self):
        self.assertEqual(brief_args({"text": PAN})["text"], "[omitted]")
        self.assertEqual(brief_args({"text": GSTIN})["text"], "[omitted]")
        SECRETS.register(ODD, "t")
        self.assertEqual(brief_args({"text": f"see {ODD}"})["text"], "[omitted]")
        self.assertEqual(brief_args({"text": "hello"})["text"], "hello")

    async def test_history_model_call_rows_and_error_text_never_hold_values(self):
        # the user's own instruction contains a PAN and a provider error repeats it: neither may land in tasks.db or the activity feed
        err = ProviderError("unavailable", f"upstream rejected the request for {PAN}", provider="mock")
        async with Env(MockProvider([err])) as e:
            t = await e.run(f"Please use my PAN {PAN} for the form")
            self.assertEqual(t.status.value, "FAILED")
            hist = e.h.rt.history.get(t.task_id)
            self.assertNotIn(PAN, json.dumps(hist, default=str), "history holds neither the instruction's PAN nor the error's")
            self.assertNotIn(PAN, t.error or "")
            self.assertNotIn(PAN, json.dumps(t.activity))
            self.assertNotIn(PAN, json.dumps(hist["model_call_timing"]))


class AuditScrubTests(unittest.TestCase):
    def test_audit_detail_is_scrubbed_defensively(self):
        import tempfile

        from octg_agent.business.store import BusinessStore

        with tempfile.TemporaryDirectory() as d:
            s = BusinessStore(Path(d) / "b.db")
            SECRETS.register(ODD, "t")
            try:
                s.audit("use", detail=f"typed {ODD} and {PAN}", outcome="ok")
                row = s.audit_tail(1)[0]
                self.assertNotIn(ODD, json.dumps(row))
                self.assertNotIn(PAN, json.dumps(row))
            finally:
                SECRETS.clear()


if __name__ == "__main__":
    unittest.main()
