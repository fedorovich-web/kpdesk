"""Fixed-origin Yandex ID exchange; transient tokens, bounded reads and no retries."""
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from kpdesk.db.auth_sessions import LoginDenied, _normalized_email
from kpdesk.yandex.settings import YandexSettings

_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
_VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}\Z')


class YandexError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class YandexProfile:
    subject: str = field(repr=False)
    client_id: str
    email: str | None = field(repr=False)
    name: str | None = field(repr=False)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError('invalid_json_constant')


class YandexClient:
    def __init__(self, settings: YandexSettings, http: httpx.AsyncClient | None = None) -> None:
        self.settings, self.http = settings, http

    def _enabled(self) -> None:
        if not self.settings.enabled:
            raise YandexError('yandex_disabled')

    def authorization_url(self, state: str, code_challenge: str) -> str:
        self._enabled()
        if not _TOKEN.fullmatch(state) or not _TOKEN.fullmatch(code_challenge):
            raise YandexError('yandex_request_invalid')
        return 'https://oauth.yandex.ru/authorize?' + urlencode({
            'response_type': 'code', 'client_id': self.settings.client_id,
            'redirect_uri': self.settings.redirect_uri, 'scope': 'login:email login:info',
            'state': state, 'code_challenge': code_challenge, 'code_challenge_method': 'S256'})

    async def _json(self, method: str, url: str, *, form: dict[str, str] | None = None,
                    authorization: str | None = None) -> dict[str, object]:
        self._enabled()
        if (method, url) not in {('POST', 'https://oauth.yandex.ru/token'),
                                 ('GET', 'https://login.yandex.ru/info?format=json')}:
            raise YandexError('yandex_request_invalid')
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        if authorization is not None:
            headers['Authorization'] = authorization
        try:
            async with AsyncExitStack() as stack:
                http = self.http
                if http is None:
                    http = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
                response = await stack.enter_async_context(http.stream(method, url, data=form,
                    headers=headers, timeout=15, follow_redirects=False))
                if response.status_code != 200:
                    raise YandexError('yandex_provider_unavailable')
                if response.headers.get('Content-Encoding', '').strip().lower() not in ('', 'identity'):
                    raise YandexError('yandex_provider_invalid')
                if response.is_stream_consumed:
                    if len(response.content) > 65536:
                        raise YandexError('yandex_provider_invalid')
                    body = bytearray(response.content)
                else:
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > 65536:
                            raise YandexError('yandex_provider_invalid')
                        body.extend(chunk)
            value: object = json.loads(body, object_pairs_hook=_unique_object,
                                       parse_constant=_invalid_constant)
            if not isinstance(value, dict):
                raise TypeError('invalid_json_object')
            return value
        except httpx.HTTPError:
            raise YandexError('yandex_provider_unavailable') from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise YandexError('yandex_provider_invalid') from None

    async def exchange_and_profile(self, code: str, code_verifier: str) -> YandexProfile:
        self._enabled()
        if (not isinstance(code, str) or not 1 <= len(code) <= 2048
                or any(not 33 <= ord(c) <= 126 for c in code)
                or not isinstance(code_verifier, str) or not _VERIFIER.fullmatch(code_verifier)):
            raise YandexError('yandex_request_invalid')
        token = await self._json('POST', 'https://oauth.yandex.ru/token', form={
            'grant_type': 'authorization_code', 'code': code, 'client_id': self.settings.client_id,
            'client_secret': self.settings.client_secret, 'redirect_uri': self.settings.redirect_uri,
            'code_verifier': code_verifier})
        access = token.get('access_token')
        if (not isinstance(access, str) or not 1 <= len(access) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in access)):
            raise YandexError('yandex_provider_invalid')
        profile = await self._json('GET', 'https://login.yandex.ru/info?format=json',
                                   authorization='OAuth ' + access)
        subject, client_id = profile.get('id'), profile.get('client_id')
        if (not isinstance(subject, str) or not re.fullmatch(r'[0-9]{1,128}', subject)
                or client_id != self.settings.client_id):
            raise YandexError('yandex_profile_invalid')
        try:
            raw_email = profile.get('default_email')
            if raw_email is not None and not isinstance(raw_email, str):
                raise TypeError('invalid_email')
            email = _normalized_email(raw_email) if raw_email is not None else None
            raw_name = profile.get('real_name') or profile.get('display_name')
            name = raw_name.strip()[:200] if isinstance(raw_name, str) and raw_name.strip() else None
            if name is not None and (any(ord(c) < 32 for c in name) or not name.encode('utf-8')):
                raise ValueError('invalid_name')
            return YandexProfile(subject, self.settings.client_id, email, name)
        except (LoginDenied, ValueError, TypeError, UnicodeError):
            raise YandexError('yandex_profile_invalid') from None
