"""Manual isolated PostgreSQL auth test with physically independent COMMITs.

Run explicitly from backend: python -m scripts.verify_auth_independent_connections

Creates a unique, marked clone of kpdesk_tenant_test, transient LOGIN roles,
applies test-only A3-A8 schema inside that clone, verifies actual HTTP
commit/rollback, independent logins, rate limits and sessions, then DROPS the clone and
transient roles in finally. NEVER touches production kpdesk schema/data.
"""
from __future__ import annotations

import csv
import hashlib
import io
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Annotated

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import dotenv_values
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from kpdesk.api.tenant_quote_routes import unreleased_tenant_quote_router
from kpdesk.api.tenant_session_routes import (
    get_unreleased_auth_repository,
    get_unreleased_catalog_engine,
    get_unreleased_login_engine,
    unreleased_router,
)
from kpdesk.catalog.schema import COLUMNS_V2
from kpdesk.db.auth_sessions import (
    AuthSessionRepository,
    LoginDenied,
    SessionDenied,
    hash_password,
)
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.committed_login import CommittedLogin, CommittedSessionRevocation
from kpdesk.main import create_app


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _make_engine(
    database: str, *, username: str, password: str,
    port: int, role: str | None = None,
    autocommit: bool = False,
) -> Engine:
    engine = create_engine(
        URL.create(
            "postgresql+psycopg", username=username, password=password,
            host="127.0.0.1", port=port, database=database,
        ),
        isolation_level="AUTOCOMMIT" if autocommit else None,
        poolclass=NullPool, pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )
    if role is not None:
        @event.listens_for(engine, "begin")
        def _assume_scoped_role(connection: Connection) -> None:
            # Only generated fixed identifiers are accepted by construction.
            connection.exec_driver_sql("SET ROLE " + role)
    return engine


def _verify_http_transactions(
    *, app_engine: Engine, auth_engine: Engine, owner_engine: Engine,
    org_a: uuid.UUID, org_b: uuid.UUID, alice: uuid.UUID, bob: uuid.UUID,
    actual_password: str,
) -> None:
    """Exercise actual per-request COMMIT/ROLLBACK via two independent LOGINs."""
    app = create_app(testing=True)
    app.include_router(unreleased_router)
    app.include_router(unreleased_tenant_quote_router)
    app.dependency_overrides[get_unreleased_catalog_engine] = lambda: app_engine
    app.dependency_overrides[get_unreleased_login_engine] = lambda: auth_engine
    rollback_id = uuid.uuid4()
    issued_cookie: list[str] = []
    issued_csrf: list[str] = []

    @app.post("/__isolated_e2e__/force_rollback")
    def test_only_rollback(
        repository: Annotated[
            AuthSessionRepository,
            Depends(get_unreleased_auth_repository, scope="function"),
        ],
    ) -> None:
        bind_authenticated_tenant(
            repository.connection, session_token=issued_cookie[0],
            organization_id=org_a, operation="write", csrf=issued_csrf[0],
        )
        repository.connection.execute(text("""
            INSERT INTO catalog_versions(
                id, organization_id, source_sha256, original_filename, rows_count
            ) VALUES (:id, :org, :sha, 'rollback-only.csv', 1)
        """), {"id": rollback_id, "org": org_a, "sha": "f" * 64})
        raise HTTPException(status_code=409, detail="deliberate test rollback")

    origin = {"Origin": "http://localhost:5173"}
    account_bucket = "a:" + hashlib.sha256(b"alice@isolation.test").hexdigest()
    with owner_engine.connect() as db:
        previous_attempts = db.scalar(text("""
            SELECT attempts FROM auth_login_limits WHERE bucket=:bucket
        """), {"bucket": account_bucket}) or 0
    with TestClient(
        app, base_url="https://testserver", client=("127.0.0.1", 50321),
    ) as client:
        wrong = client.post("/api/session/login", headers=origin, json={
            "email": "alice@isolation.test", "password": "incorrect-pass",
        })
        _assert(wrong.status_code == 401, "HTTP invalid login was accepted.")
        with owner_engine.connect() as db:
            attempts = db.scalar(text("""
                SELECT attempts FROM auth_login_limits WHERE bucket=:bucket
            """), {"bucket": account_bucket})
            _assert(attempts == previous_attempts + 1,
                    "HTTP 401 did not commit rate-limit state.")

        login = client.post("/api/session/login", headers=origin, json={
            "email": "alice@isolation.test", "password": actual_password,
        })
        _assert(login.status_code == 200, f"Independent HTTP login: {login.status_code}")
        _assert("Secure" in login.headers["set-cookie"], "Missing Secure cookie.")
        csrf = login.json()["csrf_token"]
        cookie = client.cookies.get("__Host-kpdesk_session")
        _assert(isinstance(cookie, str), "Committed login cookie missing.")
        issued_cookie.append(cookie)
        issued_csrf.append(csrf)
        own = f"/api/session/organizations/{org_a}"
        foreign = f"/api/session/organizations/{org_b}"
        _assert(client.get("/api/session/me").json() == {
            "user_id": str(alice),
        }, "Committed session cannot cross HTTP DB transactions.")
        _assert(
            client.get(f"{foreign}/catalog/current").status_code == 403,
            "Foreign tenant accessible over HTTP.",
        )

        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer, delimiter=";", lineterminator="\n")
        writer.writerow(COLUMNS_V2)
        item = {
            "sku": "HTTP-A-PAPER", "name": "Office paper A4 80",
            "category": "paper", "sale_unit": "пачка",
            "pieces_per_sale_unit": 1, "price_rub": "429.90",
            "paper_format": "A4", "paper_gsm": 80, "attributes_json": "{}",
        }
        writer.writerow([item.get(column, "") for column in COLUMNS_V2])
        content = csv_buffer.getvalue().encode("utf-8")
        headers = {
            **origin, "X-CSRF-Token": csrf, "Idempotency-Key": "e2e-catalog-1",
        }
        imported = client.post(
            f"{own}/catalog/import", headers=headers,
            files={"file": ("paper.csv", content, "text/csv")},
        )
        _assert(imported.status_code == 201,
                f"Independent HTTP catalog import: {imported.status_code}")
        version = imported.json()["version_id"]
        with owner_engine.connect() as db:
            rows = db.scalar(text("""
                SELECT count(*) FROM catalog_items
                WHERE catalog_version_id=:version AND organization_id=:org
            """), {"version": version, "org": org_a})
            _assert(rows == 1, "HTTP 201 returned before catalog COMMIT.")
        current = client.get(f"{own}/catalog/current")
        _assert(current.status_code == 200 and
                current.json()["version_id"] == version,
                "Next independent HTTP request lost committed catalog.")

        quote_body = {
            "catalog_version_id": version,
            "extracted_request": {"lines": [{
                "source_quote": "Office paper A4, 2 packs",
                "description": "Office paper A4 80", "category": "paper",
                "quantity": 2, "unit": "пачка", "brand": None,
                "paper_format": "A4", "paper_gsm": 80,
                "pen_ink_color": None, "pen_ink_type": None, "attributes": {},
            }]},
        }
        quote_headers = {**headers, "Idempotency-Key": "e2e-quote-1"}
        saved = client.post(f"{own}/quotes", headers=quote_headers, json=quote_body)
        _assert(saved.status_code == 201,
                f"Independent quote HTTP create: {saved.status_code}")
        quote_id = saved.json()["quote_id"]
        _assert(saved.json()["total_rub"] == "859.80",
                "Quote snapshot total changed on HTTP request.")
        with owner_engine.connect() as db:
            present = db.scalar(text("""
                SELECT count(*) FROM quote_revisions
                WHERE quote_id=:quote AND organization_id=:org
            """), {"quote": quote_id, "org": org_a})
            _assert(present == 1, "HTTP 201 returned before quote COMMIT.")
        _assert(client.get(f"{own}/quotes/{quote_id}").status_code == 200,
                "Committed quote unavailable to next HTTP connection.")
        _assert(client.get(f"{foreign}/quotes/{quote_id}").status_code == 403,
                "Foreign tenant accessed committed quote.")
        replay = client.post(f"{own}/quotes", headers=quote_headers, json=quote_body)
        _assert(replay.status_code == 201 and replay.json() == saved.json(),
                "HTTP tenant quote idempotency failed.")

        # Two physical HTTP writers compete for the SAME quote revision.
        # A8 advisory locks + optimistic revision check must admit one winner.
        revision_body = {
            **quote_body, "expected_revision": 1,
            "extracted_request": {
                "lines": [
                    {**quote_body["extracted_request"]["lines"][0], "quantity": 3},
                ],
            },
        }
        revision_barrier = Barrier(2)

        def compete_revision(worker: int) -> tuple[int, dict[str, object]]:
            with TestClient(
                app, base_url="https://testserver",
                client=(f"127.0.0.{worker + 2}", 50330 + worker),
            ) as competing:
                revision_barrier.wait(timeout=15)
                response = competing.post(
                    f"{own}/quotes/{quote_id}/revisions",
                    headers={
                        **origin, "Cookie": f"__Host-kpdesk_session={cookie}",
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": f"e2e-quote-revision-race-{worker}",
                    },
                    json=revision_body,
                )
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            revisions = list(executor.map(compete_revision, (0, 1)))
        _assert(
            sorted(status for status, _ in revisions) == [201, 409],
            f"Concurrent quote revision outcomes: {[status for status, _ in revisions]}",
        )
        successful_revision = next(body for status, body in revisions if status == 201)
        rejected_revision = next(body for status, body in revisions if status == 409)
        _assert(successful_revision["revision"] == 2 and
                successful_revision["total_rub"] == "1289.70",
                "Concurrent quote winner did not preserve calculated total.")
        _assert(rejected_revision["code"] == "stale_revision",
                "Concurrent losing quote edit was not rejected as stale.")
        with owner_engine.connect() as db:
            revision_count = db.scalar(text("""
                SELECT count(*) FROM quote_revisions
                WHERE quote_id=:quote AND organization_id=:org
            """), {"quote": quote_id, "org": org_a})
            _assert(revision_count == 2, "Concurrent edits duplicated quote history.")
        historical = client.get(f"{own}/quotes/{quote_id}?revision=1")
        _assert(historical.status_code == 200 and
                historical.json() == saved.json(),
                "Concurrent edits changed immutable prior quote revision.")

        # Concurrent identical imports must converge on ONE catalog version.
        refreshed_item = {**item, "price_rub": "489.90"}
        fresh_csv = io.StringIO(newline="")
        fresh_writer = csv.writer(
            fresh_csv, delimiter=";", lineterminator="\n",
        )
        fresh_writer.writerow(COLUMNS_V2)
        fresh_writer.writerow([refreshed_item.get(c, "") for c in COLUMNS_V2])
        refreshed_content = fresh_csv.getvalue().encode("utf-8")
        import_barrier = Barrier(2)

        def compete_import(worker: int) -> tuple[int, dict[str, object]]:
            with TestClient(
                app, base_url="https://testserver",
                client=(f"127.0.0.{worker + 4}", 50340 + worker),
            ) as competing:
                import_barrier.wait(timeout=15)
                response = competing.post(
                    f"{own}/catalog/import",
                    headers={
                        **origin, "Cookie": f"__Host-kpdesk_session={cookie}",
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": "e2e-catalog-concurrent",
                    },
                    files={
                        "file": ("refreshed.csv", refreshed_content, "text/csv"),
                    },
                )
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            imports = list(executor.map(compete_import, (0, 1)))
        _assert(
            [status for status, _ in imports] == [201, 201],
            f"Concurrent catalog import outcomes: {[status for status, _ in imports]}",
        )
        first_import = imports[0][1]
        _assert(first_import == imports[1][1] and
                first_import["version_id"] != version,
                "Concurrent same-key imports did not converge.")
        with owner_engine.connect() as db:
            version_count = db.scalar(text("""
                SELECT count(*) FROM catalog_versions WHERE organization_id=:org
            """), {"org": org_a})
            _assert(version_count == 3, "Concurrent publish created duplicate versions.")
        _assert(
            client.get(f"{own}/catalog/current").json()["version_id"]
            == first_import["version_id"],
            "Concurrent import failed to atomically publish the pointer.",
        )
        stale = client.post(
            f"{own}/quotes", headers={
                **quote_headers, "Idempotency-Key": "quote-old-catalog",
            }, json=quote_body,
        )
        _assert(stale.status_code == 409 and
                stale.json()["code"] == "stale_catalog",
                "Quote was created from superseded catalog after refresh.")
        _assert(client.get(f"{own}/quotes/{quote_id}?revision=1").json()
                == saved.json(),
                "Price snapshot changed after concurrent catalog refresh.")

        # Different authenticated user in the second organization can reuse
        # both import and quote idempotency keys without observing Alice data.
        with TestClient(
            app, base_url="https://testserver", client=("127.0.0.11", 50355),
        ) as bob_client:
            bob_login = bob_client.post(
                "/api/session/login", headers=origin,
                json={"email": "bob@isolation.test", "password": actual_password},
            )
            _assert(bob_login.status_code == 200, "Second user HTTP login failed.")
            bob_csrf = bob_login.json()["csrf_token"]
            _assert(bob_client.get("/api/session/me").json() == {
                "user_id": str(bob),
            }, "Second authenticated HTTP identity was confused with Alice.")
            _assert(
                bob_client.get(f"{own}/catalog/current").status_code == 403,
                "Second organization read Alice catalog.",
            )
            _assert(
                bob_client.get(f"{own}/quotes/{quote_id}").status_code == 403,
                "Second organization read Alice quote.",
            )
            other_item = {**item, "price_rub": "329.90"}
            other_file = io.StringIO(newline="")
            other_writer = csv.writer(
                other_file, delimiter=";", lineterminator="\n",
            )
            other_writer.writerow(COLUMNS_V2)
            other_writer.writerow([other_item.get(c, "") for c in COLUMNS_V2])
            bob_headers = {
                **origin, "X-CSRF-Token": bob_csrf,
                "Idempotency-Key": "e2e-catalog-1",
            }
            bob_import = bob_client.post(
                f"{foreign}/catalog/import", headers=bob_headers,
                files={"file": (
                    "independent.csv", other_file.getvalue().encode("utf-8"),
                    "text/csv",
                )},
            )
            _assert(bob_import.status_code == 201,
                    f"Second organization import: {bob_import.status_code}")
            bob_quote = bob_client.post(
                f"{foreign}/quotes",
                headers={
                    **bob_headers, "Idempotency-Key": "e2e-quote-1",
                },
                json={**quote_body,
                      "catalog_version_id": bob_import.json()["version_id"]},
            )
            _assert(bob_quote.status_code == 201,
                    f"Second organization quote: {bob_quote.status_code}")
            _assert(bob_quote.json()["total_rub"] == "659.80",
                    "Cross-org quote used Alice prices.")
            _assert(bob_quote.json()["quote_id"] != quote_id,
                    "Independent organization reused Alice quote document.")
            _assert(
                client.get(f"{foreign}/quotes/{bob_quote.json()['quote_id']}")
                .status_code == 403,
                "Alice read the second organization's quote.",
            )
            _assert(
                bob_client.get(f"{foreign}/catalog/current").json()[
                    "version_id"
                ] == bob_import.json()["version_id"],
                "Bob's catalog was not separately active.",
            )

        rollback = client.post("/__isolated_e2e__/force_rollback", headers=origin)
        _assert(rollback.status_code == 409, "Failure path did not reach HTTP 409.")
        with owner_engine.connect() as db:
            count = db.scalar(text("""
                SELECT count(*) FROM catalog_versions WHERE id=:id
            """), {"id": rollback_id})
            _assert(count == 0, "HTTP error committed a partially written row.")
        logout = client.post(
            "/api/session/logout",
            headers={**origin, "X-CSRF-Token": csrf},
        )
        _assert(logout.status_code == 204, "Independent HTTP logout failed.")
        _assert(client.get("/api/session/me").status_code == 401,
                "Revoked session accepted in a subsequent HTTP transaction.")


def verify_independent_connections() -> None:
    import importlib

    config = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = config.get("KP_DESK_DB_PASSWORD")
    if not isinstance(password, str) or len(password) < 32:
        raise RuntimeError("Existing owner DB credentials are unavailable.")
    port = int(config.get("KP_DESK_DB_PORT") or 55432)
    nonce = secrets.token_hex(6)
    database = "kpdesk_auth_e2e_" + nonce
    auth_login = "kpdesk_auth_connection_" + nonce
    catalog_login = "kpdesk_catalog_connection_" + nonce
    marker = "kpdesk:isolated-auth-e2e:" + nonce
    auth_secret = secrets.token_urlsafe(36)
    catalog_secret = secrets.token_urlsafe(36)
    # Generated identifiers and urlsafe passwords cannot contain SQL quotes.
    admin = _make_engine("postgres", username="kpdesk",
                         password=password, port=port, autocommit=True)
    clone: Engine | None = None
    auth_engine: Engine | None = None
    app_engine: Engine | None = None
    created_database = False
    created_auth_login = False
    created_catalog_login = False
    initial_test_role = False
    try:
        with admin.connect() as connection:
            _assert(connection.scalar(text("""
                SELECT rolsuper FROM pg_roles WHERE rolname=current_user
            """)) is True, "This verification requires existing test owner.")
            initial_test_role = bool(connection.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='kpdesk_auth_test')
            """)))
            _assert(not initial_test_role, "A leftover auth test role exists; abort.")
            template_users = connection.scalar(text("""
                SELECT numbackends FROM pg_stat_database
                WHERE datname='kpdesk_tenant_test'
            """))
            _assert(template_users == 0, "Template test DB has active connections.")
            rev = connection.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:name)
            """), {"name": database})
            _assert(rev is False, "Generated database name is occupied.")
            connection.exec_driver_sql(
                f"CREATE DATABASE {database} TEMPLATE kpdesk_tenant_test"
            )
            created_database = True
            connection.exec_driver_sql(
                f"COMMENT ON DATABASE {database} IS '{marker}'"
            )
        clone = _make_engine(database, username="kpdesk",
                             password=password, port=port)
        alice, bob, org_a, org_b, cat_a, cat_b = (uuid.uuid4() for _ in range(6))
        actual_password = "Independent-Physical-Connection-Test-2026"
        with clone.begin() as connection:
            _assert(connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "0006_legacy_quarantine", "Isolated clone baseline wrong.")
            _assert(connection.scalar(text("SELECT current_database()"))
                    == database, "Clone database mismatch.")
            with Operations.context(MigrationContext.configure(connection)):
                for version in (
                    "0007_tenant_contract_rls", "0008_supplier_review_rls",
                    "0009_auth_sessions", "0010_auth_gateway", "0011_auth_role_split",
                    "0012_session_bound_rls",
                ):
                    importlib.import_module("migrations.versions." + version).upgrade()
            connection.execute(text("""
                INSERT INTO users(id,email,password_hash,status,email_verified_at)
                VALUES (:id,'alice@isolation.test',:hash,'active',now())
            """), {"id": alice, "hash": hash_password(actual_password)})
            connection.execute(text("""
                INSERT INTO users(id,email,password_hash,status,email_verified_at)
                VALUES (:id,'bob@isolation.test',:hash,'active',now())
            """), {"id": bob, "hash": hash_password(actual_password)})
            for org, name in ((org_a, "isolation owner org"),
                              (org_b, "foreign isolation org")):
                connection.execute(text("""
                    INSERT INTO organizations(id,name,status)
                    VALUES (:org,:name,'active')
                """), {"org": org, "name": name})
            connection.execute(text("""
                INSERT INTO organization_memberships(
                    organization_id,user_id,role,status
                ) VALUES (:org,:user,'owner','active')
            """), {"org": org_a, "user": alice})
            connection.execute(text("""
                INSERT INTO organization_memberships(
                    organization_id,user_id,role,status
                ) VALUES (:org,:user,'owner','active')
            """), {"org": org_b, "user": bob})
            for org, cat in ((org_a, cat_a), (org_b, cat_b)):
                connection.execute(text("""
                    INSERT INTO catalog_versions(
                        id,organization_id,source_sha256,original_filename,rows_count
                    ) VALUES (:cat,:org,:sha,'isolated.csv',1)
                """), {"cat": cat, "org": org, "sha": "a" * 64})
            # HTTP import and quotes need INSERT on append-only snapshots;
            # only active pointer and quote head may be updated.
            for table in (
                "catalog_versions", "catalog_items", "idempotency_records",
                "catalog_active", "quote_documents", "quote_revisions",
                "quote_write_requests",
            ):
                connection.exec_driver_sql(
                    f"GRANT SELECT, INSERT ON TABLE public.{table} TO kpdesk_app"
                )
            connection.execute(text(
                "GRANT UPDATE ON TABLE catalog_active, quote_documents TO kpdesk_app"
            ))
            connection.exec_driver_sql(
                "CREATE ROLE " + auth_login
                + " LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE"
                + " NOREPLICATION NOBYPASSRLS PASSWORD '" + auth_secret + "'"
            )
            created_auth_login = True
            connection.exec_driver_sql(
                "CREATE ROLE " + catalog_login
                + " LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE"
                + " NOREPLICATION NOBYPASSRLS PASSWORD '" + catalog_secret + "'"
            )
            created_catalog_login = True
            connection.exec_driver_sql(
                f"GRANT kpdesk_auth_test TO {auth_login}"
            )
            connection.exec_driver_sql(
                f"GRANT kpdesk_app TO {catalog_login}"
            )
        # Independent physical connections: every Engine uses NullPool.
        auth_engine = _make_engine(
            database, username=auth_login, password=auth_secret,
            port=port, role="kpdesk_auth_test",
        )
        app_engine = _make_engine(
            database, username=catalog_login, password=catalog_secret,
            port=port, role="kpdesk_app",
        )
        with auth_engine.begin() as connection:
            _assert(connection.scalar(text("SELECT current_user")) == "kpdesk_auth_test",
                    "Authentication did not assume its restricted DB role.")
            _assert(connection.scalar(text("SELECT session_user")) == auth_login,
                    "Authentication login identity is not physically separate.")
            first_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        with app_engine.begin() as connection:
            _assert(connection.scalar(text("SELECT current_user")) == "kpdesk_app",
                    "Catalog did not assume its restricted DB role.")
            _assert(connection.scalar(text("SELECT session_user")) == catalog_login,
                    "Catalog login identity is not physically separate.")
            app_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            _assert(first_pid != app_pid, "Auth and catalog reused a physical DB session.")
            _assert(connection.scalar(text("""
                SELECT has_table_privilege(current_user,'public.auth_sessions','INSERT')
            """)) is False, "Catalog app can forge session rows.")
            _assert(connection.scalar(text("""
                SELECT has_function_privilege(
                    current_user,'public.kpdesk_auth_credential(text,text)','EXECUTE'
                )
            """)) is False, "Catalog can steal credential material.")

        service = CommittedLogin(auth_engine)
        test_peer = "192.0.2.42"

        def reject_login(_: int) -> None:
            try:
                service.attempt("missing@isolation.test", "never-correct-123", peer=test_peer)
            except LoginDenied:
                return
            raise AssertionError("Unknown account incorrectly authenticated.")

        # Two threads, distinct physical DB connections, concurrent account+IP updates.
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(reject_login, range(6)))
        with clone.connect() as connection:
            counter = connection.scalar(text("""
                SELECT attempts FROM auth_login_limits
                WHERE bucket = :bucket
            """), {
                "bucket": "a:" + hashlib.sha256(
                    b"missing@isolation.test"
                ).hexdigest(),
            })
            _assert(counter == 6, f"Committed attempts lost across workers: {counter}.")
        # Seventh attempt from a DIFFERENT peer still triggers account lockout.
        reject_different = False
        try:
            service.attempt(
                "missing@isolation.test", "never-correct-123",
                peer="198.51.100.42",
            )
        except LoginDenied:
            reject_different = True
        _assert(reject_different, "Account lockout failed across source IPs.")
        with clone.connect() as connection:
            counter = connection.scalar(text("""
                SELECT attempts FROM auth_login_limits WHERE bucket=:bucket
            """), {"bucket": "a:" + hashlib.sha256(b"missing@isolation.test").hexdigest()})
            _assert(counter == 7, "Denied HTTP-equivalent attempt did not commit.")

        issued = service.attempt(
            "alice@isolation.test", actual_password, peer="203.0.113.5",
        )
        expected_hash = hashlib.sha256(issued.session_token.encode("ascii")).hexdigest()
        with clone.connect() as connection:
            seen = connection.scalar(text("""
                SELECT count(*) FROM auth_sessions WHERE token_sha256=:hash
            """), {"hash": expected_hash})
            _assert(seen == 1, "Committed session invisible to a new DB connection.")
        with app_engine.begin() as connection:
            _assert(
                AuthSessionRepository(connection).authenticate(
                    issued.session_token, operation="write", csrf=issued.csrf_token,
                ).user_id == alice,
                "Catalog cannot validate a previously committed login.",
            )
            connection.execute(text("""
                SELECT set_config('kpdesk.organization_id', :org, true)
            """), {"org": str(org_b)})
            _assert(connection.scalar(text(
                "SELECT count(*) FROM catalog_versions"
            )) == 0, "Forged old org GUC granted unauthenticated catalog access.")
            access = bind_authenticated_tenant(
                connection, session_token=issued.session_token,
                organization_id=org_a, operation="read",
            )
            _assert(access.identity.user_id == alice,
                    "Session-bound RLS returned a forged identity.")
            _assert(connection.scalar(text(
                "SELECT count(*) FROM catalog_versions"
            )) == 1, "Session-bound RLS did not show exactly one own catalog.")
            _assert(connection.scalar(text("""
                SELECT count(*) FROM catalog_versions WHERE id=:foreign
            """), {"foreign": cat_b}) == 0,
                    "Session-bound RLS exposed foreign catalog.")
        _verify_http_transactions(
            app_engine=app_engine, auth_engine=auth_engine, owner_engine=clone,
            org_a=org_a, org_b=org_b, alice=alice, bob=bob,
            actual_password=actual_password,
        )
        revoked = CommittedSessionRevocation(auth_engine).revoke(issued.session_token)
        _assert(revoked, "Authorized independent logout failed.")
        with app_engine.begin() as connection:
            # Even deliberately forged session SHA GUC after server-side
            # revocation may no longer authorize RLS on a fresh connection.
            connection.execute(text("""
                SELECT set_config('kpdesk.session_sha256', :digest, true)
            """), {"digest": expected_hash})
            _assert(connection.scalar(text(
                "SELECT count(*) FROM catalog_versions"
            )) == 0, "Revoked bearer leaked a tenant under independent RLS.")
            try:
                AuthSessionRepository(connection).authenticate(issued.session_token)
            except SessionDenied:
                pass
            else:
                raise AssertionError("Catalog accepted revoked token after COMMIT.")
        print(
            "AUTH_INDEPENDENT_CONNECTIONS_PASSED "
            "catalog_cannot_mint=true denied_counter_committed=7 "
            "concurrent_workers=2 login_committed=true revoke_committed=true "
            "session_bound_rls=true forged_org_guc_denied=true "
            "http_catalog_quote_committed=true http_error_rollback=true "
            "concurrent_quote_revision_protected=true "
            "concurrent_import_deduplicated=true "
            "two_org_http_isolation=true production_untouched=true"
        )
    finally:
        # Explicit ordered cleanup. Never apply FORCE to a DB not created here.
        if app_engine is not None:
            app_engine.dispose()
        if auth_engine is not None:
            auth_engine.dispose()
        if clone is not None:
            clone.dispose()
        try:
            with admin.connect() as connection:
                if created_database:
                    actual = connection.scalar(text("""
                        SELECT shobj_description(oid,'pg_database')
                        FROM pg_database WHERE datname=:name
                    """), {"name": database})
                    if actual != marker:
                        raise RuntimeError("Refusing cleanup: test DB marker differs.")
                    connection.exec_driver_sql(f"DROP DATABASE {database} WITH (FORCE)")
                existing: set[str] = set(connection.execute(text("""
                    SELECT rolname FROM pg_roles
                    WHERE rolname IN (:auth_login, :catalog_login, :auth_test)
                """), {
                    "auth_login": auth_login,
                    "catalog_login": catalog_login,
                    "auth_test": "kpdesk_auth_test",
                }).scalars())
                if created_catalog_login and catalog_login in existing:
                    connection.exec_driver_sql(
                        f"REVOKE kpdesk_app FROM {catalog_login}"
                    )
                    connection.exec_driver_sql(f"DROP ROLE {catalog_login}")
                if created_auth_login and auth_login in existing:
                    if "kpdesk_auth_test" in existing:
                        connection.exec_driver_sql(
                            f"REVOKE kpdesk_auth_test FROM {auth_login}"
                        )
                    connection.exec_driver_sql(f"DROP ROLE {auth_login}")
                if created_database and not initial_test_role and connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='kpdesk_auth_test')")
                ):
                    connection.exec_driver_sql("DROP ROLE kpdesk_auth_test")
        finally:
            admin.dispose()


if __name__ == "__main__":
    verify_independent_connections()
