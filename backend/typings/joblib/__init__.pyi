"""Minimal joblib boundary typing. No broad fake package API."""

from pathlib import Path
from typing import Any

def dump(value: object, filename: str | Path, compress: int = 0) -> list[str]: ...
def load(filename: str | Path) -> Any: ...
