"""Legacy grace cannot be spoofed as payment; operator-only, isolated and expiring."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import Connection

from kpdesk.billing.grace import _GRACE_QUOTA, _GRACE_SUMMARY, create_grace_schema
from scripts import grant_billing_grace


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, value: object) -> None:
        self.statements.append(str(value))


def test_grace_ddl_is_tenant_inaccessible_and_time_bounded() -> None:
    recorder=RecordingConnection()
    create_grace_schema(recorder)  # type: ignore[arg-type]
    sql="\n".join(recorder.statements)
    assert 'CREATE TABLE public.billing_grace' in sql
    assert "ends_at <= starts_at + interval '30 days'" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON TABLE public.billing_grace" in sql
    assert "REVOKE ALL ON FUNCTION public.kpdesk_billing_grace_active(uuid)" in sql
    assert "SECURITY DEFINER SET search_path=pg_catalog,pg_temp" in sql
    assert "NOT EXISTS (" in sql and "FROM public.billing_grants paid" in sql
    assert "CREATE OR REPLACE FUNCTION public.kpdesk_billing_quota_guard()" in sql
    assert "CREATE OR REPLACE FUNCTION public.kpdesk_billing_summary(p_org uuid)" in sql
    assert 'INSERT INTO public.billing_orders' not in sql
    assert 'INSERT INTO public.billing_grants' not in sql


def test_grace_bypasses_only_before_entitlement_and_paid_takes_priority() -> None:
    assert _GRACE_QUOTA.index('kpdesk_billing_grace_active') < _GRACE_QUOTA.index(
        'grant_row:=public.kpdesk_billing_entitlement')
    assert _GRACE_QUOTA.count('kpdesk_billing_grace_active') == 1
    assert "'grace_ends_at'" in _GRACE_SUMMARY


def test_unapproved_0050_migration_does_not_touch_database() -> None:
    import importlib.util
    source=Path(__file__).resolve().parents[1]/'migrations/versions/0050_billing_grace.py'
    spec=importlib.util.spec_from_file_location('billing_grace_0050',source)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op=SimpleNamespace(get_context=lambda: SimpleNamespace(config=SimpleNamespace(attributes={})),
                              get_bind=lambda: pytest.fail('unapproved migration opened connection'))
    with pytest.raises(RuntimeError, match='explicit operator approval'):
        module.upgrade()


def test_grant_operator_requires_backup_and_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    bad=Mock(side_effect=AssertionError('must not connect'))
    monkeypatch.setattr(grant_billing_grace,'create_engine',bad)
    with pytest.raises(ValueError, match='confirmation'):
        grant_billing_grace.run(apply=True)
    with pytest.raises(ValueError, match='duration'):
        grant_billing_grace.run(days=31)
    bad.assert_not_called()


def test_grant_operator_no_credentials_or_organization_parameters_exposed() -> None:
    text=(Path(__file__).resolve().parents[1]/'scripts/grant_billing_grace.py').read_text()
    assert 'legacy_launch_transition' in text
    assert "count != 1" in text
    assert 'billing_enabled' not in text or 'billing_config' in text
    assert "INSERT INTO public.billing_grace" in text
    assert "INSERT INTO public.billing_orders" not in text
    assert "INSERT INTO public.billing_grants" not in text


def test_provisioner_image_includes_operator_but_application_image_does_not() -> None:
    dockerfile=(Path(__file__).resolve().parents[2]/'Dockerfile').read_text(encoding='utf-8')
    provisioner=dockerfile.split('FROM runtime AS provisioner',1)[1].split('FROM runtime AS application',1)[0]
    application=dockerfile.split('FROM runtime AS application',1)[1]
    assert 'backend/scripts/grant_billing_grace.py' in provisioner
    assert 'backend/scripts/grant_billing_grace.py' not in application
