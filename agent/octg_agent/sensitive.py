"""Generic sensitive-value protection for operational logs, task activity, history and error text.

This module is deliberately neutral: it imports nothing from the engine, the AI layer or the Business Brain, so every layer can use it.

Three mechanisms (all best-effort defence in depth; the strongest protection is never to put a value there in the first place):
  1. `field_category(label text / element)`: is this FIELD one that holds credentials, OTPs, card/payment data, tax or government identifiers,
     or bank/account identifiers? (decided from the field's own name/label/type, never from a website list)
  2. `looks_sensitive_value(text)` / `scrub_text(text)`: does this VALUE have the shape of a PAN, GSTIN, IFSC, card number or a 12-digit
     government-ID number?
  3. `SECRETS`: a process-wide registry of EXACT values that the agent itself resolved from the Business Brain. Anything that is about to be
     written to a log/activity/history/error and contains one of them is redacted, whatever its shape (so a synthetic or oddly formatted
     identifier is still protected).
"""
from __future__ import annotations

import re
import threading
from typing import Any

REDACTED = "[redacted]"

PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
GSTIN_RE = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
UDYAM_RE = re.compile(r"\bUDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}\b")
AADHAAR_LIKE_RE = re.compile(r"(?<!\d)(?<!\d[ -])\d{4}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

# field labels (name / placeholder / aria-label / type / autocomplete) that identify a sensitive FIELD
_FIELD_HINTS: list[tuple[str, re.Pattern]] = [
    ("password", re.compile(r"pass(word|code|wd)|\bpwd\b", re.I)),
    ("otp", re.compile(r"(?<![a-z])otp(?![a-z])|one[- ]?time|verification code|security code|\b2fa\b|\bmfa\b|auth(entication)? code", re.I)),
    ("card", re.compile(r"card ?(number|no\b|num)|\bcc-|\bcvv\b|\bcvc\b|expir(y|ation)|credit card|debit card|name on card", re.I)),
    ("pan", re.compile(r"\bpan\b(?! ?code)|permanent account", re.I)),
    ("gstin", re.compile(r"\budyam\b|\bmsme registration\b|\bgstin\b|\bgst ?(no|number|id|registration)\b|tax (id|identification)|\bvat ?(no|number|id)\b|\btin\b|\btan\b", re.I)),
    ("bank", re.compile(r"account ?(no\b|number|num)|bank account|\biban\b|\bifsc\b|routing number|\bswift\b|sort code", re.I)),
    ("identity", re.compile(r"aadhaar|aadhar|\bssn\b|social security|passport ?(no|number)|national id", re.I)),
    ("secret", re.compile(r"secret|api[ _-]?key|access[ _-]?token|private key", re.I)),
]


def field_category(*texts: Any, typ: str | None = None, autocomplete: str | None = None) -> str | None:
    """The kind of sensitive data a FIELD holds (password/otp/card/pan/gstin/bank/identity/secret), from its own labels; None if ordinary."""
    if (typ or "").lower() == "password":
        return "password"
    ac = (autocomplete or "").lower()
    if re.search(r"(current|new)-password", ac):
        return "password"
    if "one-time-code" in ac:
        return "otp"
    if re.search(r"\bcc-", ac):
        return "card"
    blob = " ".join(str(t) for t in texts if t)
    for cat, pat in _FIELD_HINTS:
        if pat.search(blob):
            return cat
    return None


def element_category(el: dict | None) -> str | None:
    """Same, for an element dict as produced by inspect_page."""
    if not el:
        return None
    if el.get("sensitive"):
        return field_category(el.get("name"), el.get("placeholder"), typ=el.get("type")) or "secret"
    return field_category(el.get("name"), el.get("placeholder"), el.get("label"), el.get("id"), typ=el.get("type"), autocomplete=el.get("autocomplete"))


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def card_like(s: str) -> bool:
    d = _digits(s)
    return 13 <= len(d) <= 19 and _luhn(d) and len(set(d)) > 1


def value_category(text: Any) -> str | None:
    """If the WHOLE string has the shape of a sensitive identifier, which kind."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s or len(s) > 40:
        return None
    if GSTIN_RE.fullmatch(s):
        return "gstin"
    if UDYAM_RE.fullmatch(s.upper()):
        return "gstin"  # a registration/tax identifier: the same protection as a GSTIN
    if PAN_RE.fullmatch(s):
        return "pan"
    if IFSC_RE.fullmatch(s):
        return "bank"
    if re.fullmatch(r"[\d -]+", s):
        if card_like(s):
            return "card"
        if AADHAAR_LIKE_RE.fullmatch(s):
            return "identity"
    return None


def looks_sensitive_value(text: Any) -> bool:
    return value_category(text) is not None


class SecretRegistry:
    """Exact values currently known to be private (resolved Business Brain values). In memory only; cleared per owner (task)."""

    MIN_LEN = 4

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_owner: dict[str, set[str]] = {}
        self._flat: list[str] = []

    def _rebuild(self) -> None:
        vals: set[str] = set()
        for s in self._by_owner.values():
            vals |= s
        self._flat = sorted(vals, key=len, reverse=True)

    def register(self, value: str, owner: str = "") -> None:
        if not isinstance(value, str):
            return
        variants = {value, " ".join(value.split())}
        variants = {v for v in variants if len(v) >= self.MIN_LEN}
        if not variants:
            return
        with self._lock:
            self._by_owner.setdefault(owner, set()).update(variants)
            self._rebuild()

    def clear(self, owner: str | None = None) -> None:
        with self._lock:
            if owner is None:
                self._by_owner.clear()
            else:
                self._by_owner.pop(owner, None)
            self._rebuild()

    def contains(self, text: str) -> bool:
        flat = self._flat
        return bool(flat) and any(v in text for v in flat)

    def scrub(self, text: str) -> str:
        for v in self._flat:
            if v in text:
                text = text.replace(v, REDACTED)
        return text

    def __len__(self) -> int:
        return len(self._flat)


SECRETS = SecretRegistry()


def scrub_text(text: str) -> str:
    """Redact registered exact values and anything shaped like a PAN/GSTIN/IFSC/card/12-digit government ID. Safe on any string."""
    if not isinstance(text, str) or not text:
        return text
    text = SECRETS.scrub(text)
    if len(text) < 9:
        return text
    text = GSTIN_RE.sub("[redacted:gstin]", text)
    text = UDYAM_RE.sub("[redacted:udyam]", text)
    text = PAN_RE.sub("[redacted:pan]", text)
    text = IFSC_RE.sub("[redacted:bank]", text)
    text = CARD_RE.sub(lambda m: "[redacted:card]" if card_like(m.group(0)) else m.group(0), text)
    text = AADHAAR_LIKE_RE.sub("[redacted:id]", text)
    return text


def redact_call_params(params: dict, *, category: str | None, forced: bool) -> tuple[dict, bool]:
    """For the action log: hide the text a call enters into a sensitive field (or any text, for a caller-declared sensitive call, or text whose
    shape is a sensitive identifier). Returns (params, was_redacted)."""
    out = dict(params)
    hit = False
    for k in ("text", "option"):
        v = out.get(k)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and (forced or category or looks_sensitive_value(str(v)) or SECRETS.contains(str(v))):
            out[k] = f"[redacted: {category or 'sensitive'} value]"
            hit = True
    if isinstance(out.get("values"), list) and (forced or category):
        out["values"] = [f"[redacted: {category or 'sensitive'} value]" for _ in out["values"]]
        hit = True
    return out, hit
