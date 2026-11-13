"""Bounded acceptance diagnostics; never persist arbitrary provider bodies/IDs."""
import re
from typing import Any

import httpx

from kpdesk.extraction.groq_telemetry import retry_after_seconds

_HEADERS = frozenset({
    'x-ratelimit-limit-requests', 'x-ratelimit-limit-tokens',
    'x-ratelimit-remaining-requests', 'x-ratelimit-remaining-tokens',
    'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens',
})
_VALUE = re.compile(r'[0-9.smhd]{1,64}\Z')
_UNIT = re.compile(r'\bon (tokens|requests) per (minute|day)\b', re.IGNORECASE)


def rate_limit_diagnostics(response: httpx.Response, payload: object) -> dict[str, Any]:
    result: dict[str, Any] = {}
    headers = {name: value for name in sorted(_HEADERS)
               if (value := response.headers.get(name)) is not None and _VALUE.fullmatch(value)}
    if headers:
        result['headers'] = headers
    if response.status_code != 429:
        return result
    error = payload.get('error') if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get('code')
    result['provider_error_code'] = code if code in ('rate_limit_exceeded', 'rate_limit_error') else 'other'
    result['retry_after_seconds'] = retry_after_seconds(response.headers.get('retry-after'))
    message = error.get('message')
    if isinstance(message, str) and len(message) <= 16_384:
        unit = _UNIT.search(message)
        if unit:
            result['limit_unit'] = unit[1].lower() + '_per_' + unit[2].lower()
            for label in ('Limit', 'Used', 'Requested'):
                match = re.search(r'\b' + label + r'[: ]+(\d{1,12})(?!\d|[.,]\d)', message, re.IGNORECASE)
                if match:
                    result[label.lower()] = int(match[1])
    return result
