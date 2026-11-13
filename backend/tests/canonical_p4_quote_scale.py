"""Clone-only quote list scale check after every other A45 scenario finishes.

Replicates a real, previously validated immutable quote snapshot, not a
claimed sample of independent customer documents. The marked clone is
destroyed after this check; immutable revisions must never be deleted.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from canonical_p4_performance import _plan_summary, _quantile
from canonical_p4_quote_index_trial import compare_quote_list_index
from canonical_p4_quote_query_trial import compare_quote_list_sql
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

_QUOTE_COUNT_TARGETS = (100, 1000)
_SAMPLES = 5
_LIST_SQL = text("""
    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
    SELECT d.id,d.current_revision,r.total_rub,r.is_complete
    FROM public.quote_documents d
    JOIN public.quote_revisions r
      ON r.organization_id=d.organization_id
     AND r.quote_id=d.id AND r.number=d.current_revision
    WHERE d.organization_id=:org
    ORDER BY d.created_at DESC,d.id DESC
    LIMIT 50
""")


def _seed_immutable_quote_copies(
    owner: Engine, organization: uuid.UUID, template_id: uuid.UUID,
    session: IssuedSession, copies: int,
) -> None:
    """Insert copies with actual PostgreSQL constraints, triggers and membership."""
    if not 1 <= copies <= 1000:
        raise ValueError("Quote scale must stay within the reviewed fixture limit")
    identifiers = [uuid.uuid4() for _ in range(copies)]
    with owner.begin() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError("Quote scale requires marked disposable clone")
        if connection.scalar(text("SELECT current_user")) != "kpdesk":
            raise RuntimeError("Quote scale requires the verified clone owner")
        if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
            "0030_tenant_llm_cache"
        ):
            raise RuntimeError("Unexpected quote scale schema")
        digest = hashlib.sha256(session.session_token.encode("ascii")).hexdigest()
        connection.execute(text("""
            SELECT set_config('kpdesk.session_sha256', :digest, true)
        """), {"digest": digest})
        if connection.scalar(text("""
            SELECT public.kpdesk_session_tenant_allowed(:org, 'write')
        """), {"org": organization}) is not True:
            raise RuntimeError("Session lacks quote write access in scale clone")
        # SELECT a real immutable revision, preserving its SHA and verified PDF
        # contents; the AFTER INSERT manager-decision trigger still runs.
        source = connection.execute(text("""
            SELECT number FROM public.quote_revisions
            WHERE organization_id=:org AND quote_id=:template AND number=1
        """), {"org": organization, "template": template_id}).one_or_none()
        if source is None:
            raise RuntimeError("Quote scale template is not a real revision")
        params = {"org": organization, "ids": identifiers, "template": template_id}
        documents = connection.execute(text("""
            INSERT INTO public.quote_documents(id,organization_id,current_revision)
            SELECT generated.id,:org,1
            FROM unnest(CAST(:ids AS uuid[])) AS generated(id)
        """), params)
        if documents.rowcount != copies:
            raise AssertionError("Quote scale document insertion count mismatch")
        revisions = connection.execute(text("""
            INSERT INTO public.quote_revisions(
                organization_id,quote_id,number,catalog_version_id,
                snapshot,snapshot_sha256,is_complete,total_rub
            )
            SELECT :org,generated.id,r.number,r.catalog_version_id,
                   r.snapshot,r.snapshot_sha256,r.is_complete,r.total_rub
            FROM unnest(CAST(:ids AS uuid[])) AS generated(id)
            CROSS JOIN public.quote_revisions r
            WHERE r.organization_id=:org AND r.quote_id=:template AND r.number=1
        """), params)
        if revisions.rowcount != copies:
            raise AssertionError("Quote scale revision insertion count mismatch")


def verify_quote_list_scale(
    *, owner: Engine, tenant: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, template_id: str, session: IssuedSession,
    factory: Callable[[str], FastAPI], output_dir: Path,
) -> dict[str, object]:
    """Run last on A45 clone. Five timed sequential HTTPS TestClient reads."""
    with owner.connect() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError("Scale test cannot target a non-clone database")
        initial = connection.scalar(text("""
            SELECT count(*) FROM public.quote_documents WHERE organization_id=:org
        """), {"org": organization})
    if not isinstance(initial, int) or initial >= _QUOTE_COUNT_TARGETS[0]:
        raise RuntimeError("Unexpected base quote count for synthetic scale test")
    original_quote_count = initial

    scenarios: list[dict[str, object]] = []
    with TestClient(factory("https://testserver"), base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, session.session_token)
        url = f"/api/session/organizations/{organization}/quotes"
        pdf_url = f"{url}/{template_id}/pdf?revision=1"
        # Warm lazy app routing before any measured SQL or HTTP request.
        if client.get(url).status_code != 200:
            raise AssertionError("Authenticated quote scale warmup failed")
        for target in _QUOTE_COUNT_TARGETS:
            _seed_immutable_quote_copies(
                owner, organization, uuid.UUID(template_id), session, target - initial,
            )
            initial = target
            with owner.connect() as connection:
                actual = connection.scalar(text("""
                    SELECT count(*) FROM public.quote_documents
                    WHERE organization_id=:org
                """), {"org": organization})
                if actual != target:
                    raise AssertionError("Synthetic quote scale count mismatch")

            sql_count = 0
            def count_sql(
                _conn: object, _cursor: object, _statement: str,
                _parameters: object, _context: object, _many: bool,
            ) -> None:
                nonlocal sql_count
                sql_count += 1

            event.listen(tenant, "before_cursor_execute", count_sql)
            latencies: list[float] = []
            per_request_sql: list[int] = []
            try:
                for _ in range(_SAMPLES):
                    before = sql_count
                    started = time.perf_counter_ns()
                    response = client.get(url)
                    duration = (time.perf_counter_ns() - started) / 1e6
                    if response.status_code != 200:
                        raise AssertionError(f"Scaled quote HTTP denied: {response.status_code}")
                    items = response.json()["items"]
                    if len(items) != 50 or len({item["quote_id"] for item in items}) != 50:
                        raise AssertionError("Scaled quote list lost its LIMIT or uniqueness")
                    measured_sql = sql_count - before
                    if not 1 <= measured_sql <= 10:
                        raise AssertionError(
                            f"Scaled quote list SQL budget exceeded: {measured_sql}"
                        )
                    per_request_sql.append(measured_sql)
                    latencies.append(duration)
            finally:
                event.remove(tenant, "before_cursor_execute", count_sql)

            with tenant.begin() as connection:
                bind_authenticated_tenant(
                    connection, session_token=session.session_token,
                    organization_id=organization, operation="read",
                )
                plan = _plan_summary(
                    connection.scalar(_LIST_SQL, {"org": organization})
                )
            # Check that mass insertion did not invalidate the real template PDF.
            pdf = client.get(pdf_url)
            if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF-"):
                raise AssertionError("Scaled quote source PDF is no longer readable")
            scenarios.append({
                "stored_quotes": target,
                "synthetic_copies_of_one_real_test_quote": target - original_quote_count,
                "http_samples": _SAMPLES,
                "returned_rows": 50,
                "sql_per_request": per_request_sql,
                "http_p50_ms": _quantile(latencies, .5),
                "http_p95_ms": _quantile(latencies, .95),
                "http_max_ms": _quantile(latencies, 1),
                "restricted_rls_explain": plan,
            })
        # Only after 1000 documents exist: A/B/A on the same clone, with
        # the candidate dropped before the A45 scenario finishes.
        index_trial = compare_quote_list_index(
            owner=owner, tenant=tenant, organization=organization,
            session_token=session.session_token, client=client,
            list_url=url, explain_sql=_LIST_SQL,
        )
        query_trial = compare_quote_list_sql(
            owner=owner, tenant=tenant, organization=organization,
            outsider=outsider, session_token=session.session_token,
        )
        # Commit a deliberately incomplete document only in the disposable
        # clone, after all other E2E scenarios and SQL measurements. Check the
        # real HTTP response, not merely a direct owner-role comparison.
        before_orphan = client.get(url)
        if before_orphan.status_code != 200:
            raise AssertionError("P4 orphan HTTP baseline failed")
        original_items = before_orphan.json()["items"]
        if len(original_items) != 50:
            raise AssertionError("P4 orphan HTTP baseline has no full page")
        with owner.begin() as connection:
            if not is_live_accounts_http_clone(connection) or (
                connection.scalar(text("SELECT current_user")) != "kpdesk"
            ):
                raise RuntimeError("P4 committed orphan test requires clone owner")
            connection.execute(text("""
                INSERT INTO public.quote_documents(
                    id,organization_id,current_revision,created_at
                ) VALUES (
                    :id,:org,1,clock_timestamp() + interval '1 day'
                )
            """), {"id": uuid.uuid4(), "org": organization})
        after_orphan = client.get(url)
        if after_orphan.status_code != 200 or (
            after_orphan.json().get("items") != original_items
        ):
            raise AssertionError(
                "HTTP quote list changed after an incomplete clone-only document"
            )
        outsider_attempt = client.get(
            f"/api/session/organizations/{outsider}/quotes"
        )
        if outsider_attempt.status_code != 403:
            raise AssertionError("P4 cross-tenant quote list not fail-closed")

    result: dict[str, object] = {
        "kind": "p4_disposable_clone_quote_list_scale",
        "source": "One original synthetic E2E quote replicated inside a marked clone",
        "claims_production_sla": False,
        "load": "5 sequential HTTPS TestClient reads per quote count, no real TCP/TLS",
        "cleanup": "Only through drop of the entire provenance-marked clone; immutable revisions retained until drop",
        "scenarios": scenarios,
        "index_trial": index_trial,
        "query_trial": query_trial,
        "committed_orphan_http_results_identical": True,
        "committed_orphan_cross_tenant_http_status": 403,
    }
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise RuntimeError("Quote scale evidence directory is unsafe")
    path = output_dir / f"p4-quote-scale-{uuid.uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print("P4_QUOTE_SCALE_EVIDENCE " + str(path))
    print("P4_QUOTE_SCALE " + json.dumps(result, ensure_ascii=False))
    return result
