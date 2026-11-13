"""A34 source-bound, authenticated request candidate review API tests."""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from postgres_clone_connection import drop_legacy_test_clone as drop_test_clone

from kpdesk.api import tenant_candidate_review_routes as routes
from kpdesk.api.tenant_session_routes import get_unreleased_auth_repository
from kpdesk.main import create_app
from kpdesk.matching.real_requests import build_request_split_manifest, load_request_intake
from kpdesk.matching.request_annotations import CandidateDecision, _sha
from kpdesk.matching.request_candidate_queue import build_request_candidate_queue
from kpdesk.matching.schema import CatalogProduct
from kpdesk.matching.tenant_candidate_evidence import (
    VerifiedCandidateEvidence,
    verify_candidate_archive,
)

ORG = uuid.UUID("95000000-0000-4000-8000-000000000001")
REVIEWER = uuid.UUID("95000000-0000-4000-8000-000000000002")
ROUTE = f"/api/session/organizations/{ORG}/candidate-reviews"


def make_bundle(tmp_path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    sources = tmp_path / "sources"
    sources.mkdir()
    records: list[dict[str, Any]] = []
    variants = (
        "плотная цветная бумага формата А4",
        "тонер для лазерного принтера HP",
        "набор металлических скрепок 50 мм",
        "файлы вкладыши полипропиленовые",
        "картонные архивные коробки",
        "кабель USB Type-C длиной два метра",
    )
    for index in range(6):
        quote = (
            f"Нужны {variants[index]}, партия номер {index + 1}; "
            "количество для заявки 15 упаковок."
        )
        original = sources / f"batch-{index}.txt"
        original.write_text("Оригинальный заказ\n" + quote, encoding="utf-8")
        records.append({
            "request_id": f"candidate-request-{index}",
            "origin_declared": "customer_received",
            "source_batch_id": f"candidate-batch-{index}",
            "source_file": original.name,
            "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "source_quote": quote,
            "request_text": quote,
            "captured_at": "2026-10-22T14:00:00+02:00",
            "rights_status": "review_required",
            "privacy_status": "review_required",
        })
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({
        "schema_version": "real-request-intake-v1", "records": records,
    }, ensure_ascii=False), encoding="utf-8")
    intake = load_request_intake(dataset, sources)
    split = build_request_split_manifest(intake)
    products = {
        sku: CatalogProduct(
            sku=sku, name=name, category="stationery_other",
            sale_unit="штука", pieces_per_sale_unit=1,
            price_rub=Decimal("50.00"), attributes={"product_kind": "item"},
        ) for sku, name in (
            ("SKU-A", "Бумага для офиса А4"),
            ("SKU-B", "Цветная плотная бумага формат А4"),
            ("SKU-C", "Архивная папка А4"),
        )
    }
    queue = build_request_candidate_queue(intake, split, products, candidates_per_request=3)
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("dataset.json", dataset.read_bytes())
            archive.writestr("split.json", json.dumps(split, ensure_ascii=False))
            archive.writestr("catalog.json", json.dumps({
                "catalog": [item.model_dump(mode="json") for item in products.values()],
            }, ensure_ascii=False))
            archive.writestr("queue.json", json.dumps(queue, ensure_ascii=False))
            for original in sorted(sources.iterdir()):
                archive.writestr("sources/" + original.name, original.read_bytes())
        payload = output.getvalue()
    evidence = verify_candidate_archive(payload)
    assert evidence.queue_sha256 == _sha(queue)
    tasks = queue["tasks"]
    assert isinstance(tasks, list)
    task = tasks[0]
    decision = {
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
        "queue_sha256": evidence.queue_sha256,
        "request_id": task["request_id"],
        "task_sha256": _sha(task),
        "accepted_skus": [],
        "rejected_skus": task["candidate_skus"],
        "unresolved_skus": [],
        "no_candidate_match": True,
        "rationale": "Проверены все предложенные позиции относительно исходной заявки, совпадений нет.",
        "personally_reviewed": True,
    }
    return payload, decision, queue


def test_tenant_candidate_routes_absent_in_legacy_and_require_session() -> None:
    with TestClient(create_app(testing=True)) as legacy:
        assert legacy.get(ROUTE, params={"queue_sha256": "a" * 64}).status_code == 404
    with TestClient(
        create_app(testing=True, tenant_sessions=True),
        base_url="https://testserver",
    ) as client:
        assert client.get(ROUTE, params={"queue_sha256": "a" * 64}).status_code == 401
        assert client.post(
            ROUTE + "/inspect",
            headers={"Origin": "https://localhost:5173", "X-CSRF-Token": "fake"},
            files={"evidence": ("bundle.zip", b"not-a-zip", "application/zip")},
        ).status_code == 401
        request = client.post(ROUTE, headers={
            "Origin": "https://localhost:5173",
            "X-CSRF-Token": "fake",
        }, files={"evidence": ("review.zip", b"not-a-zip", "application/zip")},
            data={"review_json": "{}"})
        assert request.status_code == 401


def test_candidate_archive_rechecks_exact_originals_split_and_no_extra_files(tmp_path: Path) -> None:
    payload, decision, queue = make_bundle(tmp_path)
    result = verify_candidate_archive(payload)
    assert result.queue_sha256 == decision["queue_sha256"]
    assert len(result.tasks) == len(queue["tasks"])
    assert all(task["partition"] != "test" for task in result.tasks.values())
    with io.BytesIO(payload) as source, zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["queue.json"] = members["queue.json"].replace(b"SKU-A", b"SKU-Z")
    with io.BytesIO() as changed:
        with zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        tampered = changed.getvalue()
    with pytest.raises(ValueError, match="Candidate queue|modified|provenance"):
        verify_candidate_archive(tampered)
    members["sources/../../other.txt"] = b"attack"
    with io.BytesIO() as changed:
        with zipfile.ZipFile(changed, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        traversal = changed.getvalue()
    with pytest.raises(ValueError, match="Unsafe"):
        verify_candidate_archive(traversal)


def test_candidate_write_contract_rejects_tampering_before_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, decision, _ = make_bundle(tmp_path)
    observed: list[str] = []
    fake_scope = SimpleNamespace(identity=SimpleNamespace(user_id=REVIEWER))
    class FakeStore:
        def __init__(self, scope: object) -> None:
            assert scope is fake_scope

        def _authorize(self, *, write: bool) -> None:
            assert write

        def attest(self, evidence: VerifiedCandidateEvidence, review: CandidateDecision) -> bool:
            observed.append(str(review.request_id))
            return True

    monkeypatch.setattr(routes, "_store", lambda *_args, **_kwargs: fake_scope)
    monkeypatch.setattr(routes, "TenantCandidateReviewStore", FakeStore)
    app = create_app(testing=True, tenant_sessions=True)
    app.dependency_overrides[get_unreleased_auth_repository] = lambda: object()
    headers = {"Origin": "https://localhost:5173", "X-CSRF-Token": "mock"}
    with TestClient(app, base_url="https://testserver") as client:
        def post(body: dict[str, Any], original: bytes = payload) -> Response:
            response: Response = client.post(
                ROUTE, headers=headers, files={"evidence": ("source.zip", original)},
                data={"review_json": json.dumps(body, ensure_ascii=False)},
            )
            return response

        wrong_sha = {**decision, "archive_sha256": "0" * 64}
        assert post(wrong_sha).status_code == 409
        wrong_task = {**decision, "task_sha256": "0" * 64}
        assert post(wrong_task).status_code == 422
        forged = {**decision, "rejected_skus": ["FAKE"]}
        assert post(forged).status_code == 422
        absent = dict(decision)
        absent.pop("personally_reviewed")
        assert post(absent).status_code == 422
        assert observed == []
        accepted = post(decision)
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["recorded"] is True
        assert accepted.json()["reviewer_user_id"] == str(REVIEWER)
        assert accepted.json()["independent_gold"] is False
        assert accepted.json()["training_approved"] is False
        assert observed == [decision["request_id"]]
        assert accepted.headers["cache-control"] == "no-store"


@pytest.mark.skipif(
    __import__("os").environ.get("KP_DESK_RUN_ISOLATED_CANDIDATE_PG") != "1",
    reason="Opt-in disposable PostgreSQL clone and actual authenticated HTTPS session",
)
def test_candidate_sql_audit_and_https_with_actual_tenant_session(tmp_path: Path) -> None:
    import importlib
    import re
    import secrets
    import subprocess
    from collections.abc import Iterator

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from dotenv import dotenv_values
    from sqlalchemy import Engine, create_engine, text
    from sqlalchemy.engine import URL
    from sqlalchemy.exc import DBAPIError

    from kpdesk.api.tenant_session_routes import SESSION_COOKIE
    from kpdesk.db.auth_sessions import AuthSessionRepository

    credentials = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    password = credentials.get("KP_DESK_DB_PASSWORD")
    assert isinstance(password, str) and len(password) >= 32

    def connect(database: str, *, autocommit: bool = False) -> Engine:
        return create_engine(URL.create(
            "postgresql+psycopg", username="kpdesk", password=password,
            host="127.0.0.1", port=int(credentials.get("KP_DESK_DB_PORT") or 55432),
            database=database,
        ), isolation_level="AUTOCOMMIT" if autocommit else None)

    nonce = secrets.token_hex(6)
    name = f"kpdesk_accounts_http_{nonce}"
    assert re.fullmatch(r"kpdesk_accounts_http_[0-9a-f]{12}", name)
    admin = connect("postgres", autocommit=True)
    created = False
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 OWNER kpdesk')
            created = True
            conn.exec_driver_sql(
                f'COMMENT ON DATABASE "{name}" IS \'kpdesk:accounts-http:{nonce}\''
            )
        with subprocess.Popen([
            "docker", "exec", "kpdesk-db-1", "pg_dump",
            "-U", "kpdesk", "-d", "kpdesk", "-Fc", "--no-owner",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as backup:
            assert backup.stdout is not None
            restored = subprocess.run([
                "docker", "exec", "-i", "kpdesk-db-1", "pg_restore",
                "-U", "kpdesk", "-d", name, "--exit-on-error", "--no-owner",
            ], stdin=backup.stdout, capture_output=True, timeout=150, check=False)
            backup.stdout.close()
            assert backup.wait(timeout=150) == 0
            assert restored.returncode == 0, restored.stderr.decode(errors="replace")[-650:]

        engine = connect(name)
        try:
            with engine.connect() as conn:
                transaction = conn.begin()
                try:
                    source_revision = conn.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    assert source_revision in (
                        "0024_supplier_unit_reviews", "0025_request_candidate_audit",
                        "0026_request_source_restrictions", "0027_candidate_adjudications", "0028_quote_manager_decisions", "0029_manager_decisions_read", "0030_tenant_llm_cache", "0031_catalog_taxonomy_v4",
                    )
                    if source_revision == "0024_supplier_unit_reviews":
                        migration = importlib.import_module(
                            "migrations.versions.0025_request_candidate_audit"
                        )
                        with Operations.context(MigrationContext.configure(conn)):
                            migration.upgrade()
                    if source_revision in (
                        "0024_supplier_unit_reviews", "0025_request_candidate_audit",
                    ):
                        migration = importlib.import_module(
                            "migrations.versions.0026_request_source_restrictions"
                        )
                        with Operations.context(MigrationContext.configure(conn)):
                            migration.upgrade()
                    if source_revision not in (
                        "0027_candidate_adjudications", "0028_quote_manager_decisions", "0029_manager_decisions_read", "0030_tenant_llm_cache", "0031_catalog_taxonomy_v4",
                    ):
                        migration = importlib.import_module(
                            "migrations.versions.0027_candidate_adjudications"
                        )
                        with Operations.context(MigrationContext.configure(conn)):
                            migration.upgrade()
                    assert conn.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass('public.tenant_request_candidate_adjudications')
                    """)).one() == (True, True)
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                          'kpdesk_tenant_runtime',
                          'public.tenant_request_candidate_adjudications','INSERT'
                        )
                    """)) is False
                    assert conn.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity FROM pg_class
                        WHERE oid=to_regclass('public.tenant_request_source_restrictions')
                    """)).one() == (True, True)
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                          'kpdesk_tenant_runtime',
                          'public.tenant_request_source_restrictions','INSERT'
                        )
                    """)) is False
                    assert conn.execute(text("""
                        SELECT relrowsecurity,relforcerowsecurity
                        FROM pg_class
                        WHERE oid=to_regclass('public.tenant_request_candidate_reviews')
                    """)).one() == (True, True)
                    assert conn.scalar(text("""
                        SELECT has_table_privilege(
                          'kpdesk_tenant_runtime',
                          'public.tenant_request_candidate_reviews','INSERT'
                        )
                    """)) is False
                    assert conn.scalar(text("""
                        SELECT has_function_privilege(
                          'kpdesk_tenant_runtime',
                          'public.kpdesk_candidate_review_attest(uuid,text,text,text,text,text,text,text,text,text,jsonb,jsonb,jsonb,jsonb,boolean,text)',
                          'EXECUTE'
                        )
                    """)) is True
                    outsider = uuid.uuid4()
                    conn.execute(text("""
                        INSERT INTO organizations(id,name,status)
                        VALUES(:org,'Candidate review test','active'),
                              (:other,'Candidate other org','active')
                    """), {"org": ORG, "other": outsider})
                    conn.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES(:id,:email,'active',clock_timestamp())
                    """), {"id": REVIEWER, "email": f"reviewer-{nonce}@invalid.local"})
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                          organization_id,user_id,role,status
                        ) VALUES(:org,:user,'editor','active')
                    """), {"org": ORG, "user": REVIEWER})
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    conn.execute(text("""
                        INSERT INTO auth_sessions(token_sha256,user_id,csrf_sha256,expires_at)
                        VALUES(:digest,:user,:csrf,clock_timestamp()+interval '1 hour')
                    """), {
                        "digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                        "user": REVIEWER,
                        "csrf": hashlib.sha256(csrf.encode("ascii")).hexdigest(),
                    })
                    # A second independently authenticated editor reviews the
                    # same source-bound task under a different real session.
                    second = uuid.uuid4()
                    second_token, second_csrf = (
                        secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    )
                    conn.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES(:id,:email,'active',clock_timestamp())
                    """), {"id": second, "email": f"reviewer2-{nonce}@invalid.local"})
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                          organization_id,user_id,role,status
                        ) VALUES(:org,:user,'editor','active')
                    """), {"org": ORG, "user": second})
                    conn.execute(text("""
                        INSERT INTO auth_sessions(token_sha256,user_id,csrf_sha256,expires_at)
                        VALUES(:digest,:user,:csrf,clock_timestamp()+interval '1 hour')
                    """), {
                        "digest": hashlib.sha256(second_token.encode("ascii")).hexdigest(),
                        "user": second,
                        "csrf": hashlib.sha256(second_csrf.encode("ascii")).hexdigest(),
                    })
                    # Independent owner is not one of the original two reviewers.
                    owner_id = uuid.uuid4()
                    owner_token, owner_csrf = (
                        secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    )
                    conn.execute(text("""
                        INSERT INTO users(id,email,status,email_verified_at)
                        VALUES(:id,:email,'active',clock_timestamp())
                    """), {"id": owner_id, "email": f"arbiter-{nonce}@invalid.local"})
                    conn.execute(text("""
                        INSERT INTO organization_memberships(
                          organization_id,user_id,role,status
                        ) VALUES(:org,:user,'owner','active')
                    """), {"org": ORG, "user": owner_id})
                    conn.execute(text("""
                        INSERT INTO auth_sessions(token_sha256,user_id,csrf_sha256,expires_at)
                        VALUES(:digest,:user,:csrf,clock_timestamp()+interval '1 hour')
                    """), {
                        "digest": hashlib.sha256(owner_token.encode("ascii")).hexdigest(),
                        "user": owner_id,
                        "csrf": hashlib.sha256(owner_csrf.encode("ascii")).hexdigest(),
                    })
                    conn.execute(text("SET ROLE kpdesk_tenant_runtime"))
                    def live_auth() -> Iterator[AuthSessionRepository]:
                        yield AuthSessionRepository(conn)
                    app = create_app(testing=True, tenant_sessions=True)
                    app.dependency_overrides[get_unreleased_auth_repository] = live_auth
                    payload, decision, queue = make_bundle(tmp_path)
                    headers = {
                        "Origin": "https://localhost:5173",
                        "X-CSRF-Token": csrf,
                        "Cookie": f"{SESSION_COOKIE}={token}",
                    }
                    with TestClient(app, base_url="https://testserver") as client:
                        def submit(body: dict[str, Any]) -> Response:
                            result: Response = client.post(
                                ROUTE, headers=headers,
                                files={"evidence": ("candidate.zip", payload)},
                                data={"review_json": json.dumps(body, ensure_ascii=False)},
                            )
                            return result

                        preview = client.post(
                            ROUTE + "/inspect", headers=headers,
                            files={"evidence": ("candidate.zip", payload)},
                            data={"offset": "0", "limit": "2"},
                        )
                        assert preview.status_code == 200, preview.text
                        assert preview.json()["queue_sha256"] == decision["queue_sha256"]
                        assert len(preview.json()["tasks"]) == 2
                        assert preview.json()["tasks"][0]["candidates"][0]["name"]
                        assert preview.json()["training_approved"] is False
                        assert preview.headers["cache-control"] == "no-store"

                        first = submit(decision)
                        assert first.status_code == 201, first.text
                        assert first.json()["reviewer_user_id"] == str(REVIEWER)
                        assert first.json()["training_approved"] is False
                        assert submit(decision).status_code == 201
                        consensus_before = client.get(
                            ROUTE + "/consensus",
                            params={"queue_sha256": decision["queue_sha256"]},
                            headers=headers,
                        )
                        assert consensus_before.status_code == 200, consensus_before.text
                        assert not consensus_before.json()["agreed"]
                        assert len(consensus_before.json()["incomplete"]) == 1
                        second_headers = {
                            **headers, "X-CSRF-Token": second_csrf,
                            "Cookie": f"{SESSION_COOKIE}={second_token}",
                        }
                        second_review = client.post(
                            ROUTE, headers=second_headers,
                            files={"evidence": ("candidate.zip", payload)},
                            data={"review_json": json.dumps(decision, ensure_ascii=False)},
                        )
                        assert second_review.status_code == 201, second_review.text
                        assert second_review.json()["reviewer_user_id"] == str(second)
                        consensus_after = client.get(
                            ROUTE + "/consensus",
                            params={"queue_sha256": decision["queue_sha256"]},
                            headers=headers,
                        )
                        assert consensus_after.status_code == 200, consensus_after.text
                        assert len(consensus_after.json()["agreed"]) == 1
                        assert consensus_after.json()["agreed"][0]["reviewer_count"] == 2
                        assert consensus_after.json()["training_approved"] is False

                        # Another request receives two incompatible human judgments.
                        from kpdesk.matching.request_annotations import _sha
                        second_case = queue["tasks"][1]
                        dispute = {
                            **decision,
                            "request_id": second_case["request_id"],
                            "task_sha256": _sha(second_case),
                            "accepted_skus": [],
                            "rejected_skus": second_case["candidate_skus"],
                            "no_candidate_match": True,
                        }
                        second_opinion = {
                            **dispute,
                            "accepted_skus": second_case["candidate_skus"][:1],
                            "rejected_skus": second_case["candidate_skus"][1:],
                            "no_candidate_match": False,
                        }
                        assert submit(dispute).status_code == 201
                        different = client.post(
                            ROUTE, headers=second_headers,
                            files={"evidence": ("candidate.zip", payload)},
                            data={"review_json": json.dumps(second_opinion)},
                        )
                        assert different.status_code == 201, different.text
                        conflicts = client.get(
                            ROUTE + "/consensus",
                            params={"queue_sha256": decision["queue_sha256"]},
                            headers=headers,
                        )
                        assert conflicts.status_code == 200, conflicts.text
                        assert len(conflicts.json()["conflicts"]) == 1
                        owner_headers = {
                            **headers, "X-CSRF-Token": owner_csrf,
                            "Cookie": f"{SESSION_COOKIE}={owner_token}",
                        }
                        arbitration = {
                            "archive_sha256": decision["archive_sha256"],
                            "queue_sha256": decision["queue_sha256"],
                            "request_id": dispute["request_id"],
                            "task_sha256": dispute["task_sha256"],
                            "finding": "resolve_proposals",
                            "accepted_skus": second_opinion["accepted_skus"],
                            "rejected_skus": second_opinion["rejected_skus"],
                            "no_candidate_match": False,
                            "rationale": (
                                "Independent owner compared the original request with "
                                "every proposal and resolved the two disagreeing reviews."
                            ),
                            "personally_adjudicated": True,
                        }
                        def resolve(body: dict[str, Any], headers_for_actor: dict[str, str]) -> Response:
                            result: Response = client.post(
                                f"/api/session/organizations/{ORG}/candidate-adjudications",
                                headers=headers_for_actor,
                                files={"evidence": ("candidate.zip", payload)},
                                data={"review_json": json.dumps(body)},
                            )
                            return result
                        assert resolve(arbitration, headers).status_code == 409
                        decided = resolve(arbitration, owner_headers)
                        assert decided.status_code == 201, decided.text
                        assert decided.json()["adjudicator_user_id"] == str(owner_id)
                        assert decided.json()["independent_gold"] is False
                        assert decided.json()["training_approved"] is False
                        assert resolve(arbitration, owner_headers).status_code == 201
                        assert resolve(
                            {**arbitration, "finding": "needs_evidence",
                             "accepted_skus": [], "rejected_skus": [],
                             "rationale": "The supporting document requires another check."},
                            owner_headers,
                        ).status_code == 409
                        adjudications = client.get(
                            f"/api/session/organizations/{ORG}/candidate-adjudications",
                            params={"queue_sha256": decision["queue_sha256"]},
                            headers=owner_headers,
                        )
                        assert adjudications.status_code == 200, adjudications.text
                        assert len(adjudications.json()["rows"]) == 1
                        assert adjudications.json()["rows"][0]["original_reviewer_count"] == 2
                        assert adjudications.json()["training_approved"] is False
                        assert resolve(
                            {**arbitration, "request_id": decision["request_id"],
                             "task_sha256": decision["task_sha256"]},
                            owner_headers,
                        ).status_code == 409

                        from kpdesk.matching.tenant_candidate_evidence import (
                            verify_candidate_archive,
                        )
                        verified = verify_candidate_archive(payload)
                        source_sha = next(iter(verified.source_sha256s))
                        restriction = {
                            "archive_sha256": hashlib.sha256(payload).hexdigest(),
                            "queue_sha256": decision["queue_sha256"],
                            "source_sha256": source_sha,
                            "review_kind": "rights",
                            "finding": "requires_evidence",
                            "evidence_sha256": None,
                            "rationale": "Original customer request requires independent license review.",
                            "personally_reviewed": True,
                        }
                        def restrict(body: dict[str, Any]) -> Response:
                            result: Response = client.post(
                                ROUTE + "/source-restrictions", headers=headers,
                                files={"evidence": ("candidate.zip", payload)},
                                data={"review_json": json.dumps(body, ensure_ascii=False)},
                            )
                            return result
                        assert restrict(restriction).status_code == 201
                        assert restrict(restriction).status_code == 201
                        assert restrict({**restriction, "finding": "rejected"}).status_code == 409
                        assert restrict({**restriction, "finding": "approved"}).status_code == 422
                        history = client.get(
                            ROUTE + "/source-restrictions",
                            params={"queue_sha256": decision["queue_sha256"]},
                            headers=headers,
                        )
                        assert history.status_code == 200, history.text
                        assert len(history.json()["rows"]) == 1
                        assert history.json()["training_approved"] is False
                        assert history.json()["rights_verified"] is False

                        seen = client.get(ROUTE, params={
                            "queue_sha256": decision["queue_sha256"],
                        }, headers=headers)
                        assert seen.status_code == 200, seen.text
                        assert len(seen.json()["rows"]) == 4
                        assert seen.json()["rows"][0]["reviewer_user_id"] == str(REVIEWER)
                        assert seen.json()["rows"][0]["no_candidate_match"] is True
                        assert seen.json()["independent_gold"] is False

                        conflicting = dict(decision)
                        candidate_skus = decision["rejected_skus"]
                        conflicting["accepted_skus"] = candidate_skus[:1]
                        conflicting["rejected_skus"] = candidate_skus[1:]
                        conflicting["no_candidate_match"] = False
                        conflicting["rationale"] = (
                            "A second conflicting determination cannot rewrite "
                            "the same authenticated reviewer record."
                        )
                        conflict = submit(conflicting)
                        assert conflict.status_code == 409, conflict.text

                        foreign = client.get(
                            f"/api/session/organizations/{outsider}/candidate-reviews",
                            params={"queue_sha256": decision["queue_sha256"]},
                            headers=headers,
                        )
                        assert foreign.status_code == 403
                    conn.execute(text("RESET ROLE"))
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            UPDATE public.tenant_request_candidate_reviews
                            SET rationale='Attempt to rewrite an immutable human opinion'
                            WHERE organization_id=:org
                        """), {"org": ORG})
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            DELETE FROM public.tenant_request_source_restrictions
                            WHERE organization_id=:org
                        """), {"org": ORG})
                    with pytest.raises(DBAPIError), conn.begin_nested():
                        conn.execute(text("""
                            UPDATE public.tenant_request_candidate_adjudications
                            SET rationale='Attempt to override a signed dispute resolution'
                            WHERE organization_id=:org
                        """), {"org": ORG})
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
    finally:
        try:
            if created:
                drop_test_clone(admin, name)
        finally:
            admin.dispose()
