"""Small verified unit registry; no density, packaging or implicit conversions."""
from decimal import Decimal


def normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


_UNITS: dict[str, tuple[str, Decimal]] = {}
for _dimension, _factor, _aliases in (
    ("length", "1", ("м", "m", "метр", "метра", "метров")),
    ("length", ".01", ("см", "cm", "сантиметр", "сантиметров")),
    ("length", ".001", ("мм", "mm", "миллиметр", "миллиметров")),
    ("mass", "1", ("кг", "kg", "килограмм", "килограмма", "килограммов")),
    ("mass", ".001", ("г", "g", "грамм", "грамма", "граммов")),
    ("volume", "1", ("л", "l", "литр", "литра", "литров")),
    ("volume", ".001", ("мл", "ml", "миллилитр", "миллилитров")),
    ("count", "1", ("шт", "шт.", "штука", "штуки", "штук", "piece", "pcs")),
):
    for _alias in _aliases:
        _UNITS[_alias] = (_dimension, Decimal(_factor))


def unit_info(unit: str) -> tuple[str, Decimal] | None:
    return _UNITS.get(normalized(unit))


def convert(value: Decimal, source: str | None, target: str | None) -> Decimal:
    if source is None or target is None:
        if source == target:
            return value
        raise ValueError("unit_missing")
    source_info, target_info = unit_info(source), unit_info(target)
    if source_info is None or target_info is None:
        if normalized(source) == normalized(target):
            return value
        raise ValueError("unit_mismatch")
    if source_info[0] != target_info[0]:
        raise ValueError("unit_mismatch")
    return value * source_info[1] / target_info[1]
