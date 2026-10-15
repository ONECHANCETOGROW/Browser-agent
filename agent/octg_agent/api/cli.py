"""Command line: start the agent, print tokens, manage authorized folders, and drive actions (the Phase 1 test driver).

  python -m octg_agent serve
  python -m octg_agent token
  python -m octg_agent call inspect_page
  python -m octg_agent call click element_id=el_003            # snapshot_id is filled from the agent's latest snapshot (CLI convenience)
  python -m octg_agent repl
  python -m octg_agent fs allow "D:\\Uploads"

AI layer (Phase 2):
  python -m octg_agent provider list | detect | test <name> | set-key <name> | usage
  python -m octg_agent task start "open example.com and read the heading" [--provider local|gemini|freellmapi] [--policy LOCAL_FIRST] [--wait]
  python -m octg_agent task status | approve | deny | pause | resume | stop | list | show <id>

Business Brain (Phase 3, off by default; see `business --help`):
  python -m octg_agent business status | list | set <key> | docs enroll <folder> | enable ...
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import urllib.error
import urllib.request

from ..config import load_secrets, load_settings
from ..engine.registry import ACTIONS, tool_schema
from ..files.authorization import AuthorizationError, AuthorizedFolders
from .business_cli import add_parser as add_business_parser


# ---------------------------------------------------------------- HTTP helpers (stdlib only)
def _http(method: str, path: str, body: dict | None = None) -> dict:
    settings = load_settings()
    secrets = load_secrets(settings)
    req = urllib.request.Request(
        settings.base_url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"X-OCTG-Token": secrets.api_token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"success": False, "error": {"code": f"HTTP_{e.code}", "message": e.read().decode(errors="replace")}}
    except (urllib.error.URLError, ConnectionError) as e:
        return {"success": False, "error": {"code": "AGENT_NOT_RUNNING", "message": f"Cannot reach the agent at {settings.base_url} ({e}). Start it with: python -m octg_agent serve"}}


def parse_kv(tokens: list[str]) -> dict:
    out: dict = {}
    for t in tokens:
        if "=" not in t:
            raise SystemExit(f"Expected key=value, got {t!r}")
        k, v = t.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _fill_snapshot(action: str, params: dict) -> dict:
    spec = ACTIONS.get(action)
    if spec and spec.uses_elements and params.get("element_id") and not params.get("snapshot_id"):
        st = _http("GET", "/api/state")
        tab = params.get("tab_id") if params.get("tab_id") is not None else st.get("active_tab_id")
        snap = (st.get("latest_snapshots") or {}).get(str(tab))
        if snap:
            params = {**params, "snapshot_id": snap["snapshot_id"]}
            print(f"(using latest snapshot {snap['snapshot_id']} for tab {tab})", file=sys.stderr)
    return params


def run_action(action: str, params: dict, compact: bool = False) -> int:
    resp = _http("POST", "/api/action", {"action": action, "parameters": _fill_snapshot(action, params), "source": "cli"})
    print(json.dumps(resp, indent=None if compact else 2, ensure_ascii=False))
    return 0 if resp.get("success") else 1


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def _print_task(t: dict, compact: bool = False) -> None:
    u = t.get("usage") or {}
    print(f"{t['task_id']}  {t['status']}  step {t['step_count']}  model calls {t['model_calls']}  {t['duration_s']}s")
    print(f"  task:   {t['instruction'][:140]}")
    if t.get("current_url"):
        print(f"  page:   {t['current_url'][:100]}  {t.get('current_page_title', '')[:50]!r}")
    if t.get("current_step"):
        print(f"  doing:  {t['current_step'][:120]}")
    if t.get("pending_approval"):
        a = t["pending_approval"]
        print(f"  APPROVAL NEEDED: {a['description']}  [risk {a['risk'].get('level')}]\n    -> python -m octg_agent task approve   |   task deny")
    if t.get("waiting_message") and t["status"] == "WAITING_FOR_USER":
        print(f"  WAITING FOR YOU: {t['waiting_message']}\n    -> do it in Chrome, then: python -m octg_agent task resume")
    if t.get("result"):
        print(f"  result: {t['result'][:300]}")
    if t.get("error"):
        print(f"  error:  {t['error'][:300]}")
    if u.get("by_provider"):
        print("  models: " + "; ".join(f"{p} {v['calls']} call(s) {v['input_tokens']}+{v['output_tokens']} tok" for p, v in u["by_provider"].items()) +
              (f"; est. cost ${u['estimated_cost_usd']}" if u.get("estimated_cost_usd") is not None else ""))
    if not compact:
        for a in t.get("activity", [])[-8:]:
            print(f"    . {a['kind']}: {a['text'][:130]}")


def cmd_task(args) -> int:
    if args.task_cmd == "start":
        r = _http("POST", "/api/tasks", {"instruction": args.instruction, "provider": args.provider, "policy": args.policy, "max_steps": args.max_steps,
                                          "approve_medium": True if args.approve_medium else None,
                                          "business": True if args.business else False if args.no_business else None,
                                          "client": args.client or None})
        if not r.get("ok"):
            print(json.dumps(r.get("error", r), indent=2))
            return 1
        tid = r["task"]["task_id"]
        print(f"started {tid}")
        if not args.wait:
            return 0
        import time as _t

        seen = 0
        while True:
            _t.sleep(1.0)
            d = _http("GET", f"/api/tasks/{tid}").get("task") or {}
            acts = d.get("activity", [])
            for a in acts[seen:]:
                print(f"  {a['kind']:<9} {a['text'][:150]}")
            seen = len(acts)
            if d.get("status") in TERMINAL or d.get("status") in ("WAITING_FOR_APPROVAL", "WAITING_FOR_USER"):
                _print_task(d, compact=True)
                return 0 if d.get("status") == "COMPLETED" else (2 if d.get("status", "").startswith("WAITING") else 1)
    if args.task_cmd in ("status", "show"):
        r = _http("GET", f"/api/tasks/{args.task_id or 'current'}")
        if not r.get("ok"):
            print(json.dumps(r.get("error", r)))
            return 1
        if args.task_cmd == "show" or getattr(args, "json", False):
            print(json.dumps(r["task"], indent=2, default=str))
        else:
            _print_task(r["task"])
        return 0
    if args.task_cmd == "select-client":
        r = _http("POST", f"/api/tasks/{args.task_id or 'current'}/client", {"client": args.client})
        if not r.get("ok"):
            print(json.dumps(r.get("error", r)))
            return 1
        print(f"client selected: {(r['task'].get('business') or {}).get('client')}\nnow: python -m octg_agent task resume")
        return 0
    if args.task_cmd == "list":
        for x in _http("GET", "/api/tasks?limit=20").get("tasks", []):
            print(f"{x['task_id']}  {x['status']:<10} {x['browser_actions']:>2} actions {x['model_calls']:>2} calls  {x['duration_s']:>6}s  {x['instruction'][:70]}")
        return 0
    r = _http("POST", f"/api/tasks/{args.task_id or 'current'}/{args.task_cmd}")
    if not r.get("ok"):
        print(json.dumps(r.get("error", r)))
        return 1
    print(f"{args.task_cmd}: ok -> {r['task']['status']}")
    return 0


def cmd_provider(args) -> int:
    from ..reasoning.keys import KeyStore

    if args.prov_cmd == "set-key":
        import getpass

        key = getpass.getpass(f"API key for {args.name} (input hidden): ").strip()
        if not key:
            print("No key entered; nothing saved.")
            return 1
        KeyStore(load_settings().data_dir).set(args.name, key)
        print(f"Saved to {load_settings().data_dir / 'provider_keys.json'} (local file; never shown or sent anywhere). An environment variable works too.")
        return 0
    if args.prov_cmd == "remove-key":
        print("removed" if KeyStore(load_settings().data_dir).remove(args.name) else "no stored key for that provider")
        return 0
    if args.prov_cmd == "list":
        r = _http("GET", "/api/providers")
        if not r.get("ok"):
            print(json.dumps(r.get("error", r)))
            return 1
        print(f"routing policy: {r['policy']}")
        for p in r["providers"]:
            key = "" if p["name"] == "local" else f"  key: {p.get('key_source') or 'NOT SET'}"
            print(f"  {'*' if p['available'] else 'o'} {p['name']:<11} {p['model']:<26} {'available' if p['available'] else 'unavailable'}: {p['detail']}{key}")
        return 0
    if args.prov_cmd == "detect":
        for rt in _http("GET", "/api/providers/detect-local").get("runtimes", []):
            print(json.dumps(rt))
        return 0
    if args.prov_cmd == "test":
        r = _http("POST", f"/api/providers/{args.name}/test")
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1
    if args.prov_cmd == "usage":
        print(json.dumps(_http("GET", "/api/providers").get("usage_report", {}), indent=2))
        return 0
    return 1


# ---------------------------------------------------------------- commands
def cmd_serve(args) -> int:
    from aiohttp import web

    from ..app import create_app
    from ..logging_setup import setup_console

    settings = load_settings()
    secrets = load_secrets(settings)
    setup_console(settings.log_level)
    app = create_app(settings, secrets)
    print("OCTG local agent (Phase 1)")
    print(f"  listening on   {settings.base_url}   (loopback only)")
    print(f"  extension WS   ws://{settings.host}:{settings.port}/ext")
    print(f"  pairing token  {secrets.pairing_token}   <- paste into the extension side panel")
    print(f"  data folder    {settings.data_dir}")
    print("Ctrl+C to stop.")
    web.run_app(app, host=settings.host, port=settings.port, print=None)
    return 0


def cmd_hub(args) -> int:
    """`hub open`: make a one-time code and open the Knowledge Hub in the browser, already signed in. `hub url`: just print the address."""
    import urllib.request
    import webbrowser

    from ..hub.security import LoginCodes

    settings = load_settings()
    url = f"{settings.base_url}/hub/"
    if args.hub_cmd == "url":
        print(url)
        return 0
    if args.hub_cmd == "pin":
        return _hub_pin(settings, args)
    try:
        urllib.request.urlopen(f"{settings.base_url}/healthz", timeout=3).read()
    except Exception:  # noqa: BLE001
        print(f"The agent is not running at {settings.base_url}. Start it first (double-click start_agent.bat, or: python -m octg_agent serve).", file=sys.stderr)
        return 1
    secrets = load_secrets(settings)  # makes sure the tokens exist
    code = LoginCodes(settings.data_dir).issue()
    link = f"{url}#code={code}"
    print(f"Knowledge Hub: {url}")
    print("A one-time sign-in link was made (valid 5 minutes, usable once).")
    if args.no_browser:
        print(link)
    elif not webbrowser.open(link):
        print("Could not open a browser. Open this once:", link)
    return 0


def _hub_pin(settings, args) -> int:
    """`hub pin set [PIN]` / `hub pin clear` / `hub pin status`: the Knowledge Hub sign-in PIN (4 to 12 digits; only a salted hash is stored)."""
    import getpass

    from ..hub.security import HubPin

    pin = HubPin(settings.data_dir)
    action = args.pin_action or "status"
    if action == "status":
        print("A sign-in PIN is set." if pin.is_set() else "No sign-in PIN is set: the Hub asks for the access code (python -m octg_agent token --api).")
        return 0
    if action == "clear":
        pin.clear()
        print("The sign-in PIN was removed. The Hub asks for the access code again.")
        return 0
    value = args.pin_value or getpass.getpass("New PIN (4 to 12 digits): ")
    if not HubPin.valid(value):
        print("A PIN is 4 to 12 digits (numbers only).", file=sys.stderr)
        return 1
    pin.set(value)
    print("The sign-in PIN is set. Sign in to the Hub with it (the access code still works as a way back in).")
    return 0


def cmd_eye(args) -> int:
    """`eye status | on | off | deny <site> | allow <site>`: the owner's switch and site list for the AI's browsing (the eye). A plain file, data/eye.json; takes effect on the next request."""
    from ..collab.eye import EyePolicy
    from ..config import load_settings

    pol = EyePolicy(load_settings().data_dir)
    c = args.eye_cmd
    if c in ("on", "off"):
        pol.set_enabled(c == "on")
    elif c in ("deny", "allow"):
        if not args.site:
            print(f"usage: eye {c} <site, e.g. example.com>", file=sys.stderr)
            return 2
        getattr(pol, c)(args.site)
    d = pol.load()
    print(f"AI browsing (the eye): {'ON' if d['enabled'] else 'OFF'}")
    print("Blocked sites (the AI cannot open or follow links to these): " + ", ".join(d["deny"]))
    if d["allow_override"]:
        print("Allowed although normally blocked: " + ", ".join(d["allow_override"]))
    print("What it can do: open pages in ONE dedicated tab, read them, scroll, click view controls, sort, page through tables, type into search/filter boxes.")
    print("What it refuses: anything that looks like it changes something (delete, send, pay, publish, save, approve, log out ...), password/personal fields, blocked sites.")
    return 0


def cmd_connectors(args) -> int:
    """`connectors status | verify <source> | gsc-login --client-secret FILE | map ...`: the owner's terminal view of the data sources (GSC, Bing Webmaster)."""
    import asyncio

    from ..business.campaigns import Campaigns
    from ..business.store import BusinessStore
    from ..collab.store import CollabStore
    from ..connectors import SOURCES, ConnectorError, domain_of
    from ..connectors.hub import ConnectorHub, ConnectorStore

    settings = load_settings()
    folder = settings.data_dir / "connectors"
    hub = ConnectorHub(folder, ConnectorStore(CollabStore(settings.data_dir / "collab.db")))
    act = args.conn_cmd
    if act == "status":
        for r in hub.status():
            print(f"{r['label']}: {'CONNECTED' if r['connected'] else 'not connected'}" + (f" - {r['missing']}" if r["missing"] else ""))
            if r.get("identity") and "@" in str(r["identity"]):
                print(f"    add {r['identity']} as a user of each property in the webmaster tool")
            print(f"    credentials folder: {folder}  mappings: {r['mappings']}")
        return 0
    if act == "verify":
        if args.source not in SOURCES:
            print("source is one of: " + ", ".join(SOURCES), file=sys.stderr)
            return 2
        try:
            res = asyncio.run(hub.verify(args.source))
        except ConnectorError as e:
            print(f"{e.code}: {e.message}", file=sys.stderr)
            return 1
        print(f"OK: the credentials work and can see {res['properties_visible']} propert{'y' if res['properties_visible'] == 1 else 'ies'}.")
        return 0
    if act == "gsc-login":
        from ..connectors import oauth
        if not args.client_secret:
            print("pass --client-secret <the OAuth client JSON (Desktop app) from your Google Cloud project>", file=sys.stderr)
            return 2
        try:
            path = asyncio.run(oauth.login(args.client_secret, folder, on_url=lambda u: print("If the browser does not open, open this address:\n" + u)))
        except ConnectorError as e:
            print(f"{e.code}: {e.message}", file=sys.stderr)
            return 1
        print(f"Signed in. The refresh token was stored in {path}. Next: python -m octg_agent connectors verify google_search_console, then map each client's property in the Knowledge Hub.")
        return 0
    if act == "map":
        if not (args.source in SOURCES and args.client and args.property):
            print("usage: connectors map --source SOURCE --client <client id or exact name> --property <property>", file=sys.stderr)
            return 2
        biz = BusinessStore(settings.data_dir / "business.db")
        try:
            c = biz.find_client(args.client)
        except Exception as e:  # noqa: BLE001
            print(f"client not found or ambiguous: {getattr(e, 'message', e)}", file=sys.stderr)
            return 1
        doms = [Campaigns(biz).list(c["client_id"])[i]["website"] for i in range(len(Campaigns(biz).list(c["client_id"]))) if Campaigns(biz).list(c["client_id"])[i].get("website")]
        try:
            site = biz.get_value("contact.website", c["client_id"]).value
            doms.append(site)
        except Exception:  # noqa: BLE001
            pass
        if not sys.stdin.isatty():
            print("Mapping gives a client access to a data source: run this in a terminal and type YES.", file=sys.stderr)
            return 2
        if input(f"Map {args.source} property {args.property} to client {c['name']}? Type YES to confirm: ").strip() != "YES":
            print("Not mapped.")
            return 1
        try:
            m = asyncio.run(hub.map(c["client_id"], args.source, args.property, client_domains=doms, mapped_by="owner:cli", allow_mismatch=args.allow_mismatch))
        except ConnectorError as e:
            print(f"{e.code}: {e.message}", file=sys.stderr)
            return 1
        print(f"Mapped {m['property']} to {c['name']}.")
        return 0
    print("connectors: status | verify <source> | gsc-login --client-secret FILE | map --source S --client C --property P", file=sys.stderr)
    return 2


def _conn_args(a):
    """`connectors map --source S` and `connectors verify S` both name the source: accept either spelling."""
    if getattr(a, "source", None) is None and getattr(a, "source_opt", None):
        a.source = a.source_opt
    return a


def cmd_token(args) -> int:
    s = load_secrets(load_settings())
    print(s.api_token if args.api else s.pairing_token)
    return 0


def cmd_tools(args) -> int:
    if args.json:
        print(json.dumps(tool_schema(), indent=2))
    else:
        for a in ACTIONS.values():
            print(f"{a.name:<18} [{a.risk.level:<6}] {a.description}")
    return 0


def cmd_state(args) -> int:
    print(json.dumps(_http("GET", "/api/state"), indent=2))
    return 0


def cmd_call(args) -> int:
    params = json.loads(args.json) if args.json else {}
    params.update(parse_kv(args.params))
    return run_action(args.action, params, args.compact)


def cmd_repl(args) -> int:
    print("OCTG REPL. Type: <action> key=value ...   or   <action> {json}.  'tools' lists actions, 'quit' exits.")
    while True:
        try:
            line = input("octg> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("quit", "exit"):
            return 0
        if line == "tools":
            cmd_tools(argparse.Namespace(json=False))
            continue
        action, _, rest = line.partition(" ")
        try:
            params = json.loads(rest) if rest.strip().startswith("{") else parse_kv(shlex.split(rest))
        except (ValueError, SystemExit) as e:
            print(f"bad parameters: {e}")
            continue
        run_action(action, params, compact=args.compact)


def cmd_fs(args) -> int:
    folders = AuthorizedFolders(load_settings().folders_path)
    try:
        if args.fs_cmd == "allow":
            e = folders.allow(args.path, args.label)
            print(f"Authorized: {e['path']}")
        elif args.fs_cmd == "revoke":
            print("Revoked." if folders.revoke(args.path) else "That folder was not authorized.")
        else:
            roots = folders.roots()
            print("\n".join(r["path"] for r in roots) if roots else "No authorized folders. Add one with: python -m octg_agent fs allow <folder>")
    except AuthorizationError as e:
        print(f"Refused: {e.message}", file=sys.stderr)
        return 1
    return 0


def _safe_console() -> None:
    """Windows consoles/pipes may be cp1252; never crash on a character (page titles, arrows) the console cannot show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> None:
    _safe_console()
    p = argparse.ArgumentParser(prog="octg_agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="start the local agent").set_defaults(fn=cmd_serve)
    t = sub.add_parser("token", help="print the pairing token for the extension")
    t.add_argument("--api", action="store_true", help="print the HTTP API token instead")
    t.set_defaults(fn=cmd_token)
    tl = sub.add_parser("tools", help="list actions")
    tl.add_argument("--json", action="store_true", help="full machine-readable tool schema")
    tl.set_defaults(fn=cmd_tools)
    sub.add_parser("state", help="show the agent's browser state").set_defaults(fn=cmd_state)
    c = sub.add_parser("call", help="run one action")
    c.add_argument("action")
    c.add_argument("params", nargs="*", help="key=value pairs (JSON values allowed)")
    c.add_argument("--json", help="parameters as a JSON object")
    c.add_argument("--compact", action="store_true")
    c.set_defaults(fn=cmd_call)
    r = sub.add_parser("repl", help="interactive driver")
    r.add_argument("--compact", action="store_true")
    r.set_defaults(fn=cmd_repl)
    f = sub.add_parser("fs", help="manage authorized folders (local only)")
    fs = f.add_subparsers(dest="fs_cmd", required=True)
    for name in ("allow", "revoke"):
        x = fs.add_parser(name)
        x.add_argument("path")
        if name == "allow":
            x.add_argument("--label")
    fs.add_parser("list")
    f.set_defaults(fn=cmd_fs)
    tk = sub.add_parser("task", help="run and control AI tasks")
    ts = tk.add_subparsers(dest="task_cmd", required=True)
    st = ts.add_parser("start")
    st.add_argument("instruction")
    st.add_argument("--provider", help="local | gemini | freellmapi (default: routed by policy)")
    st.add_argument("--policy", help="LOCAL_FIRST | GEMINI_FIRST | FALLBACK | MANUAL")
    st.add_argument("--max-steps", type=int)
    st.add_argument("--approve-medium", action="store_true", help="also ask before medium-risk actions")
    st.add_argument("--wait", action="store_true", help="follow the task until it finishes or needs you")
    st.add_argument("--business", action="store_true", help="let this task use your Business Brain (otherwise decided from the wording)")
    st.add_argument("--no-business", action="store_true", help="do not let this task use your Business Brain")
    st.add_argument("--client", help="run this task FOR a client (id or exact name): only that client's details are available to it. Without it, a client is chosen only if exactly one client's name is in the wording")
    sc = ts.add_parser("select-client", help="choose the client of the running task (when it asked which one), then: task resume")
    sc.add_argument("client")
    sc.add_argument("task_id", nargs="?")
    for name in ("status", "show", "approve", "deny", "pause", "resume", "stop", "cancel"):
        x = ts.add_parser(name)
        x.add_argument("task_id", nargs="?")
        if name == "status":
            x.add_argument("--json", action="store_true")
    ts.add_parser("list")
    tk.set_defaults(fn=cmd_task)
    pv = sub.add_parser("provider", help="model providers")
    ps = pv.add_subparsers(dest="prov_cmd", required=True)
    for name in ("list", "detect", "usage"):
        ps.add_parser(name)
    for name in ("test", "set-key", "remove-key"):
        ps.add_parser(name).add_argument("name")
    pv.set_defaults(fn=cmd_provider)
    hb = sub.add_parser("hub", help="the OCTG Knowledge Hub (local web interface)")
    hb.add_argument("hub_cmd", choices=["open", "url", "pin"], nargs="?", default="open")
    hb.add_argument("pin_action", choices=["set", "clear", "status"], nargs="?", help="with `hub pin`: set, clear or status")
    hb.add_argument("pin_value", nargs="?", help="with `hub pin set`: the PIN (omit it to be asked, without it showing on screen)")
    hb.add_argument("--no-browser", action="store_true", help="print the one-time link instead of opening a browser")
    hb.set_defaults(fn=cmd_hub)
    ey = sub.add_parser("eye", help="the AI's browsing (the eye): status, on, off, and the blocked-site list")
    ey.add_argument("eye_cmd", choices=["status", "on", "off", "deny", "allow"], nargs="?", default="status")
    ey.add_argument("site", nargs="?", help="with deny / allow: a site such as example.com")
    ey.set_defaults(fn=cmd_eye)
    cn = sub.add_parser("connectors", help="GSC / Bing Webmaster data sources (status, verify, sign in, map)")
    cn.add_argument("conn_cmd", choices=["status", "verify", "gsc-login", "map"], nargs="?", default="status")
    cn.add_argument("source", nargs="?", help="with verify: google_search_console or bing_webmaster_tools")
    cn.add_argument("--client-secret", help="with gsc-login: the OAuth client JSON (Desktop app) from your Google Cloud project")
    cn.add_argument("--source", dest="source_opt", help=argparse.SUPPRESS)
    cn.add_argument("--client", help="with map: client id or exact name")
    cn.add_argument("--property", help="with map: the GSC / Bing property")
    cn.add_argument("--allow-mismatch", action="store_true", help="with map: the property's domain differs from the client's website but it really is theirs")
    cn.set_defaults(fn=lambda a: cmd_connectors(_conn_args(a)))
    add_business_parser(sub)
    from ..collab.client import add_parser as add_collab_parser
    add_collab_parser(sub)
    args = p.parse_args(argv)
    try:
        sys.exit(args.fn(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
