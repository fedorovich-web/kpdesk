from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kpdesk.company.logo import CompanyLogo
from kpdesk.company.requisites import PartyDetails


class CompanyProfileData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    display_name: str = Field(min_length=1, max_length=200)
    about: str = Field(default="", max_length=1000)
    advantages: str = Field(default="", max_length=1000)
    contacts: str = Field(default="", max_length=600)
    default_terms: str = Field(default="", max_length=1000)
    generation_style: str = Field(default="", max_length=500)
    # Omit absent details so historical profile/document digests remain unchanged.
    issuer_details: PartyDetails | None = Field(default=None, exclude_if=lambda value: value is None)
    logo: CompanyLogo | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("display_name", "about", "advantages", "contacts", "default_terms", "generation_style")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL is not allowed")
        return value.strip()

    @field_validator("display_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value:
            raise ValueError("Company name must not be blank")
        return value


def profile_digest(data: CompanyProfileData) -> str:
    raw = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CompanyProfileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: int = Field(ge=1, le=2147483647, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data: CompanyProfileData

    @model_validator(mode="after")
    def consistent_digest(self) -> CompanyProfileSnapshot:
        if self.sha256 != profile_digest(self.data):
            raise ValueError("Company profile digest mismatch")
        return self
