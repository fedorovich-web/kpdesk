from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pytest

from kpdesk.matching import runtime
from kpdesk.matching.artifact import TrustedModel
from kpdesk.matching.rank import rank_candidates as original_rank
from kpdesk.matching.retrieve import retrieve_candidates as original_retrieve
from kpdesk.matching.schema import Candidate, CatalogProduct, RequestedItem
from scripts import audit_ai_runtime_quality as audit


def test_metrics_exclude_no_match_from_positive_denominators() -> None:
    rows = [
        audit.DiagnosticPrediction("positive-hit", frozenset({"A"}), ("A", "B"), 1.0),
        audit.DiagnosticPrediction("positive-miss", frozenset({"C"}), ("B",), 2.0),
        audit.DiagnosticPrediction("no-match-empty", frozenset(), (), 3.0),
        audit.DiagnosticPrediction("no-match-suggested", frozenset(), ("A",), 4.0),
    ]
    result = audit.score_predictions(rows)
    assert result["cases"] == 4
    assert result["positive_cases"] == 2
    assert result["top1"] == {"hits": 1, "denominator": 2, "value": 0.5}
    assert result["recall5"]["value"] == 0.5
    assert result["recall5"]["gold_skus"] == 2
    assert result["no_match"] == {
        "cases": 2, "nonempty_suggestions": 1, "empty_suggestions": 1,
        "nonempty_suggestion_rate": 0.5,
    }
    assert result["automatic_acceptance_measured"] is False


def test_all_no_match_and_empty_cases_do_not_report_false_perfect_accuracy() -> None:
    batches: list[list[audit.DiagnosticPrediction]] = [
        [], [audit.DiagnosticPrediction("negative", frozenset(), (), 1.0)],
    ]
    for rows in batches:
        result = audit.score_predictions(rows)
        assert result["top1"]["value"] is None
        assert result["recall5"]["value"] is None
        assert result["top1"]["denominator"] == 0


def test_recall_counts_multiple_relevant_skus_without_changing_query_denominator() -> None:
    measured = audit.score_predictions([
        audit.DiagnosticPrediction("multi", frozenset({"A", "B"}), ("A",), 1.0),
    ])
    assert measured["top1"]["value"] == 1.0
    assert measured["recall5"]["retrieved_gold_skus"] == 1
    assert measured["recall5"]["gold_skus"] == 2
    assert measured["recall5"]["value"] == 0.5


def test_duplicate_query_or_predicted_sku_is_rejected() -> None:
    row = audit.DiagnosticPrediction("q", frozenset({"A"}), ("A",), 1.0)
    with pytest.raises(ValueError, match="Duplicate"):
        audit.score_predictions([row, row])
    with pytest.raises(ValueError, match="Duplicate"):
        audit.score_predictions([
            audit.DiagnosticPrediction("q", frozenset({"A"}), ("A", "A"), 1.0),
        ])


def test_audit_uses_actual_runtime_model_and_same_retrieval_pool_without_training(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = runtime.load_local_ml_models()
    pools: list[Sequence[Candidate]] = []
    ml_calls: list[object] = []

    def no_fit(*args: object, **kwargs: object) -> None:
        raise AssertionError("Audit must not train a model")

    def retrieve(
        request: RequestedItem, products: Sequence[CatalogProduct], limit: int = 20,
    ) -> list[Candidate]:
        pool = original_retrieve(request, products, limit)
        pools.append(pool)
        return pool

    def rank(
        request: RequestedItem, pool: Sequence[Candidate],
        mode: Literal['rapidfuzz', 'tfidf', 'ml'], artifact: TrustedModel | None = None,
    ) -> list[Candidate]:
        assert pool is pools[-1]
        if mode == "ml":
            assert artifact is models.reranker
            ml_calls.append(artifact)
        return original_rank(request, pool, mode, artifact)

    monkeypatch.setattr(audit, "load_local_ml_models", lambda: models)
    monkeypatch.setattr(audit, "retrieve_candidates", retrieve)
    monkeypatch.setattr(audit, "rank_candidates", rank)
    monkeypatch.setattr(models.reranker.pipeline, "fit", no_fit)
    monkeypatch.setattr("kpdesk.matching.training.train_reranker", no_fit)
    report = audit.build_report()
    assert len(pools) == 35 and len(ml_calls) == 35
    assert report["production_quality_confirmed"] is False
    assert report["llm"]["quality_status"] == "not_measured"
    assert report["runtime"]["reranker"]["sha256"] == runtime.RERANKER_SHA256
    assert report["runtime"]["reranker"]["loaded_training_data_kind"] == "synthetic_example"
    assert report["dataset"]["independent_human_labels"] is False
    assert report["dataset"]["blind_holdout"] is False
    for mode in ("rapidfuzz", "tfidf", "ml"):
        test = report["modes"][mode]["original_splits"]["test"]
        assert test["cases"] == 12
        assert test["positive_cases"] == 5
        assert test["no_match"]["cases"] == 7
        assert report["modes"][mode]["constraint_violations"] == 0


def test_llm_summary_exposes_only_configured_boolean_and_allowlisted_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "secret-test-provider-key"
    (tmp_path / ".env").write_text(
        f"OPENROUTER_API_KEY={key}\nOPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free\n",
        encoding="utf-8",
    )
    summary = audit.llm_configuration(tmp_path / ".env")
    assert summary == {
        "provider": "openrouter", "configured": True, "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "configuration_source": "process_env_or_root_dotenv_via_LLMSettings.from_local_env",
        "quality_status": "not_measured", "provider_calls": 0,
    }
    assert key not in json.dumps(summary)
    (tmp_path / ".env").write_text(
        f"OPENROUTER_API_KEY={key}\nOPENROUTER_MODEL={key}\n", encoding="utf-8",
    )
    invalid = audit.llm_configuration(tmp_path / ".env")
    assert invalid["configured"] is False and invalid["model"] is None
    assert key not in json.dumps(invalid)


def test_narrative_claim_probe_is_labeled_validator_boundary_only() -> None:
    probe = audit.narrative_boundary_probe()
    assert probe["kind"] == "validator_boundary"
    assert probe["unsupported_claim_accepted_by_schema"] is True
    assert probe["provider_behavior_measured"] is False


def test_cli_output_is_create_only_and_contains_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit, "build_report", lambda: {"production_quality_confirmed": False})
    target = tmp_path / "audit.json"
    assert audit.main(["--output", str(target)]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"production_quality_confirmed": False}
    with pytest.raises(SystemExit):
        audit.main(["--output", str(target)])
