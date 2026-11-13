"""Import Google Sheets into the existing strict catalog validation pipeline.

Google API keys work only for public resources. Private spreadsheets require an
authorized OAuth bearer token, provided by a future user-consent flow.
This module performs read/preview only: it never publishes a catalog.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re

import httpx

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import (
    COLUMNS,
    COLUMNS_V2,
    CatalogImportResult,
    ImportErrorDetail,
    ImportLimits,
)

_SHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
_BASE_URL = "https://sheets.googleapis.com"
_SHEET_RANGE = "'Products'!A1:L5001"
_MAX_PAYLOAD_BYTES = 10 * 1024 * 1024


class GoogleSheetsError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _invalid_catalog(
    source_id: str, content: bytes, code: str, message: str, row: int
) -> CatalogImportResult:
    return CatalogImportResult(
        original_filename=f"google-sheets-{source_id}.csv",
        source_sha256=hashlib.sha256(content).hexdigest(),
        products=(),
        errors=(ImportErrorDetail(code, message, row),),
    )


def _cell_to_string(value: object, *, sku: bool, row: int) -> str:
    if sku and not isinstance(value, str):
        raise GoogleSheetsError("sku_not_text")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise GoogleSheetsError("invalid_cell_type")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    raise GoogleSheetsError("invalid_cell_type")


def _parse_google_values(source_id: str, payload: bytes) -> CatalogImportResult:
    try:
        response = httpx.Response(200, content=payload).json()
    except ValueError as error:
        raise GoogleSheetsError("invalid_response") from error

    if not isinstance(response, dict):
        raise GoogleSheetsError("invalid_response")
    values = response.get("values")
    if not isinstance(values, list):
        raise GoogleSheetsError("invalid_response")
    if len(values) > 5001:
        raise GoogleSheetsError("too_many_rows")

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    if not values or not isinstance(values[0], list):
        raise GoogleSheetsError("invalid_sheet_data")
    header = tuple(values[0])
    expected_columns = COLUMNS_V2 if header == COLUMNS_V2 else COLUMNS
    for index, row in enumerate(values, 1):
        if not isinstance(row, list) or len(row) > len(expected_columns):
            raise GoogleSheetsError("invalid_sheet_data")
        try:
            cells = [
                _cell_to_string(value, sku=(index > 1 and col == 0), row=index)
                for col, value in enumerate(row)
            ]
        except GoogleSheetsError as error:
            return _invalid_catalog(
                source_id, payload, error.code, "Неподдерживаемый тип ячейки Sheets.", index
            )
        writer.writerow(cells + [""] * (len(expected_columns) - len(cells)))
    return parse_price_list(
        f"google-sheets-{source_id}.csv",
        output.getvalue().encode("utf-8"),
        ImportLimits(),
    )


async def fetch_sheet_catalog(
    spreadsheet_id: str, oauth_access_token: str, client: httpx.AsyncClient
) -> CatalogImportResult:
    """Read fixed Products!A1:K5001 range, validate, and return *preview only*."""
    if not _SHEET_ID.fullmatch(spreadsheet_id):
        raise ValueError("Invalid Google spreadsheet ID.")
    if not oauth_access_token.strip():
        raise ValueError("OAuth access token required for private Google Sheets.")

    url = f"{_BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{_SHEET_RANGE}"
    try:
        async with client.stream(
            "GET", url,
            params={"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"},
            headers={"Authorization": f"Bearer {oauth_access_token}"},
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
        ) as response:
            if response.status_code in (401, 403):
                raise GoogleSheetsError("oauth_denied")
            if response.status_code == 429:
                raise GoogleSheetsError("rate_limited")
            if response.status_code != 200:
                raise GoogleSheetsError("provider_error")
            parts: list[bytes] = []
            length = 0
            async for part in response.aiter_bytes(65536):
                length += len(part)
                if length > _MAX_PAYLOAD_BYTES:
                    raise GoogleSheetsError("too_large")
                parts.append(part)
    except httpx.TimeoutException as error:
        raise GoogleSheetsError("provider_timeout") from error
    except httpx.RequestError as error:
        raise GoogleSheetsError("provider_unreachable") from error
    return _parse_google_values(spreadsheet_id, b"".join(parts))
