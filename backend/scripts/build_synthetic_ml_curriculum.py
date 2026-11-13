"""Curated synthetic multi-category matching curriculum, not customer gold.

Product names, requests and labels below are local hand-authored teaching cases.
No supplier prices or scraped customer documents are imported. The training
CLI records the synthetic provenance and never promotes it to production.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.matching.training import split_groups

# category, SKU, catalog title, independent wording of the same *synthetic*
# intended article, typed profile, sale unit. Every requested SKU is explicit.
_SPECS: tuple[tuple[str, str, str, str, dict[str, Any], str], ...] = (
    ("paper", "paper-a4-white80", "Бумага офисная белая А4 80 г/м2, 500 листов",
     "белая бумага для печати А4 плотность 80 500 листов", {"paper_format": "A4", "paper_gsm": 80}, "пачка"),
    ("paper", "paper-a4-color80", "Бумага цветная А4 80 г/м2, 100 листов",
     "цветные листы для офиса А4 80 плотность 100 листов", {"paper_format": "A4", "paper_gsm": 80}, "пачка"),
    ("paper", "paper-a3-white80", "Бумага офисная белая А3 80 г/м2, 500 листов",
     "офисные белые листы А3 80 плотности 500 в пачке", {"paper_format": "A3", "paper_gsm": 80}, "пачка"),
    ("paper", "paper-a4-white120", "Бумага белая А4 120 г/м2, 250 листов",
     "плотная белая бумага А4 120 250 листов", {"paper_format": "A4", "paper_gsm": 120}, "пачка"),
    ("paper", "paper-a4-color160", "Бумага цветная А4 160 г/м2, 250 листов",
     "цветная плотная бумага А4 160 250 листов", {"paper_format": "A4", "paper_gsm": 160}, "пачка"),

    ("pen", "pen-ball-blue", "Ручка шариковая синяя 0,7 мм",
     "синяя шариковая ручка с толщиной письма 0,7", {"pen_ink_color": "синий", "attributes": {"pen_type": "шариковая", "tip_mm_x10": 7}}, "штука"),
    ("pen", "pen-ball-black", "Ручка шариковая чёрная 0,7 мм",
     "чёрная ручка шариковая тонкий стержень 0,7", {"pen_ink_color": "черный", "attributes": {"pen_type": "шариковая", "tip_mm_x10": 7}}, "штука"),
    ("pen", "pen-gel-blue", "Ручка гелевая синяя 0,5 мм",
     "гелевая ручка синие чернила 0,5 миллиметра", {"pen_ink_color": "синий", "attributes": {"pen_type": "гелевая", "tip_mm_x10": 5}}, "штука"),
    ("pen", "pen-gel-red", "Ручка гелевая красная 0,5 мм",
     "красная гелевая ручка стержень 0,5 мм", {"pen_ink_color": "красный", "attributes": {"pen_type": "гелевая", "tip_mm_x10": 5}}, "штука"),

    ("notebook", "notebook-a5-grid48", "Тетрадь А5 48 листов клетка",
     "тетрадка 48 листов А5 в клетку", {"attributes": {"format": "A5", "sheets": 48, "ruling": "клетка"}}, "штука"),
    ("notebook", "notebook-a5-line48", "Тетрадь А5 48 листов линейка",
     "ученическая тетрадка А5 48 листов в линейку", {"attributes": {"format": "A5", "sheets": 48, "ruling": "линейка"}}, "штука"),
    ("notebook", "notebook-a4-grid96", "Тетрадь А4 96 листов клетка",
     "общая большая тетрадь А4 96 листов в клетку", {"attributes": {"format": "A4", "sheets": 96, "ruling": "клетка"}}, "штука"),
    ("notebook", "notebook-a4-line96", "Тетрадь А4 96 листов линейка",
     "общая большая тетрадь А4 на 96 листов линейка", {"attributes": {"format": "A4", "sheets": 96, "ruling": "линейка"}}, "штука"),

    ("mouse", "mouse-usb-1200", "Мышь компьютерная проводная USB 1200 dpi",
     "проводная компьютерная мышка USB чувствительность 1200", {"attributes": {"connection": "usb", "dpi": 1200}}, "штука"),
    ("mouse", "mouse-usb-3200", "Мышь компьютерная проводная USB 3200 dpi",
     "мышь проводная с кабелем USB 3200 точек на дюйм", {"attributes": {"connection": "usb", "dpi": 3200}}, "штука"),
    ("mouse", "mouse-radio-1600", "Мышь беспроводная радиоканал 1600 dpi",
     "мышка без провода радиоканал 1600 dpi", {"attributes": {"connection": "radio", "dpi": 1600}}, "штука"),
    ("mouse", "mouse-bt-2400", "Мышь беспроводная Bluetooth 2400 dpi",
     "блютуз мышь без проводов 2400 dpi", {"attributes": {"connection": "bluetooth", "dpi": 2400}}, "штука"),

    ("usb_drive", "flash-16-usb3", "Флешка USB 3.0 16 ГБ",
     "usb накопитель 16 гигабайт версия 3.0", {"attributes": {"capacity_gb": 16, "usb_standard": "3.0"}}, "штука"),
    ("usb_drive", "flash-32-usb3", "Флешка USB 3.0 32 ГБ",
     "флеш накопитель на 32 гигабайта USB 3.0", {"attributes": {"capacity_gb": 32, "usb_standard": "3.0"}}, "штука"),
    ("usb_drive", "flash-64-usb32", "Флешка USB 3.2 64 ГБ",
     "съемная память usb 3.2 64 гигабайта", {"attributes": {"capacity_gb": 64, "usb_standard": "3.2"}}, "штука"),
    ("usb_drive", "flash-128-usb32", "Флешка USB 3.2 128 ГБ",
     "накопитель на 128 Гб интерфейс 3.2", {"attributes": {"capacity_gb": 128, "usb_standard": "3.2"}}, "штука"),

    ("paperclip", "clip-28-100", "Скрепки металлические 28 мм 100 штук",
     "скрепки канцелярские металлические 28 миллиметров по 100", {"attributes": {"size_mm": 28, "pack_count": 100}}, "пачка"),
    ("paperclip", "clip-33-100", "Скрепки металлические 33 мм 100 штук",
     "металлические скрепки для бумаг 33 мм пачка 100", {"attributes": {"size_mm": 33, "pack_count": 100}}, "пачка"),
    ("paperclip", "clip-50-50", "Скрепки металлические 50 мм 50 штук",
     "крупные скрепки металлические 50 миллиметров 50 шт", {"attributes": {"size_mm": 50, "pack_count": 50}}, "пачка"),
    ("paperclip", "clip-28-200", "Скрепки металлические 28 мм 200 штук",
     "скрепки для бумаги 28 миллиметров 200 штук пачка", {"attributes": {"size_mm": 28, "pack_count": 200}}, "пачка"),

    ("ruler", "ruler-150", "Линейка пластиковая 15 см",
     "линейка для черчения пластик длина 150 мм", {"attributes": {"length_mm": 150}}, "штука"),
    ("ruler", "ruler-200", "Линейка пластиковая 20 см",
     "чертёжная линейка пластик 200 миллиметров", {"attributes": {"length_mm": 200}}, "штука"),
    ("ruler", "ruler-300", "Линейка металлическая 30 см",
     "металлическая линейка 300 мм длина", {"attributes": {"length_mm": 300}}, "штука"),
    ("ruler", "ruler-500", "Линейка металлическая 50 см",
     "металлическая линейка длинная 500 мм", {"attributes": {"length_mm": 500}}, "штука"),

    ("stapler", "stapler-10", "Степлер канцелярский до 10 листов",
     "маленький степлер на десять листов", {"attributes": {"capacity_sheets": 10}}, "штука"),
    ("stapler", "stapler-20", "Степлер канцелярский до 20 листов",
     "степлер настольный сшивание двадцати листов", {"attributes": {"capacity_sheets": 20}}, "штука"),
    ("stapler", "stapler-30", "Степлер канцелярский до 30 листов",
     "сшиватель бумаги на тридцать листов", {"attributes": {"capacity_sheets": 30}}, "штука"),
    ("stapler", "stapler-50", "Степлер мощный до 50 листов",
     "усиленный степлер для пятидесяти листов", {"attributes": {"capacity_sheets": 50}}, "штука"),

    ("envelope", "envelope-c5-white", "Конверт C5 белый",
     "почтовый конверт белого цвета формат C5", {"attributes": {"format": "C5", "color": "белый"}}, "штука"),
    ("envelope", "envelope-dl-white", "Конверт DL белый",
     "белый конверт для писем европочтовый DL", {"attributes": {"format": "DL", "color": "белый"}}, "штука"),
    ("envelope", "envelope-c4-kraft", "Конверт C4 крафтовый",
     "крафт пакет конверт C4 для документов", {"attributes": {"format": "C4", "color": "крафт"}}, "штука"),
    ("envelope", "envelope-c5-beige", "Конверт C5 бежевый",
     "бежевый конверт для почты формат С5", {"attributes": {"format": "C5", "color": "бежевый"}}, "штука"),

    ("folder", "folder-a4-20", "Папка-регистратор А4 корешок 20 мм",
     "папка архивная формат А4 узкий корешок двадцать мм", {"attributes": {"format": "A4", "spine_mm": 20}}, "штука"),
    ("folder", "folder-a4-50", "Папка-регистратор А4 корешок 50 мм",
     "архивная папка А4 ширина корешка пятьдесят миллиметров", {"attributes": {"format": "A4", "spine_mm": 50}}, "штука"),
    ("folder", "folder-a4-75", "Папка-регистратор А4 корешок 75 мм",
     "офисный регистратор документов А4 корешок 75 миллиметров", {"attributes": {"format": "A4", "spine_mm": 75}}, "штука"),
    ("folder", "folder-a5-40", "Папка-регистратор А5 корешок 40 мм",
     "папка для бумаг А5 корешок сорок мм", {"attributes": {"format": "A5", "spine_mm": 40}}, "штука"),

    ("glue", "glue-stick-15", "Клей-карандаш 15 г",
     "канцелярский клей в стике пятнадцать граммов", {"attributes": {"glue_type": "stick", "volume_ml": 15}}, "штука"),
    ("glue", "glue-stick-35", "Клей-карандаш 35 г",
     "клей карандаш большой тридцать пять граммов", {"attributes": {"glue_type": "stick", "volume_ml": 35}}, "штука"),
    ("glue", "glue-pva-50", "Клей ПВА 50 мл",
     "жидкий ПВА клей объем 50 миллилитров", {"attributes": {"glue_type": "pva", "volume_ml": 50}}, "штука"),
    ("glue", "glue-pva-100", "Клей ПВА 100 мл",
     "клей жидкий ПВА сто миллилитров", {"attributes": {"glue_type": "pva", "volume_ml": 100}}, "штука"),

    ("marker", "marker-perm-black", "Маркер перманентный чёрный 2 мм",
     "черный нестираемый маркер перманентный два мм", {"attributes": {"ink_color": "черный", "marker_type": "permanent", "tip_mm_x10": 20}}, "штука"),
    ("marker", "marker-perm-blue", "Маркер перманентный синий 2 мм",
     "синий нестираемый фломастер маркер перманентный", {"attributes": {"ink_color": "синий", "marker_type": "permanent", "tip_mm_x10": 20}}, "штука"),
    ("marker", "marker-board-black", "Маркер для доски чёрный 2 мм",
     "черный маркер стираемый для белой доски", {"attributes": {"ink_color": "черный", "marker_type": "whiteboard", "tip_mm_x10": 20}}, "штука"),
    ("marker", "marker-board-red", "Маркер для доски красный 2 мм",
     "красный маркер для магнитной маркерной доски", {"attributes": {"ink_color": "красный", "marker_type": "whiteboard", "tip_mm_x10": 20}}, "штука"),
)

_QUERY_FORMS = (
    "Закупить для отдела: {item}; требуется 5 единиц.",
    "Позиция коммерческой заявки: {item}, количество 3.",
    "Подберите товар по описанию: {item}; необходимо две единицы.",
)


def build_curriculum() -> dict[str, Any]:
    products: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    used_titles: set[str] = set()
    for category, sku, name, phrase, details, unit in _SPECS:
        if sku in {item["sku"] for item in products} or name.casefold() in used_titles:
            raise ValueError("Duplicate curated training product.")
        used_titles.add(name.casefold())
        product = CatalogProduct.model_validate({
            "sku": sku, "name": name, "category": category,
            "sale_unit": unit, "pieces_per_sale_unit": 1,
            "price_rub": "100.00", **details,
        })
        products.append(product.model_dump(mode="json"))
        for index, form in enumerate(_QUERY_FORMS):
            request = RequestedItem.model_validate({
                "description": form.format(item=phrase),
                "category": category, "quantity": 3, "unit": unit,
                # All ranking labels originate from our synthetic pairing,
                # not from supplier pricing or an unverified actual order.
            })
            queries.append({
                "query_id": f"{sku}-phr-{index + 1}",
                "query_group_id": f"intent-{sku}",
                "request": request.model_dump(mode="json"),
                "relevant_skus": [sku],
            })
    result: dict[str, Any] = {
        "provenance": {
            "kind": "synthetic_example",
            "annotation_method": "local_hand_curated_synthetic_intent_to_sku",
            "owner_authorized_local_training": True,
            "synthetic_only": True,
            "independent_gold": False,
            "rights_verified": False,
            "production_training": False,
        },
        "catalog": products, "queries": queries,
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a locally curated synthetic curriculum.")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    body = build_curriculum()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with options.output.open("x", encoding="utf-8") as file:
        json.dump(body, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    dataset = load_dataset(options.output)
    split = split_groups(dataset.queries, seed=42)
    print(json.dumps({
        "dataset": str(options.output),
        "catalog_skus": len(dataset.products),
        "labeled_synthetic_queries": len(dataset.queries),
        "train_queries": len(split.train),
        "validation_queries": len(split.validation),
        "test_queries": len(split.test),
        "training_source": dataset.provenance_kind,
        "dataset_sha256": dataset.source_sha256,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
