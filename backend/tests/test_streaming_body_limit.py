"""Reject oversized streams before multipart spooling or JSON accumulation."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import ExitStack
from tempfile import SpooledTemporaryFile
from unittest.mock import Mock

import httpx
import pytest
import starlette.formparsers

from kpdesk.api.routes import get_catalog_repository
from kpdesk.config import AppSettings
from kpdesk.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, "0"])
async def test_streamed_multipart_stops_at_limit_and_closes_tempfiles(
    monkeypatch: pytest.MonkeyPatch, declared_length: str | None,
) -> None:
    with ExitStack() as cleanup:
        app = create_app(testing=True, settings=AppSettings(max_upload_bytes=1024))
        repository = Mock()
        app.dependency_overrides[get_catalog_repository] = lambda: repository
        files: list[SpooledTemporaryFile[bytes]] = []

        def tempfile(*, max_size: int) -> SpooledTemporaryFile[bytes]:
            result: SpooledTemporaryFile[bytes] = cleanup.enter_context(SpooledTemporaryFile(max_size=max_size))
            files.append(result)
            return result

        monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tempfile)
        chunks_read = 0

        async def content() -> AsyncIterator[bytes]:
            nonlocal chunks_read
            yield (
                b'--audit\r\nContent-Disposition: form-data; name="file"; '
                b'filename="price.csv"\r\nContent-Type: text/csv\r\n\r\n'
            )
            for _ in range(20):
                chunks_read += 1
                yield b"x" * 8192
            yield b"\r\n--audit--\r\n"

        headers = {
            "Origin": "http://localhost:5173", "Idempotency-Key": "stream-test",
            "Content-Type": "multipart/form-data; boundary=audit",
        }
        if declared_length is not None:
            headers["Content-Length"] = declared_length
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            response = await client.post("/api/catalog/import", headers=headers, content=content())
        assert response.status_code == 413
        assert chunks_read <= 9, "Oversized data reached the multipart spool before rejection"
        assert response.json()["code"] == "file_too_large"
        assert files and all(file.closed for file in files)
        repository.publish.assert_not_called()


@pytest.mark.asyncio
async def test_streamed_json_is_bounded_before_parsing_or_catalog_access() -> None:
    app = create_app(testing=True, settings=AppSettings(max_upload_bytes=1024))
    repository = Mock()
    app.dependency_overrides[get_catalog_repository] = lambda: repository
    chunks_read = 0

    async def content() -> AsyncIterator[bytes]:
        nonlocal chunks_read
        yield b'{"requested_item":{"description":"'
        for _ in range(20):
            chunks_read += 1
            yield b"x" * 8192
        yield b'","category":"paper"}}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/matches/preview", content=content(),
            headers={"Origin": "http://localhost:5173", "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert chunks_read <= 9
    repository.current.assert_not_called()
