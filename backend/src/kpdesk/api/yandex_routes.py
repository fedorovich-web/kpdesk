"""Optional same-origin Yandex login/link with opaque browser binding."""
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import (
    COOKIE_AGE_SECONDS,
    SESSION_COOKIE,
    get_unreleased_catalog_engine,
    get_unreleased_login_engine,
)
from kpdesk.db.auth_sessions import SessionDenied, _token_digest
from kpdesk.yandex.client import YandexError
from kpdesk.yandex.service import YandexService
from kpdesk.yandex.settings import YandexSettings

router = APIRouter(prefix='/api/session')
FLOW_COOKIE = '__Host-kpdesk_yandex_flow'
_CODES = {'yandex_disabled', 'yandex_unavailable', 'yandex_configuration_invalid',
    'yandex_provider_unavailable', 'yandex_provider_invalid', 'yandex_profile_invalid',
    'yandex_flow_expired', 'yandex_cancelled', 'yandex_account_link_required',
    'yandex_account_unavailable', 'yandex_identity_conflict', 'yandex_rate_limited',
    'yandex_email_required', 'yandex_request_invalid'}


def get_yandex_settings() -> YandexSettings:
    try:
        return YandexSettings.from_env()
    except (ValueError, TypeError):
        raise ApiError(503, 'yandex_configuration_invalid', 'Вход через Яндекс пока недоступен.') from None


def get_yandex_service(
    settings: Annotated[YandexSettings, Depends(get_yandex_settings)],
    auth: Annotated[Engine, Depends(get_unreleased_login_engine)],
    tenant: Annotated[Engine, Depends(get_unreleased_catalog_engine)],
) -> YandexService:
    return YandexService(settings, auth, tenant)


SERVICE = Annotated[YandexService, Depends(get_yandex_service)]
COOKIE = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def _private(response: Response) -> None:
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                              'X-Robots-Tag': 'noindex, nofollow'})


def _browser_cookie(response: Response, value: str) -> None:
    response.set_cookie(FLOW_COOKIE, value, max_age=600, path='/', secure=True,
                        httponly=True, samesite='lax')


def _cookie(raw: str | None) -> str:
    try:
        _token_digest(raw or '')
    except SessionDenied:
        raise ApiError(401, 'invalid_session', 'Для продолжения войдите в KPDesk.') from None
    return raw or ''


def _api_error(error: YandexError) -> ApiError:
    code = error.code if error.code in _CODES else 'yandex_unavailable'
    status = 429 if code == 'yandex_rate_limited' else (503 if code in {
        'yandex_disabled', 'yandex_unavailable', 'yandex_configuration_invalid',
        'yandex_provider_unavailable', 'yandex_provider_invalid'} else 400)
    return ApiError(status, code, 'Не удалось завершить вход через Яндекс. Попробуйте ещё раз.')


def _failure(code: str, target: str = '/auth/login') -> RedirectResponse:
    safe = code if code in _CODES else 'yandex_unavailable'
    response = RedirectResponse(target + '?' + urlencode({'oauth_error': safe}), status_code=303)
    response.delete_cookie(FLOW_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
    _private(response)
    return response


@router.get('/providers')
def providers(response: Response, service: SERVICE) -> dict[str, bool]:
    _private(response)
    try:
        return {'yandex': service.provider_available()}
    except YandexError as error:
        raise _api_error(error) from None


@router.get('/yandex/start')
def start(request: Request, service: SERVICE,
          next: Annotated[str, Query(max_length=32)] = '/app') -> RedirectResponse:
    try:
        result = service.start('login', next, request.client.host if request.client else '')
    except YandexError as error:
        raise _api_error(error) from None
    response = RedirectResponse(result.authorization_url, status_code=303)
    _browser_cookie(response, result.browser_token)
    _private(response)
    return response


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    next_path: Literal['/app', '/admin'] = '/app'


@router.post('/yandex/link/start')
def link_start(body: LinkRequest, request: Request, response: Response, service: SERVICE,
               csrf: Annotated[str, Header(alias='X-CSRF-Token', min_length=43, max_length=43)],
               cookie: COOKIE = None) -> dict[str, str]:
    raw = _cookie(cookie)
    try:
        result = service.start('link', body.next_path,
            request.client.host if request.client else '', raw, csrf)
    except YandexError as error:
        raise _api_error(error) from None
    _browser_cookie(response, result.browser_token)
    _private(response)
    return {'authorization_url': result.authorization_url}


@router.get('/yandex/identity')
def identity(response: Response, service: SERVICE, cookie: COOKIE = None) -> dict[str, bool]:
    raw = _cookie(cookie)
    try:
        result = service.identity(raw)
    except SessionDenied:
        raise ApiError(401, 'invalid_session', 'Для продолжения войдите в KPDesk.') from None
    except YandexError as error:
        raise _api_error(error) from None
    _private(response)
    return {'linked': result}


@router.get('/yandex/callback')
async def callback(request: Request, service: SERVICE) -> RedirectResponse:
    failure_target = '/auth/login'
    try:
        query = request.query_params
        if any(len(query.getlist(name)) > 1 for name in ('state', 'code', 'error')):
            raise YandexError('yandex_flow_expired')
        state, code, error = query.get('state'), query.get('code'), query.get('error')
        browser = request.cookies.get(FLOW_COOKIE)
        if (not state or len(state) > 128 or not browser or len(browser) > 128
                or bool(code) == bool(error) or (code is not None and len(code) > 2048)
                or (error is not None and len(error) > 128)):
            raise YandexError('yandex_flow_expired')
        attempt = await run_in_threadpool(service.claim, state, browser)
        if attempt.mode == 'link':
            failure_target = attempt.next_path if attempt.next_path in ('/app', '/admin') else '/app'
        result = await service.finish(attempt, code, error)
    except YandexError as failure:
        return _failure(failure.code, failure_target)
    except SQLAlchemyError:
        return _failure('yandex_unavailable', failure_target)
    target = result.next_path if result.next_path in ('/app', '/admin') else '/app'
    response = RedirectResponse(target, status_code=303)
    # OAuth return is a cross-site navigation. Strict can suppress the new cookie
    # on the immediately-following redirect; Lax permits this top-level flow.
    # Unsafe actions still require same-origin Origin and CSRF checks.
    response.set_cookie(SESSION_COOKIE, result.session.session_token, max_age=COOKIE_AGE_SECONDS,
                        path='/', secure=True, httponly=True, samesite='lax')
    response.delete_cookie(FLOW_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
    _private(response)
    return response
