"""Provider readiness. Does not initiate OAuth, paid CRM requests or 1C writes."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from kpdesk.api.persisted_quote_routes import require_owner
from kpdesk.integrations.contracts import planned_provider_statuses

router = APIRouter(
    prefix="/api/integrations",
    dependencies=[Depends(require_owner)],
)


class ProviderReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    status: str
    tenant_enabled: bool
    live_verified: bool
    auth_modes: tuple[str, ...]
    planned_operations: tuple[str, ...]
    configuration_dependent: bool


@router.get("/readiness", response_model=list[ProviderReadiness])
def integration_readiness() -> list[ProviderReadiness]:
    # No arbitrary client-provided URL, tokens, provider credentials or network.
    return [ProviderReadiness.model_validate(row) for row in planned_provider_statuses()]
