from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, Unpack

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from kpdesk.company.lookup import (
    CompanyLookupError,
    CompanyLookupService,
    LookupSettings,
    PartyDetails,
    validate_inn,
)

INN = "7707083893"
IP_INN = "500100732259"
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def provider_party(inn: str = INN) -> dict[str, Any]:
    return {"suggestions": [{"value": "ignored", "data": {
        "inn": inn, "type": "LEGAL" if len(inn) == 10 else "INDIVIDUAL",
        "branch_type": "MAIN", "kpp": "773601001" if len(inn) == 10 else None,
        "ogrn": "1027700132195" if len(inn) == 10 else "304500116000157",
        "name": {"full_with_opf": "ПАО\u0000 СБЕРБАНК", "short_with_opf": "Сбербанк"},
        "address": {"unrestricted_value": "Москва, ул. Вавилова, 19"},
        "state": {"status": "ACTIVE"},
        "management": {"name": "SECRET PERSON"}, "founders": ["SECRET PERSON"],
    }}]}


class ServiceOptions(TypedDict, total=False):
    clock: Callable[[], datetime]
    org_per_minute: int
    daily_upstream_limit: int


def service(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Unpack[ServiceOptions],
) -> CompanyLookupService:
    return CompanyLookupService(
        LookupSettings(api_key=SecretStr("offline-test-key")),
        transport=httpx.MockTransport(handler), **kwargs,
    )


@pytest.mark.parametrize("inn", [INN, IP_INN])
def test_inn_checksum_accepts_legal_and_individual_entrepreneur(inn: str) -> None:
    assert validate_inn(inn) == inn


@pytest.mark.parametrize("inn", ["7707083894", "500100732258", "123", "７７０７０８３８９３",
                                   "0000000000", " 7707083893", "7707083893\n"])
def test_inn_checksum_rejects_invalid_identifiers(inn: str) -> None:
    with pytest.raises(ValueError):
        validate_inn(inn)


def test_party_contract_rejects_unknown_fields_and_overlong_names() -> None:
    with pytest.raises(ValidationError):
        PartyDetails(full_name="x" * 501, short_name="x", inn=INN)
    with pytest.raises(ValidationError):
        PartyDetails.model_validate({"full_name": "x", "short_name": "x", "inn": INN,
                                     "director": "private"})


def test_lookup_uses_fixed_post_and_exposes_only_minimal_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
        )
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Token offline-test-key"
        assert json.loads(request.content) == {
            "query": INN, "count": 1, "branch_type": "MAIN", "type": "LEGAL",
        }
        return httpx.Response(200, json=provider_party())

    result = service(handler).lookup(ORG, INN)
    assert not result.cached
    assert result.source == "dadata"
    assert result.party is not None
    assert result.party.model_dump() == {
        "full_name": "ПАО СБЕРБАНК", "short_name": "Сбербанк", "inn": INN,
        "kpp": "773601001", "ogrn": "1027700132195",
        "address": "Москва, ул. Вавилова, 19", "status": "ACTIVE",
    }
    assert "SECRET" not in result.model_dump_json()


def test_individual_query_and_no_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["type"] == "INDIVIDUAL"
        return httpx.Response(200, json={"suggestions": []})

    assert service(handler).lookup(ORG, IP_INN).party is None


@pytest.mark.parametrize("change", [
    {"inn": "7707083894"}, {"inn": "500100732259"}, {"branch_type": "BRANCH"},
    {"type": "INDIVIDUAL"}, {"name": {"full_with_opf": "x" * 501}},
    {"kpp": "1"}, {"ogrn": "1"}, {"name": None},
])
def test_untrusted_provider_party_is_rejected(change: dict[str, object]) -> None:
    payload = provider_party()
    payload["suggestions"][0]["data"].update(change)
    with pytest.raises(CompanyLookupError) as error:
        service(lambda _: httpx.Response(200, json=payload)).lookup(ORG, INN)
    assert error.value.status_code == 502


@pytest.mark.parametrize("payload", [{}, {"suggestions": "bad"}, {"suggestions": [{}]}])
def test_malformed_provider_response_is_rejected(payload: object) -> None:
    with pytest.raises(CompanyLookupError) as error:
        service(lambda _: httpx.Response(200, json=payload)).lookup(ORG, INN)
    assert error.value.code == "company_lookup_invalid_response"


@pytest.mark.parametrize("status,expected", [(403, 503), (429, 429), (302, 502), (500, 502)])
def test_provider_errors_do_not_leak_bodies_or_follow_redirects(status: int, expected: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="SECRET BODY", headers={"Location": "https://evil.test"})

    with pytest.raises(CompanyLookupError) as error:
        service(handler).lookup(ORG, INN)
    assert error.value.status_code == expected
    assert "SECRET" not in str(error.value)
    assert calls == 1


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("secret"), httpx.ConnectError("secret")])
def test_transport_failures_are_clean(exception: httpx.HTTPError) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exception

    with pytest.raises(CompanyLookupError) as error:
        service(handler).lookup(ORG, INN)
    assert error.value.status_code == 503
    assert "secret" not in str(error.value)


def test_missing_settings_fail_before_any_network() -> None:
    lookup = CompanyLookupService(LookupSettings(api_key=None))
    with pytest.raises(CompanyLookupError) as error:
        lookup.lookup(ORG, INN)
    assert error.value.status_code == 503
    assert error.value.code == "company_lookup_not_configured"


def test_cache_is_org_scoped_keeps_original_timestamp_and_expires() -> None:
    calls = 0
    now = datetime(2026, 10, 28, tzinfo=UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=provider_party())

    lookup = service(handler, clock=lambda: now)
    first = lookup.lookup(ORG, INN)
    now += timedelta(seconds=30)
    cached = lookup.lookup(ORG, INN)
    assert cached.cached and cached.fetched_at == first.fetched_at
    lookup.lookup(uuid.uuid4(), INN)
    assert calls == 2
    now += timedelta(days=1)
    assert not lookup.lookup(ORG, INN).cached
    assert calls == 3


def test_org_limiter_applies_to_cached_requests_and_daily_budget_is_not_refunded() -> None:
    lookup = service(lambda _: httpx.Response(200, json=provider_party()), org_per_minute=2)
    lookup.lookup(ORG, INN)
    lookup.lookup(ORG, INN)
    with pytest.raises(CompanyLookupError) as error:
        lookup.lookup(ORG, INN)
    assert error.value.code == "company_lookup_rate_limited"
    lookup = service(lambda _: httpx.Response(500), daily_upstream_limit=1)
    with pytest.raises(CompanyLookupError):
        lookup.lookup(ORG, INN)
    with pytest.raises(CompanyLookupError) as error:
        lookup.lookup(uuid.uuid4(), INN)
    assert error.value.code == "company_lookup_daily_limit"


def test_oversized_response_is_rejected() -> None:
    with pytest.raises(CompanyLookupError) as error:
        service(lambda _: httpx.Response(200, content=b" " * (1024 * 1024 + 1))).lookup(ORG, INN)
    assert error.value.code == "company_lookup_invalid_response"


def test_individual_without_registered_ogrn_returns_no_party() -> None:
    payload = provider_party(IP_INN)
    payload["suggestions"][0]["data"]["ogrn"] = None
    assert service(lambda _: httpx.Response(200, json=payload)).lookup(ORG, IP_INN).party is None


def test_settings_use_server_environment_then_root_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kpdesk.company.lookup.PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("KP_DESK_DADATA_API_KEY", raising=False)
    assert LookupSettings.from_environment().api_key is None
    (tmp_path / ".env").write_text("KP_DESK_DADATA_API_KEY=offline-file\n", encoding="utf-8")
    file_key = LookupSettings.from_environment().api_key
    assert file_key is not None and file_key.get_secret_value() == "offline-file"
    monkeypatch.setenv("KP_DESK_DADATA_API_KEY", "offline-process")
    settings = LookupSettings.from_environment()
    assert settings.api_key is not None and settings.api_key.get_secret_value() == "offline-process"
    assert "offline-process" not in repr(settings)
    monkeypatch.setenv("KP_DESK_DADATA_API_KEY", "")
    assert LookupSettings.from_environment().api_key is None


def test_parallel_requests_share_one_atomic_daily_budget() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"suggestions": []})

    lookup = service(handler, daily_upstream_limit=1)

    def attempt(_: int) -> str:
        try:
            lookup.lookup(uuid.uuid4(), INN)
            return "ok"
        except CompanyLookupError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert results.count("ok") == 1
    assert results.count("company_lookup_daily_limit") == 19
    assert len(calls) == 1


def test_org_window_capacity_fails_closed_and_recovers_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kpdesk.company.lookup._MAX_ENTRIES", 2)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    lookup = service(lambda _: httpx.Response(200, json={"suggestions": []}), clock=lambda: now)
    lookup.lookup(ORG, INN)
    lookup.lookup(uuid.uuid4(), INN)
    with pytest.raises(CompanyLookupError) as error:
        lookup.lookup(uuid.uuid4(), INN)
    assert error.value.code == "company_lookup_rate_limited"
    now += timedelta(minutes=1)
    assert lookup.lookup(uuid.uuid4(), INN).party is None


def test_cache_evicts_oldest_entries_at_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kpdesk.company.lookup._MAX_ENTRIES", 2)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    lookup = service(lambda _: httpx.Response(200, json={"suggestions": []}), clock=lambda: now)
    for org in [ORG, uuid.uuid4(), uuid.uuid4()]:
        now += timedelta(minutes=1)
        lookup.lookup(org, INN)
    now += timedelta(minutes=1)
    assert not lookup.lookup(ORG, INN).cached
