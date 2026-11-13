"""Reviewed vendor profile replay must be exact-schema and tenant-scoped."""
from __future__ import annotations

import uuid
from typing import Literal, cast

import pytest

from kpdesk.catalog.supplier_inference import SourceInspection, inspect_supplier_source
from kpdesk.catalog.supplier_profiles import (
    PriceBasis,
    ReviewedSupplierProfile,
    SupplierSchemaDrift,
    approve_supplier_profile,
    reuse_supplier_profile,
    schema_fingerprint,
    tabular_mapping_from_profile,
)
from kpdesk.db.tenant_access import AuthenticatedUser

ORG = uuid.UUID("10000000-0000-4000-8000-000000000001")
FOREIGN_ORG = uuid.UUID("10000000-0000-4000-8000-000000000002")
REVIEWER = AuthenticatedUser(uuid.UUID("10000000-0000-4000-8000-000000000003"))


def _approved(
    filename: str, content: bytes, *,
    fields: dict[str, int | str] | None = None,
    price_basis: PriceBasis = "standard",
    currency: str | None = None,
) -> tuple[SourceInspection, ReviewedSupplierProfile]:
    inspection = inspect_supplier_source(filename, content)
    columns = fields or inspection.proposed_fields
    profile = approve_supplier_profile(
        inspection,
        organization_id=ORG, supplier_id="real_vendor_01",
        reviewer=REVIEWER, source_sha256=inspection.source_sha256,
        selected_columns=columns,
        price_basis=price_basis,
        confirmed_currency=cast(Literal["RUB"] | None, currency),
        review_evidence="Supplier source checked by responsible human",
        explicitly_confirmed=True,
    )
    return inspection, profile


def test_confirm_once_then_reuse_changed_prices_with_same_columns() -> None:
    old = ("Артикул;Наименование;Цена;Валюта;Ед. изм.\n"
           "00012;Бумага А4;450,00;RUB;пачка\n").encode()
    current = ("Артикул;Наименование;Цена;Валюта;Ед. изм.\n"
               "00012;Бумага А4;469,00;RUB;пачка\n").encode()
    before, approved = _approved("vendor.csv", old)
    after = inspect_supplier_source("vendor.csv", current)
    assert before.source_sha256 != after.source_sha256
    assert schema_fingerprint(before) == schema_fingerprint(after)
    replay = reuse_supplier_profile(
        approved, after, "vendor.csv", current,
        organization_id=ORG, supplier_id="real_vendor_01",
    )
    assert replay.staging.rows[0].supplier_sku == "00012"
    assert str(replay.staging.rows[0].price_rub) == "469.00"
    assert replay.staging.rows[0].source_sale_unit == "пачка"
    assert replay.requires_confirmation is True
    assert replay.canonical_importable is False
    assert replay.model_training_authorized is False


def test_replay_mapping_digest_is_reconstructed_from_approved_profile() -> None:
    from kpdesk.catalog.supplier_staging import mapping_digest

    raw = b"sku;name;price;currency\n0001;Pencil HB;15;RUB\n"
    _, profile = _approved("supplier.csv", raw)
    mapping = tabular_mapping_from_profile(profile)
    assert mapping.sku == 0
    assert mapping.name == 1
    assert mapping.price == 2
    assert mapping.currency == 3
    assert mapping_digest(mapping, "reject") != mapping_digest(mapping, "quarantine_rows")
    assert mapping_digest(mapping, "reject") == mapping_digest(
        tabular_mapping_from_profile(profile), "reject",
    )
    with pytest.raises(ValueError, match="tabular"):
        _, json_profile = _approved(
            "supplier.json", b'[{"name":"Pencil","price":"15 RUB"}]',
            fields={"name": "name", "price": "price"},
        )
        tabular_mapping_from_profile(json_profile)


def test_header_price_currency_change_invalidates_profile() -> None:
    old = "Артикул;Название;Цена;Валюта\n005;Мышь;999;RUB\n".encode()
    _, approved = _approved("price.csv", old)
    new = "Артикул;Название;Цена опт;Валюта\n005;Мышь;799;RUB\n".encode()
    new_inspection = inspect_supplier_source("price.csv", new)
    with pytest.raises(SupplierSchemaDrift, match="schema changed"):
        reuse_supplier_profile(
            approved, new_inspection, "price.csv", new,
            organization_id=ORG, supplier_id="real_vendor_01",
        )


def test_cross_organization_cross_supplier_and_tampered_bytes_rejected() -> None:
    original = "Артикул;Название;Цена\n01;Мышь;500 RUB\n".encode()
    inspected, approved = _approved("shop.csv", original)
    for org, supplier in (
        (FOREIGN_ORG, "real_vendor_01"), (ORG, "other_supplier"),
    ):
        with pytest.raises(ValueError, match="Cross-tenant"):
            reuse_supplier_profile(
                approved, inspected, "shop.csv", original,
                organization_id=org, supplier_id=supplier,
            )
    with pytest.raises(SupplierSchemaDrift, match="content changed"):
        reuse_supplier_profile(
            approved, inspected, "shop.csv", original.replace(b"500", b"999"),
            organization_id=ORG, supplier_id="real_vendor_01",
        )


def test_reviewer_must_confirm_and_cannot_inject_sku_or_unknown_column() -> None:
    raw = "Название;Цена\nТовар;1599 RUB\n".encode()
    source = inspect_supplier_source("a.csv", raw)
    valid_kwargs = {
        "organization_id": ORG,
        "supplier_id": "real_vendor_01",
        "reviewer": REVIEWER,
        "source_sha256": source.source_sha256,
        "selected_columns": {"name": 0, "price": 1},
        "price_basis": "standard",
        "confirmed_currency": None,
        "review_evidence": "Reviewed actual original columns",
        "explicitly_confirmed": True,
    }
    def call(**overrides: object) -> ReviewedSupplierProfile:
        values = {**valid_kwargs, **overrides}
        return approve_supplier_profile(
            source, organization_id=cast(uuid.UUID, values["organization_id"]),
            supplier_id=cast(str, values["supplier_id"]),
            reviewer=cast(AuthenticatedUser, values["reviewer"]),
            source_sha256=cast(str, values["source_sha256"]),
            selected_columns=cast(dict[str, int | str], values["selected_columns"]),
            price_basis=cast(PriceBasis, values["price_basis"]),
            confirmed_currency=cast(Literal["RUB"] | None, values["confirmed_currency"]),
            review_evidence=cast(str, values["review_evidence"]),
            explicitly_confirmed=cast(bool, values["explicitly_confirmed"]),
        )
    with pytest.raises(ValueError, match="Human review"):
        call(explicitly_confirmed=False)
    with pytest.raises(ValueError, match="Human review"):
        call(reviewer="pretend_verified")
    with pytest.raises(ValueError, match="missing original"):
        call(selected_columns={"name": 0, "price": 1, "sku": 55})


def test_price_role_must_match_wholesale_vs_retail_source() -> None:
    original = "Код;Название;Цена опт;Цена розница\n77;Товар;800;1500\n".encode()
    selection: dict[str, int | str] = {"sku": 0, "name": 1, "price": 2}
    with pytest.raises(ValueError, match="Price role"):
        _approved("ambiguous.csv", original, fields=selection,
                  price_basis="retail", currency="RUB")
    inspection, profile = _approved(
        "ambiguous.csv", original, fields=selection,
        price_basis="wholesale", currency="RUB",
    )
    assert "ambiguous_price_columns" in inspection.issues
    assert profile.price_basis == "wholesale"
    new = "Код;Название;Цена опт;Цена розница\n77;Товар;815;1579\n".encode()
    replay = reuse_supplier_profile(
        profile, inspect_supplier_source("ambiguous.csv", new),
        "ambiguous.csv", new,
        organization_id=ORG, supplier_id="real_vendor_01",
    )
    assert str(replay.staging.rows[0].price_rub) == "815"


def test_json_profile_maps_existing_keys_across_versions() -> None:
    old = b'[{"id":"0001","title":"Cable","cost":"120 RUB"}]'
    new = b'[{"id":"0001","title":"Cable","cost":"130 RUB"}]'
    _, profile = _approved(
        "vendor.json", old,
        fields={"sku": "id", "name": "title", "price": "cost"},
    )
    replay = reuse_supplier_profile(
        profile, inspect_supplier_source("vendor.json", new),
        "vendor.json", new,
        organization_id=ORG, supplier_id="real_vendor_01",
    )
    assert replay.staging.rows[0].supplier_sku == "0001"
    assert str(replay.staging.rows[0].price_rub) == "130"


def test_tampered_profile_price_basis_or_currency_rejected() -> None:
    from dataclasses import replace

    raw = b"sku;name;price\n01;Cable;500 RUB\n"
    inspection, approved = _approved("supplier.csv", raw)
    forged = replace(approved, confirmed_currency="RUB")
    with pytest.raises(ValueError, match="checksum"):
        reuse_supplier_profile(
            forged, inspection, "supplier.csv", raw,
            organization_id=ORG, supplier_id="real_vendor_01",
        )


def test_profile_rejects_unresolved_excel_sheet() -> None:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["Название", "Цена"])  # type: ignore[union-attr]
    workbook.active.append(["Бумага", 599])  # type: ignore[union-attr]
    workbook.create_sheet("other")
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    inspected = inspect_supplier_source("vendor.xlsx", output.getvalue())
    assert "sheet_selection_required" in inspected.issues
    with pytest.raises(ValueError):
        approve_supplier_profile(
            inspected, organization_id=ORG, supplier_id="vendor",
            reviewer=REVIEWER, source_sha256=inspected.source_sha256,
            selected_columns={"name": 0, "price": 1},
            price_basis="standard", confirmed_currency="RUB",
            review_evidence="Reviewed actual column mapping",
            explicitly_confirmed=True,
        )


def test_persisted_mapping_profile_checksum_blocks_tampered_evidence() -> None:
    from copy import deepcopy

    from kpdesk.catalog.supplier_mapping_storage import (
        parse_profile_payload,
        profile_payload,
    )

    inspection, profile = _approved(
        "profile.csv",
        b"sku;name;price;currency\nA;Widget;10.125;RUB\n",
    )
    assert inspection.source_sha256 == profile.approved_source_sha256
    serialized = profile_payload(profile)
    assert parse_profile_payload(serialized) == profile
    for key, changed in (
        ("review_evidence", "Falsified operator review"),
        ("price_basis", "wholesale"),
        ("approved_source_sha256", "b" * 64),
    ):
        altered = deepcopy(serialized)
        altered[key] = changed
        with pytest.raises(ValueError, match="checksum"):
            parse_profile_payload(altered)
    malformed = deepcopy(serialized)
    cast(list[list[object]], malformed["columns"])[0][1] = 45
    with pytest.raises(ValueError, match="checksum"):
        parse_profile_payload(malformed)


def test_formula_quarantined_excel_cannot_be_approved_for_automatic_mapping_reuse() -> None:
    import io

    from openpyxl import Workbook

    book = Workbook()
    active = book.active
    assert active is not None
    active.append(["sku", "name", "price", "currency", "formula"])
    active.append(["A1", "Item", 10.125, "RUB", "=1+1"])
    buffer = io.BytesIO()
    book.save(buffer)
    inspected = inspect_supplier_source(
        "formula.xlsx", buffer.getvalue(), formula_policy="quarantine_rows",
    )
    assert "xlsx_formula_cells_quarantined" in inspected.issues
    with pytest.raises(ValueError, match="provenance|structure"):
        approve_supplier_profile(
            inspected, organization_id=ORG,
            supplier_id="formula_vendor", reviewer=REVIEWER,
            source_sha256=inspected.source_sha256,
            selected_columns={"sku": 0, "name": 1, "price": 2, "currency": 3},
            price_basis="standard",
            confirmed_currency=None,
            review_evidence="Formula provenance cannot justify automatic reuse",
            explicitly_confirmed=True,
        )
