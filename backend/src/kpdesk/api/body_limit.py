"""Bound incoming bytes before FastAPI buffers JSON or spools multipart files."""
from __future__ import annotations

from starlette.datastructures import Headers
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kpdesk.api.errors import json_error

_MULTIPART_OVERHEAD = 65_536
_CANONICAL_REVIEW_ALLOWANCE = 4 * 1024 * 1024
_CHECKPOINT_ALLOWANCE = 100_000


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_upload_bytes: int) -> None:
        self.app = app
        self.max_upload_bytes = max_upload_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        allowance = (
            _CANONICAL_REVIEW_ALLOWANCE if path.endswith("/canonical/export") else
            _CHECKPOINT_ALLOWANCE if path.endswith((
                "/checkpoints/save", "/checkpoints/restore", "/checkpoints/append",
                "/checkpoints/restore-page",
            )) else 0
        )
        maximum = self.max_upload_bytes + _MULTIPART_OVERHEAD + allowance
        if path.endswith("/company-profile/logo/prepare"):
            maximum = min(self.max_upload_bytes, 1024 * 1024) + _MULTIPART_OVERHEAD
        declared_length = Headers(scope=scope).get("content-length")
        if declared_length is not None:
            try:
                declared = int(declared_length)
            except ValueError:
                declared = -1
            if declared < 0 or declared > maximum:
                response = json_error(
                    Request(scope), 400 if declared < 0 else 413,
                    "invalid_content_length" if declared < 0 else "file_too_large",
                    "Неверный Content-Length." if declared < 0 else "Файл превышает лимит.",
                )
                await response(scope, receive, send)
                return

        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    scope.setdefault("state", {})["body_limit_exceeded"] = True
                    # Starlette closes every open multipart spool for this
                    # exception. Its parser/JSON wrapper turns it into HTTP 400;
                    # the application handler uses our state flag to return 413.
                    raise MultiPartException("Request body exceeds byte limit.")
            return message

        await self.app(scope, bounded_receive, send)
