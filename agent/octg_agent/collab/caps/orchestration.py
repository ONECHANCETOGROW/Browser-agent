"""Coordination capabilities: recording reasoning artifacts, deployment targets, implementation handoffs, approvals, changes and post-change verification.

The four states the owner cares about are kept apart on purpose:
  PROPOSED  - an approval request / a handoff exists                       (approval.request, handoff.create)
  APPROVED  - the OWNER approved it in the Knowledge Hub                    (never through this protocol)
  EXECUTED  - the collaborator reports the action done, with its evidence  (change.mark_deployed; refused unless APPROVED)
  VERIFIED  - this agent checked the live result itself                    (verify.page)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from ...seo import fetch as F
from ...seo.signals import extract, host_key
from ..capabilities import PERMISSIONS, A, B, I, O, S, capability
from ..store import ARTIFACT_KINDS, STAGES, CollabError
from .core import client_context, flat, require_client
from .web import fetch_error

ACTION_KINDS = ("production_deploy", "publish_content", "business_profile_change", "send_message", "ad_budget_change", "delete_data", "credential_change", "destructive_db_change",
                "other_high_impact")
DEFAULT_CONSTRAINTS = [
    "Change only the files needed for the listed findings; no unrelated refactors or formatting churn.",
    "Never overwrite production data (databases, uploads, data folders outside the web root).",
    "Business facts in copy (name, phone, address, hours, prices) must match the client's VERIFIED Knowledge Hub record; ask if unverified.",
    "No content copied from competitors; no fabricated reviews, credentials, awards or statistics.",
    "No doorway pages: a location page must contain genuine local detail.",
    "Production deployment requires an APPROVED production_deploy approval for this change.",
]
MAX_BODY = 400_000


def _cap_body(body) -> None:
    if len(json.dumps(body, default=str)) > MAX_BODY:
        raise CollabError("TOO_LARGE", f"artifact body exceeds {MAX_BODY} bytes; store large material as evidence")


def _own_artifact(ctx, aid: str, kind: str | None = None) -> dict:
    a = ctx.svc.store.get_artifact(aid)
    if ctx.client_id and a["client_id"] and a["client_id"] != ctx.client_id:
        raise CollabError("SCOPE_MISMATCH", "that artifact belongs to another client", status=403)
    if kind and a["kind"] != kind:
        raise CollabError("WRONG_KIND", f"{aid} is a {a['kind']}, not a {kind}")
    return a


@capability("artifact.record", "Record a reasoning/coordination artifact (analysis, plan, note, report) in the current task so research, reasoning, implementation and "
            "verification stay separately auditable.",
            props={"kind": S("analysis | plan | note | report", enum=["analysis", "plan", "note", "report"]), "stage": S("research | reasoning | implementation | verification | coordination", enum=list(STAGES)),
                   "title": S("Title.", minLength=1, maxLength=300), "body": O("Structured content."), "body_md": S("Readable Markdown.", maxLength=200000),
                   "links": O("References: request ids, evidence ids, artifact ids, finding ids.")},
            required=("kind", "stage", "title"), output={"artifact_id": "id"}, permissions=("collab:write",), scope="client_optional", read_only=False, category="coordination", timeout_s=15)
async def artifact_record(ctx, p: dict) -> dict:
    _cap_body(p.get("body"))
    a = ctx.svc.store.add_artifact(task_id=ctx.task_id, client_id=ctx.client_id, kind=p["kind"], stage=p["stage"], title=p["title"], body=p.get("body"), body_md=p.get("body_md", ""),
                                   links=p.get("links"), created_by=ctx.caller)
    return {"artifact_id": a["artifact_id"], "created_at": a["created_at"]}


@capability("artifact.get", "Read one artifact (finding set, report, handoff, change, verification...) of the current client.",
            props={"artifact_id": S("Artifact id.", minLength=1)}, required=("artifact_id",), output={"artifact": "{kind, stage, title, body, body_md, links, status}"},
            permissions=("client:read",), scope="client_optional", category="coordination", timeout_s=15)
async def artifact_get(ctx, p: dict) -> dict:
    a = _own_artifact(ctx, p["artifact_id"])
    if a["client_id"] and not ctx.client_id:
        raise CollabError("CLIENT_REQUIRED", "this artifact belongs to a client: pass that client")
    return {"artifact": a}


@capability("target.register", "Register (or replace) where ONE client's website lives: domain, environment, local source folder, hosting account/site, deploy and rollback method. "
            "Refuses a domain that differs from the client's website in the Knowledge Hub unless explicitly confirmed.",
            props={"domain": S("Domain, e.g. example.com.", minLength=3, maxLength=200), "environment": S("production | staging", enum=["production", "staging"]),
                   "local_path": S("Local source folder the implementer edits.", maxLength=500), "hosting": S("Hosting account / site identifier (no credentials).", maxLength=200),
                   "deploy_method": S("How a deploy is done.", maxLength=1000), "rollback_method": S("How to roll back.", maxLength=1000), "notes": S("Notes.", maxLength=2000),
                   "verified_evidence": A("What was checked to confirm this mapping (e.g. hosting site list, live HTML marker).", maxItems=20),
                   "confirm_domain_mismatch": B("Set true only when the owner confirmed a domain that differs from the Knowledge Hub website.")},
            required=("domain", "environment"), output={"target": "{target_id, ...}"}, permissions=("collab:write", "client:read"), scope="client", read_only=False, category="coordination", timeout_s=15)
async def target_register(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    site = flat(client_context(ctx.admin(), cid))["website"]
    dom = host_key(p["domain"])
    if site and host_key(site) != dom and not p.get("confirm_domain_mismatch"):
        raise CollabError("DOMAIN_MISMATCH", f"{dom} is not the client's website on file ({host_key(site)}); confirm with the owner and pass confirm_domain_mismatch=true")
    if not site:
        ctx.limit("the client has no website in the Knowledge Hub, so the domain could not be cross-checked")
    if p.get("local_path") and not Path(p["local_path"]).is_dir():
        raise CollabError("NO_SUCH_FOLDER", "local_path does not exist on this machine")
    t = ctx.svc.store.put_target(client_id=cid, domain=p["domain"], environment=p["environment"], local_path=p.get("local_path", ""), hosting=p.get("hosting", ""),
                                 deploy_method=p.get("deploy_method", ""), rollback_method=p.get("rollback_method", ""), notes=p.get("notes", ""),
                                 verified_evidence=p.get("verified_evidence"), actor=ctx.caller)
    return {"target": t}


def _file_refs(local_path: str, pages: list[str]) -> list[dict]:
    """Candidate source files for each page URL under the target's folder (existence checks only on computed paths; no directory listing)."""
    if not local_path or not Path(local_path).is_dir():
        return []
    root = Path(local_path)
    out = []
    for u in pages[:40]:
        path = urlsplit(u).path.strip("/")
        cands = []
        for base in ("", "site", "public_html", "public", "src"):
            b = root / base / path if path else root / base
            for name in ("index.php", "index.html", "index.htm"):
                cands.append(b / name)
            if path:
                cands += [root / base / (path + ".php"), root / base / (path + ".html")]
        found = [str(c) for c in cands if c.is_file()]
        out.append({"url": u, "candidates": found[:3]})
    return out


@capability("handoff.create", "Prepare an implementation handoff package for ONE client from stored research: client/domain/target, verified business context, the selected "
            "findings with evidence, page URLs, candidate source files, proposed changes, constraints, acceptance criteria, test requirements, approval requirements and rollback.",
            props={"finding_ids": A("Findings to implement (from a finding set / report).", maxItems=40), "source_artifact_id": S("Finding set or report the findings come from (default: latest finding set)."),
                   "target_id": S("Deployment target (default: the client's production target)."), "constraints": A("Extra constraints.", maxItems=20),
                   "notes": S("Context for the implementer.", maxLength=4000)},
            output={"handoff_id": "artifact id", "package": "the handoff package"}, permissions=("client:read", "collab:write"), scope="client", read_only=False, category="coordination", timeout_s=20)
async def handoff_create(ctx, p: dict) -> dict:
    cid = require_client(ctx)
    st = ctx.svc.store
    if p.get("source_artifact_id"):
        src = _own_artifact(ctx, p["source_artifact_id"])
        if src["status"] == "invalidated":
            raise CollabError("INVALIDATED", "that finding set was invalidated (its data was not the site's content); run a new audit")
    else:
        rows = [r for r in st.artifacts(client_id=cid, kind="finding_set", limit=20) if r["status"] != "invalidated"][:1]
        if not rows:
            raise CollabError("NO_FINDINGS", "no finding set for this client: run seo.audit_site first")
        src = st.get_artifact(rows[0]["artifact_id"])
    pool = src["body"].get("findings") or src["body"].get("recommendations") or []
    want = p.get("finding_ids") or [f["finding_id"] for f in pool if f["type"] == "defect"][:8]
    chosen = [f for f in pool if f["finding_id"] in want]
    unknown = [w for w in want if w not in {f["finding_id"] for f in pool}]
    if unknown:
        raise CollabError("UNKNOWN_FINDINGS", f"not in {src['artifact_id']}: {unknown}")
    if not chosen:
        raise CollabError("NO_FINDINGS", "nothing selected to implement")
    targets = st.targets(cid)
    tgt = next((t for t in targets if t["target_id"] == p.get("target_id")), None) if p.get("target_id") else next((t for t in targets if t["environment"] == "production"), None)
    if p.get("target_id") and tgt is None:
        raise CollabError("NO_SUCH_TARGET", "that target is not registered for this client")
    cdoc = client_context(ctx.admin(), cid, include_unverified=True)
    info = flat(cdoc)
    pages = list(dict.fromkeys(u for f in chosen for u in f["pages"] if u))
    package = {
        "client": cdoc["client"], "domain": tgt["domain"] if tgt else host_key(info["website"]),
        "target": {k: tgt[k] for k in ("target_id", "environment", "local_path", "hosting", "deploy_method", "rollback_method", "notes")} if tgt else None,
        "business_context": {"summary": info, "unverified_fields": cdoc["status"]["unverified"], "rule": "use only VERIFIED values in published copy"},
        "research": {"source_artifact_id": src["artifact_id"], "source_kind": src["kind"], "request_id": (src["links"] or {}).get("request_id"),
                     "research_session_id": src["body"].get("research_session_id"), "evidence_ids": sorted({e.get("evidence_id") for f in chosen for e in f.get("evidence", []) if e.get("evidence_id")})},
        "findings": chosen, "pages": pages, "file_references": _file_refs(tgt["local_path"] if tgt else "", pages),
        "proposed_changes": [{"finding_id": f["finding_id"], "change": f["recommendation"], "pages": f["pages"][:10]} for f in chosen],
        "constraints": DEFAULT_CONSTRAINTS + list(p.get("constraints") or []),
        "acceptance_criteria": [{"finding_id": f["finding_id"], "criterion": f["verification"]} for f in chosen] +
                               [{"finding_id": "*", "criterion": "verify.page passes on every changed page of the live site after deployment"}],
        "test_requirements": ["Run the project's own checks (syntax/lint/build) on every changed file.", "Render each changed page locally (dev server/preview) and confirm HTTP 200 and the intended markup.",
                              "Review the diff: only files tied to the listed findings changed.", "After deploy: verify.page with assertions per finding; crawl the changed pages for regressions."],
        "approval_requirements": [{"action_kind": "production_deploy", "when": "before publishing to the production target", "how": "approval.request; the owner decides in the Knowledge Hub (Approvals)"}]
                                 if (not tgt or tgt["environment"] == "production") else [],
        "rollback": {"method": (tgt or {}).get("rollback_method") or "restore the pre-deploy backup of the changed files", "requirement": "take a backup/checkpoint of every file before changing or deploying it"},
        "notes": p.get("notes", ""), "status": "ready",
    }
    if not tgt:
        ctx.limit("no deployment target is registered for this client: register one (target.register) before implementation")
        ctx.outcome.completeness = "partial"
    a = st.add_artifact(task_id=ctx.task_id, client_id=cid, kind="handoff", stage="reasoning", title=f"Handoff: {len(chosen)} finding(s) for {package['domain']}", body=package,
                        links={"source": src["artifact_id"], "request_id": ctx.request_id}, status="ready", created_by=ctx.caller)
    return {"handoff_id": a["artifact_id"], "package": package}


@capability("handoff.get", "Retrieve an implementation handoff package (scope-checked).", props={"handoff_id": S("Handoff artifact id.", minLength=1)}, required=("handoff_id",),
            output={"package": "...", "status": "ready | in_progress | done"}, permissions=("client:read",), scope="client", category="coordination", timeout_s=10)
async def handoff_get(ctx, p: dict) -> dict:
    a = _own_artifact(ctx, p["handoff_id"], "handoff")
    return {"handoff_id": a["artifact_id"], "status": a["status"], "package": a["body"], "created_at": a["created_at"]}


@capability("approval.request", "Ask the OWNER to approve a high-impact action (production deploy, publishing, profile changes, messages, budgets, deletions, credentials, destructive "
            "database changes). Creates a PROPOSED approval; only the owner can approve it, in the Knowledge Hub. This capability cannot approve anything.",
            props={"action_kind": S("What kind of action.", enum=list(ACTION_KINDS)), "title": S("One-line summary.", minLength=5, maxLength=300),
                   "description": S("What will happen, why, risk, rollback.", maxLength=4000), "details": O("Structured details (files, diff summary, URLs...)."),
                   "artifact_id": S("Related change/handoff artifact.")},
            required=("action_kind", "title"), output={"approval_id": "id", "status": "proposed", "how_to_decide": "where the owner approves"},
            permissions=("approval:request",), scope="client_optional", read_only=False, category="coordination", timeout_s=15)
async def approval_request(ctx, p: dict) -> dict:
    _cap_body(p.get("details"))
    details = dict(p.get("details") or {})
    if p.get("artifact_id"):
        linked = _own_artifact(ctx, p["artifact_id"])
        if linked["kind"] == "change":
            b = linked["body"]
            details.setdefault("change", {"summary": b.get("summary"), "files": b.get("files"), "diff_summary": b.get("diff_summary"), "tests": b.get("tests"),
                                           "skipped_tests": b.get("skipped_tests"), "backup": b.get("backup"), "target": b.get("target"), "status": linked["status"]})
            if p["action_kind"] == "production_deploy" and linked["status"] not in ("tested", "ready_to_deploy"):
                raise CollabError("NOT_READY", f"the change is {linked['status']}: only a tested change can be proposed for production")
    ap = ctx.svc.store.request_approval(task_id=ctx.task_id, client_id=ctx.client_id, action_kind=p["action_kind"], title=p["title"], description=p.get("description", ""),
                                        details=details, requested_by=ctx.caller, artifact_id=p.get("artifact_id"))
    if p.get("artifact_id"):
        ctx.svc.store.update_artifact(p["artifact_id"], links={"approval_id": ap["approval_id"]}, actor=ctx.caller)
    return {"approval_id": ap["approval_id"], "status": ap["status"], "how_to_decide": "Knowledge Hub → Approvals (http://127.0.0.1:8765/hub/#/approvals) or: python -m octg_agent collab approvals"}


@capability("approval.status", "Read the state of an approval this caller requested (proposed / approved / denied / executed / verified / failed / withdrawn).",
            props={"approval_id": S("Approval id.", minLength=1)}, required=("approval_id",), output={"approval": "{status, decided_by, decided_at, decision_note, ...}"},
            permissions=("approval:request",), scope="client_optional", category="coordination", timeout_s=10)
async def approval_status(ctx, p: dict) -> dict:
    ap = ctx.svc.store.get_approval(p["approval_id"])
    if ap["requested_by"] != ctx.caller:
        raise CollabError("NO_SUCH_APPROVAL", "no such approval", status=404)
    return {"approval": ap}


@capability("change.record", "Record an implemented (not yet deployed) change for ONE client: the handoff and findings it addresses, files changed, diff summary, tests run with "
            "results, backup location and status. Linked to the originating research through the handoff.",
            props={"handoff_id": S("Handoff it implements.", minLength=1), "finding_ids": A("Findings addressed.", maxItems=40), "summary": S("What changed.", minLength=3, maxLength=4000),
                   "files": A("Files changed (paths).", maxItems=200), "diff_summary": S("Diff stat / short diff.", maxLength=60000),
                   "tests": {"type": "array", "maxItems": 100, "items": {"type": "object"}, "description": "[{name, passed (bool), detail}]"},
                   "backup": S("Where the pre-change copy is.", maxLength=1000), "status": S("implemented | tested | ready_to_deploy", enum=["implemented", "tested", "ready_to_deploy"])},
            required=("handoff_id", "summary", "files", "tests", "status"), output={"change_id": "artifact id", "all_tests_passed": "bool"},
            permissions=("collab:write", "client:read"), scope="client", read_only=False, category="coordination", timeout_s=15)
async def change_record(ctx, p: dict) -> dict:
    h = _own_artifact(ctx, p["handoff_id"], "handoff")
    fids = p.get("finding_ids") or [f["finding_id"] for f in h["body"]["findings"]]
    unknown = [f for f in fids if f not in {x["finding_id"] for x in h["body"]["findings"]}]
    if unknown:
        raise CollabError("UNKNOWN_FINDINGS", f"not part of handoff {h['artifact_id']}: {unknown}")
    tests = p["tests"]
    bad = [t for t in tests if not isinstance(t.get("name"), str) or not (isinstance(t.get("passed"), bool) or t.get("skipped") is True)]
    if bad or not tests:
        raise CollabError("BAD_TESTS", "tests must be a non-empty list of {name: str, passed: bool, detail?} or {name, skipped: true, detail: why + where it is checked instead}")
    skipped = [t for t in tests if t.get("skipped") is True]
    if any(len(str(t.get("detail") or "")) < 15 for t in skipped):
        raise CollabError("BAD_TESTS", "a skipped test needs a detail saying why it could not run and how it will be checked instead")
    run = [t for t in tests if t.get("skipped") is not True]
    if not run:
        raise CollabError("BAD_TESTS", "at least one test must actually run")
    passed = all(t["passed"] for t in run)
    if skipped:
        ctx.limit(f"{len(skipped)} test(s) skipped with a stated reason: " + "; ".join(t["name"] for t in skipped) + " (shown to the owner with the approval)")
    status = p["status"] if passed or p["status"] == "implemented" else "implemented"
    if p["status"] != status:
        ctx.limit("a change with failing tests cannot be 'tested' or 'ready_to_deploy'; recorded as implemented")
    body = {"handoff_id": h["artifact_id"], "finding_ids": fids, "summary": p["summary"], "files": p["files"], "diff_summary": p.get("diff_summary", ""), "tests": tests,
            "all_tests_passed": passed, "skipped_tests": skipped, "backup": p.get("backup", ""), "target": h["body"].get("target"), "domain": h["body"].get("domain"), "deployment": None, "verifications": []}
    a = ctx.svc.store.add_artifact(task_id=ctx.task_id, client_id=ctx.client_id, kind="change", stage="implementation", title=f"Change: {p['summary'][:120]}", body=body,
                                   links={"handoff": h["artifact_id"], "request_id": ctx.request_id}, status=status, created_by=ctx.caller)
    ctx.svc.store.update_artifact(h["artifact_id"], status="in_progress", links={"changes": (h["links"].get("changes") or []) + [a["artifact_id"]]}, actor=ctx.caller)
    return {"change_id": a["artifact_id"], "status": status, "all_tests_passed": passed, "skipped_tests": [t["name"] for t in skipped]}


@capability("change.mark_deployed", "Report that a recorded change was deployed, with the hosting operation's result. For a production target this is REFUSED unless the linked "
            "production_deploy approval is APPROVED by the owner; the approval then moves to EXECUTED. Verification is a separate step (verify.page).",
            props={"change_id": S("Change artifact.", minLength=1), "approval_id": S("The approved production_deploy approval."), "operation": S("Hosting operation used.", maxLength=300),
                   "succeeded": B("Did the hosting operation report success?"), "detail": S("Hosting response summary.", maxLength=4000)},
            required=("change_id", "operation", "succeeded"), output={"change": "status deployed | deploy_failed", "approval": "status executed | failed"},
            permissions=("collab:write", "approval:request"), scope="client", read_only=False, category="coordination", timeout_s=15)
async def change_mark_deployed(ctx, p: dict) -> dict:
    st = ctx.svc.store
    ch = _own_artifact(ctx, p["change_id"], "change")
    if ch["status"] not in ("tested", "ready_to_deploy", "deploy_failed"):
        raise CollabError("NOT_READY", f"change is {ch['status']}: only a tested change can be deployed")
    production = ((ch["body"].get("target") or {}).get("environment") or "production") == "production"
    ap = None
    if production:
        if not p.get("approval_id"):
            raise CollabError("APPROVAL_REQUIRED", "production deploys need an approved production_deploy approval (approval.request, then the owner decides)", status=403)
        ap = st.get_approval(p["approval_id"])
        if ap["requested_by"] != ctx.caller or ap["client_id"] != ctx.client_id or ap["action_kind"] != "production_deploy":
            raise CollabError("APPROVAL_MISMATCH", "that approval is not a production_deploy approval for this client", status=403)
        if ap["artifact_id"] and ap["artifact_id"] != ch["artifact_id"]:
            raise CollabError("APPROVAL_MISMATCH", "that approval was granted for a different change", status=403)
        if ap["status"] != "approved":
            raise CollabError("APPROVAL_REQUIRED", f"the approval is {ap['status']}, not approved by the owner", status=403)
        ap = st.transition_approval(ap["approval_id"], "executed" if p["succeeded"] else "failed", actor=ctx.caller,
                                    data={"change_id": ch["artifact_id"], "operation": p["operation"], "succeeded": p["succeeded"], "detail": p.get("detail", "")})
    body = dict(ch["body"])
    body["deployment"] = {"operation": p["operation"], "succeeded": p["succeeded"], "detail": p.get("detail", ""), "at": time.time(), "approval_id": p.get("approval_id")}
    ch = st.update_artifact(ch["artifact_id"], status="deployed" if p["succeeded"] else "deploy_failed", body=body, actor=ctx.caller)
    return {"change": {"change_id": ch["artifact_id"], "status": ch["status"]}, "approval": {"approval_id": ap["approval_id"], "status": ap["status"]} if ap else None}


def _same_link(href: str, target: str) -> bool:
    """A root-relative target ('/x.php') matches only that exact PATH on any host; an absolute target must match the URL (fragment/trailing slash ignored)."""
    h = href.split("#")[0]
    if target.startswith("/"):
        return (urlsplit(h).path.rstrip("/") or "/") == (target.split("#")[0].rstrip("/") or "/")
    return h.rstrip("/") == target.split("#")[0].rstrip("/")


CHECK_TYPES = ("status", "title_equals", "title_contains", "meta_description_contains", "meta_description_min_length", "h1_count", "h1_contains", "canonical_equals", "text_contains",
               "text_absent", "jsonld_type", "not_noindex", "header_contains", "final_url_equals", "max_redirects", "img_alt_complete", "tel_link_present", "link_present", "link_absent")


def _check(c: dict, r: F.FetchResult, sig: dict | None) -> dict:
    t, v = c.get("type"), c.get("value")
    s = sig or {}
    actual, ok = None, False
    if t == "status":
        actual, ok = r.status, r.status == int(v if v is not None else 200)
    elif t == "final_url_equals":
        actual, ok = r.final_url, r.final_url.rstrip("/") == str(v).rstrip("/")
    elif t == "max_redirects":
        actual, ok = len(r.chain), len(r.chain) <= int(v)
    elif t == "header_contains":
        actual = r.headers.get(str(c.get("header", "")).lower(), "")
        ok = str(v).lower() in actual.lower()
    elif sig is None:
        actual, ok = "not an HTML page", False
    elif t == "title_equals":
        actual, ok = s["title"], s["title"] == v
    elif t == "title_contains":
        actual, ok = s["title"], str(v).lower() in s["title"].lower()
    elif t == "meta_description_contains":
        actual, ok = s["meta_description"], str(v).lower() in s["meta_description"].lower()
    elif t == "meta_description_min_length":
        actual, ok = len(s["meta_description"]), len(s["meta_description"]) >= int(v)
    elif t == "h1_count":
        actual, ok = len(s["h1"]), len(s["h1"]) == int(v)
    elif t == "h1_contains":
        actual, ok = s["h1"], any(str(v).lower() in h.lower() for h in s["h1"])
    elif t == "canonical_equals":
        actual, ok = s["canonical"], s["canonical"].rstrip("/") == str(v).rstrip("/")
    elif t == "text_contains":
        actual, ok = None, str(v).lower() in s["text"].lower()
    elif t == "text_absent":
        actual, ok = None, str(v).lower() not in s["text"].lower()
    elif t == "jsonld_type":
        actual, ok = s["jsonld_types"], v in s["jsonld_types"]
    elif t == "not_noindex":
        xr = r.headers.get("x-robots-tag", "")
        actual, ok = {"meta_robots": s["meta_robots"], "x_robots_tag": xr}, not s["noindex"] and "noindex" not in xr.lower()
    elif t == "img_alt_complete":
        actual, ok = s["images_missing_alt"], s["images_missing_alt"] == 0
    elif t == "tel_link_present":
        actual, ok = s["tel_links"], bool(s["tel_links"]) if v is None else any(str(v) in x.replace("-", "").replace(" ", "") for x in s["tel_links"])
    elif t in ("link_present", "link_absent"):
        links = [l["url"] for l in s.get("links_internal", []) + s.get("links_external", [])]
        hit = any(_same_link(l, str(v)) for l in links)
        actual, ok = None, hit if t == "link_present" else not hit
    return {"type": t, "expected": v, "actual": actual, "passed": bool(ok), **({"header": c["header"]} if c.get("header") else {})}


@capability("verify.page", "Verify a live page against explicit expectations (status, title, description, H1, canonical, text, schema, noindex, headers, redirects, alt text, links). "
            "With change_id, records a VERIFICATION artifact on that change, marks the change verified/failed, moves its executed approval to VERIFIED when everything passes, "
            "and sets a follow-up measurement date.",
            props={"url": S("Page URL.", minLength=1, maxLength=2000), "checks": {"type": "array", "maxItems": 40, "items": {"type": "object"},
                                                                                  "description": f"[{{type, value?, header?}}], type one of {list(CHECK_TYPES)}"},
                   "change_id": S("Change this verifies."), "finding_ids": A("Findings these checks prove fixed.", maxItems=40), "follow_up_days": I("Measure again after N days (default 28).", minimum=1, maximum=180),
                   "cache_bust": B("Append a cache-busting query parameter (default true)."),
                   "mode": S("auto (default: HTTP, or the real browser when the site serves bot-protection pages to scripts) | http | browser", enum=["auto", "http", "browser"])},
            required=("url", "checks"), output={"passed": "bool", "checks": "[{type, expected, actual, passed}]", "verification_id": "artifact id (with change_id)", "follow_up_on": "date"},
            permissions=("net:fetch", "collab:write"), scope="client_optional", read_only=False, category="coordination", timeout_s=60, limitations=("Checks the raw HTML as served to a crawler.",))
async def verify_page(ctx, p: dict) -> dict:
    bad = [c for c in p["checks"] if c.get("type") not in CHECK_TYPES]
    if bad or not p["checks"]:
        raise CollabError("BAD_CHECKS", f"each check needs a type from {list(CHECK_TYPES)}")
    url = p["url"]
    mode = p.get("mode", "auto")
    fetch_url = url + (("&" if "?" in url else "?") + f"octg_verify={int(time.time())}") if p.get("cache_bust", True) else url
    r = None
    if mode != "browser":
        try:
            r = await F.fetch(fetch_url)
        except F.FetchError as e:
            raise fetch_error(e) from None
        if r.challenge and mode == "http":
            raise CollabError("SITE_CHALLENGE", "the site answered with a bot-protection page; nothing was verified (use mode auto/browser)")
    if r is not None and not r.challenge:
        sig = extract(r.text(), r.final_url) if r.is_html else None
        results = [_check(c, r, sig) for c in p["checks"]]
        ev = ctx.evidence("verification_fetch", {"http": r.summary(), "signals": {k: v for k, v in (sig or {}).items() if k != "text"}, "checks": results}, source_url=r.final_url,
                          summary=f"{sum(bool(x['passed']) for x in results)}/{len(results)} checks passed")
        ctx.evidence("verification_html", r.body, source_url=r.final_url, summary=f"HTTP {r.status}", ext="html")
        ctx.source("http", r.final_url, r.fetched_at)
        final_url, status, via = r.final_url, r.status, "http"
    else:
        if r is not None:
            ctx.limit(f"plain HTTP got a bot-protection page (HTTP {r.status}); verified through the real browser instead")
        async with ctx.svc._browser_lock:
            results, ev, final_url = await _verify_in_browser(ctx, fetch_url, p["checks"])
        status, via = None, "browser"
    measured = [x for x in results if x["passed"] is not None]
    passed = bool(measured) and all(x["passed"] for x in measured)
    if len(measured) < len(results):
        ctx.limit(f"{len(results) - len(measured)} check(s) cannot be measured in browser mode and were not counted: " + ", ".join(x["type"] for x in results if x["passed"] is None))
        ctx.outcome.completeness = "partial"
    follow = time.time() + 86400 * p.get("follow_up_days", 28)
    out = {"url": url, "final_url": final_url, "status": status, "via": via, "passed": passed, "checks": results, "evidence_id": ev}
    if p.get("change_id"):
        st = ctx.svc.store
        ch = _own_artifact(ctx, p["change_id"], "change")
        ver = st.add_artifact(task_id=ctx.task_id, client_id=ctx.client_id or ch["client_id"], kind="verification", stage="verification", title=f"Verification of {url}: {'passed' if passed else 'FAILED'}",
                              body={"change_id": ch["artifact_id"], "url": url, "via": via, "checks": results, "passed": passed, "finding_ids": p.get("finding_ids") or ch["body"].get("finding_ids"),
                                    "deployed": (ch["body"].get("deployment") or {}).get("succeeded"), "follow_up_on": follow, "evidence_id": ev,
                                    "note": "A passing technical check is not evidence of a ranking change; compare search data after the follow-up date."},
                              links={"change": ch["artifact_id"], "request_id": ctx.request_id, "evidence": [ev]}, status="passed" if passed else "failed", created_by=ctx.caller)
        body = dict(ch["body"])
        body["verifications"] = (body.get("verifications") or []) + [{"verification_id": ver["artifact_id"], "url": url, "passed": passed, "at": time.time()}]
        all_ok = all(v["passed"] for v in body["verifications"])
        new_status = ch["status"]
        if ch["status"] in ("deployed", "verified", "verification_failed"):
            new_status = "verified" if all_ok else "verification_failed"
        st.update_artifact(ch["artifact_id"], status=new_status, body=body, actor=ctx.caller)
        apid = (body.get("deployment") or {}).get("approval_id")
        if apid and all_ok:
            ap = st.get_approval(apid)
            if ap["status"] == "executed":
                st.transition_approval(apid, "verified", actor=ctx.caller, data={"verification_id": ver["artifact_id"], "url": url})
        out.update(verification_id=ver["artifact_id"], change_status=new_status, follow_up_on=follow)
    return out


BROWSER_CHECKS = ("title_equals", "title_contains", "h1_count", "h1_contains", "text_contains", "text_absent", "tel_link_present", "link_present", "link_absent", "final_url_equals")


async def _verify_in_browser(ctx, url: str, checks: list[dict]):
    """The same checks against the page as the owner's Chrome renders it. Checks a browser cannot see (status code, headers, head metadata, schema) are NOT
    MEASURED (passed=None), never guessed."""
    from ...seo.fetch import CHALLENGE_RE
    if ctx.rt.session is None:
        raise CollabError("EXTENSION_NOT_CONNECTED", "the site needs the real browser to verify and the Chrome extension is not connected", retryable=True)
    tab = await ctx.open_tab(url)
    st = await ctx.browser("get_page_state", {"tab_id": tab})
    page = ((st.get("result") or {}).get("page")) or {}
    if CHALLENGE_RE.search((page.get("title") or "").lower()):
        await ctx.browser("wait", {"tab_id": tab, "ms": 6000})
        st = await ctx.browser("get_page_state", {"tab_id": tab})
        page = ((st.get("result") or {}).get("page")) or {}
        if CHALLENGE_RE.search((page.get("title") or "").lower()):
            raise CollabError("SITE_CHALLENGE", "the real browser was shown a bot-protection challenge; it was not bypassed and nothing was verified")
    rd = await ctx.browser("read_page", {"tab_id": tab, "scope": "page", "max_chars": 60000})
    ins = await ctx.browser("inspect_page", {"tab_id": tab, "max_elements": 1000, "viewport_only": False})
    text = ((rd.get("result") or {}).get("text") or "")
    hrefs = [e.get("href") or "" for e in (((ins.get("result") or {}).get("elements")) or []) if e.get("tag") == "a" and e.get("href")]
    title = page.get("title") or ""
    h1 = [h["text"] for h in (page.get("headings") or []) if h.get("level") == 1]
    final = page.get("url") or url
    out = []
    for c in checks:
        t, v = c.get("type"), c.get("value")
        if t not in BROWSER_CHECKS:
            out.append({"type": t, "expected": v, "actual": "not measurable in browser mode", "passed": None})
            continue
        if t == "title_equals":
            actual, ok = title, title == v
        elif t == "title_contains":
            actual, ok = title, str(v).lower() in title.lower()
        elif t == "h1_count":
            actual, ok = len(h1), len(h1) == int(v)
        elif t == "h1_contains":
            actual, ok = h1, any(str(v).lower() in h.lower() for h in h1)
        elif t == "text_contains":
            actual, ok = None, str(v).lower() in text.lower()
        elif t == "text_absent":
            actual, ok = None, str(v).lower() not in text.lower()
        elif t == "tel_link_present":
            tels = [h[4:] for h in hrefs if h.lower().startswith("tel:")]
            actual, ok = tels[:5], bool(tels) if v is None else any(str(v) in x.replace("-", "").replace(" ", "") for x in tels)
        elif t in ("link_present", "link_absent"):
            target = str(v)
            hit = any(_same_link(h, target) for h in hrefs)
            actual, ok = None, hit if t == "link_present" else not hit
        else:  # final_url_equals
            actual, ok = final, final.split("?")[0].rstrip("/") == str(v).rstrip("/")
        out.append({"type": t, "expected": v, "actual": actual, "passed": bool(ok)})
    ev = ctx.evidence("verification_browser", {"url": final, "title": title, "h1": h1, "links": sorted(set(hrefs))[:500], "text": text[:30000], "checks": out}, source_url=final,
                      summary=f"browser: {sum(1 for x in out if x['passed'])}/{len(out)} checks passed")
    ctx.source("browser", final)
    await ctx.close_tabs()
    return out, ev, final


@capability("agent.browser_task", "Hand a natural-language browser instruction to the agent's own AI task loop and wait for it. The loop's own approval gate still applies "
            "(risky clicks wait for the owner in the side panel). Not granted to collaborators by default.",
            props={"instruction": S("What the local agent should do.", minLength=3, maxLength=2000), "max_steps": I("Step cap.", minimum=1, maximum=60)},
            required=("instruction",), output={"task": "the local task summary (status, result, steps)"}, permissions=("agent:task",), read_only=False, category="agent",
            needs_browser=True, timeout_s=600, max_timeout_s=1800, limitations=("Uses whichever model provider the agent is configured for.",))
async def browser_task(ctx, p: dict) -> dict:
    from ...brain.manager import TaskError
    try:
        t = await ctx.rt.tasks.start(p["instruction"], max_steps=p.get("max_steps"))
    except TaskError as e:
        raise CollabError(e.code, e.message, retryable=e.code == "TASK_ALREADY_RUNNING") from None
    try:
        while True:
            d = ctx.rt.tasks.get(t.task_id) or {}
            if d.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
                return {"task": d}
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        try:
            ctx.rt.tasks.stop(t.task_id)
        except TaskError:
            pass
        raise


@capability("artifact.invalidate", "Mark a research artifact (e.g. a finding set) INVALID when its data turned out not to describe reality (blocked crawl, wrong site...). "
            "It stays in the record with the reason; reports and handoffs stop using it.",
            props={"artifact_id": S("Artifact to invalidate.", minLength=1), "reason": S("Why it is invalid, with the evidence.", minLength=10, maxLength=2000)},
            required=("artifact_id", "reason"), output={"artifact_id": "id", "status": "invalidated"}, permissions=("collab:write",), scope="client_optional", read_only=False,
            category="coordination", timeout_s=15)
async def artifact_invalidate(ctx, p: dict) -> dict:
    a = _own_artifact(ctx, p["artifact_id"])
    if a["kind"] not in ("finding_set", "analysis", "report", "handoff"):
        raise CollabError("WRONG_KIND", "only research/analysis artifacts can be invalidated; changes and verifications are historical records")
    body = dict(a["body"] or {})
    body["invalidation"] = {"reason": p["reason"], "by": ctx.caller, "at": time.time(), "request_id": ctx.request_id}
    a = ctx.svc.store.update_artifact(a["artifact_id"], status="invalidated", body=body, actor=ctx.caller)
    sid = body.get("research_session_id")
    if sid:
        s = ctx.admin().research.get_session(sid)
        if s["client_id"] == a["client_id"]:
            ctx.admin().research.invalidate_session(sid, p["reason"], actor=f"collab:{ctx.caller}")
    return {"artifact_id": a["artifact_id"], "status": a["status"], "research_session_invalidated": sid or None}
