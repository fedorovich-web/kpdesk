"""Evidence-bound offline canonical export of *fully human-reviewed* vendor rows.

No DB publishing, no fabricated SKU, prices, pack sizes or product category.
Only exports a strict KPDesk CSV that passes the existing canonical validator.
The session-verified reviewer identity must be supplied by a future trusted
backend service, never by user-controlled JSON.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext
from typing import Literal

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, CatalogImportResult, ImportLimits
from kpdesk.catalog.supplier_profiles import PriceBasis
from kpdesk.catalog.supplier_staging import (
    StagedSupplierRow,
    SupplierStaging,
    _source_high_precision_amount,
)
from kpdesk.db.tenant_access import AuthenticatedUser
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES, TAXONOMY_VERSION

type CanonicalUnit = Literal["штука", "пачка", "коробка"]


class CanonicalExportRejected(ValueError):
    """Row-specific reason why a supplier file cannot safely be published."""

    def __init__(self, code: str, line: int | None = None) -> None:
        self.code = code
        self.line = line
        super().__init__(f"{code}: source row {line}" if line is not None else code)


@dataclass(frozen=True, slots=True)
class ReviewedPrecisionPrice:
    """Human-selected cents conversion; evidence is not supplier authorization."""

    price_rub: Decimal
    rounding_mode: Literal["half_up", "half_even", "down", "up"]
    explanation: str


@dataclass(frozen=True, slots=True)
class ReviewedProductDecision:
    """Reviewer explicitly fills missing/ambiguous canonical attributes per line."""

    line: int
    category: str
    sale_unit: CanonicalUnit
    pieces_per_sale_unit: int
    source_row_sha256: str
    confirmed: bool
    price_unit_review_evidence: str | None = None
    precision_review: ReviewedPrecisionPrice | None = None
    brand: str | None = None
    paper_format: str | None = None
    paper_gsm: int | None = None
    pen_ink_color: str | None = None
    pen_ink_type: str | None = None
    attributes: dict[str, str | int | list[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewedExclusion:
    """An explicit human exclusion, never an invisible dropped source row."""

    line: int
    source_row_sha256: str
    confirmed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CanonicalPreview:
    """Not a successful catalog publication, even if conversion validates."""

    source_sha256: str
    csv_sha256: str
    organization_id: uuid.UUID
    reviewer_id: uuid.UUID
    taxonomy_version: str
    reviewed_price_basis: PriceBasis
    products_count: int
    excluded_source_rows: tuple[int, ...]
    csv_content: bytes
    validation: CatalogImportResult
    published: bool = False
    model_training_authorized: bool = False


def source_row_sha256(row: StagedSupplierRow) -> str:
    """Bind an approval to immutable original-source staged values."""
    representation: dict[str, object] = {
        "line": row.line,
        "supplier_sku": row.supplier_sku,
        "name": row.name,
        "price_rub": str(row.price_rub) if row.price_rub is not None else None,
        "source_category": row.source_category,
        "source_sale_unit": row.source_sale_unit,
        "source_pack_size": row.source_pack_size,
        "issues": row.issues,
    }
    if row.formula_columns:
        representation["formula_columns"] = row.formula_columns
    if row.source_high_precision_price is not None:
        representation["source_high_precision_price"] = row.source_high_precision_price
    return hashlib.sha256(
        json.dumps(representation, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_SOURCE_UNIT_FAMILY: dict[str, CanonicalUnit] = {
    "шт": "штука",
    "шт.": "штука",
    "штука": "штука",
    "pcs": "штука",
    "pc": "штука",
    "piece": "штука",
    "уп": "пачка",
    "уп.": "пачка",
    "упаковка": "пачка",
    "пачка": "пачка",
    "кор": "коробка",
    "кор.": "коробка",
    "коробка": "коробка",
    "box": "коробка",
}


def _price_unit_basis_needs_review(
    row: StagedSupplierRow, decision: ReviewedProductDecision,
) -> bool:
    # A mapped but missing/invalid package cannot prove the price denominator,
    # even when the supplier describes the sale unit as one piece.
    if "missing_pack_size" in row.issues or "invalid_pack_size" in row.issues:
        return True
    # Missing original unit is never proof that a price is per piece,
    # including when the vendor's pack column equals one.
    if row.source_sale_unit is None:
        return True
    source_family = _SOURCE_UNIT_FAMILY.get(row.source_sale_unit.strip().casefold())
    if source_family is None or source_family != decision.sale_unit:
        return True
    if row.source_pack_size is None:
        # A literal "шт" identifies one piece; a "пачка"/"коробка"
        # without an original quantity cannot establish the denominator.
        return source_family != "штука" or decision.pieces_per_sale_unit != 1
    # Even if the source says "шт", a separate "12 in pack" field can
    # change commercial price meaning. Require an explicit attestation.
    return row.source_pack_size != decision.pieces_per_sale_unit


_ROUNDING_RULES = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "down": ROUND_DOWN,
    "up": ROUND_UP,
}


def checked_precision_rounding(
    row: StagedSupplierRow, review: ReviewedPrecisionPrice,
) -> Decimal:
    """Validate explicit cents conversion on the unchanged source row.

    This is an arithmetic consistency check, not authentication or catalog
    authorization. Reused by canonical preview and offline review batches.
    """
    raw = row.source_high_precision_price
    if raw is None:
        raise CanonicalExportRejected("unexpected_price_precision_review", row.line)
    if "unconfirmed_or_non_rub_currency" in row.issues:
        raise CanonicalExportRejected("price_precision_currency_unverified", row.line)
    if (
        row.formula_columns
        or "xls_formula_and_macro_provenance_unverified" in row.issues
    ):
        raise CanonicalExportRejected("price_precision_formula_provenance_unverified", row.line)
    if not isinstance(review, ReviewedPrecisionPrice):
        raise CanonicalExportRejected("invalid_price_precision_review", row.line)
    source = _source_high_precision_amount(raw)
    if (
        source is None
        or not isinstance(review.rounding_mode, str)
        or review.rounding_mode not in _ROUNDING_RULES
        or not isinstance(review.price_rub, Decimal)
        or not review.price_rub.is_finite()
        or not Decimal(0) < review.price_rub < Decimal(1000000000000)
        or not isinstance(review.explanation, str)
        or not 32 <= len(review.explanation.strip()) <= 500
        or any(ord(character) < 32 for character in review.explanation)
    ):
        raise CanonicalExportRejected("invalid_price_precision_review", row.line)
    with localcontext() as context:
        context.prec = max(40, len(source.as_tuple().digits) + 4)
        rounded = source.quantize(
            Decimal("0.01"), rounding=_ROUNDING_RULES[review.rounding_mode],
        )
    if review.price_rub != rounded or str(review.price_rub) != str(rounded):
        raise CanonicalExportRejected("price_precision_rounding_mismatch", row.line)
    return rounded


def _reviewed_price_amount(
    row: StagedSupplierRow, decision: ReviewedProductDecision,
) -> Decimal | None:
    review = decision.precision_review
    if row.source_high_precision_price is None:
        if review is not None:
            raise CanonicalExportRejected("unexpected_price_precision_review", row.line)
        return row.price_rub
    if review is None:
        raise CanonicalExportRejected("price_precision_review_required", row.line)
    return checked_precision_rounding(row, review)


def _safe_excel_text(value: str | None) -> str:
    if value is None:
        return ""
    if (
        not isinstance(value, str)
        or len(value) > 500
        or any(ord(character) < 32 and character != "\n" for character in value)
        or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        raise CanonicalExportRejected("unsafe_spreadsheet_cell")
    return value


def preview_canonical_catalog(
    staging: SupplierStaging,
    decisions: tuple[ReviewedProductDecision | ReviewedExclusion, ...],
    *,
    organization_id: uuid.UUID,
    reviewer: AuthenticatedUser,
    expected_source_sha256: str,
    price_basis: PriceBasis,
    explicitly_confirmed_price_basis: bool,
    limits: ImportLimits | None = None,
) -> CanonicalPreview:
    """Return a complete validated CSV in memory; never perform publish/write.

    All source rows must have one explicit authenticated-human decision. No row
    is silently dropped, no surrogate SKU is generated, no unit inferred and
    every canonical product is re-validated by the real catalog parser.
    """
    limits = limits or ImportLimits()
    if (
        not isinstance(organization_id, uuid.UUID)
        or not isinstance(reviewer, AuthenticatedUser)
        or not isinstance(reviewer.user_id, uuid.UUID)
        or len(expected_source_sha256) != 64
        or expected_source_sha256 != staging.sha256
        or not explicitly_confirmed_price_basis
        or price_basis not in ("standard", "retail", "wholesale", "rrp", "vat_included")
        or not staging.rows
        or len(staging.rows) > limits.max_rows
        or len(decisions) != len(staging.rows)
    ):
        raise CanonicalExportRejected("approval_or_source_mismatch")
    decision_by_line: dict[int, ReviewedProductDecision | ReviewedExclusion] = {}
    for decision in decisions:
        if decision.line in decision_by_line:
            raise CanonicalExportRejected("duplicate_review_decision", decision.line)
        decision_by_line[decision.line] = decision
    if {row.line for row in staging.rows} != set(decision_by_line):
        raise CanonicalExportRejected("missing_or_unknown_review_decision")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS_V2)
    seen_sku: set[str] = set()
    excluded_lines: list[int] = []
    for row in staging.rows:
        decision = decision_by_line[row.line]
        if (
            type(decision.confirmed) is not bool
            or not decision.confirmed
            or decision.source_row_sha256 != source_row_sha256(row)
        ):
            raise CanonicalExportRejected("unconfirmed_or_changed_source_row", row.line)
        if isinstance(decision, ReviewedExclusion):
            if (
                not isinstance(decision.reason, str)
                or not 8 <= len(decision.reason.strip()) <= 500
            ):
                raise CanonicalExportRejected("exclusion_reason_required", row.line)
            excluded_lines.append(row.line)
            continue
        if "xls_formula_and_macro_provenance_unverified" in row.issues:
            raise CanonicalExportRejected("unverified_legacy_xls_provenance", row.line)
        if row.formula_columns or "xlsx_formula_cell_quarantined" in row.issues:
            raise CanonicalExportRejected("unverified_xlsx_formula_provenance", row.line)
        price_rub = _reviewed_price_amount(row, decision)
        if not row.supplier_sku or not row.name or price_rub is None:
            raise CanonicalExportRejected("missing_original_identity_or_rub_price", row.line)
        if row.supplier_sku in seen_sku:
            raise CanonicalExportRejected("duplicate_original_supplier_sku", row.line)
        seen_sku.add(row.supplier_sku)
        if decision.category not in CATEGORY_ATTRIBUTES:
            raise CanonicalExportRejected("unsupported_or_unmapped_category", row.line)
        if (
            decision.sale_unit not in ("штука", "пачка", "коробка")
            or type(decision.pieces_per_sale_unit) is not int
            or not 1 <= decision.pieces_per_sale_unit <= 100000
            or (decision.sale_unit == "штука" and decision.pieces_per_sale_unit != 1)
        ):
            raise CanonicalExportRejected("sale_unit_or_pack_review_required", row.line)
        # A changed denominator can turn a vendor pack price into an incorrectly
        # cheaper per-piece quote. A row's generic approval does not attest to
        # that separate commercial fact. Never divide vendor prices here.
        if _price_unit_basis_needs_review(row, decision):
            evidence = decision.price_unit_review_evidence
            if (
                not isinstance(evidence, str)
                or not 32 <= len(evidence.strip()) <= 500
                or any(ord(character) < 32 for character in evidence)
            ):
                raise CanonicalExportRejected("price_unit_basis_requires_review", row.line)
        try:
            attributes = json.dumps(
                decision.attributes, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            fields = {
                "sku": _safe_excel_text(row.supplier_sku),
                "name": _safe_excel_text(row.name),
                "category": decision.category,
                "sale_unit": decision.sale_unit,
                "pieces_per_sale_unit": decision.pieces_per_sale_unit,
                "price_rub": str(price_rub),
                "brand": _safe_excel_text(decision.brand),
                "paper_format": _safe_excel_text(decision.paper_format),
                "paper_gsm": decision.paper_gsm or "",
                "pen_ink_color": _safe_excel_text(decision.pen_ink_color),
                "pen_ink_type": _safe_excel_text(decision.pen_ink_type),
                "attributes_json": attributes,
            }
            writer.writerow([fields[column] for column in COLUMNS_V2])
        except (ValueError, TypeError, OverflowError) as error:
            raise CanonicalExportRejected("invalid_reviewed_product", row.line) from error
    if not seen_sku:
        raise CanonicalExportRejected("empty_reviewed_catalog")
    encoded = stream.getvalue().encode("utf-8")
    result = parse_price_list(
        "reviewed-supplier.csv", encoded,
        ImportLimits(max_upload_bytes=limits.max_upload_bytes),
    )
    if not result.publishable or len(result.products) != len(staging.rows) - len(excluded_lines):
        first = result.errors[0] if result.errors else None
        raise CanonicalExportRejected(
            first.code if first else "canonical_validation_failed",
            (first.row - 1) if first and first.row else None,
        )
    return CanonicalPreview(
        source_sha256=staging.sha256,
        csv_sha256=hashlib.sha256(encoded).hexdigest(),
        organization_id=organization_id, reviewer_id=reviewer.user_id,
        taxonomy_version=TAXONOMY_VERSION,
        reviewed_price_basis=price_basis,
        products_count=len(staging.rows) - len(excluded_lines),
        excluded_source_rows=tuple(excluded_lines),
        csv_content=encoded, validation=result,
    )
