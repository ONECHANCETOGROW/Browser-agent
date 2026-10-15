import unittest

from .helpers import API, Harness


class HttpApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_required(self):
        async with Harness() as h:
            self.assertEqual((await h.api("GET", "/api/state", token=None))[0], 401)
            self.assertEqual((await h.api("GET", "/api/state", token="wrong"))[0], 401)
            self.assertEqual((await h.api("POST", "/api/action", json_body={"action": "ping"}, token=None))[0], 401)
            status, data = await h.api("GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertFalse(data["connected"])

    async def test_non_ascii_token_header_is_rejected_not_a_crash(self):
        async with Harness() as h:
            status, _ = await h.api("GET", "/api/state", token="tést")
            self.assertEqual(status, 401)

    async def test_bearer_header_also_works(self):
        async with Harness() as h:
            status, _ = await h.api("GET", "/api/state", token=None, headers={"Authorization": f"Bearer {API}"})
            self.assertEqual(status, 200)

    async def test_web_page_requests_are_rejected_even_with_the_token(self):
        async with Harness() as h:
            status, _ = await h.api("POST", "/api/action", json_body={"action": "get_tabs"}, headers={"Origin": "https://evil.example"})
            self.assertEqual(status, 403)

    async def test_dns_rebinding_host_rejected(self):
        async with Harness() as h:
            status, _ = await h.api("GET", "/api/state", headers={"Host": "evil.example:80"})
            self.assertEqual(status, 403)

    async def test_healthz_is_open_but_reveals_nothing(self):
        async with Harness() as h:
            status, data = await h.api("GET", "/healthz", token=None)
            self.assertEqual((status, data), (200, {"ok": True}))

    async def test_tools_endpoint_exports_schema(self):
        async with Harness() as h:
            status, data = await h.api("GET", "/api/tools")
            self.assertEqual(status, 200)
            names = {t["name"] for t in data["tools"]}
            self.assertTrue({"click", "inspect_page", "upload_file"} <= names)

    async def test_action_endpoint_validates_body(self):
        async with Harness() as h:
            self.assertEqual((await h.api("POST", "/api/action", json_body=[1]))[0], 400)
            self.assertEqual((await h.api("POST", "/api/action", json_body={"parameters": {}}))[0], 400)
            self.assertEqual((await h.api("POST", "/api/action", json_body={"action": "ping", "parameters": 3}))[0], 400)
            status, data = await h.api("POST", "/api/action", json_body={"action": "ping"})
            self.assertEqual(status, 200)
            self.assertEqual(data["error"]["code"], "EXTENSION_NOT_CONNECTED")

    async def test_allow_list_cannot_be_changed_over_http(self):
        async with Harness() as h:
            for path in ("/api/fs/allow", "/api/folders", "/api/authorized_folders"):
                self.assertEqual((await h.api("POST", path, json_body={"path": str(h.tmp)}))[0], 404)
            self.assertEqual((await h.call("fs_list_roots"))["result"]["roots"], [])
            # and there is no action that can do it either
            r = await h.call("fs_allow", {"path": str(h.tmp)})
            self.assertEqual(r["error"]["code"], "UNKNOWN_ACTION")


if __name__ == "__main__":
    unittest.main()
