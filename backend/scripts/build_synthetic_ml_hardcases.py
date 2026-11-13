"""A38: source-free synthetic absent-SKU challenges for real ML training.

Hard incompatibilities and plausible but absent accessory/variant queries are
hand-authored. This is NOT human gold or customer query evidence.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.schema import RequestedItem
from kpdesk.matching.training import split_groups
from scripts.build_synthetic_ml_curriculum import build_curriculum

# Category, synthetic intent, phrase, constraint profile.
_NO_MATCH: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("paper", "paper-b5-90", "Бумага B5 плотность 90 г/м2", {"paper_format": "B5", "paper_gsm": 90}),
    ("pen", "pen-purple-roller", "Ручка роллер с фиолетовыми чернилами 0,8 мм",
     {"pen_ink_color": "фиолетовый", "attributes": {"pen_type": "роллер", "tip_mm_x10": 8}}),
    ("notebook", "notebook-a6-dots", "Тетрадь А6 72 листа в точку",
     {"attributes": {"format": "A6", "sheets": 72, "ruling": "точка"}}),
    ("mouse", "mouse-usb-4500", "Проводная мышь USB 4500 dpi",
     {"attributes": {"connection": "usb", "dpi": 4500}}),
    ("usb_drive", "flash-256", "Флешка USB 3.2 256 гигабайт",
     {"attributes": {"capacity_gb": 256, "usb_standard": "3.2"}}),
    ("paperclip", "clips-44-150", "Скрепки 44 мм по 150 штук",
     {"attributes": {"size_mm": 44, "pack_count": 150}}),
    ("ruler", "ruler-400", "Линейка для черчения 40 см",
     {"attributes": {"length_mm": 400}}),
    ("stapler", "stapler-75", "Степлер усиленный до 75 листов",
     {"attributes": {"capacity_sheets": 75}}),
    ("envelope", "envelope-c6-red", "Красный конверт C6",
     {"attributes": {"format": "C6", "color": "красный"}}),
    ("folder", "folder-a3-100", "Папка регистратор А3 корешок 100 мм",
     {"attributes": {"format": "A3", "spine_mm": 100}}),
    ("glue", "glue-pva-250", "Клей ПВА 250 миллилитров",
     {"attributes": {"glue_type": "pva", "volume_ml": 250}}),
    ("marker", "marker-board-green", "Маркер для доски зелёный",
     {"attributes": {"ink_color": "зелёный", "marker_type": "whiteboard"}}),
    # More challenging absent concepts; hard-profile metadata is intentionally
    # missing and the same-category existing articles remain retrievable.
    ("paper", "paper-receipt-roll", "Термобумага рулон для кассовых чеков", {}),
    ("pen", "pen-ink-refill", "Стержни сменные для шариковых ручек", {}),
    ("notebook", "notebook-stickers", "Самоклеящиеся закладки на страницах блокнота", {}),
    ("mouse", "mouse-deskpad", "Коврик под компьютерную мышь", {}),
    ("usb_drive", "usb-adapter", "Переходник USB Type C для флеш памяти", {}),
    ("paperclip", "clip-magnet-holder", "Магнитный контейнер для хранения скрепок", {}),
    ("marker", "marker-textile", "Текстильный маркер для нанесения рисунка на ткань", {}),
)

_FORMS = (
    "Требуется поставка: {phrase}.",
    "Подобрать отсутствующую в текущем каталоге позицию: {phrase}.",
)


def build_hardcases() -> dict[str, Any]:
    body = build_curriculum()
    assert isinstance(body["queries"], list)
    catalog = body["catalog"]
    assert isinstance(catalog, list)
    catalog_categories = {
        category: next(item["sale_unit"] for item in catalog if item["category"] == category)
        for category, *_ in _NO_MATCH
    }
    seen: set[str] = set()
    for category, intent, phrase, profile in _NO_MATCH:
        if intent in seen:
            raise ValueError("Duplicate synthetic no-match intent.")
        seen.add(intent)
        for index, form in enumerate(_FORMS):
            request = RequestedItem.model_validate({
                "description": form.format(phrase=phrase),
                "category": category, "quantity": 2, "unit": catalog_categories[category],
                **profile,
            })
            body["queries"].append({
                "query_id": f"nomatch-{intent}-{index + 1}",
                "query_group_id": f"absent-{intent}",
                "request": request.model_dump(mode="json"),
                "relevant_skus": [],
            })
    body["provenance"] = {
        "kind": "synthetic_example",
        "annotation_method": "local_hand_curated_positive_and_absent_sku_challenges",
        "synthetic_only": True,
        "owner_authorized_local_training": True,
        "independent_gold": False,
        "production_training": False,
        "negative_examples_hand_curated": True,
        "not_customer_no_match_evidence": True,
    }
    return body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create explicitly labelled synthetic no-match corpus.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    body = build_hardcases()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(body, f, sort_keys=True, ensure_ascii=False, indent=2)
        f.write("\n")
    data = load_dataset(args.output)
    split = split_groups(data.queries, seed=42)
    counts = {
        name: {
            "queries": len(part),
            "positive": sum(bool(row.relevant_skus) for row in part),
            "no_match": sum(not row.relevant_skus for row in part),
            "groups": len({row.query_group_id for row in part}),
        }
        for name, part in (
            ("train", split.train), ("validation", split.validation), ("test", split.test)
        )
    }
    if any(value["positive"] == 0 or value["no_match"] == 0 for value in counts.values()):
        raise ValueError("Each split requires positive and no-match queries.")
    print(json.dumps({
        "source_sha256": data.source_sha256,
        "catalog_skus": len(data.products),
        "query_count": len(data.queries),
        "no_match_total": sum(not row.relevant_skus for row in data.queries),
        "splits": counts,
        "provenance": data.provenance_kind,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
