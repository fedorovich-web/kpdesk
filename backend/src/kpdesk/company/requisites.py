"""Bounded manager-reviewed company requisites, independent of lookup providers."""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_inn(value: str) -> str:
    """Validate the ASCII 10/12 digit INN and its official check digits."""
    if not re.fullmatch(r"(?:[0-9]{10}|[0-9]{12})", value) or set(value) == {"0"}:
        raise ValueError("Некорректный ИНН: требуется 10 или 12 цифр.")
    digits = [int(char) for char in value]

    def checksum(weights: tuple[int, ...]) -> int:
        return sum(a * b for a, b in zip(digits, weights, strict=False)) % 11 % 10

    valid = (
        checksum((2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[9]
        if len(digits) == 10 else
        checksum((7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[10]
        and checksum((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[11]
    )
    if not valid:
        raise ValueError("Некорректная контрольная сумма ИНН.")
    return value


class PartyDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    full_name: str = Field(min_length=1, max_length=500)
    short_name: str = Field(min_length=1, max_length=200)
    inn: str
    kpp: str | None = Field(default=None, pattern=r"^[0-9]{9}$")
    ogrn: str | None = Field(default=None, pattern=r"^(?:[0-9]{13}|[0-9]{15})$")
    address: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, max_length=80)

    @field_validator("inn")
    @classmethod
    def valid_inn(cls, value: str) -> str:
        return validate_inn(value)

    @field_validator("full_name", "short_name", "address", "status", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> object:
        if isinstance(value, str):
            # Never return control characters to a PDF, DOM or downstream log.
            return " ".join("".join(
                " " if unicodedata.category(char).startswith("C") else char for char in value
            ).split())
        return value


