"""Deterministic field extraction from document text: identifier patterns that pass their own validators, plus a few 'Label: value' lines.
It only ever produces CANDIDATES (UNVERIFIED); the user decides what becomes a fact."""
from __future__ import annotations

import re

from .. import validators as v
from ...sensitive import GSTIN_RE, PAN_RE

_LABELED = [
    ("identity.legal_name", re.compile(r"legal\s+name(?:\s+of\s+(?:the\s+)?business)?\s*[:\-–]\s*(.{2,120})", re.I)),
    ("identity.trade_name", re.compile(r"trade\s+name(?:\s*,?\s*if\s+any)?\s*[:\-–]\s*(.{2,120})", re.I)),
]
_CIN = re.compile(r"\b[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}\b")
_TAN = re.compile(r"\b[A-Z]{4}[0-9]{5}[A-Z]\b")


def extract_candidates(text: str) -> list[tuple[str, str]]:
    """[(field_key, value)] in order of appearance, de-duplicated."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(k: str, val: str) -> None:
        if (k, val) not in seen:
            seen.add((k, val))
            out.append((k, val))

    up = text.upper()
    gstin_spans = []
    for m in GSTIN_RE.finditer(up):
        gstin_spans.append(m.span())
        if v.gstin_checksum_ok(m.group(0)):
            add("taxation.gstin", m.group(0))
    for m in PAN_RE.finditer(up):
        if not any(a <= m.start() < b for a, b in gstin_spans):
            add("taxation.pan", m.group(0))
    for m in _CIN.finditer(up):
        add("registration.cin", m.group(0))
    pans = {x for k, x in out if k == "taxation.pan"}
    for m in _TAN.finditer(up):
        if not any(a <= m.start() < b for a, b in gstin_spans) and m.group(0) not in pans:
            add("taxation.tan", m.group(0))
    for key, pat in _LABELED:
        for m in pat.finditer(text):
            val = m.group(1).strip().splitlines()[0].strip(" .\t")
            if val and not re.fullmatch(r"[_\-\s.]*", val) and len(val) >= 2:
                add(key, val)
    return out
