"""End-to-end suite: real agent + real Chromium + the real extension, against local fixture pages.

    python -m tests.e2e.run_e2e [--headed] [--only 07,12] [--list]

Each case is tagged with the Phase 1 criteria (1-22) it demonstrates. Results go to tests/e2e/last_run.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from .env import EXT_ID, E2EEnv

CRITERIA = {
    1: "Detect current tab", 2: "Detect all tabs", 3: "Identify active tab", 4: "Read URL/title", 5: "Inspect page",
    6: "Identify interactive elements", 7: "Navigate", 8: "Open tab", 9: "Switch tabs", 10: "Close tab", 11: "Back/forward", 12: "Click",
    13: "Type", 14: "Select", 15: "Scroll", 16: "Screenshot", 17: "Upload local file (no debugger)", 18: "Detect page/navigation changes",
    19: "Verify action results", 20: "Structured responses", 21: "Reconnect after disconnect", 22: "Reject unsupported Chrome pages",
}
TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
RESPONSE_KEYS = {"request_id", "success", "action", "result", "error", "verification", "risk", "duration_ms"}

CASES: list[tuple[str, str, list[int], object]] = []


def case(cid: str, name: str, crit: list[int]):
    def deco(fn):
        CASES.append((cid, name, crit, fn))
        return fn
    return deco


class T:
    """Test driver: thin client over the agent's HTTP API plus page helpers."""

    def __init__(self, env: E2EEnv):
        self.env = env
        self.responses: list[dict] = []
        self.snap: str | None = None
        self.els: list[dict] = []
        self.main: int = 0
        self.tmp = env.tmp / "files"
        self.tmp.mkdir(exist_ok=True)

    # ---- api
    def call(self, action: str, **params) -> dict:
        body = json.dumps({"action": action, "parameters": params}).encode()
        req = urllib.request.Request(self.env.base + "/api/action", data=body, headers={"X-OCTG-Token": self.env.api_token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise AssertionError(f"HTTP {e.code} from the agent for {action}: {e.read()[:300]!r}") from None
        self.responses.append(resp)
        return resp

    def get(self, path: str):
        req = urllib.request.Request(self.env.base + path, headers={"X-OCTG-Token": self.env.api_token})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def ok(self, action: str, **params) -> dict:
        r = self.call(action, **params)
        assert r["success"], f"{action}{params} failed: {json.dumps(r['error'])}"
        return r

    def fail(self, action: str, code: str, **params) -> dict:
        r = self.call(action, **params)
        assert not r["success"], f"{action}{params} should have failed with {code} but succeeded: {json.dumps(r['result'])[:300]}"
        assert r["error"]["code"] == code, f"{action}: expected {code}, got {json.dumps(r['error'])}"
        return r

    # ---- page helpers
    def reset(self) -> None:
        for tb in self.ok("get_tabs")["result"]["tabs"]:
            if tb["tab_id"] != self.main:
                self.ok("close_tab", tab_id=tb["tab_id"])
        self.ok("switch_tab", tab_id=self.main)
        self.ok("navigate", url=self.env.fixture("index.html"), tab_id=self.main)

    def inspect(self, **kw) -> dict:
        r = self.ok("inspect_page", **kw)
        self.snap = r["result"]["snapshot_id"]
        self.els = r["result"]["elements"]
        return r["result"]

    def el(self, name: str, role: str | None = None, frame: int | None = None) -> dict:
        for e in self.els:
            if e["name"] == name and (role is None or e["role"] == role) and (frame is None or e.get("frame", 0) == frame):
                return e
        raise AssertionError(f"element {name!r} (role={role}) not found; have: {[(e['role'], e['name']) for e in self.els]}")

    def act(self, action: str, name: str, role: str | None = None, **params) -> dict:
        e = self.el(name, role, params.pop("frame", None))
        return self.ok(action, element_id=e["id"], snapshot_id=self.snap, **params)

    def act_fail(self, action: str, code: str, name: str, role: str | None = None, **params) -> dict:
        e = self.el(name, role, params.pop("frame", None))
        return self.fail(action, code, element_id=e["id"], snapshot_id=self.snap, **params)

    def text_present(self, text: str) -> bool:
        return self.call("verify", checks=[{"type": "text_present", "value": text}])["success"]

    def page_text(self) -> str:
        return self.ok("read_page", scope="page", max_chars=50000)["result"]["text"]

    def active(self) -> dict:
        return self.ok("get_active_tab")["result"]["tab"]


# ============================================================================ cases
@case("01", "current tab: id, url, title, supported", [1, 4])
def c_current(t: T):
    tab = t.active()
    assert tab["url"].endswith("/index.html"), tab
    assert tab["title"] == "OCTG Fixture Home"
    assert tab["supported"] is True and tab["active"] is True
    assert tab["tab_id"] == t.main


@case("02", "all tabs listed, exactly one active, browser state overview", [2, 3])
def c_tabs(t: T):
    t.reset()
    new = t.ok("new_tab", url=t.env.fixture("page2.html"))["result"]["tab"]
    tabs = t.ok("get_tabs")["result"]
    assert tabs["count"] == 2 and len(tabs["tabs"]) == 2
    active = [x for x in tabs["tabs"] if x["active"]]
    assert len(active) == 1 and active[0]["tab_id"] == new["tab_id"] == tabs["active_tab_id"]
    assert {x["title"] for x in tabs["tabs"]} == {"OCTG Fixture Home", "Fixture Page Two"}
    st = t.ok("get_browser_state")["result"]
    assert st["tab_count"] == 2 and st["current_website"] == "127.0.0.1" and st["active_tab"]["tab_id"] == new["tab_id"]
    agent_view = t.get("/api/state")
    assert agent_view["tab_count"] == 2 and agent_view["active_tab_id"] == new["tab_id"], "agent-side tab model out of sync"
    t.reset()


@case("03", "inspect page: structure, roles, shadow DOM, iframe, hidden file input", [5, 6])
def c_inspect(t: T):
    t.reset()
    r = t.inspect(include_text=True)
    assert r["snapshot_id"].startswith("snap_") and r["title"] == "OCTG Fixture Home"
    assert any(h["text"] == "Fixture Home" for h in r["page"]["headings"])
    assert "Fixture Home" in r["visible_text"]
    roles = {(e["role"], e["name"]) for e in t.els}
    for want in [("textbox", "Full name"), ("combobox", "Country"), ("checkbox", "I agree to the terms"), ("radio", "Pro plan"), ("button", "Click me"),
                 ("link", "Go to page two"), ("textbox", "Rich editor"), ("clickable", "Custom clickable"), ("button", "Shadow button"), ("file", "Pick a document")]:
        assert want in roles, f"missing {want}"
    assert t.el("Shadow button")["tag"] == "button"
    assert t.el("Frame input")["frame"] > 0, "iframe content not inspected"
    hidden = t.el("Choose logos", "file")
    assert hidden["hidden"] is True and hidden["multiple"] is True
    country = t.el("Country")
    assert [o["label"] for o in country["options"]][:3] == ["Choose…", "United States", "India"]
    assert t.el("Full name")["placeholder"] == "Your name"
    assert t.el("I agree to the terms")["checked"] is False
    assert all(e["id"].startswith("el_") for e in t.els) and len({e["id"] for e in t.els}) == len(t.els)
    assert all({"rect", "in_viewport", "tag"} <= set(e) for e in t.els)
    small = t.ok("inspect_page", max_elements=5)["result"]
    assert len(small["elements"]) == 5 and small["truncated"] is True
    vp = t.ok("inspect_page", viewport_only=True)["result"]
    assert 0 < len(vp["elements"]) < r["counts"]["returned"] and all(e["in_viewport"] for e in vp["elements"])
    t.inspect()


@case("04", "read visible text: viewport vs whole page", [5])
def c_read(t: T):
    t.reset()
    vp = t.ok("read_page")["result"]
    assert "Fixture Home" in vp["text"] and "Footer marker text" not in vp["text"]
    full = t.ok("read_page", scope="page")["result"]
    assert "Footer marker text" in full["text"] and "Inner scroll content" in full["text"]
    st = t.ok("get_page_state")["result"]
    assert st["page"]["scroll"]["max_y"] > 1000 and st["page"]["dialogs"] == [] and st["tab"]["tab_id"] == t.main
    assert st["evidence"]["text_sig"]


@case("05", "navigate: load, redirect flag, hash change, failure is reported", [7, 18, 19])
def c_navigate(t: T):
    t.reset()
    r = t.ok("navigate", url=t.env.fixture("page2.html"))
    assert r["result"]["final_url"].endswith("/page2.html") and r["result"]["title"] == "Fixture Page Two" and r["result"]["redirected"] is False
    v = r["verification"]
    assert v["navigated"] is True and v["url"]["changed"] is True and v["observed_change"] is True
    assert {c["name"] for c in v["checks"]} >= {"load_completed", "navigation_observed", "not_error_page"} and all(c["passed"] for c in v["checks"])
    h = t.ok("navigate", url=t.env.fixture("page2.html") + "#frag")
    assert h["result"]["final_url"].endswith("#frag") and h["verification"]["url"]["changed"] is True
    bad = t.fail("navigate", "NAVIGATION_FAILED", url="http://nonexistent-host.invalid/")
    assert "ERR_" in bad["error"]["message"], bad["error"]
    assert t.active()["supported"] is False, "Chrome's error page must be treated as uncontrollable"
    jr = t.call("navigate", url="javascript:alert(1)")
    assert not jr["success"] and jr["error"]["code"] == "INVALID_PARAMS"
    t.reset()


@case("06", "tabs: open, switch (id and match), close, last-tab protection", [8, 9, 10])
def c_tab_ops(t: T):
    t.reset()
    r = t.ok("new_tab", url=t.env.fixture("page2.html"), active=False)
    new = r["result"]["tab"]
    assert new["active"] is False and new["title"] == "Fixture Page Two"
    assert [x["tab_id"] for x in r["verification"]["new_tabs"]] == [new["tab_id"]]
    assert t.active()["tab_id"] == t.main, "a background tab must not steal focus"
    s = t.ok("switch_tab", tab_id=new["tab_id"])
    assert s["result"]["tab"]["active"] is True and s["result"]["window_focused"] is True and t.active()["tab_id"] == new["tab_id"]
    assert s["verification"]["active_tab_changed"] is not None
    t.ok("switch_tab", match={"title_contains": "fixture home"})
    assert t.active()["tab_id"] == t.main
    t.fail("switch_tab", "TAB_NOT_FOUND", match={"title_contains": "no such tab anywhere"})
    c = t.ok("close_tab", tab_id=new["tab_id"])
    assert c["verification"]["closed_tabs"] == [new["tab_id"]] and c["result"]["closed_tab_id"] == new["tab_id"]
    assert new["tab_id"] not in [x["tab_id"] for x in t.ok("get_tabs")["result"]["tabs"]]
    assert new["tab_id"] not in [x["tab_id"] for x in t.get("/api/state")["tabs"]], "agent-side tab model still lists the closed tab"
    t.fail("close_tab", "TAB_NOT_FOUND", tab_id=new["tab_id"])
    t.fail("close_tab", "LAST_TAB", tab_id=t.main)
    assert t.active()["tab_id"] == t.main


@case("07", "back / forward / no history", [11])
def c_history(t: T):
    t.reset()
    t.ok("navigate", url=t.env.fixture("page2.html"))
    b = t.ok("back")
    assert b["result"]["final_url"].endswith("/index.html") and b["verification"]["url"]["changed"] is True
    f = t.ok("forward")
    assert f["result"]["final_url"].endswith("/page2.html")
    rl = t.ok("reload")
    assert rl["result"]["navigation"]["started"] is True and rl["verification"]["navigated"] is True
    new = t.ok("new_tab", url=t.env.fixture("page2.html"))["result"]["tab"]
    t.fail("back", "NO_HISTORY", tab_id=new["tab_id"])
    t.fail("forward", "NO_HISTORY", tab_id=new["tab_id"])
    t.reset()


@case("08", "click: buttons, links, new-tab links, overlays, shadow DOM, custom widgets, coordinates", [12, 18, 19])
def c_click(t: T):
    t.reset()
    t.inspect()
    r = t.act("click", "Click me")
    assert t.text_present("Clicked!")
    v = r["verification"]
    assert v["observed_change"] is True and v["page"]["text_changed"] is True and v["url"]["changed"] is False and r["result"]["element"]["name"] == "Click me"
    t.act("click", "Custom clickable", "clickable")
    assert t.text_present("Custom clicked"), "cursor:pointer heuristic element not clickable"
    t.act("click", "Shadow button")
    assert t.text_present("Shadow clicked")
    t.act("click", "Frame button", frame=t.el("Frame button")["frame"])
    cov = t.act_fail("click", "ELEMENT_NOT_INTERACTABLE", "Covered button")
    assert cov["error"]["details"]["obscured_by"]["tag"] == "div", cov["error"]
    assert not t.text_present("Covered clicked")
    t.act("click", "Covered button", force=True)
    assert t.text_present("Covered clicked")
    t.act_fail("click", "ELEMENT_NOT_INTERACTABLE", "Pick a document", "file")
    t.ok("scroll", to="top")
    t.inspect()
    e = t.el("Click me")["rect"]
    t.ok("click", x=e["x"] + e["w"] // 2, y=e["y"] + e["h"] // 2)
    assert t.text_present("Clicked!"), "coordinate click did not reach the button"
    bad = t.call("click", x=5000, y=5)
    assert not bad["success"] and bad["error"]["code"] == "INVALID_PARAMS"
    nl = t.act("click", "Open page two in a new tab")
    assert nl["result"]["opened_tab"]["url"].endswith("page2.html?from=blank"), nl["result"]
    assert len(nl["verification"]["new_tabs"]) == 1, nl["verification"]
    assert t.active()["tab_id"] == nl["result"]["opened_tab"]["tab_id"], "the new tab should be in front"
    t.ok("switch_tab", tab_id=t.main)
    t.inspect()
    go = t.act("click", "Go to page two")
    assert go["verification"]["navigated"] is True and go["verification"]["url"]["after"].endswith("/page2.html"), go["verification"]
    assert go["verification"]["title"]["after"] == "Fixture Page Two", go["verification"]["title"]
    t.reset()
    t.inspect()
    nv = t.act("click", "Navigate via script")
    assert nv["verification"]["navigated"] is True, nv["verification"]
    t.reset()


@case("09", "type: replace/append, keys mode, textarea, contenteditable, password masking, iframe", [13, 19])
def c_type(t: T):
    t.reset()
    t.inspect()
    r = t.act("type", "Full name", text="Ada Lovelace")
    assert r["result"]["value_matches"] is True and r["result"]["value_after"] == "Ada Lovelace"
    assert any(c["name"] == "value_matches" and c["passed"] for c in r["verification"]["checks"])
    r = t.act("type", "Full name", text=" Byron", clear=False)
    assert r["result"]["value_after"] == "Ada Lovelace Byron"
    r = t.act("type", "Full name", text="Replaced")
    assert r["result"]["value_after"] == "Replaced"
    pw = t.act("type", "Password", text="S3cret-Value!")
    assert "S3cret" not in json.dumps(pw), "typed password must never be echoed"
    assert pw["result"]["value_after"].startswith("[hidden") and "credential_field" in pw["risk"]["tags"] and pw["risk"]["level"] == "high"
    cc = t.act("type", "Card number", text="4111111111111111")
    assert "4111111111111111" not in json.dumps(cc["result"]) and cc["result"]["value_after"].startswith("[hidden") and "credential_field" in cc["risk"]["tags"], cc
    t.act("type", "Email", text="ada@example.test")
    t.act("type", "Notes", text="line one\nline two")
    t.act("type", "Rich editor", text="Hello rich text")
    assert "Hello rich text" in t.page_text()
    t.act("type", "Key box", text="xyz", mode="keys")
    assert t.text_present("Key: z")
    t.act("clear", "Full name")
    t.inspect()
    assert t.el("Full name")["value"] == ""
    assert t.el("Password")["value"] == "[hidden]", "password value must be masked in inspections"
    assert t.el("Card number")["value"] == "[hidden]" and t.el("Card number")["sensitive"] is True, "card-like fields must be masked too"
    t.act_fail("type", "NOT_EDITABLE", "Click me", text="nope")
    t.act("type", "Frame input", text="framed", frame=t.el("Frame input")["frame"])
    t.inspect()
    assert t.el("Frame input")["value"] == "framed"


@case("10", "select / check / uncheck / radio", [14, 19])
def c_select(t: T):
    t.reset()
    t.inspect()
    r = t.act("select", "Country", option="India")
    assert r["result"]["selected"][0]["value"] == "in" and r["result"]["value_matches"] is True
    assert t.act("select", "Country", option="de")["result"]["selected"][0]["label"] == "Germany"
    assert t.act("select", "Country", option=1)["result"]["selected"][0]["value"] == "us"
    nf = t.act_fail("select", "OPTION_NOT_FOUND", "Country", option="Narnia")
    assert any(o["value"] == "in" for o in nf["error"]["details"]["available"])
    t.act_fail("select", "NOT_A_SELECT", "Full name", option="x")
    c = t.act("check", "I agree to the terms")
    assert c["result"]["checked"] is True and c["result"]["was"] is False
    assert t.call("verify", checks=[{"type": "element_checked", "element_id": t.el("I agree to the terms")["id"], "snapshot_id": t.snap, "equals": True}])["success"]
    assert t.act("check", "I agree to the terms")["result"]["was"] is True  # idempotent
    assert t.act("uncheck", "I agree to the terms")["result"]["checked"] is False
    assert t.act("check", "Pro plan")["result"]["checked"] is True
    t.act_fail("uncheck", "NOT_CHECKABLE", "Pro plan")
    t.act_fail("check", "NOT_CHECKABLE", "Full name")
    t.inspect()
    assert t.el("Country")["value"] == "us" and t.el("Pro plan")["checked"] is True


@case("11", "scroll: by amount, to bottom/top, limits, element into view", [15, 19])
def c_scroll(t: T):
    t.reset()
    t.inspect()
    r = t.ok("scroll", direction="down")
    assert r["result"]["moved"] is True and r["result"]["after"]["y"] > 300 and r["verification"]["page"]["scroll_changed"] is True
    r = t.ok("scroll", direction="down", amount=100)
    assert r["result"]["after"]["y"] - r["result"]["before"]["y"] == 100
    b = t.ok("scroll", to="bottom")
    assert b["result"]["reached_limit"] is True and b["result"]["after"]["y"] == b["result"]["max"]["y"]
    assert "Footer marker text" in t.ok("read_page")["result"]["text"]
    end = t.ok("scroll", direction="down")
    assert end["result"]["moved"] is False and end["result"]["reached_limit"] is True and end["verification"]["page"]["scroll_changed"] is False
    t.ok("scroll", to="top")
    assert t.ok("get_page_state")["result"]["page"]["scroll"]["y"] == 0
    iv = t.act("scroll", "Open dialog")
    assert iv["result"]["mode"] == "into_view"
    assert t.call("scroll")["error"]["code"] == "INVALID_PARAMS"


@case("12", "screenshot: PNG/JPEG saved locally, background tab activated", [16])
def c_screenshot(t: T):
    t.reset()
    r = t.ok("screenshot")
    p = Path(r["result"]["path"])
    assert p.is_file() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and r["result"]["width"] >= 400 and r["result"]["height"] >= 300 and p.stat().st_size > 2000
    assert "data_url" not in r["result"] and r["result"]["scope"] == "visible_viewport"
    j = t.ok("screenshot", format="jpeg", quality=60)
    assert Path(j["result"]["path"]).read_bytes()[:3] == b"\xff\xd8\xff"
    other = t.ok("new_tab", url=t.env.fixture("page2.html"), active=False)["result"]["tab"]
    bg = t.ok("screenshot", tab_id=other["tab_id"])
    assert bg["result"]["activated_tab"] is True and bg["result"]["tab_id"] == other["tab_id"]
    t.reset()


@case("13", "upload from authorized folder via file input (no debugger)", [17, 19])
def c_upload(t: T):
    from octg_agent.files.authorization import AuthorizedFolders
    t.reset()
    up = t.tmp / "uploads"
    up.mkdir(exist_ok=True)
    (up / "logo.png").write_bytes(TINY_PNG)
    (up / "a.txt").write_text("alpha")
    (up / "b.txt").write_text("bravo!")
    secret = t.tmp / "secret.txt"
    secret.write_text("not for upload")
    t.inspect()
    t.act_fail("upload_file", "FILE_NOT_AUTHORIZED", "Pick a document", "file", local_file_path=str(up / "logo.png"))
    AuthorizedFolders(t.env.data_dir / "authorized_folders.json").allow(str(up))  # what `octg_agent fs allow` does
    r = t.act("upload_file", "Pick a document", "file", local_file_path=str(up / "logo.png"))
    assert r["result"]["files"] == [{"name": "logo.png", "size": len(TINY_PNG), "type": "image/png"}] and r["result"]["value_matches"] is True
    assert r["risk"]["level"] == "high" and r["risk"]["approval"] == "required"
    assert r["result"]["local_files"][0]["sha256"]
    assert t.text_present(f"Files: logo.png ({len(TINY_PNG)})"), "the page's own change handler did not see the file"
    m = t.act("upload_file", "Choose logos", "file", local_file_paths=[str(up / "a.txt"), str(up / "b.txt")])
    assert [f["name"] for f in m["result"]["files"]] == ["a.txt", "b.txt"]
    assert t.text_present("Files: a.txt (5), b.txt (6)")
    t.act_fail("upload_file", "INVALID_PARAMS", "Pick a document", "file", local_file_paths=[str(up / "a.txt"), str(up / "b.txt")])
    t.act_fail("upload_file", "FILE_NOT_AUTHORIZED", "Pick a document", "file", local_file_path=str(secret))
    t.act_fail("upload_file", "FILE_NOT_AUTHORIZED", "Pick a document", "file", local_file_path=str(up / ".." / "secret.txt"))
    t.act_fail("upload_file", "NOT_A_FILE_INPUT", "Full name", local_file_path=str(up / "a.txt"))
    t.act_fail("upload_file", "FILE_NOT_FOUND", "Pick a document", "file", local_file_path=str(up / "missing.txt"))


@case("14", "detect page/navigation changes: events, SPA route, dialogs, downloads", [18, 19])
def c_changes(t: T):
    t.reset()
    mark = t.get("/api/state")["event_seq"]
    t.inspect()
    spa = t.act("click", "SPA route")
    v = spa["verification"]
    assert v["url"]["changed"] is True and v["url"]["after"].endswith("#/spa") and v["navigated"] is False and v["page"]["text_changed"] is True
    d = t.act("click", "Open dialog")
    assert d["verification"]["page"]["dialogs_opened"] and "Are you sure" in d["verification"]["page"]["dialogs_opened"][0]
    assert t.ok("get_page_state")["result"]["page"]["dialogs"][0]["role"] == "dialog"
    t.ok("new_tab", url=t.env.fixture("page2.html"), active=True)
    dl = t.ok("download_file", url=t.env.fixture("sample.txt"))
    assert dl["result"]["state"] == "complete" and dl["result"]["bytes"] > 10 and Path(dl["result"]["filename"]).is_file()
    assert t.ok("list_downloads")["result"]["downloads"][0]["download_id"] == dl["result"]["download_id"]
    t.reset()
    names = {e["event"] for e in t.get(f"/api/events?since={mark}&limit=500")["events"]}
    assert names & {"navigation_spa", "navigation_fragment"}, "same-document navigation not reported"
    for want in ("tab_created", "tab_removed", "navigation_committed", "navigation_completed", "download_created", "download_changed", "tab_activated"):
        assert want in names, f"agent never saw event {want}; saw {sorted(names)}"
    t.fail("download_file", "INVALID_PARAMS", url="file:///C:/Windows/win.ini")
    t.fail("download_file", "INVALID_PARAMS", url=t.env.fixture("sample.txt"), filename="..\\..\\evil.txt")


@case("15", "verify + wait: pass, fail, timeout, polling", [19])
def c_verify(t: T):
    t.reset()
    t.inspect()
    ok = t.ok("verify", checks=[{"type": "url_contains", "value": "index.html"}, {"type": "title_contains", "value": "fixture home"},
                                {"type": "text_present", "value": "Footer marker text"}, {"type": "text_absent", "value": "Never shown"},
                                {"type": "element_exists", "role": "button", "name_contains": "click me"}, {"type": "element_absent", "role": "button", "name_contains": "zzz"},
                                {"type": "tab_count", "equals": 1}, {"type": "tab_active"}, {"type": "url_regex", "value": r"index\.html$"}])
    assert ok["result"]["all_passed"] is True and len(ok["result"]["checks"]) == 9
    bad = t.call("verify", checks=[{"type": "url_contains", "value": "index.html"}, {"type": "text_present", "value": "Absolutely not here"}])
    assert bad["success"] is False and bad["error"]["code"] == "POSTCONDITION_FAILED" and bad["result"]["all_passed"] is False
    assert [c["passed"] for c in bad["result"]["checks"]] == [True, False]
    t.act("click", "Show delayed text")
    w = t.ok("wait", until=[{"type": "text_present", "value": "Delayed text arrived"}], timeout_ms=6000, poll_ms=100)
    assert w["result"]["satisfied"] is True and w["result"]["waited_ms"] >= 300
    to = t.call("wait", until=[{"type": "text_present", "value": "Never appears"}], timeout_ms=800)
    assert to["success"] is False and to["result"]["satisfied"] is False and to["error"]["code"] == "POSTCONDITION_FAILED"
    assert t.ok("wait", ms=250)["result"]["waited_ms"] == 250
    t.fail("verify", "INVALID_PARAMS", checks=[{"type": "mind_reading"}])


@case("16", "STALE_ELEMENT: superseded, detached, changed, document replaced, unknown id", [5, 19])
def c_stale(t: T):
    t.reset()
    a = t.inspect()["snapshot_id"]
    old = {e["name"]: e["id"] for e in t.els}
    b = t.inspect()["snapshot_id"]
    assert a != b
    r = t.fail("click", "STALE_ELEMENT", element_id=old["Click me"], snapshot_id=a)
    assert r["error"]["details"]["reason"] == "snapshot_superseded" and r["error"]["details"]["current_snapshot_id"] == b
    assert not t.text_present("Clicked!"), "a stale click must not touch the page"
    t.fail("click", "INVALID_PARAMS", element_id=old["Click me"])
    t.fail("click", "ELEMENT_NOT_FOUND", element_id="el_999", snapshot_id=b)
    t.act("click", "Vanish")
    r = t.act_fail("click", "STALE_ELEMENT", "Vanish")
    assert r["error"]["details"]["reason"] == "detached"
    t.inspect()
    t.act("click", "Rename me")
    r = t.act_fail("click", "STALE_ELEMENT", "Rename me")
    assert r["error"]["details"]["reason"] == "changed"
    e = t.inspect()["snapshot_id"]
    victim = t.el("Click me")["id"]
    t.ok("navigate", url=t.env.fixture("page2.html"))
    t.ok("navigate", url=t.env.fixture("index.html"))
    r = t.fail("click", "STALE_ELEMENT", element_id=victim, snapshot_id=e)
    assert r["error"]["details"]["reason"] == "document_changed"
    assert not t.text_present("Clicked!")


@case("17", "press_key: Enter submits, Tab moves focus, typing keys, risk tag", [12, 13, 18])
def c_keys(t: T):
    t.reset()
    t.inspect()
    t.act("type", "Key box", text="")
    k = t.act("press_key", "Key box", key="a")
    assert k["result"]["default_action"] == "insert_text" and t.text_present("Key: a")
    t.act("press_key", "Key box", key="b")
    t.act("press_key", "Key box", key="Backspace")
    t.inspect()
    assert t.el("Key box")["value"] == "a"
    tab = t.act("press_key", "Key box", key="Tab")
    assert tab["result"]["default_action"] == "focus_moved" and tab["verification"]["page"]["focus_changed"] is True
    t.act("press_key", "Key box", key="Escape")
    t.act("type", "Full name", text="Zed Enterkey")
    enter = t.act("press_key", "Full name", key="Enter")
    assert enter["result"]["default_action"] == "form_submit" and "may_submit_form" in enter["risk"]["tags"]
    assert enter["verification"]["navigated"] is True and "submitted.html" in enter["verification"]["url"]["after"] and "name=Zed+Enterkey" in enter["verification"]["url"]["after"]
    t.reset()


@case("18", "risk tags attached to responses (not enforced in Phase 1)", [20])
def c_risk(t: T):
    t.reset()
    t.inspect()
    d = t.act("click", "Delete account")
    assert d["success"] is True, "Phase 1 only tags; it must not block"
    assert d["risk"]["level"] == "high" and d["risk"]["approval"] == "required" and "potentially_irreversible" in d["risk"]["tags"] and d["risk"]["approval_enforced"] is False
    assert t.act("click", "Click me")["risk"]["level"] == "medium"
    t.inspect()
    assert t.responses[-1]["risk"]["level"] == "none"
    s = t.act("click", "Send form")
    assert s["risk"]["level"] == "high" and "form_submit" in s["risk"]["tags"]
    t.reset()


@case("19", "unsupported Chrome pages are rejected, never reported as success", [22])
def c_unsupported(t: T):
    t.reset()
    msg = "This Chrome page cannot be controlled by the extension."
    for url in ("chrome://version/", "about:blank", "chrome-extension://" + EXT_ID + "/sidepanel/panel.html"):
        tab = t.ok("new_tab", url=url, active=True)["result"]["tab"]
        listed = next(x for x in t.ok("get_tabs")["result"]["tabs"] if x["tab_id"] == tab["tab_id"])
        assert listed["supported"] is False and listed["restricted_reason"], listed
        for action, params in [("inspect_page", {}), ("get_page_state", {}), ("read_page", {}), ("screenshot", {}), ("click", {"x": 10, "y": 10}),
                               ("scroll", {"direction": "down"}), ("press_key", {"key": "a"}),
                               ("type", {"element_id": "el_001", "snapshot_id": "snap_x", "text": "x"})]:
            r = t.fail(action, "UNSUPPORTED_PAGE", **params)
            assert r["success"] is False and r["error"]["message"] == msg and r["result"] is None, (url, action, r)
        assert t.ok("verify", checks=[{"type": "url_contains", "value": url.split(":")[0]}])["success"], "tab-level verify still works"
        t.ok("close_tab", tab_id=tab["tab_id"])
    t.reset()
    t.call("navigate", url="http://nonexistent-host.invalid/")
    t.fail("inspect_page", "UNSUPPORTED_PAGE")
    t.ok("navigate", url=t.env.fixture("page2.html"))  # navigating away from a restricted page is allowed and recovers
    assert t.ok("inspect_page")["success"]
    t.reset()


@case("20", "side panel shows live status, task and restricted-page warning", [20])
def c_panel(t: T):
    t.reset()
    t.ok("set_task", task="Phase 1 panel check")
    panel = t.ok("new_tab", url=f"chrome-extension://{EXT_ID}/sidepanel/panel.html", active=False)["result"]["tab"]
    tgt = next(x for x in t.env.targets() if x["url"].endswith("sidepanel/panel.html") and x["type"] == "page")
    js = ("JSON.stringify({badge: badge.textContent, agent: agentStatus.textContent, browser: browserStatus.textContent, site: website.textContent, "
          "url: url.textContent, title: title.textContent, task: task.textContent, action: action.textContent, log: log.innerText, warnHidden: restricted.hidden, warn: restricted.textContent})")
    time.sleep(1.0)
    s = json.loads(t.env.cdp_eval(tgt, js))
    assert s["badge"] == "connected" and s["agent"] == "Connected" and s["site"] == "127.0.0.1", s
    assert "index.html" in s["url"] and s["title"] == "OCTG Fixture Home" and s["task"] == "Phase 1 panel check" and s["warnHidden"] is True, s
    assert "tab" in s["browser"] and ("✔" in s["log"] or "▶" in s["log"]), s
    t.ok("new_tab", url="chrome://version/", active=True)
    time.sleep(1.0)
    s = json.loads(t.env.cdp_eval(tgt, js))
    assert s["warnHidden"] is False and "cannot be controlled by the extension" in s["warn"], s
    t.ok("set_task", task=None)
    t.reset()


@case("21", "downloaded file can be saved only into an authorized folder", [17])
def c_save_download(t: T):
    from octg_agent.files.authorization import AuthorizedFolders
    t.reset()
    dest = t.tmp / "client_assets"
    dest.mkdir(exist_ok=True)
    dl = t.ok("download_file", url=t.env.fixture("sample.txt"), filename="octg_e2e/sample_%d.txt" % int(time.time()))
    t.fail("fs_save_download", "FILE_NOT_AUTHORIZED", download_id=dl["result"]["download_id"], dest_dir=str(dest))
    AuthorizedFolders(t.env.data_dir / "authorized_folders.json").allow(str(dest))
    s = t.ok("fs_save_download", download_id=dl["result"]["download_id"], dest_dir=str(dest), filename="saved.txt")
    assert (dest / "saved.txt").read_text().startswith("OCTG fixture download payload")
    t.fail("fs_save_download", "FILE_EXISTS", download_id=dl["result"]["download_id"], dest_dir=str(dest), filename="saved.txt")
    found = t.ok("fs_find", pattern="*.txt")["result"]
    assert any(f["path"].endswith("saved.txt") for f in found["files"]) and all(str(t.tmp) in f["path"] for f in found["files"])
    roots = t.ok("fs_list_roots")["result"]["roots"]
    assert len(roots) >= 2


@case("22", "reconnect: agent restart is survived without touching the browser", [21])
def c_reconnect(t: T):
    t.reset()
    before = t.ok("ping")["result"]
    t.env.stop_agent()
    time.sleep(1.0)
    t0 = time.time()
    t.env.start_agent()
    t.env.wait_connected(40)
    took = time.time() - t0
    after = t.ok("ping")["result"]
    assert after["extension_id"] == before["extension_id"]
    st = t.get("/api/state")
    assert st["connected"] is True and st["tab_count"] >= 1, "tab model not rebuilt after reconnect"
    tab = t.active()
    assert tab["tab_id"] == t.main
    r = t.inspect()
    assert r["snapshot_id"]
    print(f"      (reconnected in {took:.1f}s)")


SLOW = {"24", "25"}  # run with --resilience


@case("24", "MV3 worker idle: connection survives 75 s with no commands", [21])
def c_idle(t: T):
    t.reset()
    sw = t.env.extension_worker()
    t.env.cdp_eval(sw, "globalThis.__octg_marker = 'same-worker'; 1")
    before = t.get("/api/state")["reconnects"]
    time.sleep(75)
    p = t.ok("ping")
    assert t.get("/api/state")["connected"] is True
    sw2 = t.env.extension_worker()
    alive = t.env.cdp_eval(sw2, "globalThis.__octg_marker === 'same-worker'")
    t.ok("get_tabs")
    t.inspect()
    print(f"      (worker {'stayed alive' if alive else 'was restarted and recovered'}; reconnects {before} -> {t.get('/api/state')['reconnects']})")


@case("25", "MV3 worker killed: it wakes itself and reconnects", [21])
def c_kill_worker(t: T):
    t.reset()
    sw = t.env.extension_worker()
    assert t.env.browser_cdp("Target.closeTarget", {"targetId": sw["id"]})["result"]["success"] is True
    t0 = time.time()
    time.sleep(1.5)
    assert t.get("/api/state")["connected"] is False, "worker kill had no effect"
    t.fail("ping", "EXTENSION_NOT_CONNECTED")
    t.env.wait_connected(60)
    took = time.time() - t0
    assert took < 50, f"recovery took {took:.0f}s"
    t.ok("ping")
    t.ok("get_tabs")
    t.inspect()
    t.act("click", "Click me")
    assert t.text_present("Clicked!")
    print(f"      (recovered {took:.0f}s after the worker was killed)")


@case("26", "find_text: jump to text near the bottom of a long page; context, element ids, hidden content, secrets, frames; stale rules intact", [5, 15, 19])
def c_find_text(t: T):
    t.reset()
    url = t.env.fixture("long.html")
    t.ok("navigate", url=url, tab_id=t.main)
    sc = t.ok("get_page_state")["result"]["page"]["scroll"]
    assert sc["y"] == 0 and sc["max_y"] > 40000, f"the fixture must be a genuinely long page: {sc}"
    assert "Thursday" not in t.ok("read_page")["result"]["text"], "the target is not on screen to begin with"
    scrolls_before = sum(1 for r in t.responses if r["action"] == "scroll")

    # 1-5. one call reaches content ~50,000 px down; no blind scrolling; the match is on screen; the surrounding content can then be read
    r = t.ok("find_text", text="Thursday at 03:00 UTC")
    res, f = r["result"], r["result"]["focused"]
    assert res["found"] is True and res["total_matches"] == 1 and res["hidden_matches"] == 0 and res["frames_searched"] >= 2, res
    assert f["in_viewport"] is True and f["scrolled"] is True and f["match_index"] == 0 and f["nearest_heading"] == "Maintenance window", f
    assert (f["match_type"], f["is_heading"], f["is_link"], f["element_tag"], f["visibility"]) == ("body_text", False, False, "p", "in_view"), f
    assert "quarterly maintenance window is [[Thursday at 03:00 UTC]] for the EU cluster" in f["context"], f["context"]
    assert res["scroll"]["before"]["y"] == 0 and 45000 < res["scroll"]["after"]["y"] <= res["scroll"]["max_y"], res["scroll"]
    assert r["verification"]["page"]["scroll_changed"] is True and any(c["name"] == "match_in_view" and c["passed"] for c in r["verification"]["checks"]), r["verification"]
    now = t.ok("get_page_state")["result"]["page"]["scroll"]["y"]
    assert abs(now - res["scroll"]["after"]["y"]) <= 1, (now, res["scroll"])
    around = t.ok("read_page")["result"]["text"]
    assert "quarterly maintenance window is Thursday at 03:00 UTC for the EU cluster" in around and "Acknowledge maintenance window" in around, around[:300]
    assert sum(1 for r2 in t.responses if r2["action"] == "scroll") == scrolls_before, "no scroll actions were needed"

    # matching rules: case-insensitive by default, optional case sensitivity, whole words, text split across inline elements
    assert t.ok("find_text", text="THURSDAY at 03:00 utc")["result"]["found"] is True
    assert t.ok("find_text", text="THURSDAY", case_sensitive=True)["result"]["found"] is False
    assert t.ok("find_text", text="Thursday", case_sensitive=True)["result"]["found"] is True
    assert t.ok("find_text", text="name-alpha")["result"]["total_matches"] == 2
    assert t.ok("find_text", text="NAME-alpha", case_sensitive=True)["result"]["total_matches"] == 1
    assert t.ok("find_text", text="cat", scroll=False)["result"]["total_matches"] == 2
    assert t.ok("find_text", text="cat", whole_word=True, scroll=False)["result"]["total_matches"] == 1
    split = t.ok("find_text", text="emergency  rollback")["result"]  # spans <b> and <i>; whitespace is normalised
    assert split["found"] and "[[emergency rollback]]" in split["focused"]["context"], split

    # several matches: ordered, described, and index moves between them
    m = t.ok("find_text", text="maintenance window", scroll=False)
    assert m["result"]["total_matches"] == 4 and m["result"]["focused"] is None and m["verification"]["page"]["scroll_changed"] is False, m["result"]
    ms = m["result"]["matches"]
    assert [x["match_index"] for x in ms] == [0, 1, 2, 3], "document order, nothing ranked: the navigation link comes first even though a real heading exists"
    # semantic metadata lets the CALLER choose: a phrase in a navigation link, an H2 section heading, a paragraph and a button
    assert [x["match_type"] for x in ms] == ["link", "heading", "body_text", "other"], [(x["match_type"], x["element_tag"]) for x in ms]
    assert [x["element_tag"] for x in ms] == ["a", "h2", "p", "button"], [x["element_tag"] for x in ms]
    assert [x["is_link"] for x in ms] == [True, False, False, False] and [x["is_heading"] for x in ms] == [False, True, False, False], ms
    assert ms[1]["heading_level"] == 2 and "heading_level" not in ms[0] and "heading_level" not in ms[2], ms
    assert [x["role"] for x in ms] == ["link", "heading", None, "button"], [x["role"] for x in ms]
    assert ms[2]["nearest_heading"] == "Maintenance window" and ms[0]["nearest_heading"] is not None
    # the page currently sits at the "emergency rollback" match, in the middle: the contents link is above the viewport, the section is below it
    assert ms[0]["visibility"] == "above" and {x["visibility"] for x in ms[1:]} == {"below"}, [x["visibility"] for x in ms]
    assert all(x["in_viewport"] is (x["visibility"] == "in_view") for x in ms)
    third = t.ok("find_text", text="maintenance window", index=2)["result"]
    assert third["focused"]["match_index"] == 2 and "quarterly" in third["focused"]["context"] and third["focused"]["in_viewport"], third["focused"]
    h2 = t.ok("find_text", text="maintenance window", index=1)["result"]  # the model picks the heading: index selects exactly that occurrence
    assert h2["focused"]["match_index"] == 1 and h2["focused"]["is_heading"] is True and h2["focused"]["heading_level"] == 2 and h2["focused"]["in_viewport"], h2["focused"]
    assert [x["match_index"] for x in h2["matches"]] == [0, 1, 2, 3], "choosing an index does not reorder matches"
    link = t.ok("find_text", text="maintenance window", index=0)["result"]["focused"]
    assert link["is_link"] is True and link["match_type"] == "link" and link["role"] == "link" and link["visibility"] == "in_view", link
    over = t.ok("find_text", text="maintenance window", index=9)["result"]
    assert over["focused"] is None and over["total_matches"] == 4 and any("out of range" in n for n in over["notes"]), over

    # not found / hidden / secrets: a negative answer is a normal result, never an error; hidden text is reported but never scrolled to
    before_y = t.ok("get_page_state")["result"]["page"]["scroll"]["y"]
    nf = t.ok("find_text", text="this phrase is nowhere on the page")
    assert nf["result"]["found"] is False and nf["result"]["total_matches"] == 0 and nf["result"]["hidden_matches"] == 0 and nf["result"]["focused"] is None
    assert nf["verification"]["page"]["scroll_changed"] is False
    for hidden in ("hidden-only phrase", "inside-details phrase"):
        h = t.ok("find_text", text=hidden)["result"]
        assert h["found"] is False and h["hidden_matches"] == 1 and any("hidden" in n for n in h["notes"]), (hidden, h)
    for secret in ("s3cr3t-pass-value", "textarea secret content", "typed visible input value", "option-label-text"):
        s = t.ok("find_text", text=secret)["result"]
        assert s["found"] is False and s["hidden_matches"] == 0 and s["matches"] == [], (secret, s)
    assert t.ok("get_page_state")["result"]["page"]["scroll"]["y"] == before_y, "failed searches must not move the page"

    # shadow DOM, a scrolling container, and an iframe (matches in frames are found and brought into view)
    sh = t.ok("find_text", text="shadow-only phrase")["result"]
    assert sh["found"] and sh["focused"]["in_viewport"], sh
    assert not t.text_present("Box scrolled")
    bx = t.ok("find_text", text="deep-in-box marker")["result"]
    assert bx["found"] and bx["focused"]["in_viewport"], bx
    assert t.ok("wait", until=[{"type": "text_present", "value": "Box scrolled"}], timeout_ms=3000)["result"]["satisfied"], "the scroll container itself was scrolled"
    fr = t.ok("find_text", text="inside-frame marker")
    assert fr["result"]["found"] and fr["result"]["focused"]["frame"] > 0 and fr["result"]["focused"]["in_viewport"], fr["result"]
    assert t.ok("get_page_state")["result"]["page"]["scroll"]["y"] > 8000, "the iframe itself was brought into view in the parent page"

    # existing stale-element behaviour and verification are untouched: ids are only returned for the CURRENT snapshot, find_text never replaces it
    t.ok("navigate", url=url)
    assert not any("element_id" in x for x in t.ok("find_text", text="maintenance window", scroll=False)["result"]["matches"]), "no snapshot -> no element ids"
    snap1 = t.inspect()["snapshot_id"]
    r = t.ok("find_text", text="maintenance window", index=3)["result"]
    ids = {x["match_index"]: x for x in r["matches"]}
    assert r["focused"]["snapshot_id"] == snap1 and r["focused"]["element_id"], r["focused"]
    assert ids[0]["snapshot_id"] == snap1 and "element_id" not in ids[1] and "element_id" not in ids[2], ids
    assert t.els and next(e for e in t.els if e["id"] == r["focused"]["element_id"])["name"] == "Acknowledge maintenance window"
    assert t.ok("get_browser_state")["result"]["snapshot"]["snapshot_id"] == snap1, "find_text must not create or replace snapshots"
    t.ok("click", element_id=r["focused"]["element_id"], snapshot_id=snap1)  # the id works because it belongs to the current snapshot
    assert t.text_present("Acknowledged")
    t.ok("find_text", text="Overview")  # moves the page; earlier ids stay valid (scrolling does not change the document)
    t.ok("click", element_id=ids[0]["element_id"], snapshot_id=snap1)
    snap2 = t.inspect()["snapshot_id"]
    assert snap2 != snap1
    stale = t.fail("click", "STALE_ELEMENT", element_id=r["focused"]["element_id"], snapshot_id=snap1)
    assert stale["error"]["details"]["reason"] == "snapshot_superseded" and stale["error"]["details"]["current_snapshot_id"] == snap2
    again = t.ok("find_text", text="maintenance window", index=3)["result"]
    assert again["focused"]["snapshot_id"] == snap2 and again["focused"]["element_id"]
    t.ok("navigate", url=t.env.fixture("page2.html"))
    t.ok("navigate", url=url)
    gone = t.fail("click", "STALE_ELEMENT", element_id=again["focused"]["element_id"], snapshot_id=snap2)
    assert gone["error"]["details"]["reason"] == "document_changed"

    # explicit tab_id on a background tab, validation, restricted pages
    other = t.ok("new_tab", url=url, active=False)["result"]["tab"]["tab_id"]
    bg = t.ok("find_text", text="End of handbook marker", tab_id=other)
    assert bg["result"]["found"] and bg["result"]["focused"]["in_viewport"] and t.active()["tab_id"] == t.main, "explicit tab_id works without stealing focus"
    t.ok("close_tab", tab_id=other)
    t.fail("find_text", "INVALID_PARAMS", text="   ")
    t.fail("find_text", "INVALID_PARAMS", text="x", index=-1)
    t.fail("find_text", "INVALID_PARAMS", text="x", selector="#nope")
    tab = t.ok("new_tab", url="chrome://version/", active=True)["result"]["tab"]
    r = t.fail("find_text", "UNSUPPORTED_PAGE", text="Chrome")
    assert r["result"] is None and r["error"]["message"] == "This Chrome page cannot be controlled by the extension."
    t.ok("close_tab", tab_id=tab["tab_id"])
    assert t.ok("verify", checks=[{"type": "url_contains", "value": "long.html"}, {"type": "title_contains", "value": "long fixture"}])["success"]
    t.reset()


@case("23", "every response is structured; logs have required fields and no secrets", [20])
def c_structured(t: T):
    assert len(t.responses) > 100
    ids = set()
    for r in t.responses:
        assert RESPONSE_KEYS <= set(r), r
        assert isinstance(r["success"], bool) and isinstance(r["request_id"], str) and r["request_id"] not in ids
        ids.add(r["request_id"])
        if r["success"]:
            assert r["error"] is None, r
        else:
            assert r["error"]["code"] and r["error"]["message"], r
        assert r["risk"] is None or {"level", "tags", "approval"} <= set(r["risk"])
    entries = t.get("/api/logs?tail=200")["entries"]
    assert entries
    for e in entries:
        assert {"ts", "request_id", "action", "parameters", "success", "result", "error", "duration_ms"} <= set(e), e
    text = "".join(p.read_text(encoding="utf-8") for p in t.env.data_dir.joinpath("logs").glob("*.jsonl"))
    assert "iVBORw0KGgo" not in text and "data_b64" not in text.replace('"data_b64": "[redacted]"', ""), "uploaded file bytes leaked into the log"
    assert "S3cret" not in text, "a password typed into a credential field leaked into the log"
    assert "4111111111111111" not in text, "a card number typed into a sensitive field leaked into the log"
    for page_content in ("Inner scroll content", "Tall filler content", "You arrived at the second page", "Are you sure about this",
                         "for the EU cluster", "Routine paragraph", "Release code"):  # only ever present in page content, never in a caller parameter
        assert page_content not in text, f"page content {page_content!r} was written to the log"
    types = {e["action"] for e in entries}
    assert {"click", "type", "inspect_page"} <= types


# ============================================================================ runner
def main() -> int:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--real-chrome", action="store_true", help="use the installed Google Chrome (fresh temporary profile, extension loaded via CDP pipe) instead of Chromium")
    ap.add_argument("--only", help="comma separated case ids, e.g. 03,08")
    ap.add_argument("--resilience", action="store_true", help="also run the slow MV3 service-worker idle/kill cases (24, 25)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--chrome-arg", action="append", default=[], help="extra Chrome flag (repeatable), e.g. for diagnosing window-occlusion effects")
    args = ap.parse_args()
    if args.list:
        for cid, name, crit, _ in CASES:
            print(f"{cid}  {name}  {crit}")
        return 0
    only = set(args.only.split(",")) if args.only else None
    env = E2EEnv(headed=args.headed, real_chrome=args.real_chrome, chrome_args=args.chrome_arg)
    results: list[dict] = []
    t0 = time.time()
    try:
        env.start()
        t = T(env)
        t.main = t.active()["tab_id"]
        print(f"Browser {env.chrome_version}; agent :{env.agent_port}; fixtures :{env.fixture_port}\n")
        for cid, name, crit, fn in CASES:
            if only and cid not in only and cid != "23":
                continue
            if cid in SLOW and not (args.resilience or (only and cid in only)):
                continue
            start = time.time()
            try:
                fn(t)
                results.append({"id": cid, "name": name, "criteria": crit, "passed": True, "seconds": round(time.time() - start, 2)})
                print(f"  PASS {cid} {name}  ({time.time() - start:.1f}s)")
            except Exception as e:  # noqa: BLE001 - report every failure and keep going
                tb = traceback.format_exc(limit=4)
                results.append({"id": cid, "name": name, "criteria": crit, "passed": False, "seconds": round(time.time() - start, 2), "error": f"{type(e).__name__}: {e}", "trace": tb})
                print(f"  FAIL {cid} {name}\n       {type(e).__name__}: {str(e)[:600]}")
    finally:
        env.stop()
    passed = [r for r in results if r["passed"]]
    print(f"\n{len(passed)}/{len(results)} cases passed in {time.time() - t0:.0f}s")
    print("\nPhase 1 criteria:")
    for n, label in CRITERIA.items():
        rel = [r for r in results if n in r["criteria"]]
        status = "NOT RUN" if not rel else ("PASS" if all(r["passed"] for r in rel) else "FAIL")
        print(f"  {n:>2}. {label:<42} {status}  {[r['id'] for r in rel]}")
    out = "last_run_real_chrome.json" if args.real_chrome else "last_run.json"
    Path(__file__).with_name(out).write_text(json.dumps({"browser": env.chrome_version, "results": results}, indent=2), encoding="utf-8")
    return 0 if len(passed) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
