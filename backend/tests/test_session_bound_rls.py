"""A8 RLS must validate a real session, not a mutable organization GUC.

Apply A3-A8 DDL and synthetic accounts only in a rolled-back tenant test DB
transaction. No persistent roles, rows, or schema changes.
"""
from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from kpdesk.catalog.tenant_storage import TenantCatalogRepository
from kpdesk.db.auth_sessions import AuthSessionRepository, hash_password
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

ROOT = Path(__file__).resolve().parents[2]


def test_a8_authenticated_rls_rejects_forged_org_and_membership() -> None:
    env = dotenv_values(ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk_tenant_test",
    ))
    org_a, org_b, owner, viewer, editor = (uuid.uuid4() for _ in range(5))
    version_a, version_b = uuid.uuid4(), uuid.uuid4()
    quote_a, quote_b = uuid.uuid4(), uuid.uuid4()
    password_owner = "A8-Independent-Verified-Session-2026"
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                assert conn.scalar(text(
                    "SELECT version_num FROM alembic_version"
                )) == "0006_legacy_quarantine"
                with Operations.context(MigrationContext.configure(conn)):
                    for revision in (
                        "0007_tenant_contract_rls", "0008_supplier_review_rls",
                        "0009_auth_sessions", "0010_auth_gateway",
                        "0011_auth_role_split", "0012_session_bound_rls",
                    ):
                        importlib.import_module(
                            "migrations.versions." + revision
                        ).upgrade()
                for org, name in ((org_a, "A8 Org A"), (org_b, "A8 Org B")):
                    conn.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES (:id,:name,'active')
                    """), {"id": org, "name": name})
                for person, email, pw in (
                    (owner, "owner@a8.local", hash_password(password_owner)),
                    (viewer, "viewer@a8.local", hash_password("Viewer-Password-A8-2026")),
                    (editor, "editor@a8.local", hash_password("Editor-Password-A8-2026")),
                ):
                    conn.execute(text("""
                        INSERT INTO users(
                            id,email,password_hash,status,email_verified_at
                        ) VALUES (:id,:email,:hash,'active',now())
                    """), {"id": person, "email": email, "hash": pw})
                for person, role in (
                    (owner, "owner"), (viewer, "viewer"), (editor, "editor"),
                ):
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                            organization_id,user_id,role,status
                        ) VALUES (:org,:person,:role,'active')
                    """), {"org": org_a, "person": person, "role": role})
                for org, version in ((org_a, version_a), (org_b, version_b)):
                    conn.execute(text("""
                        INSERT INTO catalog_versions(
                            id,organization_id,source_sha256,original_filename,rows_count
                        ) VALUES (:id,:org,:hash,'safe-test.csv',1)
                    """), {"id": version, "org": org, "hash": "a" * 64})

                for org, quote in ((org_a, quote_a), (org_b, quote_b)):
                    conn.execute(text("""
                        INSERT INTO quote_documents(
                            id, organization_id, current_revision
                        ) VALUES (:quote,:org,1)
                    """), {"quote": quote, "org": org})
                assert conn.scalar(text("""
                    SELECT has_function_privilege(
                        'kpdesk_app','public.kpdesk_auth_member(uuid,uuid)','EXECUTE'
                    )
                """)) is False
                conn.execute(text("GRANT USAGE ON SCHEMA public TO kpdesk_app"))
                conn.execute(text(
                    "GRANT SELECT, INSERT ON TABLE catalog_versions TO kpdesk_app"
                ))
                conn.execute(text(
                    "GRANT SELECT ON TABLE quote_documents TO kpdesk_app"
                ))
                conn.execute(text("SET ROLE kpdesk_auth_test"))
                issued = AuthSessionRepository(conn).login(
                    "owner@a8.local", password_owner, peer="192.0.2.16",
                )
                viewer_session = AuthSessionRepository(conn).login(
                    "viewer@a8.local", "Viewer-Password-A8-2026",
                    peer="192.0.2.16",
                )
                editor_session = AuthSessionRepository(conn).login(
                    "editor@a8.local", "Editor-Password-A8-2026",
                    peer="192.0.2.16",
                )
                conn.execute(text("SET ROLE kpdesk_app"))
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0
                assert conn.scalar(text(
                    "SELECT count(*) FROM quote_documents"
                )) == 0

                # Forge the OLD organization setting. It must have NO effect.
                conn.execute(text("""
                    SELECT set_config('kpdesk.organization_id', :forged, true)
                """), {"forged": str(org_b)})
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0
                # Forge even a user-id-shaped context - still no session.
                conn.execute(text("""
                    SELECT set_config('kpdesk.user_id', :person, true)
                """), {"person": str(owner)})
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0
                with pytest.raises(TenantAccessDenied):
                    bind_authenticated_tenant(
                        conn, session_token="bad", organization_id=org_a,
                        operation="read",
                    )
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0

                allowed = bind_authenticated_tenant(
                    conn, session_token=issued.session_token,
                    organization_id=org_a, operation="read",
                )
                assert allowed.identity.user_id == owner
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 1
                assert conn.scalar(text(
                    "SELECT count(*) FROM quote_documents"
                )) == 1
                assert conn.scalar(text("""
                    SELECT count(*) FROM quote_documents WHERE id=:quote
                """), {"quote": quote_b}) == 0
                assert conn.scalar(text("""
                    SELECT count(*) FROM catalog_versions WHERE id=:foreign
                """), {"foreign": version_b}) == 0

                # A mistyped/forged app identity must also clear the token
                # context before raising; no SQL after a handled exception
                # may inherit the previously valid owner's RLS context.
                with pytest.raises(TenantAccessDenied):
                    TenantCatalogRepository(
                        conn, AuthenticatedUser(viewer), org_a,
                        session_token=issued.session_token,
                    ).current()
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0
                bind_authenticated_tenant(
                    conn, session_token=issued.session_token,
                    organization_id=org_a, operation="read",
                )

                # A valid owner token cannot select an unjoined organization
                # just by switching org GUC, or by altering query WHERE.
                conn.execute(text("""
                    SELECT set_config('kpdesk.organization_id', :forged, true)
                """), {"forged": str(org_b)})
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 1
                with pytest.raises(TenantAccessDenied):
                    bind_authenticated_tenant(
                        conn, session_token=issued.session_token,
                        organization_id=org_b, operation="read",
                    )
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0

                # A real viewer session reads but does not write, even when
                # SQL bypasses Python's read-only UI.
                read_scope = bind_authenticated_tenant(
                    conn, session_token=viewer_session.session_token,
                    organization_id=org_a, operation="read",
                )
                assert read_scope.identity.user_id == viewer
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 1
                with pytest.raises(DBAPIError), conn.begin_nested():
                    conn.execute(text("""
                        INSERT INTO catalog_versions(
                            id,organization_id,source_sha256,
                            original_filename,rows_count
                        ) VALUES (:id,:org,:hash,'forbidden.csv',1)
                    """), {"id": uuid.uuid4(), "org": org_a, "hash": "b" * 64})
                with pytest.raises(TenantAccessDenied):
                    bind_authenticated_tenant(
                        conn, session_token=viewer_session.session_token,
                        organization_id=org_a, operation="write",
                        csrf=viewer_session.csrf_token,
                    )
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0

                with pytest.raises(TenantAccessDenied):
                    bind_authenticated_tenant(
                        conn, session_token=editor_session.session_token,
                        organization_id=org_a, operation="manage",
                        csrf=editor_session.csrf_token,
                    )
                with pytest.raises(TenantAccessDenied):
                    bind_authenticated_tenant(
                        conn, session_token=issued.session_token,
                        organization_id=org_a, operation="manage",
                    )
                manager = bind_authenticated_tenant(
                    conn, session_token=issued.session_token,
                    organization_id=org_a, operation="manage",
                    csrf=issued.csrf_token,
                )
                assert manager.identity.user_id == owner
                bind_authenticated_tenant(
                    conn, session_token=issued.session_token,
                    organization_id=org_a, operation="write",
                    csrf=issued.csrf_token,
                )
                conn.execute(text("""
                    INSERT INTO catalog_versions(
                        id,organization_id,source_sha256,
                        original_filename,rows_count
                    ) VALUES (:id,:org,:hash,'owner-allowed.csv',1)
                """), {"id": uuid.uuid4(), "org": org_a, "hash": "c" * 64})
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 2
                with pytest.raises(DBAPIError), conn.begin_nested():
                    conn.execute(text("""
                        INSERT INTO catalog_versions(
                            id,organization_id,source_sha256,
                            original_filename,rows_count
                        ) VALUES (:id,:org,:hash,'cross-tenant.csv',1)
                    """), {"id": uuid.uuid4(), "org": org_b, "hash": "d" * 64})

                # Revocation immediately removes RLS visibility without
                # a Python-cache restart; same outer transaction, new SQL.
                conn.execute(text("SET ROLE kpdesk_auth_test"))
                assert AuthSessionRepository(conn).revoke(issued.session_token)
                conn.execute(text("SET ROLE kpdesk_app"))
                assert conn.scalar(text(
                    "SELECT count(*) FROM catalog_versions"
                )) == 0
                with pytest.raises(TenantAccessDenied):
                    bind_authenticated_tenant(
                        conn, session_token=issued.session_token,
                        organization_id=org_a, operation="read",
                    )
                conn.execute(text("RESET ROLE"))
            finally:
                transaction.rollback()
        with engine.connect() as verify:
            assert verify.scalar(text(
                "SELECT version_num FROM alembic_version"
            )) == "0006_legacy_quarantine"
            assert verify.scalar(text(
                "SELECT to_regprocedure("
                "'public.kpdesk_session_tenant_allowed(uuid,text)')"
            )) is None
            assert verify.scalar(text(
                "SELECT count(*) FROM organizations WHERE id=:org"
            ), {"org": org_a}) == 0
    finally:
        engine.dispose()

