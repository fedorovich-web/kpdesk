from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kpdesk.universal.importer import parse_universal_price
from kpdesk.universal.matching import build_quote, match_request
from kpdesk.universal.schema import (
    CatalogSnapshot,
    QuoteContext,
    QuoteSnapshot,
    RequestLine,
    Requirement,
    Selection,
    Specification,
    UniversalProduct,
    UniversalRequest,
)


def catalog(**changes: object) -> CatalogSnapshot:
    fields: dict[str, object] = {"sku": "C1", "name": "Кабель", "sale_unit": "м",
                                   "price_rub": "1200", "price_quantity": "100"}
    fields.update(changes)
    return CatalogSnapshot(version_id=uuid4(), source_sha256="a" * 64,
                           products=(UniversalProduct.model_validate(fields),))


def request(**changes: object) -> UniversalRequest:
    fields: dict[str, object] = {"description": "Кабель", "source_quote": "Кабель 250 см",
                                   "quantity": "250", "sale_unit": "см"}
    fields.update(changes)
    return UniversalRequest(lines=(RequestLine.model_validate(fields),))


def test_unit_conversion_cannot_recommend_an_unrepresentable_sale_quantity() -> None:
    c = catalog(sale_unit='мм', price_rub='1', price_quantity='1')
    r = request(quantity='999999999999.999', sale_unit='м')
    candidate = match_request(r, c)['lines'][0]['candidates'][0]
    assert candidate['status'] == 'incompatible'
    assert 'quantity_precision' in candidate['reason_codes']
    quote = build_quote(c, r, (Selection(line_number=1, sku='C1'),),
                        QuoteContext(issuer_name='Поставщик'), uuid4(), 1)
    assert not quote.ready_for_draft and quote.total_rub is None


def test_price_basis_overflow_is_a_domain_rejection_instead_of_decimal_failure() -> None:
    c = catalog(price_rub='99999999999999.99', price_quantity='0.001')
    r = request(quantity='999999999999.999', sale_unit='м')
    with pytest.raises(ValueError):
        build_quote(c, r, (Selection(line_number=1, sku='C1'),),
                    QuoteContext(issuer_name='Поставщик'), uuid4(), 1)


def test_explicit_manager_selection_price_basis_conversion_and_json_roundtrip() -> None:
    c, r = catalog(), request()
    absent = build_quote(c, r, (), QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    assert not absent.ready_for_draft and absent.total_rub is None
    q = build_quote(c, r, (Selection(line_number=1, sku="C1"),),
                    QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    assert q.ready_for_draft and q.total_rub == Decimal("30.00")
    assert q.lines[0].sale_quantity == Decimal("2.5")
    assert QuoteSnapshot.model_validate_json(q.model_dump_json()) == q
    assert len(q.snapshot_sha256) == 64 and not q.approved


@pytest.mark.parametrize("changes,code", [
    ({"brand": "Other"}, "brand_mismatch"),
    ({"model": "XYZ"}, "model_mismatch"),
    ({"sku": "different"}, "sku_mismatch"),
    ({"sale_unit": "кг"}, "unit_mismatch"),
    ({"quantity": None}, "quantity_missing"),
])
def test_hard_constraints_never_lexical_accept(changes: dict[str, object], code: str) -> None:
    c, r = catalog(brand="Brand", model="Model"), request(**changes)
    result = match_request(r, c)
    assert code in result["lines"][0]["candidates"][0]["reason_codes"]
    q = build_quote(c, r, (Selection(line_number=1, sku="C1"),),
                    QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    assert not q.ready_for_draft and q.total_rub is None


def test_numeric_specs_convert_only_same_dimension_and_strings_exact() -> None:
    c = catalog(specifications=(Specification(name="длина", value=Decimal(1), unit="м"),
                                Specification(name="цвет", value="синий")))
    r = request(requirements=(Requirement(name="длина", value=Decimal(50), unit="см",
                                         operator="min", source_quote="50 см"),
                              Requirement(name="цвет", value="СИНИЙ", source_quote="синий")))
    assert match_request(r, c)["lines"][0]["candidates"][0]["status"] == "compatible"
    wrong = request(requirements=(Requirement(name="длина", value=Decimal(1), unit="кг",
                                             operator="min", source_quote="1 кг"),))
    assert match_request(wrong, c)["lines"][0]["candidates"][0]["status"] == "incompatible"


def test_duplicate_missing_specs_and_discrete_fraction_block_readiness() -> None:
    c = catalog(specifications=(Specification(name="цвет", value="синий"),
                                Specification(name="Цвет", value="красный")))
    r = request(requirements=(Requirement(name="цвет", value="синий", source_quote="синий"),))
    assert match_request(r, c)["lines"][0]["candidates"][0]["status"] == "needs_review"
    missing = match_request(r, catalog())["lines"][0]["candidates"][0]
    assert missing["status"] == "needs_review"
    discrete = catalog(sale_unit="шт", price_quantity="1")
    assert match_request(request(quantity="1.5", sale_unit="шт"), discrete)["lines"][0]["candidates"][0]["status"] == "incompatible"


def test_unknown_unit_requires_explicit_step_and_decimal_limits() -> None:
    with pytest.raises(ValidationError):
        catalog(sale_unit="канистра")
    assert catalog(sale_unit="канистра", quantity_step="1").products[0].sale_unit == "канистра"
    with pytest.raises(ValidationError):
        request(quantity="0.0001")
    with pytest.raises(ValidationError):
        catalog(price_rub="NaN")


def test_import_arbitrary_category_and_extra_specs_all_or_nothing() -> None:
    data = "sku;name;price_rub;sale_unit;category;цвет\nX;Жидкость;100;л;Химия будущего;синий\n".encode()
    c = parse_universal_price("price.csv", data)
    assert c.products[0].category == "Химия будущего"
    assert c.products[0].source_row == 2
    assert c.products[0].specifications == (Specification(name="цвет", value="синий"),)
    with pytest.raises(ValueError):
        parse_universal_price("bad.csv", data + b"Y;Bad;;\n")


def test_duplicate_sku_and_header_rejected() -> None:
    for data in (b"sku;name;price_rub;sale_unit;sku\nX;Y;1;kg;Z\n",
                 b"sku;name;price_rub;sale_unit\nX;Y;1;kg\nX;Z;2;kg\n"):
        with pytest.raises(ValueError):
            parse_universal_price("bad.csv", data)


def test_numeric_and_text_spec_json_roundtrip_and_unknown_dimension() -> None:
    c = catalog(specifications=(Specification(name="точность", value=Decimal("0.123456")),
                                Specification(name="номер", value="50")))
    restored = CatalogSnapshot.model_validate_json(c.model_dump_json())
    assert restored == c
    unknown = catalog(specifications=(Specification(name="давление", value=Decimal(2), unit="бар"),))
    r = request(requirements=(Requirement(name="давление", value=Decimal(1), unit="бар",
                                         operator="min", source_quote="давление от 1 бар"),))
    assert match_request(r, unknown)["lines"][0]["candidates"][0]["status"] == "needs_review"


def test_snapshot_rejects_tampering_even_with_recomputed_digest() -> None:
    from kpdesk.universal.schema import snapshot_digest
    q = build_quote(catalog(), request(), (Selection(line_number=1, sku="C1"),),
                    QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    body = q.model_dump(mode="json")
    body["lines"][0]["sale_quantity"] = "999"
    with pytest.raises(ValidationError):
        QuoteSnapshot.model_validate(body)
    body["snapshot_sha256"] = snapshot_digest(body)
    with pytest.raises(ValidationError):
        QuoteSnapshot.model_validate(body)


def test_liquid_fractional_quantity_step_and_exact_text_specs() -> None:
    c = catalog(sale_unit="л", price_rub="150", price_quantity="1", quantity_step="0.25",
                specifications=(Specification(name="марка", value="АБ-12"),))
    r = request(quantity="1250", sale_unit="мл", requirements=(
        Requirement(name="марка", value="АБ-12", source_quote="марка АБ-12"),))
    q = build_quote(c, r, (Selection(line_number=1, sku="C1"),),
                    QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    assert q.total_rub == Decimal("187.50")
    assert match_request(request(quantity="1.1", sale_unit="л"), c)["lines"][0]["candidates"][0]["status"] == "incompatible"
    assert match_request(request(quantity="1", sale_unit="л", requirements=(
        Requirement(name="марка", value="АБ-1", source_quote="АБ-1"),)), c)["lines"][0]["candidates"][0]["status"] == "incompatible"


def test_xlsx_arbitrary_columns_and_formulas_preserve_safety() -> None:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Products"
    headers = ["sku", "name", "price_rub", "sale_unit", *[f"характеристика {i}" for i in range(20)]]
    sheet.append(headers)
    sheet.append(["N", "Насос", 100, "шт", *[str(i) for i in range(20)]])
    stream = BytesIO()
    workbook.save(stream)
    c = parse_universal_price("price.xlsx", stream.getvalue())
    assert len(c.products[0].specifications) == 20
    sheet["E2"] = "=1+1"
    stream = BytesIO()
    workbook.save(stream)
    with pytest.raises(ValueError, match="formula_forbidden"):
        parse_universal_price("price.xlsx", stream.getvalue())


@pytest.mark.parametrize("field,value", [("sku", 123), ("model", 123), ("name", 123),
                                          ("brand", 123), ("category", 123), ("sale_unit", 123)])
def test_xlsx_nontext_identity_cannot_silently_change(field: str, value: object) -> None:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Products"
    headers = ["sku", "name", "price_rub", "sale_unit", "model", "brand", "category"]
    row: list[object] = ["00123", "Насос", 100, "шт", "M", "B", "Насосы"]
    row[headers.index(field)] = value
    sheet.append(headers)
    sheet.append(row)
    sheet.cell(2, headers.index(field) + 1).number_format = "00000"
    stream = BytesIO()
    workbook.save(stream)
    with pytest.raises(ValueError, match="non_text_identity"):
        parse_universal_price("price.xlsx", stream.getvalue())


def test_xlsx_date_model_rejected_and_text_sku_preserved() -> None:
    from datetime import date
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Products"
    sheet.append(["sku", "name", "price_rub", "sale_unit", "model"])
    sheet.append(["00123", "Насос", 100, "шт", date(2026, 1, 2)])
    stream = BytesIO()
    workbook.save(stream)
    with pytest.raises(ValueError, match="non_text_identity"):
        parse_universal_price("price.xlsx", stream.getvalue())
    sheet["E2"] = "01-02"
    stream = BytesIO()
    workbook.save(stream)
    result = parse_universal_price("price.xlsx", stream.getvalue())
    assert result.products[0].sku == "00123" and result.products[0].price_rub == Decimal(100)
