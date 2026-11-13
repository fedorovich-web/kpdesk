"""PostgreSQL-only catalog persistence tests (never touch the production database)."""

import os
from collections.abc import Iterator

import pytest
from postgres_clone_connection import LEGACY_TEST_OPT_IN, legacy_test_database
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.storage import (
    CatalogImportConflict,
    InvalidCatalog,
    get_active_catalog,
    list_catalog_items,
    publish_catalog,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(LEGACY_TEST_OPT_IN) != "1",
    reason="Explicit opt-in required for per-test marked disposable PostgreSQL clones",
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    with legacy_test_database() as engine:
        yield engine


def sample_catalog(sku: str, price: str = "349.90") -> bytes:
    return (
        "sku;name;category;sale_unit;pieces_per_sale_unit;price_rub;"
        "brand;paper_format;paper_gsm;pen_ink_color;pen_ink_type\n"
        f"{sku};Бумага А4 80;paper;пачка;1;{price};Paper;A4;80;;\n"
    ).encode()


def test_atomic_import_keeps_previous_catalog_when_validation_fails(engine: Engine) -> None:
    valid = parse_price_list("paper.csv", sample_catalog("000001"), ImportLimits())
    bad = parse_price_list("paper.csv", sample_catalog("000002", "-1.00"), ImportLimits())
    with Session(engine) as session:
        first = publish_catalog(session, valid, "post-1", valid.source_sha256)
        active = get_active_catalog(session)
        assert active is not None
        assert active.id == first.id
    with Session(engine) as session, pytest.raises(InvalidCatalog):
        publish_catalog(session, bad, "post-2", bad.source_sha256)
    with Session(engine) as session:
        active = get_active_catalog(session)
        assert active is not None
        assert active.id == first.id
        assert [p.sku for p in list_catalog_items(session, first.id)] == ["000001"]


def test_import_idempotency_rejects_key_collision_with_different_body(engine: Engine) -> None:
    original = parse_price_list("paper.csv", sample_catalog("000001"), ImportLimits())
    modified = parse_price_list("paper.csv", sample_catalog("000002"), ImportLimits())
    with Session(engine) as session:
        first = publish_catalog(session, original, "same-key", original.source_sha256)
    with Session(engine) as session:
        repeat = publish_catalog(session, original, "same-key", original.source_sha256)
        assert repeat.id == first.id
    with Session(engine) as session, pytest.raises(CatalogImportConflict):
        publish_catalog(session, modified, "same-key", modified.source_sha256)
    with Session(engine) as session:
        active = get_active_catalog(session)
        assert active is not None
        assert active.id == first.id
        assert session.scalar(text("SELECT count(*) FROM catalog_versions")) == 1


def test_new_category_attributes_roundtrip_postgresql(engine: Engine) -> None:
    """Migration v2 JSONB persists distinct typed profiles without SKU loss."""
    import csv
    import io
    import json

    from kpdesk.catalog.schema import COLUMNS_V2

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerows([
        list(COLUMNS_V2),
        ["M-001", "Мышь wireless 1600 DPI", "mouse", "штука", 1,
         "496.51", "SONNEN", "", "", "", "",
         json.dumps({"connection": "wireless", "dpi": 1600})],
        ["K-001", "Клавиатура USB 104", "keyboard", "штука", 1,
         "725.94", "SONNEN", "", "", "", "",
         json.dumps({"connection": "wired", "keys": 104})],
    ])
    parsed = parse_price_list("new.csv", stream.getvalue().encode(), ImportLimits())
    assert parsed.publishable, parsed.errors

    with Session(engine) as session:
        version = publish_catalog(session, parsed, "attr-v2-1", parsed.source_sha256)
    with Session(engine) as session:
        result = list_catalog_items(session, version.id)
    assert [p.category for p in result] == ["keyboard", "mouse"]
    assert {p.sku: p.attributes for p in result} == {
        "K-001": {"connection": "wired", "keys": 104},
        "M-001": {"connection": "wireless", "dpi": 1600},
    }


def test_v3_catalog_jsonb_and_long_category_roundtrip(engine: Engine) -> None:
    """Registry v3 supports 16+ char categories and lists of device model IDs."""
    import csv
    import io
    import json

    from kpdesk.catalog.schema import COLUMNS_V2

    stream = io.StringIO(newline="")
    rows = [
        ["P-101", "Карандаш графитовый HB", "pencil", "штука", 1, "19.90",
         "Test", "", "", "", "", json.dumps({"pencil_type": "graphite", "lead_grade": "HB"})],
        ["D-101", "Папка А4 для документов", "document_folder", "штука", 1, "125.50",
         "Test", "", "", "", "", json.dumps({"folder_kind": "zip", "format": "A4"})],
        ["T-101", "Картридж черный оригинальный", "toner_cartridge", "штука", 1, "990.00",
         "Test", "", "", "", "", json.dumps({
             "cartridge_code": "CF283A", "color": "black", "oem_status": "original",
             "compatible_models": ["HP LaserJet Pro M125", "HP LaserJet Pro M127"],
         })],
    ]
    csv.writer(stream, delimiter=";", lineterminator="\n").writerows([list(COLUMNS_V2), *rows])
    parsed = parse_price_list("assortment.csv", stream.getvalue().encode(), ImportLimits())
    assert parsed.publishable, parsed.errors
    with Session(engine) as session:
        version = publish_catalog(session, parsed, "catalog-v3-example", parsed.source_sha256)
    with Session(engine) as session:
        indexed = {item.sku: item for item in list_catalog_items(session, version.id)}
    assert len(indexed) == 3
    assert indexed["D-101"].category == "document_folder"
    assert indexed["T-101"].category == "toner_cartridge"
    assert indexed["T-101"].attributes["compatible_models"] == [
        "HP LaserJet Pro M125", "HP LaserJet Pro M127",
    ]
