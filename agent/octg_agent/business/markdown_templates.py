"""Blank Markdown profile templates for the Knowledge Hub (one for your own business, one per client).

Each is plain Markdown the Hub's Markdown reader understands (see markdown_import.py): `Label: value` lines, a Services / Service areas / Keywords list, a description section.
Leave a value empty (or delete the line) to skip it. Nothing is invented: an empty label is simply not read. Protected identifiers (tax ids, passwords, bank or card details) do
not belong in a Markdown file: type those into the Hub's protected fields.
"""
from __future__ import annotations

CLIENT = """# Client profile

<!-- Fill in what you know. Leave a value empty (or delete the line) to skip it. Nothing is saved until you review it in the Knowledge Hub. -->
<!-- The business name is the only required line. One file = one client. -->

## Business information

- **Business name:**
- **Legal name:**
- **Trade name:**
- **Client ID:**

## Contact

- **Website:**
- **Contact person:**
- **Email:**
- **Phone:**

## Primary address

- **Address line 1:**
- **Address line 2:**
- **City:**
- **State:**
- **Postal code:**
- **Country:**

## Account

- **Status:**
- **Plan:**
- **Onboarding date:**

## Business description

<!-- Write a few sentences about the client's business below this line, then delete this comment. -->

## Services

<!-- One service per line, like:  - Emergency plumbing -->

## Service areas

<!-- One place per line, like:  - Springfield -->

## Target locations

<!-- Places the client wants to rank in, one per line -->

## SEO targets

| Keyword | Location | Priority | Service |
|---|---|---|---|
"""

ME = """# Business profile

<!-- Fill in what you know. Leave a value empty (or delete the line) to skip it. Nothing is saved until you review it in the Knowledge Hub. -->

## Business identity

- **Legal entity name:**
- **Trade name:**
- **Entity type:**
- **Incorporation date:**

## Contact

- **Website:**
- **General email:**
- **Support email:**
- **Phone:**

## Address

- **Address line 1:**
- **Address line 2:**
- **City:**
- **State:**
- **Postal code:**
- **Country:**

## Founder

- **Full name:**
- **Role:**
- **Email:**

## Business description

<!-- Write a few sentences about your business below this line, then delete this comment. -->

## Services

<!-- One service per line, like:  - Local SEO -->
"""

TEMPLATES = {"client": ("OCTG_Client_Profile_TEMPLATE.md", CLIENT), "me": ("OCTG_Business_Profile_TEMPLATE.md", ME)}


def template(kind: str) -> tuple[str, str]:
    """(file name, text) of the blank profile for "client" or "me"."""
    if kind not in TEMPLATES:
        raise KeyError(kind)
    return TEMPLATES[kind]
