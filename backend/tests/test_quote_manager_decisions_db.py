"""PostgreSQL 0028 immutable manager-attestation contract, rollback-only."""
from __future__ import annotations

import hashlib
import importlib
import secrets
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from kpdesk.api.tenant_quote_routes import unreleased_tenant_quote_router
from kpdesk.api.tenant_session_routes import SESSION_COOKIE, get_unreleased_auth_repository
from kpdesk.db.auth_sessions import AuthSessionRepository
from kpdesk.extraction.schema import ExtractedRequest
from kpdesk.main import create_app
from kpdesk.matching.schema import CatalogProduct
from kpdesk.quotes.storage import ManagerSkuSelection, _digest, _make_snapshot

_ROOT = Path(__file__).resolve().parents[2]
_TARGET = "0029_manager_decisions_read"


def test_immutable_manager_decision_attested_by_real_postgres_trigger() -> None:
    env = dotenv_values(_ROOT / ".env")
    password = env.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32
    engine = create_engine(URL.create(
        "postgresql+psycopg", username="kpdesk", password=password,
        host="127.0.0.1", port=int(env.get("KP_DESK_DB_PORT") or 55432),
        database="kpdesk",
    ))
    person, organization, catalog, quote_id = (uuid.uuid4() for _ in range(4))
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    source = ExtractedRequest.model_validate({"lines": [{
        "source_quote": "Paper A4 80", "description": "Office paper A4",
        "category": "paper", "quantity": 2, "unit": "пачка",
        "brand": None, "paper_format": "A4", "paper_gsm": 80,
        "pen_ink_color": None, "pen_ink_type": None, "attributes": {},
    }]})
    product = CatalogProduct(
        sku="A43-PAPER", name="Office paper A4", category="paper",
        sale_unit="пачка", pieces_per_sale_unit=1,
        paper_format="A4", paper_gsm=80, price_rub=Decimal("270.50"),
    )
    snapshot = _make_snapshot(
        source, [product], [ManagerSkuSelection(line_number=1, sku="A43-PAPER")],
    )
    assert snapshot.ready_for_draft and snapshot.total_rub == Decimal("541.00")
    payload = snapshot.model_dump(mode="json")
    statement = text("""
        INSERT INTO public.quote_revisions(
          organization_id,quote_id,number,catalog_version_id,
          snapshot,snapshot_sha256,is_complete,total_rub
        ) VALUES (:org,:quote,:number,:catalog,:snapshot,:hash,true,:total)
    """).bindparams(bindparam("snapshot", type_=JSONB))

    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                version = connection.scalar(text("SELECT version_num FROM alembic_version"))
                assert version in ("0028_quote_manager_decisions", _TARGET,
                                   "0030_tenant_llm_cache", "0031_catalog_taxonomy_v4")
                if version == "0028_quote_manager_decisions":
                    with Operations.context(MigrationContext.configure(connection)):
                        importlib.import_module(
                            "migrations.versions.0029_quote_manager_decisions_read"
                        ).upgrade()
                table = connection.execute(text("""
                    SELECT c.relrowsecurity,c.relforcerowsecurity
                    FROM pg_class c
                    WHERE c.oid='public.tenant_quote_manager_decisions'::regclass
                """)).one()
                assert table == (True, True)
                assert connection.scalar(text("""
                    SELECT has_table_privilege(
                      'kpdesk_tenant_runtime',
                      'public.tenant_quote_manager_decisions','SELECT'
                    )
                """)) is False
                assert connection.scalar(text("""
                    SELECT has_table_privilege(
                      'kpdesk_app','public.tenant_quote_manager_decisions','INSERT'
                    )
                """)) is False
                connection.execute(text("""
                    INSERT INTO public.organizations(id,name,status)
                    VALUES (:id,'A43 synthetic organization','active')
                """), {"id": organization})
                connection.execute(text("""
                    INSERT INTO public.users(
                      id,email,password_hash,status,email_verified_at
                    ) VALUES (:id,:email,:hash,'active',now())
                """), {"id": person, "email": f"synthetic-{person}@example.invalid",
                        "hash": "not-a-login-credential"})
                connection.execute(text("""
                    INSERT INTO public.organization_memberships(
                      organization_id,user_id,role,status
                    ) VALUES (:org,:person,'owner','active')
                """), {"org": organization, "person": person})
                connection.execute(text("""
                    INSERT INTO public.auth_sessions(
                      token_sha256,user_id,csrf_sha256,expires_at
                    ) VALUES (:hash,:person,:csrf,clock_timestamp()+interval '1 hour')
                """), {"hash": digest, "person": person, "csrf": "a" * 64})
                connection.execute(text("""
                    INSERT INTO public.catalog_versions(
                      id,organization_id,source_sha256,original_filename,rows_count
                    ) VALUES (:id,:org,:sha,'synthetic-a43.csv',1)
                """), {"id": catalog, "org": organization, "sha": "b" * 64})
                connection.execute(text("""
                    INSERT INTO public.catalog_items(
                      organization_id,catalog_version_id,sku,name,category,
                      sale_unit,pieces_per_sale_unit,price_rub,
                      paper_format,paper_gsm,attributes
                    ) VALUES (
                      :org,:catalog,'A43-PAPER','Office paper A4','paper',
                      'пачка',1,270.50,'A4',80,'{}'::jsonb
                    )
                """), {"org": organization, "catalog": catalog})
                connection.execute(text("""
                    INSERT INTO public.quote_documents(
                      id,organization_id,current_revision
                    ) VALUES (:quote,:org,1)
                """), {"quote": quote_id, "org": organization})
                connection.execute(text("""
                    SELECT set_config('kpdesk.session_sha256',:digest,true)
                """), {"digest": digest})
                assert connection.scalar(text("""
                    SELECT public.kpdesk_session_tenant_allowed(:org,'write')
                """), {"org": organization}) is True
                connection.execute(text("SET LOCAL ROLE kpdesk_tenant_runtime"))
                assert connection.scalar(text("SELECT current_user")) == (
                    "kpdesk_tenant_runtime"
                )
                connection.execute(statement, {
                    "org": organization, "quote": quote_id, "number": 1,
                    "catalog": catalog, "snapshot": payload,
                    "hash": _digest(payload), "total": Decimal("541.00"),
                })
                with connection.begin_nested() as private:
                    with pytest.raises(DBAPIError):
                        connection.execute(text("""
                            SELECT count(*) FROM public.tenant_quote_manager_decisions
                        """))
                    private.rollback()
                assert connection.scalar(text("SELECT has_function_privilege('kpdesk_tenant_runtime','public.kpdesk_quote_manager_decisions_get(uuid,uuid,integer)','EXECUTE')")) is True
                connection.execute(text("RESET ROLE"))
                record = connection.execute(text("""
                    SELECT line_number,sku,approved_by_user_id,
                           catalog_version_id
                    FROM public.tenant_quote_manager_decisions
                    WHERE organization_id=:org AND quote_id=:quote
                """), {"org": organization, "quote": quote_id}).one()
                assert record == (1, "A43-PAPER", person, catalog)
                connection.execute(text("SET LOCAL ROLE kpdesk_tenant_runtime"))
                read = text("SELECT line_number,sku,catalog_version_id,approved_by_user_id FROM public.kpdesk_quote_manager_decisions_get(:org,:quote,:revision)")
                params = {"org": organization, "quote": quote_id, "revision": 1}
                assert connection.execute(read, params).one() == (
                    1, "A43-PAPER", catalog, person,
                )
                assert connection.execute(read, {**params, "org": uuid.uuid4()}).all() == []
                assert connection.execute(read, {**params, "quote": uuid.uuid4()}).all() == []
                assert connection.execute(read, {**params, "revision": 0}).all() == []
                http_app = create_app(testing=True)
                http_app.include_router(unreleased_tenant_quote_router)
                http_app.dependency_overrides[get_unreleased_auth_repository] = (
                    lambda: AuthSessionRepository(connection)
                )
                with TestClient(http_app, base_url="https://testserver") as client:
                    path = (
                        f"/api/session/organizations/{organization}"
                        f"/quotes/{quote_id}/manager-decisions/1"
                    )
                    assert client.get(path).status_code == 401
                    client.cookies.set(SESSION_COOKIE, token)
                    verified = client.get(path)
                    assert verified.status_code == 200, verified.text
                    assert verified.headers["cache-control"] == "no-store"
                    assert verified.json()["items"][0]["approved_by_user_id"] == str(person)
                    assert verified.json()["items"][0]["sku"] == "A43-PAPER"
                    assert client.get(path.replace(str(organization), str(uuid.uuid4()))).status_code == 403
                    assert client.get(path.replace(str(quote_id), str(uuid.uuid4()))).status_code == 404
                connection.execute(text("RESET ROLE"))

                with connection.begin_nested() as invalid:
                    connection.execute(text("SET LOCAL ROLE kpdesk_tenant_runtime"))
                    connection.execute(text("""
                        SELECT set_config('kpdesk.session_sha256','',true)
                    """))
                    with pytest.raises(DBAPIError):
                        connection.execute(statement, {
                            "org": organization, "quote": quote_id, "number": 2,
                            "catalog": catalog, "snapshot": payload,
                            "hash": _digest(payload), "total": Decimal("541.00"),
                        })
                    invalid.rollback()

                with connection.begin_nested() as forged:
                    forged_quote = uuid.uuid4()
                    connection.execute(text("""
                        INSERT INTO public.quote_documents(
                          id,organization_id,current_revision
                        ) VALUES (:quote,:org,1)
                    """), {"quote": forged_quote, "org": organization})
                    forged_snapshot = {
                        **payload,
                        "lines": [{**payload["lines"][0], "sku": "UNLISTED-SKU"}],
                    }
                    connection.execute(text("SET LOCAL ROLE kpdesk_tenant_runtime"))
                    with pytest.raises(DBAPIError):
                        connection.execute(statement, {
                            "org": organization, "quote": forged_quote, "number": 1,
                            "catalog": catalog, "snapshot": forged_snapshot,
                            "hash": _digest(forged_snapshot),
                            "total": Decimal("541.00"),
                        })
                    forged.rollback()

                with connection.begin_nested() as immutable:
                    with pytest.raises(DBAPIError):
                        connection.execute(text("""
                            UPDATE public.tenant_quote_manager_decisions
                            SET sku='FORGED' WHERE organization_id=:org
                        """), {"org": organization})
                    immutable.rollback()
                assert connection.scalar(text("""
                    SELECT count(*) FROM public.tenant_quote_manager_decisions
                    WHERE organization_id=:org
                """), {"org": organization}) == 1
            finally:
                transaction.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.organizations WHERE id=:org
            """), {"org": organization}) == 0
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == version
    finally:
        engine.dispose()
