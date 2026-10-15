"""Build the SEO report (structured data + readable Markdown) from STORED research only. It never fetches anything and never invents a number:
every figure comes from an observation, a finding or the client's Knowledge Hub record, and every gap is listed under "missing data"."""
from __future__ import annotations

import json
from datetime import datetime

from .signals import host_key

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
FRESH_HOURS = 12.0      # a search observation is "fresh" when it was taken this close to the moment the report is built
UNAVAILABLE = "unavailable"


def _d(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _esc(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _query_for(k: dict) -> str:
    """The query text seo.serp_observe builds for a campaign keyword (so keywords can be matched to their observations)."""
    loc = k.get("location")
    return k["keyword"] if not loc or loc.split(",")[0].lower() in k["keyword"].lower() else f"{k['keyword']} {loc}"


def _basis(f: dict) -> str:
    return "inferred hypothesis" if f.get("type") == "hypothesis" or f.get("confidence") == "hypothesis" else "measured on site"


def _age(seconds: float) -> str:
    h = max(seconds, 0) / 3600
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


def build_report(*, client: dict, summary: dict, campaign: dict | None, audit: dict | None, competitors: dict | None, serp: list[dict], connectors: dict | None,
                 generated_at: float, request_id: str = "", performance: dict | None = None, attempts: list[dict] | None = None, fresh_hours: float = FRESH_HOURS) -> dict:
    """`attempts`: searches that were tried but gave no usable result ([{query, at, outcome}]); they are never observations, only context for "unavailable"."""
    name = client["client"]["name"]
    findings = (audit or {}).get("body", {}).get("findings", []) if audit else []
    missing: list[str] = []
    comps_body = (competitors or {}).get("body", {})
    comp_obs = comps_body.get("observations") or comps_body.get("comparison") or []
    # ---------------------------------------------------------------- keyword observations (baseline)
    observed = {}
    for o in sorted([o for o in serp if o["kind"] == "keyword_rank"], key=lambda o: o["query"]):
        d = json.loads(o["data"] or "{}")
        observed[o["query"].lower()] = {"query": o["query"], "location": o["location"], "engine": o["search_engine"], "observed_at": o["observed_at"], "client_position": o["position"],
                                        "checked_results": d.get("checked_results"), "client_url": o["url"], "evidence": o["evidence"]}
    planned = [_query_for(k) for k in (campaign or {}).get("keywords", []) if k.get("area_status") != "outside"]
    tries: dict[str, list[dict]] = {}
    for a in attempts or []:
        tries.setdefault(a["query"].lower(), []).append(a)
    fresh_s = fresh_hours * 3600
    kw_rows = []
    for q in sorted(dict.fromkeys(planned + [r["query"] for r in observed.values()]), key=str.lower):
        r = observed.get(q.lower())
        last_try = max(tries.get(q.lower(), []), key=lambda a: a["at"], default=None)
        if r:
            age = generated_at - r["observed_at"]
            note = f"a later attempt on {_d(last_try['at'])} gave no usable result ({last_try['outcome']})" if last_try and last_try["at"] > r["observed_at"] else ""
            kw_rows.append({**r, "status": "fresh" if age <= fresh_s else "older", "age_hours": round(age / 3600, 1), "note": note})
        else:
            kw_rows.append({"query": q, "location": None, "engine": None, "observed_at": None, "client_position": None, "checked_results": None, "client_url": "", "evidence": "",
                            "status": UNAVAILABLE, "age_hours": None,
                            "note": (f"no usable observation: the attempt on {_d(last_try['at'])} {last_try['outcome']}" if last_try else "no usable observation: this keyword has not been searched")})
    got = [k for k in kw_rows if k["status"] != UNAVAILABLE]
    n_fresh = sum(1 for k in kw_rows if k["status"] == "fresh")
    n_older = sum(1 for k in kw_rows if k["status"] == "older")
    n_unavail = len(kw_rows) - len(got)
    coverage_complete = bool(kw_rows) and n_fresh == len(kw_rows)
    pack_rows = [{"query": o["query"], "observed_at": o["observed_at"], "client_in_pack": json.loads(o["data"] or "{}").get("client_in_pack"), "names": json.loads(o["data"] or "{}").get("names", [])}
                 for o in serp if o["kind"] == "local_pack_result"]
    top_domains: dict[str, dict] = {}
    for o in serp:
        if o["kind"] != "serp_result":
            continue
        cls = json.loads(o["data"] or "{}").get("class")
        e = top_domains.setdefault(o["domain"], {"domain": o["domain"], "class": cls, "appearances": 0, "best": 99})
        e["appearances"] += 1
        e["best"] = min(e["best"], o["position"] or 99)
    top_list = sorted(top_domains.values(), key=lambda e: (-e["appearances"], e["best"]))[:12]
    seen_dom: dict[str, dict] = {}
    for o in serp:
        if o["kind"] == "serp_result" and o["domain"]:
            e = seen_dom.setdefault(host_key(o["domain"]), {"queries": set(), "best": None})
            e["queries"].add(o["query"].lower())
            if o["position"]:
                e["best"] = o["position"] if e["best"] is None else min(e["best"], o["position"])
    if not serp:
        missing.append("No search-results observations: keyword positions are unknown (run seo.serp_observe).")
    # ---------------------------------------------------------------- data sources
    srcs = [{"source": "Knowledge Hub (client record)", "status": "used", "detail": f"{len(client['facts'])} facts, {len(client['services'])} services, {len(client['locations'])} locations"}]
    if audit:
        b = audit["body"]
        browser = b.get("mode") == "browser"
        srcs.append({"source": "Website crawl (" + ("real browser" if browser else "HTTP") + ")", "status": "used",
                     "detail": f"{len(b.get('pages', []))} pages of {b.get('site')} on {_d(b.get('crawled_at'))}" + (" (plain HTTP was refused by the site's bot protection)" if b.get("http_block") else "")})
        if b.get("not_measured"):
            missing.append("Not measured in browser mode (so no findings about them): " + ", ".join(b["not_measured"]) + ".")
    else:
        missing.append("No site audit: technical findings are unknown (run seo.audit_site).")
    if serp:
        engines = sorted({o["search_engine"] for o in serp})
        dates = [k["observed_at"] for k in got]
        srcs.append({"source": "Search results in the owner's browser", "status": "partial" if not coverage_complete else "used",
                     "detail": f"{len(got)} of {len(kw_rows)} campaign keywords observed on {', '.join(engines)} ({n_fresh} fresh, {n_older} older" +
                               (f"; observation dates {_d(min(dates))} to {_d(max(dates))}" if dates else "") + f"), {n_unavail} unavailable"})
    if kw_rows and not coverage_complete:
        weak = [f"'{k['query']}' ({k['status']}" + (f", {k['note']}" if k["status"] == UNAVAILABLE else f", observed {_d(k['observed_at'])}") + ")" for k in kw_rows if k["status"] != "fresh"]
        missing.append(f"Search coverage is incomplete: only {n_fresh} of {len(kw_rows)} keywords have a fresh observation (taken within {fresh_hours:g} h of this report). Not fresh: " + "; ".join(weak) + ".")
    if not client["services"] and not client["locations"]:
        missing.append("The Knowledge Hub has no services or service areas for this client, so keyword locations could not be checked against real service areas.")
    comp_body = (competitors or {}).get("body", {})
    comps_raw = comp_body.get("competitors", [])
    comp_ok = [c for c in comps_raw if not c.get("error")]
    comp_bad = list(comp_body.get("unreadable") or []) + [c for c in comps_raw if c.get("error")]
    if competitors:
        via = {}
        for c in comp_ok:
            via[c.get("via") or "http"] = via.get(c.get("via") or "http", 0) + 1
        srcs.append({"source": "Competitor pages", "status": "used" if comp_ok else "unavailable",
                     "detail": f"{len(comp_ok)} read (" + ", ".join(f"{n} via {v}" for v, n in sorted(via.items())) + ")" + (f"; {len(comp_bad)} could not be read: " + ", ".join(f"{c['domain']} ({c.get('error')})" for c in comp_bad) if comp_bad else "")})
    elif serp:
        missing.append("No competitor review (run seo.competitors).")
    comps = []
    for c in comp_ok:
        seen = seen_dom.get(host_key(c["domain"]))
        if seen:
            comps.append({**c, "searches_seen_in": len(seen["queries"]), "best_position_observed": seen["best"]})
        elif c.get("keywords") and c.get("best_position") not in (None, 99):   # a review made by seo.competitors carries its own positions
            comps.append({**c, "searches_seen_in": len(c["keywords"]), "best_position_observed": c["best_position"]})
        else:
            comps.append({**c, "searches_seen_in": None, "best_position_observed": None})
    performance = performance or {}
    for s in (connectors or {}).get("sources", []):
        if s["source"] in ("google_search_console", "bing_webmaster_tools", "google_analytics_4"):
            if s["source"] in performance:   # first-party data was actually retrieved and is in this report
                pf = performance[s["source"]]
                srcs.append({"source": pf["label"] + " (first-party data)", "status": "used", "detail": f"property {pf['property']}, {pf['window']}"})
                continue
            cm = s.get("client_mapping") or {}
            why = cm.get("why_not") or s.get("missing", "")
            srcs.append({"source": s["source"], "status": "connected" if s["connected"] and cm.get("ready", True) else "NOT connected", "detail": why})
            if not (s["connected"] and cm.get("ready", True)):
                missing.append(f"{s['source'].replace('_', ' ').title()} is not connected for this client ({why or 'no data retrieved'}): no impressions/clicks/{'traffic' if 'analytics' in s['source'] else 'query'} data from it in this report.")
    if not connectors and not performance:
        missing.append("GSC / Bing Webmaster / Analytics status was not checked; no first-party performance data is included.")
    for src, pf in performance.items():   # first-party data that came back thin is said so
        if pf["totals"].get("impressions") == 0:
            missing.append(f"{pf['label']} returned no impressions for {pf['window']}: the property may be new or the site may not be shown for any query yet.")
    if client["status"]["unverified"]:
        missing.append(f"{len(client['status']['unverified'])} client field(s) are unverified in the Knowledge Hub.")
    if campaign:
        outside = [k for k in campaign.get("keywords", []) if k.get("area_status") == "outside"]
        if outside:
            missing.append(f"{len(outside)} keyword location(s) are outside the client's known service areas and need the owner's confirmation.")
    # ---------------------------------------------------------------- recommendations and tasks
    recs = sorted(findings, key=lambda f: (-f["priority_score"], SEV_ORDER.get(f["severity"], 9)))
    defects = [f for f in recs if f["type"] == "defect"]
    opps = [f for f in recs if f["type"] == "opportunity"]
    hyps = [f for f in recs if f["type"] == "hypothesis"]
    tasks = [{"task": f["recommendation"], "finding_id": f["finding_id"], "pages": f["pages"][:5], "effort": f["effort"], "acceptance": f["verification"],
              "needs_approval": "publishing site changes to production" if f["category"] != "local" or f["rule"] != "nap_phone_absent" else "owner confirmation of the correct phone first"}
             for f in (defects + opps)[:15]]
    success = [
        "Every implemented finding passes its stated verification check on the live site (recorded as a verification artifact).",
        "No regression: re-crawl shows no new 4xx/5xx, noindex or canonical problems.",
        f"Re-run the same {len(kw_rows)} search observations about 4-6 weeks after changes and compare with this baseline (same engine, same query wording; keywords that were unavailable here need a first valid observation)." if kw_rows else
        "Establish a search-results baseline before judging ranking changes.",
        ("Compare the first-party (GSC / Bing) impressions, clicks and average position for the target queries with the window in this report, using the same window length." if performance
         else "Once GSC (first-party search data) is connected: impressions and clicks for the target queries/pages vs. the 28 days before the change."),
    ]
    exec_lines = []
    if audit:
        bs = audit["body"].get("summary", {}).get("by_severity", {})
        exec_lines.append(f"The crawl of {audit['body'].get('site')} found {len(findings)} item(s): " + ", ".join(f"{n} {s}" for s, n in sorted(bs.items(), key=lambda x: SEV_ORDER.get(x[0], 9))) + ".")
    if kw_rows:
        exec_lines.append(f"Keyword search coverage: {n_fresh} of {len(kw_rows)} campaign keywords have a fresh observation (taken within {fresh_hours:g} h of this report), {n_older} only an older one (dated in the table), "
                          f"{n_unavail} none." + ("" if coverage_complete else " Coverage is NOT complete: do not read this as a full ranking picture."))
        for label, grp in (("fresh", [k for k in kw_rows if k["status"] == "fresh"]), ("older", [k for k in kw_rows if k["status"] == "older"])):
            hit = [k for k in grp if k["client_position"]]
            if grp:
                exec_lines.append(f"Among the {len(grp)} {label} observation(s) the client's site appeared in the organic results shown for {len(hit)}" +
                                  (f" (best position #{min(k['client_position'] for k in hit)})" if hit else "") + "; dated observations from one browser, not guarantees.")
    if pack_rows:
        exec_lines.append(f"A map pack appeared on {len(pack_rows)} of the {len(got)} observed search(es); the client's name was found among its links on {sum(1 for r in pack_rows if r['client_in_pack'])}.")
    if defects:
        exec_lines.append(f"Top confirmed fix: {defects[0]['title']} ({defects[0]['severity']}).")
    if missing:
        exec_lines.append(f"{len(missing)} data gap(s) limit this report (see Missing data).")
    data = {"performance": performance, "client": client["client"], "generated_at": generated_at, "request_id": request_id, "campaign": {k: (campaign or {}).get(k) for k in ("campaign_id", "name", "website", "objective",
            "language", "country", "status", "baseline_date", "baseline_session_id", "services", "locations")} if campaign else None,
            "executive_summary": exec_lines, "data_sources": srcs, "baseline": {"keywords": kw_rows, "local_pack": pack_rows, "domains_seen": top_list,
                         "coverage": {"keywords": len(kw_rows), "fresh": n_fresh, "older": n_older, "unavailable": n_unavail, "complete": coverage_complete, "fresh_within_hours": fresh_hours}},
            "competitors": comps, "competitors_unreadable": comp_bad, "competitor_observations": comp_obs,
            "technical_issues": [f for f in recs if f["category"] in ("technical", "indexability", "structured_data", "duplication")],
            "recommendations": recs, "missing_data": missing, "implementation_tasks": tasks, "success_criteria": success,
            "sources": {"audit_artifact": audit["artifact_id"] if audit else None, "competitor_artifact": competitors["artifact_id"] if competitors else None}}
    # ---------------------------------------------------------------- markdown
    L = [f"# SEO report — {name}", "", f"Generated {_d(generated_at)} from stored research (request {request_id}). Every figure below is a dated observation or a finding with evidence.", ""]
    L += ["## Executive summary", ""] + [f"- {x}" for x in exec_lines or ["Not enough data collected yet."]] + [""]
    L += ["## How to read this report", "",
          f"- **Fresh observation**: a search taken within {fresh_hours:g} h of this report.",
          "- **Older observation**: a valid, dated search that is older than that. It is shown with its own date and may have changed since.",
          "- **Unavailable**: the search could not be observed (CAPTCHA wall not bypassed, unreadable page, or never searched). No position is implied: this is not 'not ranking'.",
          "- **Measured on site**: a finding from the crawl, with its evidence. **Inferred hypothesis**: a prompt for review, not a measurement.", ""]
    L += ["## Client and campaign", "", f"- Client: **{name}** (`{client['client']['client_id']}`)", f"- Website: {summary.get('website') or '-'}",
          f"- Phone on file: {summary.get('phone') or '-'} · Location: {', '.join(x for x in (summary.get('city'), summary.get('state')) if x) or '-'}"]
    if campaign:
        L += [f"- Campaign: {campaign['name']} (`{campaign['campaign_id']}`), status {campaign['status']}, {campaign.get('language')}-{campaign.get('country')}",
              f"- Objective: {campaign.get('objective') or '-'}", f"- Target services: {', '.join(campaign.get('services') or []) or '-'}",
              f"- Target locations: {', '.join(campaign.get('locations') or []) or '-'}", f"- Baseline: {_d(campaign.get('baseline_date'))} (session {campaign.get('baseline_session_id') or '-'})"]
    L += ["", "## Data sources", "", "| Source | Status | Detail |", "|---|---|---|"] + [f"| {_esc(s['source'])} | {_esc(s['status'])} | {_esc(s['detail'])} |" for s in srcs] + [""]
    if performance:
        L += ["## Search performance (first-party data)", "", "_GSC and Bing report averages over many searches, places and devices, with rare queries omitted. They are not comparable with the single-browser positions below._", ""]
        for src, pf in performance.items():
            t = pf["totals"]
            L += [f"### {pf['label']} — {pf['window']} (property `{pf['property']}`)", "",
                  f"- Clicks **{t.get('clicks', 0)}**, impressions **{t.get('impressions', 0)}**" + (f", average position **{t['position']:.1f}**" if t.get("position") else "") + (f", CTR {t['ctr'] * 100:.1f}%" if t.get("ctr") else ""), ""]
            if pf["queries"]:
                L += ["| Query | Clicks | Impressions | Avg position |", "|---|---|---|---|"] + [f"| {_esc(q['query'])} | {q.get('clicks', 0)} | {q.get('impressions', 0)} | {('%.1f' % q['position']) if q.get('position') else '-'} |"
                                                                                           for q in pf["queries"][:15]] + [""]
    L += ["## Baseline: keyword observations", ""]
    if kw_rows:
        def _evidence(k):
            if k["status"] == "older":
                return f"OLDER ({_age(generated_at - k['observed_at'])} old)"
            return "FRESH" if k["status"] == "fresh" else "UNAVAILABLE"

        def _pos(k):
            if k["status"] == UNAVAILABLE:
                return UNAVAILABLE
            return ("#" + str(k["client_position"])) if k["client_position"] else "not in results shown (measured absence)"
        L += [f"Coverage: **{n_fresh} fresh, {n_older} older, {n_unavail} unavailable** of {len(kw_rows)} keywords" + (" (complete)." if coverage_complete else " (NOT complete)."), "",
              "| Query | Evidence | Observed | Engine | Client organic position | Results checked | Note |", "|---|---|---|---|---|---|---|"]
        L += [f"| {_esc(k['query'])} | {_evidence(k)} | {_d(k['observed_at']) if k['observed_at'] else UNAVAILABLE} | {k['engine'] or '-'} | {_pos(k)} | {k['checked_results'] or '-'} | {_esc(k['note'])} |" for k in kw_rows]
        if pack_rows:
            L += ["", "Map pack: " + "; ".join(f"'{r['query']}' (observed {_d(r['observed_at'])}): {'client listed' if r['client_in_pack'] else 'client not found'}" for r in pack_rows)]
        if top_list:
            L += ["", "Domains seen most often in those results:", ""] + [f"- {t['domain']} ({t['class']}): {t['appearances']}x, best #{t['best']}" for t in top_list]
    else:
        L += ["No search observations yet."]
    L += ["", "## Competitor findings", ""]
    if comps or comp_bad:
        L += ["| Domain | Best position seen | Searches seen in | Read via | Title | Words | Schema |", "|---|---|---|---|---|---|---|"]
        L += [f"| {_esc(c['domain'])} | {('#' + str(c['best_position_observed'])) if c['best_position_observed'] else UNAVAILABLE} | "
              f"{c['searches_seen_in'] if c['searches_seen_in'] else UNAVAILABLE} | {c.get('via') or '-'} | {_esc((c.get('title') or '')[:70])} | "
              f"{c['word_count'] if c.get('word_count') is not None else UNAVAILABLE} | {_esc(', '.join((c.get('schema') or [])[:3]) or 'none found' if c.get('schema') is not None else 'not measured (browser read)')} |" for c in comps]
        L += [f"| {_esc(c['domain'])} | {UNAVAILABLE} | {UNAVAILABLE} | {c.get('tried') or '-'} | could not be read ({_esc(c.get('error'))}) | {UNAVAILABLE} | {UNAVAILABLE} |" for c in comp_bad]
        L += ["", "_Positions come from the recorded search results of the keywords above (each from its newest valid observation). 'unavailable' means the search data does not show that site; it is not a ranking of zero._"]
        L += [""] + [f"- {o['observation']}" for o in comp_obs] + ["", "_Correlations from a small sample; not proof of why these pages rank._"]
    else:
        L += ["No competitor review yet."]
    L += ["", "## Prioritised recommendations", ""]
    if recs:
        L += ["| # | Finding | Basis | Type | Severity | Confidence | Effort | Pages |", "|---|---|---|---|---|---|---|---|"]
        L += [f"| {i} | {_esc(f['title'])} (`{f['finding_id']}`) | {_basis(f)} | {f['type']} | {f['severity']} | {f['confidence']} | {f['effort']} | {len(f['pages'])} |" for i, f in enumerate(recs[:30], 1)]
        L += ["", "### Details", ""]
        for f in recs[:30]:
            L += [f"**{f['title']}** — `{f['finding_id']}` · {f['category']} · {f['type']} · {f['severity']} / {f['confidence']}", "",
                  f"- Pages: " + (", ".join(f["pages"][:5]) + (f" (+{len(f['pages']) - 5} more)" if len(f["pages"]) > 5 else "") if f["pages"] else "-"),
                  f"- Evidence: " + "; ".join(f"{_esc(e.get('url', ''))}: {_esc(e.get('detail', ''))}" for e in f["evidence"][:4]),
                  f"- Recommendation: {f['recommendation']}", f"- Verify by: {f['verification']}"] + ([f"- Note: {f['notes']}"] if f.get("notes") else []) + [""]
    else:
        L += ["No findings (no audit run)."]
    L += ["## Missing data", ""] + ([f"- {m}" for m in missing] or ["- None noted."]) + [""]
    L += ["## Implementation tasks", ""] + ([f"{i}. {t['task']} (`{t['finding_id']}`, effort {t['effort']}) — accept when: {t['acceptance']}" for i, t in enumerate(tasks, 1)] or ["- None yet."]) + [""]
    L += ["## Success criteria", ""] + [f"- {s}" for s in success] + [""]
    return {"data": data, "markdown": "\n".join(L)}
