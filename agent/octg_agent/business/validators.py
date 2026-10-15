"""Format checks for business identifiers. A failed check is a WARNING stored with the fact (`format_ok`), never a silent fix or a rewrite."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from ..sensitive import card_like

PAN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
GSTIN = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
CIN = re.compile(r"[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}")
LLPIN = re.compile(r"[A-Z]{3}-[0-9]{4}")
TAN = re.compile(r"[A-Z]{4}[0-9]{5}[A-Z]")
UDYAM = re.compile(r"UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}")
PIN = re.compile(r"[1-9][0-9]{5}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_checksum_ok(v: str) -> bool:
    """The published GSTIN check character (base-36 weighted sum over the first 14 characters)."""
    if len(v) != 15:
        return False
    total = 0
    for i, ch in enumerate(v[:14]):
        n = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += n // 36 + n % 36
    return _CHARS[(36 - total % 36) % 36] == v[14]


def gstin_with_checksum(first14: str) -> str:
    """Helper for synthetic test data: append the correct check character."""
    total = 0
    for i, ch in enumerate(first14):
        n = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += n // 36 + n % 36
    return first14 + _CHARS[(36 - total % 36) % 36]


def _upper(v: str) -> str:
    return v.strip().upper()


def check_pan(v: str):
    u = _upper(v)
    return (bool(PAN.fullmatch(u)), "" if PAN.fullmatch(u) else "does not look like a PAN (5 letters, 4 digits, 1 letter)")


def check_gstin(v: str):
    u = _upper(v)
    if not GSTIN.fullmatch(u):
        return False, "does not look like a GSTIN (15 characters)"
    return (True, "") if gstin_checksum_ok(u) else (False, "GSTIN check character does not match")


def check_cin(v: str):
    ok = bool(CIN.fullmatch(_upper(v)))
    return ok, "" if ok else "does not look like a CIN (21 characters)"


def check_llpin(v: str):
    ok = bool(LLPIN.fullmatch(_upper(v)))
    return ok, "" if ok else "does not look like an LLPIN (AAA-1234)"


def check_tan(v: str):
    ok = bool(TAN.fullmatch(_upper(v)))
    return ok, "" if ok else "does not look like a TAN"


def check_udyam(v: str):
    ok = bool(UDYAM.fullmatch(_upper(v)))
    return ok, "" if ok else "does not look like an Udyam number (UDYAM-XX-00-0000000)"


def check_email(v: str):
    ok = bool(EMAIL.fullmatch(v.strip()))
    return ok, "" if ok else "does not look like an e-mail address"


def check_phone(v: str):
    digits = re.sub(r"\D", "", v)
    ok = 8 <= len(digits) <= 15
    return ok, "" if ok else "a phone number has 8-15 digits"


def check_url(v: str):
    u = urlparse(v.strip() if "://" in v else "https://" + v.strip())
    ok = bool(u.netloc and "." in u.netloc)
    return ok, "" if ok else "does not look like a web address"


def check_date(v: str):
    try:
        date.fromisoformat(v.strip())
        return True, ""
    except ValueError:
        return False, "use the ISO form YYYY-MM-DD"


def check_pin(v: str):
    ok = bool(PIN.fullmatch(v.strip()))
    return ok, "" if ok else "a PIN code has 6 digits"


def check_digits(v: str):
    ok = bool(re.fullmatch(r"[0-9]{2,8}", v.strip()))
    return ok, "" if ok else "expected digits only"


def check_nonempty(v: str):
    ok = bool(v.strip())
    return ok, "" if ok else "empty"


VALIDATORS = {"pan": check_pan, "gstin": check_gstin, "cin": check_cin, "llpin": check_llpin, "tan": check_tan, "udyam": check_udyam, "email": check_email, "phone": check_phone,
              "url": check_url, "date": check_date, "pin": check_pin, "digits": check_digits, "text": check_nonempty, "enum": check_nonempty, "none": lambda v: (True, "")}

# ---- VERY HIGH: never storable
SECRET_NAME = re.compile(r"pass(word|code|wd)|(?<![a-z])otp(?![a-z])|one[ _-]?time|secret|api[ _-]?key|token|(?<![a-z])cvv(?![a-z])|(?<![a-z])cvc(?![a-z])|card[ _-]?(number|no)|private[ _-]?key|pwd", re.I)
SECRET_VALUE = re.compile(r"^(sk|pk|rk)[-_][A-Za-z0-9_-]{16,}$|^AIza[0-9A-Za-z_-]{20,}$|^gh[pousr]_[A-Za-z0-9]{20,}$|^xox[abprs]-[A-Za-z0-9-]{10,}$|^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|^freellmapi-[0-9a-f]{20,}$|"
                          r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def secret_reason(field_name: str, value: str) -> str | None:
    """Why this must not be stored (it is a password / OTP / key / payment credential), or None."""
    if SECRET_NAME.search(field_name or ""):
        return "looks like a password, OTP, key or payment credential"
    v = (value or "").strip()
    if SECRET_VALUE.search(v):
        return "looks like an API key, token or private key"
    if card_like(v):
        return "looks like a payment card number"
    return None


def normalize_for_compare(kind: str, value: str) -> str:
    """Comparison form only; the stored value is never altered."""
    v = " ".join(value.split())
    if kind in ("pan", "gstin", "cin", "llpin", "tan", "udyam"):
        return v.upper().replace(" ", "")
    if kind == "email":
        return v.lower()
    if kind == "phone":
        d = re.sub(r"\D", "", v)
        return d[-10:] if len(d) > 10 else d
    if kind == "url":
        return v.lower().rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")
    if kind == "date":
        return v
    return v.lower()
