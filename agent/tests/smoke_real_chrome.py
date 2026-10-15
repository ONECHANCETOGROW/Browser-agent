"""Smoke test for YOUR installed Chrome with the extension loaded and connected to a running agent.

    python -m octg_agent serve                 # terminal 1 (keep open), extension connected via the side panel
    python -m tests.smoke_real_chrome          # terminal 2, from D:\\Tool\\browser agent\\agent

Safety: it never closes, navigates, types into or clicks anything in your existing tabs. The only calls that touch them are read-only
(get_tabs, get_browser_state, get_active_tab, get_page_state, read_page on the active tab; only lengths/URLs are printed, never page text).
All interaction happens in tabs it opens itself from a local fixture page, and those are closed again at the end.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import secrets
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from octg_agent.config import DEFAULT_EXTENSION_ID, load_secrets, load_settings

from .e2e.env import FIXTURES, QuietHandler, free_port
from .e2e.run_e2e import T


class LiveEnv:
    """Just enough of E2EEnv for the T driver, pointed at the agent you already started."""

    def __init__(self):
        self.settings = load_settings()
        self.secrets = load_secrets(self.settings)
        self.base = self.settings.base_url
        self.api_token = self.secrets.api_token
        self.data_dir = self.settings.data_dir
        self.tmp = Path(tempfile.mkdtemp(prefix="octg_smoke_"))
        self.fixture_port = free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.fixture_port), functools.partial(QuietHandler, directory=str(FIXTURES)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def fixture(self, name: str) -> str:
        return f"http://127.0.0.1:{self.fixture_port}/{name}"

    def stop(self) -> None:
        self.httpd.shutdown()


RESULTS: list[tuple[str, bool, str]] = []


def step(label: str):
    def deco(fn):
        def run(*a, **k):
            t0 = time.time()
            try:
                note = fn(*a, **k) or ""
                RESULTS.append((label, True, note))
                print(f"  PASS  {label}  {note}  ({time.time() - t0:.1f}s)")
            except Exception as e:  # noqa: BLE001
                RESULTS.append((label, False, f"{type(e).__name__}: {e}"))
                print(f"  FAIL  {label}\n        {type(e).__name__}: {str(e)[:500]}")
                if "--debug" in sys.argv:
                    traceback.print_exc()
        return run
    return deco


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-tabs", action="store_true", help="leave the tabs it opened (for looking at them)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    env = LiveEnv()
    t = T(env)
    st: dict = {}
    pw = "SMOKE-PW-" + secrets.token_hex(6)
    created: list[int] = []
    print(f"Agent {env.base}; fixture page on 127.0.0.1:{env.fixture_port}\n")

    @step("0  agent reachable and extension connected (ID + Chrome version)")
    def preflight():
        try:
            with urllib.request.urlopen(env.base + "/healthz", timeout=3):
                pass
        except Exception as e:
            raise RuntimeError("Agent is not running. Start it: python -m octg_agent serve") from e
        state = t.get("/api/state")
        if not state["connected"]:
            raise RuntimeError("Agent is running but the extension is not connected. Side panel -> Connection -> paste `python -m octg_agent token` -> Connect.")
        ext = state["extension"]
        assert ext["extension_id"] == DEFAULT_EXTENSION_ID, f"unexpected extension id {ext['extension_id']}"
        ua = ext.get("user_agent") or ""
        st["ua"] = ua
        return f"id={ext['extension_id']} version={ext['version']} {ua.split(' ')[-2] if 'Chrome/' in ua else ''}"

    preflight()
    if not RESULTS[-1][1]:
        env.stop()
        return 1

    @step("5/6 python -m octg_agent call get_tabs sees your real tabs")
    def real_tabs():
        r = t.ok("get_tabs")["result"]
        assert r["count"] >= 1 and r["active_tab_id"] is not None
        st["initial_count"], st["initial_active"] = r["count"], r["active_tab_id"]
        print(f"        {r['count']} tab(s) seen. Compare with your Chrome window:")
        for x in r["tabs"]:
            host = x["url"].split("/")[2] if "://" in x["url"] and len(x["url"].split("/")) > 2 else x["url"][:40]
            print(f"          {'*' if x['active'] else ' '} #{x['tab_id']}  {host[:38]:<38} {x['title'][:48]!r}  {'' if x['supported'] else '[restricted]'}")
        agent_view = t.get("/api/state")
        assert agent_view["tab_count"] == r["count"], "agent tab model disagrees with Chrome"
        return f"({r['count']} tabs)"

    real_tabs()

    @step("8a get_active_tab / get_browser_state / get_page_state / read_page on your active tab (read-only)")
    def read_only():
        act = t.ok("get_active_tab")["result"]["tab"]
        t.ok("get_browser_state")
        tid = act["tab_id"]
        if act["supported"]:
            ps = t.ok("get_page_state", tab_id=tid)["result"]
            txt = t.ok("read_page", tab_id=tid)["result"]
            return f"active tab '{ps['tab']['title'][:40]}' url={act['url'][:60]} (viewport text {len(txt['text'])} chars)"
        r = t.fail("get_page_state", "UNSUPPORTED_PAGE", tab_id=tid)
        return f"active tab is a restricted page ({act['url'][:40]}) -> UNSUPPORTED_PAGE, as expected"

    read_only()

    @step("8b new tab on the local fixture page")
    def new_tab():
        r = t.ok("new_tab", url=env.fixture("index.html"))
        st["A"] = r["result"]["tab"]["tab_id"]
        created.append(st["A"])
        assert r["result"]["tab"]["title"] == "OCTG Fixture Home"
        assert [x["tab_id"] for x in r["verification"]["new_tabs"]] == [st["A"]]

    new_tab()
    if "A" not in st:
        env.stop()
        return 1
    A = st["A"]

    @step("8c inspect_page + get_page_state + read_page in that tab")
    def inspect():
        r = t.inspect(tab_id=A)
        assert r["counts"]["returned"] >= 25, r["counts"]
        t.el("Full name"), t.el("Shadow button"), t.el("Frame input")  # shadow DOM + iframe
        s = t.ok("get_page_state", tab_id=A)["result"]
        assert s["page"]["scroll"]["max_y"] > 1000
        assert "Fixture Home" in t.ok("read_page", tab_id=A)["result"]["text"]
        return f"{r['counts']['returned']} elements, snapshot {r['snapshot_id']}"

    inspect()

    @step("8d type (text + password) and click, with verification")
    def type_click():
        r = t.act("type", "Full name", tab_id=A, text="Smoke Test")
        assert r["result"]["value_matches"] is True and r["result"]["value_after"] == "Smoke Test"
        p = t.act("type", "Password", tab_id=A, text=pw)
        assert pw not in json.dumps(p) and p["result"]["value_after"].startswith("[hidden")
        c = t.act("click", "Click me", tab_id=A)
        assert c["verification"]["page"]["text_changed"] is True
        assert t.call("verify", tab_id=A, checks=[{"type": "text_present", "value": "Clicked!"}])["success"]

    type_click()

    @step("8e scroll (down, bottom, top)")
    def scroll():
        d = t.ok("scroll", tab_id=A, direction="down")
        assert d["result"]["moved"] and d["verification"]["page"]["scroll_changed"]
        b = t.ok("scroll", tab_id=A, to="bottom")
        assert b["result"]["reached_limit"]
        t.ok("scroll", tab_id=A, to="top")

    scroll()

    @step("8f screenshot (PNG saved locally)")
    def shot():
        r = t.ok("screenshot", tab_id=A)
        p = Path(r["result"]["path"])
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and r["result"]["width"] > 300
        return f"{r['result']['width']}x{r['result']['height']} -> {p.name}"

    shot()

    @step("8g open a second tab, switch_tab both ways")
    def tabs():
        r = t.ok("new_tab", url=env.fixture("page2.html"))
        st["B"] = r["result"]["tab"]["tab_id"]
        created.append(st["B"])
        assert t.active()["tab_id"] == st["B"]
        t.ok("switch_tab", tab_id=A)
        assert t.active()["tab_id"] == A
        t.ok("switch_tab", tab_id=st["B"])
        assert t.active()["tab_id"] == st["B"]

    tabs()

    @step("8h navigate, back, forward (in the second tab)")
    def history():
        B = st["B"]
        n = t.ok("navigate", tab_id=B, url=env.fixture("index.html"))
        assert n["verification"]["navigated"] and n["result"]["title"] == "OCTG Fixture Home"
        t.ok("navigate", tab_id=B, url=env.fixture("submitted.html?smoke=1"))
        b = t.ok("back", tab_id=B)
        assert b["result"]["final_url"].endswith("/index.html"), b["result"]
        f = t.ok("forward", tab_id=B)
        assert "submitted.html" in f["result"]["final_url"], f["result"]
        return f"(back via {b['result'].get('via')})"

    history()

    @step("12 restricted pages return UNSUPPORTED_PAGE")
    def restricted():
        for url in ("chrome://version/", "about:blank"):
            r = t.ok("new_tab", url=url, active=True)
            tid = r["result"]["tab"]["tab_id"]
            created.append(tid)
            for action, params in (("inspect_page", {}), ("read_page", {}), ("screenshot", {}), ("click", {"x": 10, "y": 10})):
                e = t.fail(action, "UNSUPPORTED_PAGE", tab_id=tid, **params)
                assert e["error"]["message"] == "This Chrome page cannot be controlled by the extension."
            t.ok("close_tab", tab_id=tid)
            created.remove(tid)

    restricted()

    @step("13 no password / sensitive field contents in the logs")
    def logs():
        text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (env.data_dir / "logs").glob("*.jsonl"))
        assert text, "no log files found"
        assert pw not in text, "the password typed during this test was written to a log file"
        assert "[redacted: credential field]" in text, "credential redaction marker missing"
        entries = [json.loads(x) for x in text.splitlines() if x.strip()]
        typed = [e for e in entries if e["action"] == "type" and e["request_id"] in {r['request_id'] for r in t.responses}]
        assert typed
        return f"({len(entries)} log lines scanned in {env.data_dir / 'logs'})"

    logs()

    @step("cleanup: close the tabs the test opened, restore your original active tab")
    def cleanup():
        if args.keep_tabs:
            return "(kept: --keep-tabs)"
        for tid in list(created):
            if any(x["tab_id"] == tid for x in t.ok("get_tabs")["result"]["tabs"]):
                t.ok("close_tab", tab_id=tid)
        t.ok("switch_tab", tab_id=st["initial_active"])
        now = t.ok("get_tabs")["result"]
        assert now["count"] == st["initial_count"], f"tab count {now['count']} != initial {st['initial_count']}"
        assert now["active_tab_id"] == st["initial_active"]

    cleanup()
    env.stop()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} steps passed")
    print("\nMANUAL CHECKS (cannot be automated; Chrome requires a real click):")
    print("  [ ] Click the OCTG toolbar icon -> side panel opens")
    print("  [ ] Badge says 'connected'; Current tab, Current URL and Page title match the tab you are looking at")
    print("  [ ] Switch to a chrome://extensions tab -> panel shows the 'cannot be controlled' warning")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
