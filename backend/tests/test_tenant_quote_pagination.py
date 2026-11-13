"""Offline HTTP contract for continuation of immutable tenant quote heads."""
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from kpdesk.api import tenant_quote_routes as routes
from kpdesk.api.errors import ApiError
from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.db.tenant_access import TenantAccessDenied

Boundary = tuple[TestClient, UUID, Mock, Mock, Mock]


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[Boundary]:
    org = uuid4()
    repository = Mock()
    store = Mock()
    authorize = Mock(return_value=store)
    monkeypatch.setattr(routes, '_store', authorize)
    app = FastAPI()
    app.add_exception_handler(ApiError, lambda request, error: JSONResponse(
        {'code': error.code}, status_code=error.status_code,
    ))
    app.include_router(routes.unreleased_tenant_quote_router)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: repository
    with TestClient(app) as client:
        yield client, org, repository.connection, store, authorize


def rows(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=UUID(int=i + 1), current_revision=2,
                            total_rub=Decimal('123.40'), is_complete=True, approved=False)
            for i in range(count)]


def test_first_page_returns_50_and_continuation_without_exposing_extra_row(boundary: Boundary) -> None:
    client, org, connection, store, authorize = boundary
    connection.execute.return_value.all.return_value = rows(51)
    response = client.get(f'/api/session/organizations/{org}/quotes')
    assert response.status_code == 200
    body = response.json()
    assert len(body['items']) == 50
    assert body['next_cursor'] == str(UUID(int=50))
    assert body['items'][0]['total_rub'] == '123.40'
    assert response.headers['cache-control'] == 'no-store'
    repository = authorize.call_args.args[0]
    authorize.assert_called_once_with(repository, None, org, write=False)
    assert repository.connection is connection
    store._authorize.assert_called_once_with(write=False)


@pytest.mark.parametrize('count', [0, 1, 50])
def test_last_page_has_no_continuation(boundary: Boundary, count: int) -> None:
    client, org, connection, _, _ = boundary
    connection.execute.return_value.all.return_value = rows(count)
    body = client.get(f'/api/session/organizations/{org}/quotes').json()
    assert len(body['items']) == count
    assert body['next_cursor'] is None


def test_continuation_is_resolved_inside_the_authorized_organization(boundary: Boundary) -> None:
    client, org, connection, _, _ = boundary
    cursor = uuid4()
    stamp = datetime(2026, 11, 3, tzinfo=UTC)
    anchor = Mock()
    anchor.one_or_none.return_value = SimpleNamespace(created_at=stamp, id=cursor)
    page = Mock()
    page.all.return_value = rows(2)
    connection.execute.side_effect = [anchor, page]
    response = client.get(f'/api/session/organizations/{org}/quotes?cursor={cursor}')
    assert response.status_code == 200
    assert len(response.json()['items']) == 2
    assert response.json()['next_cursor'] is None
    assert connection.execute.call_args_list[0].args[1] == {'org': org, 'cursor': cursor}
    assert connection.execute.call_args_list[1].args[1] == {
        'org': org, 'before_created_at': stamp, 'before_id': cursor,
    }


def test_missing_or_foreign_cursor_does_not_restart_at_first_page(boundary: Boundary) -> None:
    client, org, connection, _, _ = boundary
    connection.execute.return_value.one_or_none.return_value = None
    response = client.get(f'/api/session/organizations/{org}/quotes?cursor={uuid4()}')
    assert response.status_code == 422
    assert connection.execute.call_count == 1


def test_malformed_cursor_is_rejected_before_query(boundary: Boundary) -> None:
    client, org, connection, _, _ = boundary
    response = client.get(f'/api/session/organizations/{org}/quotes?cursor=invalid')
    assert response.status_code == 422
    connection.execute.assert_not_called()


def test_revoked_membership_cannot_query_any_page(boundary: Boundary) -> None:
    client, org, connection, store, _ = boundary
    store._authorize.side_effect = TenantAccessDenied('revoked')
    response = client.get(f'/api/session/organizations/{org}/quotes?cursor={uuid4()}')
    assert response.status_code == 403
    connection.execute.assert_not_called()


@pytest.mark.parametrize('continuation', [False, True])
def test_list_exposes_current_revision_approval_on_each_page(boundary: Boundary, continuation: bool) -> None:
    client, org, connection, _, _ = boundary
    page_rows = rows(2)
    page_rows[0].approved = True
    page = Mock()
    page.all.return_value = page_rows
    suffix = ''
    if continuation:
        cursor = uuid4()
        anchor = Mock()
        anchor.one_or_none.return_value = SimpleNamespace(
            id=cursor, created_at=datetime(2026, 11, 3, tzinfo=UTC),
        )
        connection.execute.side_effect = [anchor, page]
        suffix = f'?cursor={cursor}'
    else:
        connection.execute.return_value = page
    response = client.get(f'/api/session/organizations/{org}/quotes{suffix}')
    assert response.status_code == 200
    assert [item['approved'] for item in response.json()['items']] == [True, False]
