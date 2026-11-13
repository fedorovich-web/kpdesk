"""Full-taxonomy canonical catalog import checks at 1000 and 5000 products.

Strict catalog CSV/XLSX import is distinct from supplier source staging:
the caller must explicitly review and supply compatible attributes.
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter
from decimal import Decimal

import pytest
from openpyxl import Workbook

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS_V2, ImportLimits
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts.build_a46_taxonomy_coverage import build_taxonomy_coverage

_CATEGORIES = tuple(sorted(CATEGORY_ATTRIBUTES))


def _verified_synthetic_canonical_rows(size: int) -> list[list[str]]:
    if not 1 <= size <= 5001:
        raise ValueError("Unsupported synthetic catalog size")
    source = build_taxonomy_coverage()
    examples = {
        category: [item for item in source["catalog"] if item["category"] == category]
        for category in _CATEGORIES
    }
    if not all(examples.values()):
        raise AssertionError("Taxonomy fixture lacks catalog category")
    rows = [list(COLUMNS_V2)]
    for offset in range(size):
        category = _CATEGORIES[offset % len(_CATEGORIES)]
        examples_for_category = examples[category]
        sample = examples_for_category[
            (offset // len(_CATEGORIES)) % len(examples_for_category)
        ]
        # These are explicitly synthetic variants, not independently sourced
        # supplier SKUs or product-matching gold labels.
        unit = "пачка" if category == "paper" else (
            "коробка" if offset % 4 == 0 else "штука"
        )
        pack = 1 if unit != "коробка" else 10
        rows.append([
            f"CAT-{offset+1:05d}",
            f"{sample['name']} вариант {offset+1:05d}",
            category,
            unit,
            str(pack),
            f"{100+offset//10}.{offset%100:02d}",
            "",
            "A4" if category == "paper" else "",
            "80" if category == "paper" else "",
            "синий" if category == "pen" else "",
            "шариковая" if category == "pen" else "",
            json.dumps(sample["attributes"], ensure_ascii=False, separators=(",", ":")),
        ])
    return rows


def _canonical_csv(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=";", lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def _canonical_xlsx(rows: list[list[str]]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Products")
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


@pytest.mark.parametrize("size", [1000, 5000])
def test_canonical_csv_and_xlsx_agree_for_all_taxonomy_categories(size: int) -> None:
    rows = _verified_synthetic_canonical_rows(size)
    csv_result = parse_price_list(
        "canonical.csv", _canonical_csv(rows), ImportLimits(),
    )
    xlsx_result = parse_price_list(
        "canonical.xlsx", _canonical_xlsx(rows), ImportLimits(),
    )
    assert csv_result.publishable and xlsx_result.publishable, (
        csv_result.errors[:3], xlsx_result.errors[:3],
    )
    assert len(csv_result.products) == len(xlsx_result.products) == size
    assert csv_result.products == xlsx_result.products
    assert len({p.sku for p in csv_result.products}) == size
    assert {p.category for p in csv_result.products} == set(_CATEGORIES)
    distribution = Counter(p.category for p in csv_result.products)
    assert min(distribution.values()) >= size // len(_CATEGORIES)
    for offset, product in enumerate(csv_result.products):
        assert product.sku == f"CAT-{offset+1:05d}"
        assert product.name.endswith(f"вариант {offset+1:05d}")
        assert product.price_rub == Decimal(f"{100+offset//10}.{offset%100:02d}")
        assert product.category == rows[offset+1][2]
        assert product.pieces_per_sale_unit == int(rows[offset+1][4])
        assert product.attributes == json.loads(rows[offset+1][11])


def test_canonical_5001_products_rejected_atomically() -> None:
    rows = _verified_synthetic_canonical_rows(5001)
    result = parse_price_list("oversize.csv", _canonical_csv(rows), ImportLimits())
    assert not result.publishable
    assert result.products == ()
    assert any(error.code == "too_many_rows" for error in result.errors)


def test_canonical_wrong_category_attributes_rejected_atomically() -> None:
    rows = _verified_synthetic_canonical_rows(1000)
    # Preserve all products but deliberately apply incompatible taxonomy to
    # one SKU; never publish a partial result with the remaining valid rows.
    rows[1][2] = "printer"
    result = parse_price_list("wrong-category.csv", _canonical_csv(rows), ImportLimits())
    assert not result.publishable
    assert result.products == ()
    assert any(error.code == "invalid_product" and error.row == 2 for error in result.errors)
