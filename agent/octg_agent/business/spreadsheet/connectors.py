"""Where tabular data can come FROM. Every connector hands the same thing to the same pipeline: a `Workbook` (sheets of rows of text) that `Parser` validates, `Differ` compares and `apply_plan` writes.

    file (.xlsx / .csv)        available now        reads a file the user chose
    markdown                   available now        its own reader (business/markdown_import.py) building the same plan type
    google_sheets              future               a connector would fetch the sheet and return a Workbook; NOTHING below it changes
    pdf                        future               text extraction would produce candidates for the same review

A future connector therefore needs no database of its own and no security code of its own: it only has to produce rows. The registry below is what the Knowledge Hub shows ("coming later").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .xlsx import Workbook


class SourceConnector(Protocol):
    kind: str
    label: str
    available: bool

    def load(self, ref: str) -> tuple[Workbook, str, str]:
        """(workbook, display name, content fingerprint). Raises on failure; never writes anything."""


@dataclass(frozen=True)
class FileConnector:
    kind: str = "spreadsheet"
    label: str = "Excel / CSV file"
    available: bool = True

    def load(self, ref: str) -> tuple[Workbook, str, str]:
        import hashlib
        from .xlsx import read_xlsx
        data = Path(ref).read_bytes()
        return read_xlsx(data), Path(ref).name[:80], hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PlannedConnector:
    kind: str
    label: str
    available: bool = False
    note: str = "planned: it will feed the same validation and review pipeline"

    def load(self, ref: str):
        raise NotImplementedError(f"the {self.label} connector does not exist yet")


CONNECTORS: dict[str, SourceConnector] = {
    "spreadsheet": FileConnector(),
    "markdown": FileConnector("markdown", "Markdown knowledge file"),
    "manual": FileConnector("manual", "Entered by hand in the Knowledge Hub"),
    "document": FileConnector("document", "Authorized documents"),
    "google_sheets": PlannedConnector("google_sheets", "Google Sheets"),
    "pdf": PlannedConnector("pdf", "PDF"),
}


def describe_connectors() -> list[dict]:
    return [{"kind": k, "label": c.label, "available": bool(c.available), "note": getattr(c, "note", "")} for k, c in CONNECTORS.items()]
