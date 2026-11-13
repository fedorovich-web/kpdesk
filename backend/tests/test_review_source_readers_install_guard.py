"""Read-only source SQL rehearsal must remain strictly clone-owner-scoped."""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from kpdesk.db.review_source_readers import (
    BRIDGE_DDL,
    BRIDGE_SIGNATURE,
    DISCOVERY_DDL,
    DISCOVERY_SIGNATURE,
    install_readonly_source_readers_on_clone,
)


@pytest.mark.parametrize(
    ("transaction", "clone", "role", "revision"),
    [
        (False, True, "kpdesk", "0030_tenant_llm_cache"),
        (True, False, "kpdesk", "0030_tenant_llm_cache"),
        (True, True, "kpdesk_tenant_runtime", "0030_tenant_llm_cache"),
        (True, True, "kpdesk", "0031_catalog_taxonomy_v4"),
    ],
)
def test_test_helper_refuses_unsafe_installation_without_ddl(
    transaction: bool, clone: bool, role: str, revision: str,
) -> None:
    connection = Mock()
    connection.in_transaction.return_value = transaction
    connection.scalar.side_effect = [role, revision]
    with patch(
        "kpdesk.db.review_source_readers.is_live_accounts_http_clone",
        return_value=clone,
    ), pytest.raises(RuntimeError, match="tagged clone owner"):
        install_readonly_source_readers_on_clone(connection)
    connection.execute.assert_not_called()


def test_reader_sql_has_no_writer_or_secret_access() -> None:
    for statement in (BRIDGE_DDL, DISCOVERY_DDL):
        assert "SECURITY DEFINER" in statement
        assert "kpdesk_session_tenant_allowed(p_org,'read')" in statement
        assert "a84_provenance_signing_secret" not in statement
        assert "INSERT" not in statement.upper()
        assert "UPDATE " not in statement.upper()
        assert "DELETE " not in statement.upper()
    assert "a79_catalog_review_provenance" in BRIDGE_DDL
    assert "mapping_sha256" in DISCOVERY_DDL
    assert BRIDGE_SIGNATURE != DISCOVERY_SIGNATURE
