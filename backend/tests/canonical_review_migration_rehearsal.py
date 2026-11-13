"""A83 transactional DDL rehearsal on a provenance-tagged PostgreSQL clone.

This is NOT an Alembic production revision. It uses Alembic's MigrationContext
to verify the current head, then exercises SQLAlchemy transaction/savepoint
semantics for the exact A72/A75/A76/A77/A79 installer graph.
"""
from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from canonical_checkpoint_clone_schema import install_checkpoint_prototype
from canonical_checkpoint_current_clone_schema import install_current_review_index
from canonical_checkpoint_paged_clone_schema import install_paged_checkpoint_recovery
from canonical_checkpoint_probe_clone_schema import install_checkpoint_probe
from canonical_review_publication_clone_schema import install_publication_provenance
from canonical_review_rollout_audit_acceptance import (
    verify_empty_review_schema_rejected,
    verify_review_rollout_drift_rejected,
)
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_rollout_audit import audit_review_schema

_TABLES = (
    "a72_canonical_checkpoints",
    "a72_canonical_row_decisions",
    "a77_current_head",
    "a77_current_row",
    "a79_catalog_review_provenance",
    "a84_provenance_signing_secret",
)
_FUNCTIONS = (
    "a72_checkpoint_put", "a72_checkpoint_get",
    "a75_checkpoint_page", "a76_checkpoint_probe",
    "a77_apply_immutable_decisions", "a77_checkpoint_page",
    "a77_checkpoint_probe", "a79_review_snapshot",
    "a79_bind_published_review",
)
_ROLES = ("kpdesk_tenant_runtime", "kpdesk_app", "kpdesk_auth_runtime")


def _assert_clone_owner(connection: Connection) -> None:
    if (
        not connection.in_transaction()
        or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"
        or MigrationContext.configure(connection).get_current_revision()
        != "0030_tenant_llm_cache"
    ):
        raise RuntimeError("A83 migration rehearsal requires a verified clone owner.")


def _install(connection: Connection) -> None:
    install_checkpoint_prototype(connection)
    install_current_review_index(connection)
    install_paged_checkpoint_recovery(connection)
    install_checkpoint_probe(connection)
    install_publication_provenance(connection)


def _present_objects(connection: Connection) -> tuple[set[str], set[str]]:
    tables = set(connection.scalars(text("""
        SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND c.relname=ANY(:names)
    """), {"names": list(_TABLES)}))
    functions = set(connection.scalars(text("""
        SELECT p.proname FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname=ANY(:names)
    """), {"names": list(_FUNCTIONS)}))
    return tables, functions


def _check_installed(
    connection: Connection, *, expected_revision: str = "0030_tenant_llm_cache",
) -> None:
    assert _present_objects(connection) == (set(_TABLES), set(_FUNCTIONS))
    rls = connection.execute(text("""
        SELECT relname,relrowsecurity,relforcerowsecurity
        FROM pg_catalog.pg_class
        WHERE relname=ANY(:names)
    """), {"names": list(_TABLES)}).all()
    assert len(rls) == len(_TABLES)
    assert all(row.relrowsecurity and row.relforcerowsecurity for row in rls)
    runtime_permissions = connection.execute(text("""
        SELECT rolname,table_name,has_table_privilege(rolname,
          'public.'||table_name,'SELECT,INSERT,UPDATE,DELETE') AS access
        FROM (SELECT unnest(CAST(:roles AS text[])) AS rolname) r
        CROSS JOIN (SELECT unnest(CAST(:names AS text[])) AS table_name) t
    """), {"roles": list(_ROLES), "names": list(_TABLES)}).all()
    assert len(runtime_permissions) == len(_ROLES) * len(_TABLES)
    assert all(not row.access for row in runtime_permissions)
    grants = connection.execute(text("""
        SELECT
          has_function_privilege('kpdesk_tenant_runtime',
            'public.a79_review_snapshot(uuid,text,text,text,text,text,uuid)',
            'EXECUTE') AS snapshot,
          has_function_privilege('kpdesk_tenant_runtime',
            'public.a79_bind_published_review(uuid,uuid,uuid,text,text,text,text,text,text,uuid,bigint,text)',
            'EXECUTE') AS binder,
          has_function_privilege('kpdesk_app',
            'public.a79_bind_published_review(uuid,uuid,uuid,text,text,text,text,text,text,uuid,bigint,text)',
            'EXECUTE') AS app_binder
    """)).one()
    assert grants.snapshot and grants.binder and not grants.app_binder
    function_acl = connection.execute(text("""
        SELECT p.proname,p.prosecdef,p.proconfig,
          has_function_privilege('kpdesk_tenant_runtime',p.oid,'EXECUTE')
            AS tenant_allowed,
          has_function_privilege('kpdesk_app',p.oid,'EXECUTE')
            AS app_allowed,
          has_function_privilege('kpdesk_auth_runtime',p.oid,'EXECUTE')
            AS auth_allowed
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname=ANY(:names)
    """), {"names": list(_FUNCTIONS)}).all()
    assert len(function_acl) == len(_FUNCTIONS)
    for function in function_acl:
        assert function.prosecdef is True
        assert any(
            option.replace(" ", "") == "search_path=pg_catalog,pg_temp"
            for option in (function.proconfig or [])
        )
        assert function.tenant_allowed is (
            function.proname != "a77_apply_immutable_decisions"
        )
        assert function.app_allowed is False
        assert function.auth_allowed is False
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
        expected_revision
    )
    audit_review_schema(connection, expected_revision=expected_revision).require_ready()


def verify_review_migration_rollback(engine: Engine) -> None:
    """Prove failed DDL is wholly undone and a clean retry remains possible."""
    with engine.begin() as connection:
        _assert_clone_owner(connection)
        assert _present_objects(connection) == (set(), set())
        verify_empty_review_schema_rejected(connection)
        # Inject a duplicate-table error at the end of full installation.
        # SAVEPOINT rollback must remove all DDL and dependent grants/triggers.
        try:
            with connection.begin_nested():
                _install(connection)
                _check_installed(connection)
                verify_review_rollout_drift_rejected(connection)
                connection.execute(text("""
                    CREATE TABLE public.a79_catalog_review_provenance (bad int)
                """))
        except DBAPIError as error:
            assert getattr(error.orig, "sqlstate", None) == "42P07"
        else:
            raise AssertionError("A83 injected DDL error was not exercised.")
        assert _present_objects(connection) == (set(), set())
        assert MigrationContext.configure(connection).get_current_revision() == (
            "0030_tenant_llm_cache"
        )
        # Successful installation can be rolled back without a migration stamp.
        with connection.begin_nested() as rollback:
            _install(connection)
            _check_installed(connection)
            rollback.rollback()
        assert _present_objects(connection) == (set(), set())
        assert MigrationContext.configure(connection).get_current_revision() == (
            "0030_tenant_llm_cache"
        )
    # COMMIT outer transaction; no temporary schema objects must survive.
    with engine.begin() as check:
        _assert_clone_owner(check)
        assert _present_objects(check) == (set(), set())


def verify_actual_alembic_upgrade_downgrade(
    engine: Engine, organization_id: str, reviewer_id: str,
) -> None:
    """Run both REAL Alembic commands on the provenance-tagged clone only."""
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("version_locations", os.pathsep.join((
        str(backend / "migrations" / "versions"),
        str(backend / "migrations" / "clone_versions"),
    )))
    with engine.begin() as connection:
        _assert_clone_owner(connection)
        assert _present_objects(connection) == (set(), set())
        config.attributes["connection"] = connection

        command.upgrade(config, "0031_reviewed_catalog_clone")
        _check_installed(
            connection, expected_revision="0031_reviewed_catalog_clone",
        )
        # A downgrade containing real human-review rows must fail closed
        # without erasing checkpoints or changing the Alembic revision.
        with connection.begin_nested() as rollback:
            connection.execute(text("""
                INSERT INTO public.a72_canonical_checkpoints (
                  organization_id,supplier_id,source_sha256,mapping_sha256,
                  taxonomy_version,price_basis,idempotency_key,
                  reviewer_user_id,row_count
                ) VALUES (
                  :organization,'a86-downgrade-guard',repeat('a',64),repeat('b',64),
                  'kpdesk-taxonomy-v4','standard','a86-reject-nonempty',
                  :reviewer,1
                )
            """), {"organization": organization_id, "reviewer": reviewer_id})
            try:
                command.downgrade(config, "0030_tenant_llm_cache")
            except RuntimeError as error:
                assert "nonempty" in str(error)
            else:
                raise AssertionError("Alembic downgraded nonempty review history.")
            assert MigrationContext.configure(connection).get_current_revision() == (
                "0031_reviewed_catalog_clone"
            )
            rollback.rollback()
        _check_installed(
            connection, expected_revision="0031_reviewed_catalog_clone",
        )
        command.downgrade(config, "0030_tenant_llm_cache")
        assert MigrationContext.configure(connection).get_current_revision() == (
            "0030_tenant_llm_cache"
        )
        assert _present_objects(connection) == (set(), set())
        assert connection.scalar(text("""
            SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'
        """)) == 0
    with engine.begin() as connection:
        _assert_clone_owner(connection)
        assert _present_objects(connection) == (set(), set())
