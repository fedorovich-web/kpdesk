"""Saved, bounded PDF appearance; no external assets, fonts or executable markup."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class DocumentAppearance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    template: Literal["presentation", "compact"] = "presentation"
    accent_color: str = Field(default="#007F5F", min_length=7, max_length=7, pattern=r"^#[0-9A-Fa-f]{6}$")
    show_company_section: StrictBool = True
    layout_version: Literal[1, 2] = Field(default=1, exclude_if=lambda value: value == 1)
    default_logo: Literal["kpdesk_v1"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("layout_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("PDF layout version must be an integer")
        return value

    @field_validator("accent_color")
    @classmethod
    def canonical_color(cls, value: str) -> str:
        return value.upper()


def header_text_color(accent_color: str) -> str:
    """Pick the higher WCAG contrast of black and white for the table heading."""
    channels = [int(accent_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in channels]
    luminance = sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear, strict=True))
    return "#000000" if (luminance + .05) / .05 >= 1.05 / (luminance + .05) else "#FFFFFF"
