"""Markdown knowledge files -> CANDIDATE values and rows, deterministically. No model reads the file and nothing in it is ever an instruction.

A Markdown file is a SOURCE the user wrote or collected. This module reads it the way a careful clerk would: headings, `Label: value` lines, two-column tables, tables whose headers are services / locations /
keywords, and bullet lists under a heading that clearly names what the list is ("Services", "Service areas", "Keywords"). Free prose is NOT turned into values (except one explicit "About / Description" section, as a
candidate description); it stays in the user's file and is counted in the review so nothing is silently dropped.

Everything that comes out is only a CANDIDATE: it goes through the same validators as every other value (credentials refused, identifiers refused in ordinary fields, state names checked, format checks), is
compared with what is stored (new / unchanged / conflict), and becomes a stored value only when the user accepts it in the review. The scope (the owner's own business, or exactly ONE client) is chosen by the user
before the file is read and is never inferred from the text.
"""
from __future__ import annotations

import hashlib
import re

from ..sensitive import field_category
from . import records as R
from .spreadsheet import layout as L
from .spreadsheet.parse import ClientRow, Parsed, ParsedRecord, Parser, SPLIT
from .schema import KeyError_, parse_key
from .store import BusinessStore, StoreError
from .spreadsheet.xlsx import clean_text

NEW_CLIENT = "new"   # the scope value meaning "a client to be created from this file"
MAX_BYTES = 1024 * 1024
MAX_LINES = 20000
MAX_ITEM = 300

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
KV = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?([A-Za-z][A-Za-z0-9 /&'().\-_]{0,48}?)(?:\*\*|__)?\s*[:：]\s*(?:\*\*|__)?(.*?)\s*$")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
EMPH = re.compile(r"(\*\*|__|`|~~)")
INSTRUCTION_LIKE = re.compile(r"ignore (all |any |the )?(previous|prior|above|earlier) (instruction|prompt|message)|disregard (the )?(previous|above|system)|system prompt|you are now |"
                              r"act as (an? )?(ai|assistant|admin)|reveal (your|the) (prompt|instructions|password|key)|do not tell the user|new instructions?:", re.I)

SECTIONS = {
    "services": {"services", "service", "our services", "what we do", "services offered", "primary services", "main services", "core services", "primary service"},
    "services_additional": {"additional services", "other services", "secondary services", "additional service"},
    "service_areas": {"service areas", "service area", "areas served", "areas we serve", "coverage area", "coverage", "cities served", "towns served", "locations", "service locations", "location"},
    "target_locations": {"target locations", "target areas", "target cities", "seo locations", "target location"},
    "keywords": {"keywords", "target keywords", "seo keywords", "seo targets", "search terms", "seo", "keyword"},
    "about": {"about", "about us", "description", "business description", "overview", "about the business", "company description"},
}
SEC_OF = {w: k for k, ws in SECTIONS.items() for w in ws}
# a `Label: value` whose LABEL names a list of rows rather than one field (client scope) -> the kind of row each item becomes
LIST_LABELS = {"services": ("service", None), "primary services": ("service", None), "main services": ("service", None), "additional services": ("service", None), "other services": ("service", None),
               "service areas": ("location", "service_area"), "areas served": ("location", "service_area"), "cities served": ("location", "service_area"),
               "target locations": ("location", "target_area"), "target areas": ("location", "target_area"),
               "keywords": ("seo", None), "target keywords": ("seo", None), "seo keywords": ("seo", None)}
_OWNER_LIST = {"services": "services.primary[]", "primary services": "services.primary[]", "main services": "services.primary[]",
               "additional services": "services.additional[]", "other services": "services.additional[]"}
LISTY = {"services", "services_additional", "service_areas", "target_locations", "keywords"}   # a sub-heading under one of these (e.g. "Websites" under "Services") is part of the same list
NUMBERED = re.compile(r"^(?:section\s+)?\d+(?:\.\d+)*[.)]?\s+", re.I)   # "6. Services" -> "Services"
# A value that says "there is nothing here yet" is NOT a value: it is left blank on purpose (never an error, never guessed).
UNRESOLVED_START = re.compile(r"^\W*(not\s+(provided|available|applicable|supplied|known|given)|n/?a\b|nil\b|none\b|unknown\b|tbd\b|tba\b|pending\b|to\s+be\s+(confirmed|verified|supplied|added|provided)|"
                              r"verify\b|conflicting\b|unverified\b|leave\s+(it\s+)?blank|no\s+\w+\s+(yet|available))", re.I)
UNRESOLVED_CAPS = re.compile(r"\b(NOT PROVIDED|CONFLICTING|VERIFY|UNVERIFIED)\b")   # the file's own capital-letter flags, anywhere in the value
UNRESOLVED_SENTENCE = re.compile(r"\bleave\s+(it\s+)?blank\b|\bhas\s+stated\s+(that\s+)?there\s+is\s+no\b|\bdo\s+not\s+(invent|guess|enter)\b", re.I)
EXPLAIN_SEP = re.compile(r"\s+(?:---|--|—|–)(?:\s+|$)")   # "value --- a note about it"
STRICT_KINDS = {"pan", "gstin", "tan", "cin", "llpin", "udyam", "email", "phone", "url", "date", "pin", "digits"}
URL_ONLY = re.compile(r"^https?://\S+$", re.I)
PERSON_SECTION = re.compile(r"\b(founders?|owners?|partners?|directors?|proprietors?|promoters?|leadership|management)\b", re.I)
# Labels the owner's spreadsheet layout has no column for, but the Business Brain has a field for (read from Markdown only for YOUR OWN business).
_ME_EXTRA = {
    "identity.legal_name": ("legal entity name", "legal company name", "registered business name"),
    "identity.trade_name": ("trade public brand name", "trade or brand name", "public brand name", "brand trade name"),
    "identity.constitution": ("entity type", "constitution", "constitution of business", "business type", "type of entity", "legal structure", "business structure", "organization type"),
    "registration.incorporation_date": ("incorporation date", "date of incorporation", "exact incorporation date", "date of formation", "formation date", "commencement date", "date of commencement"),
    "registration.registration_number": ("business registration number", "company registration number", "filing number", "filing entity number", "wyoming filing entity number", "registration number"),
    "contact.email": ("general email", "general business email", "general registration email", "general inbox", "main email"),
    "owners[0].name": ("founder name", "founder full name"),
    "owners[0].email": ("founder email", "founder e mail"),
    "owners[0].phone": ("founder phone", "founder mobile"),
    "owners[0].role": ("founder role", "founder title"),
}
ME_EXTRA = {L.hnorm(a): k for k, aliases in _ME_EXTRA.items() for a in aliases}
# inside a "Founder" / "Owners" / "Partners" section these plain labels describe THAT PERSON, not the business
PERSON_LABELS = {L.hnorm(a): f for f, aliases in {"name": ("full name", "name", "owner name", "legal name"), "role": ("role", "designation", "title", "position"),
                                                   "email": ("email", "e mail", "email address"), "phone": ("phone", "mobile", "phone number", "mobile number")}.items() for a in aliases}
KV_TABLE_HEAD = {"field", "key", "item", "label", "attribute", "name", "property", "detail"}
KV_TABLE_VALUE = {"value", "details", "answer", "info", "information", "entry", "data"}


def plain(text: str, prefer_url: bool = False) -> str:
    """Markdown decoration removed: emphasis, code ticks, links ([text](url) -> the URL when a web address is wanted, else the text)."""
    t = AUTOLINK.sub(r"\1", text)
    t = LINK.sub(lambda m: (m.group(2) if prefer_url else (m.group(1) or m.group(2))), t)
    t = EMPH.sub("", t)
    t = re.sub(r"(?<![A-Za-z0-9])[*_](?=\S)(.+?)(?<=\S)[*_](?![A-Za-z0-9])", r"\1", t)
    return " ".join(t.split())


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


class _Reader:
    def __init__(self, store: BusinessStore, text: str, name: str, scope: str, client_name: str = ""):
        self.scope = scope
        self.new_client = scope == NEW_CLIENT   # the file DESCRIBES A CLIENT THAT DOES NOT EXIST YET: it is created from this file, after review
        self.client_name = client_name.strip()
        self.kind = "me" if scope == "me" else "client"
        raw = text.encode("utf-8", "replace")
        self.parser = Parser(store)
        self.p: Parsed = self.parser.begin(name[:80], hashlib.sha256(raw).hexdigest(), "markdown", scope)
        self.row: ClientRow | None = None
        if self.new_client:
            self.row = ClientRow("~md", "", "", 1)   # no forced id: compared with existing clients (same name / address) exactly like a workbook row
            self.p.clients.append(self.row)
        elif self.kind == "client":
            c = store.get_client(scope)
            if c["archived"]:
                raise StoreError("CLIENT_ARCHIVED", "that client is archived: restore it first")
            self.row = ClientRow("md", c["ref"], c["name"], 0, forced_id=scope)
            self.p.clients.append(self.row)
        self.facts = self.p.owner if self.kind == "me" else self.row.facts
        self.scope_ref = "me" if self.kind == "me" else self.row.ident
        self.seen_fact: dict[str, tuple[int, str]] = {}
        self.seen_rec: set[str] = set()
        self.owner_idx = -1   # which person of a Founder / Owners section is being described
        self.addr: dict[str, dict] = {}
        self.stats = {"lines": 0, "headings": [], "paragraphs": 0, "tables": 0, "list_items": 0, "unread_items": 0, "pairs": 0, "ignored_tables": 0, "code_blocks": 0}

    # ------------------------------------------------------------------ helpers
    def issue(self, level, section, line, col, code, msg):
        self.parser.issue(level, section, line, col, code, msg)

    def add_fact(self, key: str, value, section: str, line: int, label: str, many: bool = False) -> None:
        f = self.parser.make_fact(self.scope_ref, self.kind, key, value, section, line, label, many=many)
        if f is None:
            return
        ident = f.prep.pk.display() if not many else f"{key}:{f.prep.vhash}"
        if ident in self.seen_fact:
            first_line, first_hash = self.seen_fact[ident]
            if not many and f.prep.vhash != first_hash:
                self.issue("warning", section, line, label, "FIELD_TWICE", f"'{label}' is given again with a different value (first on line {first_line}): only the first is a candidate")
            return
        self.seen_fact[ident] = (line, f.prep.vhash)
        self.facts.append(f)

    def add_record(self, kind: str, rec: dict, section: str, line: int, label: str = "") -> None:
        if self.row is None:
            self.issue("info", section, line, label, "NOT_FOR_OWNER", f"a {kind} row belongs to a client: it was not read for your own business (choose a client to import it)")
            return
        try:
            clean = R.clean_record(kind, rec)
        except R.RecordError as e:
            self.issue("error", section, line, label or kind, e.code, e.message)
            return
        key = f"{kind}|{R.rec_key(kind, clean)}"
        if key in self.seen_rec:
            return
        self.seen_rec.add(key)
        self.p.records.append(ParsedRecord(kind, self.row.ident, self.row.ref, clean, section, line, self.parser.new_uid()))

    def unresolved(self, v: str) -> bool:
        return bool(UNRESOLVED_START.search(v) or UNRESOLVED_CAPS.search(v) or UNRESOLVED_SENTENCE.search(v))

    def clean_for(self, key: str, v: str, section: str, line: int, label: str) -> str | None:
        """The value to use for a catalog key, or None when the line is not a value (the reason is recorded; it is never an error and never guessed)."""
        if self.unresolved(v):
            self.issue("info", section, line, label, "LEFT_BLANK", f"'{label}' is marked as not provided / unverified in the file: left blank")
            return None
        try:
            kind = parse_key(key.replace("[]", "[0]"), self.kind).field.kind
        except KeyError_:
            kind = ""
        if kind in STRICT_KINDS:
            v = EXPLAIN_SEP.split(v, 1)[0].strip()   # "value --- a note about it"
            if len(v.split()) > 3 and len(re.findall(r"[A-Za-z]{3,}", v)) >= 3:
                self.issue("info", section, line, label, "LEFT_BLANK", f"'{label}' holds a sentence, not a value: left blank (put just the value after the label to import it)")
                return None
        if key in ("identity.description", "services.description") and URL_ONLY.match(v):
            self.issue("info", section, line, label, "LEFT_BLANK", f"'{label}' is only a web address, which is not a description: left out")
            return None
        return v

    def extra_key(self, nk: str, section: str) -> str | None:
        """A label with no spreadsheet column that the Business Brain still has a field for (own business only), or a plain label inside a Founder / Owners section."""
        if self.kind != "me":
            return None
        if PERSON_SECTION.search(section) and nk in PERSON_LABELS:
            field = PERSON_LABELS[nk]
            if field == "name":
                self.owner_idx += 1
            return f"owners[{max(self.owner_idx, 0)}].{field}"
        return ME_EXTRA.get(nk)

    def label_col(self, label: str):
        d = L.ME if self.kind == "me" else L.CLIENTS
        key = L.hnorm(label)
        for col in d.cols:
            if key in col.names():
                return col, None
        if key in d.ambiguous:
            return None, d.ambiguous[key]
        return None, None

    def kv(self, label: str, value: str, section: str, line: int) -> bool:
        """One `Label: value`. True if the label was understood (even when the value was refused)."""
        label = plain(label)
        if not label:
            return False
        if not value.strip():   # "Phone:" with nothing after it (a blank template line): understood, nothing to read
            nk0 = L.hnorm(label)
            return bool(nk0.replace("_", " ") in LIST_LABELS or (self.kind == "me" and (nk0 in ME_EXTRA or (nk0 in PERSON_LABELS and PERSON_SECTION.search(section)))) or self.label_col(label)[0] is not None)
        cat = field_category(label)
        nk = L.hnorm(label)
        if cat in L.REFUSED_CATEGORIES and nk not in {n for c in (L.ME.cols + L.CLIENTS.cols) for n in c.names()}:
            self.issue("warning", section, line, label, "REFUSED_LABEL", f"'{label}' looks like a {cat} field: never imported (the value was not read)")
            return True
        if nk.replace("_", " ") in LIST_LABELS:
            self.list_label(nk.replace("_", " "), value, section, line, label)
            return True
        xk = self.extra_key(nk, section)
        if xk:
            v = plain(value, prefer_url=xk.endswith(".website"))
            if v:
                v = self.clean_for(xk, v, section, line, label)
                if v:
                    self.stats["pairs"] += 1
                    self.add_fact(xk, v, section, line, label)
            return True
        col, ambiguous = self.label_col(label)
        if col is None:
            if ambiguous:
                self.issue("warning", section, line, label, "AMBIGUOUS_LABEL", f"'{label}' could mean {' or '.join(ambiguous)}: not read (use the exact name, e.g. '{ambiguous[0]}')")
                return True
            return False
        t = col.target
        if t.kind == "ref":
            if self.new_client and not self.row.ref:
                self.row.ref = plain(value)[:40]
            return True
        v = plain(value, prefer_url=bool(col.names() & {"website", "web_site", "url"}))
        if not v:
            return True
        if t.kind == "fact":
            v = self.clean_for(t.key, v, section, line, label)
            if v:
                self.stats["pairs"] += 1
                self.add_fact(t.key, v, section, line, label)
        elif self.unresolved(v):
            self.issue("info", section, line, label, "LEFT_BLANK", f"'{label}' is marked as not provided / unverified in the file: left blank")
        elif t.kind == "multi":
            self.stats["pairs"] += 1
            for item in (x.strip() for x in SPLIT.split(v)):
                if item:
                    self.add_fact(t.key, item, section, line, label, many=True)
        elif t.kind == "addr":
            self.stats["pairs"] += 1
            self.addr.setdefault(t.key, {})[t.part] = (v, section, line, label)
        return True

    def list_label(self, label: str, value: str, section: str, line: int, shown: str) -> None:
        if self.unresolved(value):
            self.issue("info", section, line, shown, "LEFT_BLANK", f"'{shown}' is marked as not provided / unverified in the file: left blank")
            return
        for item in (plain(x) for x in SPLIT.split(value)):
            if item:
                self.list_item_for(label, item, section, line, shown)

    def list_item_for(self, label: str, item: str, section: str, line: int, shown: str) -> None:
        if URL_ONLY.match(item):
            self.issue("info", section, line, shown, "LEFT_BLANK", f"'{shown}' is only a web address, which is not a name: left out")
            return
        if self.kind == "me":
            key = _OWNER_LIST.get(label)
            if key:
                self.add_fact(key, item, section, line, shown, many=True)
            else:
                self.issue("info", section, line, shown, "NOT_FOR_OWNER", f"'{shown}' lists are kept per client: not read for your own business")
            return
        kind, ltype = LIST_LABELS[label]
        rec = {"service": {"service": item}, "location": {"location": item, "location_type": ltype or ""}, "seo": {"keyword": item}}[kind]
        self.add_record(kind, rec, section, line, shown)

    def section_item(self, section_kind: str, item: str, section: str, line: int) -> None:
        item = plain(item)
        if not item:
            return
        label = {"services": "services", "services_additional": "additional services", "service_areas": "service areas", "target_locations": "target locations", "keywords": "keywords"}[section_kind]
        self.list_item_for(label, item, section, line, section)

    # ------------------------------------------------------------------ tables
    def table(self, rows: list[tuple[int, list[str]]], section: str, section_kind: str | None) -> None:
        head_line, head = rows[0]
        body = rows[1:]
        hn = [L.hnorm(h) for h in head]
        self.stats["tables"] += 1
        if len(head) == 2 and hn[0] in KV_TABLE_HEAD and hn[1] in KV_TABLE_VALUE:
            for ln, cells in body:
                if len(cells) >= 2:
                    self.kv(cells[0], cells[1], section, ln)
            return
        if self.row is None:
            self.stats["ignored_tables"] += 1
            self.issue("info", section, head_line, "", "TABLE_IGNORED", "a table of rows was not read for your own business (only two-column Field | Value tables are)")
            return
        for d, kind in ((L.SEO, "seo"), (L.SERVICES, "service"), (L.LOCATIONS, "location")):   # a table with a keyword column is SEO targets even if it also has a location column
            maps = L.map_headers(d, [(i, h, head_line) for i, h in enumerate(head) if h])
            targets: dict[str, L.ColMap] = {}
            for m in maps:
                name = m.target if m.status in (L.CONFIDENT, L.CONFIRMED) else (m.candidates[0] if m.status == L.NEEDS and len(m.candidates) == 1 and d.col(m.candidates[0]) else "")
                if name and name != "client_id":   # in a table of rows, a header with exactly ONE possible meaning ("Type" in a locations table) is that
                    targets.setdefault(name, m)
            main = {"service": ("service",), "location": ("location", "city"), "seo": ("keyword",)}[kind]
            if any(x in targets for x in main):
                for ln, cells in body:
                    rec = {}
                    for col in d.cols:
                        m = targets.get(col.name)
                        if m is not None and m.index < len(cells) and cells[m.index].strip():
                            rec[col.target.key] = plain(cells[m.index])
                    if rec:
                        self.add_record(kind, rec, section, ln)
                return
        self.stats["ignored_tables"] += 1
        self.issue("info", section, head_line, "", "TABLE_IGNORED", "a table was not recognised (its columns are not Field/Value, services, locations or keywords): ignored")

    # ------------------------------------------------------------------ main loop
    def run(self, text: str) -> Parsed:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(lines) > MAX_LINES:
            raise StoreError("TOO_LARGE", f"the file has more than {MAX_LINES} lines")
        self.stats["lines"] = len(lines)
        i = 0
        # simple front matter: key: value lines between --- fences at the very top
        if lines and lines[0].strip() == "---":
            j = next((k for k in range(1, min(len(lines), 60)) if lines[k].strip() == "---"), None)
            if j:
                for k in range(1, j):
                    m = re.match(r"^\s*([A-Za-z][\w \-]{0,40}):\s*(.+?)\s*$", lines[k])
                    if m:
                        self.kv(m.group(1), m.group(2), "front matter", k + 1)
                i = j + 1
        section, sec_kind = "Document", None
        prose: list[str] = []
        about: list[tuple[int, str]] = []
        in_code = in_comment = False
        kinds: dict[int, str] = {}
        pending_list_label: str | None = None
        base_indent: int | None = None   # indent of the first item of the current list: anything indented deeper is a nested detail of its parent and is not read

        def flush_prose():
            if prose:
                self.stats["paragraphs"] += 1
                if sec_kind == "about":
                    about.append((0, " ".join(prose)))
                prose.clear()

        while i < len(lines):
            raw = lines[i]
            n = i + 1
            i += 1
            if in_comment:
                if "-->" in raw:
                    in_comment = False
                continue
            if raw.strip().startswith("<!--"):
                if "-->" not in raw:
                    in_comment = True
                continue
            if FENCE.match(raw):
                flush_prose()
                in_code = not in_code
                if in_code:
                    self.stats["code_blocks"] += 1
                continue
            if in_code:
                continue
            line = clean_text(raw) if raw.strip() else ""
            if line and INSTRUCTION_LIKE.search(line):
                self.issue("info", section, n, "", "INSTRUCTION_LIKE", "this line reads like an instruction to an AI. It is only text in a source file: it is never treated as an instruction")
            if not line:
                flush_prose()
                pending_list_label = None
                base_indent = None
                continue
            h = HEADING.match(line)
            if h:
                flush_prose()
                section = plain(h.group(2))[:80] or "Document"
                level = len(h.group(1))
                sec_kind = SEC_OF.get(L.hnorm(NUMBERED.sub("", section)).replace("_", " "))
                for lv in [lv for lv in kinds if lv >= level]:
                    del kinds[lv]
                if sec_kind is None:   # "### Websites" under "## 6. Services" is still part of the services list
                    parent = max((lv for lv in kinds if kinds[lv] in LISTY), default=None)
                    sec_kind = kinds[parent] if parent is not None else None
                if sec_kind:
                    kinds[level] = sec_kind
                self.stats["headings"].append(section)
                pending_list_label = None
                base_indent = None
                continue
            if line.startswith("|") and i < len(lines) and TABLE_SEP.match(lines[i]):
                flush_prose()
                rows = [(n, _cells(line))]
                i += 1
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append((i + 1, _cells(clean_text(lines[i]))))
                    i += 1
                self.table(rows, section, sec_kind)
                continue
            li = LIST_ITEM.match(line)
            if li:
                flush_prose()
                self.stats["list_items"] += 1
                body = li.group(3)
                indent = len(raw) - len(raw.lstrip())
                if base_indent is None:
                    base_indent = indent
                nested = indent > base_indent
                kvm = KV.match(line) if not nested else None
                if kvm and ":" in body and not body.lower().startswith(("http://", "https://")) and self.kv(kvm.group(1), kvm.group(2), section, n):
                    continue
                if nested:
                    continue   # nested items belong to their parent line and are not read
                if pending_list_label:
                    self.list_item_for(pending_list_label, plain(body), section, n, pending_list_label)
                elif sec_kind in ("services", "services_additional", "service_areas", "target_locations", "keywords"):
                    self.section_item(sec_kind, body, section, n)
                else:
                    self.stats["unread_items"] += 1
                continue
            kvm = KV.match(line)
            if kvm and not kvm.group(2).startswith("//"):
                label, value = kvm.group(1), kvm.group(2)
                if not value.strip() and L.hnorm(label).replace("_", " ") in LIST_LABELS:
                    pending_list_label = L.hnorm(label).replace("_", " ")   # "Services:" followed by bullets
                    continue
                if self.kv(label, value, section, n):
                    continue
            prose.append(plain(line))
        flush_prose()
        # an explicit About / Description section becomes ONE candidate description (never verified by itself)
        if about:
            text_about = " ".join(t for _, t in about).strip()
            if text_about:
                self.add_fact("identity.description", text_about, "About", 0, "About / Description")
                self.stats["paragraphs"] = max(0, self.stats["paragraphs"] - len(about))
        # addresses assemble into ONE value
        for key, parts in self.addr.items():
            first = next(iter(parts.values()))
            self.add_fact(key, {name: v[0] for name, v in parts.items()}, first[1], first[2], "address lines")
        if self.stats["paragraphs"]:
            self.p.warnings.append(f"{self.stats['paragraphs']} paragraph(s) of free text were not turned into values: they stay in your file, and only the labelled lines, tables and lists above became candidates")
        if self.stats["unread_items"]:
            self.p.warnings.append(f"{self.stats['unread_items']} list item(s) are not under a heading such as Services, Service areas or Keywords, so they were not read")
        if self.stats["ignored_tables"]:
            self.p.warnings.append(f"{self.stats['ignored_tables']} table(s) were not recognised and ignored")
        if self.new_client:
            self.name_new_client()
        self.p.info = {k: v for k, v in self.stats.items() if not k.startswith("_")}
        return self.p

    def name_new_client(self) -> None:
        """A client is told apart by its name, so the file (or the upload form) must give one. It is never taken from prose or guessed."""
        def got(key):
            return next((f.prep.canon for f in self.row.facts if f.key == key), "")
        name = got("identity.business_name") or got("identity.trade_name") or got("identity.legal_name")
        if not name and self.client_name:
            self.add_fact("identity.business_name", self.client_name, "Upload form", 0, "Client name")
            name = got("identity.business_name")
        if not name:
            raise StoreError("NO_CLIENT_NAME", "this file does not say which client it is. Add a line such as 'Business name: Example Roofing' to the file, or type the client's name in the box under the file chooser")
        self.row.name = name


def parse_markdown(store: BusinessStore, text: str, *, name: str, scope: str, client_name: str = "") -> Parsed:
    """Markdown text for ONE scope ("me", a client id, or "new" = a client created from this file) -> a plan of candidate values/rows (the same plan type the spreadsheet importer produces)."""
    if not isinstance(text, str) or not text.strip():
        raise StoreError("EMPTY", "the file is empty")
    if "\x00" in text:
        raise StoreError("NOT_TEXT", "that is not a text file")
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise StoreError("TOO_LARGE", f"the file is larger than {MAX_BYTES // 1024} KB")
    if scope not in ("me", NEW_CLIENT):
        store.get_client(scope)   # raises NO_SUCH_CLIENT
    return _Reader(store, text.lstrip("﻿"), name or "knowledge.md", scope, client_name[:120]).run(text.lstrip("﻿"))
