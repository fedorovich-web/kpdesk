"""Privacy-preserving diagnostic metadata for a single provider inference attempt.

Never includes request/response content, Authorization, user identity or arbitrary
provider-controlled headers. Not a billable-token ledger or quota source.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_LOG = logging.getLogger("kpdesk.llm")
_FINISH_REASONS = frozenset(("stop", "length", "content_filter", "tool_calls"))
_ERROR_CODES = frozenset((
    "json_validate_failed", "invalid_json_schema", "invalid_request_error",
    "context_length_exceeded", "model_not_found", "server_error",
))


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CallMetrics:
    model: str
    outcome: str
    elapsed_ms: int
    http_status: int | None
    finish_reason: str | None
    usage: Usage | None
    retry_after_seconds: int | None
    provider_error_code: str | None = None


def safe_provider_error_code(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return "other"
    error = value["error"]
    for field in ("code", "type"):
        code = error.get(field)
        if isinstance(code, str) and code in _ERROR_CODES:
            return code
    return "other"


def _token_count(value: object) -> int | None:
    if type(value) is int and 0 <= value <= 100_000_000:
        return value
    return None


def parse_usage(value: object) -> Usage | None:
    if not isinstance(value, dict):
        return None
    prompt = _token_count(value.get("prompt_tokens"))
    completion = _token_count(value.get("completion_tokens"))
    total = _token_count(value.get("total_tokens"))
    if prompt is None or completion is None or total is None:
        return None
    if prompt + completion > total:
        return None
    details = value.get("completion_tokens_details")
    reasoning: int | None = None
    if isinstance(details, dict):
        reasoning = _token_count(details.get("reasoning_tokens"))
        if reasoning is not None and reasoning > completion:
            reasoning = None
    return Usage(prompt, completion, total, reasoning)


def safe_finish_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in _FINISH_REASONS else "other"


def retry_after_seconds(value: str | None) -> int | None:
    if value is None or len(value) > 64:
        return None
    if value.isascii() and value.isdecimal():
        return min(int(value[:10]), 3600) if len(value) <= 10 else 3600
    try:
        moment = parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        return None
    delta = (moment - datetime.now(UTC)).total_seconds()
    return max(0, min(3600, int(delta) + (delta % 1 > 0)))


def emit_safe_metrics(metrics: CallMetrics) -> None:
    """Fixed diagnostic vocabulary: no arbitrary exception or provider body."""
    _LOG.info(
        "openrouter_inference model=%s outcome=%s http_status=%s elapsed_ms=%d "
        "finish_reason=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s "
        "reasoning_tokens=%s retry_after_seconds=%s provider_error_code=%s",
        metrics.model, metrics.outcome, metrics.http_status, metrics.elapsed_ms,
        metrics.finish_reason,
        metrics.usage.prompt_tokens if metrics.usage else None,
        metrics.usage.completion_tokens if metrics.usage else None,
        metrics.usage.total_tokens if metrics.usage else None,
        metrics.usage.reasoning_tokens if metrics.usage else None,
        metrics.retry_after_seconds,
        metrics.provider_error_code,
    )
