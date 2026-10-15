"""Architecture guards: the browser engine stays model-independent and website-agnostic; the extension stays minimal-privilege."""
import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "agent" / "octg_agent"
EXT = ROOT / "extension"

SITE_NAMES = ["instagram", "facebook", "shopify", "gmail", "canva", "semrush", "search console", "searchconsole", "linkedin", "twitter",
              "tiktok", "youtube", "flipkart", "amazon", "wordpress", "hostinger", "example-agency", "cloudy"]
PROVIDER_NAMES = ["gemini", "openai", "freellmapi", "anthropic", "ollama"]  # only the model layer / AI layer / CLI may name these


class IsolationTests(unittest.TestCase):
    def test_engine_and_bridge_never_import_the_model_or_ai_layer(self):
        for sub in ("engine", "bridge", "files"):
            for py in (PKG / sub).glob("*.py"):
                tree = ast.parse(py.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    mods = []
                    if isinstance(node, ast.Import):
                        mods = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        mods = [("." * node.level) + (node.module or "")] + [a.name for a in node.names]
                    for m in mods:
                        self.assertNotIn("reasoning", m, f"{py.name} imports the model layer")
                        self.assertNotIn("brain", m, f"{py.name} imports the AI orchestration layer")

    def test_no_website_specific_logic_anywhere(self):
        files = [p for p in PKG.rglob("*.py")] + [p for p in EXT.rglob("*") if p.suffix in (".js", ".json", ".html", ".css")]
        for p in files:
            text = p.read_text(encoding="utf-8").lower()
            for name in SITE_NAMES:
                self.assertNotIn(name, text, f"{p.relative_to(ROOT)} mentions {name!r}")

    def test_browser_engine_and_extension_do_not_know_model_providers(self):
        files = [*(PKG / "engine").glob("*.py"), *(PKG / "bridge").glob("*.py"), *(PKG / "files").glob("*.py"), *EXT.rglob("*.js"), *EXT.rglob("*.html")]
        for p in files:
            text = p.read_text(encoding="utf-8").lower()
            for name in PROVIDER_NAMES:
                self.assertNotIn(name, text, f"{p.relative_to(ROOT)} mentions provider {name!r}")

    def test_no_remote_endpoints_in_code(self):
        urls = re.compile(r"https?://(?!127\.0\.0\.1|localhost|www\.w3\.org)[\w.-]+\.\w+", re.I)
        for p in [*(PKG / "engine").glob("*.py"), *(PKG / "bridge").glob("*.py"), *(PKG / "files").glob("*.py"), *EXT.rglob("*.js")]:
            for m in urls.finditer(p.read_text(encoding="utf-8")):
                self.assertTrue(m.group(0).endswith(("google.com",)) or "chromewebstore" in m.group(0), f"{p.name}: remote URL {m.group(0)}")

    def test_extension_permissions_are_exactly_what_phase_1_needs(self):
        m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["manifest_version"], 3)
        self.assertEqual(set(m["permissions"]), {"tabs", "scripting", "webNavigation", "sidePanel", "storage", "alarms", "downloads"})
        self.assertNotIn("debugger", m["permissions"])
        self.assertNotIn("debugger", m.get("optional_permissions", []))
        for banned in ("cookies", "history", "webRequest", "nativeMessaging", "management", "proxy", "privacy", "browsingData", "clipboardRead", "identity"):
            self.assertNotIn(banned, m["permissions"])
        self.assertEqual(m["host_permissions"], ["<all_urls>"])
        self.assertNotIn("content_scripts", m)  # injected on demand only
        self.assertNotIn("externally_connectable", m)

    def test_extension_extension_id_matches_the_pinned_key(self):
        import base64
        import hashlib
        from octg_agent.config import DEFAULT_EXTENSION_ID
        der = base64.b64decode(json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))["key"])
        h = hashlib.sha256(der).hexdigest()[:32]
        self.assertEqual("".join(chr(ord("a") + int(c, 16)) for c in h), DEFAULT_EXTENSION_ID)

    def test_extension_never_reads_credentials(self):
        text = chr(10).join(p.read_text(encoding="utf-8") for p in EXT.rglob("*.js"))
        for banned in ("chrome.cookies", "document.cookie", "chrome.debugger", "chrome.history", "chrome.identity", "eval(", "new Function("):
            self.assertNotIn(banned, text)
        # page-facing and background code must not use web storage at all; the side panel may remember a UI choice (the selected model name)
        code = chr(10).join(p.read_text(encoding="utf-8") for sub in ("background", "content") for p in (EXT / sub).rglob("*.js"))
        self.assertNotIn("localStorage", code)
        panel = (EXT / "sidepanel" / "panel.js").read_text(encoding="utf-8")
        self.assertEqual(panel.count("localStorage."), 2)  # exactly one getItem and one setItem (both wrapped in try/catch helpers)
        self.assertNotIn("localStorage.setItem('octg.token", panel)

    def test_model_api_keys_never_reach_the_extension(self):
        for p in EXT.rglob("*"):
            if p.suffix in (".js", ".html", ".json"):
                text = p.read_text(encoding="utf-8").lower()
                for needle in ("api_key", "apikey", "gemini_api", "provider_keys", "bearer "):
                    self.assertNotIn(needle, text, f"{p.name} mentions {needle!r}")

    def test_extension_only_dials_loopback(self):
        text = (EXT / "background" / "bridge.js").read_text(encoding="utf-8")
        self.assertIn("isLocalUrl", text)
        self.assertIn("'127.0.0.1', 'localhost', '[::1]'", text)


if __name__ == "__main__":
    unittest.main()
