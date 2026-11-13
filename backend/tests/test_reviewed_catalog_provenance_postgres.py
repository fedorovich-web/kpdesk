"""Actual reviewed-catalog Alembic 0033 acceptance on a tagged disposable clone."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone
from postgres_clone_connection import postgres_test_engine as _engine
from sqlalchemy import text

from kpdesk.db.review_schema_manifest import REVIEW_FUNCTIONS, REVIEW_TABLES

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    os.environ.get("KP_DESK_RUN_ISOLATED_0033_PG") != "1",
    reason="Explicit opt-in to create and remove a tagged PostgreSQL snapshot clone",
)
def test_0033_provenance_is_installed_inactive_and_reversible_on_empty_clone() -> None:
    """No application data or credentials from the source database are modified."""
    nonce = secrets.token_hex(6)
    name = f"kpdesk_accounts_http_{nonce}"
    assert re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name)
    admin = _engine("postgres", autocommit=True)
    created = False
    try:
        source = _engine("kpdesk")
        try:
            with source.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM public.alembic_version"))
                    == "0031_catalog_taxonomy_v4"
                )
                assert connection.execute(
                    text("""
                    SELECT (SELECT count(*) FROM public.organizations),
                           (SELECT count(*) FROM public.users),
                           (SELECT count(*) FROM public.quote_documents)
                """)
                ).one() == (0, 0, 0)
        finally:
            source.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            connection.exec_driver_sql(
                f"COMMENT ON DATABASE \"{name}\" IS 'kpdesk:accounts-http:{nonce}'"
            )
        with subprocess.Popen(
            [
                "docker",
                "exec",
                "kpdesk-db-1",
                "pg_dump",
                "-U",
                "kpdesk",
                "-d",
                "kpdesk",
                "-Fc",
                "--no-owner",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as backup:
            assert backup.stdout is not None
            restored = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "kpdesk-db-1",
                    "pg_restore",
                    "-U",
                    "kpdesk",
                    "-d",
                    name,
                    "--exit-on-error",
                    "--no-owner",
                ],
                stdin=backup.stdout,
                capture_output=True,
                check=False,
                timeout=120,
            )
            backup.stdout.close()
            assert backup.wait(timeout=120) == 0, "Source pg_dump failed."
            assert restored.returncode == 0, restored.stderr.decode(
                "utf-8",
                errors="replace",
            )[-500:]

        clone = _engine(name)
        try:
            config = Config(str(_BACKEND / "alembic.ini"))
            with clone.connect() as connection:
                transaction = connection.begin()
                try:
                    # Pre-provision only in the isolated clone; no production
                    # extension, SQL grants, or reviewer keys are modified.
                    connection.execute(
                        text("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
                    )
                    config.attributes["connection"] = connection
                    config.attributes["kpdesk_review_schema_0033_approved"] = True
                    command.upgrade(config, "0033_reviewed_catalog_provenance")
                    assert (
                        connection.scalar(text("SELECT version_num FROM public.alembic_version"))
                        == "0033_reviewed_catalog_provenance"
                    )
                    for table in REVIEW_TABLES:
                        assert connection.execute(
                            text("""
                            SELECT relrowsecurity,relforcerowsecurity
                            FROM pg_catalog.pg_class
                            WHERE oid=to_regclass(:name)
                        """),
                            {"name": "public." + table},
                        ).one() == (True, True)
                        assert (
                            connection.scalar(
                                text("""
                            SELECT has_table_privilege(
                                'kpdesk_tenant_runtime', :name, 'SELECT'
                            )
                        """),
                                {"name": "public." + table},
                            )
                            is False
                        )
                    assert (
                        connection.scalar(
                            text("""
                        SELECT count(*) FROM public.a84_provenance_signing_secret
                    """)
                        )
                        == 0
                    )
                    for signature in REVIEW_FUNCTIONS:
                        assert (
                            connection.scalar(
                                text("""
                            SELECT has_function_privilege(
                                'kpdesk_tenant_runtime',:signature,'EXECUTE'
                            )
                        """),
                                {"signature": "public." + signature},
                            )
                            is False
                        )
                    # New reader is independently guarded and may expose only
                    # the attested boolean to tenant runtime, not table data.
                    config.attributes["kpdesk_review_bridge_0034_approved"] = True
                    command.upgrade(config, "0034_reviewed_source_bridge")
                    assert (
                        connection.scalar(text("SELECT version_num FROM public.alembic_version"))
                        == "0034_reviewed_source_bridge"
                    )
                    signature = (
                        "public.kpdesk_reviewed_catalog_bridge("
                        "uuid,uuid,text,text,text,text,text)"
                    )
                    assert connection.scalar(text("""
                        SELECT has_function_privilege(
                          'kpdesk_tenant_runtime',:signature,'EXECUTE'
                        )
                    """), {"signature": signature}) is True
                    assert connection.scalar(text("""
                        SELECT has_function_privilege(
                          'kpdesk_auth_runtime',:signature,'EXECUTE'
                        )
                    """), {"signature": signature}) is False
                    assert connection.scalar(text("""
                        SELECT public.kpdesk_reviewed_catalog_bridge(
                          gen_random_uuid(),gen_random_uuid(),'test',
                          repeat('a',64),repeat('b',64),
                          repeat('c',64),repeat('d',64)
                        )
                    """)) is False
                    config.attributes["kpdesk_supplier_discovery_0035_approved"] = True
                    command.upgrade(config, "0035_supplier_source_discovery")
                    discovery_signature = (
                        "public.kpdesk_reviewed_supplier_versions("
                        "uuid,text,integer,integer)"
                    )
                    assert connection.scalar(text("""
                        SELECT has_function_privilege(
                            'kpdesk_tenant_runtime',:signature,'EXECUTE'
                        )
                    """), {"signature": discovery_signature}) is True
                    assert connection.scalar(text("""
                        SELECT has_function_privilege(
                            'kpdesk_auth_runtime',:signature,'EXECUTE'
                        )
                    """), {"signature": discovery_signature}) is False
                    assert connection.scalar(text("""
                        SELECT count(*) FROM public.kpdesk_reviewed_supplier_versions(
                            gen_random_uuid(),'test',100,0
                        )
                    """)) == 0
                    assert connection.scalar(text("""
                        SELECT has_table_privilege(
                            'kpdesk_tenant_runtime',
                            'public.a79_catalog_review_provenance','SELECT'
                        )
                    """)) is False
                    config.attributes["kpdesk_supplier_registry_0036_approved"] = True
                    command.upgrade(config, "0036_supplier_registry")
                    registry_signatures = (
                        "public.kpdesk_supplier_registry_list(uuid,integer,integer)",
                        "public.kpdesk_supplier_registry_upsert(uuid,text,text,text,text,integer)",
                        "public.kpdesk_supplier_registry_status(uuid,text)",
                    )
                    assert connection.scalar(text("""
                        SELECT relrowsecurity AND relforcerowsecurity
                        FROM pg_catalog.pg_class
                        WHERE oid='public.tenant_supplier_registry'::regclass
                    """)) is True
                    for registry_sig in registry_signatures:
                        assert connection.scalar(text("""
                            SELECT has_function_privilege(
                                'kpdesk_tenant_runtime',:sig,'EXECUTE'
                            )
                        """), {"sig": registry_sig}) is True
                        assert connection.scalar(text("""
                            SELECT has_function_privilege(
                                'kpdesk_auth_runtime',:sig,'EXECUTE'
                            )
                        """), {"sig": registry_sig}) is False
                    assert connection.scalar(text("""
                        SELECT has_table_privilege(
                            'kpdesk_tenant_runtime',
                            'public.tenant_supplier_registry','SELECT'
                        )
                    """)) is False
                    assert connection.scalar(text("""
                        SELECT count(*) FROM public.kpdesk_supplier_registry_list(
                          gen_random_uuid(),40,0
                        )
                    """)) == 0
                    assert connection.scalar(text("""
                        SELECT count(*) FROM public.kpdesk_supplier_registry_upsert(
                          gen_random_uuid(),'untrusted','Unsafe','', 'active',0
                        )
                    """)) == 0
                    assert connection.scalar(text("""
                        SELECT public.kpdesk_supplier_registry_status(
                          gen_random_uuid(),'untrusted'
                        )
                    """)) is None
                    command.downgrade(config, "0035_supplier_source_discovery")
                    assert connection.scalar(text("""
                        SELECT to_regclass('public.tenant_supplier_registry')
                    """)) is None
                    for registry_sig in registry_signatures:
                        assert connection.scalar(
                            text("SELECT to_regprocedure(:signature)"),
                            {"signature": registry_sig},
                        ) is None
                    command.downgrade(config, "0034_reviewed_source_bridge")
                    assert connection.scalar(
                        text("SELECT to_regprocedure(:signature)"),
                        {"signature": discovery_signature},
                    ) is None
                    command.downgrade(config, "0033_reviewed_catalog_provenance")
                    assert connection.scalar(
                        text("SELECT to_regprocedure(:signature)"),
                        {"signature": signature},
                    ) is None
                    command.downgrade(config, "0032_supplier_quote_audit")
                    assert (
                        connection.scalar(text("SELECT version_num FROM public.alembic_version"))
                        == "0032_supplier_quote_audit"
                    )
                    for table in REVIEW_TABLES:
                        assert (
                            connection.scalar(
                                text("SELECT to_regclass(:name)"), {"name": "public." + table}
                            )
                            is None
                        )
                finally:
                    transaction.rollback()
        finally:
            clone.dispose()
    finally:
        try:
            if created:
                drop_test_clone(admin, name)
        finally:
            admin.dispose()
