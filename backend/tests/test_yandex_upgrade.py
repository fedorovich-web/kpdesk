"""Offline fail-closed tests for the explicit production upgrade operator."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config

from scripts import upgrade_yandex as operator


def test_operator_can_only_select_reviewed_migrations_with_exact_predecessors() -> None:
    assert {target: upgrade.previous for target, upgrade in operator.UPGRADES.items()} == {
        "0049_yandex_id": "0048_lava_billing",
        "0050_billing_grace": "0049_yandex_id",
        "0051_service_admin": "0050_billing_grace",
    }
    with pytest.raises(ValueError, match="unknown"):
        operator.checked_plan("0050_service_admin")
    with pytest.raises(ValueError, match="unknown"):
        operator.run("0050_service_admin", apply=True)


@pytest.mark.parametrize("target", ("0049_yandex_id", "0050_billing_grace", "0051_service_admin"))
def test_operator_does_not_connect_without_maintenance_backup_approval(target: str) -> None:
    with pytest.raises(ValueError, match="confirmation"):
        operator.run(target, apply=True)
    with pytest.raises(ValueError, match="confirmation"):
        operator.run(target, apply=True, maintenance=True)


class Connection:
    def __init__(self, in_transaction: bool = True) -> None:
        self.transaction = in_transaction
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return self.transaction

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)


def test_yandex_operator_sets_only_0049_flags_then_clears_them(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Connection()
    checks = iter(("0048_lava_billing", "0049_yandex_id"))
    monkeypatch.setattr(operator, "verify_target", lambda *_: next(checks))
    called: list[str] = []

    def apply(config: Config, target: str) -> None:
        assert config.attributes["production_connection"] is connection
        assert config.attributes["kpdesk_yandex_id_0049_approved"] is True
        assert config.attributes["kpdesk_yandex_id_0049_working_database_approved"] is True
        assert target == "0049_yandex_id"
        called.append(target)

    monkeypatch.setattr(command, "upgrade", apply)
    config = Config()
    operator.apply_in_transaction(connection, "0049_yandex_id", "a" * 24, config)  # type: ignore[arg-type]
    assert called == ["0049_yandex_id"]
    assert "production_connection" not in config.attributes
    assert "kpdesk_yandex_id_0049_approved" not in config.attributes
    assert connection.statements[-1] == "LOCK TABLE public.alembic_version IN EXCLUSIVE MODE"


def test_wrong_revision_refuses_migration_before_any_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Connection()
    monkeypatch.setattr(operator, "verify_target", lambda *_: "0047_auth_reset_serialization")
    upgrade = Mock()
    monkeypatch.setattr(command, "upgrade", upgrade)
    with pytest.raises(ValueError, match="previous_revision"):
        operator.apply_in_transaction(connection, "0049_yandex_id", "a" * 24, Config())  # type: ignore[arg-type]
    upgrade.assert_not_called()


def test_inactive_transaction_refuses_even_approved_upgrade() -> None:
    connection = Connection(in_transaction=False)
    with pytest.raises(ValueError, match="transaction"):
        operator.apply_in_transaction(connection, "0049_yandex_id", "a" * 24, Config())  # type: ignore[arg-type]
    assert connection.statements == []


def test_yandex_secret_is_environment_only_and_not_written_to_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from kpdesk.yandex.settings import YandexSettings
    from kpdesk.yandex.state import VerifierCipher

    env = {
        "KP_DESK_YANDEX_ENABLED": "true",
        "KP_DESK_YANDEX_CLIENT_ID": "0123456789abcdef0123456789abcdef",
        "KP_DESK_YANDEX_CLIENT_SECRET": "only-in-runtime-environment-test-secret",
    }
    original_from_env = YandexSettings.from_env
    monkeypatch.setattr("kpdesk.yandex.state.YandexSettings.from_env",
                        lambda: original_from_env(env))
    one = VerifierCipher.from_runtime()
    two = VerifierCipher.from_runtime()
    encrypted = one.seal("v" * 64)
    assert two.open(encrypted) == "v" * 64
    assert env["KP_DESK_YANDEX_CLIENT_SECRET"] not in encrypted
    assert env["KP_DESK_YANDEX_CLIENT_SECRET"] not in repr(one)
