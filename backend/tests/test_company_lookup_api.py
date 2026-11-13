from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from kpdesk.api import tenant_company_lookup_routes as routes
from kpdesk.api.errors import ApiError, json_error
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_catalog_engine
from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.company.lookup import CompanyLookupService, LookupSettings
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

INN = "7707083893"
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    state: dict[str, Any] = {"transaction": False, "allowed": True, "revoked": False,
                             "calls": 0, "auth_calls": 0, "db_error": False}

    class OfflineEngine:
        @contextmanager
        def begin(self) -> Any:
            if state["db_error"]:
                raise OperationalError("secret sql", None, Exception("secret"))
            state["transaction"] = True
            try:
                yield object()
            finally:
                state["transaction"] = False

    def authenticate(self: object, token: str, *, operation: str, csrf: str | None) -> AuthenticatedUser:
        state["auth_calls"] += 1
        assert state["transaction"]
        assert operation == "write"
        if token != "valid-session" or csrf != "valid-csrf" or state["revoked"]:
            raise SessionDenied()
        return AuthenticatedUser(uuid.uuid4())

    def authorize(self: TenantCatalogRepository, operation: str) -> None:
        assert state["transaction"] and operation == "write"
        if not state["allowed"] or self.organization_id != ORG:
            raise TenantAccessDenied()

    def handler(_: httpx.Request) -> httpx.Response:
        assert not state["transaction"], "DB authorization transaction must end before network"
        state["calls"] += 1
        return httpx.Response(200, json={"suggestions": []})

    monkeypatch.setattr(AuthSessionRepository, "authenticate", authenticate)
    monkeypatch.setattr(TenantCatalogRepository, "_authorize", authorize)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: OfflineEngine()
    lookup = CompanyLookupService(LookupSettings(api_key=SecretStr("offline-test-key")),
                                  transport=httpx.MockTransport(handler), org_per_minute=2)
    app.dependency_overrides[routes.get_company_lookup_service] = lambda: lookup

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return json_error(request, error.status_code, error.code, error.message)

    client = TestClient(app, base_url="https://testserver")
    client.cookies.set(SESSION_COOKIE, "valid-session")
    client.headers["X-CSRF-Token"] = "valid-csrf"
    with client:
        yield client, state


def lookup(client: TestClient, organization: uuid.UUID = ORG, **changes: object) -> httpx.Response:
    return cast(httpx.Response, client.post(
        f"/api/session/organizations/{organization}/company-lookup", json={"inn": INN, **changes},
    ))


def test_lookup_response_and_cache_reauthorize_each_request(boundary: tuple[TestClient, dict[str, Any]]) -> None:
    client, state = boundary
    first = lookup(client)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert set(first.json()) == {"party", "source", "fetched_at", "cached"}
    assert first.json()["party"] is None and not first.json()["cached"]
    second = lookup(client)
    assert second.json()["cached"]
    assert first.json()["fetched_at"] == second.json()["fetched_at"]
    assert state["auth_calls"] == 2 and state["calls"] == 1


@pytest.mark.parametrize("change,expected", [("session", 401), ("csrf", 401),
                                               ("organization", 403), ("membership", 403)])
def test_denied_requests_do_not_spend_budget_or_observe_cache(
    boundary: tuple[TestClient, dict[str, Any]], change: str, expected: int,
) -> None:
    client, state = boundary
    assert lookup(client).status_code == 200
    if change == "session":
        state["revoked"] = True
    elif change == "csrf":
        client.headers["X-CSRF-Token"] = "wrong"
    elif change == "membership":
        state["allowed"] = False
    response = lookup(client, uuid.uuid4() if change == "organization" else ORG)
    assert response.status_code == expected
    assert state["calls"] == 1
    state["revoked"], state["allowed"] = False, True
    client.headers["X-CSRF-Token"] = "valid-csrf"
    assert lookup(client).status_code == 200
    assert lookup(client).status_code == 429


def test_missing_cookie_and_invalid_input_never_call_provider(boundary: tuple[TestClient, dict[str, Any]]) -> None:
    client, state = boundary
    client.cookies.clear()
    assert lookup(client).status_code == 401
    client.cookies.set(SESSION_COOKIE, "valid-session")
    assert lookup(client, inn="7707083894").status_code == 422
    assert lookup(client, extra="field").status_code == 422
    assert lookup(client, inn=7707083893).status_code == 422
    assert state["calls"] == 0


def test_database_unavailable_is_clean_and_never_calls_provider(boundary: tuple[TestClient, dict[str, Any]]) -> None:
    client, state = boundary
    state["db_error"] = True
    response = lookup(client)
    assert response.status_code == 503
    assert "secret" not in response.text
    assert state["calls"] == 0
