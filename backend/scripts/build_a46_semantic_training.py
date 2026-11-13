from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.cli import load_dataset
from kpdesk.matching.schema import RequestedItem
from kpdesk.matching.training import split_groups
from scripts.build_synthetic_ml_hardcases import build_hardcases

_NEGATIVE_INTENTS: tuple[tuple[str, str, str, str], ...] = (
    ("paper", "carbon-copy", "Копировальная бумага для ручного переноса текста", "Листы копирки для дублирования надписей под шариковой ручкой"),
    ("paper", "continuous-feed", "Перфорированная бумага для матричного принтера", "Бумага с боковыми отверстиями для непрерывной печати"),
    ("pen", "erasable", "Термостираемая шариковая ручка с ластиком", "Ручка с чернилами стираемыми трением"),
    ("pen", "calligraphy", "Перьевая ручка для каллиграфии со сменным пером", "Каллиграфическое перо для красивого письма тушью"),
    ("notebook", "waterproof", "Влагостойкий блокнот для полевых записей под дождём", "Блокнот с водонепроницаемыми страницами для геодезиста"),
    ("notebook", "music-score", "Нотная тетрадь с пятью линейками на странице", "Тетрадка с готовым нотным станом для музыканта"),
    ("mouse", "trackball", "Трекбол с неподвижным корпусом и вращаемым шаром", "Устройство управления курсором с шаром под большим пальцем"),
    ("mouse", "presenter", "Воздушная мышь для управления курсором жестами", "Пульт-мышка с гироскопом для презентаций"),
    ("usb_drive", "rugged", "Ударопрочная флешка в герметичном металлическом корпусе", "Влагозащищённый USB накопитель для суровых условий эксплуатации"),
    ("usb_drive", "write-once", "Накопитель с аппаратным режимом однократной записи", "USB носитель с защитой от последующего изменения файлов"),
    ("paperclip", "butterfly", "Скрепка-бабочка для крупных бумажных пачек", "Широкий металлический зажим для толстого пакета листов"),
    ("paperclip", "bookmarks", "Декоративная скрепка-закладка с фигурным наконечником", "Фигурные закладки в виде скрепок для блокнота"),
    ("ruler", "folding", "Складной деревянный метр с шарнирными секциями", "Плотницкая раскладная линейка для замеров"),
    ("ruler", "laser", "Лазерная линейка с измерением расстояния лучом", "Цифровой дальномер с лазерной разметкой"),
    ("stapler", "electric", "Электростеплер с автоматическим сшиванием бумаг", "Канцелярский сшиватель с моторизованной подачей скоб"),
    ("stapler", "long-arm", "Брошюровочный степлер с удлинённым плечом", "Сшиватель для прошивки листов по центру разворота"),
    ("envelope", "tamper-evident", "Почтовый конверт с индикатором вскрытия", "Конверт пломбируемый с защитной лентой от подмены"),
    ("envelope", "window", "Конверт с прозрачным адресным окошком", "Письменный конверт с пластиковым окном для реквизитов"),
    ("folder", "zipper", "Папка на молнии с внутренними отделениями", "Портфельная папка-органайзер с застёжкой и карманами"),
    ("folder", "suspension", "Подвесная картотечная папка с крючками", "Подвесной файловый держатель для выдвижного шкафа"),
    ("glue", "epoxy", "Двухкомпонентный эпоксидный клей со смолой и отвердителем", "Эпоксидный состав для склеивания металла из двух частей"),
    ("glue", "hot-melt", "Термоклеевые стержни для клеевого пистолета", "Прутки горячего клея для термопистолета"),
    ("marker", "uv-visible", "Маркер с чернилами видимыми только под ультрафиолетом", "Невидимый фломастер для секретной маркировки УФ-лампой"),
    ("marker", "paint", "Маркер с лакокрасочной краской для металла", "Красящий маркер с эмалью для деталей оборудования"),
)


def build_semantic_training() -> dict[str, Any]:
    body = build_hardcases()
    catalog = body["catalog"]
    queries = body["queries"]
    if not isinstance(catalog, list) or not isinstance(queries, list):
        raise TypeError("Invalid synthetic base curriculum.")
    units: dict[str, str] = {}
    for product in catalog:
        category, unit = product["category"], product["sale_unit"]
        existing = units.setdefault(category, unit)
        if existing != unit:
            raise ValueError("Ambiguous catalog sale unit for synthetic training.")
    identities: set[str] = set()
    for category, name, description, paraphrase in _NEGATIVE_INTENTS:
        identity = f"semantic-{category}-{name}"
        if identity in identities or category not in units:
            raise ValueError("Ambiguous semantic negative training intent.")
        identities.add(identity)
        for index, phrase in enumerate((description, paraphrase), start=1):
            request = RequestedItem.model_validate({
                "description": phrase, "category": category,
                "quantity": 1, "unit": units[category],
            })
            queries.append({
                "query_id": f"{identity}-{index}",
                "query_group_id": identity,
                "request": request.model_dump(mode="json"),
                "relevant_skus": [],
            })
    body["provenance"] = {
        "kind": "synthetic_example",
        "annotation_method": "local_hand_authored_semantic_negatives_a46",
        "synthetic_only": True,
        "independent_gold": False,
        "production_training": False,
        "source_customer_gold": False,
        "external_holdout_used_for_training": False,
    }
    return body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite synthetic training data.")
    document = build_semantic_training()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(document, output, sort_keys=True, ensure_ascii=False, indent=2)
        output.write("\n")
    dataset = load_dataset(args.output)
    split = split_groups(dataset.queries, seed=42)
    print(json.dumps({
        "queries": len(dataset.queries),
        "groups": len({record.query_group_id for record in dataset.queries}),
        "no_match": sum(not record.relevant_skus for record in dataset.queries),
        "train": len(split.train), "validation": len(split.validation),
        "test": len(split.test), "sha256": dataset.source_sha256,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
