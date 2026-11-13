"""Strict import limits and row-indexed errors."""

from dataclasses import dataclass

from kpdesk.matching.schema import CatalogProduct

COLUMNS: tuple[str, ...] = (
    "sku",
    "name",
    "category",
    "sale_unit",
    "pieces_per_sale_unit",
    "price_rub",
    "brand",
    "paper_format",
    "paper_gsm",
    "pen_ink_color",
    "pen_ink_type",
)


# Backward compatible: v1 remains 11 columns; v2 adds explicit typed attributes.
COLUMNS_V2: tuple[str, ...] = (*COLUMNS, "attributes_json")


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_upload_bytes: int = 10 * 1024 * 1024
    max_uncompressed_bytes: int = 40 * 1024 * 1024
    max_xml_member_bytes: int = 8 * 1024 * 1024
    max_archive_entries: int = 256
    max_compression_ratio: int = 100
    max_rows: int = 5000
    max_cell_chars: int = 2000


@dataclass(frozen=True, slots=True)
class ImportErrorDetail:
    code: str
    message: str
    row: int | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogImportResult:
    original_filename: str
    source_sha256: str
    products: tuple[CatalogProduct, ...]
    errors: tuple[ImportErrorDetail, ...]

    @property
    def publishable(self) -> bool:
        return bool(self.products) and not self.errors
