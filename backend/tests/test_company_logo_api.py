from collections.abc import Iterator
from typing import cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Connection
from test_company_logo import source_logo

from kpdesk.api import tenant_company_profile_routes as routes
from kpdesk.api.body_limit import RequestBodyLimitMiddleware
from kpdesk.api.errors import ApiError, json_error
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.company.logo import CompanyLogo, prepare_logo
from kpdesk.db.auth_sessions import AuthSessionRepository, SessionDenied
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

ORG = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, bool], Mock]]:
    state = {"owner": True, "revoked": False}

    def authenticate(self: object, token: str, *, operation: str, csrf: str | None) -> AuthenticatedUser:
        assert operation == "write"
        if token != "session" or csrf != "csrf" or state["revoked"]:
            raise SessionDenied()
        return AuthenticatedUser(uuid4())

    def authorize(connection: object, *, session_token: str, organization_id: UUID,
                  operation: str, csrf: str | None) -> None:
        assert session_token == "session" and csrf == "csrf" and operation == "manage"
        if organization_id != ORG or not state["owner"]:
            raise TenantAccessDenied()

    monkeypatch.setattr(AuthSessionRepository, "authenticate", authenticate)
    monkeypatch.setattr(routes, "bind_authenticated_tenant", authorize)
    prepare = Mock(wraps=prepare_logo)
    monkeypatch.setattr(routes, "prepare_logo", prepare)
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_upload_bytes=10 * 1024 * 1024)
    app.include_router(routes.router)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: AuthSessionRepository(cast(Connection, object()))

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return json_error(request, error.status_code, error.code, error.message)

    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "session")
        client.headers["X-CSRF-Token"] = "csrf"
        yield client, state, prepare


def test_logo_prepare_is_private_and_returns_only_a_valid_snapshot(boundary: tuple[TestClient, dict[str, bool], Mock]) -> None:
    client, _, prepare = boundary
    result = client.post(f"/api/session/organizations/{ORG}/company-profile/logo/prepare",
                         files={"file": ("private-client-name.jpg", source_logo(fmt="JPEG"), "image/jpeg")})
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    logo = CompanyLogo.model_validate(result.json()["logo"])
    assert logo.mime_type == "image/png" and logo.width == 400
    assert "private-client-name" not in result.text
    assert prepare.call_count == 1


@pytest.mark.parametrize("case,status", [("anonymous", 401), ("csrf", 401), ("revoked", 401), ("member", 403), ("other-org", 403)])
def test_unauthorized_logo_is_rejected_before_decoding(boundary: tuple[TestClient, dict[str, bool], Mock], case: str, status: int) -> None:
    client, state, prepare = boundary
    org = ORG
    if case == "anonymous": client.cookies.clear()
    if case == "csrf": client.headers.pop("X-CSRF-Token")
    if case == "revoked": state["revoked"] = True
    if case == "member": state["owner"] = False
    if case == "other-org": org = uuid4()
    result = client.post(f"/api/session/organizations/{org}/company-profile/logo/prepare", files={"file": ("bad.png", b"bad", "image/png")})
    assert result.status_code == status
    prepare.assert_not_called()


@pytest.mark.parametrize("size", [1048577, 2 * 1024 * 1024])
def test_both_stream_and_multipart_logo_limits_are_bounded(boundary: tuple[TestClient, dict[str, bool], Mock], size: int) -> None:
    client, _, prepare = boundary
    result = client.post(f"/api/session/organizations/{ORG}/company-profile/logo/prepare", files={"file": ("big.png", b"x" * size, "image/png")})
    assert result.status_code == 413
    prepare.assert_not_called()


def test_svg_and_corrupted_raster_return_clear_validation_error(boundary: tuple[TestClient, dict[str, bool], Mock]) -> None:
    client, _, _ = boundary
    for raw in (b"<svg><script>alert(1)</script></svg>", b"\x89PNG\r\n\x1a\ninvalid"):
        result = client.post(f"/api/session/organizations/{ORG}/company-profile/logo/prepare", files={"file": ("logo.png", raw, "image/png")})
        assert result.status_code == 422 and result.json()["code"] == "company_logo_invalid"


@pytest.mark.parametrize('diagnostic', [
    'logo_png_crc_invalid', 'private-client-name.jpg customer@example.test internal decoder data',
])
def test_logo_error_does_not_echo_internal_or_source_diagnostics(
    boundary: tuple[TestClient, dict[str, bool], Mock], diagnostic: str,
) -> None:
    client, _, prepare = boundary
    prepare.side_effect = ValueError(diagnostic)
    result = client.post(f'/api/session/organizations/{ORG}/company-profile/logo/prepare',
                         files={'file': ('logo.png', b'synthetic', 'image/png')})
    assert result.status_code == 422
    assert result.json()['code'] == 'company_logo_invalid'
    assert diagnostic not in result.text
    assert any('а' <= c.lower() <= 'я' for c in result.json()['message'])
