"""The read-only first-audit workflow for a real client: a preflight that says exactly what is ready and what the OWNER still has to do, and a one-call start of the audit.

The audit is a `seo_visibility` loop with `baseline_only`: it establishes the baseline (search observations in the owner's Chrome), reads the client's own site (crawl + audit), collects first-party search
performance where an authorized property is mapped, profiles the competitors seen in the search results, builds the prioritised report, and then ENDS (status completed). It plans nothing, changes nothing
and deploys nothing: no capability it uses writes to a client website, and the risky-path exclusions of the crawler apply.
"""
from __future__ import annotations

from ...business.campaigns import Campaigns
from ...business.store import StoreError
from ...connectors import LABEL, PROVIDERS, SOURCES
GSC, BING = SOURCES
from ..capabilities import B, I, S, capability
from ..store import CollabError
from .core import client_context, flat, require_client

GSC_STEPS = PROVIDERS[GSC]['steps']      # owner-facing setup text lives in connectors/providers.json
BING_STEPS = PROVIDERS[BING]['steps']
MAP_STEP = PROVIDERS['map_step']


def _check(name: str, level: str, detail: str, action: list[str] | None = None) -> dict:
    return {"name": name, "level": level, "detail": detail, **({"owner_actions": action} if action else {})}


async def preflight(ctx, cid: str, campaign_id: str | None = None) -> dict:
    admin = ctx.admin()
    cdoc = client_context(admin, cid)
    info = flat(cdoc)
    checks: list[dict] = []
    camps = Campaigns(admin.store)
    camp = None
    try:
        camp = camps.find_for_client(cid, campaign_id)
    except StoreError as e:
        checks.append(_check("SEO campaign", "blocking", e.message, ["Ask the collaborator to create the campaign (seo.campaign_upsert) from the client's keyword list, or add SEO targets in the Hub (client -> SEO targets)."]))
    website = (camp or {}).get("website") or info["website"]
    checks.append(_check("Client website", "ok" if website else "blocking", website or "no website in the Knowledge Hub or the campaign",
                         None if website else ["Add the website on the client's Contact tab in the Knowledge Hub."]))
    if camp:
        kws = camp["keywords"]
        outside = [k for k in kws if k.get("area_status") == "outside"]
        usable = [k for k in kws if k.get("area_status") != "outside"]
        checks.append(_check("Target keywords", "ok" if usable else "blocking", f"{len(usable)} keyword(s) will be observed" + (f"; {len(outside)} outside the client's known service areas are skipped until you confirm them" if outside else ""),
                             None if usable else ["Add keywords, or confirm the service areas, in the Hub (client -> SEO targets / Locations)."]))
        if not (cdoc["locations"] or info["target_locations"]):
            checks.append(_check("Service areas", "limits", "the Knowledge Hub has no service areas for this client, so keyword locations cannot be validated", ["Add service areas on the client's Locations tab."]))
    checks.append(_check("Chrome extension (search results, bot-protected sites)", "ok" if ctx.rt.session is not None else "limits",
                         "connected" if ctx.rt.session is not None else "not connected: search observations and bot-protected pages cannot be read until it is",
                         None if ctx.rt.session is not None else ["Open Chrome with the OCTG extension and press Connect in its side panel."]))
    hub = ctx.svc.connectors
    folder = str(hub.folder)
    for src in SOURCES:
        cli = hub.client(src)
        cfg = cli.configured()
        maps = hub.db.mappings(client_id=cid, source=src)
        st = hub.db.state(src)
        steps = GSC_STEPS if src == "google_search_console" else BING_STEPS
        if not cfg["configured"]:
            checks.append(_check(LABEL[src], "limits", f"not connected: {cfg['problem']}; this audit will run without it and say so",
                                 [x.format(folder=folder, identity="<the service account's e-mail>") for x in steps] + [MAP_STEP.format(label=LABEL[src])]))
        elif not maps:
            ident = cfg["identity"] or ""
            checks.append(_check(LABEL[src], "limits", "credentials are present but no property is mapped to this client", [MAP_STEP.format(label=LABEL[src])] + ([f"Make sure {ident} is a user of the client's property."] if "@" in ident else [])))
        elif st and not st["ok"]:
            checks.append(_check(LABEL[src], "limits", f"mapped, but the last verification failed: {st['code']}: {st['message']}", ["Fix the credentials/permission, then run connectors.verify."]))
        else:
            checks.append(_check(LABEL[src], "ok", "mapped property: " + ", ".join(m["property"] for m in maps) + ("" if st else " (credentials not verified yet: the audit's first request will check)")))
    active = [lp for lp in ctx.svc.loops.db.list(client_id=cid, limit=50) if lp["status"] in ("active", "blocked", "paused")]
    if active:
        checks.append(_check("Existing loops", "warn", f"{len(active)} loop(s) for this client are still open ({', '.join(a['loop_id'] for a in active)}): starting another audit alongside is allowed but usually unnecessary"))
    checks.append(_check("Read-only guarantee", "ok", "the audit reads search results, the client's public site (risky paths such as reset/install/admin/login/cart are never requested), competitor sites and the "
                         "connected search data. It writes research records only. No change is planned and nothing is deployed."))
    blocking = [c for c in checks if c["level"] == "blocking"]
    limits = [c for c in checks if c["level"] == "limits"]
    actions = [a for c in blocking + limits for a in c.get("owner_actions", [])]
    return {"client": cdoc["client"], "campaign_id": (camp or {}).get("campaign_id"), "ready_to_start": not blocking, "complete_data": not blocking and not limits, "checks": checks,
            "blocking": [c["name"] for c in blocking], "will_be_missing": [c["name"] for c in limits],
            "will_run": ["search observations for each campaign keyword (organic position, map pack)", "site crawl + technical audit", "competitor profiles from the observed results",
                         "first-party search performance from GSC and Bing (only for sources that are ready)", "the prioritised SEO report"],
            "will_not_run": ["any change to the client's website", "any deployment", "any approval request", "contacting anyone"], "owner_actions": actions}


@capability("seo.audit_preflight", "Before the first real audit of ONE client: what is ready, what will be missing from the audit, and the exact steps the owner still has to do (credentials, property mapping, "
            "Chrome connection). Read-only; starts nothing.",
            props={"campaign_id": S("Campaign (default: the client's only campaign).")}, output={"ready_to_start": "bool", "complete_data": "bool", "checks": "[{name, level, detail, owner_actions?}]", "owner_actions": "[string]"},
            permissions=("client:read", "connector:read"), scope="client", category="seo", timeout_s=30)
async def audit_preflight(ctx, p: dict) -> dict:
    return await preflight(ctx, require_client(ctx), p.get("campaign_id"))


@capability("seo.start_baseline_audit", "Start the READ-ONLY baseline audit of ONE client as a loop that ends by itself: search observations, site audit, competitor profiles, GSC/Bing data where "
            "authorized, and the prioritised report. Refuses when the preflight finds a blocking problem. Plans, changes and deploys nothing.",
            props={"campaign_id": S("Campaign (default: the client's only campaign)."), "competitor_depth": S("page (default) | site (also a bounded read of each competitor's site)", enum=["page", "site"]),
                   "max_pages": I("Pages of the client's own site to audit (default 25).", minimum=1, maximum=80), "gsc_days": I("GSC window in days (default 28).", minimum=7, maximum=90),
                   "engine": S("google (default) | bing", enum=["google", "bing"]), "allow_duplicate": B("Start even if another loop for this client is open.")},
            output={"loop": "the loop description"}, permissions=("collab:write", "research:write", "client:read", "search:read", "net:fetch", "agent:read", "connector:read"), scope="client", read_only=False,
            category="seo", timeout_s=60)
async def start_baseline_audit(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    pf = await preflight(ctx, cid, p.get("campaign_id"))
    if not pf["ready_to_start"]:
        raise CollabError("PREFLIGHT_BLOCKED", "the audit cannot start yet: " + "; ".join(pf["blocking"]), {"checks": [c for c in pf["checks"] if c["level"] == "blocking"], "owner_actions": pf["owner_actions"]})
    if any(c["name"] == "Existing loops" for c in pf["checks"]) and not p.get("allow_duplicate"):
        raise CollabError("LOOP_ALREADY_OPEN", "another loop for this client is still open: finish or stop it, or pass allow_duplicate", {"checks": [c for c in pf["checks"] if c["name"] == "Existing loops"]})
    cfg = {"campaign_id": pf["campaign_id"], "engine": p.get("engine", "google"), "audit": True, "competitors": True, "competitor_depth": p.get("competitor_depth", "page"),
           "max_pages": p.get("max_pages", 25), "gsc_days": p.get("gsc_days", 28), "min_gap_hours": 0}
    d = await ctx.svc.loops.create(ctx.caller, cid, "seo_visibility", f"Baseline audit: {pf['client']['name']}", cfg, {"baseline_only": True, "max_requests": 40})
    ctx.source("loop_controller", "", loop=d["loop_id"])
    return {"loop": d, "preflight": {"complete_data": pf["complete_data"], "will_be_missing": pf["will_be_missing"], "owner_actions": pf["owner_actions"]}}
