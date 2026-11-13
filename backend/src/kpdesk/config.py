"""Validated local HTTP settings and runtime-only database credentials."""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )
    allowed_tenant_origins: tuple[str, ...] = (
        "https://127.0.0.1:5173",
        "https://localhost:5173",
    )
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=10 * 1024 * 1024)
    enable_synthetic_ml: bool = Field(
        default_factory=lambda: os.environ.get("KP_DESK_ENABLE_SYNTHETIC_ML") == "1"
    )


def application_database_url() -> URL:
    """Never permit a server request to connect with the PostgreSQL owner role."""
    env = dotenv_values(PROJECT_ROOT / ".env")
    password = env.get("KP_DESK_APP_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Database application role is not configured.")
    return URL.create(
        "postgresql+psycopg",
        username="kpdesk_app",
        password=password,
        host="127.0.0.1",
        port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk",
    )
