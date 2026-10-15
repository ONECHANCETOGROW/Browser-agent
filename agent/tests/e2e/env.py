"""End-to-end environment: real agent process + real Chrome (temp profile, extension loaded) + local fixture web server.

Nothing here touches your normal Chrome profile. The extension is paired through the same chrome.storage config the side panel writes.
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = ROOT / "agent"
EXT_DIR = ROOT / "extension"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXT_ID = "kfofbldlpmoobgiafcbohiiebmfggecg"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_installed_chrome() -> Path:
    """The branded Google Chrome that is installed on this PC (not Playwright's Chromium)."""
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              str(Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe")):
        if Path(c).is_file():
            return Path(c)
    raise SystemExit("Google Chrome is not installed in the usual locations.")


def find_chrome() -> Path:
    """Prefer a Chromium that still honours --load-extension (Chrome 137+ branded builds ignore it); OCTG_CHROME overrides."""
    pw = sorted((Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob("chromium-*/chrome-win*/chrome.exe"), reverse=True)
    cands = [os.environ.get("OCTG_CHROME", ""), *map(str, pw), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    raise SystemExit("No Chromium/Chrome found. Set OCTG_CHROME to the browser executable.")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # keep test output readable
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class E2EEnv:
    def __init__(self, headed: bool = False, chrome: Path | None = None, real_chrome: bool = False, chrome_args: list[str] | None = None):
        """real_chrome=True: use the installed branded Chrome. It ignores --load-extension (Chrome 137+), so the extension is loaded
        through CDP `Extensions.loadUnpacked` over a DevTools pipe by tests/e2e/chrome_launcher.js (needs Node)."""
        self.real_chrome = real_chrome
        self.chrome_args = list(chrome_args or [])  # extra browser flags, e.g. --use-fake-device-for-media-stream for voice tests
        if real_chrome:
            # Chrome's native window-occlusion tracking marks a headed window whose area is covered by other windows as HIDDEN: it then renders no frames, so
            # captureVisibleTab fails ("image readback failed") and frame-driven page events (scroll) never fire. That made the screenshot and find_text cases flaky
            # whenever something covered the test window (see PHASE3_CLIENTS.md). Test harness only; product code is unchanged.
            self.chrome_args = ["--disable-features=CalculateNativeWinOcclusion", *self.chrome_args]
        self.headed = (headed or real_chrome) and not os.environ.get("OCTG_E2E_HEADLESS")  # OCTG_E2E_HEADLESS=1: run the installed Chrome headless (diagnosis aid)
        self.chrome_exe = chrome or (find_installed_chrome() if real_chrome else find_chrome())
        self.launcher: subprocess.Popen | None = None
        self.extension_id_loaded = ""
        self.tmp = Path(tempfile.mkdtemp(prefix="octg_e2e_"))
        self.agent_port, self.fixture_port, self.cdp_port = free_port(), free_port(), free_port()
        self.data_dir = self.tmp / "data"
        self.agent: subprocess.Popen | None = None
        self.chrome: subprocess.Popen | None = None
        self.httpd: http.server.ThreadingHTTPServer | None = None
        self.api_token = ""
        self.pairing_token = ""
        self.chrome_version = ""

    # ------------------------------------------------------------ urls
    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.agent_port}"

    def fixture(self, name: str) -> str:
        return f"http://127.0.0.1:{self.fixture_port}/{name}"

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        handler = functools.partial(QuietHandler, directory=str(FIXTURES))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.fixture_port), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.start_agent()
        self.start_chrome()
        self.pair_extension()

    def start_agent(self) -> None:
        env = {**os.environ, "OCTG_DATA_DIR": str(self.data_dir), "OCTG_PORT": str(self.agent_port), "PYTHONUNBUFFERED": "1"}
        env.pop("OCTG_CONFIG", None)
        self._agent_log = open(self.tmp / f"agent_{int(time.time())}.log", "w", encoding="utf-8")
        self.agent = subprocess.Popen([sys.executable, "-m", "octg_agent", "serve"], cwd=AGENT_DIR, env=env, stdout=self._agent_log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                with urllib.request.urlopen(self.base + "/healthz", timeout=1):
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("agent did not start; see " + self._agent_log.name)
        secrets = json.loads((self.data_dir / "secrets.json").read_text())
        self.api_token, self.pairing_token = secrets["api_token"], secrets["pairing_token"]

    def stop_agent(self) -> None:
        if self.agent and self.agent.poll() is None:
            self.agent.terminate()
            try:
                self.agent.wait(5)
            except subprocess.TimeoutExpired:
                self.agent.kill()
        self.agent = None

    def start_chrome(self) -> None:
        if self.real_chrome:
            self.launcher = subprocess.Popen(
                ["node", str(Path(__file__).with_name("chrome_launcher.js")), str(self.chrome_exe), str(self.tmp / "profile"), str(EXT_DIR),
                 str(self.cdp_port), self.fixture("index.html"), *self.chrome_args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            line = self.launcher.stdout.readline()
            info = json.loads(line) if line.strip().startswith("{") else {"ok": False, "error": line}
            if not info.get("ok"):
                raise RuntimeError("Could not load the extension into Chrome: " + json.dumps(info.get("error")))
            self.extension_id_loaded = info["extension_id"]
            if self.extension_id_loaded != EXT_ID:
                raise RuntimeError(f"Extension loaded with ID {self.extension_id_loaded}, expected {EXT_ID}")
            self.chrome = self.launcher
        else:
            self._start_chromium()
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=1) as r:
                    self.chrome_version = json.loads(r.read())["Browser"]
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("Chrome did not expose the DevTools port")

    def _start_chromium(self) -> None:
        args = [
            str(self.chrome_exe), f"--user-data-dir={self.tmp / 'profile'}", f"--load-extension={EXT_DIR}",
            "--disable-features=DisableLoadExtensionCommandLineSwitch,ExtensionsMenuAccessControl",
            f"--remote-debugging-port={self.cdp_port}", "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-background-networking", "--window-size=1100,850", "--force-device-scale-factor=1",
        ]
        if not self.headed:
            args.append("--headless=new")
        args += self.chrome_args
        args.append(self.fixture("index.html"))
        self.chrome = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        if self.chrome and self.chrome.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.chrome.pid), "/T", "/F"], capture_output=True)
        if self.launcher and self.launcher.poll() is None:  # also takes Chrome down with it
            self.launcher.terminate()
            try:
                self.launcher.wait(5)
            except subprocess.TimeoutExpired:
                self.launcher.kill()
        self.stop_agent()
        if self.httpd:
            self.httpd.shutdown()
        try:
            self._agent_log.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ DevTools (used ONLY to pair the extension / inspect the side panel)
    def targets(self) -> list[dict]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=3) as r:
            return json.loads(r.read())

    def cdp_eval(self, target: dict, expression: str, await_promise: bool = True):
        async def go():
            async with aiohttp.ClientSession() as s, s.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "awaitPromise": await_promise, "returnByValue": True}})
                async for m in ws:
                    d = json.loads(m.data)
                    if d.get("id") == 1:
                        if "exceptionDetails" in d.get("result", {}):
                            raise RuntimeError(d["result"]["exceptionDetails"])
                        return d["result"]["result"].get("value")
        return asyncio.run(go())

    def browser_cdp(self, method: str, params: dict | None = None) -> dict:
        """One command on the browser-level DevTools endpoint (test harness only)."""
        ver = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=3).read())

        async def go():
            async with aiohttp.ClientSession() as s, s.ws_connect(ver["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                await ws.send_json({"id": 1, "method": method, "params": params or {}})
                async for m in ws:
                    d = json.loads(m.data)
                    if d.get("id") == 1:
                        return d
        return asyncio.run(go())

    def extension_worker(self, timeout: float = 20) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            for t in self.targets():
                if t.get("type") == "service_worker" and EXT_ID in t.get("url", ""):
                    return t
            time.sleep(0.25)
        raise RuntimeError("The extension's service worker never appeared: the extension failed to load. Targets: " + json.dumps([(t['type'], t['url']) for t in self.targets()]))

    def pair_extension(self) -> None:
        sw = self.extension_worker()
        for _ in range(60):  # the target is listed a moment before the extension APIs are injected into the worker
            try:
                if self.cdp_eval(sw, "typeof chrome !== 'undefined' && !!chrome.storage"):
                    break
            except RuntimeError:
                pass
            time.sleep(0.25)
        cfg = {"url": f"ws://127.0.0.1:{self.agent_port}/ext", "token": self.pairing_token, "enabled": True}
        self.cdp_eval(sw, f"chrome.storage.local.set({{octg_config: {json.dumps(cfg)}}})")
        self.wait_connected()

    def wait_connected(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                req = urllib.request.Request(self.base + "/api/state", headers={"X-OCTG-Token": self.api_token})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if json.loads(r.read()).get("connected"):
                        return
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("Extension did not connect to the agent within %ss" % timeout)
