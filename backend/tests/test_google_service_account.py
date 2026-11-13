"""Service Account is read-only for shared My Drive folders; no fake storage quota."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from google.oauth2.service_account import Credentials

from kpdesk.integrations.google_service_account import (
    GOOGLE_FOLDER_ID,
    GOOGLE_SERVICE_ACCOUNT_EMAIL,
    GoogleCredentialsError,
    GoogleFolderError,
    get_service_access_token,
    load_service_credentials,
    probe_folder_access,
)


def mock_key_json(path: Path, *, email: str = GOOGLE_SERVICE_ACCOUNT_EMAIL) -> None:
    path.write_text(json.dumps({
        "type": "service_account",
        "project_id": "kpdesk",
        "private_key_id": "demo-not-real",
        "private_key": "test-placeholder-not-a-real-key",
        "client_email": email,
        "client_id": "0",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }), encoding="utf-8")


def test_no_credentials_is_explicit_and_never_falls_back_to_api_key(tmp_path: Path) -> None:
    with pytest.raises(GoogleCredentialsError, match="not_configured"):
        load_service_credentials(tmp_path / "google-service-account.json", trusted_root=tmp_path)


def test_credentials_must_live_in_expected_private_path(tmp_path: Path) -> None:
    root = tmp_path / "credentials"
    root.mkdir()
    elsewhere = tmp_path / "outside.json"
    mock_key_json(elsewhere)
    with pytest.raises(GoogleCredentialsError, match="untrusted_path"):
        load_service_credentials(elsewhere, trusted_root=root)

    foreign = root / "foreign.json"
    mock_key_json(foreign)
    with pytest.raises(GoogleCredentialsError, match="untrusted_path"):
        load_service_credentials(foreign, trusted_root=root)


def test_reject_wrong_service_account_or_json_key_type(tmp_path: Path) -> None:
    p = tmp_path / "google-service-account.json"
    mock_key_json(p, email="unrelated@other.iam.gserviceaccount.com")
    with pytest.raises(GoogleCredentialsError, match="wrong_account"):
        load_service_credentials(p, trusted_root=tmp_path)
    p.write_text('{"type":"authorized_user"}', encoding="utf-8")
    with pytest.raises(GoogleCredentialsError, match="invalid_credentials"):
        load_service_credentials(p, trusted_root=tmp_path)


def test_service_account_uses_least_privilege_read_scopes(tmp_path: Path) -> None:
    p = tmp_path / "google-service-account.json"
    mock_key_json(p)
    fake = SimpleNamespace(service_account_email=GOOGLE_SERVICE_ACCOUNT_EMAIL)
    with patch(
        "kpdesk.integrations.google_service_account.service_account.Credentials.from_service_account_file",
        return_value=fake,
    ) as loader:
        result = load_service_credentials(p, trusted_root=tmp_path)
    assert result is cast(Credentials, fake)
    scopes = loader.call_args.kwargs["scopes"]
    assert set(scopes) == {
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    }
    assert all("drive.file" not in scope and "drive.readonly" not in scope for scope in scopes)


@pytest.mark.asyncio
async def test_refreshes_service_token_without_returning_private_key() -> None:
    creds = SimpleNamespace(token=None)

    def refresh(_request: object) -> None:
        creds.token = "ephemeral-access-token"

    creds.refresh = refresh
    token = await get_service_access_token(creds)
    assert token == "ephemeral-access-token"


@pytest.mark.asyncio
async def test_folder_probe_is_read_only_and_reports_my_drive() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "www.googleapis.com"
        assert request.url.path == f"/drive/v3/files/{GOOGLE_FOLDER_ID}"
        assert request.headers["authorization"] == "Bearer transient"
        return httpx.Response(200, json={
            "id": GOOGLE_FOLDER_ID,
            "name": "KPDesk",
            "mimeType": "application/vnd.google-apps.folder",
            "capabilities": {"canAddChildren": True},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_folder_access(GOOGLE_FOLDER_ID, "transient", client)
    assert len(requests) == 1
    assert result.id == GOOGLE_FOLDER_ID
    assert result.name == "KPDesk"
    assert result.is_shared_drive is False
    assert result.can_add_children is True
    assert result.can_create_new_owned_files is False


@pytest.mark.asyncio
async def test_google_api_denial_and_redirect_are_not_treated_as_success() -> None:
    for status in (302, 401, 403, 429):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _, s=status: httpx.Response(s))
        ) as client:
            with pytest.raises(GoogleFolderError):
                await probe_folder_access(GOOGLE_FOLDER_ID, "token", client)


@pytest.mark.asyncio
async def test_invalid_folder_id_never_makes_request() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: pytest.fail("Unexpected network request")
    )) as client:
        with pytest.raises(ValueError):
            await probe_folder_access("../malicious", "token", client)
