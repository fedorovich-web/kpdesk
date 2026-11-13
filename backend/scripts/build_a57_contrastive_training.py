from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.category_classifier import stratified_category_groups
from kpdesk.matching.cli import load_dataset
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.schema import CatalogProduct, RequestedItem

_SOURCE = PROJECT_ROOT / "ml/data/synthetic/ml/taxonomy-coverage-a46-20261023.json"
_SOURCE_SHA256 = "eae3fee20d7256ad7b95eb7f4d28e63f889aecdc361266e5243e40350dc7134a"

_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("folder", "Папка для документов на резинках из плотного картона", "Картонная папка с двумя резинками для подшивки бумаг"),
    ("folder", "Папка с металлическим пружинным зажимом", "Прижимная папка-скоросшиватель с пружиной для листов"),
    ("binder", "Кольцевой регистратор с четырьмя кольцами", "Архивная папка на четырех разъемных кольцах"),
    ("binder", "Папка-регистратор с рычажным механизмом", "Рычажная архивная папка для большого объема документов"),
    ("document_folder", "Портфельная папка для бумаг с защелкой", "Пластиковая документная папка с боковой защелкой"),
    ("document_folder", "Папка-конверт для документов на липучке", "Конвертная канцелярская папка с текстильной застежкой"),
    ("plastic_sleeve", "Файл-вкладыш повышенной плотности А4", "Усиленный прозрачный карман с перфорацией под подшивку"),
    ("plastic_sleeve", "Канцелярский файл с раскрытием сверху", "Вкладыш перфорированный прозрачный для вертикального хранения"),
    ("toner_cartridge", "Тонер-картридж для сетевого лазерного принтера", "Сменный порошковый блок для сетевой лазерной печати"),
    ("toner_cartridge", "Кассета с тонером увеличенного ресурса", "Лазерный картридж большой емкости для черно-белой печати"),
    ("ink_cartridge", "Сменный контейнер чернил для струйного принтера", "Чернильная кассета со струйным составом в принтер"),
    ("ink_cartridge", "Картридж с чернилами для фотопечати", "Фоточернила в сменном резервуаре струйного аппарата"),
    ("printer", "Офисный сетевой принтер с автодуплексом", "Печатающий аппарат для двухстороннего вывода документов"),
    ("printer", "Настольный принтер с сетевым портом", "Отдельное сетевое печатающее устройство без сканера"),
    ("copier", "Автономная копировальная машина с податчиком листов", "Копир для автоматического тиражирования оригиналов"),
    ("copier", "Ксерографический аппарат с двухсторонним копированием", "Машина для дуплексного снятия бумажных копий"),
    ("mfp", "МФУ с автоматической подачей оригиналов", "Комбинированный аппарат печати сканирования копирования с податчиком"),
    ("mfp", "Многофункциональный аппарат с функцией факса", "Принтер сканер копир и факс в одном корпусе"),
    ("pen", "Капиллярная пишущая ручка с тонким наконечником", "Ручка линер для тонких цветных записей"),
    ("pen", "Шариковая ручка с поворотным механизмом", "Пишущая ручка с выдвижением узла вращением корпуса"),
    ("pen_refill", "Запасной пишущий модуль роллера", "Сменная капсула с чернилами для роллерной ручки"),
    ("pen_refill", "Гелевый пишущий стержень в пластиковом корпусе", "Сменный гелевый элемент шариковой ручки"),
    ("staple", "Блок металлических скобок для настольного сшивателя", "Сменная кассета расходных скоб для степлера"),
    ("staple", "Оцинкованные скобы для канцелярского сшивания", "Коробка крепежных металлических скобок для бумаги"),
    ("stapler", "Компактный настольный сшиватель бумаг", "Маленький механический степлер для рабочего стола"),
    ("stapler", "Усиленный рычажный степлер для папки документов", "Ручной канцелярский сшиватель с длинной рукояткой"),
    ("pencil", "Графитный карандаш мягкости 2B", "Мягкий деревянный карандаш для штриховки"),
    ("pencil", "Жесткий графитный карандаш H для схем", "Твердый простой карандаш под технический чертеж"),
    ("mechanical_pencil", "Автоматический карандаш с боковой кнопкой подачи", "Карандаш с механической подачей грифеля боковым нажатием"),
    ("mechanical_pencil", "Цанговый чертежный карандаш с прорезиненным держателем", "Механический карандаш для черчения с резиновой зоной хвата"),
)


_ATTRIBUTE_OVERRIDES: dict[str, dict[str, str | int]] = {
    "a57-binder-3": {"rings": 4},
    "a57-pencil-27": {"lead_grade": "2B"},
    "a57-pencil-28": {"lead_grade": "H"},
}


def build_contrastive_training() -> dict[str, Any]:
    base = load_dataset(_SOURCE)
    if base.source_sha256 != _SOURCE_SHA256 or base.provenance_kind != "synthetic_example":
        raise ValueError("Base synthetic dataset integrity check failed.")
    split = stratified_category_groups(base.queries, seed=42)
    raw = json.loads(_SOURCE.read_text(encoding="utf-8"))
    train_ids = {record.query_id for record in split.train}
    train_groups = {record.query_group_id for record in split.train}
    held_out_groups = {record.query_group_id for record in (*split.validation, *split.test)}
    if train_groups & held_out_groups:
        raise ValueError("Base synthetic groups overlap.")
    records = [
        item for item in raw["queries"] if item["query_id"] in train_ids
    ]
    if len(records) != len(split.train):
        raise ValueError("Base train records missing.")
    catalog = list(raw["catalog"])
    products_by_category: dict[str, CatalogProduct] = {}
    for product in base.products.values():
        products_by_category.setdefault(product.category, product)
    texts = {normalize_text(item["request"]["description"]) for item in raw["queries"]}
    identifiers = set(base.products)
    for index, (category, name, paraphrase) in enumerate(_ADDITIONS, start=1):
        template = products_by_category[category]
        sku = f"a57-{category}-{index}"
        group = f"contrastive-a57-{category}-{index}"
        if sku in identifiers or group in train_groups or group in held_out_groups:
            raise ValueError("Duplicate synthetic product or intent identifier.")
        identifiers.add(sku)
        product = CatalogProduct.model_validate({
            **template.model_dump(mode="python"),
            "sku": sku,
            "name": name,
            "attributes": {**template.attributes, **_ATTRIBUTE_OVERRIDES.get(sku, {})},
        })
        catalog.append(product.model_dump(mode="json"))
        for wording, description in enumerate((name, paraphrase), start=1):
            normalized = normalize_text(description)
            if normalized in texts:
                raise ValueError("Training wording conflicts with a prior partition.")
            texts.add(normalized)
            request = RequestedItem.model_validate({
                "description": description,
                "category": category,
                "quantity": 1,
                "unit": product.sale_unit,
            })
            records.append({
                "query_id": f"{group}-{wording}",
                "query_group_id": group,
                "request": request.model_dump(mode="json"),
                "relevant_skus": [sku],
            })
    return {
        "provenance": {
            "kind": "synthetic_example",
            "annotation_method": "local_hand_authored_contrastive_train_only_a57",
            "source_sha256": base.source_sha256,
            "no_customer_training": True,
            "previous_validation_or_test_in_train": False,
        },
        "catalog": catalog,
        "queries": records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite synthetic training corpus.")
    document = build_contrastive_training()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(document, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
    data = load_dataset(args.output)
    print(json.dumps({
        "queries": len(data.queries),
        "intent_groups": len({item.query_group_id for item in data.queries}),
        "categories": len({item.request.category for item in data.queries}),
        "source_sha256": data.source_sha256,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
