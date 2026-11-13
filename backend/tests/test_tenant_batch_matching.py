"""Whole-request preview keeps catalog, policy and row order consistent."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import cast
from unittest.mock import Mock, patch

import pytest
from fastapi import Response
from pydantic import ValidationError
from starlette.requests import Request

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_matching_routes import (
    TenantBatchMatchRequest,
    TenantBatchMatchResponse,
    tenant_batch_match_preview,
    tenant_composition_variants,
)
from kpdesk.catalog.storage import PublishedCatalog
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.instructions import ConfirmedInstructions, propose_instructions


def _item(description: str = "Карандаш графитовый HB") -> RequestedItem:
    return RequestedItem(
        description=description, category="pencil", quantity=2, unit="штука",
        attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )


def _catalog(version: uuid.UUID) -> Mock:
    catalog = Mock(spec=TenantCatalogRepository)
    catalog.current.return_value = PublishedCatalog(version, "a" * 64, 2)
    catalog.items.return_value = [CatalogProduct(
        sku=sku, name="Карандаш графитовый HB", category="pencil",
        price_rub=Decimal("15.00"), sale_unit="штука", pieces_per_sale_unit=1,
        brand=brand, attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    ) for sku, brand in (("DENIED", "Acme"), ("ALLOWED", "DemoPaper"))]
    return catalog


def _run(body: TenantBatchMatchRequest, catalog: Mock) -> TenantBatchMatchResponse:
    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"})
    response = Response()
    with (
        patch("kpdesk.api.tenant_matching_routes._catalog", return_value=catalog) as authorize,
        patch("kpdesk.api.tenant_matching_routes._enabled_models",
              side_effect=AssertionError("TF-IDF does not load the optional trained model")),
    ):
        result = tenant_batch_match_preview(
            uuid.uuid4(), body, request, response,
            cast(AuthSessionRepository, object()), cookie="session", csrf="csrf",
        )
    authorize.assert_called_once()
    assert authorize.call_args.args[-2:] == ("session", "csrf")
    assert response.headers["Cache-Control"] == "no-store"
    return result


def test_batch_loads_catalog_once_and_preserves_rows_and_brand_policy() -> None:
    version = uuid.uuid4()
    catalog = _catalog(version)
    policy = propose_instructions("Исключи бренд Acme.")
    body = TenantBatchMatchRequest(
        catalog_version_id=version, requested_items=[_item(), _item("Карандаш HB")],
        instructions=ConfirmedInstructions(raw_text=policy.raw_text, rules=policy.rules),
    )
    result = _run(body, catalog)
    assert result.catalog_version_id == version
    assert result.mode == "tfidf"
    assert [line.line_number for line in result.lines] == [1, 2]
    assert all([item.sku for item in line.candidates] == ["ALLOWED"] for line in result.lines)
    assert all(line.requires_manager_confirmation for line in result.lines)
    catalog.current.assert_called_once()
    catalog.items.assert_called_once_with(version)


def test_batch_keeps_unmatched_row_instead_of_shifting_following_results() -> None:
    version = uuid.uuid4()
    catalog = _catalog(version)
    incompatible = _item().model_copy(update={"brand": "MissingBrand"})
    result = _run(TenantBatchMatchRequest(
        catalog_version_id=version, requested_items=[incompatible, _item()],
    ), catalog)
    assert result.lines[0].line_number == 1
    assert result.lines[0].candidates == []
    assert result.lines[1].line_number == 2
    assert result.lines[1].candidates


def test_batch_rejects_stale_catalog_without_loading_products() -> None:
    catalog = _catalog(uuid.uuid4())
    with pytest.raises(ApiError) as error:
        _run(TenantBatchMatchRequest(
            catalog_version_id=uuid.uuid4(), requested_items=[_item()],
        ), catalog)
    assert error.value.code == "stale_catalog"
    catalog.items.assert_not_called()


@pytest.mark.parametrize("count", [0, 21])
def test_batch_limits_request_size(count: int) -> None:
    with pytest.raises(ValidationError):
        TenantBatchMatchRequest(catalog_version_id=uuid.uuid4(), requested_items=[_item()] * count)


def test_variants_authorize_once_keep_brand_policy_and_exact_catalog() -> None:
    version = uuid.uuid4()
    catalog = _catalog(version)
    policy = propose_instructions("Исключи бренд Acme. Наценка 10%.")
    body = TenantBatchMatchRequest(
        catalog_version_id=version, requested_items=[_item()],
        instructions=ConfirmedInstructions(raw_text=policy.raw_text, rules=policy.rules),
    )
    response = Response()
    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"})
    with (
        patch("kpdesk.api.tenant_matching_routes._catalog", return_value=catalog) as authorize,
        patch("kpdesk.api.tenant_matching_routes._enabled_models", side_effect=AssertionError),
    ):
        result = tenant_composition_variants(
            uuid.uuid4(), body, request, response, cast(AuthSessionRepository, object()),
            cookie="session", csrf="csrf",
        )
        with pytest.raises(ApiError) as caught:
            tenant_composition_variants(
                uuid.uuid4(), body.model_copy(update={"catalog_version_id": uuid.uuid4()}),
                request, Response(), cast(AuthSessionRepository, object()),
                cookie="session", csrf="csrf",
            )
        assert caught.value.status_code == 409
        assert caught.value.code == 'stale_catalog'
    assert authorize.call_count == 2
    assert authorize.call_args.args[-2:] == ("session", "csrf")
    assert response.headers["Cache-Control"] == "no-store"
    assert [line.sku for line in result.variants[0].lines] == ["ALLOWED"]
    assert result.variants[0].total_rub == "33.00"
    assert result.candidate_limit_per_line == 20
    catalog.items.assert_called_once_with(version)
