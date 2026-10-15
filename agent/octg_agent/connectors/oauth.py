"""`python -m octg_agent connectors gsc-login --client-secret <file>`: sign in to Google ONCE as the owner and store a refresh token for the webmaster API (read-only scope).

The standard "installed application" flow with PKCE: a one-shot HTTP listener on 127.0.0.1 (random port) receives the redirect, the state is checked, the code is exchanged at Google's token endpoint
(a fixed host) and only the refresh token is written, to <data dir>/connectors/gsc_oauth_token.json. Nothing is printed except the outcome. The OAuth client JSON comes from the owner's own
Google Cloud project (APIs & Services -> Credentials -> OAuth client ID -> Desktop app).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import ConnectorError
from .gsc import SCOPE, TOKEN_URL
from .http import Http

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def read_client_secret(path: Path | str) -> tuple[str, str]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConnectorError("CREDENTIALS_INVALID", "that OAuth client file cannot be read as JSON") from None
    c = d.get("installed") or d.get("web") or d
    if not isinstance(c, dict) or not c.get("client_id") or not c.get("client_secret"):
        raise ConnectorError("CREDENTIALS_INVALID", "that file has no client_id/client_secret (download the OAuth client of type 'Desktop app' from Google Cloud Console)")
    return c["client_id"], c["client_secret"]


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def auth_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
                                       "code_challenge": challenge, "code_challenge_method": "S256"})


async def exchange(http: Http, client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str) -> str:
    res = await http.request("POST", TOKEN_URL, form={"grant_type": "authorization_code", "code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri, "code_verifier": verifier})
    if res.status != 200 or not isinstance(res.data, dict):
        raise ConnectorError("AUTH_FAILED", "Google refused the sign-in code (it may have expired or been used already): run the command again")
    if not res.data.get("refresh_token"):
        raise ConnectorError("AUTH_FAILED", "Google returned no refresh token (this Google account may already have granted access: revoke it at myaccount.google.com/permissions and run again)")
    return res.data["refresh_token"]


class _Catcher(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        self.server.result = q
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif'><h3>OCTG: you can close this tab.</h3></body></html>")

    def log_message(self, *a):   # never log the request line (it carries the code)
        pass


def listen() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), _Catcher)
    srv.result = None
    srv.timeout = 1.0
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def wait_for_redirect(srv: HTTPServer, timeout_s: float) -> dict:
    t = threading.Event()

    def run():
        end = timeout_s
        while srv.result is None and end > 0:
            srv.handle_request()
            end -= 1
        t.set()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    t.wait(timeout_s + 2)
    return srv.result or {}


async def login(client_secret_path: Path | str, folder: Path | str, *, open_browser: bool = True, timeout_s: float = 240, http: Http | None = None, on_url=None) -> str:
    cid, secret = read_client_secret(client_secret_path)
    srv, redirect = listen()
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(24)
    url = auth_url(cid, redirect, state, challenge)
    if on_url:
        on_url(url)
    if open_browser:
        webbrowser.open(url)
    import asyncio
    result = await asyncio.get_running_loop().run_in_executor(None, wait_for_redirect, srv, timeout_s)
    srv.server_close()
    if not result:
        raise ConnectorError("LOGIN_TIMEOUT", f"no sign-in was completed within {int(timeout_s)} seconds")
    if result.get("state") != state:
        raise ConnectorError("LOGIN_REFUSED", "the sign-in answer did not match this request (state mismatch): nothing was stored")
    if result.get("error") or not result.get("code"):
        raise ConnectorError("LOGIN_REFUSED", f"Google did not grant access ({result.get('error') or 'no code'})")
    refresh = await exchange(http or Http(), cid, secret, result["code"], redirect, verifier)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "gsc_oauth_token.json"
    out.write_text(json.dumps({"client_id": cid, "client_secret": secret, "refresh_token": refresh}), encoding="utf-8")
    return str(out)
