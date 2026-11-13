"""Bounded, deterministic quote rules. No LLM result can authorize a price.

Supported commercial instructions are markup, maximum selling-price budget,
and exact brand allow/deny conditions. Unknown clauses block confirmation.
The original text is re-parsed before calculation to prevent stale/tampered
client-side rule previews from changing an approved calculation.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.calculate import CENT, MAX_RUB

_NUMBER = r"(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)"
_MARKUP = re.compile(
    r"(?:примен(?:и|ить|яй)|добавь|установи|используй|сделай)?\s*"
    r"наценк[ауеиой]+\s*(?:в размере\s*|на\s*)?"
    + _NUMBER + r"\s*%",
    re.IGNORECASE,
)
_BUDGET = re.compile(
    r"(?:общ(?:ая|ую)\s+сумм[ау]|итогов(?:ая|ую)\s+стоимост[ьи]|"
    r"(?:общий\s+)?бюджет|сумм[ау])\s*(?:[:—-]\s*)?"
    r"(?:не более|не выше|до|максимум|не превышает)\s*"
    + _NUMBER + r"\s*(?:руб(?:лей|ля|ль|\.)?|₽)?",
    re.IGNORECASE,
)
_BRAND_REQUIRED = re.compile(
    r"(?:выбирай\s+|используй\s+)?только\s+(?:товары\s+)?бренд(?:а)?\s+([^.;!\n]{1,100})",
    re.IGNORECASE,
)
_BRAND_EXCLUDED = re.compile(
    r"(?:не\s+используй|исключи|исключить|не\s+включай)\s+"
    r"(?:товары\s+)?бренд(?:а)?\s+([^.;!\n]{1,100})",
    re.IGNORECASE,
)
_BRAND_NAME = re.compile(r"[\w][\w\s&+\-]{0,99}\Z", re.UNICODE)
_FIXED = (
    re.compile(r"подбери\s+(?:товары|позиции)\s+по\s+заявке", re.IGNORECASE),
    re.compile(r"(?:не меняй|не изменяй|сохраняй)\s+обязательные\s+характеристики", re.IGNORECASE),
    re.compile(r"(?:не использовать|не включать|запретить|без)\s+неподтвержд[её]нные\s+аналоги", re.IGNORECASE),
    re.compile(r"позиции\s+без\s+(?:подходящих\s+)?товаров\s+(?:вынеси|выведи)\s+отдельно", re.IGNORECASE),
)


class QuoteRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    markup_percent: Decimal = Field(default=Decimal(0), ge=0, le=100, max_digits=5, decimal_places=2)
    budget_max_rub: Decimal | None = Field(
        default=None, gt=0, le=MAX_RUB, max_digits=14, decimal_places=2,
    )
    required_brand: str | None = Field(default=None, max_length=100)
    excluded_brands: tuple[str, ...] = Field(default=(), max_length=10)


class InstructionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_text: str
    rules: QuoteRules
    unresolved_requirements: tuple[str, ...]
    can_confirm: bool


class ConfirmedInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_text: str = Field(min_length=1, max_length=2000)
    rules: QuoteRules


def _normalized_brand(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold().replace("ё", "е")


def _valid_brand(value: str) -> bool:
    # Conjoined clauses are not a single exact brand; do not silently accept them.
    return bool(_BRAND_NAME.fullmatch(value)) and not (
        {"и", "или", "кроме", "но", "если"} & set(value.split())
    )


def brand_allowed(brand: str | None, rules: QuoteRules) -> bool:
    """Never turn missing brand evidence into an allowed positive brand match."""
    value = _normalized_brand(brand) if brand is not None else None
    if not value:
        value = None
    return (
        (rules.required_brand is None or value == _normalized_brand(rules.required_brand))
        and (not rules.excluded_brands or value is not None)
        and (value not in {
            _normalized_brand(item) for item in rules.excluded_brands
        })
    )


def filter_catalog_by_brand(
    products: list[CatalogProduct], rules: QuoteRules | None,
) -> list[CatalogProduct]:
    if rules is None or (rules.required_brand is None and not rules.excluded_brands):
        return products
    return [product for product in products if brand_allowed(product.brand, rules)]


def _decimal(value: str) -> Decimal:
    normalized = value.replace("\u00a0", " ").strip()
    if re.fullmatch(r"(?:\d+|\d{1,3}(?: +\d{3})+)(?:[.,]\d{1,2})?", normalized) is None:
        raise ValueError("ambiguous_instruction_number")
    return Decimal(normalized.replace(" ", "").replace(",", "."))


def instruction_evidence(text: str) -> tuple[str, ...]:
    """Recognizable commercial clauses must not disappear during LLM extraction."""
    return tuple(
        match.group().strip() for pattern in (_MARKUP, _BUDGET, _BRAND_REQUIRED, _BRAND_EXCLUDED)
        for match in pattern.finditer(text)
    )


def propose_instructions(raw_text: str) -> InstructionProposal:
    if not 1 <= len(raw_text.strip()) <= 2000:
        raise ValueError("instruction_text_length")
    remaining = raw_text.lower().replace("ё", "е")
    remaining = remaining.replace("\u00a0", " ")
    unresolved: list[str] = []
    values: dict[str, Decimal] = {}
    for name, pattern in (("markup_percent", _MARKUP), ("budget_max_rub", _BUDGET)):
        matches = list(pattern.finditer(remaining))
        if matches:
            if len(matches) != 1:
                unresolved.append(f"conflicting_{name}")
            else:
                try:
                    candidate = _decimal(matches[0].group(1))
                    limit = Decimal(100) if name == "markup_percent" else MAX_RUB
                    if (
                        candidate < (Decimal(0) if name == "markup_percent" else CENT)
                        or candidate > limit
                        or candidate != candidate.quantize(CENT)
                    ):
                        unresolved.append(f"invalid_{name}")
                    else:
                        values[name] = candidate
                except (ArithmeticError, ValueError):
                    unresolved.append(f"invalid_{name}")
            remaining = pattern.sub(" ", remaining)
    required = list(_BRAND_REQUIRED.finditer(remaining))
    excluded = list(_BRAND_EXCLUDED.finditer(remaining))
    brand_require: str | None = None
    brand_exclusions: list[str] = []
    if len(required) > 1:
        unresolved.append("conflicting_required_brand")
    elif required:
        proposed = _normalized_brand(required[0].group(1))
        if not _valid_brand(proposed):
            unresolved.append("invalid_required_brand")
        else:
            brand_require = proposed
    if len(excluded) > 10:
        unresolved.append("too_many_excluded_brands")
    for match in excluded:
        proposed = _normalized_brand(match.group(1))
        if not _valid_brand(proposed):
            unresolved.append("invalid_excluded_brand")
        elif proposed in brand_exclusions:
            unresolved.append("duplicate_excluded_brand")
        else:
            brand_exclusions.append(proposed)
    if brand_require is not None and brand_require in brand_exclusions:
        unresolved.append("conflicting_brand_policy")
    remaining = _BRAND_REQUIRED.sub(" ", remaining)
    remaining = _BRAND_EXCLUDED.sub(" ", remaining)
    for pattern in _FIXED:
        remaining = pattern.sub(" ", remaining)
    fragments = [s.strip() for s in re.split(r"[.;!\n]+", remaining) if s.strip()]
    unresolved.extend(fragments)
    rules = QuoteRules(
        markup_percent=values.get("markup_percent", Decimal(0)),
        budget_max_rub=values.get("budget_max_rub"),
        required_brand=brand_require,
        # Preserve a valid preview object even for oversized unsupported lists.
        excluded_brands=tuple(brand_exclusions[:10]),
    )
    return InstructionProposal(
        raw_text=raw_text, rules=rules,
        unresolved_requirements=tuple(unresolved),
        can_confirm=not unresolved and bool(values or brand_require or brand_exclusions),
    )


def check_confirmation(instructions: ConfirmedInstructions) -> QuoteRules:
    preview = propose_instructions(instructions.raw_text)
    if not preview.can_confirm or preview.rules != instructions.rules:
        raise ValueError("instruction_confirmation_mismatch")
    return preview.rules


def selling_total(base_amount: Decimal, markup_percent: Decimal) -> Decimal:
    """Apply percentage at line granularity, rounding half up to RUB kopecks."""
    if base_amount < 0 or base_amount > MAX_RUB:
        raise ValueError("invalid_base_amount")
    value = (base_amount * (Decimal(100) + markup_percent) / Decimal(100)).quantize(
        CENT, rounding=ROUND_HALF_UP,
    )
    if value > MAX_RUB:
        raise ValueError("money_overflow")
    return value
