"""CSV as an additional (secondary) input: one CSV file is ONE sheet, named by --sheet or by the file name (clients.csv, client_services.csv ...); a folder of such files is a workbook.

Cells are DATA exactly as in a workbook: read as text, cleaned of control characters, never evaluated.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import layout as L
from .xlsx import Limits, Row, Sheet, Workbook, XlsxError, clean_text


def _decode(data: bytes, warnings: list[str], name: str) -> str:
    if b"\x00" in data:
        raise XlsxError("NOT_CSV", f"{name} is not a text CSV file (it contains binary data)")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        warnings.append(f"{name} is not UTF-8: read as Windows-1252 (save as 'CSV UTF-8' to avoid this)")
        return data.decode("cp1252", errors="replace")


def read_csv_sheet(data: bytes, sheet_name: str, limits: Limits, warnings: list[str], file_name: str) -> Sheet:
    text = _decode(data, warnings, file_name)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    old_limit = csv.field_size_limit(limits.max_cell_chars + 10)
    sh = Sheet(sheet_name)
    try:
        for i, rec in enumerate(csv.reader(io.StringIO(text, newline=""), dialect), 1):
            cells = [clean_text(x[: limits.max_cell_chars + 1]) for x in rec[: limits.max_cols + 1]]
            if len(cells) > limits.max_cols and any(cells[limits.max_cols:]):
                raise XlsxError("TOO_LARGE", f"{file_name} has data beyond column {limits.max_cols}")
            cells = cells[: limits.max_cols]
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue
            if len(sh.rows) >= limits.max_rows:
                raise XlsxError("TOO_LARGE", f"{file_name} has more than {limits.max_rows} rows")
            sh.rows.append(Row(i, cells))
    except csv.Error:
        raise XlsxError("MALFORMED", f"{file_name} could not be read as CSV (a cell is too long or the quoting is broken)") from None
    finally:
        csv.field_size_limit(old_limit)
    return sh


def sheet_name_for(path: Path, explicit: str | None) -> str:
    if explicit:
        d = L.sheet_for_name(explicit)
        if d is None:
            raise XlsxError("UNKNOWN_SHEET", f"--sheet {explicit!r} is not one of: " + ", ".join(x.name for x in L.SHEETS))
        return d.name
    d = L.sheet_for_name(path.stem.replace("_", " ").replace("-", " "))
    if d is None:
        raise XlsxError("UNKNOWN_SHEET", f"cannot tell which sheet {path.name} is: name the file after the sheet (e.g. Clients.csv) or pass --sheet \"Clients\"")
    return d.name


def read_csv_source(path: Path, sheet: str | None, limits: Limits = Limits()) -> Workbook:
    warnings: list[str] = []
    files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".csv") if path.is_dir() else [path]
    if not files:
        raise XlsxError("EMPTY", "no .csv files in that folder")
    sheets: list[Sheet] = []
    for f in files:
        if f.stat().st_size > limits.max_file_bytes:
            raise XlsxError("TOO_LARGE", f"{f.name} is larger than {limits.max_file_bytes // (1024 * 1024)} MB")
        sheets.append(read_csv_sheet(f.read_bytes(), sheet_name_for(f, sheet if not path.is_dir() else None), limits, warnings, f.name))
    return Workbook(sheets, warnings)
