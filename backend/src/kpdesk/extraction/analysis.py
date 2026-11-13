"""Explain supported request requirements using the authoritative matching rules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest
from kpdesk.matching.constraints import (
    CARTRIDGES,
    check_constraints,
    description_category_conflict,
)
from kpdesk.matching.profiles import REQUIRED_PRODUCT_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.calculate import QuoteCalculationError, calculate_line

LABELS = {
    "category": "Категория товара", "quantity": "Количество", "unit": "Единица измерения",
    "description": "Наименование", "brand": "Бренд", "paper_format": "Формат бумаги",
    "paper_gsm": "Плотность бумаги, г/м²", "pen_ink_color": "Цвет чернил",
    "pen_ink_type": "Тип чернил", "cartridge_identifier": "Код картриджа или модель принтера",
    "cartridge_code": "Код картриджа", "printer_model": "Модель принтера",
    "compatible_models": "Совместимые модели", "color": "Цвет", "oem_status": "OEM или совместимый",
    "format": "Формат", "sheets": "Количество листов", "connection": "Подключение",
    "dpi": "Разрешение, DPI", "capacity_sheets": "Вместимость, листов",
    "pencil_type": "Тип карандаша", "lead_grade": "Твёрдость грифеля",
    "lead_diameter_mm_x10": "Диаметр грифеля, десятые мм", "ink_color": "Цвет чернил",
    "marker_type": "Тип маркера", "eraser_type": "Тип ластика", "sharpener_type": "Тип точилки",
    "folder_kind": "Тип папки", "rings": "Количество колец", "thickness_microns": "Толщина, мкм",
    "labels_per_sheet": "Этикеток на листе", "width_mm": "Ширина, мм", "length_m": "Длина, м",
    "glue_type": "Тип клея", "correction_type": "Тип корректора", "staple_size": "Размер скоб",
    "length_mm": "Длина, мм", "spine_mm": "Ширина корешка, мм", "size_mm": "Размер, мм",
    "pack_count": "Количество в упаковке", "keys": "Количество клавиш", "capacity_gb": "Объём, ГБ",
    "usb_standard": "Стандарт USB", "print_technology": "Технология печати",
    "color_mode": "Режим цветности", "max_format": "Максимальный формат", "gsm": "Плотность, г/м²",
    "finish": "Поверхность", "product_kind": "Вид товара", "quantity_unit": "Количество и упаковка",
    "pen_type": "Тип ручки", "tip_mm_x10": "Толщина пишущего узла, десятые мм",
    "tip_shape": "Форма наконечника", "ruling": "Разлиновка", "material": "Материал",
    "printer_technology": "Технология принтера", "tape_type": "Тип ленты", "volume_ml": "Объём, мл",
    "holes": "Количество отверстий", "surface_material": "Материал поверхности",
    "height_mm": "Высота, мм", "refill_type": "Тип сменного блока",
    "connector_from": "Входной разъём", "connector_to": "Выходной разъём", "remover_type": "Тип антистеплера",
}

_CATEGORY_CONFLICT_NOTICE = 'Уточните категорию: описание указывает на другой тип товара.'
_CATEGORY_CONFLICT_QUESTION = 'Уточните наименование и назначение товара.'

_QUESTIONS = {
    'category': 'Какой именно товар требуется?',
    'quantity': 'Какое количество требуется?',
    'unit': 'Количество указано в штуках, пачках или коробках?',
    'description': 'Укажите наименование товара.',
    'brand': 'Укажите нужный бренд, если это обязательное условие.',
    'paper_format': 'Какой формат бумаги нужен?',
    'paper_gsm': 'Какая плотность бумаги нужна (г/м²)?',
    'pen_ink_color': 'Какой цвет чернил нужен?',
    'pen_ink_type': 'Какой тип чернил нужен?',
    'cartridge_identifier': 'Укажите код картриджа или модель принтера.',
    'cartridge_code': 'Какой код картриджа требуется?',
    'printer_model': 'Для какой модели принтера нужен картридж?',
    'compatible_models': 'С какими моделями оборудования должен быть совместим товар?',
    'color': 'Какой цвет нужен?',
    'oem_status': 'Нужен оригинальный или совместимый картридж?',
    'format': 'Какой формат нужен?',
    'sheets': 'Сколько листов должно быть?',
    'connection': 'Какой тип подключения нужен?',
    'dpi': 'Какое разрешение требуется (DPI)?',
    'capacity_sheets': 'На какое количество листов должен быть рассчитан товар?',
    'pencil_type': 'Какой тип карандаша нужен?',
    'lead_grade': 'Какая твёрдость грифеля нужна?',
    'lead_diameter_mm_x10': 'Какой диаметр грифеля нужен (в мм)?',
    'ink_color': 'Какой цвет чернил нужен?',
    'marker_type': 'Какой тип маркера нужен?',
    'eraser_type': 'Какой тип ластика нужен?',
    'sharpener_type': 'Какой тип точилки нужен?',
    'folder_kind': 'Какой тип папки нужен?',
    'rings': 'Сколько колец должно быть в папке?',
    'thickness_microns': 'Какая толщина нужна (в мкм)?',
    'labels_per_sheet': 'Сколько этикеток должно быть на листе?',
    'width_mm': 'Какая ширина нужна (в мм)?',
    'length_m': 'Какая длина нужна (в метрах)?',
    'glue_type': 'Какой тип клея нужен?',
    'correction_type': 'Какой тип корректора нужен?',
    'staple_size': 'Какой размер скоб нужен?',
    'length_mm': 'Какая длина нужна (в мм)?',
    'spine_mm': 'Какая ширина корешка нужна (в мм)?',
    'size_mm': 'Какой размер нужен (в мм)?',
    'pack_count': 'Сколько штук должно быть в упаковке?',
    'keys': 'Сколько клавиш должно быть на клавиатуре?',
    'capacity_gb': 'Какой объём памяти нужен (в ГБ)?',
    'usb_standard': 'Какой стандарт USB требуется?',
    'print_technology': 'Какая технология печати нужна?',
    'color_mode': 'Нужна цветная или чёрно-белая печать?',
    'max_format': 'Какой максимальный формат печати нужен?',
    'gsm': 'Какая плотность нужна (г/м²)?',
    'finish': 'Какой тип поверхности нужен?',
    'product_kind': 'Какой вид товара нужен?',
    'quantity_unit': 'Уточните количество и размер упаковки.',
    'pen_type': 'Какой тип ручки нужен?',
    'tip_mm_x10': 'Какая толщина пишущего узла нужна (в мм)?',
    'tip_shape': 'Какая форма наконечника нужна?',
    'ruling': 'Какая разлиновка нужна?',
    'material': 'Какой материал нужен?',
    'printer_technology': 'Для какой технологии печати нужен товар?',
    'tape_type': 'Какой тип ленты нужен?',
    'volume_ml': 'Какой объём нужен (в мл)?',
    'holes': 'Сколько отверстий должно быть?',
    'surface_material': 'Какой материал поверхности нужен?',
    'height_mm': 'Какая высота нужна (в мм)?',
    'refill_type': 'Какой тип сменного блока нужен?',
    'connector_from': 'Какой входной разъём нужен?',
    'connector_to': 'Какой выходной разъём нужен?',
    'remover_type': 'Какой тип скобоудалителя нужен?',
}


def _quantity_text(quantity: int | None, unit: str | None) -> str | None:
    if quantity is None or not unit:
        return None
    forms = {
        'штука': ('штука', 'штуки', 'штук'),
        'пачка': ('пачка', 'пачки', 'пачек'),
        'коробка': ('коробка', 'коробки', 'коробок'),
    }.get(unit)
    if forms is None:
        return f'{quantity} {unit}'
    ending = 2 if 11 <= quantity % 100 <= 14 else (
        0 if quantity % 10 == 1 else 1 if quantity % 10 in (2, 3, 4) else 2
    )
    return f'{quantity} {forms[ending]}'


class RequirementCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    field: str
    label: str
    requested: str | None
    actual: str | None = None
    status: Literal["provided", "matched", "mismatch", "missing_request", "unknown_product", "needs_review"]
    note: str | None = None


class RequestLineAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)
    line_number: int
    source_quote: str
    description: str
    missing_fields: tuple[str, ...]
    questions: tuple[str, ...]


class RequestAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)
    lines: tuple[RequestLineAnalysis, ...]
    ready_for_matching: bool
    questions_text: str
    scope_notice: str = (
        "Проверены обязательные сведения для подбора в поддерживаемых категориях. "
        "Сверьте их с заявкой клиента. Наличие, сроки и дополнительные условия "
        "этой проверкой не подтверждаются."
    )


def _value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or None
    return str(value).strip() or None


def _requirements(line: ExtractedLine | RequestedItem) -> dict[str, str | None]:
    result = {"category": _value(line.category)}
    category = line.category
    if category is None:
        return result
    fields: tuple[str, ...]
    if category == "paper":
        fields = ("paper_format", "paper_gsm")
    elif category == "pen":
        fields = ("pen_ink_color", "pen_ink_type")
    else:
        fields = ()
    result.update((name, _value(getattr(line, name))) for name in fields)
    required = REQUIRED_PRODUCT_ATTRIBUTES[category]
    if category in CARTRIDGES:
        required = frozenset({"color", "oem_status"})
        if not (line.attributes.get("printer_model") or line.attributes.get("cartridge_code")):
            result["cartridge_identifier"] = None
    for name in sorted(required | line.attributes.keys()):
        value = _value(line.attributes.get(name))
        if name in required or value is not None:
            result[name] = value
    brand = _value(line.brand)
    if brand is not None:
        result["brand"] = brand
    return result


def analyze_request(request: ExtractedRequest) -> RequestAnalysis:
    lines = []
    for index, line in enumerate(request.lines, 1):
        fields = {"quantity": _value(line.quantity), "unit": _value(line.unit), **_requirements(line)}
        if not line.description.strip():
            fields["description"] = None
        missing = tuple(name for name, value in fields.items() if value is None)
        questions = tuple(_QUESTIONS.get(name, f'Уточните характеристику «{LABELS[name]}».')
                          for name in missing)
        if description_category_conflict(line.category, line.description):
            questions += (_CATEGORY_CONFLICT_NOTICE,)
        lines.append(RequestLineAnalysis(
            line_number=index, source_quote=line.source_quote, description=line.description,
            missing_fields=missing, questions=questions,
        ))
    text = "\n\n".join(
        f"Позиция {line.line_number} — {line.description}\n" + "\n".join(
            _CATEGORY_CONFLICT_QUESTION if question == _CATEGORY_CONFLICT_NOTICE else question
            for question in line.questions
        )
        for line in lines if line.questions
    )
    return RequestAnalysis(lines=tuple(lines), questions_text=text,
                           ready_for_matching=bool(lines) and all(not line.questions for line in lines))


def product_requirements(
    request: RequestedItem, product: CatalogProduct, approved_sale_quantity: int | None = None,
) -> tuple[RequirementCheck, ...]:
    decision = check_constraints(request, product)
    checks = []
    if 'category_description_mismatch' in decision.reason_codes:
        checks.append(RequirementCheck(
            field='description', label=LABELS['description'], requested=request.description,
            actual=product.name, status='mismatch', note=_CATEGORY_CONFLICT_NOTICE,
        ))
    for name, wanted in _requirements(request).items():
        actual = (_value(getattr(product, name)) if name in {
            "category", "brand", "paper_format", "paper_gsm", "pen_ink_color", "pen_ink_type",
        } else _value(product.attributes.get("compatible_models" if name == "printer_model" else name)))
        status: Literal["matched", "mismatch", "missing_request", "unknown_product", "needs_review"]
        if wanted is None:
            status = "missing_request"
        elif actual is None:
            status = "unknown_product"
        elif name == "compatible_models":
            # Matching interprets this as product evidence, not an arbitrary request list.
            status = "needs_review"
        elif any(code in decision.reason_codes for code in (f"{name}_mismatch", f"{name}_incompatible")):
            status = "mismatch"
        else:
            status = "matched"
        checks.append(RequirementCheck(field=name, label=LABELS[name], requested=wanted,
                                       actual=actual, status=status))
    count = _quantity_text(request.quantity, request.unit)
    actual_count = None
    note = None
    quantity_status: Literal["matched", "missing_request", "needs_review"] = "missing_request"
    if count is not None:
        quantity_status = "needs_review"
        try:
            result = calculate_line(request, product, approved_sale_quantity)
        except QuoteCalculationError:
            note = "Проверьте характеристики товара и количество перед расчётом упаковки."
        else:
            if result.status == "ready":
                quantity_status = "matched"
                actual_count = _quantity_text(result.sale_quantity, result.sale_unit)
                if result.overage_pieces:
                    note = f"Округление до целых упаковок подтверждено. Дополнительно к заявке: {result.overage_pieces} шт."
            elif "fractional_package" in result.reason_codes:
                note = f"Подтвердите округление до целых упаковок: {_quantity_text(result.suggested_sale_quantity, product.sale_unit)}."
            else:
                note = "Уточните соотношение единиц заявки и упаковки товара; автоматический пересчёт невозможен."
    checks.append(RequirementCheck(field="quantity_unit", label=LABELS["quantity_unit"],
                                   requested=count, actual=actual_count, status=quantity_status, note=note))
    return tuple(checks)
