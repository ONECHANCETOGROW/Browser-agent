"""A small, SAFE .xlsx reader and writer built on the standard library only.

Why not a spreadsheet library: the workbook is untrusted INPUT, and this keeps the attack surface tiny and fully reviewable:
  * cells are read as DATA; formulas are never evaluated (only the value Excel cached is read, and formula cells are counted so the preview can say so);
  * macro workbooks, password-protected / legacy (.xls) files, DOCTYPE/entity declarations, external-data parts and over-large or over-compressed archives are refused;
  * strictly bounded: file size, archive entries, uncompressed size, compression ratio, sheets, rows, columns and cell length.
The reader returns plain strings; the importer decides what they mean. The writer produces a conventional workbook (shared strings, styles, data-validation prompts).
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class XlsxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 10 * 1024 * 1024
    max_entries: int = 300
    max_uncompressed: int = 100 * 1024 * 1024
    max_ratio: int = 200                 # compressed -> uncompressed, per entry, once an entry is larger than 1 MiB
    max_sheets: int = 50
    max_rows: int = 20000                # rows per sheet
    max_cols: int = 80
    max_cell_chars: int = 5000


@dataclass
class Row:
    num: int                              # the row number as the user sees it in Excel
    cells: list[str]                      # '' for an empty cell; never None


@dataclass
class Sheet:
    name: str
    rows: list[Row] = field(default_factory=list)
    hidden: bool = False
    formula_cells: int = 0
    error_cells: int = 0
    long_cells: int = 0
    hidden_rows: int = 0


@dataclass
class Workbook:
    sheets: list[Sheet]
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------------------ text hygiene
_ESC = re.compile(r"_x([0-9A-Fa-f]{4})_")


def clean_text(s: str) -> str:
    """Excel's _x000D_ escapes decoded, Unicode NFC, control and invisible format characters removed (they can hide text from a human reviewer), outer whitespace trimmed."""
    s = _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    return s.strip()


def _parse_xml(data: bytes, what: str) -> ET.Element:
    if re.search(rb"<!(?:DOCTYPE|ENTITY)", data, re.I):
        raise XlsxError("MALFORMED", f"the workbook contains an XML DOCTYPE/entity declaration in {what}: refused")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise XlsxError("MALFORMED", f"the workbook is damaged ({what} cannot be read)") from None


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


# ------------------------------------------------------------------------------------------------ dates
_BUILTIN_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_FMT_STRIP = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.')


def _is_date_format(fmt_id: int, custom: dict[int, str]) -> bool:
    if fmt_id in custom:
        code = _FMT_STRIP.sub("", custom[fmt_id].split(";")[0]).lower()
        return bool(re.search(r"[dmyhs]", code)) and "general" not in code
    return fmt_id in _BUILTIN_DATE_IDS


def _serial_to_text(serial: float, date1904: bool) -> str:
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    try:
        d = base + timedelta(days=serial)
    except (OverflowError, ValueError):
        return ""
    return d.strftime("%Y-%m-%d") if abs(serial - int(serial)) < 1e-9 else d.strftime("%Y-%m-%d %H:%M")


def _number_text(v: str) -> str:
    try:
        d = Decimal(v)
    except InvalidOperation:
        return v
    if d == d.to_integral_value() and abs(d) < Decimal(10) ** 18:
        return str(int(d))
    out = format(d, "f")
    return out.rstrip("0").rstrip(".") if "." in out else out


# ------------------------------------------------------------------------------------------------ reader
def read_xlsx(source: str | Path | bytes, limits: Limits = Limits()) -> Workbook:
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes() if Path(source).stat().st_size <= limits.max_file_bytes else None
    if data is None:
        raise XlsxError("TOO_LARGE", f"the file is larger than {limits.max_file_bytes // (1024 * 1024)} MB")
    if len(data) > limits.max_file_bytes:
        raise XlsxError("TOO_LARGE", f"the file is larger than {limits.max_file_bytes // (1024 * 1024)} MB")
    if len(data) == 0:
        raise XlsxError("EMPTY", "the file is empty")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise XlsxError("ENCRYPTED", "this is a password-protected or old-format (.xls) workbook: save an unprotected .xlsx copy (File > Save As > Excel Workbook)")
    if data[:2] != b"PK":
        raise XlsxError("NOT_XLSX", "this is not an .xlsx workbook (it is not a ZIP package)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise XlsxError("MALFORMED", "the workbook is damaged (not a valid ZIP package)") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise XlsxError("TOO_LARGE", "the workbook package has too many parts")
        total = 0
        for i in infos:
            total += i.file_size
            if i.file_size > 1024 * 1024 and i.compress_size and i.file_size / i.compress_size > limits.max_ratio:
                raise XlsxError("TOO_LARGE", "the workbook is compressed suspiciously well (possible zip bomb): refused")
        if total > limits.max_uncompressed:
            raise XlsxError("TOO_LARGE", "the workbook would expand to more than the allowed size: refused")
        names = {i.filename for i in infos}
        if "xl/vbaProject.bin" in names or any(n.lower().endswith("vbaproject.bin") for n in names):
            raise XlsxError("MACRO", "the workbook contains macros: macros are never opened. Save a macro-free .xlsx copy")
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise XlsxError("NOT_XLSX", "this ZIP file is not an Excel workbook (no workbook part)")

        def part(name: str) -> bytes:
            try:
                with zf.open(name) as fh:
                    blob = fh.read(limits.max_uncompressed + 1)
            except KeyError:
                raise XlsxError("MALFORMED", "the workbook is damaged (a part is missing)") from None
            if len(blob) > limits.max_uncompressed:
                raise XlsxError("TOO_LARGE", "a part of the workbook is too large")
            return blob

        ct = part("[Content_Types].xml")
        if b"macroEnabled" in ct:
            raise XlsxError("MACRO", "the workbook is macro-enabled (.xlsm): macros are never opened. Save a macro-free .xlsx copy")
        warnings: list[str] = []
        if any(n.startswith("xl/externalLinks/") for n in names):
            warnings.append("the workbook links to other workbooks: links are ignored, only the cell values stored in this file are read")
        wb = _parse_xml(part("xl/workbook.xml"), "the workbook definition")
        date1904 = (wb.find(f"{NS}workbookPr") is not None) and wb.find(f"{NS}workbookPr").get("date1904") in ("1", "true")
        rels: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in _parse_xml(part("xl/_rels/workbook.xml.rels"), "the workbook relationships"):
                if r.get("TargetMode") == "External":
                    continue
                t = r.get("Target") or ""
                rels[r.get("Id") or ""] = t.lstrip("/") if t.startswith("/") else "xl/" + t
        shared = _read_shared(part("xl/sharedStrings.xml"), limits) if "xl/sharedStrings.xml" in names else []
        xf_dates = _read_styles(part("xl/styles.xml")) if "xl/styles.xml" in names else []
        sheets_el = wb.find(f"{NS}sheets")
        entries = list(sheets_el) if sheets_el is not None else []
        if len(entries) > limits.max_sheets:
            raise XlsxError("TOO_LARGE", "the workbook has too many sheets")
        sheets: list[Sheet] = []
        for el in entries:
            name = clean_text(el.get("name") or "")
            target = rels.get(el.get(f"{NS_R}id") or "")
            if not target or target not in names:
                raise XlsxError("MALFORMED", f"sheet {name!r} cannot be found inside the workbook")
            sh = _read_sheet(part(target), name, shared, xf_dates, date1904, limits)
            sh.hidden = (el.get("state") or "visible") != "visible"
            sheets.append(sh)
        return Workbook(sheets, warnings)


def _read_shared(blob: bytes, limits: Limits) -> list[str]:
    root = _parse_xml(blob, "the shared strings")
    out: list[str] = []
    for si in root.findall(f"{NS}si"):
        parts: list[str] = []
        for ch in si:
            if ch.tag == f"{NS}t":
                parts.append(ch.text or "")
            elif ch.tag == f"{NS}r":
                parts.extend((t.text or "") for t in ch.findall(f"{NS}t"))
        out.append("".join(parts)[: limits.max_cell_chars + 1])
    return out


def _read_styles(blob: bytes) -> list[bool]:
    root = _parse_xml(blob, "the styles")
    custom: dict[int, str] = {}
    nf = root.find(f"{NS}numFmts")
    if nf is not None:
        for f in nf:
            try:
                custom[int(f.get("numFmtId") or -1)] = f.get("formatCode") or ""
            except ValueError:
                continue
    xfs = root.find(f"{NS}cellXfs")
    out: list[bool] = []
    if xfs is not None:
        for xf in xfs:
            try:
                out.append(_is_date_format(int(xf.get("numFmtId") or 0), custom))
            except ValueError:
                out.append(False)
    return out


def _read_sheet(blob: bytes, name: str, shared: list[str], xf_dates: list[bool], date1904: bool, limits: Limits) -> Sheet:
    root = _parse_xml(blob, f"sheet {name!r}")
    sh = Sheet(name)
    data = root.find(f"{NS}sheetData")
    if data is None:
        return sh
    seq = 0
    for r in data.findall(f"{NS}row"):
        seq += 1
        num = int(r.get("r") or seq) if (r.get("r") or "").isdigit() else seq
        if r.get("hidden") in ("1", "true"):
            sh.hidden_rows += 1
        cells: dict[int, str] = {}
        auto = 0
        for c in r.findall(f"{NS}c"):
            ref = c.get("r") or ""
            idx = _col_index(ref) if ref else auto
            auto = idx + 1
            if idx >= limits.max_cols:
                if c.find(f"{NS}v") is not None or c.find(f"{NS}is") is not None:
                    raise XlsxError("TOO_LARGE", f"sheet {name!r} has data beyond column {limits.max_cols}")
                continue
            if c.find(f"{NS}f") is not None:
                sh.formula_cells += 1
            t = c.get("t") or "n"
            v = c.find(f"{NS}v")
            text = ""
            if t == "inlineStr":
                is_ = c.find(f"{NS}is")
                text = "".join((x.text or "") for x in is_.iter(f"{NS}t")) if is_ is not None else ""
            elif v is None or v.text is None:
                text = ""
            elif t == "s":
                try:
                    text = shared[int(v.text)]
                except (ValueError, IndexError):
                    raise XlsxError("MALFORMED", f"sheet {name!r} refers to a text that does not exist") from None
            elif t in ("str",):
                text = v.text
            elif t == "b":
                text = "TRUE" if v.text.strip() in ("1", "true") else "FALSE"
            elif t == "e":
                sh.error_cells += 1
                text = ""
            elif t == "d":
                text = v.text.strip()[:10]
            else:
                s_idx = int(c.get("s") or 0) if (c.get("s") or "0").isdigit() else 0
                if s_idx < len(xf_dates) and xf_dates[s_idx]:
                    try:
                        text = _serial_to_text(float(v.text), date1904)
                    except ValueError:
                        text = ""
                else:
                    text = _number_text(v.text.strip())
            if len(text) > limits.max_cell_chars:
                sh.long_cells += 1
            text = clean_text(text[: limits.max_cell_chars + 1])
            if text:
                cells[idx] = text
        if not cells:
            continue
        if len(sh.rows) >= limits.max_rows:
            raise XlsxError("TOO_LARGE", f"sheet {name!r} has more than {limits.max_rows} rows")
        width = max(cells) + 1
        sh.rows.append(Row(num, [cells.get(i, "") for i in range(width)]))
    return sh


# ------------------------------------------------------------------------------------------------ writer
@dataclass
class Validation:
    """A drop-down list and/or an input hint shown when the cell is selected (guidance inside the workbook, never data)."""
    sqref: str                            # e.g. "F2:F1000"
    items: tuple[str, ...] = ()           # a closed list (blank allowed); empty = just a hint
    title: str = ""
    prompt: str = ""


@dataclass
class SheetSpec:
    name: str
    rows: list[list[str]]
    widths: list[int] = field(default_factory=list)
    kind: str = "table"                   # "table": row 1 is a header, frozen; "doc": wrapped text for reading
    validations: list[Validation] = field(default_factory=list)
    tab_color: str = ""                   # ARGB, e.g. FF1F4E79
    label_column: bool = False            # style the first column as read-only-looking labels (key/value sheets)


_CT = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
       "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
       "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
       "<Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
       "{sheets}"
       "<Override PartName=\"/xl/styles.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml\"/>"
       "<Override PartName=\"/xl/sharedStrings.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml\"/></Types>")
_STYLES = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
           "<styleSheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">"
           "<fonts count=\"3\"><font><sz val=\"11\"/><name val=\"Calibri\"/><family val=\"2\"/></font>"
           "<font><b/><sz val=\"11\"/><color rgb=\"FFFFFFFF\"/><name val=\"Calibri\"/><family val=\"2\"/></font>"
           "<font><b/><sz val=\"11\"/><name val=\"Calibri\"/><family val=\"2\"/></font></fonts>"
           "<fills count=\"4\"><fill><patternFill patternType=\"none\"/></fill><fill><patternFill patternType=\"gray125\"/></fill>"
           "<fill><patternFill patternType=\"solid\"><fgColor rgb=\"FF1F4E79\"/><bgColor indexed=\"64\"/></patternFill></fill>"
           "<fill><patternFill patternType=\"solid\"><fgColor rgb=\"FFF2F2F2\"/><bgColor indexed=\"64\"/></patternFill></fill></fills>"
           "<borders count=\"1\"><border><left/><right/><top/><bottom/><diagonal/></border></borders>"
           "<cellStyleXfs count=\"1\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/></cellStyleXfs>"
           "<cellXfs count=\"6\">"
           "<xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\"/>"                                                                   # 0 default
           "<xf numFmtId=\"49\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\" applyNumberFormat=\"1\"/>"                                          # 1 text: digits typed here stay text (leading zeros survive)
           "<xf numFmtId=\"49\" fontId=\"1\" fillId=\"2\" borderId=\"0\" xfId=\"0\" applyNumberFormat=\"1\" applyFont=\"1\" applyFill=\"1\" applyAlignment=\"1\"><alignment vertical=\"center\" wrapText=\"1\"/></xf>"  # 2 header
           "<xf numFmtId=\"49\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\" applyNumberFormat=\"1\" applyAlignment=\"1\"><alignment vertical=\"top\" wrapText=\"1\"/></xf>"           # 3 wrapped text
           "<xf numFmtId=\"49\" fontId=\"2\" fillId=\"0\" borderId=\"0\" xfId=\"0\" applyNumberFormat=\"1\" applyFont=\"1\" applyAlignment=\"1\"><alignment vertical=\"top\" wrapText=\"1\"/></xf>"  # 4 bold wrapped
           "<xf numFmtId=\"49\" fontId=\"0\" fillId=\"3\" borderId=\"0\" xfId=\"0\" applyNumberFormat=\"1\" applyFill=\"1\" applyAlignment=\"1\"><alignment vertical=\"top\" wrapText=\"1\"/></xf>"  # 5 label
           "</cellXfs><cellStyles count=\"1\"><cellStyle name=\"Normal\" xfId=\"0\" builtinId=\"0\"/></cellStyles></styleSheet>")


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _attr(s: str) -> str:
    return escape(s, {'"': "&quot;", "\n": "&#10;"})


def write_xlsx(path: str | Path | None, sheets: Iterable[SheetSpec]) -> bytes:
    """Write a workbook (every cell a TEXT cell: nothing here can ever be a formula). Returns the bytes; writes them to `path` if given."""
    sheets = list(sheets)
    sst: dict[str, int] = {}
    order: list[str] = []

    def sidx(text: str) -> int:
        if text not in sst:
            sst[text] = len(order)
            order.append(text)
        return sst[text]

    sheet_xml: list[str] = []
    for n, sp in enumerate(sheets, 1):
        ncols = max([len(r) for r in sp.rows] + [len(sp.widths), 1])
        nrows = max(len(sp.rows), 1)
        out = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">"]
        out.append(f"<sheetPr><tabColor rgb=\"{sp.tab_color}\"/></sheetPr>" if sp.tab_color else "")
        out.append(f"<dimension ref=\"A1:{_col_letter(ncols - 1)}{nrows}\"/>")
        sel = " tabSelected=\"1\"" if n == 1 else ""
        if sp.kind == "table":
            out.append(f"<sheetViews><sheetView workbookViewId=\"0\"{sel}><pane ySplit=\"1\" topLeftCell=\"A2\" activePane=\"bottomLeft\" state=\"frozen\"/><selection pane=\"bottomLeft\" activeCell=\"A2\" sqref=\"A2\"/></sheetView></sheetViews>")
        else:
            out.append(f"<sheetViews><sheetView workbookViewId=\"0\"{sel} showGridLines=\"0\"/></sheetViews>")
        out.append("<sheetFormatPr defaultRowHeight=\"15\"/>")
        col_style = 1 if sp.kind == "table" else 3
        out.append("<cols>" + "".join(f"<col min=\"{i + 1}\" max=\"{i + 1}\" width=\"{(sp.widths[i] if i < len(sp.widths) else 22)}\" customWidth=\"1\" style=\"{col_style}\"/>" for i in range(ncols)) + "</cols>")
        out.append("<sheetData>")
        for ri, row in enumerate(sp.rows, 1):
            cells = []
            for ci, val in enumerate(row):
                if val == "" and not (sp.kind == "table" and ri == 1):
                    continue
                if sp.kind == "table":
                    st = 2 if ri == 1 else (5 if (sp.label_column and ci == 0) else 1)
                else:
                    st = 4 if val.startswith("#! ") else 3
                text = val[3:] if (sp.kind != "table" and val.startswith("#! ")) else val
                cells.append(f"<c r=\"{_col_letter(ci)}{ri}\" s=\"{st}\" t=\"s\"><v>{sidx(text)}</v></c>")
            out.append(f"<row r=\"{ri}\">" + "".join(cells) + "</row>" if cells else f"<row r=\"{ri}\"/>")
        out.append("</sheetData>")
        if sp.validations:
            out.append(f"<dataValidations count=\"{len(sp.validations)}\">")
            for v in sp.validations:
                kind = " type=\"list\"" if v.items else ""
                err = " showErrorMessage=\"1\"" if v.items else ""
                hint = f" showInputMessage=\"1\" promptTitle=\"{_attr(v.title[:32])}\" prompt=\"{_attr(v.prompt[:255])}\"" if v.prompt else ""
                listed = chr(34) + ",".join(v.items) + chr(34)
                body = f"<formula1>{escape(listed)}</formula1>" if v.items else ""
                out.append(f"<dataValidation{kind} allowBlank=\"1\"{hint}{err} sqref=\"{v.sqref}\">{body}</dataValidation>")
            out.append("</dataValidations>")
        out.append("<pageMargins left=\"0.7\" right=\"0.7\" top=\"0.75\" bottom=\"0.75\" header=\"0.3\" footer=\"0.3\"/></worksheet>")
        sheet_xml.append("".join(out))

    sst_xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
               f"<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" count=\"{len(order)}\" uniqueCount=\"{len(order)}\">"
               + "".join(f"<si><t xml:space=\"preserve\">{escape(t)}</t></si>" for t in order) + "</sst>")
    wb_xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
              "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
              "<bookViews><workbookView activeTab=\"0\"/></bookViews><sheets>"
              + "".join(f"<sheet name=\"{_attr(sp.name[:31])}\" sheetId=\"{i}\" r:id=\"rId{i}\"/>" for i, sp in enumerate(sheets, 1)) + "</sheets></workbook>")
    n = len(sheets)
    wb_rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
               + "".join(f"<Relationship Id=\"rId{i}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet{i}.xml\"/>" for i in range(1, n + 1))
               + f"<Relationship Id=\"rId{n + 1}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles\" Target=\"styles.xml\"/>"
               + f"<Relationship Id=\"rId{n + 2}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings\" Target=\"sharedStrings.xml\"/></Relationships>")
    root_rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
                 "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT.format(sheets="".join(
            f"<Override PartName=\"/xl/worksheets/sheet{i}.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>" for i in range(1, n + 1))))
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", wb_xml)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/sharedStrings.xml", sst_xml)
        for i, x in enumerate(sheet_xml, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", x)
    data = buf.getvalue()
    if path is not None:
        Path(path).write_bytes(data)
    return data
