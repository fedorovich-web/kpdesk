"""Portfolio evidence and local microbenchmark guards.

These tests never retrain a model and never reuse a sealed holdout for tuning.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.benchmark_portfolio_workflow import percentile_ms, run_scenario, synthetic_csv
from scripts.portfolio_ml_evidence import _metrics, build_evidence

ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_fixture_is_deterministic_and_bounded() -> None:
    assert synthetic_csv(25) == synthetic_csv(25)
    with pytest.raises(ValueError):
        synthetic_csv(5001)
    with pytest.raises(ValueError):
        percentile_ms([], 0.95)
    assert percentile_ms([1.0, 3.0, 4.0, 2.0, 5.0], 0.8) == 4.0


def test_microbenchmark_runs_genuine_parser_ranker_and_quote() -> None:
    result = run_scenario(25, 5)
    assert result.sku_count == 25
    assert result.result_lines == 5
    assert result.result_total_rub == "3990.00"
    assert result.incorrectly_compatible == 0
    assert result.sql_queries == 0
    assert result.match_p95_ms >= result.match_p50_ms >= 0


def test_saved_synthetic_ml_evidence_sha_and_counter_integrity() -> None:
    result = build_evidence(ROOT)
    assert result["production_eligible"] is False
    assert result["llm_heldout_metrics"] is None
    assert result["catalog_match_recall_at_5"] is None
    assert result["best_by_correct_accepted_not_production_approval"] == "hybrid_a46"


def test_saved_ml_counter_inconsistency_is_rejected() -> None:
    with pytest.raises(ValueError, match="Inconsistent stored ML counters"):
        _metrics({
            "accepted": 2, "abstained": 1, "correct_accepted": 2,
            "incorrect_accepted": 1, "positive_queries": 3,
            "unknown_queries": 2, "unknown_rejected": 2,
            "accepted_precision": 1.0,
        })
