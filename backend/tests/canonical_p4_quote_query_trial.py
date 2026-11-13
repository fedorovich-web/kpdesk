"""Restricted-role SQL comparison for the tenant quote list.

All writes for malformed-head simulation are uncommitted on a marked clone.
No application SQL is changed without a matching safe-result and performance gate.
"""
from __future__ import annotations

import statistics
import uuid
from collections.abc import Mapping
from typing import Any

from canonical_p4_performance import _plan_summary, _quantile
from sqlalchemy import Connection, Engine, text
from sqlalchemy.sql.elements import TextClause

from kpdesk.api.tenant_quote_routes import _TENANT_QUOTE_LIST_SQL
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

_REPETITIONS = 9

# Historical reference query: exact old INNER JOIN semantics. The live route
# SQL is imported separately and must match its ordered rows and values.
_BASELINE = text("""
    SELECT d.id,d.current_revision,r.total_rub,r.is_complete
    FROM public.quote_documents d
    JOIN public.quote_revisions r
      ON r.organization_id=d.organization_id
     AND r.quote_id=d.id AND r.number=d.current_revision
    WHERE d.organization_id=:org
    ORDER BY d.created_at DESC,d.id DESC
    LIMIT 50
""")
_FAST_PRELIMIT = text("""
    WITH recent AS MATERIALIZED (
        SELECT d.id,d.organization_id,d.current_revision,d.created_at
        FROM public.quote_documents d
        WHERE d.organization_id=:org
        ORDER BY d.created_at DESC,d.id DESC
        LIMIT 50
    )
    SELECT d.id,d.current_revision,r.total_rub,r.is_complete
    FROM recent d
    JOIN public.quote_revisions r
      ON r.organization_id=d.organization_id
     AND r.quote_id=d.id AND r.number=d.current_revision
    ORDER BY d.created_at DESC,d.id DESC
""")
_SAFE_PRELIMIT = text("""
    WITH recent AS MATERIALIZED (
        SELECT d.id,d.organization_id,d.current_revision,d.created_at
        FROM public.quote_documents d
        WHERE d.organization_id=:org
          AND EXISTS (
              SELECT 1 FROM public.quote_revisions eligible
              WHERE eligible.organization_id=d.organization_id
                AND eligible.quote_id=d.id
                AND eligible.number=d.current_revision
          )
        ORDER BY d.created_at DESC,d.id DESC
        LIMIT 50
    )
    SELECT d.id,d.current_revision,r.total_rub,r.is_complete
    FROM recent d
    JOIN public.quote_revisions r
      ON r.organization_id=d.organization_id
     AND r.quote_id=d.id AND r.number=d.current_revision
    ORDER BY d.created_at DESC,d.id DESC
""")
_LATERAL = text("""
    SELECT d.id,d.current_revision,r.total_rub,r.is_complete
    FROM public.quote_documents d
    JOIN LATERAL (
        SELECT rev.total_rub,rev.is_complete
        FROM public.quote_revisions rev
        WHERE rev.organization_id=d.organization_id
          AND rev.quote_id=d.id
          AND rev.number=d.current_revision
        LIMIT 1
    ) r ON true
    WHERE d.organization_id=:org
    ORDER BY d.created_at DESC,d.id DESC
    LIMIT 50
""")
# The benchmark measures the exact SQL executed by the live FastAPI route.
_GUARDED_PRELIMIT = _TENANT_QUOTE_LIST_SQL
_VARIANTS: Mapping[str, TextClause] = {
    "baseline": _BASELINE,
    "fast_prelimit": _FAST_PRELIMIT,
    "safe_prelimit": _SAFE_PRELIMIT,
    "lateral_lookup": _LATERAL,
    "guarded_prelimit": _GUARDED_PRELIMIT,
}


def _row_fingerprints(
    connection: Connection, statement: TextClause, organization: uuid.UUID,
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(statement, {"org": organization}).all()
    return tuple(tuple(row) for row in rows)


def _assert_clone_owner(owner: Engine, organization: uuid.UUID) -> None:
    with owner.connect() as connection:
        if not is_live_accounts_http_clone(connection):
            raise RuntimeError("P4 query trial requires provenance-marked clone")
        if connection.scalar(text("SELECT current_user")) != "kpdesk":
            raise RuntimeError("P4 query trial owner role mismatch")
        if connection.scalar(text("SELECT version_num FROM public.alembic_version")) != (
            "0030_tenant_llm_cache"
        ):
            raise RuntimeError("P4 query trial schema mismatch")
        if connection.scalar(text("""
            SELECT count(*) FROM public.quote_documents WHERE organization_id=:org
        """), {"org": organization}) != 1000:
            raise RuntimeError("P4 query trial requires 1000 stored synthetic quotes")


def _verify_missing_revision_semantics(
    owner: Engine, organization: uuid.UUID,
) -> dict[str, int]:
    """Rollback-only orphan-head case; no constraints or triggers disabled.

    Quote documents currently permit a head without a matching revision while
    the write transaction is in progress. Taking LIMIT before JOIN must not
    silently return 49 instead of 50 when such a head is present.
    """
    _assert_clone_owner(owner, organization)
    with owner.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("""
                INSERT INTO public.quote_documents (
                    id,organization_id,current_revision,created_at
                ) VALUES (
                    :id,:org,1,clock_timestamp() + interval '1 day'
                )
            """), {"id": uuid.uuid4(), "org": organization})
            outcome = {
                label: len(_row_fingerprints(connection, sql, organization))
                for label, sql in _VARIANTS.items()
            }
            if outcome != {
                "baseline": 50,
                "fast_prelimit": 49,
                "safe_prelimit": 50,
                "lateral_lookup": 50,
                "guarded_prelimit": 50,
            }:
                raise AssertionError(
                    "P4 pre-limit malformed-head equivalence assumption changed"
                )
            baseline = _row_fingerprints(connection, _BASELINE, organization)
            for name in ("safe_prelimit", "lateral_lookup", "guarded_prelimit"):
                if _row_fingerprints(connection, _VARIANTS[name], organization) != baseline:
                    raise AssertionError(
                        f"Safe variant {name} differs with orphan quote head"
                    )
            return outcome
        finally:
            # An orphan document must never be committed even to a test clone.
            transaction.rollback()


def compare_quote_list_sql(
    *, owner: Engine, tenant: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, session_token: str,
) -> dict[str, Any]:
    """Compare query plans and complete ordered records under actual tenant RLS."""
    _assert_clone_owner(owner, organization)
    malformed_head = _verify_missing_revision_semantics(owner, organization)
    evidence: dict[str, list[dict[str, object]]] = {
        key: [] for key in _VARIANTS
    }
    expected: tuple[tuple[object, ...], ...] | None = None
    # Rotate ordering across samples to reduce systematic warmed-query advantage.
    labels = tuple(_VARIANTS)
    for sample in range(_REPETITIONS):
        with tenant.begin() as connection:
            bind_authenticated_tenant(
                connection, session_token=session_token,
                organization_id=organization, operation="read",
            )
            baseline = _row_fingerprints(connection, _BASELINE, organization)
            if len(baseline) != 50:
                raise AssertionError("P4 query baseline did not return 50 records")
            if expected is None:
                expected = baseline
            elif baseline != expected:
                raise AssertionError("Quote list order is unstable")
            # An already-bound tenant session cannot read outside its own RLS scope
            # simply by supplying a different organization parameter.
            for label in labels:
                if _row_fingerprints(connection, _VARIANTS[label], outsider):
                    raise AssertionError("P4 quote query leaked outsider tenant rows")
            for offset in range(len(labels)):
                label = labels[(sample + offset) % len(labels)]
                statement = _VARIANTS[label]
                actual = _row_fingerprints(connection, statement, organization)
                if actual != expected:
                    raise AssertionError(
                        f"P4 quote variant {label} changed ordered results"
                    )
                plan = _plan_summary(connection.scalar(text(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement.text
                ), {"org": organization}))
                if plan["actual_rows"] != 50:
                    raise AssertionError(f"P4 quote variant {label} plan row count")
                evidence[label].append(plan)

    summaries: dict[str, dict[str, object]] = {}
    for label, plans in evidence.items():
        durations = []
        index_names: set[str] = set()
        blocks: list[int] = []
        for plan in plans:
            duration = plan["execution_ms"]
            indexes = plan["index_names"]
            buffers = plan["shared_hit_blocks"]
            if not isinstance(duration, (int, float)):
                raise TypeError("P4 EXPLAIN duration must be numeric")
            if not isinstance(indexes, list) or any(
                not isinstance(name, str) for name in indexes
            ):
                raise TypeError("P4 EXPLAIN index list malformed")
            if not isinstance(buffers, int):
                raise TypeError("P4 EXPLAIN buffers malformed")
            durations.append(float(duration))
            index_names.update(indexes)
            blocks.append(buffers)
        summaries[label] = {
            "samples": len(plans),
            "sql_median_ms": round(statistics.median(durations), 3),
            "sql_p95_ms": _quantile(durations, .95),
            "buffer_hits_median": statistics.median(blocks),
            "plan_index_names": sorted(index_names),
        }
    return {
        "kind": "p4_quote_list_relational_sql_comparison",
        "scope": "1000 synthetic documents, real tenant RLS / HTTPS app unchanged",
        "repetitions_per_variant": _REPETITIONS,
        "all_ordered_rows_and_values_equal": True,
        "cross_tenant_query_parameters_return_zero_rows": True,
        "orphan_head_in_uncommitted_clone_transaction": malformed_head,
        "unsafe_fast_prelimit_must_not_be_deployed": True,
        "variants": summaries,
        "notes": "EXPLAIN ANALYZE cost includes actual work but not FastAPI HTTP overhead. Simulated orphan is transaction-rollback only. No production SQL changes.",
    }
