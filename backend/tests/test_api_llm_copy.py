"""Customer failure distinctions without a provider, session or database."""
import pytest

from kpdesk.api.errors import ApiError, llm_error_message
from kpdesk.api.universal_routes import _errors
from kpdesk.db.provider_budget import ProviderBudgetError
from kpdesk.llm.client import LLMError, _budget_error


@pytest.mark.parametrize('code,status,meaning', [
    ('invalid_output', 502, 'не прошёл проверку'),
    ('provider_timeout', 504, 'не ответил вовремя'),
    ('unverified_narrative', 422, 'подтвердить факты'),
    ('rate_limited', 429, 'частоту запросов'),
    ('inference_quota_exceeded', 429, 'лимит запросов'),
    ('duplicate_inference_in_progress', 429, 'Повтор запроса к ИИ заблокирован'),
    ('review_context_too_large', 422, 'слишком много данных'),
    ('organization_forbidden', 403, 'Нет доступа'),
    ('quota_ledger_unavailable', 503, 'учёт запроса'),
])
def test_failure_reason_remains_distinct_without_changing_http_contract(
    code: str, status: int, meaning: str,
) -> None:
    with pytest.raises(ApiError) as caught, _errors():
        raise LLMError(code)
    assert caught.value.status_code == status
    assert caught.value.code == code
    assert meaning in caught.value.message
    assert ' или временно недоступен' not in caught.value.message


def test_unknown_error_copy_never_interpolates_untrusted_details() -> None:
    untrusted = 'private-provider-diagnostic-sentinel'
    message = llm_error_message(untrusted)
    assert untrusted not in message
    assert 'проверьте результат' in message.lower()


@pytest.mark.parametrize('reason,code,status', [
    ('forbidden', 'organization_forbidden', 403),
    ('ledger_unavailable', 'quota_ledger_unavailable', 503),
])
def test_budget_authority_and_configuration_failures_are_not_provider_errors(
    reason: str, code: str, status: int,
) -> None:
    failure = _budget_error(ProviderBudgetError(reason))
    with pytest.raises(ApiError) as caught, _errors():
        raise failure
    assert caught.value.code == code
    assert caught.value.status_code == status


@pytest.mark.parametrize('reason', [
    'operation_busy', 'operation_blocked', 'fingerprint_conflict', 'ambiguous',
])
def test_blocked_replay_never_promises_a_future_result(reason: str) -> None:
    failure = _budget_error(ProviderBudgetError(reason))
    with pytest.raises(ApiError) as caught, _errors():
        raise failure
    assert caught.value.status_code == 429
    assert caught.value.code == 'duplicate_inference_in_progress'
    assert 'Повтор запроса к ИИ заблокирован' in caught.value.message
    assert 'Дождитесь' not in caught.value.message
    assert 'Проверьте результат' in caught.value.message
