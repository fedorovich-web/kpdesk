"""Reusable human-reviewed supplier mappings with fail-closed schema drift detection.

Pure domain logic only. A verified server session and tenant-scoped, append-only
profile storage MUST be built before accepting user approvals via HTTP. No DB,
external model, product publication, or real ML training occurs here.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Literal, cast

from kpdesk.catalog.supplier_inference import SourceColumn, SourceInspection
from kpdesk.catalog.supplier_json import JsonSupplierMapping, stage_json_supplier_prices
from kpdesk.catalog.supplier_staging import (
    SupplierMapping,
    SupplierStaging,
    stage_supplier_prices,
)
from kpdesk.db.tenant_access import AuthenticatedUser

type PriceBasis = Literal[
    "standard", "retail", "wholesale", "rrp", "vat_included",
]

_ALLOWED_FIELDS = frozenset((
    "name", "price", "sku", "currency", "category",
    "sale_unit", "pieces_per_sale_unit",
))
_FORBIDDEN_STRUCTURE = frozenset((
    "sheet_selection_required", "ambiguous_header_row", "duplicate_source_headers",
    "xls_formula_and_macro_provenance_unverified",
    "xlsx_formula_cells_quarantined",
))


class SupplierSchemaDrift(ValueError):
    """The vendor changed the source layout; the old approved map is invalid."""


@dataclass(frozen=True, slots=True)
class ReviewedSupplierProfile:
    """A human-approved MAPPING, never human approval of all product row values."""

    organization_id: uuid.UUID
    supplier_id: str
    fingerprint: str
    source_format: str
    columns: tuple[tuple[str, SourceColumn], ...]
    header_row: int | None
    sheet: str | None
    delimiter: str | None
    encoding: str | None
    items_key: str | None
    confirmed_currency: Literal["RUB"] | None
    price_basis: PriceBasis
    approved_by: uuid.UUID
    approved_source_sha256: str
    review_evidence: str
    profile_sha256: str


@dataclass(frozen=True, slots=True)
class ReusedSupplierProfile:
    """Still review-only; price/stock freshness must be checked per new version."""

    supplier_id: str
    inspected_source_sha256: str
    profile_sha256: str
    staging: SupplierStaging
    requires_confirmation: bool = True
    canonical_importable: bool = False
    model_training_authorized: bool = False


def _schema_evidence(inspection: SourceInspection) -> dict[str, object]:
    if (
        inspection.header_row is None
        or not 1 <= len(inspection.observed_columns) <= 64
        or inspection.source_format not in ("csv", "xls", "xlsx", "json")
    ):
        raise ValueError("An exact, reviewed source header is required.")
    return {
        "source_format": inspection.source_format,
        "observed_columns": inspection.observed_columns,
        "header_row": inspection.header_row,
        "sheet": inspection.selected_sheet,
        "encoding": inspection.encoding,
        "delimiter": inspection.delimiter,
        "items_key": inspection.items_key,
    }


def schema_fingerprint(inspection: SourceInspection) -> str:
    """Exact layout binding (NOT file bytes) for refreshed vendor price lists."""
    packed = json.dumps(
        _schema_evidence(inspection), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _price_basis_is_consistent(price_label: str, basis: PriceBasis) -> bool:
    label = price_label.casefold().replace("ё", "е")
    if "опт" in label:
        return basis == "wholesale"
    if "розниц" in label or "цена продажи" in label:
        return basis == "retail"
    if "ррц" in label:
        return basis == "rrp"
    if "с ндс" in label:
        return basis == "vat_included"
    if "без ндс" in label or "старая" in label:
        return False  # Cannot silently use a pre-tax/obsolete price.
    return basis == "standard"


def _check_columns(
    inspection: SourceInspection, columns: dict[str, SourceColumn],
    basis: PriceBasis,
) -> tuple[tuple[str, SourceColumn], ...]:
    if (
        not {"name", "price"} <= columns.keys()
        or not columns.keys() <= _ALLOWED_FIELDS
        or any(type(value) not in (int, str) for value in columns.values())
    ):
        raise ValueError("Required name/price fields or allowed columns missing.")
    values = list(columns.values())
    if len(values) != len(set(values)):
        raise ValueError("A supplier source column cannot map to two target fields.")
    labels = inspection.observed_columns
    is_json = inspection.source_format == "json"
    for value in values:
        if is_json:
            if not isinstance(value, str) or value not in labels:
                raise ValueError("JSON profile references a missing original key.")
        elif type(value) is not int or not 0 <= value < len(labels):
            raise ValueError("Tabular profile references a missing original column.")
    raw_price = columns["price"]
    position = labels.index(raw_price) if isinstance(raw_price, str) else raw_price
    if not _price_basis_is_consistent(labels[position], basis):
        raise ValueError("Price role conflicts with supplier column semantics.")
    return tuple(sorted(columns.items()))


def _profile_checksum(data: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def approve_supplier_profile(
    inspection: SourceInspection,
    *,
    organization_id: uuid.UUID,
    supplier_id: str,
    reviewer: AuthenticatedUser,
    source_sha256: str,
    selected_columns: dict[str, SourceColumn],
    price_basis: PriceBasis,
    confirmed_currency: Literal["RUB"] | None,
    review_evidence: str,
    explicitly_confirmed: bool,
) -> ReviewedSupplierProfile:
    """Build a reviewed profile only after an independently verified human act.

    The function does NOT authenticate a client itself. reviewer must be from a
    trusted server session (never from request JSON).
    """
    if (
        not isinstance(organization_id, uuid.UUID)
        or not isinstance(reviewer, AuthenticatedUser)
        or not isinstance(reviewer.user_id, uuid.UUID)
        or not 1 <= len(supplier_id) <= 120
        or not supplier_id.isascii()
        or not all(char.isalnum() or char in "._-" for char in supplier_id)
        or inspection.source_sha256 != source_sha256
        or not explicitly_confirmed
        or not 8 <= len(review_evidence.strip()) <= 500
        or confirmed_currency not in (None, "RUB")
        or price_basis not in ("standard", "retail", "wholesale", "rrp", "vat_included")
    ):
        raise ValueError("Human review, source evidence and tenant identity are mandatory.")
    if _FORBIDDEN_STRUCTURE.intersection(inspection.issues):
        raise ValueError("Source requires further explicit structure/provenance review.")
    fields = _check_columns(inspection, selected_columns, price_basis)
    fingerprint = schema_fingerprint(inspection)
    data: dict[str, object] = {
        "organization_id": str(organization_id),
        "supplier_id": supplier_id,
        "fingerprint": fingerprint,
        "source_format": inspection.source_format,
        "columns": fields,
        "header_row": inspection.header_row,
        "sheet": inspection.selected_sheet,
        "delimiter": inspection.delimiter,
        "encoding": inspection.encoding,
        "items_key": inspection.items_key,
        "confirmed_currency": confirmed_currency,
        "price_basis": price_basis,
        "approved_by": str(reviewer.user_id),
        "approved_source_sha256": source_sha256,
        "review_evidence": review_evidence.strip(),
    }
    return ReviewedSupplierProfile(
        organization_id, supplier_id, fingerprint, inspection.source_format,
        fields, inspection.header_row, inspection.selected_sheet, inspection.delimiter,
        inspection.encoding, inspection.items_key, confirmed_currency, price_basis,
        reviewer.user_id, source_sha256, review_evidence.strip(), _profile_checksum(data),
    )



def tabular_mapping_from_profile(profile: ReviewedSupplierProfile) -> SupplierMapping:
    """Recreate exactly the reviewed mapping contract for replay and provenance."""
    if profile.source_format not in ("csv", "xls", "xlsx"):
        raise ValueError("Only reviewed tabular mappings can be replayed.")
    indexes = {key: int(value) for key, value in profile.columns}
    if profile.encoding not in ("utf-8", "utf-8-sig", "cp1251", None):
        raise ValueError("Unrecognized original source encoding.")
    if profile.delimiter not in (";", ",", "\t", None):
        raise ValueError("Unrecognized original CSV separator.")
    return SupplierMapping(
        name=indexes["name"], price=indexes["price"],
        sku=indexes.get("sku"), currency=indexes.get("currency"),
        category=indexes.get("category"), sale_unit=indexes.get("sale_unit"),
        pieces_per_sale_unit=indexes.get("pieces_per_sale_unit"),
        header_row=profile.header_row or 1, sheet=profile.sheet,
        delimiter=cast(Literal[";", ",", "\t"], profile.delimiter or ";"),
        encoding=cast(
            Literal["utf-8", "utf-8-sig", "cp1251"],
            profile.encoding or "utf-8-sig",
        ),
        confirmed_currency=profile.confirmed_currency,
    )


def reuse_supplier_profile(
    profile: ReviewedSupplierProfile,
    inspection: SourceInspection,
    filename: str,
    content: bytes,
    *,
    organization_id: uuid.UUID,
    supplier_id: str,
) -> ReusedSupplierProfile:
    """Apply only to the same supplier and exact source schema; no publication."""
    if (
        not isinstance(organization_id, uuid.UUID)
        or organization_id != profile.organization_id
        or supplier_id != profile.supplier_id
    ):
        raise ValueError("Cross-tenant or cross-supplier profile reuse denied.")
    audit_data: dict[str, object] = {
        "organization_id": str(profile.organization_id),
        "supplier_id": profile.supplier_id,
        "fingerprint": profile.fingerprint,
        "source_format": profile.source_format,
        "columns": profile.columns,
        "header_row": profile.header_row,
        "sheet": profile.sheet,
        "delimiter": profile.delimiter,
        "encoding": profile.encoding,
        "items_key": profile.items_key,
        "confirmed_currency": profile.confirmed_currency,
        "price_basis": profile.price_basis,
        "approved_by": str(profile.approved_by),
        "approved_source_sha256": profile.approved_source_sha256,
        "review_evidence": profile.review_evidence,
    }
    if _profile_checksum(audit_data) != profile.profile_sha256:
        raise ValueError("Supplier profile audit checksum mismatch.")
    if schema_fingerprint(inspection) != profile.fingerprint:
        raise SupplierSchemaDrift("Supplier schema changed; mapping requires re-review.")
    if hashlib.sha256(content).hexdigest() != inspection.source_sha256:
        raise SupplierSchemaDrift("Supplier source content changed after inspection.")
    columns = dict(profile.columns)
    if profile.source_format == "json":
        values = {key: str(value) for key, value in columns.items()}
        json_mapping = JsonSupplierMapping(
            name=values["name"], price=values["price"],
            sku=values.get("sku"), currency=values.get("currency"),
            category=values.get("category"), sale_unit=values.get("sale_unit"),
            pieces_per_sale_unit=values.get("pieces_per_sale_unit"),
            items_key=profile.items_key, confirmed_currency=profile.confirmed_currency,
        )
        staged = stage_json_supplier_prices(filename, content, json_mapping)
    else:
        staged = stage_supplier_prices(
            filename, content, tabular_mapping_from_profile(profile),
        )
    return ReusedSupplierProfile(
        supplier_id, inspection.source_sha256, profile.profile_sha256, staged,
    )
