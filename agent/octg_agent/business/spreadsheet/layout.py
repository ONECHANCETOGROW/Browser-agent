"""The workbook layout (DATA, like the field catalog): which sheets exist, which columns each has, what each column means, and how a user's own header wording is mapped.

Mapping is conservative and deterministic (no model):
  * an exact template name or a listed synonym  -> CONFIDENT;
  * a wording that could mean materially different fields ("Name", "Business Name", "Contact") -> NEEDS CONFIRMATION, never a silent choice;
  * a header that asks for a password, OTP, key, card, bank account or government-ID number -> REFUSED (the column is never read);
  * anything else -> UNKNOWN: reported and ignored (a closest-name hint is shown but never applied).
The user can confirm with --map "Sheet:Header=column" or skip with --skip "Sheet:Header".
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ...sensitive import field_category

REFUSED_CATEGORIES = {"password", "otp", "card", "bank", "identity", "secret"}   # never imported, whatever the column is called


def hnorm(text: str) -> str:
    """Comparison form of a header / field name: 'Primary Address Line 1' -> 'primary_address_line_1'."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


@dataclass(frozen=True)
class Target:
    kind: str            # "fact" (one catalog fact) | "multi" (a repeatable catalog fact, items separated by ; | or new lines) | "addr" (one part of a compound address)
                         # | "ref" (the client's reference) | "attr" (an attribute of a structured row)
    key: str = ""        # catalog key, or the attribute name
    part: str = ""       # address part name


@dataclass(frozen=True)
class Col:
    name: str
    target: Target
    synonyms: tuple[str, ...] = ()
    required: bool = False
    hint: str = ""       # one line of guidance (template cell prompt / README)

    def names(self) -> set[str]:
        return {hnorm(self.name), *(hnorm(s) for s in self.synonyms)}


@dataclass(frozen=True)
class SheetDef:
    key: str                         # me | clients | services | locations | seo
    name: str                        # the sheet's name in the template
    layout: str                      # "kv" (Field | Value rows) or "table" (a header row, then one record per row)
    cols: tuple[Col, ...]
    ambiguous: dict[str, tuple[str, ...]] = field(default_factory=dict)   # hnorm(header) -> candidate column names
    sheet_names: tuple[str, ...] = ()
    purpose: str = ""

    def col(self, name: str) -> Col | None:
        return next((c for c in self.cols if c.name == name), None)


def _c(name, kind, key="", part="", syn=(), required=False, hint=""):
    return Col(name, Target(kind, key, part), tuple(syn), required, hint)


# ------------------------------------------------------------------------------------------ My Business (the owner's own profile): Field | Value
ME = SheetDef(
    "me", "My Business", "kv",
    (
        _c("legal_name", "fact", "identity.legal_name", syn=("legal business name", "registered name", "legal name of business", "legal name of the business", "entity name"), hint="The registered / legal name of your business."),
        _c("trade_name", "fact", "identity.trade_name", syn=("trading name", "brand name", "brand", "doing business as", "dba"), hint="The name you trade under, if different."),
        _c("website", "fact", "contact.website", syn=("web site", "website url", "web address", "url", "business website"), hint="Your website address."),
        _c("email", "fact", "contact.email", syn=("e-mail", "email address", "e-mail address", "business email", "contact email"), hint="Your main business e-mail."),
        _c("support_email", "fact", "contact.support_email", syn=("support e-mail", "support email address", "customer support email"), hint="A separate support e-mail, if you have one."),
        _c("phone", "fact", "contact.phone", syn=("phone number", "mobile", "mobile number", "telephone", "contact number", "business phone", "contact phone"), hint="Your main business phone."),
        _c("address_line_1", "addr", "addresses.principal", "line1", syn=("address line 1", "address 1", "street address", "street"), hint="Street address of your principal place of business."),
        _c("address_line_2", "addr", "addresses.principal", "line2", syn=("address line 2", "address 2"), hint="Building / area, if any."),
        _c("city", "addr", "addresses.principal", "locality", syn=("town", "locality"), hint="City."),
        _c("state", "addr", "addresses.principal", "state", syn=("province", "state province"), hint="State / province (as written on forms, e.g. Maharashtra)."),
        _c("country", "addr", "addresses.principal", "country", hint="Country."),
        _c("postal_code", "addr", "addresses.principal", "pin", syn=("zip", "zip code", "pin code", "pincode", "postcode", "postal"), hint="PIN / ZIP / postal code. Format the cell as Text so leading zeros stay."),
        _c("description", "fact", "identity.description", syn=("business description", "about", "about us", "about the business"), hint="A short description of your business."),
        _c("primary_services", "multi", "services.primary[]", syn=("primary service", "primary services offered", "main services", "main service"), hint="Your main services. Separate several with a semicolon (;) or put each on its own line."),
        _c("additional_services", "multi", "services.additional[]", syn=("additional service", "other services", "secondary services"), hint="Other services. Separate several with a semicolon (;) or one per line."),
        _c("registration.udyam", "fact", "registration.udyam", syn=("udyam", "udyam number", "udyam registration", "udyam registration number", "msme registration"), hint="HIGH sensitivity: stored encrypted, never shown to a model. Leave blank if you have none."),
        _c("registration.pan", "fact", "taxation.pan", syn=("pan", "pan number", "permanent account number", "taxation.pan"), hint="HIGH sensitivity: stored encrypted, never shown to a model. Leave blank if unknown."),
        _c("registration.gstin", "fact", "taxation.gstin", syn=("gstin", "gst number", "gst no", "gst registration number", "taxation.gstin"), hint="HIGH sensitivity: stored encrypted, never shown to a model. Leave blank if you have none."),
        _c("registration.tan", "fact", "taxation.tan", syn=("tan", "tan number", "taxation.tan"), hint="HIGH sensitivity. Leave blank if you have none."),
        _c("registration.cin", "fact", "registration.cin", syn=("cin", "corporate identification number"), hint="HIGH sensitivity. Leave blank if you have none."),
    ),
    ambiguous={
        **{k: ("legal_name", "trade_name") for k in ("name", "business_name", "company_name", "company", "business", "firm_name", "organization_name", "organisation_name", "organization", "organisation")},
        "address": ("address_line_1",),
        "services": ("primary_services", "additional_services"), "service": ("primary_services", "additional_services"),
    },
    sheet_names=("my business", "business", "my company", "owner", "octg", "my profile"),
    purpose="One Chance to Grow's own business profile (two columns: Field, Value). Blank means missing.",
)

# ------------------------------------------------------------------------------------------ Clients: one row per client
_CLIENT_NAME_WORDS = ("name", "business_name", "company_name", "company", "business", "client", "client_name", "firm_name", "organization_name", "organisation_name")
CLIENTS = SheetDef(
    "clients", "Clients", "table",
    (
        _c("client_id", "ref", syn=("client id", "clientid", "client code", "client ref", "client reference", "reference", "ref"), hint="Your own id for the client, e.g. CLIENT-001. Used to link the other sheets to this client."),
        _c("business_name", "fact", "identity.business_name", syn=("company name", "client name", "name of business", "firm name", "organization name", "organisation name"), hint="The name the client goes by (also how you will pick them in a task)."),
        _c("legal_name", "fact", "identity.legal_name", syn=("legal business name", "registered name", "legal name of business", "entity name"), hint="Registered / legal name, if different."),
        _c("trade_name", "fact", "identity.trade_name", syn=("trading name", "brand name", "doing business as", "dba"), hint="Trade name, if different."),
        _c("website", "fact", "contact.website", syn=("web site", "website url", "web address", "url", "site url", "business website"), hint="The client's website."),
        _c("email", "fact", "contact.email", syn=("e-mail", "email address", "e-mail address", "business email", "contact email"), hint="Business e-mail."),
        _c("phone", "fact", "contact.phone", syn=("phone number", "mobile", "mobile number", "telephone", "contact number", "business phone", "contact phone"), hint="Business phone."),
        _c("contact_person", "fact", "contact.person", syn=("contact name", "point of contact", "primary contact", "owner name", "contact person name"), hint="Who to speak to."),
        _c("description", "fact", "identity.description", syn=("business description", "about the business", "description of business", "company description"), hint="Short description."),
        _c("primary_address_line_1", "addr", "location.address", "line1", syn=("address line 1", "address 1", "street address", "street", "address_line_1"), hint="Street address of the client's primary location."),
        _c("primary_address_line_2", "addr", "location.address", "line2", syn=("address line 2", "address 2", "suite", "unit", "address_line_2"), hint="Suite / unit, if any."),
        _c("city", "addr", "location.address", "city", syn=("town", "locality"), hint="City of the primary address."),
        _c("state", "addr", "location.address", "state", syn=("province", "state province"), hint="State / province."),
        _c("country", "addr", "location.address", "country", hint="Country."),
        _c("postal_code", "addr", "location.address", "postal_code", syn=("zip", "zip code", "postcode", "pin code", "pincode"), hint="ZIP / postal code. Format the cell as Text so leading zeros stay."),
        _c("status", "fact", "account.status", syn=("client status", "account status"), hint="e.g. Active, Paused."),
        _c("onboarding_date", "fact", "account.onboarding_date", syn=("onboarding date", "start date", "client since"), hint="A real date cell, or text as YYYY-MM-DD."),
        _c("plan", "fact", "account.plan", syn=("package", "service plan", "plan package"), hint="The package / plan."),
        _c("internal_notes", "fact", "account.internal_notes", syn=("internal notes", "octg notes"), hint="HIGH sensitivity: stored encrypted, never shown to a model."),
        _c("pan", "fact", "taxation.pan", syn=("pan number", "permanent account number"), hint="HIGH sensitivity (India). Leave blank if unknown."),
        _c("gstin", "fact", "taxation.gstin", syn=("gst number", "gst no", "gst registration number"), hint="HIGH sensitivity (India). Leave blank if unknown."),
        _c("tan", "fact", "taxation.tan", syn=("tan number",), hint="HIGH sensitivity (India)."),
        _c("cin", "fact", "registration.cin", syn=("corporate identification number",), hint="HIGH sensitivity (India)."),
        _c("udyam", "fact", "registration.udyam", syn=("udyam number", "udyam registration", "udyam registration number", "msme registration"), hint="HIGH sensitivity (India)."),
    ),
    ambiguous={
        **{k: ("business_name", "legal_name", "trade_name") for k in _CLIENT_NAME_WORDS if k not in ("business_name", "company_name", "client_name", "firm_name", "organization_name", "organisation_name")},
        "address": ("primary_address_line_1",),
        "notes": ("internal_notes", "description"), "note": ("internal_notes", "description"), "comments": ("internal_notes", "description"),
        "contact": ("contact_person", "phone", "email"),
        "id": ("client_id",), "client_no": ("client_id",), "code": ("client_id",),
    },
    sheet_names=("clients", "client", "client list", "customers"),
    purpose="One row per client. Each client is a separate scope: nothing here is ever mixed with another client or with your own business.",
)

PRIORITIES = ("High", "Medium", "Low")
LOCATION_TYPES = ("primary", "service_area", "target_area", "other")

SERVICES = SheetDef(
    "services", "Client Services", "table",
    (
        _c("client_id", "ref", syn=("client id", "clientid", "client code", "client ref", "client reference", "client"), required=True, hint="The client this service belongs to (the id used on the Clients sheet)."),
        _c("service", "attr", "service", syn=("service name", "services", "service offered"), required=True, hint="One service per row."),
        _c("service_category", "attr", "category", syn=("category", "service category"), hint="A grouping, e.g. Locksmith."),
        _c("priority", "attr", "priority", hint="High, Medium or Low."),
        _c("notes", "attr", "notes", syn=("note", "comments"), hint="Anything worth remembering about this service."),
    ),
    ambiguous={"type": ("service_category",), "service_type": ("service_category",), "id": ("client_id",)},
    sheet_names=("client services", "services", "client service"),
    purpose="One row per client/service relationship.",
)

LOCATIONS = SheetDef(
    "locations", "Client Locations", "table",
    (
        _c("client_id", "ref", syn=("client id", "clientid", "client code", "client ref", "client reference", "client"), required=True, hint="The client this location belongs to."),
        _c("location", "attr", "location", syn=("location name", "place", "area", "service area"), hint="A place name, e.g. a town or neighbourhood. Give this and/or the city."),
        _c("city", "attr", "city", syn=("town",), hint="City."),
        _c("state", "attr", "state_region", syn=("province", "state province"), hint="State / province."),
        _c("country", "attr", "country", hint="Country."),
        _c("postal_code", "attr", "postal_code", syn=("zip", "zip code", "postcode", "pin code", "pincode"), hint="ZIP / postal code (format as Text)."),
        _c("location_type", "attr", "location_type", syn=("location type",), hint="primary, service_area, target_area or other."),
        _c("priority", "attr", "priority", hint="High, Medium or Low."),
        _c("notes", "attr", "notes", syn=("note", "comments"), hint="Anything worth remembering."),
    ),
    ambiguous={"type": ("location_type",), "id": ("client_id",)},
    sheet_names=("client locations", "locations", "client location"),
    purpose="One row per service location of a client.",
)

SEO = SheetDef(
    "seo", "Client SEO", "table",
    (
        _c("client_id", "ref", syn=("client id", "clientid", "client code", "client ref", "client reference", "client"), required=True, hint="The client this target belongs to."),
        _c("keyword", "attr", "keyword", syn=("keywords", "target keyword", "search term", "seo keyword"), required=True, hint="One keyword per row."),
        _c("location", "attr", "location", syn=("target location", "geo", "target city"), hint="Where the client wants to rank for this keyword."),
        _c("priority", "attr", "priority", hint="High, Medium or Low."),
        _c("service", "attr", "service", syn=("target service", "seo service"), hint="The service this keyword is about."),
        _c("notes", "attr", "notes", syn=("note", "comments"), hint="Anything worth remembering."),
    ),
    ambiguous={"id": ("client_id",)},
    sheet_names=("client seo", "seo", "seo targets", "client seo targets"),
    purpose="Structured SEO targeting information (keyword + location) that a future research feature can use. Nothing is researched or ranked by importing it.",
)

SHEETS: tuple[SheetDef, ...] = (ME, CLIENTS, SERVICES, LOCATIONS, SEO)
DOC_SHEETS = {"readme", "read_me", "instructions", "instruction", "guide", "help", "about", "notes", "template", "examples", "example"}


def sheet_for_name(name: str) -> SheetDef | None:
    n = hnorm(name).replace("_", " ")
    for d in SHEETS:
        if n == d.name.lower() or n in d.sheet_names:
            return d
    return None


def is_doc_sheet(name: str) -> bool:
    return hnorm(name) in DOC_SHEETS


# ------------------------------------------------------------------------------------------ header mapping
CONFIDENT, NEEDS, UNKNOWN, REFUSED, SKIPPED, CONFIRMED = "CONFIDENT", "NEEDS CONFIRMATION", "UNKNOWN", "REFUSED", "SKIPPED", "CONFIRMED"


@dataclass
class ColMap:
    sheet: str
    header: str                      # the user's own wording
    index: int                       # position in the header row (kv sheets: 0)
    status: str
    target: str = ""                 # the template column it maps to ("" when unmapped)
    candidates: tuple[str, ...] = ()
    note: str = ""
    row: int = 0                     # kv sheets: the row of this Field

    def to_dict(self) -> dict:
        return {"sheet": self.sheet, "header": self.header, "status": self.status, "target": self.target, "candidates": list(self.candidates), "note": self.note}


def map_headers(d: SheetDef, headers: list[tuple[int, str, int]], overrides: dict[str, str] | None = None, skips: set[str] | None = None, extra=None) -> list[ColMap]:
    """headers: (position, text, row). `overrides`: hnorm(header) -> template column name chosen by the user; `skips`: hnorm(header). `extra(text)` may resolve a header the layout does not
    list (My Business accepts any catalog key such as owners[0].name) and returns a Col or None."""
    overrides, skips = overrides or {}, skips or set()
    names: dict[str, Col] = {}
    for c in d.cols:
        for n in c.names():
            names.setdefault(n, c)
    used: dict[str, str] = {}
    out: list[ColMap] = []
    for pos, text, row in headers:
        key = hnorm(text)
        if not key:
            out.append(ColMap(d.name, text, pos, UNKNOWN, note="a column without a header: ignored", row=row))
            continue
        if key in skips:
            out.append(ColMap(d.name, text, pos, SKIPPED, note="skipped at your request", row=row))
            continue
        if key in overrides:
            tgt = overrides[key]
            if d.col(tgt) is None:
                out.append(ColMap(d.name, text, pos, UNKNOWN, note=f"{tgt!r} is not a column of {d.name}", row=row))
                continue
            cm = ColMap(d.name, text, pos, CONFIRMED, tgt, note="mapped as you confirmed", row=row)
        elif key in names:
            tgt = names[key].name
            cm = ColMap(d.name, text, pos, CONFIDENT, tgt, note="exact name" if key == hnorm(tgt) else "known alternative name", row=row)
        elif key in d.ambiguous:
            cands = d.ambiguous[key]
            out.append(ColMap(d.name, text, pos, NEEDS, "", cands, "could mean " + (" or ".join(cands)) if len(cands) > 1 else f"probably {cands[0]}, but it could be something else: confirm", row=row))
            continue
        else:
            extra_col = extra(text) if extra else None
            if extra_col is not None:
                cm = ColMap(d.name, text, pos, CONFIDENT, extra_col.name, note="a catalog field", row=row)
                names.setdefault(key, extra_col)
            else:
                cat = field_category(text)
                if cat in REFUSED_CATEGORIES:
                    out.append(ColMap(d.name, text, pos, REFUSED, note=f"looks like a {cat} column: never imported (passwords, OTPs, keys, payment cards, bank and government-ID numbers are not stored)", row=row))
                    continue
                close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.78)
                hint = f" (closest known column: {names[close[0]].name}: not applied; use --map to confirm)" if close else ""
                out.append(ColMap(d.name, text, pos, UNKNOWN, note="not a known column: ignored" + hint, row=row))
                continue
        if cm.target in used:
            out.append(ColMap(d.name, text, pos, NEEDS, "", (cm.target,), f"'{used[cm.target]}' already maps to {cm.target}: skip one of them", row=row))
            continue
        used[cm.target] = text
        out.append(cm)
    return out
