"""The Business Profile field catalog. It is DATA (versioned), not logic: a form field maps to a catalog key, never to free text.

Sensitivity (what a fact is, independent of who asks):
  LOW        public/descriptive            MEDIUM   contact, address, names, dates
  HIGH       tax / registration identifiers, anything private   (encrypted at rest, never placed in any model prompt)
  VERY_HIGH  passwords, OTPs, keys, payment credentials -> NEVER stored (see validators.secret_reason)

Out of scope in v1 (deliberately absent): banking details, Aadhaar / government-ID numbers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CATALOG_VERSION = 3
LEVELS = ("LOW", "MEDIUM", "HIGH")  # VERY_HIGH is a refusal, not a stored level
LEVEL_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "VERY_HIGH": 3}

CONSTITUTIONS = ("Proprietorship", "Partnership", "LLP", "Private Limited", "Public Limited", "One Person Company", "Hindu Undivided Family", "Society/Trust",
                 "Limited Liability Company (LLC)", "Corporation (C-Corp)", "S Corporation", "Nonprofit Organization", "Other")


@dataclass(frozen=True)
class Part:
    name: str
    label: str
    kind: str
    aliases: tuple[str, ...]
    prompt_ok: bool = False


@dataclass(frozen=True)
class Field:
    key: str                      # template key; "[]" marks an instance index, e.g. owners[].name
    label: str
    group: str
    sensitivity: str
    kind: str                     # validator name
    aliases: tuple[str, ...] = ()
    many: bool = False
    prompt_ok: bool = False       # an ENUM fact a LOCAL model may be shown (never a cloud model, never HIGH)
    parts: tuple[Part, ...] = ()  # compound value (stored as one unit so parts of different sources can never mix)
    custom: bool = False

    @property
    def compound(self) -> bool:
        return bool(self.parts)

    def part(self, name: str) -> Part | None:
        return next((p for p in self.parts if p.name == name), None)


_ADDRESS_PARTS = (
    Part("line1", "Address line 1", "text", ("address line 1", "address 1", "street address", "street", "building", "building no", "flat", "door no", "house no", "building number")),
    Part("line2", "Address line 2", "text", ("address line 2", "address 2", "road", "landmark")),
    Part("locality", "City / locality", "text", ("locality", "city", "town", "village", "city village")),
    Part("district", "District", "text", ("district",)),
    Part("state", "State", "text", ("state", "state ut", "province"), prompt_ok=True),
    Part("pin", "PIN code", "pin", ("pin code", "pincode", "pin", "postal code", "zip code", "zip", "postcode")),
    Part("country", "Country", "text", ("country",)),
)
_ADDRESS_ALIASES = ("business address", "principal place of business", "address of principal place of business", "registered address", "registered office address", "office address", "address", "principal address")


def _f(key, label, group, sens, kind, aliases=(), **kw) -> Field:
    return Field(key, label, group, sens, kind, tuple(aliases), **kw)


CATALOG: tuple[Field, ...] = (
    _f("identity.legal_name", "Legal name of business", "identity", "MEDIUM", "text",
       ("legal name", "legal name of business", "legal name of the business", "business name", "name of business", "name of the business", "company name", "firm name",
        "organization name", "organisation name", "entity name")),
    _f("identity.trade_name", "Trade name", "identity", "MEDIUM", "text", ("trade name", "brand name", "doing business as", "dba")),
    _f("identity.constitution", "Constitution of business", "identity", "MEDIUM", "enum",
       ("constitution of business", "constitution", "business type", "type of business", "type of entity", "entity type", "business structure", "legal structure", "organization type"), prompt_ok=True),
    _f("identity.description", "Business description", "identity", "LOW", "text", ("business description", "about your business", "description of business")),
    _f("contact.email", "Business e-mail", "contact", "MEDIUM", "email", ("email", "e-mail", "email address", "e-mail address", "business email", "contact email")),
    _f("contact.phone", "Business phone", "contact", "MEDIUM", "phone", ("phone", "phone number", "mobile", "mobile number", "contact number", "telephone", "business phone", "contact phone")),
    _f("contact.website", "Website", "contact", "LOW", "url", ("website", "web site", "website url", "business website", "web address")),
    _f("contact.support_email", "Support e-mail", "contact", "MEDIUM", "email", ("support email", "support e-mail", "support email address", "customer support email", "helpdesk email")),
    _f("addresses.principal", "Principal place of business", "addresses", "MEDIUM", "none", _ADDRESS_ALIASES, parts=_ADDRESS_PARTS),
    _f("addresses.additional[]", "Additional place of business", "addresses", "MEDIUM", "none", ("additional place of business", "additional address"), many=True, parts=_ADDRESS_PARTS),
    _f("registration.incorporation_date", "Date of incorporation / commencement", "registration", "MEDIUM", "date",
       ("date of incorporation", "incorporation date", "date of commencement of business", "business start date", "date of establishment", "date of formation")),
    _f("registration.cin", "CIN", "registration", "HIGH", "cin", ("cin", "corporate identification number", "company identification number", "corporate identity number")),
    _f("registration.llpin", "LLPIN", "registration", "HIGH", "llpin", ("llpin", "llp identification number")),
    _f("registration.udyam", "Udyam registration number", "registration", "HIGH", "udyam", ("udyam", "udyam registration number", "udyam number", "msme registration")),
    _f("registration.registration_number", "Business registration number", "registration", "HIGH", "text",
       ("registration number", "business registration number", "company registration number", "registration no", "incorporation number")),
    _f("taxation.pan", "PAN of business", "taxation", "HIGH", "pan", ("pan", "pan number", "permanent account number", "business pan", "company pan", "pan of business", "pan of the business")),
    _f("taxation.gstin", "GSTIN", "taxation", "HIGH", "gstin", ("gstin", "gst number", "gst no", "gst registration number", "gstin number", "gst identification number")),
    _f("taxation.tan", "TAN", "taxation", "HIGH", "tan", ("tan", "tan number", "tax deduction account number")),
    _f("owners[].name", "Owner / partner / director name", "owners", "MEDIUM", "text",
       ("owner name", "proprietor name", "name of proprietor", "partner name", "director name", "authorized signatory name", "authorised signatory name", "name of owner", "promoter name"), many=True),
    _f("owners[].role", "Owner designation", "owners", "LOW", "text", ("designation", "role"), many=True),
    _f("owners[].email", "Owner e-mail", "owners", "MEDIUM", "email", ("owner email", "proprietor email", "director email", "signatory email"), many=True),
    _f("owners[].phone", "Owner phone", "owners", "MEDIUM", "phone", ("owner mobile", "proprietor mobile", "director mobile", "signatory mobile", "owner phone"), many=True),
    _f("owners[].pan", "Owner PAN", "owners", "HIGH", "pan", ("owner pan", "proprietor pan", "director pan", "signatory pan", "pan of proprietor"), many=True),
    _f("owners[].dob", "Owner date of birth", "owners", "HIGH", "date", ("date of birth", "dob", "owner date of birth"), many=True),
    _f("services.description", "Services offered", "services", "LOW", "text", ("services offered", "description of services", "nature of business", "nature of business activity", "business activities")),
    _f("services.primary[]", "Primary service", "services", "LOW", "text", ("primary service", "main service", "core service"), many=True),
    _f("services.additional[]", "Additional service", "services", "LOW", "text", ("additional service", "other service", "secondary service"), many=True),
    _f("services.hsn_sac", "HSN / SAC codes", "services", "LOW", "digits", ("hsn", "sac", "hsn code", "sac code", "hsn sac", "hsn sac code"), many=True),
    _f("social.profile[]", "Social profile link", "social", "LOW", "url", ("social media", "social profile", "social media link", "profile url"), many=True),
)

BY_KEY = {f.key: f for f in CATALOG}

# ---------------------------------------------------------------- the CLIENT catalog (Client Brain). A client is a separate entity: same machinery, its own catalog and its own scope.
_CLIENT_ADDRESS_PARTS = (
    Part("line1", "Street address", "text", ("address line 1", "address 1", "street address", "street", "building", "suite")),
    Part("line2", "Address line 2", "text", ("address line 2", "address 2", "suite unit", "unit", "landmark")),
    Part("city", "City", "text", ("city", "town", "locality")),
    Part("state", "State / province", "text", ("state", "province", "state province")),
    Part("country", "Country", "text", ("country",)),
    Part("postal_code", "ZIP / postal code", "text", ("zip code", "zip", "postal code", "postcode", "pin code", "pincode")),
)

CLIENT_CATALOG: tuple[Field, ...] = (
    # identity
    _f("identity.business_name", "Business name", "identity", "MEDIUM", "text", ("business name", "name of business", "company name", "firm name", "organization name", "organisation name")),
    _f("identity.legal_name", "Legal name", "identity", "MEDIUM", "text", ("legal name", "legal name of business", "registered name", "entity name")),
    _f("identity.trade_name", "Trade name", "identity", "MEDIUM", "text", ("trade name", "brand name", "doing business as", "dba")),
    _f("identity.description", "Business description", "identity", "LOW", "text", ("business description", "about the business", "description of business", "company description")),
    # contact
    _f("contact.website", "Website", "contact", "LOW", "url", ("website", "web site", "website url", "business website", "web address", "site url")),
    _f("contact.email", "Business e-mail", "contact", "MEDIUM", "email", ("email", "e-mail", "email address", "e-mail address", "business email", "contact email")),
    _f("contact.phone", "Business phone", "contact", "MEDIUM", "phone", ("phone", "phone number", "mobile", "mobile number", "contact number", "telephone", "business phone", "contact phone")),
    _f("contact.person", "Contact person", "contact", "MEDIUM", "text", ("contact person", "contact name", "point of contact", "primary contact", "owner name")),
    # location
    _f("location.address", "Primary business address", "location", "MEDIUM", "none", ("business address", "primary address", "street address full", "address", "business location", "office address"), parts=_CLIENT_ADDRESS_PARTS),
    # service area
    _f("service_area.cities[]", "Service area: city", "service_area", "LOW", "text", ("service area city", "city served", "cities served", "service city"), many=True),
    _f("service_area.towns[]", "Service area: town", "service_area", "LOW", "text", ("service area town", "town served", "towns served"), many=True),
    _f("service_area.counties[]", "Service area: county", "service_area", "LOW", "text", ("service area county", "county served", "counties served", "county"), many=True),
    _f("service_area.regions[]", "Service area: region", "service_area", "LOW", "text", ("service area region", "region served", "regions served", "service region"), many=True),
    _f("service_area.other[]", "Service area: other", "service_area", "LOW", "text", ("service area", "areas served", "area served"), many=True),
    # services
    _f("services.primary[]", "Primary service", "services", "LOW", "text", ("primary service", "main service", "core service"), many=True),
    _f("services.secondary[]", "Secondary service", "services", "LOW", "text", ("secondary service", "additional service", "other service"), many=True),
    _f("services.categories[]", "Service category", "services", "LOW", "text", ("service category", "service categories"), many=True),
    # local SEO
    _f("local_seo.gbp_reference", "Google Business Profile reference", "local_seo", "MEDIUM", "text", ("google business profile", "business profile reference", "gbp", "gbp url", "google maps url", "place id")),
    _f("local_seo.primary_category", "Primary business category", "local_seo", "LOW", "text", ("primary category", "business category", "main category", "category")),
    _f("local_seo.secondary_categories[]", "Secondary business category", "local_seo", "LOW", "text", ("secondary category", "additional category", "additional categories"), many=True),
    _f("local_seo.target_locations[]", "Local SEO target location", "local_seo", "LOW", "text", ("local target location", "local seo location", "map target location"), many=True),
    # SEO
    _f("seo.target_keywords[]", "Target keyword", "seo", "LOW", "text", ("target keyword", "target keywords", "keyword", "keywords", "seo keyword", "search term"), many=True),
    _f("seo.target_services[]", "Target service", "seo", "LOW", "text", ("target service", "target services", "seo service"), many=True),
    _f("seo.target_locations[]", "Target location", "seo", "LOW", "text", ("target location", "target locations", "seo location", "target city"), many=True),
    _f("seo.goals[]", "SEO goal", "seo", "LOW", "text", ("seo goal", "seo goals", "goal", "goals", "campaign goal"), many=True),
    _f("seo.current_status", "Current SEO status", "seo", "LOW", "text", ("current seo status", "seo status", "current status")),
    # marketing
    _f("marketing.social_profiles[]", "Social profile link", "marketing", "LOW", "url", ("social profile", "social media", "social media link", "profile url"), many=True),
    _f("marketing.directories[]", "Directory / citation", "marketing", "LOW", "text", ("directory", "citation", "directory listing", "citation listing"), many=True),
    _f("marketing.content_notes", "Content notes", "marketing", "MEDIUM", "text", ("content notes", "content plan", "content ideas")),
    # account / internal
    _f("account.status", "Client status", "account", "LOW", "text", ("client status", "account status")),
    _f("account.onboarding_date", "Onboarding date", "account", "LOW", "date", ("onboarding date", "start date", "client since")),
    _f("account.plan", "Plan / package", "account", "LOW", "text", ("plan", "package", "service plan", "plan package")),
    _f("account.internal_notes", "Internal OCTG notes", "account", "HIGH", "text", ("internal notes", "octg notes", "notes")),
    _f("account.authorized_users[]", "Authorized OCTG user", "account", "MEDIUM", "text", ("authorized user", "authorised user", "octg user"), many=True),
    # identifiers used when OCTG files forms for the client
    _f("registration.cin", "CIN", "registration", "HIGH", "cin", ("cin", "corporate identification number", "company identification number")),
    _f("registration.udyam", "Udyam registration number", "registration", "HIGH", "udyam", ("udyam", "udyam registration number", "udyam number", "msme registration")),
    _f("taxation.pan", "PAN", "taxation", "HIGH", "pan", ("pan", "pan number", "permanent account number")),
    _f("taxation.gstin", "GSTIN", "taxation", "HIGH", "gstin", ("gstin", "gst number", "gst no", "gst registration number", "gstin number")),
    _f("taxation.tan", "TAN", "taxation", "HIGH", "tan", ("tan", "tan number")),
)
CLIENT_BY_KEY = {f.key: f for f in CLIENT_CATALOG}
CATALOGS = {"me": (CATALOG, BY_KEY), "client": (CLIENT_CATALOG, CLIENT_BY_KEY)}
ME, CLIENT = "me", "client"
CUSTOM_RE = re.compile(r"^other\.[a-z0-9_]{1,40}$")
_INSTANCE = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class ParsedKey:
    raw: str
    field: Field
    instance: int | None
    part: str | None          # a part of a compound value, or "full"
    kind: str = "me"          # which catalog: "me" (the owner's own business) or "client"

    @property
    def storage_key(self) -> str:
        """Identity of the stored value (the part is not part of it): 'owners[].name' + instance, 'addresses.principal'."""
        return self.field.key

    def display(self) -> str:
        base = self.field.key.replace("[]", f"[{self.instance}]") if self.instance is not None else self.field.key
        return base + (f".{self.part}" if self.part else "")

    @property
    def label(self) -> str:
        if self.part == "full":
            return self.field.label
        if self.part:
            return f"{self.field.label} — {self.field.part(self.part).label}"
        return self.field.label

    @property
    def sensitivity(self) -> str:
        return self.field.sensitivity

    @property
    def prompt_ok(self) -> bool:
        if self.part and self.part != "full":
            return bool(self.field.part(self.part) and self.field.part(self.part).prompt_ok)
        return self.field.prompt_ok and not self.part


class KeyError_(ValueError):
    pass


def parse_key(raw: str, kind: str = "me") -> ParsedKey:
    """'owners[0].name' -> (owners[].name, instance 0); 'addresses.principal.pin' -> (addresses.principal, part pin). Unknown keys raise KeyError_.
    `kind` selects the catalog: "me" (the owner's business) or "client"."""
    if not isinstance(raw, str) or not raw or len(raw) > 80:
        raise KeyError_(f"not a valid business field key: {str(raw)[:40]!r}")
    by = CATALOGS[kind][1]
    raw = raw.strip()
    inst = _INSTANCE.search(raw)
    instance = int(inst.group(1)) if inst else None
    template = _INSTANCE.sub("[]", raw, count=1)
    f = by.get(template) or by.get(template + "[]")  # a repeatable field written without an index means its first entry
    if f:
        if f.many and instance is None:
            instance = 0
        if not f.many and instance is not None:
            raise KeyError_(f"{f.key} has no instances")
        if f.compound:
            return ParsedKey(raw, f, instance, "full", kind)  # whole compound value (addresses.principal == addresses.principal.full)
        return ParsedKey(raw, f, instance, None, kind)
    base, _, last = template.rpartition(".")
    f = by.get(base) or by.get(base + "[]")
    if f and f.compound and (last == "full" or f.part(last)):
        if f.many and instance is None:
            instance = 0
        if not f.many and instance is not None:
            raise KeyError_(f"{f.key} has no instances")
        return ParsedKey(raw, f, instance, last, kind)
    if CUSTOM_RE.match(template):
        return ParsedKey(raw, Field(template, template.split(".", 1)[1].replace("_", " ").capitalize(), "other", "HIGH", "text", custom=True), None, None, kind)
    raise KeyError_(f"unknown business field {raw!r}")


def valid_key(raw: str, kind: str = "me") -> bool:
    try:
        parse_key(raw, kind)
        return True
    except KeyError_:
        return False


# ------------------------------------------------------------ label matching (deterministic; no model)
_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _alias_hit(tokset: set[str], alias: str) -> bool:
    aw = words(alias)
    return bool(aw) and all(w in tokset for w in aw)


def match_label(*texts: str, kind: str = "me") -> list[tuple[ParsedKey, int]]:
    """Which catalog keys could a form field with this label/placeholder be asking for? Returns (key, specificity) best first.
    Within one compound field only the most specific alias wins ('address line 1' beats 'address')."""
    toks = set()
    for t in texts:
        toks.update(words(t))
    if not toks:
        return []
    found: dict[str, tuple[ParsedKey, int]] = {}
    for f in CATALOGS[kind][0]:
        cands: list[tuple[str, str | None, int]] = []
        for a in f.aliases:
            if _alias_hit(toks, a):
                cands.append((f.key, "full" if f.compound else None, len(words(a))))
        for p in (f.parts if not f.many else ()):  # an "additional address" is asked for by its own name, not by the generic address-part labels
            for a in p.aliases:
                if _alias_hit(toks, a):
                    cands.append((f.key, p.name, len(words(a))))
        if not cands:
            continue
        best = max(c[2] for c in cands)
        for _, part, n in cands:
            if n != best:
                continue
            raw = f.key.replace("[]", "[0]") + (f".{part}" if part and part != "full" else "")
            pk = parse_key(raw, kind)
            found.setdefault(pk.display(), (pk, n))
    return sorted(found.values(), key=lambda x: -x[1])
