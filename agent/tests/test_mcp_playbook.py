"""Every chat that connects through the MCP adapter is told how to use the eye, so the owner never has to repeat it."""
from __future__ import annotations

import json
import unittest

from octg_agent.collab import mcp_server as M
from octg_agent.collab import playbook as P
from octg_agent.collab.capabilities import REGISTRY
import octg_agent.collab.caps  # noqa: F401


class PlaybookTests(unittest.TestCase):
    def test_the_connector_sends_the_short_guide_when_a_chat_connects(self):
        r = M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, None)["result"]
        self.assertEqual(r["instructions"], P.SHORT)
        for must in ("collab_playbook", "eye.open", "eye.wait_human", "Never try to get past a check", "SEEN", "INFERRED", "NOT SEEN", "INDIA", "FoxyProxy", "web.archive.org", "owner.ask", "gbp.reply_post", "One client per task"):
            self.assertIn(must, r["instructions"], must)
        self.assertLess(len(P.SHORT), 3200, "it is read in every chat: keep it short")

    def test_the_full_guide_is_a_tool_that_needs_no_agent_connection(self):
        names = [t["name"] for t in M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)["result"]["tools"]]
        self.assertEqual(names[0], "collab_playbook", "listed first, described as READ THIS FIRST")
        out = M.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "collab_playbook", "arguments": {}}}, None)["result"]
        self.assertFalse(out["isError"])
        text = json.loads(out["content"][0]["text"])["playbook"]
        self.assertTrue(text.startswith(P.SHORT))
        for must in ("Search Console", "Google Analytics 4", "Google Business Profile", "Meta Ad Library", "search-analytics?resource_id=sc-domain", "gbp.reviews", "reload the OCTG extension"):
            self.assertIn(must, text, must)

    def test_every_capability_the_guide_names_really_exists(self):
        for cap in ("eye.open", "eye.look", "eye.act", "eye.table", "eye.close", "eye.wait_human", "gbp.reviews", "gbp.reply_post", "owner.ask"):
            self.assertIn(cap, REGISTRY, f"{cap} is named in the guide but is not a real capability")
            self.assertIn(cap, P.FULL, f"the guide never mentions {cap}")


if __name__ == "__main__":
    unittest.main()
