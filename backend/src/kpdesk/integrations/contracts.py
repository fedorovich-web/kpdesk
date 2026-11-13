"""Explicitly unconnected 1C/Bitrix24/amoCRM integration contracts.

This module defines offline capabilities and the eventual tenant-aware sync
boundary. It NEVER creates provider credentials, tokens, paid accounts, network
calls, CRM leads/deals, 1C documents or catalog publications.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class Provider(StrEnum):
    ONE_C = "1c"
    BITRIX24 = "bitrix24"
    AMOCRM = "amocrm"


type ExchangeOperation = Literal[
    "import_products", "import_requests", "export_quote",
    "sync_quote_status",
]
type ConnectionStatus = Literal[
    "not_connected", "awaiting_authorization", "connected", "error",
]


class IntegrationUnavailable(RuntimeError):
    """Provider must be connected and explicitly enabled by its tenant."""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: Provider
    auth_modes: tuple[str, ...]
    planned_operations: tuple[ExchangeOperation, ...]
    configuration_dependent: bool
    live_verified: bool = False


CAPABILITIES: tuple[ProviderCapabilities, ...] = (
    ProviderCapabilities(
        Provider.ONE_C, ("manual_file_exchange", "odata", "http_service"),
        ("import_products", "import_requests", "export_quote", "sync_quote_status"),
        True,
    ),
    ProviderCapabilities(
        Provider.BITRIX24, ("oauth2", "inbound_webhook"),
        ("import_products", "import_requests", "export_quote", "sync_quote_status"),
        True,
    ),
    ProviderCapabilities(
        Provider.AMOCRM, ("oauth2",),
        ("import_products", "import_requests", "export_quote", "sync_quote_status"),
        True,
    ),
)


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    organization_id: uuid.UUID
    provider: Provider
    status: ConnectionStatus = "not_connected"
    tenant_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.organization_id, uuid.UUID) or not isinstance(
            self.provider, Provider
        ):
            raise TypeError("An integration must have a valid tenant/provider identity.")
        if self.status not in (
            "not_connected", "awaiting_authorization", "connected", "error",
        ):
            raise ValueError("Unknown provider connection state.")

    def require_ready(self) -> None:
        if self.status != "connected" or not self.tenant_enabled:
            raise IntegrationUnavailable("Integration is not connected and authorized.")


@dataclass(frozen=True, slots=True)
class ExchangeIdentity:
    """Idempotency identity, never a proof of tenant membership."""

    organization_id: uuid.UUID
    provider: Provider
    operation: ExchangeOperation
    external_id: str
    external_revision: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.organization_id, uuid.UUID)
            or not isinstance(self.provider, Provider)
            or self.operation not in (
                "import_products", "import_requests", "export_quote", "sync_quote_status",
            )
            or not 1 <= len(self.external_id) <= 256
            or not 1 <= len(self.external_revision) <= 128
        ):
            raise ValueError("Invalid external synchronization identity.")

    def idempotency_sha256(self) -> str:
        encoded = "\x1f".join((
            str(self.organization_id), self.provider.value, self.operation,
            self.external_id, self.external_revision,
        )).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ProviderTransport(Protocol):
    """Future adapter contract. Authentication, timeout and retry are server-only."""

    async def fetch(
        self,
        connection: ProviderConnection,
        operation: ExchangeOperation,
        cursor: str | None,
    ) -> tuple[tuple[bytes, ...], str | None]: ...

    async def push(
        self,
        connection: ProviderConnection,
        operation: ExchangeOperation,
        payload: bytes,
        key: ExchangeIdentity,
    ) -> str: ...


def planned_provider_statuses() -> tuple[dict[str, object], ...]:
    """Safe status for local owner UI before any provider accounts exist."""
    return tuple({
        "provider": item.provider.value,
        "status": "not_connected",
        "tenant_enabled": False,
        "live_verified": False,
        "auth_modes": item.auth_modes,
        "planned_operations": item.planned_operations,
        "configuration_dependent": item.configuration_dependent,
    } for item in CAPABILITIES)


@dataclass(frozen=True, slots=True)
class IntegrationGateway:
    """Strict boundary used by future external API adapter implementations."""

    transport: ProviderTransport

    async def import_page(
        self,
        connection: ProviderConnection,
        operation: ExchangeOperation,
        cursor: str | None = None,
    ) -> tuple[tuple[bytes, ...], str | None]:
        connection.require_ready()
        if operation not in ("import_products", "import_requests"):
            raise ValueError("Only explicit inbound operations may fetch provider data.")
        if cursor is not None and (not cursor or len(cursor) > 512):
            raise ValueError("Invalid pagination cursor.")
        return await self.transport.fetch(connection, operation, cursor)

    async def export(
        self,
        connection: ProviderConnection,
        operation: ExchangeOperation,
        payload: bytes,
        identity: ExchangeIdentity,
    ) -> str:
        connection.require_ready()
        if operation not in ("export_quote", "sync_quote_status"):
            raise ValueError("Only explicit outbound operations may mutate external data.")
        if (
            identity.organization_id != connection.organization_id
            or identity.provider != connection.provider
            or identity.operation != operation
        ):
            raise IntegrationUnavailable("Cross-organization/provider sync blocked.")
        if not 1 <= len(payload) <= 1_048_576:
            raise ValueError("External payload size is invalid.")
        return await self.transport.push(connection, operation, payload, identity)
