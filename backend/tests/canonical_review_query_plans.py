"""A93 physical index order and actual query plans on a disposable PG clone.

All decisions are synthetic; timings are diagnostic, not latency guarantees.
This helper must never be invoked against a persistent/production database.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import cast

from sqlalchemy import Engine, text

from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

_KEY_PREFIX = (
    "organization_id", "supplier_id", "source_sha256",
    "mapping_sha256", "taxonomy_version", "price_basis",
)
_INDEXES = {
    "a72_scope_latest_idx": (
        *_KEY_PREFIX, "created_at desc", "checkpoint_id desc",
    ),
    "a77_current_head_pkey": _KEY_PREFIX,
    "a77_current_row_pkey": (*_KEY_PREFIX, "source_line"),
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plan_summary(document: object) -> dict[str, object]:
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, list) or not document:
        raise AssertionError("EXPLAIN did not return JSON plan data.")
    result = document[0]
    if not isinstance(result, dict) or not isinstance(result.get("Plan"), dict):
        raise TypeError("EXPLAIN lacks a root plan.")
    root = result["Plan"]
    indexes: list[str] = []

    def walk(node: dict[str, object]) -> None:
        index = node.get("Index Name")
        if isinstance(index, str):
            indexes.append(index)
        children = node.get("Plans", ())
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    walk(child)

    walk(root)
    elapsed = result.get("Execution Time")
    assert isinstance(elapsed, (float, int)) and elapsed >= 0
    assert root.get("Actual Rows") is not None
    return {
        "root": root["Node Type"],
        "index_names": sorted(set(indexes)),
        "actual_rows": root["Actual Rows"],
        "execution_ms": round(elapsed, 3),
        "shared_hits": root.get("Shared Hit Blocks", 0),
        "shared_reads": root.get("Shared Read Blocks", 0),
    }


def verify_review_query_plans(owner: Engine, organization_id: uuid.UUID) -> None:
    """Check physical columns and sample plans at the 5000-decision stage."""
    with owner.begin() as connection:
        if (
            not is_live_accounts_http_clone(connection)
            or connection.scalar(text("SELECT current_user")) != "kpdesk"
            or connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            != "0030_tenant_llm_cache"
        ):
            raise RuntimeError("A93 EXPLAIN requires an isolated owner clone.")

        for name, expected in _INDEXES.items():
            columns = cast(list[str], connection.execute(text("""
                SELECT pg_get_indexdef(i.indexrelid, k.position, true)
                    || CASE WHEN (i.indoption[k.position - 1]::int & 1) <> 0
                            THEN ' DESC' ELSE '' END AS key
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class idx ON idx.oid=i.indexrelid
                JOIN pg_catalog.pg_namespace ns ON ns.oid=idx.relnamespace
                CROSS JOIN LATERAL generate_series(1,i.indnkeyatts) AS k(position)
                WHERE ns.nspname='public' AND idx.relname=:name
                ORDER BY k.position
            """), {"name": name}).scalars().all())
            assert tuple(column.lower() for column in columns) == expected, (name, columns)

        # Update optimizer statistics ONLY inside the disposable clone.
        connection.execute(text("ANALYZE public.a72_canonical_checkpoints"))
        connection.execute(text("ANALYZE public.a77_current_head"))
        connection.execute(text("ANALYZE public.a77_current_row"))
        scope = {
            "org": organization_id,
            "supplier": "supplier-a76-benchmark",
            "source": _sha("a76-5000-synthetic-source"),
            "mapping": _sha("a76-5000-synthetic-mapping"),
            "taxonomy": "a76-local-benchmark-v1",
            "basis": "standard",
        }
        queries = {
            "latest_checkpoint": text("""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT checkpoint_id FROM public.a72_canonical_checkpoints
                WHERE organization_id=:org AND supplier_id=:supplier
                  AND source_sha256=:source AND mapping_sha256=:mapping
                  AND taxonomy_version=:taxonomy AND price_basis=:basis
                ORDER BY created_at DESC,checkpoint_id DESC LIMIT 1
            """),
            "idempotency_lookup": text("""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT checkpoint_id FROM public.a72_canonical_checkpoints
                WHERE organization_id=:org AND supplier_id=:supplier
                  AND source_sha256=:source AND mapping_sha256=:mapping
                  AND idempotency_key='a76-batch-0099'
            """),
            "current_head": text("""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT checkpoint_id,total_count FROM public.a77_current_head
                WHERE organization_id=:org AND supplier_id=:supplier
                  AND source_sha256=:source AND mapping_sha256=:mapping
                  AND taxonomy_version=:taxonomy AND price_basis=:basis
            """),
            "paged_current_rows": text("""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT source_line,decision FROM public.a77_current_row
                WHERE organization_id=:org AND supplier_id=:supplier
                  AND source_sha256=:source AND mapping_sha256=:mapping
                  AND taxonomy_version=:taxonomy AND price_basis=:basis
                ORDER BY source_line LIMIT 50
            """),
        }
        plans = {
            name: _plan_summary(connection.scalar(query, scope))
            for name, query in queries.items()
        }
        assert [plans[name]["actual_rows"] for name in queries] == [1, 1, 1, 50]
        print("A93_SQL_QUERY_PLANS " + json.dumps({
            "scope": "disposable clone / 5000 synthetic decisions",
            "physical_index_order_verified": sorted(_INDEXES),
            "plans": plans,
        }, sort_keys=True))
