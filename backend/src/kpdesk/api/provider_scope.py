"""Authenticated tenant operation identity and injectable shared ledger factory."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine

from kpdesk.api.errors import ApiError
from kpdesk.db.provider_budget import ProviderLimits, SharedProviderBudget
from kpdesk.llm.client import AttemptBudget
from kpdesk.llm.settings import LLMSettings

type BudgetFactory = Callable[..., AttemptBudget]


def get_provider_budget_factory() -> BudgetFactory:
    return SharedProviderBudget


def operation_budget(factory: BudgetFactory, engine: Engine, *, organization_id: UUID,
                     cookie: str | None, csrf: str | None, key: str | None,
                     purpose: str, payload: str, settings: LLMSettings) -> AttemptBudget:
    if not settings.processing_approved:
        raise ApiError(503, "synthetic_project_required", "Разрешён только синтетический тестовый проект.")
    if key is not None:
        try:
            operation_id = UUID(key)
        except ValueError as error:
            raise ApiError(422, "invalid_idempotency_key", "Нужен UUID ключ операции.") from error
    else:
        # Old clients are conservatively deduplicated even after uncertain results.
        operation_id = uuid5(NAMESPACE_URL, str(organization_id) + purpose + payload)
    fingerprint = hashlib.sha256(("openrouter:" + settings.model + ":" + purpose + ":" + payload)
                                 .encode()).hexdigest()
    return factory(engine, organization_id=organization_id, session_cookie=cookie or "",
        csrf_token=csrf or "", operation_id=operation_id, fingerprint=fingerprint,
        limits=ProviderLimits(settings.requests_per_minute, settings.daily_request_limit,
                              settings.max_parallel, settings.max_calls_per_quote))
