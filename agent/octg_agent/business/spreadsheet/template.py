"""The blank workbook template. It contains NO business data: header rows and guidance only, so nothing in it can ever be imported by accident. Worked examples live on the
README sheet (which the importer never reads) and are marked as examples."""
from __future__ import annotations

from pathlib import Path

from . import layout as L
from .xlsx import SheetSpec, Validation, _col_letter, write_xlsx

TEMPLATE_NAME = "OCTG_Business_Data_TEMPLATE.xlsx"
LAST_ROW = 2000
TAB = {"me": "FF1F4E79", "clients": "FF2E7D32", "services": "FF8E6C00", "locations": "FF8E6C00", "seo": "FF6A1B9A"}


def _readme() -> SheetSpec:
    rows: list[list[str]] = []
    add = lambda *lines: rows.extend([[x] for x in lines])   # noqa: E731
    add("#! OCTG Business Data workbook",
        "This workbook is how you give the OCTG agent your business and your clients' information. Fill in the sheets, save the file as .xlsx, then import it from the terminal.",
        "",
        "#! How to import",
        "1. Save your filled workbook under a NEW name outside the agent's folder (for example D:\\OCTG_Data\\OCTG_Business_Data.xlsx). Keep this template blank.",
        "2. Preview only (writes nothing):   python -m octg_agent business import \"D:\\OCTG_Data\\OCTG_Business_Data.xlsx\" --dry-run",
        "3. Import for real:   python -m octg_agent business import \"D:\\OCTG_Data\\OCTG_Business_Data.xlsx\"",
        "   The agent shows a preview first (what is new, changed, unchanged, missing, in conflict, protected), asks about anything unclear, makes a backup, and only writes after you confirm.",
        "",
        "#! Rules",
        "1. BLANK MEANS MISSING. Leave a cell empty when you do not know it. Nothing is guessed, copied from another client or from your own business, or invented.",
        "2. NEVER put passwords, one-time codes (OTPs), API keys, payment-card numbers, bank account numbers or Aadhaar numbers in this workbook. Those columns/values are refused.",
        "3. Protected fields (PAN, GSTIN, Udyam, TAN, CIN, internal notes) are stored encrypted with Windows protection and are never shown to an AI model.",
        "4. Every cell is plain data. Text such as \"ignore previous instructions\" would be stored as ordinary text and never obeyed. Formulas are never calculated; macros are never opened.",
        "5. Nothing existing is overwritten silently. If the workbook disagrees with a value you already confirmed, the agent asks you: keep the existing value, or use the workbook's.",
        "6. If a client in the workbook looks like one you already have (same name or web address), the agent asks whether they are the same client. It never merges on its own.",
        "7. The import never deletes anything. Rows missing from the workbook are left alone.",
        "8. Keep phone numbers, PIN / ZIP codes and ids as TEXT (the sheets are already formatted as text), so leading zeros are not lost.",
        "9. Do not rename the sheets or the header cells. You may reorder columns, and the importer recognises some alternative names; if a header could mean two things it asks you.",
        "",
        "#! The sheets")
    for d in L.SHEETS:
        add(f"#! {d.name}", d.purpose)
        if d.layout == "kv":
            add("Columns: Field | Value | Guidance (Guidance is for you and is ignored). Put each value in the Value column next to its Field.")
        add("Fields: " + "; ".join(c.name + (" (required)" if c.required else "") for c in d.cols), "")
    add("#! Linking the client sheets",
        "The Clients sheet has one row per client with your own id in client_id (for example CLIENT-001). The sheets Client Services, Client Locations and Client SEO use the same client_id on every row to say which client the row belongs to. A client_id that is not on the Clients sheet (and not already stored) is refused.",
        "",
        "#! EXAMPLES (illustration only: this sheet is never imported)",
        "Client Services:   CLIENT-001 | Emergency Locksmith | Locksmith | High | 24/7",
        "Client Locations:  CLIENT-001 | Example Town | Example Town | EX | USA | 00000 | primary | High |",
        "Client SEO:        CLIENT-001 | emergency locksmith | Example Town, EX | High | Emergency Locksmith |",
        "These lines are examples of the layout only. Do not copy them into the real sheets unless they are true.",
        "",
        "#! Allowed values",
        "priority: High, Medium or Low.    location_type: primary, service_area, target_area or other.    Dates: a real date cell, or text written YYYY-MM-DD.",
        "Several services in one My Business cell: separate with a semicolon (;) or put each on its own line.")
    return SheetSpec("README", rows, [130], kind="doc", tab_color="FF9E9E9E")


def _table(d: L.SheetDef) -> SheetSpec:
    headers = [c.name for c in d.cols]
    vals: list[Validation] = []
    for i, c in enumerate(d.cols):
        rng = f"{_col_letter(i)}2:{_col_letter(i)}{LAST_ROW}"
        items = L.PRIORITIES if c.name == "priority" else L.LOCATION_TYPES if c.name == "location_type" else ()
        hint = (c.hint + (" Required." if c.required else "")).strip()
        vals.append(Validation(rng, tuple(items), c.name, hint))
    widths = [max(18, min(40, len(h) + 8)) for h in headers]
    return SheetSpec(d.name, [headers], widths, "table", vals, TAB[d.key])


def _kv(d: L.SheetDef) -> SheetSpec:
    rows = [["Field", "Value", "Guidance"]] + [[c.name, "", c.hint] for c in d.cols]
    vals = [Validation(f"B{i}", (), c.name, c.hint) for i, c in enumerate(d.cols, 2)]
    return SheetSpec(d.name, rows, [28, 48, 90], "table", vals, TAB[d.key], label_column=True)


def build_template() -> list[SheetSpec]:
    return [_readme()] + [(_kv(d) if d.layout == "kv" else _table(d)) for d in L.SHEETS]


def write_template(path: str | Path, *, force: bool = False) -> Path:
    """Write the blank template. An existing file is never overwritten unless `force` (it may be your filled workbook)."""
    p = Path(path)
    if p.is_dir():
        p = p / TEMPLATE_NAME
    if p.exists() and not force:
        raise FileExistsError(f"{p} already exists: it was not overwritten (it might be your filled-in workbook). Choose another name, or use --force.")
    p.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(p, build_template())
    return p
