"""No provider calls: preserve bounded technical429 evidence without identifiers."""
import json

import httpx
from canonical_groq_diagnostics import rate_limit_diagnostics


def test_429_preserves_unit_budget_and_wait_without_provider_message() -> None:
    payload = {'error': {'code': 'rate_limit_exceeded', 'message':
        'Rate limit reached in organization org_PRIVATE on tokens per minute (TPM): '
        'Limit 8000, Used 7500, Requested 2000. Contact customer_PRIVATE.'}}
    response = httpx.Response(429, headers={'retry-after': '12',
        'x-ratelimit-limit-tokens': '8000', 'x-ratelimit-remaining-tokens': '500',
        'x-ratelimit-reset-tokens': '12.5s', 'authorization': 'Bearer PRIVATE_KEY'})
    result = rate_limit_diagnostics(response, payload)
    assert result['provider_error_code'] == 'rate_limit_exceeded'
    assert result['limit_unit'] == 'tokens_per_minute'
    assert result['retry_after_seconds'] == 12
    assert result['limit'] == 8000 and result['used'] == 7500 and result['requested'] == 2000
    assert result['headers']['x-ratelimit-remaining-tokens'] == '500'
    assert 'PRIVATE' not in json.dumps(result)


def test_success_keeps_budget_headers_for_the_next_call_without_copying_content() -> None:
    response = httpx.Response(200, headers={'x-ratelimit-limit-requests': '30',
        'x-ratelimit-remaining-requests': '29', 'x-ratelimit-reset-requests': '2m59.56s'})
    result = rate_limit_diagnostics(response, {'choices': [{'content': 'CUSTOMER_PRIVATE'}]})
    assert result['headers']['x-ratelimit-remaining-requests'] == '29'
    assert 'limit_unit' not in result and 'CUSTOMER_PRIVATE' not in json.dumps(result)


def test_unknown_error_codes_and_invalid_header_values_are_not_copied() -> None:
    response = httpx.Response(429, headers={'x-ratelimit-limit-tokens': 'org_PRIVATE',
        'x-ratelimit-reset-tokens': '9' * 100, 'retry-after': 'PRIVATE', 'x-secret': 'PRIVATE'})
    result = rate_limit_diagnostics(response, {'error': {'code': 'PRIVATE', 'message': 'PRIVATE'}})
    assert result == {'provider_error_code': 'other', 'retry_after_seconds': None}


def test_bare_429_does_not_invent_a_specific_limit() -> None:
    result = rate_limit_diagnostics(httpx.Response(429), {})
    assert result == {'provider_error_code': 'other', 'retry_after_seconds': None}


def test_success_without_metadata_has_no_diagnostics() -> None:
    assert rate_limit_diagnostics(httpx.Response(200), []) == {}
