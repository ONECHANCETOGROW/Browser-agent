"""Out-of-process text extraction. Run as:  python -I worker.py <path> <ext>   (stdlib only, no network, no rendering, no execution).

The parent enforces a wall-clock timeout and an output cap and kills this process if either is exceeded, so a hostile or broken file costs at most
one dead worker. Prints ONE JSON line: {"ok": bool, "text": str, "extractor": str, "note": str}.
"""
import csv
import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET

MAX_READ = 8 * 1024 * 1024
MAX_TEXT = 400_000
MAX_ZIP_ENTRIES = 400
MAX_ZIP_UNCOMPRESSED = 40 * 1024 * 1024
MAX_RATIO = 120


def out(ok, text="", extractor="", note=""):
    sys.stdout.write(json.dumps({"ok": ok, "text": text[:MAX_TEXT], "extractor": extractor, "note": note}))
    sys.stdout.flush()


def read_limited(path):
    with open(path, "rb") as f:
        data = f.read(MAX_READ + 1)
    if len(data) > MAX_READ:
        raise ValueError("file too large to extract")
    return data


def decode(b):
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return b.decode(enc)
        except (UnicodeError, ValueError):
            continue
    return b.decode("utf-8", "replace")


def flatten(obj, prefix=""):
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            lines += flatten(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            lines += flatten(v, f"{prefix}{i}.")
    else:
        lines.append(f"{prefix.rstrip('.')}: {obj}")
    return lines


def safe_zip(path):
    z = zipfile.ZipFile(path)
    infos = z.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError("archive has too many entries")
    total = 0
    for i in infos:
        total += i.file_size
        if i.file_size > 1_000_000 and i.compress_size > 0 and i.file_size / i.compress_size > MAX_RATIO:
            raise ValueError("suspicious compression ratio")
    if total > MAX_ZIP_UNCOMPRESSED:
        raise ValueError("archive expands too much")
    return z


def parse_xml(data):
    if b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML with a DOCTYPE or entities is refused")
    return ET.fromstring(data)


def docx_text(path):
    z = safe_zip(path)
    root = parse_xml(z.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in root.iter(ns + "p"):
        paras.append("".join(t.text or "" for t in p.iter(ns + "t")))
    return "\n".join(paras)


def xlsx_text(path):
    z = safe_zip(path)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in parse_xml(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    lines = []
    for name in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))[:10]:
        root = parse_xml(z.read(name))
        for row in root.iter(ns + "row"):
            cells = []
            for c in row.iter(ns + "c"):
                v = c.find(ns + "v")
                if c.get("t") == "s" and v is not None and v.text and v.text.isdigit() and int(v.text) < len(shared):
                    cells.append(shared[int(v.text)])
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(ns + "t")))
                elif v is not None and v.text:
                    cells.append(v.text)
            if cells:
                lines.append("\t".join(cells))
            if len(lines) > 20000:
                break
    return "\n".join(lines)


def pdf_text(path):
    try:
        import pypdf  # optional dependency
    except ImportError:
        return None
    r = pypdf.PdfReader(path)
    if r.is_encrypted:
        raise ValueError("encrypted PDF")
    return "\n".join((page.extract_text() or "") for page in r.pages[:60])


def main():
    path, ext = sys.argv[1], sys.argv[2].lower()
    try:
        if ext in (".txt", ".csv"):
            text = decode(read_limited(path))
            if ext == ".csv":
                text = "\n".join(", ".join(r) for r in csv.reader(io.StringIO(text)))
            return out(True, text, "text")
        if ext == ".json":
            return out(True, "\n".join(flatten(json.loads(decode(read_limited(path))))), "json")
        if ext == ".docx":
            return out(True, docx_text(path), "docx")
        if ext == ".xlsx":
            return out(True, xlsx_text(path), "xlsx")
        if ext == ".pdf":
            t = pdf_text(path)
            if t is None:
                return out(False, "", "pdf", "PDF text extraction needs the optional pypdf package (not installed)")
            return out(True, t, "pdf", "" if t.strip() else "no text layer (a scan: OCR is not enabled)")
        return out(False, "", "none", "this file type is registered but its content is not read (OCR is not enabled)")
    except Exception as e:  # noqa: BLE001 - hostile input must never crash the parent; report a short reason only
        return out(False, "", ext.lstrip("."), f"could not read: {type(e).__name__}")


if __name__ == "__main__":
    main()
