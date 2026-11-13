"""Tenant candidate preview uses the same confirmed commercial brand policy as quote save."""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from fastapi import Response
from starlette.requests import Request

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_matching_routes import (
    TenantMatchRequest,
    TenantMatchResponse,
    tenant_match_preview,
)
from kpdesk.catalog.storage import PublishedCatalog
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.quotes.instructions import ConfirmedInstructions, propose_instructions


def _products() -> list[CatalogProduct]:
    return [
        CatalogProduct(
            sku=sku, name="Карандаш графитовый HB", category="pencil",
            price_rub=Decimal(price), sale_unit="штука", pieces_per_sale_unit=1,
            brand=brand, attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        )
        for sku, price, brand in (
            ("FORBIDDEN", "10.00", "Acme"),
            ("PERMITTED", "15.00", "DemoPaper"),
        )
    ]


def _run(
    *, instructions: ConfirmedInstructions,
) -> tuple[TenantMatchResponse, Mock]:
    version = uuid.uuid4()
    catalog = Mock(spec=TenantCatalogRepository)
    catalog.current.return_value = PublishedCatalog(version, "a" * 64, 2)
    catalog.items.return_value = _products()
    body = TenantMatchRequest(
        catalog_version_id=version,
        requested_item=RequestedItem(
            description="Карандаш графитовый HB", category="pencil",
            quantity=2, unit="штука",
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
        instructions=instructions,
    )
    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"})
    with (
        patch("kpdesk.api.tenant_matching_routes._catalog", return_value=catalog),
        patch("kpdesk.api.tenant_matching_routes._enabled_models",
              return_value=SimpleNamespace(reranker=None)),
        patch("kpdesk.api.tenant_matching_routes.retrieve_candidates",
              return_value=[]) as retrieve,
        patch("kpdesk.api.tenant_matching_routes.rank_candidates", return_value=[]),
    ):
        result = tenant_match_preview(
            uuid.uuid4(), body, request, Response(),
            cast(AuthSessionRepository, object()), cookie="session", csrf="csrf",
        )
    return result, retrieve


def test_preview_filters_denied_brands_before_retrieval() -> None:
    preview = propose_instructions("Исключи бренд Acme.")
    assert preview.can_confirm
    result, retrieval = _run(instructions=ConfirmedInstructions(
        raw_text=preview.raw_text, rules=preview.rules,
    ))
    assert result.candidates == []
    candidates = retrieval.call_args.args[1]
    assert [product.sku for product in candidates] == ["PERMITTED"]


def test_preview_rejects_tampered_policy_without_retrieval() -> None:
    preview = propose_instructions("Исключи бренд Acme.")
    wrong = preview.rules.model_copy(update={"excluded_brands": ()})
    with pytest.raises(ApiError) as error:
        _run(instructions=ConfirmedInstructions(raw_text=preview.raw_text, rules=wrong))
    assert error.value.status_code == 422
    assert error.value.code == "invalid_instructions"


def test_explicit_lexical_preview_works_without_ml_and_requires_manager() -> None:
    version = uuid.uuid4()
    catalog = Mock(spec=TenantCatalogRepository)
    catalog.current.return_value = PublishedCatalog(version, "a" * 64, 2)
    catalog.items.return_value = _products()
    preview = propose_instructions("Исключи бренд Acme.")
    assert preview.can_confirm
    body = TenantMatchRequest(
        catalog_version_id=version,
        requested_item=RequestedItem(
            description="Карандаш графитовый HB", category="pencil",
            quantity=2, unit="штука",
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
        mode="rapidfuzz",
        instructions=ConfirmedInstructions(raw_text=preview.raw_text, rules=preview.rules),
    )
    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"})
    with (
        patch("kpdesk.api.tenant_matching_routes._catalog", return_value=catalog),
        patch("kpdesk.api.tenant_matching_routes._enabled_models",
              side_effect=AssertionError("ML must not be loaded for an explicit lexical search")),
    ):
        result = tenant_match_preview(
            uuid.uuid4(), body, request, Response(),
            cast(AuthSessionRepository, object()), cookie="session", csrf="csrf",
        )
    assert result.mode == "rapidfuzz"
    assert result.requires_manager_confirmation
    assert result.calibrated_no_match is False
    assert len(result.candidates) == 1
    assert result.candidates[0].sku == "PERMITTED"
    assert result.candidates[0].status == "compatible"
    assert 0 <= result.candidates[0].score <= 1
