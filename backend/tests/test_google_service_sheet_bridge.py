"""Bridge Service Account token acquisition to existing strict Sheets importer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from kpdesk.integrations.google_service_account import fetch_shared_sheet_catalog

_SPREADSHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz12345"
_FIELDS = [
    "sku", "name", "category", "sale_unit", "pieces_per_sale_unit",
    "price_rub", "brand", "paper_format", "paper_gsm",
    "pen_ink_color", "pen_ink_type",
]


@pytest.mark.asyncio
async def test_bridge_reads_shared_sheet_without_mutation() -> None:
    seen: list[httpx.Request] = []

    def mock_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer short-lived-only"
        return httpx.Response(200, json={"values": [
            _FIELDS,
            ["000001", "Бумага A4 80", "paper", "пачка", 1, 349.90, "", "A4", 80],
        ]})

    fake_credentials = SimpleNamespace()
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_api)) as client:
        with patch(
            "kpdesk.integrations.google_service_account.get_service_access_token",
            new=AsyncMock(return_value="short-lived-only"),
        ) as token:
            catalog = await fetch_shared_sheet_catalog(
                _SPREADSHEET_ID, fake_credentials, client
            )
    assert catalog.publishable
    assert catalog.products[0].sku == "000001"
    assert len(seen) == 1
    token.assert_awaited_once_with(fake_credentials)
