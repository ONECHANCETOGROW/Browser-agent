import asyncio
import base64
import json
import unittest

from .helpers import Harness, TINY_PNG_B64, tab, wait_until

SNAP = {
    "snapshot_id": "snap_1", "tab_id": 1, "url": "https://example.test/",
    "elements": [
        {"id": "el_001", "tag": "button", "role": "button", "name": "Delete account"},
        {"id": "el_002", "tag": "input", "role": "textbox", "name": "Password", "type": "password"},
        {"id": "el_003", "tag": "a", "role": "link", "name": "About us", "href": "https://example.test/about"},
        {"id": "el_004", "tag": "input", "role": "button", "name": "Save", "type": "submit"},
    ],
}


async def with_snapshot(h):
    ext = await h.extension(handlers={"inspect_page": lambda p, c: {"result": SNAP}})
    await ext.send({"type": "tabs_snapshot", "tabs": [tab(1, active=True)]})
    await wait_until(lambda: 1 in h.rt.state.tabs)
    r = await h.call("inspect_page")
    assert r["success"], r
    return ext


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_keeps_request_id_and_full_shape(self):
        async with Harness() as h:
            ext = await h.extension(handlers={"ping": lambda p, c: {"result": {"pong": True}}})
            r = await h.call("ping", request_id="abc123")
            self.assertEqual(r["request_id"], "abc123")
            self.assertEqual(ext.received[0]["request_id"], "abc123")
            self.assertEqual(ext.received[0]["parameters"], {})
            self.assertEqual(set(r), {"request_id", "success", "action", "result", "error", "verification", "risk", "duration_ms"})
            self.assertTrue(r["success"])
            self.assertEqual(r["result"], {"pong": True})
            self.assertEqual(r["risk"]["level"], "none")

    async def test_not_connected_is_a_structured_failure(self):
        async with Harness() as h:
            r = await h.call("get_tabs")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"]["code"], "EXTENSION_NOT_CONNECTED")

    async def test_unknown_action_and_bad_params_never_reach_the_extension(self):
        async with Harness() as h:
            ext = await h.extension()
            self.assertEqual((await h.call("teleport"))["error"]["code"], "UNKNOWN_ACTION")
            self.assertEqual((await h.call("navigate"))["error"]["code"], "INVALID_PARAMS")
            self.assertEqual((await h.call("navigate", {"url": 5}))["error"]["code"], "INVALID_PARAMS")
            self.assertEqual((await h.call("navigate", {"url": "x.test", "bogus": 1}))["error"]["code"], "INVALID_PARAMS")
            r = await h.call("click", {"element_id": "el_001"})
            self.assertEqual(r["error"]["code"], "INVALID_PARAMS")
            self.assertIn("snapshot_id", r["error"]["message"])
            self.assertEqual((await h.call("click"))["error"]["code"], "INVALID_PARAMS")
            self.assertEqual((await h.call("upload_file", {"element_id": "el_1", "snapshot_id": "s"}))["error"]["code"], "INVALID_PARAMS")
            self.assertEqual(ext.received, [])

    async def test_timeout_is_reported_not_hung(self):
        async with Harness() as h:
            await h.extension(handlers={"ping": lambda p, c: None})
            r = await h.call("ping", timeout_s=0.3)
            self.assertEqual(r["error"]["code"], "ACTION_TIMEOUT")
            self.assertLess(r["duration_ms"], 2000)

    async def test_extension_failure_is_passed_through_not_masked(self):
        async with Harness() as h:
            err = {"code": "UNSUPPORTED_PAGE", "message": "This Chrome page cannot be controlled by the extension."}
            await h.extension(handlers={"inspect_page": lambda p, c: {"success": False, "result": None, "error": err}})
            r = await h.call("inspect_page")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"], err)

    async def test_malformed_extension_reply_is_not_success(self):
        async with Harness() as h:
            await h.extension(handlers={"ping": lambda p, c: {"success": False, "error": None}})
            r = await h.call("ping")
            self.assertFalse(r["success"])
            self.assertEqual(r["error"]["code"], "BAD_EXTENSION_RESPONSE")

    async def test_actions_are_serialised(self):
        async with Harness() as h:
            running = {"n": 0, "max": 0}

            async def slow(p, c):
                running["n"] += 1
                running["max"] = max(running["max"], running["n"])
                await asyncio.sleep(0.05)
                running["n"] -= 1
                return {"result": {}}

            await h.extension(handlers={"ping": slow})
            await asyncio.gather(*[h.call("ping") for _ in range(5)])
            self.assertEqual(running["max"], 1)

    async def test_snapshot_recorded_and_risk_escalates_for_dangerous_targets(self):
        async with Harness() as h:
            await with_snapshot(h)
            snap = {"snapshot_id": "snap_1", "tab_id": 1}
            r = await h.call("click", {"element_id": "el_001", **snap})
            self.assertEqual(r["risk"]["level"], "high")
            self.assertEqual(r["risk"]["approval"], "required")
            self.assertIn("potentially_irreversible", r["risk"]["tags"])
            self.assertTrue(r["risk"]["reasons"])
            self.assertFalse(r["risk"]["approval_enforced"])  # Phase 1 tags only
            safe = await h.call("click", {"element_id": "el_003", **snap})
            self.assertEqual(safe["risk"]["level"], "medium")
            self.assertNotIn("potentially_irreversible", safe["risk"]["tags"])
            pw = await h.call("type", {"element_id": "el_002", "text": "x", **snap})
            self.assertIn("credential_field", pw["risk"]["tags"])
            self.assertEqual(pw["risk"]["level"], "high")
            sub = await h.call("click", {"element_id": "el_004", **snap})
            self.assertIn("form_submit", sub["risk"]["tags"])
            self.assertEqual((await h.call("press_key", {"key": "Enter"}))["risk"]["tags"].count("may_submit_form"), 1)

    async def test_stale_snapshot_id_is_forwarded_so_the_extension_can_reject_it(self):
        async with Harness() as h:
            err = {"code": "STALE_ELEMENT", "message": "stale"}
            ext = await h.extension(handlers={"click": lambda p, c: {"success": False, "error": err}})
            r = await h.call("click", {"element_id": "el_001", "snapshot_id": "snap_OLD"})
            self.assertEqual(r["error"]["code"], "STALE_ELEMENT")
            self.assertEqual(ext.received[0]["parameters"]["snapshot_id"], "snap_OLD")

    async def test_navigation_commit_marks_snapshot_stale(self):
        async with Harness() as h:
            ext = await with_snapshot(h)
            await ext.event("navigation_committed", {"tab_id": 1, "url": "https://example.test/next"})
            self.assertTrue(await wait_until(lambda: h.rt.state.snapshots[1]["stale"]))
            await ext.event("tab_removed", {"tab_id": 1, "window_id": 1})
            self.assertTrue(await wait_until(lambda: 1 not in h.rt.state.snapshots))

    async def test_upload_sends_bytes_not_paths_and_enforces_allow_list(self):
        async with Harness() as h:
            folder = h.tmp / "uploads"
            folder.mkdir()
            (folder / "logo.png").write_bytes(b"PNGDATA")
            outside = h.tmp / "private.txt"
            outside.write_text("secret")
            ext = await h.extension(handlers={"upload_file": lambda p, c: {"result": {"files": [{"name": "logo.png", "size": 7}]}}})
            denied = await h.call("upload_file", {"element_id": "el_1", "snapshot_id": "s", "local_file_path": str(folder / "logo.png")})
            self.assertEqual(denied["error"]["code"], "FILE_NOT_AUTHORIZED")
            self.assertEqual(ext.received, [])  # refused before anything left the agent
            h.rt.folders.allow(str(folder))
            ok = await h.call("upload_file", {"element_id": "el_1", "snapshot_id": "s", "local_file_path": str(folder / "logo.png")})
            self.assertTrue(ok["success"], ok)
            sent = ext.received[0]["parameters"]
            self.assertNotIn("local_file_path", sent)
            self.assertNotIn(str(folder), json.dumps(sent))
            self.assertEqual(base64.b64decode(sent["files"][0]["data_b64"]), b"PNGDATA")
            self.assertEqual(sent["files"][0]["type"], "image/png")
            self.assertEqual(ok["result"]["local_files"][0]["size"], 7)
            self.assertEqual(ok["risk"]["level"], "high")
            esc = await h.call("upload_file", {"element_id": "el_1", "snapshot_id": "s", "local_file_path": str(outside)})
            self.assertEqual(esc["error"]["code"], "FILE_NOT_AUTHORIZED")
            self.assertEqual(len(ext.received), 1)

    async def test_upload_too_large_refused(self):
        async with Harness(max_upload_bytes=10) as h:
            folder = h.tmp / "u"
            folder.mkdir()
            (folder / "big.bin").write_bytes(b"x" * 11)
            h.rt.folders.allow(str(folder))
            await h.extension()
            r = await h.call("upload_file", {"element_id": "el_1", "snapshot_id": "s", "local_file_path": str(folder / "big.bin")})
            self.assertEqual(r["error"]["code"], "FILE_TOO_LARGE")

    async def test_screenshot_is_saved_locally_and_not_returned_inline(self):
        async with Harness() as h:
            data_url = "data:image/png;base64," + TINY_PNG_B64
            await h.extension(handlers={"screenshot": lambda p, c: {"result": {"data_url": data_url, "format": "png", "width": 1, "height": 1}}})
            r = await h.call("screenshot")
            self.assertTrue(r["success"])
            self.assertNotIn("data_url", r["result"])
            from pathlib import Path
            p = Path(r["result"]["path"])
            self.assertTrue(p.is_file())
            self.assertEqual(p.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(p.parent, h.settings.screenshots_dir)

    async def test_structured_log_has_required_fields_and_redacts_payloads(self):
        async with Harness() as h:
            folder = h.tmp / "u"
            folder.mkdir()
            (folder / "a.txt").write_text("hello")
            h.rt.folders.allow(str(folder))
            await h.extension(handlers={"upload_file": lambda p, c: {"result": {"ok": True}}})
            await h.call("upload_file", {"element_id": "el_1", "snapshot_id": "s", "local_file_path": str(folder / "a.txt")}, request_id="req_log1")
            await h.call("navigate", {"url": ""})  # invalid -> logged as failure too
            entries = h.rt.actionlog.tail(10)
            up = next(e for e in entries if e["request_id"] == "req_log1")
            for key in ("ts", "request_id", "action", "parameters", "result", "duration_ms", "error", "success"):
                self.assertIn(key, up)
            self.assertNotIn("data_b64", json.dumps(up))  # file contents never reach the log
            bad = next(e for e in entries if e["action"] == "navigate")
            self.assertEqual(bad["error"]["code"], "INVALID_PARAMS")
            self.assertFalse(bad["success"])

    async def test_page_content_and_sensitive_text_never_reach_the_log(self):
        async with Harness() as h:
            snap = {"snapshot_id": "snap_1", "tab_id": 1, "url": "https://x.test/", "title": "T", "counts": {"returned": 2}, "truncated": False,
                    "elements": [{"id": "el_001", "tag": "input", "role": "textbox", "name": "Card number", "sensitive": True, "value": "[hidden]"},
                                 {"id": "el_002", "tag": "input", "role": "textbox", "name": "Email", "value": "private@person.test"}]}
            await h.extension(handlers={
                "inspect_page": lambda p, c: {"result": snap},
                "read_page": lambda p, c: {"result": {"scope": "page", "text": "CONFIDENTIAL BODY TEXT", "total_chars": 22, "truncated": False, "url": "https://x.test/", "title": "T"}},
                "type": lambda p, c: {"result": {"value_after": "[hidden: 4 chars]", "value_matches": True}},
                "verify": lambda p, c: {"result": {"all_passed": True, "checks": [{"type": "element_value", "passed": True, "actual": "private@person.test"},
                                                                                  {"type": "url_contains", "passed": True, "actual": "https://x.test/"}]}}})
            await h.call("inspect_page")
            await h.call("read_page", {"scope": "page"})
            r = await h.call("type", {"element_id": "el_001", "snapshot_id": "snap_1", "tab_id": 1, "text": "4111111111111111"})
            self.assertIn("credential_field", r["risk"]["tags"])
            await h.call("verify", {"checks": [{"type": "element_value", "element_id": "el_002", "snapshot_id": "snap_1", "equals": "private@person.test"}]})
            blob = json.dumps(h.rt.actionlog.tail(20))
            for secret in ("4111111111111111", "private@person.test", "CONFIDENTIAL BODY TEXT"):
                self.assertNotIn(secret, blob)
            self.assertIn("https://x.test/", blob)  # metadata is still logged

    async def test_agent_side_actions_work_without_a_browser(self):
        async with Harness() as h:
            self.assertEqual((await h.call("set_task", {"task": "Check indexing"}))["result"]["task"], "Check indexing")
            self.assertIsNone((await h.call("set_task", {"task": None}))["result"]["task"])
            self.assertEqual((await h.call("set_task", {"task": "Check indexing"}))["result"]["task"], "Check indexing")
            self.assertEqual(h.rt.state.task, "Check indexing")
            self.assertIn("state", (await h.call("get_agent_state"))["result"])
            ev = (await h.call("get_events"))["result"]
            self.assertIn("events", ev)
            self.assertEqual((await h.call("fs_list_roots"))["result"]["roots"], [])
            self.assertEqual((await h.call("fs_find", {"pattern": "*.png"}))["error"]["code"], "FILE_NOT_AUTHORIZED")

    async def test_fs_save_download_copies_into_authorized_folder_only(self):
        async with Harness() as h:
            src = h.tmp / "Downloads" / "report.pdf"
            src.parent.mkdir()
            src.write_bytes(b"%PDF-1")
            dest = h.tmp / "client"
            dest.mkdir()
            ext = await h.extension(handlers={"list_downloads": lambda p, c: {"result": {"downloads": [{"download_id": 7, "state": "complete", "filename": str(src)}]}}})
            denied = await h.call("fs_save_download", {"download_id": 7, "dest_dir": str(dest)})
            self.assertEqual(denied["error"]["code"], "FILE_NOT_AUTHORIZED")
            h.rt.folders.allow(str(dest))
            ok = await h.call("fs_save_download", {"download_id": 7, "dest_dir": str(dest)})
            self.assertTrue(ok["success"], ok)
            self.assertEqual((dest / "report.pdf").read_bytes(), b"%PDF-1")
            missing = await h.call("fs_save_download", {"download_id": 99, "dest_dir": str(dest)})
            self.assertEqual(missing["error"]["code"], "DOWNLOAD_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
