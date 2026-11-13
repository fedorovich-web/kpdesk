"""Fail-closed ZIP/XLSX envelope checks for untrusted supplier originals."""
from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

MAX_FILE_BYTES = 8_000_000
MAX_XLSX_ENTRIES = 8_000
MAX_EXPANDED_BYTES = 160_000_000


def validate_xlsx_archive(path: Path) -> None:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Supplier XLSX exceeds byte limit.")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = {item.filename for item in entries}
        if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
            raise ValueError("Supplier original is not an XLSX workbook.")
        if len(entries) > MAX_XLSX_ENTRIES or sum(
            item.file_size for item in entries
        ) > MAX_EXPANDED_BYTES:
            raise ValueError("Supplier XLSX exceeds expanded archive limits.")
        if any(
            item.flag_bits & 1
            or item.filename.startswith(("/", chr(92)))
            or chr(92) in item.filename
            or ".." in item.filename.split("/")
            for item in entries
        ):
            raise ValueError("Supplier XLSX contains unsafe ZIP members.")
