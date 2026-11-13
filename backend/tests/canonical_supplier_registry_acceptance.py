"""Historical 0036 storage remains intact after retiring its application workflow."""
from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.supplier_registry_schema import install_supplier_registry_on_clone


def verify_retired_supplier_registry_https(
    *, owner: Engine, organization: uuid.UUID,
    session: IssuedSession, app_factory: Callable[[str], FastAPI],
) -> None:
    """Preserve historical DDL/data; archived records no longer gate price review.

    This helper runs only inside the already tagged A45 disposable clone.
    No migration or registry writer is exposed by the application.
    """
    with owner.begin() as conn:
        install_supplier_registry_on_clone(conn)
        conn.execute(text("""
            INSERT INTO public.tenant_supplier_registry (
                organization_id,supplier_id,display_name,status,created_by,updated_by
            ) VALUES (:org,'supplier-a79','Historical source','archived',:actor,:actor)
        """), {"org": organization, "actor": session.user_id})
        before: list[str] = list(conn.execute(text("""
            SELECT snapshot_sha256 FROM public.quote_revisions
            WHERE organization_id=:org ORDER BY quote_id,number
        """), {"org": organization}).scalars())
        assert before
        assert conn.scalar(text("""
            SELECT has_table_privilege(
                'kpdesk_tenant_runtime','public.tenant_supplier_registry','SELECT'
            )
        """)) is False

    root = f"/api/session/organizations/{organization}"
    headers = {"Origin": "https://testserver", "X-CSRF-Token": session.csrf_token}
    with TestClient(app_factory("https://testserver"), base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, session.session_token)
        assert client.get(root + "/suppliers/registry").status_code == 404
        inspected = client.post(
            root + "/suppliers/supplier-a79/precision-reviews/inspect",
            headers=headers,
            files={"file": ("vendor.csv", b"sku;name;price\nA;Pen;10\n")},
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()["requires_confirmation"] is True

    with owner.connect() as conn:
        after: list[str] = list(conn.execute(text("""
            SELECT snapshot_sha256 FROM public.quote_revisions
            WHERE organization_id=:org ORDER BY quote_id,number
        """), {"org": organization}).scalars())
        assert after == before
        assert conn.scalar(text("""
            SELECT count(*) FROM public.tenant_supplier_registry
            WHERE organization_id=:org AND supplier_id='supplier-a79' AND status='archived'
        """), {"org": organization}) == 1
