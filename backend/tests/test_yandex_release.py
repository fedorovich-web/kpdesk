"""Readiness for optional identity schema preserves every older release gate."""
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from kpdesk.release_runtime import verify_release_schema
from scripts.provision_production import admission


def ready(monkeypatch: pytest.MonkeyPatch, revision: Any = '0049_yandex_id', enabled: Any = False, broken: Any = None) -> Any:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.one.return_value = ('kpdesk_tenant_runtime', revision, True, True, True)
    connection.execute.return_value.scalar_one.return_value = True
    connection.scalar.return_value = True
    for path, gate in [('kpdesk.quotes.sharing.sharing_available', 'sharing'),
                       ('kpdesk.db.universal_schema.universal_available', 'universal'),
                       ('kpdesk.db.supplier_registry_schema.registry_retired', 'registry'),
                       ('kpdesk.billing.schema.billing_available', 'billing'),
                       ('kpdesk.yandex.schema.yandex_available', 'yandex')]:
        monkeypatch.setattr(path, lambda _, current=gate: current != broken)
    monkeypatch.setattr('kpdesk.billing.settings.BillingSettings.from_env', lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr('kpdesk.billing.schema.billing_enabled', lambda _: False)
    monkeypatch.setattr('kpdesk.yandex.settings.YandexSettings.from_env', lambda: SimpleNamespace(enabled=enabled))
    cipher = Mock()
    monkeypatch.setattr('kpdesk.yandex.state.VerifierCipher.from_runtime', cipher)
    return engine, connection, cipher


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('broken', ['sharing', 'universal', 'registry', 'billing', 'yandex'])
def test_0049_preserves_all_gates_including_disabled_identity(monkeypatch: pytest.MonkeyPatch, enabled: Any, broken: Any) -> None:
    engine, _, _ = ready(monkeypatch, enabled=enabled, broken=broken)
    with pytest.raises(RuntimeError, match='schema'):
        verify_release_schema(engine)


@pytest.mark.parametrize('enabled', [False, True])
def test_complete0049_is_readonly_and_checks_envelope_only_if_enabled(monkeypatch: pytest.MonkeyPatch, enabled: Any) -> None:
    engine, connection, cipher = ready(monkeypatch, enabled=enabled)
    verify_release_schema(engine)
    assert str(connection.execute.call_args_list[0].args[0]) == 'SET TRANSACTION READ ONLY'
    assert cipher.call_count == int(enabled)


def test_enabled_identity_requires0049_without_weakening0048(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _, _ = ready(monkeypatch, revision='0048_lava_billing', enabled=True)
    with pytest.raises(RuntimeError, match='0049'):
        verify_release_schema(engine)


def test_operator_checks_structure_without_application_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _, cipher = ready(monkeypatch)
    settings = Mock(side_effect=ValueError('private-key'))
    monkeypatch.setattr('kpdesk.yandex.settings.YandexSettings.from_env', settings)
    verify_release_schema(engine, check_billing_activation=False, check_yandex_activation=False)
    settings.assert_not_called()
    cipher.assert_not_called()


def test_existing0049_is_verify_only_not_fresh_install() -> None:
    nonce = 'a' * 24
    assert admission('kpdesk', 'kpdesk', '0049_yandex_id', 'kpdesk:production:' + nonce,
                     58, 5, nonce) == 'verify'
