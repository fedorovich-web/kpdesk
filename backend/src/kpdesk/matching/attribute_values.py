"""Exact, field-scoped value aliases shared by constraints and pair features.

Only equivalent spellings are folded. Unknown values retain exact comparison;
wireless is not assumed to mean radio, and permanent does not mean waterproof.
Stored source values, names, evidence and model weights remain untouched.
"""
from kpdesk.matching.normalize import normalize_text

_COLORS: dict[str, tuple[str, ...]] = {
    'black': ('black', 'черный', 'чёрный'), 'blue': ('blue', 'синий'),
    'red': ('red', 'красный'), 'green': ('green', 'зеленый', 'зелёный'),
    'yellow': ('yellow', 'желтый', 'жёлтый'), 'white': ('white', 'белый'),
    'cyan': ('cyan', 'голубой'), 'magenta': ('magenta', 'пурпурный'),
}
_TECHNOLOGY: dict[str, tuple[str, ...]] = {'laser': ('laser', 'лазерная', 'лазерный'), 'inkjet': ('inkjet', 'струйная', 'струйный')}
_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    'marker_type': {'whiteboard': ('whiteboard', 'для досок', 'для маркерной доски'),
                    'permanent': ('permanent', 'перманентный')},
    'ink_color': _COLORS, 'color': _COLORS, 'pen_ink_color': _COLORS,
    'pen_ink_type': {'ballpoint': ('ballpoint', 'шариковая', 'шариковый'),
                     'gel': ('gel', 'гелевая', 'гелевый')},
    'print_technology': _TECHNOLOGY, 'printer_technology': _TECHNOLOGY,
    'oem_status': {'compatible': ('compatible', 'совместимый'), 'original': ('original', 'оригинальный')},
}
_ALIASES = {field: {normalize_text(alias): canonical for canonical, aliases in groups.items() for alias in aliases}
            for field, groups in _GROUPS.items()}


def normalize_attribute_value(field: str, value: str | int) -> str:
    normalized = normalize_text(str(value))
    return _ALIASES.get(field, {}).get(normalized, normalized)
