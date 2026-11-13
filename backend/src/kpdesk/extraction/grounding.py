"""Conservative quantity and paper evidence checks for untrusted extracted lines.

This is not natural-language understanding. Reject numeric values without
recognizable source evidence; never infer quantities or commercial units.
"""
from __future__ import annotations

import re
from collections import Counter

from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.matching.constraints import description_category_conflict
from kpdesk.quotes.instructions import instruction_evidence

# Explicit commercial selling units, not product technical specifications.
_UNIT = (
    r"шт(?:\.|ук(?:а|и|у|ой|ам|ами|ах|ов)?)?"
    r"|пач(?:ка|ки|ек|ке|ку|кой|ках|ками)"
    r"|уп(?:\.|ак(?:овка|овки|овок|овке|овку|овкой|овками)?)"
    r"|короб(?:ка|ки|ок|ке|ку|кой|ками)"
)
_UNIT_AFTER_COUNT = re.compile(rf"^\s*(?P<unit>{_UNIT})(?![\w])", re.IGNORECASE)
_WORD_COUNTS = {
    "один": 1, "одна": 1, "одно": 1, "одну": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20,
}
_COUNT_TOKEN = re.compile(
    r"(?<!\w)(?:\d{1,6}|" + "|".join(_WORD_COUNTS) + r")(?!\w)",
    re.IGNORECASE,
)
_PREVIOUS_WORD = re.compile(r"([а-яё]+)\s*$", re.IGNORECASE)
_AMBIGUOUS_PREFIX = re.compile(r"(?:[\d+\-−/]|\d[.,]|\d\s*[–—])\s*$")
_AMBIGUOUS_SUFFIX = re.compile(
    r"^\s*(?:[.,/\-−–—]\s*\d|\d|тыс\b|тысяч|млн\b|миллион|до\b|или\b)",
    re.IGNORECASE,
)
_QUANTITY_QUALIFIERS = frozenset({
    "от", "до", "около", "примерно", "порядка", "более", "менее",
    "минимум", "максимум", "свыше", "минус", "плюс", "по",
})
_COMPOUND_NUMERALS = _WORD_COUNTS.keys() | {
    "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
    "восемьдесят", "девяносто", "сто", "двести", "триста", "четыреста",
    "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот",
    "тысяча", "тысячи", "тысяч", "миллион", "миллиона", "миллионов",
}
_TECHNICAL_UNIT = re.compile(
    r"^\s*(?:г(?:/м[²2])?|гр|мм|см|мл|л|dpi|г/м2|вт|w|%)(?!\w)",
    re.IGNORECASE,
)
_DENSITY = re.compile(
    r"(?<!\w)(?P<value>[0-9]{1,4})\s*(?:"
    r"г(?:р\.?)?\s*/\s*м\s*(?:\^?\s*2|²)"
    r"|g\s*/\s*m\s*(?:\^?\s*2|²)|gsm)(?!\w)", re.IGNORECASE,
)
_LABELED_DENSITY = re.compile(
    r"\bплотност[ьи]\s*[:=]?\s*(?P<value>[0-9]{1,4})(?![\w.,])", re.IGNORECASE,
)
_DENSITY_AMBIGUOUS_PREFIX = re.compile(
    r"(?:[+\-−/]|(?<!\w)[0-9]+[.,]|(?<!\w)[0-9]+\s*[–—])\s*$",
)
_FORMAT_ALIASES = str.maketrans({'а': 'a', 'в': 'b'})


def _density_evidence(fragment: str, prefix: str, suffix: str) -> set[int]:
    values: set[int] = set()
    context = prefix + fragment + suffix
    quote_start, quote_end = len(prefix), len(prefix) + len(fragment)
    for pattern in (_DENSITY, _LABELED_DENSITY):
        for match in pattern.finditer(context):
            if match.start() < quote_start or match.end() > quote_end:
                continue
            before = context[:match.start('value')]
            after = context[match.end('value'):]
            previous = _PREVIOUS_WORD.search(before)
            if (_DENSITY_AMBIGUOUS_PREFIX.search(before) or _AMBIGUOUS_SUFFIX.match(after)
                or previous and previous.group(1) in _QUANTITY_QUALIFIERS):
                continue
            values.add(int(match.group('value')))
    return values


def _sale_unit(value: str) -> str:
    if value.lower().startswith(("шт",)):
        return "штука"
    if value.lower().startswith(("пач", "уп")):
        return "пачка"
    return "коробка"


def _quantity_evidence(
    fragment: str, prefix: str, suffix: str,
    model_identifiers: tuple[str, ...] = (),
) -> list[tuple[int, str | None]]:
    """Recognize only complete integer counts, with their adjacent sale units.

    A fragment of a decimal, range, multiplier or compound word count is not
    evidence. Unsupported forms require review instead of partial arithmetic.
    """
    evidence: list[tuple[int, str | None]] = []
    for match in _COUNT_TOKEN.finditer(fragment):
        before = prefix + fragment[:match.start()]
        after = fragment[match.end():] + suffix
        previous = _PREVIOUS_WORD.search(before)
        model_separator = any(
            re.search(r'[a-z]', model) and re.search(r'[0-9]$', model)
            and re.search(r'(?<!\w)' + re.escape(model) + r'\s*[–—]\s*$', fragment[:match.start()])
            for model in model_identifiers
        )
        if (
            (_AMBIGUOUS_PREFIX.search(before) and not model_separator) or _AMBIGUOUS_SUFFIX.match(after)
            or (previous and previous.group(1) in (_COMPOUND_NUMERALS | _QUANTITY_QUALIFIERS))
            or _TECHNICAL_UNIT.match(after)
        ):
            continue
        unit_match = _UNIT_AFTER_COUNT.match(after)
        token = match.group()
        evidence.append((
            int(token) if token.isdigit() else _WORD_COUNTS[token],
            _sale_unit(unit_match.group("unit")) if unit_match else None,
        ))
    return evidence


def validate_extraction_grounding(source: str, result: ExtractedRequest) -> tuple[str, ...]:
    """Return safe machine-readable reasons; never include customer text."""
    violations: list[str] = []
    for quote, uses in Counter(line.source_quote for line in result.lines).items():
        if uses > 1 and uses > source.count(quote):
            violations.append('source_fragment_reused')
    if any(quote not in source for quote in result.instruction_quotes):
        violations.append("instruction_source_fragment_missing")
    if any(
        clause not in "\n".join(result.instruction_quotes)
        for clause in instruction_evidence(source)
    ):
        violations.append("commercial_instruction_omitted")
    for index, line in enumerate(result.lines):
        if description_category_conflict(line.category, line.source_quote):
            violations.append('category_source_conflict')
        start = source.find(line.source_quote)
        if start < 0:
            violations.append(f"line_{index}:source_fragment_missing")
            continue
        fragment = line.source_quote.casefold()
        prefix = source[:start].casefold()
        suffix = source[start + len(line.source_quote):].casefold()
        if line.paper_gsm is not None and line.paper_gsm not in _density_evidence(fragment, prefix, suffix):
            violations.append(f"line_{index}:paper_gsm_unverified")
        if line.paper_format is not None:
            value = line.paper_format.casefold().translate(_FORMAT_ALIASES)
            pattern = re.escape(value)
            if re.fullmatch(r'[ab][0-9]{1,2}', value):
                pattern = value[0] + r'\s*' + value[1:]
            context = (prefix + fragment + suffix).translate(_FORMAT_ALIASES)
            matches = re.finditer(r'(?<!\w)' + pattern + r'(?!\w)', context)
            if not value.strip() or not any(
                match.start() >= len(prefix) and match.end() <= len(prefix) + len(fragment)
                for match in matches
            ):
                violations.append(f"line_{index}:paper_format_unverified")
        if line.quantity is None:
            continue
        # A source-backed alphanumeric model ending at the dash is an identifier,
        # not the first endpoint of a quantity range. It must be inside this quote.
        # Numeric-only values, borrowed context and unrelated technical fields
        # cannot authorize this exception.
        identifiers: list[str] = []
        if line.category in ('toner_cartridge', 'ink_cartridge'):
            for name in ('printer_model', 'compatible_models', 'cartridge_code'):
                raw = line.attributes.get(name)
                if isinstance(raw, str):
                    identifiers.append(raw.casefold())
                elif isinstance(raw, list):
                    identifiers.extend(value.casefold() for value in raw)
        evidence = _quantity_evidence(fragment, prefix, suffix, tuple(identifiers))
        explicit = [(count, unit) for count, unit in evidence if unit is not None]
        # A stated commercial unit is stronger evidence than a bare number
        # (which could be A4, density, cartridge number or a model code).
        if explicit or line.unit is not None:
            if not any(
                number == line.quantity and (
                    line.unit is None or unit == line.unit
                )
                for number, unit in explicit
            ):
                violations.append(f"line_{index}:quantity_or_unit_unverified")
            continue

        if not any(count == line.quantity for count, _ in evidence):
            violations.append(f"line_{index}:quantity_unverified")
    return tuple(violations)
