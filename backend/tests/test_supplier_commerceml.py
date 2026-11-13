"""Safe single-file CommerceML staging: real structure, offline generated fixtures.

These are schema regression fixtures, NOT a claim of verified 1C/CRM accounts or
source price training rights.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kpdesk.catalog.supplier_commerceml import stage_commerceml

_XML = """<?xml version="1.0" encoding="utf-8"?>
<КоммерческаяИнформация ВерсияСхемы="2.08">
  <Каталог><Товары>
    <Товар><Ид>item-1</Ид><Артикул>00001</Артикул>
      <Наименование>Бумага офисная А4</Наименование><БазоваяЕдиница>пачка</БазоваяЕдиница>
    </Товар>
    <Товар><Ид>item-2</Ид><Наименование>Клавиатура</Наименование></Товар>
  </Товары></Каталог>
  <ПакетПредложений><Предложения>
    <Предложение><Ид>item-1</Ид><Цены>
      <Цена><ИдТипаЦены>wholesale</ИдТипаЦены>
        <ЦенаЗаЕдиницу>450.00</ЦенаЗаЕдиницу><Валюта>RUB</Валюта></Цена>
      <Цена><ИдТипаЦены>retail</ИдТипаЦены>
        <ЦенаЗаЕдиницу>499.90</ЦенаЗаЕдиницу><Валюта>RUB</Валюта></Цена>
    </Цены></Предложение>
    <Предложение><Ид>item-2</Ид><Цены>
      <Цена><ИдТипаЦены>retail</ИдТипаЦены>
        <ЦенаЗаЕдиницу>1999.00</ЦенаЗаЕдиницу><Валюта>RUB</Валюта></Цена>
    </Цены></Предложение>
  </Предложения></ПакетПредложений>
</КоммерческаяИнформация>"""


def test_commerceml_explicit_retail_tier_and_missing_sku_retained() -> None:
    original = _XML.encode("utf-8")
    with pytest.raises(ValueError, match="Several CommerceML price types"):
        stage_commerceml("catalog.xml", original)
    stage = stage_commerceml("catalog.xml", original, price_type_id="retail")
    assert len(stage.rows) == 2
    assert stage.offered_price_type_ids == ("retail", "wholesale")
    assert stage.rows[0].supplier_sku == "00001"
    assert str(stage.rows[0].price_rub) == "499.90"
    assert stage.rows[0].source_sale_unit == "пачка"
    assert stage.rows[1].supplier_sku is None
    assert "missing_supplier_sku" in stage.rows[1].issues
    assert "sale_unit_requires_review" in stage.rows[1].issues
    assert not stage.canonical_importable
    assert not stage.model_training_authorized


def test_unpriced_product_kept_as_unresolved_not_zero() -> None:
    original = _XML.replace(
        "<Предложение><Ид>item-2</Ид><Цены>",
        "<Предложение><Ид>item-does-not-match</Ид><Цены>",
    )
    with pytest.raises(ValueError, match="Orphan"):
        stage_commerceml("catalog.xml", original.encode())
    without_offer = _XML.split('    <Предложение><Ид>item-2')[0]
    # Preserve valid collection close, but remove offer for the second product.
    fragment = (
        '<Предложение><Ид>item-2</Ид><Цены>'
        '<Цена><ИдТипаЦены>retail</ИдТипаЦены>'
        '<ЦенаЗаЕдиницу>1999.00</ЦенаЗаЕдиницу><Валюта>RUB</Валюта>'
        '</Цена></Цены></Предложение>'
    )
    single = _XML.replace("    " + fragment, "")
    # Structural input may contain indentation; remove the exact existing offer.
    start = single.find("<Предложение><Ид>item-2</Ид>")
    if start >= 0:
        end = single.index("</Предложение>", start) + len("</Предложение>")
        single = single[:start] + single[end:]
    stage = stage_commerceml("catalog.xml", single.encode(), price_type_id="retail")
    assert stage.rows[1].price_rub is None
    assert "commerceml_offer_missing" in stage.rows[1].issues
    assert "invalid_or_missing_price" in stage.rows[1].issues
    assert without_offer  # Demonstrate a non-empty original fixture anchor.


def test_wholesale_offer_without_currency_never_becomes_rub() -> None:
    source = _XML.replace("<ЦенаЗаЕдиницу>450.00</ЦенаЗаЕдиницу><Валюта>RUB</Валюта>",
                          "<ЦенаЗаЕдиницу>450.00</ЦенаЗаЕдиницу>")
    stage = stage_commerceml("catalog.xml", source.encode(), price_type_id="wholesale")
    assert stage.rows[0].price_rub is None
    assert "unconfirmed_or_non_rub_currency" in stage.rows[0].issues


def test_commerceml_refuses_xml_doctype_entity_and_external_reference() -> None:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///secret">]>'
        '<КоммерческаяИнформация>&xxe;</КоммерческаяИнформация>'
    ).encode()
    with pytest.raises(ValueError, match="entities and DTD"):
        stage_commerceml("bad.xml", xml)


def test_commerceml_duplicate_id_and_unsupported_layout_rejected() -> None:
    duplicate = _XML.replace(
        "<Ид>item-2</Ид><Наименование>Клавиатура",
        "<Ид>item-1</Ид><Наименование>Клавиатура",
    )
    with pytest.raises(ValueError, match="Duplicate CommerceML product"):
        stage_commerceml("bad.xml", duplicate.encode())
    with pytest.raises(ValueError, match="same file"):
        stage_commerceml(
            "catalog.xml", '<КоммерческаяИнформация><Каталог/></КоммерческаяИнформация>'.encode(),
        )


def test_commerceml_limited_source_size_and_xml_extension() -> None:
    with pytest.raises(ValueError, match="bounded"):
        stage_commerceml("catalog.xml", b"x" * 2_000_001)
    with pytest.raises(ValueError, match="bounded"):
        stage_commerceml("wrong.txt", _XML.encode())


def test_commerceml_stage_http_requires_owner_and_preserves_original_price() -> None:
    from kpdesk.api.persisted_quote_routes import get_owner_secret
    from kpdesk.main import create_app

    app = create_app(testing=True)
    app.dependency_overrides[get_owner_secret] = lambda: "test-local-owner-" + "x" * 46
    with TestClient(app) as client:
        denied = client.post(
            "/api/catalog/suppliers/commerceml-stage",
            files={"file": ("price.xml", _XML.encode(), "application/xml")},
            headers={"Origin": "http://localhost:5173"},
        )
        assert denied.status_code == 401
        ambiguous = client.post(
            "/api/catalog/suppliers/commerceml-stage",
            headers={"Origin": "http://localhost:5173",
                     "X-KPDesk-Token": "test-local-owner-" + "x" * 46},
            files={"file": ("price.xml", _XML.encode(), "application/xml")},
        )
        assert ambiguous.status_code == 422
        assert ambiguous.json()["code"] == "commerceml_review_required"
        valid = client.post(
            "/api/catalog/suppliers/commerceml-stage",
            headers={"Origin": "http://localhost:5173",
                     "X-KPDesk-Token": "test-local-owner-" + "x" * 46},
            files={"file": ("price.xml", _XML.encode(), "application/xml")},
            data={"price_type_id": "retail"},
        )
        assert valid.status_code == 200, valid.text
        assert valid.json()["published"] is False
        assert valid.json()["stage"]["canonical_importable"] is False
        assert valid.json()["stage"]["total_rows"] == 2
        assert valid.json()["stage"]["sampled_rows"][0]["price_rub"] == "499.90"
        assert valid.json()["offered_price_type_ids"] == ["retail", "wholesale"]
