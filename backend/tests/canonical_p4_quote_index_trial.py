"""A/B/A index experiment on the final 1000-quote disposable A45 clone.

Never installs or drops an index on the working database. Uses exact production
quote-list SQL, restricted RLS and authenticated HTTPS TestClient.
"""
from __future__ import annotations

import statistics
import time
import uuid
from typing import Any

from canonical_p4_performance import _plan_summary, _quantile
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.sql.elements import TextClause

from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

_CANDIDATE_NAME = "ix_p4_clone_quote_org_created_desc_id"
_CREATE = text("""
    CREATE INDEX ix_p4_clone_quote_org_created_desc_id
    ON public.quote_documents (organization_id, created_at DESC, id DESC)
""")
_DROP = text("DROP INDEX public.ix_p4_clone_quote_org_created_desc_id")
_EXPLAIN_REPETITIONS = 9
_HTTP_REPETITIONS = 7


def _assert_disposable_clone(owner: Engine, organization: uuid.UUID) -> None:
    with owner.connect() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError("P4 index experiment requires a marked disposable clone")
        if connection.scalar(text("SELECT current_user")) != "kpdesk":
            raise RuntimeError("P4 index experiment requires owner of marked clone")
        if connection.scalar(text("SELECT version_num FROM alembic_version")) != (
            "0030_tenant_llm_cache"
        ):
            raise RuntimeError("P4 index experiment schema mismatch")
        if connection.scalar(text("""
            SELECT count(*) FROM public.quote_documents WHERE organization_id=:org
        """), {"org": organization}) != 1000:
            raise RuntimeError("P4 index experiment requires 1000 seeded quotes")


def _index_exists(owner: Engine) -> bool:
    with owner.connect() as connection:
        return connection.scalar(text("""
            SELECT to_regclass('public.ix_p4_clone_quote_org_created_desc_id')
        """)) is not None


def _phase(
    *, tenant: Engine, organization: uuid.UUID, session_token: str,
    client: TestClient, list_url: str, explain_sql: TextClause,
) -> tuple[dict[str, object], tuple[str, ...]]:
    # Both EXPLAIN and HTTP route run under the real tenant session and RLS.
    plans = []
    for _ in range(_EXPLAIN_REPETITIONS):
        with tenant.begin() as connection:
            bind_authenticated_tenant(
                connection, session_token=session_token,
                organization_id=organization, operation="read",
            )
            plans.append(_plan_summary(
                connection.scalar(explain_sql, {"org": organization}),
            ))
    if any(plan["actual_rows"] != 50 for plan in plans):
        raise AssertionError("P4 index plan unexpectedly changed LIMIT 50")
    request_times = []
    result_ids: tuple[str, ...] | None = None
    for _ in range(_HTTP_REPETITIONS):
        started = time.perf_counter_ns()
        response = client.get(list_url)
        elapsed = (time.perf_counter_ns() - started) / 1e6
        if response.status_code != 200:
            raise AssertionError("P4 index comparison lost authenticated HTTP access")
        items = response.json()["items"]
        if not isinstance(items, list) or len(items) != 50:
            raise AssertionError("P4 index comparison did not return 50 quotes")
        ids = tuple(str(item["quote_id"]) for item in items)
        if len(set(ids)) != 50:
            raise AssertionError("P4 index comparison returned duplicate quotes")
        if result_ids is not None and result_ids != ids:
            raise AssertionError("P4 index comparison changed pagination within phase")
        result_ids = ids
        request_times.append(elapsed)
    assert result_ids is not None
    plan_times: list[float] = []
    plan_indexes: set[str] = set()
    buffer_hits: list[int] = []
    for plan in plans:
        plan_elapsed = plan["execution_ms"]
        indexes = plan["index_names"]
        hits = plan["shared_hit_blocks"]
        if not isinstance(plan_elapsed, (int, float)) or not isinstance(hits, int):
            raise TypeError("P4 EXPLAIN timing/buffers must be numeric")
        if not isinstance(indexes, list) or any(
            not isinstance(name, str) for name in indexes
        ):
            raise TypeError("P4 EXPLAIN index names must be strings")
        plan_times.append(float(plan_elapsed))
        plan_indexes.update(indexes)
        buffer_hits.append(hits)
    result: dict[str, object] = {
        "explain_samples": _EXPLAIN_REPETITIONS,
        "explain_p50_ms": _quantile(plan_times, .5),
        "explain_p95_ms": _quantile(plan_times, .95),
        "explain_median_ms": round(statistics.median(plan_times), 3),
        "http_samples": _HTTP_REPETITIONS,
        "http_p50_ms": _quantile(request_times, .5),
        "http_p95_ms": _quantile(request_times, .95),
        "plan_index_names": sorted(plan_indexes),
        "candidate_index_used": _CANDIDATE_NAME in plan_indexes,
        "buffer_hits_median": statistics.median(buffer_hits),
    }
    return result, result_ids


def compare_quote_list_index(
    *, owner: Engine, tenant: Engine, organization: uuid.UUID,
    session_token: str, client: TestClient,
    list_url: str, explain_sql: TextClause,
) -> dict[str, Any]:
    """Baseline -> candidate -> baseline, then verify the index is removed.

    Owner DDL is restricted to a provenance-marked temporary clone. No index
    change is recommended solely from this short synthetic benchmark.
    """
    _assert_disposable_clone(owner, organization)
    if _index_exists(owner):
        raise RuntimeError("P4 index candidate unexpectedly already exists")
    # Shared statistics for comparable starting conditions; clone only.
    with owner.begin() as connection:
        connection.execute(text("ANALYZE public.quote_documents"))
    baseline, expected = _phase(
        tenant=tenant, organization=organization, session_token=session_token,
        client=client, list_url=list_url, explain_sql=explain_sql,
    )
    candidate: dict[str, object]
    baseline_recheck: dict[str, object]
    try:
        _assert_disposable_clone(owner, organization)
        with owner.begin() as connection:
            connection.execute(_CREATE)
            connection.execute(text("ANALYZE public.quote_documents"))
        candidate, candidate_ids = _phase(
            tenant=tenant, organization=organization, session_token=session_token,
            client=client, list_url=list_url, explain_sql=explain_sql,
        )
        if candidate_ids != expected:
            raise AssertionError("Candidate index changed quote ordering/content")
    finally:
        # Guard again so even a misplaced call cannot drop an index on live DB.
        _assert_disposable_clone(owner, organization)
        if _index_exists(owner):
            with owner.begin() as connection:
                connection.execute(_DROP)
    if _index_exists(owner):
        raise AssertionError("Temporary P4 index still exists after experiment")
    baseline_recheck, recheck_ids = _phase(
        tenant=tenant, organization=organization, session_token=session_token,
        client=client, list_url=list_url, explain_sql=explain_sql,
    )
    if recheck_ids != expected:
        raise AssertionError("Quote ordering/content changed after dropping index")
    return {
        "candidate_index": _CANDIDATE_NAME,
        "candidate_ddl_only_on_disposable_clone": True,
        "candidate_dropped": True,
        "sequence": "baseline / candidate / baseline_recheck",
        "http_result_order_same_in_all_phases": True,
        "baseline": baseline,
        "candidate": candidate,
        "baseline_recheck": baseline_recheck,
        "notes": "9 restricted-RLS EXPLAIN ANALYZE per phase; 7 sequential warmed TestClient GETs; synthetic copies; order/cache/system noise; not production SLA or statistically significant speedup",
    }
