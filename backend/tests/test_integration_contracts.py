"""Integration gateway tests use fakes only; paid CRM systems are never called."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from kpdesk.api.persisted_quote_routes import get_owner_secret
from kpdesk.integrations.contracts import (
    CAPABILITIES,
    ExchangeIdentity,
    IntegrationGateway,
    IntegrationUnavailable,
    Provider,
    ProviderConnection,
    planned_provider_statuses,
)
from kpdesk.main import create_app


@dataclass
class RecordingTransport:
    fetched: int = 0
    pushed: int = 0
    payloads: list[bytes] = field(default_factory=list)

    async def fetch(
        self, connection: ProviderConnection, operation: str, cursor: str | None,
    ) -> tuple[tuple[bytes, ...], str | None]:
        self.fetched += 1
        return ((b'{"products":[]}',), None)

    async def push(
        self, connection: ProviderConnection, operation: str,
        payload: bytes, key: ExchangeIdentity,
    ) -> str:
        self.pushed += 1
        self.payloads.append(payload)
        return "fake-remote-id"


def test_all_three_provider_statuses_initially_disconnected() -> None:
    states = planned_provider_statuses()
    assert {x["provider"] for x in states} == {"1c", "bitrix24", "amocrm"}
    assert len(states) == len(CAPABILITIES) == 3
    assert all(x["status"] == "not_connected" for x in states)
    assert all(x["live_verified"] is False for x in states)
    assert all(x["tenant_enabled"] is False for x in states)
    for state in states:
        operations = state["planned_operations"]
        assert isinstance(operations, tuple)
        assert "import_products" in operations


@pytest.mark.asyncio
async def test_disabled_provider_cannot_read_or_send_anything() -> None:
    fake = RecordingTransport()
    gateway = IntegrationGateway(fake)
    connection = ProviderConnection(uuid.uuid4(), Provider.BITRIX24)
    key = ExchangeIdentity(
        connection.organization_id, Provider.BITRIX24,
        "export_quote", "Q-001", "rev-1",
    )
    with pytest.raises(IntegrationUnavailable):
        await gateway.import_page(connection, "import_products")
    with pytest.raises(IntegrationUnavailable):
        await gateway.export(connection, "export_quote", b"{}", key)
    assert fake.fetched == fake.pushed == 0


@pytest.mark.asyncio
async def test_cross_tenant_or_provider_export_is_denied_before_transport() -> None:
    fake = RecordingTransport()
    gateway = IntegrationGateway(fake)
    connection = ProviderConnection(
        uuid.uuid4(), Provider.AMOCRM, "connected", True,
    )
    foreign_org = ExchangeIdentity(
        uuid.uuid4(), Provider.AMOCRM,
        "export_quote", "Q-001", "rev-1",
    )
    foreign_provider = ExchangeIdentity(
        connection.organization_id, Provider.BITRIX24,
        "export_quote", "Q-001", "rev-1",
    )
    for identity in (foreign_org, foreign_provider):
        with pytest.raises(IntegrationUnavailable):
            await gateway.export(connection, "export_quote", b"{}", identity)
    assert fake.pushed == 0


@pytest.mark.asyncio
async def test_successful_fake_transport_requires_explicit_connected_tenant() -> None:
    fake = RecordingTransport()
    gateway = IntegrationGateway(fake)
    org = uuid.uuid4()
    connection = ProviderConnection(org, Provider.ONE_C, "connected", True)
    identity = ExchangeIdentity(org, Provider.ONE_C, "export_quote", "quote-1", "2")
    assert len(identity.idempotency_sha256()) == 64
    assert identity.idempotency_sha256() == ExchangeIdentity(
        org, Provider.ONE_C, "export_quote", "quote-1", "2",
    ).idempotency_sha256()
    assert identity.idempotency_sha256() != ExchangeIdentity(
        uuid.uuid4(), Provider.ONE_C, "export_quote", "quote-1", "2",
    ).idempotency_sha256()
    assert await gateway.export(connection, "export_quote", b"{}", identity) == "fake-remote-id"
    assert fake.pushed == 1
    assert await gateway.import_page(connection, "import_products") == (
        (b'{"products":[]}',), None
    )
    assert fake.fetched == 1


def test_readiness_route_owner_only_and_always_no_live_connection() -> None:
    app = create_app(testing=True)
    app.dependency_overrides[get_owner_secret] = lambda: "test-owner-token-" + "x" * 44
    with TestClient(app) as client:
        denied = client.get("/api/integrations/readiness")
        assert denied.status_code == 401
        allowed = client.get(
            "/api/integrations/readiness",
            headers={"X-KPDesk-Token": "test-owner-token-" + "x" * 44},
        )
        assert allowed.status_code == 200
        data = allowed.json()
        assert {x["provider"] for x in data} == {"1c", "bitrix24", "amocrm"}
        assert all(x["status"] == "not_connected" for x in data)
        assert all(x["live_verified"] is False for x in data)
        assert all(x["tenant_enabled"] is False for x in data)
        assert "access_token" not in allowed.text
        assert "refresh_token" not in allowed.text
