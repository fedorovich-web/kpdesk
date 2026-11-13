"""A76 reproducible local-only SQL benchmark on the marked disposable clone.

Synthetic fixed 5,000 source-line decisions; measurements are exploratory
local SQL timings, NOT SLA or claims about real supplier files.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import time
import uuid
from collections.abc import Callable

from canonical_checkpoint_acceptance import PUT
from sqlalchemy import Engine, text

from kpdesk.api.tenant_supplier_checkpoint_page_routes import _PAGE, _PROBE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant

_LEGACY_PAGE = text("""
    SELECT checkpoint_id,reviewer_user_id,previous_id,taxonomy_version,
           price_basis,total_count,decisions
    FROM public.a75_checkpoint_page(
      :org,:supplier,:source,:mapping,:taxonomy,:basis,:offset,:limit
    )
""")


def _sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def verify_checkpoint_benchmark(
    tenant: Engine, organization: uuid.UUID, session: IssuedSession,
    *, output: Callable[[str], None] = print,
) -> None:
    scope = {
        "org": organization, "supplier": "supplier-a76-benchmark",
        "source": _sha("a76-5000-synthetic-source"),
        "mapping": _sha("a76-5000-synthetic-mapping"),
        "taxonomy": "a76-local-benchmark-v1", "basis": "standard",
    }
    samples: list[dict[str, object]] = []
    previous: uuid.UUID | None = None
    with tenant.begin() as connection:
        bind_authenticated_tenant(
            connection, session_token=session.session_token,
            organization_id=organization, operation="write", csrf=session.csrf_token,
        )
        for batch in range(100):
            decisions = [
                {
                    "line": number, "source_row_sha256": _sha(f"a76-row-{number}"),
                    "action": "exclude", "confirmed": True,
                    "reason": "Bench fixture: synthetic catalogue heading, not a product.",
                }
                for number in range(batch * 50 + 1, batch * 50 + 51)
            ]
            checkpoint = connection.scalar(PUT, {
                **scope, "idempotency": f"a76-batch-{batch:04d}",
                "expected": previous, "decisions": decisions,
            })
            assert isinstance(checkpoint, uuid.UUID)
            previous = checkpoint
            count = (batch + 1) * 50
            if count not in (50, 500, 2500, 5000):
                continue

            expected_lines = [1, count + 1]
            # Warm both paths once before taking small median samples.
            probe = connection.execute(
                _PROBE, {**scope, "lines": expected_lines},
            ).one()
            assert probe.total_count == count
            assert probe.overlap_count == 1
            assert probe.checkpoint_id == previous
            assert connection.execute(
                _PAGE, {**scope, "offset": 0, "limit": 50},
            ).one().total_count == count

            def timed(action: Callable[[], object]) -> float:
                start = time.perf_counter_ns()
                action()
                return (time.perf_counter_ns() - start) / 1_000_000

            def single_probe(
                test_count: int = count,
                line_values: tuple[int, ...] = tuple(expected_lines),
            ) -> None:
                row = connection.execute(
                    _PROBE, {**scope, "lines": list(line_values)},
                ).one()
                assert row.total_count == test_count and row.overlap_count == 1

            def legacy_page_scan(
                test_count: int = count, snapshot: uuid.UUID = checkpoint,
            ) -> None:
                seen: set[int] = set()
                for offset in range(0, test_count, 50):
                    row = connection.execute(
                        _LEGACY_PAGE, {**scope, "offset": offset, "limit": 50},
                    ).one()
                    assert row.checkpoint_id == snapshot
                    seen.update(item["line"] for item in row.decisions)
                assert len(seen) == test_count

            probe_ms = statistics.median(timed(single_probe) for _ in range(3))
            scan_ms = statistics.median(timed(legacy_page_scan) for _ in range(3))
            samples.append({
                "rows": count, "revisions": batch + 1,
                "old_query_count": count // 50, "new_query_count": 1,
                "probe_ms_median": round(probe_ms, 2),
                "paged_scan_ms_median": round(scan_ms, 2),
            })

        # The tenant runtime role deliberately has no direct table SELECT.
        # Index plans and physical row counts are checked separately using
        # the clone schema owner, never by widening tenant permissions.
        final = connection.execute(
            _PROBE, {**scope, "lines": [1, 5000]},
        ).one()
        assert final.total_count == 5000 and final.overlap_count == 2

    assert [row["rows"] for row in samples] == [50, 500, 2500, 5000]
    output("A76_SQL_BENCHMARK " + json.dumps({
        "environment": "local disposable PostgreSQL clone, synthetic decisions",
        "samples": samples,
        "direct_table_access_by_tenant_role": False,
    }, ensure_ascii=False, sort_keys=True))
