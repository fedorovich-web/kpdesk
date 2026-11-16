"""Yandex ID contracts; in-memory transport only, no real provider activity."""
import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

from kpdesk.yandex.client import YandexClient, YandexError
from kpdesk.yandex.settings import YandexSettings
from kpdesk.yandex.state import VerifierCipher, digest_token, pkce_challenge, safe_next_path

CLIENT = '0123456789abcdef0123456789abcdef'
SECRET = 'synthetic-only-client-secret-value'
VERIFIER = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
STATE = 'b' * 43
REDIRECT = 'https://kpdesk.ru/api/session/yandex/callback'


def configured(**changes: str) -> YandexSettings:
    return YandexSettings.from_env({'KP_DESK_YANDEX_ENABLED': 'true',
        'KP_DESK_YANDEX_CLIENT_ID': CLIENT, 'KP_DESK_YANDEX_CLIENT_SECRET': SECRET,
        'KP_DESK_YANDEX_REDIRECT_URI': REDIRECT, **changes})


def profile(**changes: object) -> dict[str, object]:
    return {'id': '123456789', 'client_id': CLIENT, 'default_email': 'User@Example.test',
            'real_name': ' Учебный пользователь ', **changes}


def test_default_disabled_settings_and_secret_safe_representation() -> None:
    assert not YandexSettings.from_env({}).enabled
    assert not YandexSettings.from_env({'KP_DESK_YANDEX_ENABLED': 'false'}).enabled
    assert configured().redirect_uri == REDIRECT
    assert SECRET not in repr(configured())


@pytest.mark.parametrize('flag', ['', '1', 'TRUE', 'False', ' true'])
def test_provider_flag_is_strict(flag: str) -> None:
    with pytest.raises(ValueError):
        configured(KP_DESK_YANDEX_ENABLED=flag)


@pytest.mark.parametrize('name,value', [
    ('KP_DESK_YANDEX_CLIENT_ID', 'bad'), ('KP_DESK_YANDEX_CLIENT_ID', CLIENT + '\n'),
    ('KP_DESK_YANDEX_CLIENT_SECRET', ''), ('KP_DESK_YANDEX_CLIENT_SECRET', SECRET + '\n'),
    ('KP_DESK_YANDEX_REDIRECT_URI', 'https://evil.test/callback'),
    ('KP_DESK_YANDEX_REDIRECT_URI', REDIRECT + '?next=https://evil.test'),
])
def test_enabled_configuration_fails_closed(name: str, value: str) -> None:
    with pytest.raises(ValueError) as error:
        configured(**{name: value})
    assert SECRET not in str(error.value)


def test_standard_pkce_and_purpose_bound_encryption() -> None:
    assert pkce_challenge(VERIFIER) == 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'
    key = Fernet.generate_key()
    cipher = VerifierCipher(key)
    sealed = cipher.seal(VERIFIER)
    assert VERIFIER not in sealed and cipher.open(sealed) == VERIFIER
    assert key.decode() not in repr(cipher)
    wrong_purpose = Fernet(key).encrypt(b'a' * 43).decode()
    with pytest.raises(YandexError):
        cipher.open(wrong_purpose)
    with pytest.raises(YandexError):
        cipher.open(sealed[:-3] + 'bad')
    assert len(digest_token(STATE)) == 64 and STATE not in digest_token(STATE)


@pytest.mark.parametrize('value', ['', '//evil.test', 'https://evil.test', '/app?secret=x',
                                 '/admin/../app', '/app#billing', '/app\\evil', '/%61pp'])
def test_return_path_is_an_exact_allowlist(value: str) -> None:
    assert safe_next_path(value) == '/app'
    assert safe_next_path('/admin') == '/admin'


def test_authorization_url_contains_only_required_provider_scopes_and_pkce() -> None:
    url = urlsplit(YandexClient(configured()).authorization_url(STATE, pkce_challenge(VERIFIER)))
    assert (url.scheme, url.netloc, url.path) == ('https', 'oauth.yandex.ru', '/authorize')
    assert parse_qs(url.query) == {'response_type': ['code'], 'client_id': [CLIENT],
        'redirect_uri': [REDIRECT], 'scope': ['login:email login:info'], 'state': [STATE],
        'code_challenge': [pkce_challenge(VERIFIER)], 'code_challenge_method': ['S256']}
    assert SECRET not in url.query


@pytest.mark.asyncio
async def test_exchange_uses_fixed_origins_form_and_authorization_header() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers['Accept-Encoding'] == 'identity'
        if request.method == 'POST':
            assert str(request.url) == 'https://oauth.yandex.ru/token'
            assert parse_qs(request.content.decode()) == {'grant_type': ['authorization_code'],
                'code': ['synthetic-code'], 'client_id': [CLIENT], 'client_secret': [SECRET],
                'redirect_uri': [REDIRECT], 'code_verifier': [VERIFIER]}
            return httpx.Response(200, json={'access_token': 'synthetic-access-token'})
        assert str(request.url) == 'https://login.yandex.ru/info?format=json'
        assert request.headers['Authorization'] == 'OAuth synthetic-access-token'
        assert 'synthetic-access-token' not in str(request.url)
        return httpx.Response(200, json=profile())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await YandexClient(configured(), http).exchange_and_profile('synthetic-code', VERIFIER)
        assert not http.is_closed
    assert len(requests) == 2
    assert (result.subject, result.client_id, result.email, result.name) == (
        '123456789', CLIENT, 'user@example.test', 'Учебный пользователь')


@pytest.mark.parametrize('changes', [{'client_id': 'f' * 32}, {'id': 123}, {'id': ''},
    {'id': 'bad\n'}, {'default_email': 'bad'}, {'default_email': 123}])
@pytest.mark.asyncio
async def test_malformed_or_cross_client_identity_cannot_authenticate(changes: dict[str, object]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'access_token': 'test-token'} if request.method == 'POST'
                              else profile(**changes))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(YandexError) as error:
            await YandexClient(configured(), http).exchange_and_profile('code', VERIFIER)
    assert error.value.code == 'yandex_profile_invalid'


@pytest.mark.asyncio
async def test_missing_email_remains_explicit_for_service_to_decide_linked_or_new() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'access_token': 'test-token'} if request.method == 'POST'
        else profile(default_email=None),
    ))) as http:
        result = await YandexClient(configured(), http).exchange_and_profile('code', VERIFIER)
    assert result.email is None


@pytest.mark.asyncio
async def test_disabled_client_never_sends() -> None:
    def reject(_: httpx.Request) -> httpx.Response:
        pytest.fail('disabled provider attempted network')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        client = YandexClient(YandexSettings(), http)
        with pytest.raises(YandexError, match='yandex_disabled'):
            client.authorization_url(STATE, pkce_challenge(VERIFIER))
        with pytest.raises(YandexError, match='yandex_disabled'):
            await client.exchange_and_profile('code', VERIFIER)


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.read, self.closed = chunks, 0, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize('encoding', ['', 'gzip'])
@pytest.mark.asyncio
async def test_oversize_or_compressed_token_response_stops_before_more_body(encoding: str) -> None:
    stream = Stream([b' ' * 65536, b'x', b'must-not-read'])
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={'Content-Encoding': encoding}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(YandexError) as error:
            await YandexClient(configured(), http).exchange_and_profile('code', VERIFIER)
    assert stream.closed and stream.read == (0 if encoding else 2)
    assert len(requests) == 1 and error.value.code == 'yandex_provider_invalid'


@pytest.mark.parametrize('status', [302, 401, 429, 500])
@pytest.mark.asyncio
async def test_provider_errors_do_not_retry_redirect_or_disclose_body(status: int) -> None:
    stream = Stream([SECRET.encode()])
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://evil.test'}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as http:
        with pytest.raises(YandexError) as error:
            await YandexClient(configured(), http).exchange_and_profile('code', VERIFIER)
    assert len(requests) == 1 and stream.read == 0 and stream.closed
    assert SECRET not in str(error.value)


@pytest.mark.asyncio
async def test_duplicate_json_keys_are_rejected() -> None:
    payload = json.dumps({'access_token': 'one'})[:-1] + ',"access_token":"two"}'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=payload),
    )) as http:
        with pytest.raises(YandexError):
            await YandexClient(configured(), http).exchange_and_profile('code', VERIFIER)
