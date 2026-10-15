"""Agent status and client context: what a collaborator needs before deciding what to ask for."""
from __future__ import annotations

from ..capabilities import REGISTRY, B, capability
from ..store import CollabError

PROTECTED_NOTE = "protected (HIGH) fields are never returned through the collaboration channel"


@capability("agent.status", "The local agent's health: browser extension connection, Business Brain state, queue depth and the number of capabilities.",
            output={"browser": "connected / version", "business_brain": "enabled or not", "queue": "jobs queued/running", "capabilities": "count"},
            permissions=("agent:read",), category="agent", timeout_s=10)
async def agent_status(ctx, p: dict) -> dict:
    rt = ctx.rt
    s = rt.state.summary()
    q = ctx.svc.store
    return {"browser": {"connected": bool(s.get("connected")), "extension": (s.get("extension") or {}).get("version"), "user_agent": (s.get("extension") or {}).get("user_agent"),
                        "tabs": s.get("tab_count")},
            "business_brain": {"enabled": rt.business is not None, "knowledge_hub": "available"},
            "queue": {"queued": len(q.jobs_in(("queued",))), "running": len(q.jobs_in(("running",))), "awaiting_approval": len(q.jobs_in(("awaiting_approval",)))},
            "capabilities": len(REGISTRY)}


@capability("clients.list", "List clients by id and name so a task can name the right one (no business facts).",
            props={"include_archived": B("Include archived clients (default false).")},
            output={"clients": "[{client_id, name, archived}]"}, permissions=("clients:list",), category="clients", timeout_s=10)
async def clients_list(ctx, p: dict) -> dict:
    rows = ctx.admin().store.list_clients(include_archived=bool(p.get("include_archived")))
    return {"clients": [{"client_id": c["client_id"], "name": c["name"], "archived": c["archived"]} for c in rows]}


def client_context(admin, client_id: str, *, include_unverified: bool = True) -> dict:
    """Verified (and, flagged, unverified) LOW/MEDIUM facts + structured records of ONE client. HIGH values never leave; their keys are listed as withheld."""
    store = admin.store
    cl = store.get_client(client_id)
    facts, withheld, unverified, conflicts = {}, [], [], []
    for f in admin.fields(client_id):
        if f["status"] == "MISSING":
            continue
        if f["sensitivity"] == "HIGH":
            withheld.append(f["key"])
            continue
        vals = []
        for e in f["entries"]:
            if e["status"] == "MISSING":
                continue
            if e["status"] == "CONFLICTING":
                conflicts.append(e["display_key"])
                vals.append({"status": "CONFLICTING", "candidates": [c["value"] for c in e["candidates"]]})
                continue
            if e["status"] == "UNVERIFIED":
                unverified.append(e["display_key"])
                if not include_unverified:
                    continue
            vals.append({"value": e["value"], "status": e["status"], "source": e["source"]})
        if vals:
            facts[f["key"]] = vals if f["many"] else vals[0]
    rec = lambda kind, cols: [{k: r.get(k) for k in cols if r.get(k) not in (None, "")} | {"verification": r.get("verification")} for r in store.records(kind, client_id)]
    return {"client": {"client_id": cl["client_id"], "name": cl["name"], "archived": cl["archived"]},
            "facts": facts,
            "services": rec("service", ("service", "category", "priority", "notes")),
            "locations": rec("location", ("location", "city", "state_region", "country", "location_type", "priority")),
            "seo_targets": rec("seo", ("keyword", "location", "service", "priority")),
            "status": {"unverified": unverified, "conflicting": conflicts, "withheld_protected": withheld, "note": PROTECTED_NOTE}}


def flat(ctx_doc: dict) -> dict:
    """Convenience view used by other capabilities: website, phone, name, city/state, target locations, services, keywords."""
    f = ctx_doc["facts"]
    v = lambda k: (f.get(k) or {}).get("value") if isinstance(f.get(k), dict) else None
    many = lambda k: [x.get("value") for x in (f.get(k) or []) if isinstance(x, dict) and x.get("value")]
    addr = v("location.address") or {}
    tl = many("seo.target_locations[]") + many("local_seo.target_locations[]") + [l["location"] for l in ctx_doc["locations"] if l.get("location")]
    return {"name": v("identity.business_name") or ctx_doc["client"]["name"], "website": v("contact.website") or "", "phone": v("contact.phone") or "",
            "city": addr.get("city", "") if isinstance(addr, dict) else "", "state": addr.get("state", "") if isinstance(addr, dict) else "",
            "target_locations": list(dict.fromkeys(x for x in tl if x)), "services": [s["service"] for s in ctx_doc["services"]] + many("services.primary[]"),
            "keywords": [t["keyword"] for t in ctx_doc["seo_targets"]] + many("seo.target_keywords[]"), "gbp": v("local_seo.gbp_reference") or "",
            "primary_category": v("local_seo.primary_category") or "", "social": many("marketing.social_profiles[]")}


@capability("client.context", "Retrieve ONE client's business context from the Knowledge Hub: facts with their verification state, services, locations and SEO targets. "
            "Protected (HIGH) fields are withheld and only listed by key. Explicit client required.",
            props={"include_unverified": B("Also return values that are not verified yet, flagged as UNVERIFIED (default true).")},
            output={"client": "{client_id, name}", "facts": "{field_key: {value, status, source} | [..]}", "services": "[...]", "locations": "[...]", "seo_targets": "[...]",
                    "status": "{unverified[], conflicting[], withheld_protected[]}", "summary": "flat convenience view"},
            permissions=("client:read",), scope="client", category="clients", timeout_s=20,
            limitations=("Only what the owner has entered/imported into the Knowledge Hub; nothing is looked up online.",))
async def client_context_cap(ctx, p: dict) -> dict:
    doc = client_context(ctx.admin(), ctx.client_id, include_unverified=p.get("include_unverified", True))
    doc["summary"] = flat(doc)
    doc["deployment_targets"] = [{k: t[k] for k in ("target_id", "domain", "environment", "local_path", "hosting", "deploy_method", "rollback_method")} for t in ctx.svc.store.targets(ctx.client_id)]
    if doc["status"]["unverified"]:
        ctx.outcome.completeness = "partial"
        ctx.limit(f"{len(doc['status']['unverified'])} field(s) are not verified by the owner yet")
    ctx.source("knowledge_hub", "", client=ctx.client_id)
    return doc


def require_client(ctx) -> str:
    if not ctx.client_id:
        raise CollabError("CLIENT_REQUIRED", "this capability needs a client")
    return ctx.client_id
