"""Read-only DaData company lookup; no persistence or provider writes.

Cache, rolling 24-hour upstream budget and per-organization minute limits are
process-local and bounded. Multiple server processes have independent limits;
an account-wide provider quota still needs operational enforcement.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from kpdesk.company.requisites import (
    PartyDetails as PartyDetails,  # noqa: PLC0414 - compatibility export
)
from kpdesk.company.requisites import (
    validate_inn as validate_inn,  # noqa: PLC0414 - compatibility export
)
from kpdesk.config import PROJECT_ROOT

_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ENTRIES = 1024
_CACHE_TTL = timedelta(hours=24)


class CompanyLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    party: PartyDetails | None
    source: Literal["dadata"] = "dadata"
    fetched_at: datetime
    cached: bool = False


class LookupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr | None = Field(default=None, repr=False)

    @classmethod
    def from_environment(cls) -> LookupSettings:
        key = os.environ.get("KP_DESK_DADATA_API_KEY")
        if key is None:
            key = dotenv_values(PROJECT_ROOT / ".env", interpolate=False).get(
                "KP_DESK_DADATA_API_KEY"
            )
        return cls(api_key=SecretStr(key.strip()) if isinstance(key, str) and key.strip() else None)


class CompanyLookupError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _invalid_response() -> CompanyLookupError:
    return CompanyLookupError(
        502, "company_lookup_invalid_response", "Не удалось получить корректные реквизиты. Повторите поиск позже."
    )


def _party(payload: object, inn: str) -> PartyDetails | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("suggestions"), list):
        raise _invalid_response()
    suggestions = payload["suggestions"]
    if not suggestions:
        return None
    if len(suggestions) != 1 or not isinstance(suggestions[0], dict):
        raise _invalid_response()
    data = suggestions[0].get("data")
    expected_type = "LEGAL" if len(inn) == 10 else "INDIVIDUAL"
    if (
        not isinstance(data, dict) or data.get("inn") != inn
        or data.get("branch_type") != "MAIN" or data.get("type") != expected_type
    ):
        raise _invalid_response()
    # A 12-digit INN alone identifies a person, not a registered entrepreneur.
    if expected_type == "INDIVIDUAL" and data.get("ogrn") is None:
        return None
    ogrn = data.get("ogrn")
    if ogrn is not None and (
        not isinstance(ogrn, str) or len(ogrn) != (13 if expected_type == "LEGAL" else 15)
    ):
        raise _invalid_response()
    name = data.get("name")
    address = data.get("address")
    state = data.get("state")
    if (
        not isinstance(name, dict)
        or address is not None and not isinstance(address, dict)
        or state is not None and not isinstance(state, dict)
    ):
        raise _invalid_response()
    try:
        return PartyDetails.model_validate({
            "full_name": name.get("full_with_opf"), "short_name": name.get("short_with_opf"),
            "inn": inn, "kpp": data.get("kpp"), "ogrn": ogrn,
            "address": address.get("unrestricted_value") if address else None,
            "status": state.get("status") if state else None,
        })
    except ValidationError as error:
        raise _invalid_response() from error


class CompanyLookupService:
    def __init__(
        self, settings: LookupSettings, *, transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        org_per_minute: int = 10, daily_upstream_limit: int = 1000,
    ) -> None:
        if not 1 <= org_per_minute <= 10 or not 1 <= daily_upstream_limit <= 1000:
            raise ValueError("Lookup limits exceed supported bounds.")
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self._org_limit = org_per_minute
        self._daily_limit = daily_upstream_limit
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[uuid.UUID, str], CompanyLookupResult] = OrderedDict()
        self._org_requests: dict[uuid.UUID, deque[datetime]] = {}
        self._upstream_requests: deque[datetime] = deque()

    def lookup(self, organization_id: uuid.UUID, inn: str) -> CompanyLookupResult:
        validate_inn(inn)
        key = self._settings.api_key
        if key is None or not key.get_secret_value().strip():
            raise CompanyLookupError(503, "company_lookup_not_configured",
                                     "Поиск реквизитов по ИНН не настроен. Обратитесь к администратору KPDesk.")
        now = self._clock()
        cache_key = (organization_id, inn)
        with self._lock:
            self._take_org_slot(organization_id, now)
            for expired in [entry for entry, result in self._cache.items()
                            if now - result.fetched_at >= _CACHE_TTL]:
                del self._cache[expired]
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached.model_copy(update={"cached": True})
            while self._upstream_requests and now - self._upstream_requests[0] >= _CACHE_TTL:
                self._upstream_requests.popleft()
            if len(self._upstream_requests) >= self._daily_limit:
                raise CompanyLookupError(429, "company_lookup_daily_limit",
                                         "Дневной лимит поиска реквизитов исчерпан.")
            # Charge attempts before network, never refund failures or rollbacks.
            self._upstream_requests.append(now)
        result = CompanyLookupResult(party=self._fetch(inn, key.get_secret_value()),
                                     fetched_at=self._clock())
        with self._lock:
            previous = self._cache.get(cache_key)
            if previous is None or previous.fetched_at <= result.fetched_at:
                self._cache[cache_key] = result
                self._cache.move_to_end(cache_key)
            while len(self._cache) > _MAX_ENTRIES:
                self._cache.popitem(last=False)
        return result

    def _take_org_slot(self, organization_id: uuid.UUID, now: datetime) -> None:
        for org in list(self._org_requests):
            window = self._org_requests[org]
            while window and now - window[0] >= timedelta(minutes=1):
                window.popleft()
            if not window:
                del self._org_requests[org]
        if organization_id not in self._org_requests and len(self._org_requests) >= _MAX_ENTRIES:
            raise CompanyLookupError(429, "company_lookup_rate_limited",
                                     "Слишком много запросов реквизитов. Повторите через минуту.")
        window = self._org_requests.setdefault(organization_id, deque())
        if len(window) >= self._org_limit:
            raise CompanyLookupError(429, "company_lookup_rate_limited",
                                     "Слишком много запросов реквизитов. Повторите через минуту.")
        window.append(now)

    def _fetch(self, inn: str, key: str) -> PartyDetails | None:
        try:
            with httpx.Client(
                transport=self._transport, follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(5.0, connect=3.0, pool=3.0),
            ) as client, client.stream(
                "POST", _URL, headers={"Authorization": f"Token {key}", "Accept": "application/json"},
                json={"query": inn, "count": 1, "branch_type": "MAIN",
                      "type": "LEGAL" if len(inn) == 10 else "INDIVIDUAL"},
            ) as response:
                if response.status_code == 429:
                    raise CompanyLookupError(429, "company_lookup_provider_limit",
                                             "Лимит сервиса реквизитов исчерпан. Повторите позже.")
                if response.status_code in {401, 403}:
                    raise CompanyLookupError(503, "company_lookup_provider_unavailable",
                                             "Подключение к сервису реквизитов требует проверки. Обратитесь к администратору KPDesk.")
                if response.status_code != 200:
                    raise CompanyLookupError(502, "company_lookup_provider_unavailable",
                                             "Сервис реквизитов временно недоступен.")
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise _invalid_response()
                    content.extend(chunk)
                try:
                    payload: object = json.loads(content)
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise _invalid_response() from error
                return _party(payload, inn)
        except httpx.HTTPError as error:
            raise CompanyLookupError(503, "company_lookup_provider_unavailable",
                                     "Сервис реквизитов не отвечает. Повторите позже.") from error
