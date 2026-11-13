"""Server-only OpenRouter configuration. No legacy-key or paid-model fallback."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Literal, Self

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from kpdesk.config import PROJECT_ROOT

MODEL: Final = "nvidia/nemotron-3-ultra-550b-a55b:free"
BASE_URL: Final = "https://openrouter.ai/api/v1"


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: str = Field(min_length=1, repr=False)
    model: Literal["nvidia/nemotron-3-ultra-550b-a55b:free"] = MODEL
    base_url: Literal["https://openrouter.ai/api/v1"] = BASE_URL
    requests_per_minute: int = Field(default=20, ge=1, le=20)
    daily_request_limit: int = Field(default=50, ge=1, le=1000)
    max_parallel: int = Field(default=1, ge=1, le=20)
    max_calls_per_quote: int = Field(default=6, ge=1, le=6)
    data_mode: Literal["synthetic_only", "real_catalogs"] = "synthetic_only"
    test_project_approved: bool = False
    request_timeout_seconds: float = Field(default=45, ge=1, le=90)

    @property
    def processing_approved(self) -> bool:
        """An explicit real-catalog mode also authorizes the existing route.

        Keep the historical synthetic-project opt-in for old configurations;
        real mode is an operator choice and still requires request consent,
        tenant authorization and a shared provider budget at dispatch.
        """
        return self.data_mode == 'real_catalogs' or (
            self.data_mode == 'synthetic_only' and self.test_project_approved
        )

    @classmethod
    def from_local_env(cls, path: Path = PROJECT_ROOT / ".env") -> Self:
        local = dotenv_values(path)
        names = {
            "api_key": "OPENROUTER_API_KEY", "model": "OPENROUTER_MODEL",
            "base_url": "OPENROUTER_BASE_URL",
            "requests_per_minute": "OPENROUTER_REQUESTS_PER_MINUTE",
            "daily_request_limit": "OPENROUTER_DAILY_REQUEST_LIMIT",
            "max_parallel": "OPENROUTER_MAX_PARALLEL",
            "max_calls_per_quote": "OPENROUTER_MAX_CALLS_PER_QUOTE",
            "data_mode": "OPENROUTER_DATA_MODE",
            "test_project_approved": "OPENROUTER_TEST_PROJECT_APPROVED",
        }
        values = {}
        for field, env in names.items():
            value = os.environ.get(env, local.get(env))
            if value is not None:
                values[field] = value.strip()
        if not values.get("api_key"):
            raise ValueError("OpenRouter provider is not configured.")
        # An explicitly empty process key disables a populated .env key.
        return cls.model_validate(values)
