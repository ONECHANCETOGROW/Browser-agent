"""The Secure Context Resolver: the ONLY thing the task loop sees of the Business Brain.

What it does, per task:
  * `scope`    is the Business Brain in play for this task? (deterministic intent detection or an explicit switch)
  * `plan`     which fields could the CURRENT page be asking for? -> a manifest of KEYS and STATUSES, never values (minimum necessary context)
  * `prepare`  turn the model's tool call into the real browser call: `{{biz:KEY}}` / `{{doc:ID}}` tokens -> the exact stored value / a validated file
               path, or STOP-and-ask-the-user when a value is missing, unconfirmed or conflicting
  * `redact`   the outbound filter applied to every prompt: known values -> tokens, identifier-shaped strings masked (defence in depth)

The reader has NO write methods. A model cannot reach the store: nothing here accepts a value from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..sensitive import SECRETS, scrub_text, value_category
from .documents import DOC_TYPES, DocNotAvailable, DocumentRegistry
from .schema import CLIENT, CONSTITUTIONS, ME, KeyError_, match_label, parse_key, words
from .store import BusinessStore, NotAvailable, StoreError  # noqa: F401  (StoreError is re-exported for the task layer)

TOKEN_FULL = re.compile(r"^\s*\{\{\s*(biz|client|doc)\s*:\s*([A-Za-z0-9_.\[\]-]{1,80})\s*\}\}\s*$")
TOKEN_ANY = re.compile(r"\{\{\s*(biz|client|doc)\s*:")
CLIENT_INTENT = re.compile(r"\bclients?\b", re.I)
INTENT = re.compile(r"\b(my|our)\s+(own\s+)?(business|company|firm|organi[sz]ation|gst|gstin|pan\b|registration|details|documents?|certificates?|profile|address|trade name|legal name|website)"
                    r"|\bbusiness\s+(profile|details|registration|name|address|information)\b|\bbusiness brain\b|\bupload (my|the) [\w ]{0,30}(certificate|document|proof)", re.I)
# where a token may appear: tool -> parameters. Anything else carrying a token is refused.
TOKEN_PARAMS = {"type": ("text",), "select": ("option",), "upload_file": ("local_file_path", "local_file_paths")}
INPUT_ROLES = {"textbox", "searchbox", "combobox", "listbox", "spinbutton"}
CHOICE_ROLES = {"radio", "checkbox", "switch"}
MAX_MANIFEST = 12


@dataclass
class Fill:
    """One resolved token. `value` is the exact private string (or a file path) and exists only in memory: it is hidden from repr and never logged."""
    kind: str                 # biz | client | doc
    token: str                # the token the model wrote, e.g. {{biz:taxation.gstin}}
    key: str                  # display key or document type
    sensitivity: str
    label: str
    fact_id: str
    source_type: str
    param: str
    scope: str = "me"         # "me" (the owner's own business) or the client id
    who: str = ""             # the client's display name, for approval cards (never sent to a model)
    value: str = field(repr=False, default="")
    doc: dict | None = field(repr=False, default=None)


@dataclass
class CallPlan:
    status: str                       # none | ok | feedback | wait | blocked
    args: dict | None = None          # arguments with every token replaced (execution only)
    fills: list[Fill] = field(default_factory=list)
    message: str = ""
    kind: str = ""                    # wait kind: missing_info | conflict | unverified | blocked


@dataclass
class ManifestEntry:
    key: str
    label: str
    status: str
    sensitivity: str
    mentioned: bool = False


@dataclass
class Manifest:
    entries: list[ManifestEntry] = field(default_factory=list)
    docs: list[dict] = field(default_factory=list)
    text: str = ""
    class1: dict[str, str] = field(default_factory=dict, repr=False)   # key -> value of an ENUM fact, only when the caller allowed it
    class1_keys: list[str] = field(default_factory=list)
    token_kind: str = "biz"
    note: str = ""

    @property
    def keys(self) -> list[str]:
        return [e.key for e in self.entries]

    def __bool__(self) -> bool:
        return bool(self.entries or self.docs or self.note)


_GENERIC = set("the and for with that this from into your you are can will then them they what when where which about open find click press type search page website site please just also "
               "have has had been not but all any one use using make get go to on in of at by as is it its be a an or if so do does did my our client clients selected research "
               "fill business information details company llc inc ltd co corp services service group studio shop store com net org www http https test".split())


@dataclass
class ClientPick:
    client_id: str | None
    name: str = ""
    reason: str = ""
    candidates: list[str] = field(default_factory=list)   # client names that tied (for the "which one?" message)


def pick_client(store: BusinessStore, instruction: str) -> ClientPick:
    """Which client does the wording mean? Deterministic and conservative: only a client whose NAME (display name, trade name or website host) matches a distinctive
    word of the instruction, and only if exactly one does. Anything else is not guessed: the user selects the client."""
    iw = {w for w in words(instruction) if len(w) >= 3 and w not in _GENERIC}
    domains = {m.removeprefix("www.") for m in re.findall(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", (instruction or "").lower())}
    if not iw and not domains:
        return ClientPick(None, reason="the wording does not name a client")
    scored: list[tuple[int, dict]] = []
    for c in store.list_clients():
        names = set(words(c["name"]))
        for key in ("identity.trade_name", "identity.business_name"):
            try:
                names |= set(words(store.get_value(key, c["client_id"]).value))
            except Exception:  # noqa: BLE001 - not set / not verified / unreadable: simply not used for matching
                pass
        host = ""
        try:
            host = store.get_value("contact.website", c["client_id"]).value.lower().split("//")[-1].split("/")[0].removeprefix("www.")
            names |= set(words(host))
        except Exception:  # noqa: BLE001
            pass
        hits = len((names - _GENERIC) & iw) + (10 if host and host in domains else 0)  # the client's own web address in the wording is the strongest signal
        if hits:
            scored.append((hits, c))
    if not scored:
        return ClientPick(None, reason="no client name appears in the wording")
    top = max(h for h, _ in scored)
    best = [c for h, c in scored if h == top]
    if len(best) == 1:
        return ClientPick(best[0]["client_id"], best[0]["name"], "matched by name")
    return ClientPick(None, reason="more than one client matches the wording", candidates=[c["name"] for c in best])


class BrainReader:
    """Per-task, read-only view of the Business Brain, for ONE scope: the owner's own business, or one client. It cannot read any other scope."""

    def __init__(self, store: BusinessStore, docs: DocumentRegistry, task_id: str, instruction: str, *, intent: bool = True, override: bool | None = None,
                 client_id: str | None = None, client_name: str = "", client_reason: str = "", client_intent: bool = False):
        self._store, self._docs = store, docs
        self.task_id, self.instruction = task_id, instruction
        self._values: dict[str, str] = {}          # exact value -> token, for this task only
        self.client_id, self.client_name, self.client_reason = client_id, client_name, client_reason
        self.client_pending = bool(client_intent and not client_id)   # the task is about a client but none is selected yet: ask, never guess
        self.scope = client_id or ME
        self.kind = CLIENT if client_id else ME
        self.active, self.reason = self._scope(instruction, intent, override, bool(client_id) or self.client_pending)
        if self.active and client_id:
            self.reason = f"client {client_name or client_id} ({client_reason or 'selected'})"
        self._mentioned = {pk.storage_key for pk, _ in match_label(instruction, kind=self.kind)} if self.active else set()

    # ---------------------------------------------------------------- scope
    @staticmethod
    def _scope(instruction: str, intent: bool, override: bool | None, client_task: bool = False) -> tuple[bool, str]:
        if override is False:
            return False, "switched off for this task"
        if override is True:
            return True, "switched on for this task"
        if client_task:
            return True, "your task is about a client"
        if intent and INTENT.search(instruction or ""):
            return True, "your task mentions your business information"
        return False, "your task does not mention your business information"

    def select_client(self, client_id: str, name: str, reason: str = "selected by you") -> None:
        """The user picks the client for this task. Refused once any value of another scope was already resolved in this task (no mixing)."""
        if self._values and client_id != self.client_id:
            raise ValueError("values were already filled in for a different scope in this task")
        self.client_id, self.client_name, self.client_reason = client_id, name, reason
        self.client_pending = False
        self.scope, self.kind = client_id, CLIENT
        self.active, self.reason = True, f"client {name} ({reason})"
        self._mentioned = {pk.storage_key for pk, _ in match_label(self.instruction, kind=CLIENT)}

    @property
    def token_kind(self) -> str:
        return "client" if (self.client_id or self.client_pending) else "biz"

    @property
    def mentioned(self) -> set[str]:
        return self._mentioned

    def release(self) -> None:
        """Forget this task's exact values (the registry that protects logs is per task)."""
        SECRETS.clear(self.task_id)
        self._values.clear()

    # ---------------------------------------------------------------- manifest (keys and statuses, never values)
    def plan(self, elements: list[dict], *, allow_values: bool = False) -> Manifest:
        if not self.active:
            return Manifest()
        if self.client_pending:
            m = Manifest(note="This task is about a client but no client is selected yet. Do not guess one and do not use any business detail: call ask_user(kind=info) and ask the user to select the client "
                              "(Business Brain → Clients, or the Client box next to the task), then continue.")
            m.text = m.note
            return m
        seen: dict[str, ManifestEntry] = {}
        want_docs = bool(re.search(r"\b(upload|attach|certificate|document|proof)\b", self.instruction or "", re.I))
        for e in elements:
            role = e.get("role") or e.get("tag")
            if role == "file":
                want_docs = True
            if role in INPUT_ROLES or role in ("textbox",):
                for pk, _ in match_label(e.get("name"), e.get("placeholder"), kind=self.kind):
                    self._add(seen, pk.display(), pk)
            elif role in CHOICE_ROLES:
                name = (e.get("name") or "").strip().lower()
                if self.kind == ME and name and any(name == c.lower() for c in CONSTITUTIONS):
                    pk = parse_key("identity.constitution")
                    self._add(seen, pk.display(), pk)
                else:
                    for pk, _ in match_label(e.get("name"), kind=self.kind):
                        if pk.prompt_ok:
                            self._add(seen, pk.display(), pk)
        if self.kind == CLIENT:
            # a client task often names what it needs without a form ("search for the client's target keyword"): the keys the WORDING asks for are offered too
            for pk, _ in match_label(self.instruction, kind=CLIENT):
                self._add(seen, pk.display(), pk)
        entries = list(seen.values())[:MAX_MANIFEST]
        m = Manifest(entries=entries, token_kind=self.token_kind)
        for ent in entries:
            pk = parse_key(ent.key, self.kind)
            if allow_values and ent.status == "VERIFIED" and pk.prompt_ok and pk.sensitivity != "HIGH":
                try:
                    m.class1[ent.key] = self._store.get_value(ent.key, self.scope).value
                    m.class1_keys.append(ent.key)
                except NotAvailable:
                    pass
        if want_docs:
            m.docs = self._docs.approved_manifest(self.scope)
        m.text = self._render(m)
        return m

    def _add(self, seen: dict[str, ManifestEntry], key: str, pk) -> None:
        if key in seen:
            return
        try:
            st = self._store.status(key, self.scope)
            status = st.status
        except Exception:  # noqa: BLE001 - a broken store must not break the task; the field is simply not available
            status = "MISSING"
        seen[key] = ManifestEntry(key, pk.label, status, pk.sensitivity, pk.storage_key in self._mentioned)

    @staticmethod
    def _render(m: Manifest) -> str:
        if not m:
            return ""
        tk = m.token_kind
        who = "BUSINESS FIELDS" if tk == "biz" else "CLIENT FIELDS (the selected client only; nothing of the owner's own business or of any other client is available)"
        lines = [f"{who} (to use one, write {{{{{tk}:KEY}}}} as the whole text; the agent fills in the real value and you never see it. NEVER guess, invent or retype "
                 "details; if a field is MISSING, CONFLICT or UNVERIFIED call ask_user):"]
        for e in m.entries:
            if e.status == "VERIFIED":
                if e.key in m.class1:
                    desc = f'available, value "{m.class1[e.key]}"'
                elif e.sensitivity == "HIGH":
                    desc = "available (HIGH: the user must approve using it on this site)"
                else:
                    desc = "available"
            elif e.status == "CONFLICTING":
                desc = "CONFLICT (two different stored values: ask_user)"
            elif e.status == "UNVERIFIED":
                desc = "UNVERIFIED (not confirmed by the user: ask_user)"
            else:
                desc = "MISSING (ask_user)"
            lines.append(f"- {e.key} ({e.label}) — {desc}")
        if m.docs:
            lines.append("DOCUMENTS (upload with upload_file local_file_path={{doc:ID}}): " + "; ".join(f"{d['token']} = {d['label']} ({d['count']} approved)" for d in m.docs))
        return "\n".join(lines)

    # ---------------------------------------------------------------- the model's call -> the browser's call
    def prepare(self, tool: str, args: dict, expect: list[dict] | None, el: dict | None = None) -> CallPlan:
        tokens: list[tuple[str, int | None, str, str]] = []   # (param, list index, kind, id)
        params = TOKEN_PARAMS.get(tool, ())
        want = "doc" if tool == "upload_file" else self.token_kind
        for k, v in args.items():
            vals = v if isinstance(v, list) else [v]
            for i, item in enumerate(vals):
                if not isinstance(item, str):
                    continue
                m = TOKEN_FULL.match(item)
                if k in params and m and m.group(1) == want:
                    tokens.append((k, i if isinstance(v, list) else None, m.group(1), m.group(2)))
                elif k in params and m and m.group(1) in ("biz", "client") and tool != "upload_file":
                    # the right KIND of token for a different scope: never silently translated (the model must not choose whose data a task uses)
                    if self.token_kind == "client":
                        return self._refuse("This task is for a specific client: write the client's details as {{client:KEY}}. The owner's own business details are not available in this task.")
                    return self._refuse("No client is selected for this task, so {{client:KEY}} cannot be used. For the owner's own business details write {{biz:KEY}}, "
                                        "or call ask_user to have the user select the client.")
                elif TOKEN_ANY.search(item):
                    return self._refuse(f"A business token may only be used as the entire text of type, the option of select, or the file of upload_file ({{{{doc:ID}}}} for files); "
                                        f"not inside {tool}.{k}" + (" or combined with other text" if k in params else "") + ". Type each part into its own field.")
        for c in expect or []:
            if TOKEN_ANY.search(str(c.get("value", ""))):
                return self._refuse("Business tokens cannot be used in expectations; the agent verifies typed values itself.")
        if not tokens:
            return self._literal_guard(tool, args)
        if self.client_pending and self.active:
            self._audit("blocked", outcome="client_needed", detail=tool)
            return CallPlan("wait", message="Which client is this task for? Select the client in Business Brain → Clients (or the Client box next to the task), then press Resume. I will not guess.", kind="client_needed")
        if not self.active:
            self._audit("blocked", outcome="not_active", detail=f"{tool} used a business token but Business Brain is off for this task")
            return CallPlan("feedback", message="Business Brain is not active for this task, so {{biz:...}} / {{doc:...}} tokens cannot be used. If you need the user's business detail, call ask_user.")
        if el is not None and (el.get("sensitive") or el.get("type") == "password"):
            self._audit("blocked", outcome="sensitive_target", detail=tool)
            return CallPlan("blocked", message="the target is a password or other sensitive field; the user must enter it themselves", kind="sensitive_input")
        out_args = dict(args)
        fills: list[Fill] = []
        for param, idx, kind, ident in tokens:
            try:
                fill = self._resolve_biz(ident, param) if kind in ("biz", "client") else self._resolve_doc(ident, param)
            except _Stop as s:
                return s.plan
            fills.append(fill)
            fill.token = "{{%s:%s}}" % (kind, ident)
            if idx is None:
                out_args[param] = fill.value
            else:
                lst = list(out_args[param])
                lst[idx] = fill.value
                out_args[param] = lst
        return CallPlan("ok", out_args, fills)

    def _refuse(self, msg: str) -> CallPlan:
        self._audit("blocked", outcome="bad_token_use", detail=msg[:120])
        return CallPlan("feedback", message=msg)

    def _literal_guard(self, tool: str, args: dict) -> CallPlan:
        """The model typed something shaped like a government/tax identifier that the user never gave it: block, never type it."""
        if not self.active or tool != "type":
            return CallPlan("none")
        text = args.get("text")
        cat = value_category(text) if isinstance(text, str) else None
        if cat and " ".join(str(text).split()).lower() not in " ".join((self.instruction or "").split()).lower():
            self._audit("blocked", outcome="literal_identifier", detail=cat)
            return CallPlan("feedback", message=f"You tried to type something shaped like a {cat.upper()} identifier that you were not given. Never type identifiers from memory. "
                            "Use a {{biz:KEY}} field from BUSINESS FIELDS, or call ask_user.")
        return CallPlan("none")

    def _resolve_biz(self, key: str, param: str) -> Fill:
        try:
            pk = parse_key(key, self.kind)
        except KeyError_:
            self._audit("blocked", outcome="unknown_field", detail=key[:40])
            raise _Stop(CallPlan("feedback", message=f"{key!r} is not a business field. Use one of the keys listed under BUSINESS FIELDS, or call ask_user."))
        try:
            r = self._store.get_value(key, self.scope)
        except NotAvailable as na:
            raise _Stop(self._unavailable(na.status, pk.display(), pk.label, na.sources))
        except Exception:  # noqa: BLE001 - store failure: ask the user, never guess
            self._audit("error", field_key=pk.display(), outcome="store_error")
            raise _Stop(CallPlan("wait", message=f"I could not read your Business Profile just now, so I cannot fill in {pk.label}. Check the Business Brain, then press Resume.", kind="missing_info"))
        SECRETS.register(r.value, self.task_id)
        self._values[r.value] = "{{%s:%s}}" % (self.token_kind, pk.display())
        self._audit("retrieve", field_key=pk.display(), fact_id=r.fact_id, outcome="served", detail=f"{r.sensitivity} {r.source_type}")
        return Fill(self.token_kind, "", pk.display(), r.sensitivity, pk.label, r.fact_id, r.source_type, param, self.scope, self.client_name, r.value)

    def _resolve_doc(self, ident: str, param: str) -> Fill:
        try:
            path, info = self._docs.resolve_for_upload(ident, self.scope)
        except DocNotAvailable as d:
            self._audit("blocked", doc_id=ident if ident.startswith("doc_") else "", outcome=f"doc_{d.status.lower()}", detail=ident[:40])
            label = d.label or DOC_TYPES.get(ident, ("that document",))[0]
            if d.status == "AMBIGUOUS":
                raise _Stop(CallPlan("wait", message=f"More than one approved {label} matches. Open Business Brain → Documents and leave only the one to use (or exclude the others), then press Resume.", kind="conflict"))
            if d.status in ("CHANGED", "UNAPPROVED"):
                raise _Stop(CallPlan("wait", message=f"The {label} cannot be used: {d.message}. Open Business Brain → Documents, scan and approve it again, then press Resume.", kind="unverified"))
            raise _Stop(CallPlan("wait", message=f"I don't have a usable {label}: {d.message}. Add or approve it in Business Brain → Documents, then press Resume.", kind="missing_info"))
        self._audit("retrieve", doc_id=info["doc_id"], outcome="served", detail=f"{info['sensitivity']} {info['doc_type']}")
        return Fill("doc", "", info["doc_type"], info["sensitivity"], info["label"], info["doc_id"], "DOCUMENT", param, self.scope, self.client_name, path, info)

    def _unavailable(self, status: str, key: str, label: str, sources: list[str]) -> CallPlan:
        src = f" (sources: {', '.join(sources)})" if sources else ""
        if status == "CONFLICTING":
            self._audit("conflict_surfaced", field_key=key, outcome="needs_user")
            return CallPlan("wait", message=f"I found two different {label} values in your authorized information{src}. Open Business Brain and choose which one to use, then press Resume.", kind="conflict")
        if status == "UNVERIFIED":
            self._audit("blocked", field_key=key, outcome="unverified")
            return CallPlan("wait", message=f"I found a {label}{src} but you have not confirmed it yet. Confirm it in Business Brain (or type it into the form yourself), then press Resume.", kind="unverified")
        if status == "UNDECRYPTABLE":
            self._audit("blocked", field_key=key, outcome="undecryptable")
            return CallPlan("wait", message=f"Your stored {label} cannot be read by this Windows account. Re-enter it in Business Brain (or type it into the form yourself), then press Resume.", kind="missing_info")
        self._audit("missing", field_key=key, outcome="needs_user")
        return CallPlan("wait", message=f"I don't have a verified {label} in your Business Profile. Add it in Business Brain (or type it into the form yourself), then press Resume.", kind="missing_info")

    # ---------------------------------------------------------------- outbound filter (defence in depth; the structural rule is that values are never put in prompts)
    def redact(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            if len(v) >= SECRETS.MIN_LEN and v in text:
                text = text.replace(v, self._values[v])
        return scrub_text(text)

    def hides_value(self, el: dict) -> bool:
        """Should this form field's current value be hidden from the model? (a sensitive-labelled field, a sensitive-shaped value, or one of our values)"""
        from ..sensitive import element_category

        v = el.get("value")
        if not isinstance(v, str) or not v:
            return False
        stem = v.rstrip("…").rstrip()
        # the page may show only the start of a long value (fields report at most ~80 characters), so a prefix of one of our values counts too
        known = any(x in v or (len(stem) >= 4 and x.startswith(stem)) for x in self._values)
        return bool(element_category(el)) or value_category(v) is not None or known or SECRETS.contains(v)

    # ---------------------------------------------------------------- audit helpers (no values, ever)
    def _audit(self, event: str, **kw: Any) -> None:
        try:
            self._store.audit(event, task_id=self.task_id, actor="agent", scope=self.scope, **kw)
        except Exception:  # noqa: BLE001 - auditing must never break a task (and never raise a value into a message)
            pass

    def audit_use(self, fill: Fill, *, step: int, origin: str, label: str, outcome: str) -> None:
        self._audit("use", field_key=fill.key if fill.kind != "doc" else "", fact_id=fill.fact_id if fill.kind != "doc" else "", doc_id=fill.fact_id if fill.kind == "doc" else "",
                    outcome=outcome, detail=f"step {step}; {fill.sensitivity}; origin {origin[:60]}; field {scrub_text(label)[:40]!r}")

    def audit_prompt(self, provider: str, locality: str, manifest: Manifest, *, image: bool, purpose: str = "fill form") -> None:
        self._audit("prompt_context", provider=provider, locality=locality, outcome="sent",
                    detail=f"{purpose}; manifest={','.join(manifest.keys)[:150] or '-'}; values={','.join(manifest.class1_keys) or 'none'}; image={'yes' if image else 'no'}")

    def audit_event(self, event: str, **kw: Any) -> None:
        self._audit(event, **kw)


class _Stop(Exception):
    def __init__(self, plan: CallPlan):
        self.plan = plan
