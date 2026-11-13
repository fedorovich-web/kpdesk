"""P4 performance metrics and plan parsing must fail on malformed input."""
from __future__ import annotations

import json

import pytest
from canonical_p4_performance import (
    _plan_summary,
    _quantile,
    _resident_mib,
    _sql_family,
    _sql_timing_summary,
)


def test_nearest_rank_latency_p50_p95_with_small_samples() -> None:
    values = [7.0, 3.0, 9.0, 1.0, 5.0]
    assert _quantile(values, .5) == 5
    assert _quantile(values, .95) == 9
    assert _quantile(values, 1) == 9
    assert _quantile([0], .95) == 0


@pytest.mark.parametrize("values,fraction", [
    ([], .95), ([1.0], 0.0), ([1.0], 1.2),
    ([-1.0], .95), ([float("nan")], .95), ([float("inf")], .95),
])
def test_bad_latency_statistics_refused(values: list[float], fraction: float) -> None:
    with pytest.raises(ValueError, match="Invalid latency"):
        _quantile(values, fraction)


def test_query_plan_exposes_real_index_and_buffers() -> None:
    plan = [{
        "Execution Time": 1.2345,
        "Plan": {
            "Node Type": "Limit", "Actual Rows": 1, "Shared Hit Blocks": 4,
            "Plans": [{
                "Node Type": "Index Scan", "Index Name": "quote_revisions_idx",
                "Actual Rows": 1, "Shared Read Blocks": 2,
            }],
        },
    }]
    result = _plan_summary(json.dumps(plan))
    assert result == {
        "root": "Limit",
        "index_names": ["quote_revisions_idx"],
        "actual_rows": 1,
        "execution_ms": 1.234,
        "shared_hit_blocks": 4,
        "shared_read_blocks": 0,
    }


@pytest.mark.parametrize("payload", [
    [], {}, [1], [{"Plan": []}], [{"Plan": {"Node Type": "Scan"}}],
])
def test_query_plan_rejects_malformed_evidence(payload: object) -> None:
    with pytest.raises((ValueError, TypeError), match="EXPLAIN"):
        _plan_summary(payload)


def test_rss_is_real_memory_metric_if_os_supports_it() -> None:
    observed = _resident_mib()
    assert observed is None or observed > 0


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT * FROM public.kpdesk_auth_session(:digest)", "session_validation"),
    ("SELECT public.kpdesk_session_tenant_allowed(:org,:action)", "tenant_membership"),
    ("SELECT current_database(), to_regprocedure('public.kpdesk_session_tenant_allowed(uuid,text)') FROM pg_catalog.pg_database", "database_environment"),
    ("SELECT set_config('kpdesk.session_sha256', :value, true)", "transaction_scope"),
    ("SET LOCAL ROLE kpdesk_tenant_runtime", "role_setup"),
    ("SELECT d.id FROM public.quote_documents d JOIN public.quote_revisions r ON true", "quote_list_join"),
    ("SELECT current_revision FROM quote_documents WHERE id=:quote", "quote_head"),
    ("SELECT snapshot FROM quote_revisions WHERE quote_id=:quote", "quote_revision"),
    ("SELECT 1", "other"),
])
def test_p4_sql_families_do_not_depend_on_bound_values(sql: str, expected: str) -> None:
    assert _sql_family(sql) == expected


def test_p4_sql_timing_summary_aggregates_without_query_text_or_secrets() -> None:
    result = _sql_timing_summary({
        ("tenant", "quote_list", "session_validation"): [1.2, 2.4, 0.6],
        ("tenant", "quote_pdf", "quote_revision"): [4.5],
    })
    session = result["tenant"]["quote_list"]["session_validation"]
    assert session == {
        "count": 3,
        "dbapi_execute_total_ms": 4.2,
        "dbapi_execute_p50_ms": 1.2,
        "dbapi_execute_p95_ms": 2.4,
        "dbapi_execute_max_ms": 2.4,
    }
    assert result["tenant"]["quote_pdf"]["quote_revision"]["count"] == 1
    assert "password" not in json.dumps(result)
    assert "SELECT" not in json.dumps(result)


def test_p4_sql_timing_summary_refuses_empty_group() -> None:
    with pytest.raises(ValueError, match="SQL timing group"):
        _sql_timing_summary({("tenant", "quote_list", "other"): []})
