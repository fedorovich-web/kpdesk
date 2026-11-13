"""Actual public ASGI responses must enforce a browser boundary, including errors."""
import pytest
from fastapi.testclient import TestClient
from test_production_entrypoint import _make_production_api


@pytest.mark.parametrize('path,status', [('/api/health/live', 200), ('/missing', 404)])
def test_public_https_responses_have_browser_security_policy(path: str, status: int) -> None:
    with TestClient(_make_production_api(), base_url='https://kpdesk.ru') as client:
        response = client.get(path)
    assert response.status_code == status
    assert response.headers.get('x-content-type-options') == 'nosniff'
    assert response.headers.get('referrer-policy') == 'no-referrer'
    assert response.headers.get('x-frame-options') == 'DENY'
    assert response.headers.get('strict-transport-security') == 'max-age=31536000'
    policy = response.headers.get('content-security-policy', '')
    assert "script-src 'self'" in policy and "'unsafe-eval'" not in policy
    assert "frame-ancestors 'none'" in policy
    assert "base-uri 'self'" in policy and "form-action 'self'" in policy
    # Both document preview and public PDF rendering use local blob resources.
    assert 'frame-src blob:' in policy and 'object-src blob:' in policy


def test_security_policy_covers_boundary_rejection_without_setting_hsts_on_http() -> None:
    with TestClient(_make_production_api(), base_url='http://kpdesk.ru') as client:
        response = client.get('/api/health/live')
    assert response.status_code == 403
    assert response.json()['code'] == 'https_required'
    assert response.headers.get('x-frame-options') == 'DENY'
    assert 'strict-transport-security' not in response.headers
