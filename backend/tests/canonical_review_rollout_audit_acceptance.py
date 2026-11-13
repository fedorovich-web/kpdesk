"""A92 negative security/schema drift tests using reversible PostgreSQL SAVEPOINTs.

The connected owner must be a provenance-tagged disposable clone at revision
0030. Every intentional privilege/schema regression is rolled back immediately.
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_rollout_audit import ReviewSchemaAudit, audit_review_schema

_REVISION = "0030_tenant_llm_cache"
_BINDER = (
    "public.a79_bind_published_review("
    "uuid,uuid,uuid,text,text,text,text,text,text,uuid,bigint,text)"
)
_SNAPSHOT = "public.a79_review_snapshot(uuid,text,text,text,text,text,uuid)"


def _audit(connection: Connection) -> ReviewSchemaAudit:
    return audit_review_schema(connection, expected_revision=_REVISION)


def verify_review_rollout_drift_rejected(connection: Connection) -> None:
    """No production writes: intentionally break one invariant per savepoint."""
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A92 security regressions require an isolated transaction.")
    if connection.scalar(text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A92 security regressions require the clone owner.")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
        _REVISION
    ):
        raise RuntimeError("A92 refuses unverified schema revision.")
    initial = _audit(connection)
    initial.require_ready()
    assert initial.signing_epoch == 1
    checks: tuple[tuple[str, str, str], ...] = (
        (
            "direct_table_access:kpdesk_tenant_runtime:a84_provenance_signing_secret",
            "GRANT SELECT ON public.a84_provenance_signing_secret "
            + "TO kpdesk_tenant_runtime",
            "increased_runtime_data_privileges",
        ),
        (
            "rls_not_forced:a79_catalog_review_provenance",
            "ALTER TABLE public.a79_catalog_review_provenance "
            + "NO FORCE ROW LEVEL SECURITY",
            "disabled_forced_rls",
        ),
        (
            "public_function_execute:a79_review_snapshot",
            f"GRANT EXECUTE ON FUNCTION {_SNAPSHOT} TO PUBLIC",
            "exposed_privileged_function",
        ),
        (
            "unsafe_security_definer:a79_review_snapshot",
            f"ALTER FUNCTION {_SNAPSHOT} SET search_path = public, pg_temp",
            "unsafe_search_path",
        ),
        (
            "missing_composite_index:a72_canonical_checkpoints:checkpoint_id desc",
            "DROP INDEX public.a72_scope_latest_idx",
            "missing_scope_index",
        ),
        (
            "function_grant_mismatch:kpdesk_tenant_runtime:a79_bind_published_review",
            f"REVOKE EXECUTE ON FUNCTION {_BINDER} FROM kpdesk_tenant_runtime",
            "missing_runtime_function_permission",
        ),
        (
            "missing_immutable_trigger:a79_catalog_review_provenance:"
            + "a79_provenance_immutable",
            "ALTER TABLE public.a79_catalog_review_provenance "
            + "DISABLE TRIGGER a79_provenance_immutable",
            "disabled_immutable_provenance",
        ),
    )
    for expected, sql, description in checks:
        with connection.begin_nested() as savepoint:
            connection.execute(text(sql))
            audit = _audit(connection)
            assert not audit.ready, description
            assert expected in audit.issues, (description, audit.issues)
            try:
                audit.require_ready()
            except RuntimeError as error:
                assert expected in str(error)
            else:
                raise AssertionError("A92 accepted unsafe schema: " + description)
            savepoint.rollback()
        _audit(connection).require_ready()
    assert _audit(connection).signing_epoch == initial.signing_epoch


def verify_empty_review_schema_rejected(connection: Connection) -> None:
    """Baseline 0030 must not accidentally be declared ready for rollout."""
    if not connection.in_transaction() or not is_live_accounts_http_clone(connection):
        raise RuntimeError("A92 baseline check requires a tagged temporary clone.")
    audit = _audit(connection)
    assert not audit.ready
    assert audit.signing_epoch is None
    assert "missing_table:a72_canonical_checkpoints" in audit.issues
    assert "missing_function:a79_bind_published_review" in audit.issues
