"""Schema revision 0032 is fail-closed and never auto-upgrades the live DB."""
from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest

migration = importlib.import_module("migrations.versions.0032_supplier_quote_audit")


def test_unapproved_working_db_refuses_supplier_audit_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    connection.execute.return_value.one.return_value = (
        "kpdesk", "kpdesk", "0031_catalog_taxonomy_v4",
    )
    monkeypatch.delenv("KP_DESK_APPROVE_LOCAL_0032", raising=False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    ddl = Mock()
    monkeypatch.setattr(migration.op, "create_foreign_key", ddl)
    monkeypatch.setattr(migration.op, "execute", ddl)
    with pytest.raises(RuntimeError, match="not authorized"):
        migration.upgrade()
    ddl.assert_not_called()
    assert connection.execute.call_count == 1


@pytest.mark.parametrize(("role", "revision"), [
    ("kpdesk_tenant_runtime", "0031_catalog_taxonomy_v4"),
    ("kpdesk", "0030_tenant_llm_cache"),
])
def test_unexpected_role_or_revision_is_rejected_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch, role: str, revision: str,
) -> None:
    connection = Mock()
    connection.execute.return_value.one.return_value = (
        "kpdesk", role, revision,
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    ddl = Mock()
    monkeypatch.setattr(migration.op, "create_foreign_key", ddl)
    monkeypatch.setattr(migration.op, "execute", ddl)
    with pytest.raises(RuntimeError, match="exact owner and schema"):
        migration.upgrade()
    ddl.assert_not_called()


def test_multi_supplier_creation_cannot_write_before_guarded_schema_upgrade() -> None:
    import uuid
    from unittest.mock import patch

    from kpdesk.db.tenant_access import AuthenticatedUser
    from kpdesk.extraction.schema import ExtractedRequest
    from kpdesk.quotes.tenant_storage import TenantQuoteRepository

    connection = Mock()
    connection.scalar.return_value = None
    repo = TenantQuoteRepository(
        connection, AuthenticatedUser(uuid.uuid4()), uuid.uuid4(),
        "synthetic-session", "csrf",
    )
    request = ExtractedRequest.model_validate({"lines": [{
        "source_quote": "Карандаш HB", "description": "Карандаш HB",
        "category": "pencil", "quantity": 2, "unit": "штука",
        "brand": None, "paper_format": None, "paper_gsm": None,
        "pen_ink_color": None, "pen_ink_type": None,
        "attributes": {"pencil_type": "graphite", "lead_grade": "HB"},
    }]})
    with (
        patch.object(TenantQuoteRepository, "_authorize"),
        pytest.raises(RuntimeError, match="supplier_quote_audit_schema_unavailable"),
    ):
        repo.create_supplier_quote(
            active_catalog_id=uuid.uuid4(), source_refs=(),
            extracted=request, selections=(), instructions=None,
            key="intentionally-no-writes",
        )
    connection.execute.assert_not_called()
