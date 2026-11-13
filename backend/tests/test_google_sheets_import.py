"""Google Sheets -> strict catalog validation; no API key for private documents."""

from __future__ import annotations

import json

import httpx
import pytest

from kpdesk.integrations.google_sheets import GoogleSheetsError, fetch_sheet_catalog

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz12345"
TABLE: list[list[str | int | float]] = [
    ["sku", "name", "category", "sale_unit", "pieces_per_sale_unit", "price_rub",
     "brand", "paper_format", "paper_gsm", "pen_ink_color", "pen_ink_type"],
    ["000001", "Бумага А4 80", "paper", "пачка", 1, 349.9, "Paper", "A4", 80],
]


@pytest.mark.asyncio
async def test_oauth_sheet_price_import_keeps_text_sku_and_money() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sheets.googleapis.com"
        assert request.url.path.startswith(f"/v4/spreadsheets/{SHEET_ID}/values/")
        assert request.headers["authorization"] == "Bearer test-oauth-token"
        assert request.url.params["valueRenderOption"] == "UNFORMATTED_VALUE"
        return httpx.Response(200, json={"range": "Products!A1:K5001", "values": TABLE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_sheet_catalog(SHEET_ID, "test-oauth-token", client)
    assert result.publishable
    assert len(result.products) == 1
    assert result.products[0].sku == "000001"
    assert str(result.products[0].price_rub) == "349.9"


@pytest.mark.asyncio
async def test_numeric_sku_must_not_lose_leading_zeros() -> None:
    values = [TABLE[0], [1, *TABLE[1][1:]]]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"values": values}))
    ) as client:
        result = await fetch_sheet_catalog(SHEET_ID, "token", client)
    assert not result.publishable
    assert result.products == ()
    assert result.errors[0].code == "sku_not_text"


@pytest.mark.asyncio
async def test_bad_header_preserves_atomicity() -> None:
    values = [["sku", "WRONG", *TABLE[0][2:]], TABLE[1]]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"values": values}))
    ) as client:
        result = await fetch_sheet_catalog(SHEET_ID, "token", client)
    assert not result.publishable
    assert "invalid_header" in {error.code for error in result.errors}


@pytest.mark.asyncio
async def test_google_sheets_auth_error_and_rate_limit_are_explicit() -> None:
    for status, code in ((401, "oauth_denied"), (403, "oauth_denied"), (429, "rate_limited")):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, s=status: httpx.Response(s, json={"error": {"message": "SECRET"}})
            )
        ) as client:
            with pytest.raises(GoogleSheetsError) as error:
                await fetch_sheet_catalog(SHEET_ID, "token", client)
            assert error.value.code == code
            assert "SECRET" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_sheet_id_and_missing_oauth_never_make_network_call() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected network request"))
    ) as client:
        for sheet_id in ("../../etc/passwd", "https://evil.example", "1"):
            with pytest.raises(ValueError):
                await fetch_sheet_catalog(sheet_id, "token", client)
        with pytest.raises(ValueError):
            await fetch_sheet_catalog(SHEET_ID, "", client)


@pytest.mark.asyncio
async def test_google_sheets_rejects_invalid_value_types() -> None:
    values = [TABLE[0], ["000001", "Paper", "paper", "пачка", True, 349.9, "", "A4", 80]]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"values": values}))
    ) as client:
        result = await fetch_sheet_catalog(SHEET_ID, "token", client)
    assert not result.publishable


@pytest.mark.asyncio
async def test_google_sheets_rejects_unbounded_api_response() -> None:
    payload = json.dumps({"values": [["A" * 256] * 11] * 40000})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as client:
        with pytest.raises(GoogleSheetsError) as error:
            await fetch_sheet_catalog(SHEET_ID, "token", client)
        assert error.value.code == "too_large"


@pytest.mark.asyncio
async def test_v2_google_sheets_supports_strict_typed_category_attrs() -> None:
    import json as _json

    from kpdesk.catalog.schema import COLUMNS_V2

    row = [
        "K-001", "Клавиатура USB 104", "keyboard", "штука", 1,
        725.94, "SONNEN", "", "", "", "",
        _json.dumps({"connection": "wired", "keys": 104}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"values": [list(COLUMNS_V2), row]})
        ),
    ) as client:
        result = await fetch_sheet_catalog(SHEET_ID, "test-access", client)
    assert result.publishable, result.errors
    assert result.products[0].attributes == {"connection": "wired", "keys": 104}


@pytest.mark.asyncio
async def test_v2_google_sheets_invalid_attributes_never_publish() -> None:
    import json as _json

    from kpdesk.catalog.schema import COLUMNS_V2

    row = [
        "M-001", "Мышь wireless 1600", "mouse", "штука", 1,
        496.51, "SONNEN", "", "", "", "",
        _json.dumps({"connection": "wired", "dpi": True}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"values": [list(COLUMNS_V2), row]})
        ),
    ) as client:
        result = await fetch_sheet_catalog(SHEET_ID, "test-access", client)
    assert not result.publishable
    assert result.products == ()
