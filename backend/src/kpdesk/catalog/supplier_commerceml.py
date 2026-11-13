"""Bounded, read-only CommerceML catalog/offer staging for a 1C exchange file.

A CommerceML document can include multiple catalogs/offers, product variants and
separate price-type schedules. This adapter supports the unambiguous one-catalog,
one-offer-package, exact-ID join subset only, never assumes a price tier or SKU.
Unrecognized layouts remain review-only; no network, provider account or DB write.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import PurePath
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from kpdesk.catalog.schema import ImportLimits
from kpdesk.catalog.supplier_staging import (
    StagedSupplierRow,
    SupplierMapping,
    _stage_source_rows,
)

_MAX_XML_BYTES = 2_000_000
_MAX_XML_NODES = 35_000
_MAX_DEPTH = 18


@dataclass(frozen=True, slots=True)
class CommerceMLStage:
    """Evidence-bound staging; never directly importable or ML-training-approved."""

    source_sha256: str
    selected_price_type_id: str | None
    offered_price_type_ids: tuple[str, ...]
    rows: tuple[StagedSupplierRow, ...]
    requires_confirmation: bool = True
    canonical_importable: bool = False
    model_training_authorized: bool = False


def _tag(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: Element, name: str) -> list[Element]:
    return [child for child in element if _tag(child) == name]


def _single(parent: Element, name: str) -> Element | None:
    matching = _children(parent, name)
    if len(matching) > 1:
        raise ValueError(f"Ambiguous CommerceML section: {name}.")
    return matching[0] if matching else None


def _value(parent: Element, name: str) -> str:
    target = _single(parent, name)
    if target is None:
        return ""
    if len(target):
        raise ValueError("Expected a leaf source value, got nested CommerceML field.")
    text = (target.text or "").strip()
    if len(text) > 2000:
        raise ValueError("CommerceML source value exceeds safe cell length.")
    return text


def _required_leaf(parent: Element, name: str) -> str:
    value = _value(parent, name)
    if not value:
        raise ValueError(f"CommerceML required original field missing: {name}.")
    return value


def _structure_guard(root: Element) -> None:
    stack: list[tuple[Element, int]] = [(root, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _MAX_XML_NODES or depth > _MAX_DEPTH:
            raise ValueError("CommerceML XML complexity exceeded.")
        if len(node.attrib) > 12 or len(node.tag) > 200:
            raise ValueError("CommerceML XML node shape invalid.")
        if (node.text is not None and len(node.text) > 4000) or (
            node.tail is not None and len(node.tail) > 4000
        ):
            raise ValueError("CommerceML XML text node too long.")
        stack.extend((child, depth + 1) for child in node)


def stage_commerceml(
    filename: str,
    content: bytes,
    *,
    price_type_id: str | None = None,
    limits: ImportLimits | None = None,
) -> CommerceMLStage:
    """Stage exact-ID product/offer pairs in one CommerceML document."""
    limits = limits or ImportLimits()
    if (
        PurePath(filename).suffix.casefold() != ".xml"
        or not 1 <= len(content) <= min(limits.max_upload_bytes, _MAX_XML_BYTES)
        or not isinstance(content, bytes)
    ):
        raise ValueError("CommerceML requires a bounded nonempty .xml source.")
    if price_type_id is not None and not 1 <= len(price_type_id) <= 100:
        raise ValueError("Invalid CommerceML price type selection.")
    # Explicitly block DTD and entity declarations, regardless of parser default.
    lowered = content.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("XML entities and DTD declarations are forbidden.")
    try:
        root = fromstring(
            content, forbid_dtd=True, forbid_entities=True, forbid_external=True,
        )
    except (ParseError, DefusedXmlException, ValueError) as error:
        raise ValueError("Invalid or unsafe CommerceML XML.") from error
    _structure_guard(root)
    if _tag(root) != "КоммерческаяИнформация":
        raise ValueError("Expected official CommerceML root node.")
    catalog = _single(root, "Каталог")
    offer_package = _single(root, "ПакетПредложений")
    if catalog is None or offer_package is None:
        raise ValueError("This adapter requires catalog and offers in the same file.")
    products_parent = _single(catalog, "Товары")
    offers_parent = _single(offer_package, "Предложения")
    if products_parent is None or offers_parent is None:
        raise ValueError("No supported CommerceML product/offer collections.")
    if any(_tag(node) != "Товар" for node in products_parent):
        raise ValueError("CommerceML catalog has unsupported mixed item nodes.")
    if any(_tag(node) != "Предложение" for node in offers_parent):
        raise ValueError("CommerceML offers have unsupported mixed item nodes.")

    products: dict[str, tuple[str, str, str]] = {}
    for product in _children(products_parent, "Товар"):
        identity = _required_leaf(product, "Ид")
        if identity in products:
            raise ValueError("Duplicate CommerceML product identity.")
        name = _required_leaf(product, "Наименование")
        sku = _value(product, "Артикул")  # Never infer from GUID.
        unit = _value(product, "БазоваяЕдиница")
        products[identity] = (name, sku, unit)
    if not 1 <= len(products) <= limits.max_rows:
        raise ValueError("CommerceML product count outside intake limits.")

    offers: dict[str, list[tuple[str, str, str]]] = {}
    kinds: set[str] = set()
    for offer in _children(offers_parent, "Предложение"):
        product_id = _required_leaf(offer, "Ид")
        if product_id in offers:
            raise ValueError("Duplicate CommerceML offer identity or variant requires review.")
        price_root = _single(offer, "Цены")
        price_rows: list[tuple[str, str, str]] = []
        if price_root is not None:
            if any(_tag(row) != "Цена" for row in price_root):
                raise ValueError("CommerceML offers contain unknown price record.")
            for entry in _children(price_root, "Цена"):
                identity = _value(entry, "ИдТипаЦены")
                amount = _value(entry, "ЦенаЗаЕдиницу")
                currency = _value(entry, "Валюта")
                if not identity:
                    identity = "_single_unspecified_type"
                if any(previous[0] == identity for previous in price_rows):
                    raise ValueError("Duplicate price tiers for CommerceML offer.")
                price_rows.append((identity, amount, currency))
                kinds.add(identity)
        offers[product_id] = price_rows
    if any(identity not in products for identity in offers):
        raise ValueError("Orphan CommerceML offers or variant IDs require manual mapping.")
    if len(kinds) > 1 and price_type_id is None:
        raise ValueError("Several CommerceML price types: select the supplier price role.")
    if price_type_id is not None and price_type_id not in kinds:
        raise ValueError("Selected CommerceML price type does not exist.")
    selected = price_type_id or (next(iter(kinds)) if kinds else None)

    original_rows: list[tuple[int, list[object]]] = [
        (1, ["name", "sku", "price", "currency", "unit"])
    ]
    unpriced_ids: set[str] = set()
    for index, (identity, (name, sku, unit)) in enumerate(products.items(), 2):
        choice = next((price for price in offers.get(identity, ())
                       if price[0] == selected), None)
        if choice is None:
            unpriced_ids.add(identity)
        raw_price = choice[1] if choice is not None else ""
        currency = choice[2] if choice is not None else ""
        original_rows.append((index, [name, sku, raw_price, currency, unit]))
    staged = _stage_source_rows(
        filename, content,
        SupplierMapping(
            name=0, sku=1, price=2, currency=3, sale_unit=4, header_row=1,
        ),
        original_rows, limits,
    )
    flagged: list[StagedSupplierRow] = []
    for original_id, row in zip(products, staged.rows, strict=True):
        extra = ("commerceml_offer_missing",) if original_id in unpriced_ids else ()
        flagged.append(replace(row, issues=(*row.issues, *extra)))
    return CommerceMLStage(
        hashlib.sha256(content).hexdigest(), selected, tuple(sorted(kinds)),
        tuple(flagged),
    )
