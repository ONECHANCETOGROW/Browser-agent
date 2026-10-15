"""Guided entry of a profile (the owner's own business, or one client) in the terminal.

It asks, field by field, and only ever stores what the USER types (source USER_ENTERED, so VERIFIED). A blank answer means "I don't have it / skip": nothing is
guessed, defaulted or copied from another field, scope or document. HIGH-sensitivity fields are read with a hidden prompt. Existing values are shown masked and
kept unless the user asks to change them; conflicts are reported, never resolved here. `ask` is injected so the flow is testable.
"""
from __future__ import annotations

from typing import Callable

from .schema import CATALOGS, ME, KeyError_, parse_key
from .store import BusinessStore, StoreError

# (key, repeatable). Order = what a first-time user most needs first. Only catalog fields; nothing outside the catalog is ever asked.
ME_ORDER: list[tuple[str, bool]] = [
    ("identity.legal_name", False), ("identity.trade_name", False), ("contact.website", False), ("contact.email", False), ("contact.phone", False),
    ("addresses.principal", False), ("identity.description", False), ("services.description", False), ("identity.constitution", False),
    ("registration.incorporation_date", False), ("registration.udyam", False), ("taxation.pan", False), ("taxation.gstin", False), ("taxation.tan", False),
    ("registration.cin", False), ("registration.llpin", False), ("owners[].name", True), ("social.profile[]", True),
]
CLIENT_ORDER: list[tuple[str, bool]] = [
    ("identity.business_name", False), ("identity.legal_name", False), ("identity.trade_name", False), ("identity.description", False),
    ("contact.website", False), ("contact.email", False), ("contact.phone", False), ("contact.person", False), ("location.address", False),
    ("services.primary[]", True), ("services.secondary[]", True), ("service_area.cities[]", True), ("local_seo.primary_category", False),
    ("local_seo.gbp_reference", False), ("seo.target_keywords[]", True), ("seo.target_locations[]", True), ("seo.goals[]", True), ("seo.current_status", False),
    ("marketing.social_profiles[]", True), ("account.status", False), ("account.onboarding_date", False), ("account.plan", False),
    ("taxation.pan", False), ("taxation.gstin", False), ("registration.udyam", False),
]


def order_for(scope: str) -> list[tuple[str, bool]]:
    return ME_ORDER if scope == ME else CLIENT_ORDER


def missing_fields(store: BusinessStore, scope: str = ME) -> list[dict]:
    """Catalog fields with no current value in this scope (first instance only for repeatable fields). Nothing is invented to fill them."""
    out = []
    for f in store.list_fields(include_missing=True, scope=scope):
        if f.group == "other":
            continue
        first_instance = f.key.endswith("[0]") or "[" not in f.key
        if f.status == "MISSING" and first_instance:
            out.append({"key": f.key, "label": f.label, "sensitivity": f.sensitivity})
    return out


def run_onboarding(store: BusinessStore, scope: str, ask: Callable[[str, bool], str], say: Callable[[str], None], only: list[str] | None = None) -> dict:
    kind = store.kind_of(scope)
    stored = skipped = kept = flagged = 0
    for key, many in order_for(scope):
        if only and key not in only:
            continue
        template = key.replace("[]", "[0]")
        try:
            pk = parse_key(template, kind)
        except KeyError_:
            continue
        i = 0
        while True:
            k = key.replace("[]", f"[{i}]") if many else key
            st = store.status(k, scope)
            label = st.label + (f" #{i + 1}" if many else "")
            hidden = st.sensitivity == "HIGH"
            if st.status == "CONFLICTING":
                say(f"- {label}: CONFLICTING (two different values). Resolve it first: business resolve {k} <fact_id>   (see: business show {k})")
                flagged += 1
                break
            if st.status in ("VERIFIED", "UNVERIFIED") and st.live:
                f = st.live[0]
                hint = "type y to re-enter it part by part and REPLACE it" if pk.field.compound else "type a new value to REPLACE it"
                answer = ask(f"{label}: already {st.status} ({f.mask}). Enter = keep; {hint}", hidden).strip()
                if not answer:
                    kept += 1
                    if many:
                        i += 1
                        continue
                    break
                replace = True
            else:
                how = "type y to enter it part by part, Enter = skip" if pk.field.compound else "Enter = I don't have it / skip"
                answer = ask(f"{label} [{st.sensitivity}] ({how})" + (" (input hidden)" if hidden else ""), hidden).strip()
                if not answer:
                    skipped += 1
                    break
                replace = False
            if pk.field.compound and answer.lower() not in ("y", "yes"):
                skipped += 1
                break
            try:
                value = _compound(pk, ask) if pk.field.compound else answer
                if pk.field.compound and not value:
                    skipped += 1
                    break
                r = store.put_candidate(k, value, source_type="USER_ENTERED", replace=replace, scope=scope, actor="cli-onboard")
                stored += 1 if r.created else 0
                say(f"  stored {label}: {r.status}" + "".join(f"  (warning: {w})" for w in r.warnings))
            except StoreError as e:
                say(f"  not stored: {e.message}")
            if not many:
                break
            i += 1
            if i >= 20:
                break
    return {"stored": stored, "skipped": skipped, "kept": kept, "conflicts": flagged}


def _compound(pk, ask) -> dict:
    parts = {}
    for part in pk.field.parts:
        v = ask(f"  {part.label} (Enter = skip)", False).strip()
        if v:
            parts[part.name] = v
    return parts
