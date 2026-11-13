"""Generate reproducible, explicitly synthetic supplier prices. Not evaluation gold."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES

SEED = 20261009
GENERATOR_VERSION = "synthetic-supplier-v1"
ROOTS_AND_VARIANTS: tuple[tuple[str, str, str, str], ...] = (
    ("01", "paper", "Бумага офисная|Бумага цветная|Фотобумага|Бумага для плоттера",
     "A4 70 г/м2|A4 80 г/м2|A4 90 г/м2|A3 80 г/м2|A3 120 г/м2|A4 160 г/м2|A4 200 г/м2|A4 250 г/м2|A3 200 г/м2|A3 250 г/м2"),
    ("02", "pen", "Ручка шариковая|Ручка гелевая|Маркер перманентный|Карандаш механический",
     "синяя 0.5 мм|чёрная 0.5 мм|красная 0.5 мм|синяя 0.7 мм|чёрная 0.7 мм|зелёная 0.7 мм|синяя 1.0 мм|чёрная 1.0 мм|красная 1.0 мм|зелёная 1.0 мм"),
    ("03", "document_folder", "Папка-регистратор|Папка с зажимом|Скоросшиватель|Файл-вкладыш",
     "A4 30 мм|A4 50 мм|A4 75 мм|A5 30 мм|A5 50 мм|A5 75 мм|A4 пластик 40 мм|A4 картон 40 мм|A4 пластик 60 мм|A4 картон 60 мм"),
    ("04", "notebook", "Тетрадь|Блокнот|Этикетка|Стикеры",
     "A5 24 листа|A5 48 листов|A5 96 листов|A4 24 листа|A4 48 листов|A4 96 листов|A6 32 листа|A6 64 листа|A5 80 листов|A4 80 листов"),
    ("05", "printer", "Принтер лазерный|МФУ струйное|Принтер цветной|Копировальный аппарат",
     "A4 моно|A4 цвет|A3 моно|A3 цвет|A4 Wi-Fi|A4 USB|A3 Ethernet|A4 duplex|A3 duplex|A4 compact"),
    ("06", "toner_cartridge", "Тонер-картридж|Фотобарабан|Контейнер тонера|Блок проявки",
     "TK-100 black|TK-100 cyan|TK-200 black|TK-200 magenta|TK-300 yellow|TK-300 black|TK-400 cyan|TK-400 black|TK-500 yellow|TK-500 magenta"),
    ("07", "ink_cartridge", "Картридж струйный|Чернила пигментные|Чернила водные|Печатающая головка",
     "IC-10 black|IC-10 cyan|IC-20 magenta|IC-20 yellow|IC-30 black|IC-30 cyan|IC-40 magenta|IC-40 yellow|IC-50 black|IC-50 cyan"),
    ("08", "mouse", "Мышь оптическая|Клавиатура мембранная|Мышь игровая|Клавиатура механическая",
     "USB 800 DPI|USB 1200 DPI|USB 1600 DPI|USB 2400 DPI|Bluetooth 800 DPI|Bluetooth 1200 DPI|Bluetooth 1600 DPI|Bluetooth 2400 DPI|2.4GHz 1600 DPI|2.4GHz 2400 DPI"),
    ("09", "network_switch", "Коммутатор|Маршрутизатор|Точка доступа|Медиаконвертер",
     "4 порта 100Mb|5 портов 1Gb|8 портов 1Gb|16 портов 1Gb|24 порта 1Gb|48 портов 1Gb|8 портов PoE|16 портов PoE|24 порта PoE|10Gb SFP+"),
    ("10", "circuit_breaker", "Автоматический выключатель|Кабель силовой|Розетка|Контактор",
     "1P 10A|1P 16A|1P 25A|2P 10A|2P 16A|2P 25A|3P 10A|3P 16A|3P 25A|3P 32A"),
    ("11", "led_lamp", "Светильник LED|Лампа LED|Прожектор|LED-панель",
     "3000K 6W|4000K 6W|6500K 6W|3000K 12W|4000K 12W|6500K 12W|3000K 24W|4000K 24W|6500K 24W|4000K 36W"),
    ("12", "power_tool", "Шуруповёрт|Дрель|Перфоратор|Угловая шлифмашина",
     "12V 1.5Ah|12V 2.0Ah|18V 1.5Ah|18V 2.0Ah|18V 4.0Ah|220V 500W|220V 650W|220V 750W|220V 900W|220V 1100W"),
    ("13", "fastener", "Болт шестигранный|Гайка|Шайба|Саморез",
     "M4x10|M4x20|M5x15|M5x30|M6x20|M6x40|M8x20|M8x50|M10x30|M10x60"),
    ("14", "insulation", "Минеральная вата|Гипсокартон|Плита OSB|Сухая смесь",
     "10 мм|12 мм|15 мм|20 мм|25 мм|30 мм|40 мм|50 мм|75 мм|100 мм"),
    ("15", "pipe_fitting", "Муфта|Тройник|Колено|Шаровой кран",
     "1/2 дюйма латунь|3/4 дюйма латунь|1 дюйм латунь|1/2 дюйма сталь|3/4 дюйма сталь|1 дюйм сталь|16 мм PPR|20 мм PPR|25 мм PPR|32 мм PPR"),
    ("16", "radiator", "Радиатор|Термоголовка|Вентилятор|Клапан",
     "3 секции|4 секции|5 секций|6 секций|7 секций|8 секций|9 секций|10 секций|12 секций|14 секций"),
    ("17", "cleaning_agent", "Средство для пола|Чистящее средство|Ведро|Швабра",
     "0.5 л|0.75 л|1 л|1.5 л|2 л|3 л|5 л|10 л|20 л|25 л"),
    ("18", "safety_gloves", "Перчатки защитные|Респиратор|Каска|Защитные очки",
     "размер 7|размер 8|размер 9|размер 10|размер 11|размер S|размер M|размер L|размер XL|размер XXL"),
    ("19", "brake_pad", "Колодки тормозные|Фильтр масляный|Свеча зажигания|Ремень привода",
     "серия X 2015|серия X 2020|серия Y 2015|серия Y 2020|серия Z 2016|серия Z 2021|серия Q 2017|серия Q 2022|серия R 2018|серия R 2023"),
    ("20", "industrial_bearing", "Подшипник шариковый|Подшипник роликовый|Втулка|Муфта соединительная",
     "6000 10x26x8|6001 12x28x8|6002 15x32x9|6003 17x35x10|6004 20x42x12|6200 10x30x9|6201 12x32x10|6202 15x35x11|6203 17x40x12|6204 20x47x14"),
)

# Column order deliberately differs from the strict canonical import schema.
SCHEMAS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("Код", "sku"), ("Товар", "name"), ("Цена, руб.", "price"), ("Упаковка", "unit"), ("Исполнение", "variant")),
    (("Наименование", "name"), ("Артикул", "sku"), ("Вариант", "variant"), ("Стоимость", "price"), ("ЕИ", "unit")),
    (("Позиция", "name"), ("Размер", "variant"), ("Цена с НДС", "price"), ("Код поставщика", "sku"), ("Ед. изм.", "unit")),
    (("SKU", "sku"), ("Характеристика", "variant"), ("Описание", "name"), ("Штук в упаковке", "pack"), ("Цена", "price")),
    (("Цена RUB", "price"), ("Номенклатура", "name"), ("Код товара", "sku"), ("Фасовка", "unit"), ("Техническое исполнение", "variant")),
)


@dataclass(frozen=True, slots=True)
class SupplierCase:
    case_id: str
    category: str
    roots: tuple[str, ...]
    variants: tuple[str, ...]
    format: str
    encoding: str
    separator: str
    header_row: int
    sheet: str
    columns: tuple[tuple[str, str], ...]


def _case(record: tuple[str, str, str, str]) -> SupplierCase:
    case_id, category, roots, variants = record
    number = int(case_id)
    fmt = "xlsx" if number in {3, 5, 7, 9, 11, 13, 15, 16, 18, 20} else "csv"
    encoding = ("cp1251" if number in {4, 10, 14, 19}
                else "utf-8-sig" if number in {1, 8, 12} else "utf-8")
    separator = ("\t" if number in {2, 12, 17}
                 else "," if number in {6, 8, 10, 14} else ";")
    return SupplierCase(
        case_id=case_id, category=category,
        roots=tuple(roots.split("|")), variants=tuple(variants.split("|")),
        format=fmt, encoding=encoding, separator=separator,
        header_row=(3 if number in {3, 9, 15, 20} else 2 if number in {5, 11, 16} else 1),
        sheet=("Прайс" if number in {3, 9, 15, 20} else "Остатки"),
        columns=SCHEMAS[(number - 1) % len(SCHEMAS)],
    )


CASES: tuple[SupplierCase, ...] = tuple(map(_case, ROOTS_AND_VARIANTS))


def _records(case: SupplierCase) -> list[dict[str, str]]:
    records = []
    for i, root in enumerate(case.roots):
        for j, variant in enumerate(case.variants):
            records.append({
                "sku": f"SIM-{case.case_id}-{i + 1:02}-{j + 1:02}",
                "name": f"{root} {variant}",
                "variant": variant,
                "price": f"{int(case.case_id) * 43 + i * 19 + j * 7 + 15}.90",
                "unit": ("уп." if j % 3 == 0 else "шт."),
                "pack": str(1 if j % 3 else 10),
            })
    return records


def _xlsx_bytes(case: SupplierCase, rows: list[dict[str, str]]) -> bytes:
    book = Workbook()
    book.properties.created = datetime(2026, 10, 9, tzinfo=UTC)
    book.properties.modified = datetime(2026, 10, 9, tzinfo=UTC)
    first = book.active
    assert first is not None
    if int(case.case_id) in {5, 7, 9, 11, 16, 20}:
        first.title = "Описание"
        first.append(["СИНТЕТИЧЕСКИЕ ДАННЫЕ ДЛЯ ТЕСТИРОВАНИЯ"])
        sheet = book.create_sheet(case.sheet)
    else:
        first.title = case.sheet
        sheet = first
    for _ in range(case.header_row - 1):
        sheet.append(["СИНТЕТИЧЕСКИЙ ПРАЙС KPDesk"])
    sheet.append([label for label, _ in case.columns])
    for record in rows:
        sheet.append([record[key] for _, key in case.columns])
    binary = io.BytesIO()
    book.save(binary)
    book.close()
    normalized = io.BytesIO()
    with ZipFile(io.BytesIO(binary.getvalue())) as source, ZipFile(
        normalized, "w", ZIP_DEFLATED, compresslevel=6,
    ) as target:
        for member in source.infolist():
            info = ZipInfo(member.filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = member.external_attr
            payload = source.read(member.filename)
            if member.filename == "docProps/core.xml":
                # openpyxl overwrites modified on save, regardless of our metadata setting.
                payload, replacements = re.subn(
                    rb"(<dcterms:modified\b[^>]*>)[^<]*(</dcterms:modified>)",
                    rb"\g<1>2026-10-09T00:00:00Z\g<2>", payload,
                )
                if replacements != 1:
                    raise ValueError("Unable to freeze synthetic workbook timestamp")
            target.writestr(info, payload, compress_type=ZIP_DEFLATED)
    return normalized.getvalue()


def _csv_bytes(case: SupplierCase, rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=case.separator, lineterminator="\n")
    for _ in range(case.header_row - 1):
        writer.writerow(["СИНТЕТИЧЕСКИЙ ПРАЙС KPDesk"])
    writer.writerow([label for label, _ in case.columns])
    writer.writerows([[record[key] for _, key in case.columns] for record in rows])
    return stream.getvalue().encode(case.encoding)


class SupplierManifestEntry(TypedDict):
    case_id: str
    category: str
    file: str
    source_format: str
    encoding: str
    delimiter: str | None
    sheet: str | None
    header_row: int
    source_columns: list[str]
    row_count: int
    sku_prefix: str
    sha256: str
    taxonomy_supported: bool
    canonical_importable: bool
    provenance: str


class SupplierManifest(TypedDict):
    schema_version: int
    generator_version: str
    seed: int
    provenance: str
    license: str
    gold_review_status: str
    real_supplier_data: bool
    cases: list[SupplierManifestEntry]


def build_corpus(directory: Path) -> SupplierManifest:
    """Write deterministic fixtures and manifest; no DB, network or real supplier data."""
    directory.mkdir(parents=True, exist_ok=True)
    cases: list[SupplierManifestEntry] = []
    for case in CASES:
        rows = _records(case)
        assert len(rows) >= 40
        file = f"synthetic_{case.case_id}_{case.category}.{case.format}"
        content = _xlsx_bytes(case, rows) if case.format == "xlsx" else _csv_bytes(case, rows)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Synthetic fixture exceeds import limit")
        (directory / file).write_bytes(content)
        cases.append({
            "case_id": case.case_id,
            "category": case.category,
            "file": file,
            "source_format": case.format,
            "encoding": case.encoding if case.format == "csv" else "xlsx",
            "delimiter": case.separator if case.format == "csv" else None,
            "sheet": case.sheet if case.format == "xlsx" else None,
            "header_row": case.header_row,
            "source_columns": [name for name, _ in case.columns],
            "row_count": len(rows),
            "sku_prefix": f"SIM-{case.case_id}-",
            "sha256": hashlib.sha256(content).hexdigest(),
            "taxonomy_supported": case.category in CATEGORY_ATTRIBUTES,
            "canonical_importable": False,
            "provenance": "synthetic",
        })
    manifest: SupplierManifest = {
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "seed": SEED,
        "provenance": "synthetic",
        "license": "generated_internal_test_only",
        "gold_review_status": "not_human_reviewed",
        "real_supplier_data": False,
        "cases": cases,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[2] / "ml" / "data" / "synthetic" / "prices",
    )
    args = parser.parse_args()
    manifest = build_corpus(args.output)
    print(f"Generated {len(manifest['cases'])} synthetic supplier cases at {args.output}")
