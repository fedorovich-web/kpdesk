"""Validate explicit proxy trust without starting Uvicorn or making network calls."""
import pytest

from kpdesk.container_entrypoint import trusted_proxies


@pytest.mark.parametrize('value',['','*','0.0.0.0/0','10.0.0.0/8','proxy','127.0.0.1,','127.0.0.1,evil'])
def test_container_requires_exact_proxy_addresses(value: str) -> None:
    with pytest.raises(ValueError):
        trusted_proxies(value)


def test_proxy_allowlist_keeps_explicit_proxy_and_internal_healthcheck() -> None:
    assert set(trusted_proxies('172.20.0.8,::1').split(',')) == {'172.20.0.8','::1','127.0.0.1'}


def test_health_probe_never_imports_the_heavy_production_application(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path
    from unittest.mock import Mock

    import kpdesk.container_entrypoint as entrypoint

    monkeypatch.setitem(__import__('sys').modules, 'kpdesk.production', None)
    monkeypatch.setenv('KP_DESK_PUBLIC_ORIGIN', 'https://kpdesk.ru')
    response = Mock(status_code=200)
    response.json.return_value = {'status': 'ready'}
    request = Mock(return_value=response)
    monkeypatch.setattr('httpx.get', request)
    spec = importlib.util.spec_from_file_location('isolated_health_probe', Path(entrypoint.__file__))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.healthcheck() == 0
    request.assert_called_once_with('http://127.0.0.1:8000/api/health/ready',
        headers={'Host': 'kpdesk.ru', 'X-Forwarded-Proto': 'https'}, timeout=3, trust_env=False)
