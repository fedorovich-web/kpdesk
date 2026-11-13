"""A39: independent product taxonomy with source-free synthetic category examples.

This file contains manually authored examples, not customer gold. Each product
intent is one isolation group; test intents never feed model.fit or thresholds.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from scripts.build_synthetic_ml_curriculum import build_curriculum

# Four DISTINCT accessory product intents per category, phrased independently.
# Accessory taxonomy categories are not aliases of their parent products.
_ACCESSORIES: tuple[tuple[str, str, str, str], ...] = (
    ("mouse_pad", "pad-fabric-small", "Коврик для мыши тканевый 250 на 200 мм", "тканевая подложка под манипулятор 250 на 200"),
    ("mouse_pad", "pad-gel-rest", "Коврик компьютерный гелевый с опорой для кисти", "гелевая подставка под мышку с упором кисти"),
    ("mouse_pad", "pad-gaming-large", "Коврик игровой для мыши большой 450 на 400 мм", "большая игровая поверхность под мышь 450 на 400"),
    ("mouse_pad", "pad-cork", "Коврик для мыши пробковый экологичный", "пробковая подложка для перемещения компьютерной мышки"),
    ("pen_refill", "refill-ball-blue", "Стержень сменный шариковый синий 0,7 мм", "синий запасной пишущий узел шарикового типа"),
    ("pen_refill", "refill-gel-black", "Стержень для гелевой ручки чёрный 0,5 мм", "черная сменная гелевая паста в стержне"),
    ("pen_refill", "refill-ball-green", "Запасной стержень шариковый зелёный", "зелёный сменный стержень шариковой ручки"),
    ("pen_refill", "refill-roller-red", "Стержень для ручки роллера красный", "красный запасной роллерный пишущий стержень"),
    ("usb_adapter", "adapter-c-to-a", "Переходник USB-C на USB-A", "адаптер юсб тайп си на стандартный юсб"),
    ("usb_adapter", "adapter-a-to-c", "Адаптер USB-A на USB-C", "переходник юсб а на разъём тип си"),
    ("usb_adapter", "adapter-c-hdmi", "USB-C переходник к HDMI", "адаптер из юсб си в видеовыход HDMI"),
    ("usb_adapter", "adapter-c-network", "Сетевой адаптер USB-C Ethernet", "переходник юсб си на проводную сеть"),
    ("notebook_cover", "cover-a5-clear", "Прозрачная обложка для тетради А5", "прозрачный чехол тетради формата а5"),
    ("notebook_cover", "cover-a4-pvc", "Обложка из ПВХ на тетрадь А4", "поливинилхлоридная защита обложки тетради а4"),
    ("notebook_cover", "cover-a5-matte", "Матовая обложка тетрадная А5", "матовый защитный чехол учебной тетради а5"),
    ("notebook_cover", "cover-a5-colored", "Обложка цветная для ученической тетради", "цветная обложка на тетрадку"),
    ("staple_remover", "remover-office-black", "Антистеплер настольный чёрный", "черный инструмент для снятия скоб со стопки"),
    ("staple_remover", "remover-metal", "Скобоудалитель металлический", "металлическое устройство извлечения скобок"),
    ("staple_remover", "remover-slim", "Удалитель скоб канцелярский узкий", "узкий съёмник бумажных скоб"),
    ("staple_remover", "remover-lever", "Антистеплер рычажный эргономичный", "эргономичный извлекатель скоб рычажный"),
    ("marker_refill", "refill-marker-blue", "Чернила для заправки маркера синие", "синяя заправочная жидкость для маркера"),
    ("marker_refill", "refill-marker-black", "Набор заправки маркеров чёрный", "черная заправка письменного маркера"),
    ("marker_refill", "refill-marker-red", "Чернила красные для заправки маркера доски", "красная заправочная краска досочного маркера"),
    ("marker_refill", "refill-marker-green", "Заправочный флакон зелёных чернил для маркера", "зелёная жидкость наполнения маркера"),
)
_FORMS = (
    "Нужен товар для снабжения: {item}; три единицы.",
    "Составить заявку на {item}; количество три штуки.",
    "Поставка для офиса: {item}; заказать 3 шт.",
)


def build_category_curriculum() -> dict[str, Any]:
    original = build_curriculum()
    catalog: list[dict[str, Any]] = list(original["catalog"])
    queries: list[dict[str, Any]] = list(original["queries"])
    known = {product["sku"] for product in catalog}
    for category, sku, name, phrase in _ACCESSORIES:
        if sku in known:
            raise ValueError("Duplicate synthetic SKU in taxonomy curriculum.")
        known.add(sku)
        product = CatalogProduct.model_validate({
            "sku": sku, "name": name, "category": category, "sale_unit": "штука",
            "pieces_per_sale_unit": 1, "price_rub": "80.00",
        })
        catalog.append(product.model_dump(mode="json"))
        for number, form in enumerate(_FORMS, start=1):
            request = RequestedItem.model_validate({
                "description": form.format(item=phrase),
                "category": category, "quantity": 3, "unit": "штука",
            })
            queries.append({
                "query_id": f"{sku}-wording-{number}",
                "query_group_id": f"intent-{sku}",
                "request": request.model_dump(mode="json"),
                "relevant_skus": [sku],
            })
    return {
        "provenance": {
            "kind": "synthetic_example",
            "annotation_method": "hand_curated_product_category_and_accessory_intents",
            "synthetic_only": True,
            "independent_gold": False,
            "owner_authorized_local_training": True,
            "production_training": False,
        },
        "catalog": catalog,
        "queries": queries,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create synthetic category+accessory intents.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(build_category_curriculum(), file, ensure_ascii=False, sort_keys=True, indent=2)
        file.write("\n")
    data = load_dataset(args.output)
    counts: dict[str, int] = {}
    for product in data.products.values():
        counts[product.category] = counts.get(product.category, 0) + 1
    print(json.dumps({
        "dataset_sha256": data.source_sha256,
        "product_count": len(data.products),
        "query_count": len(data.queries),
        "category_count": len(counts),
        "categories": counts,
        "provenance": data.provenance_kind,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
