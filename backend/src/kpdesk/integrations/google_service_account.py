"""Scoped Service Account authentication for pre-existing Google resources.

No JSON key is sent to a browser or logged. A Service Account has no Drive
storage quota: it may read shared Sheets in My Drive but cannot own new files
there. New PDF/Google Docs exports require OAuth or a Workspace Shared drive.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from kpdesk.catalog.schema import CatalogImportResult
from kpdesk.config import PROJECT_ROOT

GOOGLE_SERVICE_ACCOUNT_EMAIL = "kpdesk@kpdesk.iam.gserviceaccount.com"
GOOGLE_FOLDER_ID = "1abX1xBTfY1AdKRjzxjKGbQkQlK7Gu73U"
_TRUSTED_ROOT = PROJECT_ROOT / "credentials"
_DEFAULT_JSON_PATH = _TRUSTED_ROOT / "google-service-account.json"
_SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_DRIVE_METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
SCOPES = (_SHEETS_READ_SCOPE, _DRIVE_METADATA_SCOPE)
_MAX_SECRET_FILE_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 256 * 1024
_DRIVE_FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")


class GoogleCredentialsError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GoogleFolderError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RefreshableGoogleCredentials(Protocol):
    token: str | None

    def refresh(self, request: GoogleAuthRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class FolderAccess:
    id: str
    name: str
    is_shared_drive: bool
    can_add_children: bool
    can_create_new_owned_files: bool = False


def load_service_credentials(
    path: Path = _DEFAULT_JSON_PATH,
    *,
    trusted_root: Path = _TRUSTED_ROOT,
) -> service_account.Credentials:
    """Only the project's fixed-named local key may authorize Sheets/Drive reads."""
    expected = trusted_root.resolve() / "google-service-account.json"
    if path.name != expected.name or path.resolve() != expected or path.is_symlink():
        raise GoogleCredentialsError("untrusted_path")
    if not path.is_file():
        raise GoogleCredentialsError("not_configured")
    try:
        if not (1 <= path.stat().st_size <= _MAX_SECRET_FILE_BYTES):
            raise GoogleCredentialsError("invalid_credentials")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise GoogleCredentialsError("invalid_credentials")
        if data.get("client_email") != GOOGLE_SERVICE_ACCOUNT_EMAIL:
            raise GoogleCredentialsError("wrong_account")
        if data.get("project_id") != "kpdesk":
            raise GoogleCredentialsError("wrong_project")
        if data.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise GoogleCredentialsError("invalid_credentials")
        loader = cast(
            Callable[..., service_account.Credentials],
            service_account.Credentials.from_service_account_file,
        )
        credentials = loader(str(path), scopes=SCOPES)
    except GoogleCredentialsError:
        raise
    except (ValueError, OSError, TypeError, GoogleAuthError) as error:
        raise GoogleCredentialsError("invalid_credentials") from error
    if credentials.service_account_email != GOOGLE_SERVICE_ACCOUNT_EMAIL:
        raise GoogleCredentialsError("wrong_account")
    return credentials


async def get_service_access_token(credentials: RefreshableGoogleCredentials) -> str:
    """Acquire a short-lived read-only bearer token; never return or log the key."""
    try:
        if not credentials.token:
            await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())
        token = credentials.token
        if not isinstance(token, str) or not token:
            raise GoogleCredentialsError("token_unavailable")
        return token
    except GoogleCredentialsError:
        raise
    except (GoogleAuthError, OSError, ValueError) as error:
        raise GoogleCredentialsError("token_unavailable") from error


async def probe_folder_access(
    folder_id: str, access_token: str, client: httpx.AsyncClient,
) -> FolderAccess:
    """GET folder metadata only. Does not attempt to upload or change permissions."""
    if not _DRIVE_FOLDER_ID.fullmatch(folder_id):
        raise ValueError("Invalid Google folder ID.")
    if not access_token:
        raise GoogleCredentialsError("token_unavailable")
    url = f"https://www.googleapis.com/drive/v3/files/{folder_id}"
    try:
        async with client.stream(
            "GET", url,
            params={"fields": "id,name,mimeType,driveId,capabilities(canAddChildren)"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=False,
        ) as response:
            if response.status_code in (401, 403):
                raise GoogleFolderError("access_denied")
            if response.status_code == 404:
                raise GoogleFolderError("folder_not_found")
            if response.status_code == 429:
                raise GoogleFolderError("rate_limited")
            if response.status_code != 200:
                raise GoogleFolderError("provider_error")
            chunks: list[bytes] = []
            size = 0
            async for part in response.aiter_bytes(65536):
                size += len(part)
                if size > _MAX_METADATA_BYTES:
                    raise GoogleFolderError("response_too_large")
                chunks.append(part)
    except httpx.TimeoutException as error:
        raise GoogleFolderError("provider_timeout") from error
    except httpx.RequestError as error:
        raise GoogleFolderError("provider_unreachable") from error

    try:
        content = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise GoogleFolderError("invalid_response") from error
    if not isinstance(content, dict) or content.get("id") != folder_id:
        raise GoogleFolderError("invalid_response")
    if content.get("mimeType") != "application/vnd.google-apps.folder":
        raise GoogleFolderError("invalid_resource")
    capabilities = content.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    can_add = capabilities.get("canAddChildren") is True
    drive_id = content.get("driveId")
    shared_drive = isinstance(drive_id, str) and bool(drive_id)
    name = content.get("name")
    if not isinstance(name, str):
        raise GoogleFolderError("invalid_response")
    return FolderAccess(
        id=folder_id,
        name=name,
        is_shared_drive=shared_drive,
        can_add_children=can_add,
        can_create_new_owned_files=False,
    )



async def fetch_shared_sheet_catalog(
    spreadsheet_id: str,
    credentials: RefreshableGoogleCredentials,
    client: httpx.AsyncClient,
) -> CatalogImportResult:
    """Get an ephemeral Service Account token and validate a shared Sheets price list.

    Does not publish to PostgreSQL, edit the spreadsheet, or create Drive files.
    """
    from kpdesk.integrations.google_sheets import fetch_sheet_catalog

    token = await get_service_access_token(credentials)
    return await fetch_sheet_catalog(spreadsheet_id, token, client)
