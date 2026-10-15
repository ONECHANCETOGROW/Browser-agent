import json
import re
import unittest
from pathlib import Path

from octg_agent.engine.registry import ACTIONS, tool_schema

EXT_ACTIONS_JS = Path(__file__).resolve().parents[2] / "extension" / "background" / "actions.js"


class RegistryTests(unittest.TestCase):
    def test_schema_is_machine_readable(self):
        s = tool_schema()
        json.dumps(s)  # must serialise
        self.assertEqual(len(s["tools"]), len(ACTIONS))
        for t in s["tools"]:
            self.assertTrue(t["description"], t["name"])
            self.assertEqual(t["input_schema"]["type"], "object")
            self.assertIs(t["input_schema"]["additionalProperties"], False)
            self.assertIn(t["risk"]["level"], {"none", "low", "medium", "high"})
            self.assertIn(t["risk"]["approval"], {"never", "contextual", "required"})
            self.assertTrue(t["risk"]["tags"], t["name"])
            for r in t["input_schema"]["required"]:
                self.assertIn(r, t["input_schema"]["properties"], f"{t['name']}.{r}")

    def test_required_capabilities_exist(self):
        needed = {"get_browser_state", "get_tabs", "get_active_tab", "navigate", "new_tab", "switch_tab", "close_tab", "reload", "back", "forward",
                  "inspect_page", "read_page", "get_page_state", "click", "type", "clear", "select", "check", "uncheck", "scroll", "press_key",
                  "screenshot", "upload_file", "download_file", "wait", "verify"}
        self.assertLessEqual(needed, set(ACTIONS))

    def test_element_actions_are_bound_to_snapshots(self):
        for name in ("type", "clear", "select", "check", "uncheck", "upload_file"):
            self.assertIn("snapshot_id", ACTIONS[name].required, name)
        for a in ACTIONS.values():
            if a.uses_elements:
                self.assertIn("snapshot_id", a.properties, a.name)

    def test_high_impact_actions_are_tagged(self):
        self.assertEqual(ACTIONS["upload_file"].risk.level, "high")
        self.assertEqual(ACTIONS["upload_file"].risk.approval, "required")
        self.assertEqual(ACTIONS["click"].risk.approval, "contextual")
        self.assertEqual(ACTIONS["close_tab"].risk.level, "medium")
        self.assertEqual(ACTIONS["inspect_page"].risk.level, "none")

    def test_registry_and_extension_implement_the_same_actions(self):
        """Contract test: every extension-target action has a handler in actions.js, and vice versa."""
        handlers = set(re.findall(r"^H\.(\w+)\s*=", EXT_ACTIONS_JS.read_text(encoding="utf-8"), re.M))
        registry = {n for n, a in ACTIONS.items() if a.target == "extension"}
        self.assertEqual(registry - handlers, set(), "registry actions missing in the extension")
        self.assertEqual(handlers - registry, set(), "extension handlers missing from the registry")


if __name__ == "__main__":
    unittest.main()
