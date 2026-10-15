"""The REAL side panel page (panel.html + panel.js + ask_ui.js) in headless Chromium with a stubbed `chrome` API: tab switching, the badge and banner, the Ask flow and the Inbox flow, at the side panel's
real width. The agent side is simulated by answering the panel's own messages, so this proves the page's wiring, not the agent (the agent side has its own tests)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .test_hub_browser import BrowserCase, CHROME  # noqa: F401 - BrowserCase skips itself without Chromium

PANEL = (Path(__file__).resolve().parents[2] / "extension" / "sidepanel" / "panel.html").as_uri()
STUB = """
(() => {
  window.__sent = [];
  let listener = null;
  const port = { name: 'octg-panel', postMessage: (m) => window.__sent.push(m), onMessage: { addListener: (fn) => { listener = fn; } }, onDisconnect: { addListener: () => {} } };
  window.chrome = { runtime: { connect: () => port, id: 'testid', getURL: (p) => p, sendMessage: () => {}, onMessage: { addListener() {} } }, storage: { local: { get: async () => ({}), set: async () => {} } } };
  window.__fire = (m) => listener && listener(m);
  // answer the panel's own request: the newest message with that command gets the given panel_result
  window.__answer = (command, panel, extra) => {
    const req = [...window.__sent].reverse().find((m) => m.command === command);
    if (!req) throw new Error('the panel never sent ' + command + '; sent: ' + window.__sent.map((m) => m.command || m.type).join(','));
    window.__fire({ type: 'panel_result', request_id: req.request_id, command, ok: true, panel, ...(extra || {}) });
    return req;
  };
  window.__fail = (command, error, code) => { const req = [...window.__sent].reverse().find((m) => m.command === command); window.__fire({ type: 'panel_result', request_id: req.request_id, command, ok: false, error, code }); };
  window.__state = (over) => window.__fire({ type: 'state', state: Object.assign({ connection: { state: 'connected', error: null, url: 'ws://127.0.0.1:8765/ext', has_token: true, enabled: true }, agent: {}, task: null, providers: null, toast: null,
    browser: { tab_count: 3, current_website: 'example.test', tab: { tab_id: 7, title: 'Verify', url: 'https://example.test/verify', supported: true, restricted_reason: '' } }, log: [], inbox: 0 }, over || {}) });
})();
"""
REQ = {"or_id": "or_1", "status": "open", "title": "Verify the account", "steps": ["Open the verify page", "Click Verify", "Read the message"], "question": "What does the page say?",
       "choices": ["It worked", "I see an error"], "want_page": True, "client_name": "Example Painting Co", "related": "", "created_at": 1.7e9, "attachments": []}


class PanelPageTests(BrowserCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.p.cmd("Emulation.setDeviceMetricsOverride", width=390, height=900, deviceScaleFactor=1, mobile=False)
        await self.p.cmd("Page.addScriptToEvaluateOnNewDocument", source=STUB)
        await self.p.goto(PANEL)
        await self.p.wait("typeof window.__fire === 'function' && document.getElementById('mainTabs')", what="the panel page")
        await self.p.js("window.__state()")

    async def sent(self, command=None):
        return await self.p.js("window.__sent" + (f".filter((m) => m.command === {json.dumps(command)})" if command else ""))

    async def test_the_three_sections_switch_and_the_original_task_panel_is_intact(self):
        await self.p.wait("document.getElementById('badge').textContent === 'connected'", what="connected badge")
        self.assertTrue(await self.p.js("!document.getElementById('tab-task').hidden && document.getElementById('tab-ask').hidden && document.getElementById('tab-inbox').hidden"))
        self.assertTrue(await self.p.js("!!document.getElementById('taskInput') && !!document.getElementById('run') && !!document.getElementById('log')"))
        await self.p.click("#mainTabs .mtab", "Ask")
        self.assertTrue(await self.p.js("document.getElementById('tab-task').hidden && !document.getElementById('tab-ask').hidden"))
        self.assertEqual(await self.p.js("getComputedStyle(document.getElementById('inboxCount')).display"), "none", "no red 0 badge when nothing is waiting")
        await self.p.click("#mainTabs .mtab", "Task")
        self.assertTrue(await self.p.js("!document.getElementById('tab-task').hidden && document.getElementById('tab-ask').hidden"))
        self.no_errors()

    async def test_ask_a_sentence_and_read_the_answer_in_the_panel(self):
        await self.p.click("#mainTabs .mtab", "Ask")
        await self.p.js("window.__answer('ask_clients', {clients: [{id: 'cli_1', name: 'Example Painting Co'}], default: 'cli_1'}); window.__answer('ask_list', {asks: []})")
        await self.p.wait("document.getElementById('askBody').innerText.includes('Nothing asked yet')", what="the empty feed")
        self.assertTrue(await self.p.js("[...document.querySelectorAll('#askBody select option')].map((o) => o.textContent).join() === 'Example Painting Co'"))
        await self.p.fill("#askBody textarea", "go to google and uh, search for painters in Alabama and search for best painters in Alabama. Tell me what you find.")
        await self.p.shot("30_panel_ask")
        await self.p.click("#askBody button", "Send")
        req = await self.p.js("window.__answer('ask_send', {understood: true, ask_id: 'ct_1', queries: ['painters in Alabama', 'best painters in Alabama'], notes: []})")
        self.assertEqual((req["client_id"], req["location"]), ("cli_1", ""))
        await self.p.wait("document.getElementById('askBody').innerText.includes('I understood: search Google for')", what="understood notice")
        await self.p.js("window.__answer('ask_list', {asks: [{ask_id: 'ct_1', text: 'search', client_id: 'cli_1', client_name: 'Example Painting Co', asked_at: 1.7e9, status: 'running', progress: 'search 2 of 2: best painters in Alabama', remaining: []}]})")
        await self.p.wait("document.getElementById('askBody').innerText.includes('Working: search 2 of 2')", what="progress")
        await self.p.wait("window.__sent.filter((m) => m.command === 'ask_list').length >= 3", timeout=10, what="the panel polling again while a search runs")
        await self.p.js("window.__answer('ask_list', {asks: [{ask_id: 'ct_1', text: 'search', client_id: 'cli_1', client_name: 'Example Painting Co', asked_at: 1.7e9, status: 'succeeded', outcome: 'complete', "
                        "report: 'I searched Google for 2 phrases and read the results.\\nYour website (example-painting.test) was not on the first page for any of the 2 searches I could read.', remaining: [], finished_at: 1.7e9}]})")
        await self.p.wait("document.getElementById('askBody').innerText.includes('was not on the first page for any of the 2 searches')", what="the answer")
        self.assertIn("Done", await self.p.text("#askBody"))
        await self.p.shot("31_panel_ask_answer")
        self.no_errors()

    async def test_a_waiting_request_shows_a_badge_and_banner_and_is_answered_with_an_attached_tab(self):
        await self.p.js("window.__state({inbox: 2})")
        await self.p.wait("!document.getElementById('inboxCount').hidden && document.getElementById('inboxCount').textContent === '2'", what="the badge")
        self.assertIn("2 things", await self.p.text("#inboxBanner"))
        await self.p.click("#inboxOpen")
        await self.p.wait("window.__sent.some((m) => m.command === 'inbox_list')", what="inbox_list request")
        self.assertTrue(await self.p.js("document.getElementById('inboxBanner').hidden"), "the banner hides once the Inbox is open")
        await self.p.js("window.__answer('inbox_list', {requests: [%s], open: 1})" % json.dumps(REQ))
        await self.p.wait("document.getElementById('inboxBody').innerText.includes('Click Verify')", what="the steps")
        text = await self.p.text("#inboxBody")
        for needle in ("Verify the account", "Open the verify page", "Read the message", "What does the page say?", "It worked", "I see an error", "About: Example Painting Co"):
            self.assertIn(needle, text)
        await self.p.shot("32_panel_inbox")
        await self.p.click("#inboxBody button", "I see an error")
        await self.p.fill("#inboxBody textarea", "It says the code expired")
        await self.p.click("#inboxBody button", "Attach this tab’s text and a screenshot")
        att = await self.p.js("window.__answer('inbox_attach', {attachment: {att_id: 'att_1', url: 'https://example.test/verify', title: 'Verify', chars: 321, screenshot: true}})")
        self.assertEqual((att["or_id"], att["tab_id"]), ("or_1", 7))
        await self.p.wait("document.getElementById('inboxBody').innerText.includes('Attached: Verify · 321 characters of text + a screenshot')", what="attachment line")
        await self.p.click("#inboxBody button", "Send answer")
        rep = await self.p.js("window.__answer('inbox_reply', {request: Object.assign({}, %s, {status: 'answered'}), open: 1})" % json.dumps(REQ))
        self.assertEqual((rep["or_id"], rep["choice"], rep["text"], rep["attachments"]), ("or_1", "I see an error", "It says the code expired", ["att_1"]))
        await self.p.wait("window.__sent.filter((m) => m.command === 'inbox_list').length >= 2", what="the list refreshed after replying")
        await self.p.js("window.__answer('inbox_list', {requests: [], open: 0}); window.__state({inbox: 0})")
        await self.p.wait("document.getElementById('inboxBody').innerText.includes('Nothing waiting for you')", what="the empty inbox")
        self.assertTrue(await self.p.js("document.getElementById('inboxCount').hidden"))
        self.no_errors()

    async def test_when_not_connected_the_ask_tab_says_so_and_jumps_to_the_connection_settings(self):
        await self.p.js("window.__state({connection: {state: 'idle', error: null, url: 'ws://127.0.0.1:8765/ext', has_token: false, enabled: false}})")
        await self.p.wait("document.getElementById('badge').textContent === 'idle'")
        await self.p.click("#mainTabs .mtab", "Ask")
        await self.p.wait("document.getElementById('askBody').innerText.includes('Not connected to the agent, so your clients and answers cannot load')", what="the offline notice")
        self.assertTrue(await self.p.js("[...document.querySelectorAll('#askBody button')].find((b) => b.textContent === 'Send').disabled"))
        self.assertNotIn("Add a client", await self.p.text("#askBody"))
        await self.p.shot("33_panel_not_connected")
        await self.p.click("#askBody button", "Open connection settings")
        self.assertTrue(await self.p.js("!document.getElementById('tab-task').hidden && document.getElementById('conn').open"), "lands on the Task tab with Connection open")
        await self.p.js("window.__state()")         # connecting later turns the Ask tab live without a reload
        await self.p.click("#mainTabs .mtab", "Ask")
        await self.p.wait("window.__sent.some((m) => m.command === 'ask_clients')", what="the client list is requested once connected")
        await self.p.js("window.__answer('ask_clients', {clients: [{id: 'cli_1', name: 'Lakeport Pest Control'}], default: 'cli_1'})")
        await self.p.wait("[...document.querySelectorAll('#askBody option')].some((o) => o.textContent === 'Lakeport Pest Control')", what="the client appears")
        self.assertFalse(await self.p.js("[...document.querySelectorAll('#askBody button')].find((b) => b.textContent === 'Send').disabled"))
        self.no_errors()

    async def test_the_attach_button_is_disabled_on_a_chrome_internal_page(self):
        await self.p.js("window.__state({browser: {tab_count: 1, current_website: 'extensions', tab: {tab_id: 3, title: 'Extensions', url: 'chrome://extensions', supported: false, restricted_reason: 'Chrome internal pages block extension scripts.'}}, inbox: 1})")
        await self.p.click("#mainTabs .mtab[data-tab='inbox']")
        await self.p.wait("window.__sent.some((m) => m.command === 'inbox_list')")
        await self.p.js("window.__answer('inbox_list', {requests: [%s], open: 1})" % json.dumps(REQ))
        await self.p.wait("document.getElementById('inboxBody').innerText.includes('Verify the account')")
        self.assertTrue(await self.p.js("[...document.querySelectorAll('#inboxBody button')].find((b) => b.textContent.startsWith('Attach this tab')).disabled"))
        self.assertIn("cannot be read", await self.p.text("#inboxBody"))
        self.no_errors()


if __name__ == "__main__":
    import unittest
    unittest.main()
