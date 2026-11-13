"""Bounded, exact text evidence from local TXT/CSV/XLSX/DOCX/PDF source files.

Exact location does not authenticate authorship, rights or whether a captured
quote is a real purchase request. Binary and scanned documents fail closed.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from openpyxl import load_workbook
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_RAW_BYTES = 16_000_000
MAX_UNCOMPRESSED_BYTES = 50_000_000
MAX_ZIP_ENTRIES = 3000
MAX_PAGES = 40
MAX_CELLS_PER_SHEET = 200_000
MAX_EXTRACTED_CHARACTERS = 500_000

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Audited column contracts, not inference from a user-supplied amount/location.
AUDITED_XLSX_QUANTITY_LAYOUTS: dict[str, tuple[str, str, str | None]] = {
    "bidzaar_339_280_stationery_2026.xlsx": ("C", "E", "D"),
    "bidzaar_337279_design_2026.xlsx": ("C", "D", None),
}
# MVM summary amounts are formula-derived, so no eligible raw quantity evidence.


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def normalize_source_quote(value: str) -> str:
    """Space/Unicode canonicalization only: retain exact characters and digits."""
    return _norm(value)


def _check_archive(data: bytes, *, document: str) -> None:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            paths = [entry.filename for entry in entries]
            if (
                len(entries) > MAX_ZIP_ENTRIES
                or len(set(paths)) != len(paths)
                or sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES
                or any(
                    entry.filename.startswith(("/", "\\"))
                    or "\\" in entry.filename
                    or ".." in entry.filename.split("/")
                    or entry.flag_bits & 1
                    or entry.file_size > MAX_UNCOMPRESSED_BYTES
                    for entry in entries
                )
            ):
                raise ValueError("Unsafe ZIP archive or excessive decompressed size.")
            names = set(paths)
            required = (
                {"word/document.xml", "[Content_Types].xml"}
                if document == "docx" else
                {"xl/workbook.xml", "[Content_Types].xml"}
            )
            if not required <= names or any(
                name.endswith(("/vbaProject.bin", "vbaProject.bin"))
                for name in names
            ):
                raise ValueError("Unsupported ZIP document structure or macro.")
    except BadZipFile as exc:
        raise ValueError("Invalid ZIP archive.") from exc


def extract_source_spans(path: Path, data: bytes) -> list[tuple[str, str]]:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".csv"):
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = data.decode("cp1251")
        if "\x00" in decoded:
            raise ValueError("Binary content in source text.")
        return [("text:1", decoded)]

    if suffix == ".xlsx":
        _check_archive(data, document="xlsx")
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            if len(workbook.worksheets) > 24:
                raise ValueError("Too many worksheet pages.")
            spans: list[tuple[str, str]] = []
            characters = 0
            for sheet in workbook.worksheets:
                if sheet.max_row * sheet.max_column > MAX_CELLS_PER_SHEET:
                    raise ValueError("Source XLSX too large for evidence inspection.")
                for row in sheet.iter_rows():
                    for cell in row:
                        if isinstance(cell.value, str) and cell.value.strip():
                            characters += len(cell.value)
                            if characters > MAX_EXTRACTED_CHARACTERS:
                                raise ValueError("Excessive source document text.")
                            spans.append((f"sheet:{sheet.title}!{cell.coordinate}", cell.value))
            return spans
        finally:
            workbook.close()

    if suffix == ".docx":
        _check_archive(data, document="docx")
        with ZipFile(io.BytesIO(data)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        spans = []
        total = 0
        for index, paragraph in enumerate(root.iter(f"{_WORD_NS}p"), start=1):
            text = "".join(
                child.text or "" for child in paragraph.iter(f"{_WORD_NS}t")
            )
            total += len(text)
            if total > MAX_EXTRACTED_CHARACTERS:
                raise ValueError("Excessive document text.")
            if text.strip():
                spans.append((f"paragraph:{index}", text))
        return spans

    if suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
                raise ValueError("Encrypted or excessive PDF pages.")
            spans = []
            total = 0
            for index, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                total += len(text)
                if total > MAX_EXTRACTED_CHARACTERS:
                    raise ValueError("Excessive PDF text.")
                if text.strip():
                    spans.append((f"page:{index}", text))
            return spans
        except (OSError, TypeError, KeyError, ValueError, IndexError, PdfReadError) as exc:
            raise ValueError("PDF source evidence could not be safely extracted.") from exc

    raise ValueError("Unsupported evidence source extension.")


def locate_source_excerpt(
    path: Path, excerpt: str, *, expected_sha256: str | None = None
) -> str:
    """Require exactly one verbatim excerpt occurrence in original source.

    No paraphrase/fuzzy matching, no OCR, and no claim of independently
    authenticated original document. Returns an opaque location, not text.
    """
    if path.is_symlink() or not path.is_file():
        raise ValueError("Original source file missing or unsafe.")
    size = path.stat().st_size
    if not 1 <= size <= MAX_RAW_BYTES:
        raise ValueError("Source file size outside configured limits.")
    data = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Source document SHA256 mismatch.")
    normalized = _norm(excerpt)
    if not 12 <= len(normalized) <= 2000:
        raise ValueError("Source excerpt length invalid.")
    spans = extract_source_spans(path, data)
    count = 0
    matched_location: str | None = None
    for location, source_text in spans:
        occurrences = _norm(source_text).count(normalized)
        if occurrences:
            count += occurrences
            matched_location = location
            if count > 1:
                raise ValueError("Source excerpt ambiguous: multiple occurrences.")
    if count != 1 or matched_location is None:
        raise ValueError("Source excerpt not found in source document.")
    return matched_location


def extract_xlsx_row_amount(
    path: Path, request_quote: str, *, expected_sha256: str,
    title_column: str = "C", quantity_column: str = "E", unit_column: str | None = "D",
) -> dict[str, str | int | None]:
    """Verbatim audited product row with explicit numeric quantity and optional unit.

    The columns here are **source-specific** to the audited Bidzaar spreadsheet.
    Neither the pack contents nor the sale-unit semantics are inferred.
    """
    if (title_column, quantity_column, unit_column) not in {
        ("C", "E", "D"),  # Akron Holding, office stationery
        ("C", "D", None),  # SalutDevices, creative/design materials
        ("B", "E", "D"),  # MVM, business supplies
    }:
        raise ValueError("Unsupported audited procurement worksheet column layout.")
    if path.suffix.lower() != ".xlsx" or path.is_symlink() or not path.is_file():
        raise ValueError("Quantity evidence requires a local XLSX original.")
    if not 1 <= path.stat().st_size <= MAX_RAW_BYTES:
        raise ValueError("Invalid XLSX source size.")
    binary = path.read_bytes()
    if hashlib.sha256(binary).hexdigest() != expected_sha256:
        raise ValueError("XLSX quantity source SHA256 mismatch.")
    _check_archive(binary, document="xlsx")
    workbook = load_workbook(io.BytesIO(binary), read_only=True, data_only=True)
    try:
        if not 1 <= len(workbook.worksheets) <= 24:
            raise ValueError("Unexpected procurement sheet count.")
        sheet = workbook.worksheets[0]
        if sheet.max_row > 5000 or sheet.max_column > 100:
            raise ValueError("Quantity evidence workbook is too large.")
        matched: list[int] = []
        for row in range(1, sheet.max_row + 1):
            value = sheet[f"{title_column}{row}"].value
            if isinstance(value, str) and _norm(value) == _norm(request_quote):
                matched.append(row)
        if len(matched) != 1:
            raise ValueError("Ambiguous or missing XLSX request quantity source row.")
        n = matched[0]
        unit = sheet[f"{unit_column}{n}"].value if unit_column is not None else None
        quantity = sheet[f"{quantity_column}{n}"].value
        if (
            (unit_column is not None and (
                not isinstance(unit, str) or not unit.strip() or len(unit) > 80
            ))
            or type(quantity) is not int or not 0 < quantity <= 1_000_000
        ):
            raise ValueError("Quantity or unit unavailable in source adjacent cells.")
        sheet_name = sheet.title
    finally:
        workbook.close()
    # Reject formulas instead of accepting mutable formula cache as source facts.
    formulas = load_workbook(io.BytesIO(binary), read_only=True, data_only=False)
    try:
        original = formulas.worksheets[0]
        if (original[f"{quantity_column}{n}"].data_type == "f" or (
            unit_column is not None and original[f"{unit_column}{n}"].data_type == "f"
        )):
            raise ValueError("Formula-derived quantity evidence is not accepted.")
    finally:
        formulas.close()
    return {
        "quantity": quantity,
        "unit_original": unit.strip() if isinstance(unit, str) else None,
        "unit_cell": f"sheet:{sheet_name}!{unit_column}{n}" if unit_column else None,
        "quantity_cell": f"sheet:{sheet_name}!{quantity_column}{n}",
        "verified_as": "raw_adjacent_cells",
    }
